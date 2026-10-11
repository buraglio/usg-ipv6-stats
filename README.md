# USG IPv6 Statistics

Research toolkit for measuring IPv4/IPv6 prefix deployment over time for US Government Autonomous Systems. Uses RouteViews BGP route table dumps as the primary data source, corroborated by CAIDA datasets. Motivated by [OMB Memorandum M-21-07](https://www.whitehouse.gov/wp-content/uploads/2020/11/M-21-07.pdf), which directed federal agencies to adopt IPv6-only networking by FY2025.

---

## Table of contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Typical workflow](#typical-workflow)
- [Make targets](#make-targets)
- [Corpus build](#corpus-build)
- [Pipeline: incremental run](#pipeline-incremental-run)
- [Pipeline: backfill](#pipeline-backfill)
- [Database schema](#database-schema)
- [Python API](#python-api)
- [Data sources](#data-sources)
- [Configuration variables](#configuration-variables)
- [Project layout](#project-layout)
- [Development](#development)

---

## Prerequisites

- Python 3.11 or later
- [uv](https://docs.astral.sh/uv/) — the only tool needed for environment management and running tasks

Install `uv` once:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Installation

```bash
git clone <repo>
cd usg-ipv6-stats
make setup
```

`make setup` runs `uv sync`, which creates a local `.venv`, installs all runtime and dev dependencies from `uv.lock`, and makes the `usg_ipv6` package importable. No manual `pip install` or `python -m venv` steps are needed.

---

## Typical workflow

The three-step sequence for a fresh analysis run:

```bash
# 1. Build (or refresh) the USG ASN corpus
make corpus

# 2. Process the most recent available RIB snapshot
make run

# 3. Query the database directly
uv run python -c "
import duckdb
conn = duckdb.connect('data/usg_ipv6.duckdb')
conn.execute('''
    SELECT af, COUNT(DISTINCT prefix) AS prefixes, COUNT(DISTINCT origin_asn) AS asns
    FROM prefix_records
    GROUP BY af ORDER BY af
''').fetchdf()
"
```

For historical analysis, replace step 2 with `make backfill`.

---

## Make targets

| Target | Description |
|--------|-------------|
| `make setup` | Install all dependencies via `uv sync` |
| `make corpus` | Build the USG ASN corpus (ARIN + CAIDA) |
| `make run` | Fetch and process the most recent RIB snapshot |
| `make backfill` | Process all RIB snapshots in a date range |
| `make test` | Run the full test suite |
| `make lint` | Syntax-check all production modules |

All targets accept variable overrides on the command line (see [Configuration variables](#configuration-variables)).

---

## Corpus build

The corpus maps ASN numbers to federal agencies and tiers. It is the filter that determines which BGP routes are stored as USG data.

```bash
make corpus
```

Or with explicit overrides:

```bash
make corpus DB_PATH=data/usg_ipv6.duckdb AS2ORG=data/raw/20240101.as-org2info.txt.gz
```

**What it does:**

1. Queries ARIN WHOIS REST API for ASNs registered to each of the 24 CFO Act agencies (seed handles in `usg_ipv6/corpus/federal_agencies.py`).
2. Downloads the most recent quarterly CAIDA AS2Org file if `AS2ORG` is not specified, or parses a local copy if it is.
3. Fuzzy-matches CAIDA organization names against the agency list (threshold: 85/100) to catch ASNs that ARIN registration alone would miss.
4. Deduplicates by ASN, keeping the most-specific tier when an ASN appears in multiple sources.
5. Writes `data/usg_asn_corpus.csv` and loads the `asn_corpus` table in the DuckDB file.

**Tier hierarchy (most to least specific):**

| Tier | Agencies |
|------|----------|
| `cfo_act` | 23 CFO Act civilian agencies |
| `dod` | Department of Defense |
| `federal_civilian` | Other federal entities (CAIDA-discovered only) |

**Confidence levels:**

| Level | Source |
|-------|--------|
| `high` | ARIN WHOIS (authoritative registration) |
| `medium` | CAIDA AS2Org fuzzy match |

**Corpus outputs:**

- `data/usg_asn_corpus.csv` — human-readable snapshot
- `data/usg_ipv6.duckdb` → `asn_corpus` table — used by the pipeline

**To refresh the corpus** without re-downloading AS2Org:

```bash
make corpus AS2ORG=data/raw/20240101.as-org2info.txt.gz
```

**To download AS2Org manually:**

```python
from usg_ipv6.corpus.caida import download_as2org
path = download_as2org("data/raw")
print(path)
```

---

## Pipeline: incremental run

Fetches and processes the most recent RIB snapshot not already in the database.

```bash
make run
```

Or targeting a specific collector:

```bash
make run COLLECTOR=route-views6
```

**What it does:**

1. Generates candidate RIB timestamps at 2-hour intervals over the past 7 days, most recent first.
2. Constructs the RouteViews archive URL for each candidate (`http://archive.routeviews.org/{collector}/bgpdata/{YYYY.MM}/RIBS/rib.{YYYYMMDD.HHMM}.bz2`).
3. Skips snapshots already recorded with `status='processed'` in the `snapshots` table.
4. Downloads the MRT bz2 file, validating `Content-Length` to detect partial downloads.
5. Parses the MRT TABLE_DUMP_V2 records with `mrtparse`, extracts `(prefix, origin_asn, prefix_len, af)` tuples, and filters out IPv4-mapped IPv6 prefixes (`::ffff:0:0/96`, `64:ff9b::/96`, `64:ff9b:1::/48`).
6. Writes an intermediate Parquet file, then loads only rows whose `origin_asn` appears in `asn_corpus` into the `prefix_records` table.
7. Updates the `snapshots` row to `status='processed'`.
8. Prints a JSON result: `{"processed": N}`.

**Output example:**

```json
{"processed": 1}
```

**Collectors available on RouteViews:**

The default is `route-views2` (the primary route-views collector). Other useful collectors:

| Collector | Notes |
|-----------|-------|
| `route-views2` | Default; full global table |
| `route-views6` | IPv6-only peers |
| `route-views.linx` | London peering exchange |
| `route-views.sg` | Singapore |

Full list at [archive.routeviews.org](http://archive.routeviews.org).

---

## Pipeline: backfill

Processes all available RIB snapshots in a date range. Skips any snapshot already recorded as processed.

```bash
make backfill SINCE=2021-01-01 UNTIL=2024-12-31
```

Or with all variables explicit:

```bash
make backfill \
  DB_PATH=data/usg_ipv6.duckdb \
  COLLECTOR=route-views2 \
  SINCE=2020-11-01 \
  UNTIL=2024-12-31
```

RouteViews publishes snapshots every 2 hours, so a full-year backfill is approximately 4,380 MRT files (~300–500 MB each compressed). Budget disk space and time accordingly. The pipeline skips already-processed snapshots, so backfills are resumable.

**Estimating backfill size:**

```python
from datetime import datetime
since = datetime(2021, 1, 1)
until = datetime(2024, 12, 31)
days = (until - since).days
snapshots = days * 12  # 12 two-hour slots per day
print(f"{snapshots:,} snapshots ({snapshots * 350 / 1024:.0f} GB uncompressed estimate)")
```

---

## Database schema

The analytical store is a single DuckDB file at `data/usg_ipv6.duckdb`. All tables are created automatically on first run.

### `asn_corpus`

| Column | Type | Description |
|--------|------|-------------|
| `asn` | INTEGER PK | Autonomous System Number |
| `org_name` | VARCHAR | Organization name from ARIN or CAIDA |
| `agency` | VARCHAR | Matched CFO Act agency name |
| `tier` | VARCHAR | `cfo_act`, `dod`, or `federal_civilian` |
| `arin_org_handle` | VARCHAR | ARIN organization handle (empty for CAIDA-only entries) |
| `confidence` | VARCHAR | `high` (ARIN) or `medium` (CAIDA fuzzy match) |
| `sources` | VARCHAR | Comma-separated: `arin`, `caida`, or both |
| `last_verified` | DATE | Date the record was last checked |
| `corpus_version` | VARCHAR | Corpus build date (YYYY-MM-DD) |

### `snapshots`

Tracks every MRT file that has been downloaded or attempted.

| Column | Type | Description |
|--------|------|-------------|
| `snapshot_ts` | TIMESTAMP | RIB timestamp (PK with `collector`) |
| `collector` | VARCHAR | RouteViews collector name (PK with `snapshot_ts`) |
| `source_url` | VARCHAR | Full download URL |
| `byte_count` | BIGINT | Bytes received |
| `status` | VARCHAR | `downloaded`, `processed`, or `failed` |
| `processed_at` | TIMESTAMP | When `status` was set to `processed` |

### `prefix_records`

One row per (snapshot, prefix, origin ASN) tuple — the core analytical table.

| Column | Type | Description |
|--------|------|-------------|
| `snapshot_ts` | TIMESTAMP | Source snapshot timestamp |
| `collector` | VARCHAR | Source collector |
| `peer_asn` | INTEGER | BGP peer ASN (0 in Phase 1 — not yet extracted) |
| `prefix` | VARCHAR | CIDR prefix string |
| `origin_asn` | INTEGER | Origin AS from AS_PATH |
| `prefix_len` | INTEGER | Prefix length |
| `af` | INTEGER | Address family: 4 or 6 |

### `caida_pfx2as`

Corroborating prefix-to-ASN data from CAIDA RouteViews pfx2as.

| Column | Type | Description |
|--------|------|-------------|
| `snapshot_date` | DATE | Date of the pfx2as file |
| `prefix` | VARCHAR | CIDR prefix string |
| `prefix_len` | INTEGER | Prefix length |
| `asn` | INTEGER | Origin ASN |
| `af` | INTEGER | Address family: 4 or 6 |

### `ark_reachability`

Reserved for CAIDA Ark IPv6 Topology reachability data (Phase 2).

| Column | Type | Description |
|--------|------|-------------|
| `measurement_date` | DATE | Date of measurement |
| `asn` | INTEGER | ASN |
| `ipv6_reachable` | BOOLEAN | Whether IPv6 was reachable |

**Example queries:**

```sql
-- IPv4 vs IPv6 prefix count per snapshot
SELECT snapshot_ts, af, COUNT(DISTINCT prefix) AS prefixes
FROM prefix_records
GROUP BY snapshot_ts, af
ORDER BY snapshot_ts, af;

-- Agencies sorted by IPv6 prefix count (most recent snapshot)
SELECT c.agency, pr.af, COUNT(DISTINCT pr.prefix) AS prefixes
FROM prefix_records pr
JOIN asn_corpus c ON pr.origin_asn = c.asn
WHERE pr.snapshot_ts = (SELECT MAX(snapshot_ts) FROM prefix_records)
GROUP BY c.agency, pr.af
ORDER BY af, prefixes DESC;

-- Trend: IPv6 adoption over time
SELECT DATE_TRUNC('month', snapshot_ts) AS month,
       COUNT(DISTINCT CASE WHEN af = 6 THEN prefix END) AS v6_prefixes,
       COUNT(DISTINCT CASE WHEN af = 4 THEN prefix END) AS v4_prefixes
FROM prefix_records
GROUP BY 1 ORDER BY 1;
```

---

## Python API

All functionality is importable as a library.

### Corpus

```python
from usg_ipv6.corpus.build import build_corpus
from usg_ipv6.corpus.caida import download_as2org, parse_as2org
from usg_ipv6.corpus.arin import fetch_asns_for_org

# Download latest AS2Org file and build corpus
as2org_path = download_as2org("data/raw")
csv_path = build_corpus("data/usg_ipv6.duckdb", as2org_path, version="2024-01-01")

# Query ARIN directly
asns = fetch_asns_for_org("USGSA")  # returns [396260, ...]
```

### Pipeline

```python
from datetime import datetime
from usg_ipv6.pipeline.runner import run_incremental, run_backfill

# Process the most recent snapshot
result = run_incremental("data/usg_ipv6.duckdb", "route-views2", limit=1)
print(result)  # {"processed": 1}

# Backfill a range
result = run_backfill(
    "data/usg_ipv6.duckdb",
    "route-views2",
    since=datetime(2021, 1, 1),
    until=datetime(2024, 12, 31),
)
print(result)  # {"processed": N}
```

### Database

```python
import duckdb
from usg_ipv6.db import get_connection, init_schema

conn = get_connection("data/usg_ipv6.duckdb")
init_schema(conn)  # idempotent — safe to call on an existing DB

df = conn.execute("SELECT * FROM prefix_records LIMIT 10").fetchdf()
conn.close()
```

### Low-level pipeline steps

```python
from usg_ipv6.pipeline.routeviews import rib_url, fetch_rib
from usg_ipv6.pipeline.parse import parse_mrt
from usg_ipv6.pipeline.normalize import load_parquet
from usg_ipv6.pipeline.caida_pfx2as import download_pfx2as, load_pfx2as
from datetime import datetime

conn = get_connection("data/usg_ipv6.duckdb")

# Fetch a specific snapshot
url = rib_url("route-views2", datetime(2024, 3, 15, 8, 0))
mrt_path = fetch_rib(url, "data/raw", conn)       # returns path or None (skip/fail)

# Parse to Parquet
count = parse_mrt(mrt_path, "data/raw/out.parquet")

# Filter to corpus ASNs and load
loaded = load_parquet("data/raw/out.parquet", datetime(2024, 3, 15, 8, 0), "route-views2", conn)

# Load CAIDA pfx2as corroborating data
pfx2as_path = download_pfx2as("data/raw", "2024-03-15")
load_pfx2as(pfx2as_path, "2024-03-15", conn)
```

---

## Data sources

| Source | Used for | Access |
|--------|----------|--------|
| [RouteViews](http://archive.routeviews.org) | MRT RIB dumps (primary) | Public HTTP, no auth |
| [CAIDA RouteViews pfx2as](https://publicdata.caida.org/datasets/routing/routeviews-prefix2as/) | Prefix-to-ASN corroboration | Public HTTP, no auth |
| [CAIDA AS2Org](https://publicdata.caida.org/datasets/as-organizations/) | ASN-to-organization mapping | Public HTTP, no auth |
| [ARIN WHOIS REST API](https://www.arin.net/resources/registry/whois/rws/api/) | Authoritative ASN registration | Public REST API, no auth |

ARIN's API applies rate limits; the client backs off exponentially on HTTP 429 and emits a warning if all retries are exhausted.

---

## Configuration variables

All Makefile variables can be overridden on the command line.

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `data/usg_ipv6.duckdb` | Path to the DuckDB analytical store |
| `COLLECTOR` | `route-views2` | RouteViews collector name |
| `SINCE` | `2021-01-01` | Backfill start date (ISO 8601) |
| `UNTIL` | `2024-12-31` | Backfill end date (ISO 8601) |
| `AS2ORG` | `data/as2org.txt.gz` | Path to a local CAIDA AS2Org file; auto-downloaded if missing |

---

## Project layout

```text
usg_ipv6/
  db.py                  # DuckDB connection and schema initialisation
  corpus/
    federal_agencies.py  # Seed list: 24 CFO Act agencies and ARIN handles
    arin.py              # ARIN WHOIS REST API client
    caida.py             # CAIDA AS2Org downloader and parser
    schema.py            # AsnRecord dataclass, tier priority constants
    build.py             # Corpus builder: ARIN + CAIDA merge, CSV + DuckDB output
  pipeline/
    routeviews.py        # RouteViews URL construction and MRT downloader
    parse.py             # MRT TABLE_DUMP_V2 parser → Parquet
    normalize.py         # Parquet → prefix_records (filtered to corpus ASNs)
    caida_pfx2as.py      # CAIDA pfx2as downloader and loader
    runner.py            # Orchestrator: run_incremental, run_backfill

tests/
  conftest.py            # Shared pytest fixtures (in-memory DuckDB)
  corpus/                # Unit tests for corpus modules
  pipeline/              # Unit tests for pipeline modules
  test_db.py             # Schema tests
  test_integration.py    # End-to-end mocked pipeline smoke test
  fixtures/              # Sample AS2Org and pfx2as files for tests

data/                    # Runtime outputs (gitignored except .gitkeep)
  raw/                   # Downloaded MRT and CAIDA files (gitignored)
  usg_ipv6.duckdb        # Analytical store (gitignored)
  usg_asn_corpus.csv     # Corpus snapshot (generated by make corpus)

docs/
  superpowers/specs/     # Design specification
  superpowers/plans/     # Implementation plan
```

---

## Development

```bash
# Run the test suite
make test

# Syntax-check all production modules
make lint

# Run a specific test file
uv run pytest tests/pipeline/test_parse.py -v

# Open a DuckDB shell against the analytical store
uv run python -c "import duckdb; duckdb.connect('data/usg_ipv6.duckdb').execute('.tables')"
```

Dependencies are declared in `pyproject.toml` under `[dependency-groups] dev`. To add a new runtime dependency:

```bash
uv add <package>
```

To add a dev-only dependency:

```bash
uv add --dev <package>
```
