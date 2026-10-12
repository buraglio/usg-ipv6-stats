# USG IPv6 Statistics

Research toolkit for measuring IPv4/IPv6 prefix deployment over time for US Government Autonomous Systems. Uses RouteViews BGP route table dumps as the primary data source, corroborated by CAIDA datasets. Motivated by [OMB Memorandum M-21-07](https://www.whitehouse.gov/wp-content/uploads/2020/11/M-21-07.pdf), which directed federal agencies to adopt IPv6-only networking by FY2025.

**Live dashboard:** [buraglio.github.io/usg-ipv6-stats](https://buraglio.github.io/usg-ipv6-stats/) — rebuilt automatically after every ingest run.

---

## Table of contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Typical workflow](#typical-workflow)
- [CLI reference](#cli-reference)
- [Make targets](#make-targets)
- [GitHub Actions](#github-actions)
- [Corpus build](#corpus-build)
- [Pipeline: incremental run](#pipeline-incremental-run)
- [Pipeline: backfill](#pipeline-backfill)
- [Ark reachability](#ark-reachability)
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

`make setup` runs `uv sync`, which creates a local `.venv`, installs all runtime and dev dependencies from `uv.lock`, and registers the `usg` CLI command. No manual `pip install` or `python -m venv` steps are needed.

---

## Typical workflow

```bash
# 1. Build (or refresh) the USG ASN corpus
make corpus

# 2. Process the most recent available RIB snapshot (RouteViews BGP)
make run

# 3. Ingest the most recent CAIDA Ark IPv6 active-measurement file
make ark

# 4. Query with the CLI
uv run usg stats
uv run usg corpus --tier cfo_act
uv run usg prefixes --af 6
uv run usg ark stats
uv run usg ark gap
```

For historical BGP analysis, replace step 2 with `make backfill SINCE=2021-01-01 UNTIL=2024-12-31`.
For historical Ark analysis, replace step 3 with `make ark-backfill SINCE=2021-01-01 UNTIL=2024-12-31`.

---

## CLI reference

The `usg` command is the primary interface for querying the database and running the pipeline.

```text
uv run usg [COMMAND] [OPTIONS]
```

All query commands default `--db` to `data/usg_ipv6.duckdb` and accept `--json` for machine-readable output.

### `usg stats`

IPv4/IPv6 prefix and ASN summary for the most recent snapshot.

```bash
uv run usg stats
uv run usg stats --json
uv run usg stats --db /path/to/other.duckdb
```

Example output:

```text
Snapshot : 2026-10-12 14:00:00
Corpus   : 72 ASNs

All USG:
AF    Prefixes  ASNs
----  --------  ----
IPv4  740       35

By tier:
Tier     AF    Prefixes  ASNs
-------  ----  --------  ----
cfo_act  IPv4  740       35
```

### `usg corpus`

List ASNs in the corpus, with optional filters.

```bash
uv run usg corpus                          # all ASNs
uv run usg corpus --agency "State"         # partial agency name match
uv run usg corpus --tier cfo_act           # filter by tier
uv run usg corpus --asn 11974              # look up a single ASN
uv run usg corpus --json | jq '.[] | .asn' # pipe to jq
```

### `usg prefixes`

List distinct prefix records for a snapshot, optionally filtered.

```bash
uv run usg prefixes                        # latest snapshot, first 50 rows
uv run usg prefixes --af 6                 # IPv6 only
uv run usg prefixes --af 4 --tier cfo_act  # IPv4, CFO Act agencies
uv run usg prefixes --asn 270              # prefixes for a single ASN
uv run usg prefixes --limit 200            # increase row limit
uv run usg prefixes --snapshot "2024-03-15 08:00:00"  # a specific snapshot
```

### `usg snapshots`

List processed RIB snapshots.

```bash
uv run usg snapshots                          # most recent 50
uv run usg snapshots --collector route-views6 # specific collector
uv run usg snapshots --status processed       # only successful runs
uv run usg snapshots --limit 200
```

### `usg run ingest`

Fetch and process recent RIB snapshots (incremental).

```bash
uv run usg run ingest
uv run usg run ingest --collector route-views6
uv run usg run ingest --limit 6       # fetch up to 6 snapshots
```

### `usg run backfill`

Fetch and process RIB snapshots between two dates (inclusive). Already-processed snapshots are skipped automatically, so backfills are resumable.

```bash
uv run usg run backfill 2024-01-01 2024-12-31
uv run usg run backfill 2021-01-01 2024-12-31 --collector route-views6
```

### `usg ark ingest`

Download and ingest the CAIDA Ark IPv6 active-measurement file for a given date (or the most recent available if no date is given). Requires `caida_pfx2as` to be populated first (`make run` handles this).

```bash
uv run usg ark ingest                      # latest available
uv run usg ark ingest --date 2024-03-15   # specific date (walks back up to 6 days if not published)
```

### `usg ark backfill`

Ingest all daily Ark files in a date range. Already-loaded dates are skipped.

```bash
uv run usg ark backfill 2024-01-01 2024-12-31
uv run usg ark backfill 2021-01-01 2024-12-31
```

### `usg ark stats`

Reachability summary for the most recent Ark measurement date: counts of reachable, not-reachable, and indeterminate ASNs.

```bash
uv run usg ark stats
uv run usg ark stats --json
```

Example output:

```text
Ark measurement date : 2024-03-13
Reachable            : 38
Not reachable        : 12
Indeterminate        : 22  (no IPv6 pfx2as prefixes on record)
pfx2as snapshot      : 2024-03-15
```

### `usg ark gap`

List corpus ASNs that have IPv6 prefixes in pfx2as but zero Ark-observed addresses — i.e., agencies that advertise IPv6 routes but whose addresses are not reachable from CAIDA Ark vantage points.

```bash
uv run usg ark gap
uv run usg ark gap --json
```

---

## Make targets

| Target | Description |
| -------- | ------------- |
| `make setup` | Install all dependencies via `uv sync` |
| `make corpus` | Build the USG ASN corpus (ARIN + CAIDA) |
| `make run` | Fetch and process the most recent RIB snapshot |
| `make backfill` | Process all RIB snapshots in a date range |
| `make ark` | Download and ingest the most recent CAIDA Ark IPv6 file |
| `make ark-backfill` | Ingest all daily Ark files in a date range |
| `make test` | Run the full test suite |
| `make lint` | Syntax-check all production modules |

All targets accept variable overrides on the command line (see [Configuration variables](#configuration-variables)).

---

## GitHub Actions

Five workflows automate the full pipeline in CI. All require a GitHub token with `contents: write` (provided automatically by `GITHUB_TOKEN`).

### Corpus Update (`corpus-update.yml`)

Rebuilds the USG ASN corpus from ARIN WHOIS and CAIDA AS2Org.

- **Manual dispatch** (`workflow_dispatch`): commits `data/usg_asn_corpus.csv` directly to `main`. Use this for the initial bootstrap and on-demand refreshes.
- **Scheduled** (Sundays 06:00 UTC): opens a pull request so changes can be reviewed before merge.

ARIN WHOIS is blocked from GitHub Actions IP ranges; the workflow automatically falls back to CAIDA-only corpus building when ARIN is unreachable.

### RouteViews MRT Ingest (`routeviews-ingest.yml`)

Downloads BGP MRT RIB dumps from a RouteViews collector, parses TABLE_DUMP_V2 records, and stores IPv4/IPv6 prefix records in DuckDB.

- **Scheduled**: weekly, Monday 10:00 UTC
- **Manual dispatch**: accepts `collector` and `limit` inputs

The DuckDB file is too large for git; it lives in the rolling `data-latest` GitHub Release. Each run downloads the existing DB, appends new snapshots, and re-uploads.

**Pipeline steps:**

1. Reload USG ASN corpus from committed `data/usg_asn_corpus.csv` → `asn_corpus` table
2. Download latest CAIDA pfx2as (monthly cadence, skipped if current month already loaded) → `caida_pfx2as` table
3. `fetch_rib → parse_mrt → load_parquet` → `prefix_records` table

### RouteViews MRT Backfill (`routeviews-backfill.yml`)

Historical ingest over a user-supplied date range. Accepts `since`, `until`, and `collector` inputs. Runs up to the 6-hour GitHub Actions job limit; re-trigger to continue — processed snapshots are skipped automatically.

### Publish (`publish.yml`)

Runs on every push to `main`. Creates a versioned `corpus-YYYY-MM-DD` GitHub Release with `data/usg_asn_corpus.csv` attached.

### CAIDA Ark IPv6 Reachability Ingest (`ark-ingest.yml`)

Downloads the daily CAIDA Ark IPv6 `team-1.ipv6.YYYYMMDD.nodes.as.gz` file, re-annotates each observed address using our pfx2as LPM index, and writes per-ASN reachability verdicts to `ark_reachability`.

- **Scheduled**: Wednesdays 09:00 UTC (offset from RouteViews Monday run to allow pfx2as to be current)
- **Manual dispatch**: accepts `date` (single date), `since`/`until` (backfill range)

**Requires:** RouteViews MRT Ingest must have run first so `caida_pfx2as` is populated.

**What it does:**

1. Downloads the existing DuckDB from the `data-latest` release (`|| true` for first-run safety).
2. Looks up the most recent pfx2as snapshot date.
3. Loads all IPv6 (prefix, ASN) pairs for corpus ASNs and builds an in-memory LPM index.
4. Downloads and streams the Ark nodes-as file; for each observed IPv6 address, resolves the owning ASN via LPM and accumulates per-ASN counts.
5. Writes one `ark_reachability` row per corpus ASN: `ipv6_reachable=TRUE` if observed, `FALSE` if the ASN has IPv6 pfx2as prefixes but no observed addresses, or `prefixes_in_scope=0` if the ASN has no IPv6 prefixes on record (indeterminate).
6. Re-uploads the updated DuckDB and triggers the GitHub Pages rebuild.

### Deploy GitHub Pages (`pages.yml`)

Generates the static HTML dashboard and deploys to GitHub Pages.

- Triggers: after each RouteViews MRT Ingest run, on push to `main`, and on manual dispatch
- Downloads the DuckDB from `data-latest`, runs `usg_ipv6.report_html`, deploys `web/index.html`
- Degrades gracefully: if no DuckDB exists yet, deploys a placeholder page

**One-time setup:** Settings → Pages → Source → **GitHub Actions**

---

## Corpus build

The corpus maps ASN numbers to federal agencies and tiers. It is the filter that determines which BGP routes are stored as USG data.

```bash
make corpus
```

**What it does:**

1. Queries ARIN WHOIS REST API for ASNs registered to each of the 24 CFO Act agencies. If ARIN is unreachable (e.g. from GitHub Actions IP ranges), this step is skipped and only CAIDA data is used.
2. Downloads the most recent CAIDA AS2Org file (published on the 1st of each month). Walks back up to 6 months to find the latest available file.
3. Fuzzy-matches CAIDA organization names against the agency list (threshold: 85/100) to catch ASNs that ARIN registration alone would miss.
4. Deduplicates by ASN, keeping the most-specific tier when an ASN appears in multiple sources.
5. Writes `data/usg_asn_corpus.csv` and loads the `asn_corpus` table in the DuckDB file.

**Tier hierarchy (most to least specific):**

| Tier | Agencies |
| ------ | ---------- |
| `cfo_act` | 24 CFO Act civilian agencies |
| `dod` | Department of Defense |
| `federal_civilian` | Other federal entities (CAIDA-discovered only) |

**Confidence levels:**

| Level | Source |
| ------- | -------- |
| `high` | ARIN WHOIS (authoritative registration) |
| `medium` | CAIDA AS2Org fuzzy match |

**To supply a local AS2Org file instead of downloading:**

```bash
make corpus AS2ORG=data/raw/20241001.as-org2info.txt.gz
```

**To download AS2Org manually:**

```python
from usg_ipv6.corpus.caida import download_as2org
path = download_as2org("data/raw")   # probes the last 6 months, takes the most recent
print(path)
```

---

## Pipeline: incremental run

Fetches and processes the most recent RIB snapshot not already in the database.

```bash
make run
make run COLLECTOR=route-views6
```

**What it does:**

1. Generates candidate RIB timestamps at 2-hour intervals over the past 7 days, most recent first.
2. Skips snapshots already recorded with `status='processed'` in the `snapshots` table.
3. Downloads the MRT bz2 file from the RouteViews archive.
4. Parses TABLE_DUMP_V2 records with `mrtparse`, extracts `(prefix, origin_asn, prefix_len, af)` tuples, filters IPv4-mapped IPv6 prefixes, and writes an intermediate Parquet file.
5. Loads only rows whose `origin_asn` appears in `asn_corpus` into `prefix_records`.

**Available collectors:**

| Collector | Notes |
| ----------- | ------- |
| `route-views2` | Default; full global table |
| `route-views6` | IPv6-only peers |
| `route-views.linx` | London peering exchange |
| `route-views.sg` | Singapore |

Full list at [archive.routeviews.org](http://archive.routeviews.org).

---

## Pipeline: backfill

Processes all available RIB snapshots in a date range. Already-processed snapshots are skipped, so backfills are resumable.

```bash
make backfill SINCE=2021-01-01 UNTIL=2024-12-31
make backfill SINCE=2021-01-01 UNTIL=2024-12-31 COLLECTOR=route-views6
```

RouteViews publishes snapshots every 2 hours (12 per day). A full year is ~4,380 MRT files.

---

## Ark reachability

CAIDA's Ark project publishes daily `team-1.ipv6.YYYYMMDD.nodes.as.gz` files listing every IPv6 address observed by their global vantage-point network, annotated with Ark's own AS annotation. This pipeline cross-references those observations against our pfx2as-derived LPM index to produce per-agency reachability verdicts.

**Prerequisite:** `caida_pfx2as` must be populated. Running `make run` (or the RouteViews ingest GitHub Action) loads pfx2as automatically.

```bash
# Ingest the most recent file
make ark

# Backfill a date range (skips dates already in the DB)
make ark-backfill SINCE=2023-01-01 UNTIL=2024-12-31

# Check reachability summary
uv run usg ark stats

# Show the gap: agencies advertising IPv6 but not reachable from Ark
uv run usg ark gap
```

**Attribution approach (Option C hybrid):** Each Ark address is looked up in our pfx2as LPM index to derive `our_asn`. Ark's embedded annotation (`ark_asn`) is also recorded. Divergences between the two (where Ark and pfx2as disagree on the owning ASN) are counted per ASN in `asn_divergence_count` — a publishable finding about BGP/routing-layer consistency.

**Reachability categories:**

| Category | Meaning |
| ----------- | ------- |
| Reachable | `ipv6_reachable=TRUE` — Ark observed ≥1 address in this ASN's IPv6 prefixes |
| Not reachable | `ipv6_reachable=FALSE`, `prefixes_in_scope>0` — ASN has IPv6 prefixes but none were observed |
| Indeterminate | `prefixes_in_scope=0` — ASN has no IPv6 prefixes in the pfx2as snapshot; reachability cannot be assessed |

**Lookback:** If the requested date's file is not yet published, the downloader walks back up to 6 days to find the most recent available file. The stored `measurement_date` always reflects the filename date, not the requested date.

---

## Database schema

The analytical store is a single DuckDB file at `data/usg_ipv6.duckdb`. All tables are created automatically on first run. ASN columns use `BIGINT` to accommodate the full 32-bit unsigned ASN range (0–4,294,967,295).

### `asn_corpus`

| Column | Type | Description |
| -------- | ------ | ------------- |
| `asn` | BIGINT PK | Autonomous System Number |
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
| -------- | ------ | ------------- |
| `snapshot_ts` | TIMESTAMP | RIB timestamp (PK with `collector`) |
| `collector` | VARCHAR | RouteViews collector name (PK with `snapshot_ts`) |
| `source_url` | VARCHAR | Full download URL |
| `byte_count` | BIGINT | Bytes received |
| `status` | VARCHAR | `downloaded`, `processed`, or `failed` |
| `processed_at` | TIMESTAMP | When `status` was set to `processed` |

### `prefix_records`

One row per (snapshot, peer, prefix, origin ASN) — the core analytical table.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `snapshot_ts` | TIMESTAMP | Source snapshot timestamp |
| `collector` | VARCHAR | Source collector |
| `peer_asn` | BIGINT | BGP peer ASN (0 in current build — reserved) |
| `prefix` | VARCHAR | CIDR prefix string |
| `origin_asn` | BIGINT | Origin AS from AS_PATH |
| `prefix_len` | INTEGER | Prefix length |
| `af` | INTEGER | Address family: 4 or 6 |

### `caida_pfx2as`

Corroborating prefix-to-ASN mapping from CAIDA RouteViews pfx2as.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `snapshot_date` | DATE | Date of the pfx2as file |
| `prefix` | VARCHAR | CIDR prefix string |
| `prefix_len` | INTEGER | Prefix length |
| `asn` | BIGINT | Origin ASN |
| `af` | INTEGER | Address family: 4 or 6 |

### `ark_reachability`

One row per (measurement date, ASN). Written by `make ark` / `usg ark ingest`.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `measurement_date` | DATE PK | Date of the Ark measurement file |
| `asn` | BIGINT PK | Autonomous System Number |
| `ipv6_reachable` | BOOLEAN | TRUE if ≥1 address was observed; FALSE otherwise |
| `addresses_observed` | INTEGER | Count of IPv6 addresses seen in this ASN's prefixes |
| `prefixes_in_scope` | INTEGER | Count of IPv6 pfx2as prefixes for this ASN (0 = indeterminate) |
| `pfx2as_snapshot_date` | DATE | Which pfx2as snapshot was used for attribution |
| `asn_divergence_count` | INTEGER | Addresses where Ark's ASN annotation differs from our pfx2as LPM |

### `ark_prefix_observations`

One row per (measurement date, prefix). Provides per-prefix granularity for divergence analysis.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `measurement_date` | DATE PK | Date of the Ark measurement file |
| `prefix` | VARCHAR PK | IPv6 CIDR prefix (from pfx2as) |
| `our_asn` | BIGINT | ASN from our pfx2as LPM index |
| `addresses_observed` | INTEGER | Count of Ark addresses that fell within this prefix |
| `addresses_ark_match` | INTEGER | Count where Ark's annotation agreed with our LPM |
| `pfx2as_snapshot_date` | DATE | Which pfx2as snapshot was used |

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

-- Trend: IPv6 adoption over time (monthly)
SELECT DATE_TRUNC('month', snapshot_ts) AS month,
       COUNT(DISTINCT CASE WHEN af = 6 THEN prefix END) AS v6_prefixes,
       COUNT(DISTINCT CASE WHEN af = 4 THEN prefix END) AS v4_prefixes
FROM prefix_records
GROUP BY 1 ORDER BY 1;

-- ASNs announcing IPv4 but not IPv6 (potential adoption gap)
SELECT c.agency, c.asn, c.org_name
FROM asn_corpus c
WHERE c.asn IN (
    SELECT DISTINCT origin_asn FROM prefix_records
    WHERE af = 4 AND snapshot_ts = (SELECT MAX(snapshot_ts) FROM prefix_records)
)
AND c.asn NOT IN (
    SELECT DISTINCT origin_asn FROM prefix_records
    WHERE af = 6 AND snapshot_ts = (SELECT MAX(snapshot_ts) FROM prefix_records)
)
ORDER BY c.agency;

-- Ark reachability summary for the most recent measurement
SELECT
    SUM(CASE WHEN ipv6_reachable = TRUE  THEN 1 ELSE 0 END) AS reachable,
    SUM(CASE WHEN ipv6_reachable = FALSE AND prefixes_in_scope > 0 THEN 1 ELSE 0 END) AS not_reachable,
    SUM(CASE WHEN prefixes_in_scope = 0  THEN 1 ELSE 0 END) AS indeterminate
FROM ark_reachability
WHERE measurement_date = (SELECT MAX(measurement_date) FROM ark_reachability);

-- Gap: agencies with IPv6 prefixes but no Ark-observed addresses
SELECT ar.asn, c.agency, c.tier,
       ar.addresses_observed, ar.prefixes_in_scope, ar.asn_divergence_count
FROM ark_reachability ar
JOIN asn_corpus c ON ar.asn = c.asn
WHERE ar.measurement_date = (SELECT MAX(measurement_date) FROM ark_reachability)
  AND ar.ipv6_reachable = FALSE
  AND ar.prefixes_in_scope > 0
ORDER BY c.tier, c.agency;

-- ASN attribution divergence: where Ark and pfx2as disagree most
SELECT ar.asn, c.agency,
       ar.addresses_observed, ar.asn_divergence_count,
       ROUND(100.0 * ar.asn_divergence_count / ar.addresses_observed, 1) AS divergence_pct
FROM ark_reachability ar
JOIN asn_corpus c ON ar.asn = c.asn
WHERE ar.measurement_date = (SELECT MAX(measurement_date) FROM ark_reachability)
  AND ar.addresses_observed > 0
ORDER BY divergence_pct DESC;
```

---

## Python API

All functionality is importable as a library.

### Corpus

```python
from usg_ipv6.corpus.build import build_corpus
from usg_ipv6.corpus.caida import download_as2org
from usg_ipv6.corpus.arin import fetch_asns_for_org, arin_reachable

# Check ARIN reachability before attempting queries
if arin_reachable():
    asns = fetch_asns_for_org("USGSA")

# Download latest AS2Org file (probes last 6 months)
as2org_path = download_as2org("data/raw")
csv_path = build_corpus("data/usg_ipv6.duckdb", as2org_path, version="2024-01-01")
```

### RouteViews pipeline

```python
from datetime import datetime
from usg_ipv6.pipeline.runner import run_incremental, run_backfill

result = run_incremental("data/usg_ipv6.duckdb", "route-views2", limit=1)
print(result)  # {"processed": 1}

result = run_backfill(
    "data/usg_ipv6.duckdb",
    "route-views2",
    since=datetime(2021, 1, 1),
    until=datetime(2024, 12, 31),
)
print(result)  # {"processed": N}
```

### Ark pipeline

```python
from datetime import datetime
from usg_ipv6.pipeline.ark import run_ark_ingest, run_ark_backfill

# Ingest a single date (omit date= for today's file)
result = run_ark_ingest("data/usg_ipv6.duckdb", date="2024-03-15")
print(result)
# {"date": "2024-03-13", "addresses_parsed": 48210, "asns_updated": 38,
#  "pfx2as_snapshot_date": "2024-03-15", "skipped": False}
# Note: date in result reflects the filename (lookback may resolve an earlier date)

# Backfill a range (already-loaded dates are skipped)
result = run_ark_backfill(
    "data/usg_ipv6.duckdb",
    datetime(2024, 1, 1),
    datetime(2024, 12, 31),
)
print(result)  # {"processed": N, "skipped": K, "failed": 0}
```

### LPM index (low-level)

```python
import ipaddress
from usg_ipv6.pipeline.lpm import build_lpm_index, lpm_lookup, lpm_lookup_entry

index = build_lpm_index([("2001:db8::/32", 64496), ("2001:db8:1::/48", 64497)])

asn = lpm_lookup(ipaddress.IPv6Address("2001:db8:1::1"), index)
# 64497  (longest-prefix match)

entry = lpm_lookup_entry(ipaddress.IPv6Address("2001:db8::1"), index)
# (64496, "2001:db8::/32")
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

### Report generation

```python
from usg_ipv6.report_html import generate

generate("data/usg_ipv6.duckdb", "web/index.html")
```

---

## Data sources

| Source | Used for | Access |
| -------- | ---------- | -------- |
| [RouteViews](http://archive.routeviews.org) | MRT RIB dumps (primary) | Public HTTP, no auth |
| [CAIDA RouteViews pfx2as](https://publicdata.caida.org/datasets/routing/routeviews-prefix2as/) | Prefix-to-ASN corroboration and LPM attribution | Public HTTP, no auth |
| [CAIDA Ark IPv6](https://publicdata.caida.org/datasets/topology/ark/ipv6/probe-data/team-1/) | Active-measurement IPv6 reachability (daily) | Public HTTP, no auth |
| [CAIDA AS2Org](https://publicdata.caida.org/datasets/as-organizations/) | ASN-to-organization mapping (published 1st of each month) | Public HTTP, no auth |
| [ARIN WHOIS REST API](https://www.arin.net/resources/registry/whois/rws/api/) | Authoritative ASN registration | Public REST API, no auth |

**ARIN in CI:** ARIN WHOIS is unreachable from GitHub Actions IP ranges. A fast pre-flight check (`arin_reachable()`) detects this and skips ARIN queries rather than burning through retries, falling back to CAIDA-only corpus building.

---

## Configuration variables

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `DB_PATH` | `data/usg_ipv6.duckdb` | Path to the DuckDB analytical store |
| `COLLECTOR` | `route-views2` | RouteViews collector name |
| `SINCE` | `2021-01-01` | Backfill start date (ISO 8601) |
| `UNTIL` | `2024-12-31` | Backfill end date (ISO 8601) |
| `AS2ORG` | _(empty — auto-download)_ | Path to a local CAIDA AS2Org file; if unset, the most recent monthly file is downloaded automatically |

---

## Project layout

```text
usg_ipv6/
  db.py                  # DuckDB connection and schema initialisation
  cli.py                 # usg CLI (typer): stats, corpus, prefixes, snapshots, run
  report_html.py         # Static HTML report generator for GitHub Pages
  corpus/
    federal_agencies.py  # Seed list: 24 CFO Act agencies and ARIN handles
    arin.py              # ARIN WHOIS REST client + arin_reachable() pre-flight
    caida.py             # CAIDA AS2Org downloader and parser (monthly cadence)
    schema.py            # AsnRecord dataclass, tier priority constants
    build.py             # Corpus builder: ARIN + CAIDA merge, CSV + DuckDB output
  pipeline/
    routeviews.py        # RouteViews URL construction and MRT downloader
    parse.py             # MRT TABLE_DUMP_V2 parser → Parquet (int64 ASNs)
    normalize.py         # Parquet → prefix_records (filtered to corpus ASNs)
    caida_pfx2as.py      # CAIDA pfx2as downloader and loader
    runner.py            # Orchestrator: run_incremental, run_backfill
    lpm.py               # Stdlib-only longest-prefix-match engine (IPv6)
    ark.py               # CAIDA Ark downloader, parser, and ingest orchestrator

.github/workflows/
  corpus-update.yml      # Weekly corpus rebuild; manual dispatch commits to main
  routeviews-ingest.yml  # Weekly MRT ingest; updates data-latest release
  routeviews-backfill.yml# Manual historical backfill
  ark-ingest.yml         # Wednesday Ark IPv6 reachability ingest
  publish.yml            # Versioned corpus release on push to main
  pages.yml              # Static HTML dashboard → GitHub Pages

tests/
  conftest.py            # Shared pytest fixtures (in-memory DuckDB)
  corpus/                # Unit tests for corpus modules
  pipeline/              # Unit tests for pipeline modules
  test_db.py             # Schema tests
  test_integration.py    # End-to-end mocked pipeline smoke test

data/
  usg_asn_corpus.csv     # Committed corpus snapshot (used by CI to avoid ARIN)
  raw/                   # Downloaded MRT and CAIDA files (gitignored)
  usg_ipv6.duckdb        # Analytical store (gitignored; lives in data-latest release)
```

---

## Development

```bash
make test          # run the full test suite
make lint          # syntax-check all production modules

# Run a specific test file
uv run pytest tests/pipeline/test_parse.py -v

# Generate the HTML report locally
uv run python -m usg_ipv6.report_html   # writes web/index.html

# Open the DuckDB shell
uv run python -c "import duckdb; duckdb.connect('data/usg_ipv6.duckdb').sql('SHOW TABLES').show()"
```

To add a runtime dependency: `uv add <package>`
To add a dev-only dependency: `uv add --dev <package>`
