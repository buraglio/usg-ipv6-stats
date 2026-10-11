# USG IPv6 Data Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the USG ASN corpus tooling and MRT data pipeline, producing a versioned `usg_asn_corpus.csv` and a populated `usg_ipv6.duckdb` from RouteViews and CAIDA data.

**Architecture:** A Python package (`usg_ipv6/`) with two sub-modules — `corpus/` for building and maintaining the USG ASN list from ARIN WHOIS + CAIDA AS2Org, and `pipeline/` for fetching, parsing, and loading RouteViews MRT dumps into DuckDB. A Makefile drives the full workflow. Plan 2 adds the analysis/query layer, CLI, notebooks, and web UI on top of this foundation.

**Tech Stack:** Python 3.11+, uv (package manager), duckdb 0.10+, mrtparse 2.x, requests 2.31+, rapidfuzz 3.x, pyarrow 15+ (Parquet), pytest 8+, responses 0.25+ (HTTP mocking)

**Spec:** `docs/superpowers/specs/2026-10-11-usg-ipv6-stats-design.md`

## Global Constraints

- Python 3.11 minimum
- DuckDB file lives at `data/usg_ipv6.duckdb` (project-root-relative)
- Corpus CSV at `data/usg_asn_corpus.csv`
- Raw MRT downloads go to `data/raw/` — ephemeral, gitignored
- Processed Parquet files go to `data/processed/` — committed or uploaded to Releases
- Address family field: integer `4` or `6` (not string)
- Corpus version string: `YYYY-MM-DD` date of build
- All DuckDB writes use parameterized queries — no f-string SQL
- Tests must not make real network calls — all HTTP is mocked with the `responses` library
- IPv4-mapped IPv6 prefixes (`::ffff:0:0/96`, `64:ff9b::/96`) are filtered out and never stored as `af=6`
- An ASN appears in the corpus exactly once; its `tier` is the most specific applicable tier (`cfo_act` > `dod` > `federal_civilian`)

## Review Focus

1. **Partial MRT downloads** — a download interrupted mid-file produces a truncated MRT that mrtparse parses silently to EOF; the snapshot looks complete but `prefix_records` is missing data. The manifest must record the expected byte count (from `Content-Length`) and verify the downloaded file before marking it done. → Task 5, "Write the failing test" adds `test_partial_download_not_marked_done`.
2. **IPv4-mapped IPv6 prefixes** — prefixes like `::ffff:192.0.2.0/120` or `64:ff9b::/96` appear in some BGP tables but are not native IPv6 deployments and must be excluded from `af=6` records. → Task 6, "Write the failing test" adds `test_ipv4_mapped_prefixes_filtered`.
3. **ASN tier overlap** — an ASN legitimately associated with both DoD and a civilian agency must land in exactly one tier (most-specific wins) to prevent double-counting in aggregate queries. → Task 4, "Write the failing test" adds `test_tier_deduplication_most_specific_wins`.
4. **ARIN rate limiting** — ARIN's REST API returns 429 after rapid sequential requests; the fetcher must back off and retry. → Task 2, "Write the failing test" adds `test_rate_limit_triggers_retry`.
5. **CAIDA AS2Org format versioning** — the file includes a header specifying its format version; parsing must check and fail loudly on an unrecognised version rather than silently producing garbage records. → Task 3, "Write the failing test" adds `test_unknown_format_version_raises`.

---

### Task 1: Project scaffolding + DuckDB schema

**Files:**
- Create: `pyproject.toml`
- Create: `Makefile`
- Create: `usg_ipv6/__init__.py`
- Create: `usg_ipv6/db.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_db.py`
- Create: `.gitignore`

**Interfaces:**
- Produces: `get_connection(db_path: str) -> duckdb.DuckDBPyConnection` — used by every subsequent task
- Produces: `init_schema(conn: duckdb.DuckDBPyConnection) -> None` — idempotent, called on startup

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db.py
import duckdb
import pytest
from usg_ipv6.db import get_connection, init_schema

def test_init_schema_creates_tables(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    tables = conn.execute("SHOW TABLES").fetchall()
    names = {t[0] for t in tables}
    assert "asn_corpus" in names
    assert "snapshots" in names
    assert "prefix_records" in names
    assert "caida_pfx2as" in names
    assert "ark_reachability" in names

def test_init_schema_is_idempotent(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    init_schema(conn)  # must not raise
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_db.py -v
```

Expected: `ModuleNotFoundError: No module named 'usg_ipv6'`

- [ ] **Step 3: Create `pyproject.toml`**

```toml
[project]
name = "usg-ipv6-stats"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "duckdb>=0.10",
    "mrtparse>=2.0",
    "requests>=2.31",
    "rapidfuzz>=3.0",
    "pyarrow>=15.0",
    "typer>=0.12",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "responses>=0.25",
]

[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "responses>=0.25",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- [ ] **Step 4: Create directory structure and `__init__.py` files**

```bash
mkdir -p usg_ipv6/corpus usg_ipv6/pipeline tests
touch usg_ipv6/__init__.py usg_ipv6/corpus/__init__.py usg_ipv6/pipeline/__init__.py tests/__init__.py
```

- [ ] **Step 5: Create `usg_ipv6/db.py`**

```python
import duckdb

def get_connection(db_path: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(db_path)

def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS asn_corpus (
            asn             INTEGER PRIMARY KEY,
            org_name        VARCHAR NOT NULL,
            agency          VARCHAR NOT NULL,
            tier            VARCHAR NOT NULL,
            arin_org_handle VARCHAR,
            confidence      VARCHAR NOT NULL,
            sources         VARCHAR NOT NULL,
            last_verified   DATE NOT NULL,
            corpus_version  VARCHAR NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_ts     TIMESTAMP PRIMARY KEY,
            collector       VARCHAR NOT NULL,
            source_url      VARCHAR NOT NULL,
            byte_count      BIGINT NOT NULL,
            status          VARCHAR NOT NULL,
            processed_at    TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prefix_records (
            snapshot_ts     TIMESTAMP NOT NULL,
            collector       VARCHAR NOT NULL,
            peer_asn        INTEGER NOT NULL,
            prefix          VARCHAR NOT NULL,
            origin_asn      INTEGER NOT NULL,
            prefix_len      INTEGER NOT NULL,
            af              INTEGER NOT NULL,
            FOREIGN KEY (snapshot_ts) REFERENCES snapshots(snapshot_ts)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS caida_pfx2as (
            snapshot_date   DATE NOT NULL,
            prefix          VARCHAR NOT NULL,
            prefix_len      INTEGER NOT NULL,
            asn             INTEGER NOT NULL,
            af              INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ark_reachability (
            measurement_date DATE NOT NULL,
            asn              INTEGER NOT NULL,
            ipv6_reachable   BOOLEAN NOT NULL
        )
    """)
```

- [ ] **Step 6: Create `tests/conftest.py`**

```python
import pytest
import duckdb
from usg_ipv6.db import get_connection, init_schema

@pytest.fixture
def db(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    return conn
```

- [ ] **Step 7: Create `.gitignore`**

```gitignore
data/raw/
data/usg_ipv6.duckdb
*.pyc
__pycache__/
.venv/
dist/
```

- [ ] **Step 8: Install dependencies**

```bash
uv sync --dev
```

- [ ] **Step 9: Run tests to verify they pass**

```bash
uv run pytest tests/test_db.py -v
```

Expected: PASS (2 tests)

- [ ] **Step 10: Commit**

```bash
git add pyproject.toml Makefile .gitignore usg_ipv6/ tests/
git commit -m "feat: project scaffolding and DuckDB schema"
```

---

### Task 2: Corpus — ARIN WHOIS seed

**Files:**
- Create: `usg_ipv6/corpus/arin.py`
- Create: `usg_ipv6/corpus/federal_agencies.py`
- Create: `tests/corpus/test_arin.py`

**Interfaces:**
- Consumes: nothing (standalone fetcher)
- Produces: `fetch_asns_for_org(org_handle: str) -> list[int]` — returns list of ASNs registered to an ARIN org handle
- Produces: `search_orgs_by_name(name: str) -> list[dict]` — returns list of `{handle, name}` dicts
- Produces: `CFO_ACT_AGENCIES: list[dict]` — hardcoded seed list with `{name, arin_patterns, tier}`

- [ ] **Step 1: Write the failing tests**

```python
# tests/corpus/test_arin.py
import responses as resp
import pytest
from usg_ipv6.corpus.arin import fetch_asns_for_org, search_orgs_by_name

@resp.activate
def test_fetch_asns_for_org_returns_list():
    resp.add(
        resp.GET,
        "https://whois.arin.net/rest/org/USGSA/asns.json",
        json={"asns": {"asn": ["396260", "5722"]}},
        status=200,
    )
    result = fetch_asns_for_org("USGSA")
    assert result == [396260, 5722]

@resp.activate
def test_rate_limit_triggers_retry():
    resp.add(resp.GET, "https://whois.arin.net/rest/org/USGSA/asns.json", status=429)
    resp.add(
        resp.GET,
        "https://whois.arin.net/rest/org/USGSA/asns.json",
        json={"asns": {"asn": ["396260"]}},
        status=200,
    )
    result = fetch_asns_for_org("USGSA")
    assert result == [396260]
    assert len(resp.calls) == 2

@resp.activate
def test_fetch_asns_returns_empty_on_404():
    resp.add(resp.GET, "https://whois.arin.net/rest/org/DOESNOTEXIST/asns.json", status=404)
    result = fetch_asns_for_org("DOESNOTEXIST")
    assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/corpus/test_arin.py -v
```

Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create `usg_ipv6/corpus/federal_agencies.py`**

The CFO Act agency seed list maps each agency to its known ARIN org handle patterns and tier:

```python
CFO_ACT_AGENCIES = [
    {"name": "Department of Agriculture",                  "handles": ["USDA", "USDAAGSEC"],       "tier": "cfo_act"},
    {"name": "Department of Commerce",                     "handles": ["USDOC"],                    "tier": "cfo_act"},
    {"name": "Department of Defense",                      "handles": ["DOD-NIC", "DISA-HOSTMAST"], "tier": "dod"},
    {"name": "Department of Education",                    "handles": ["USED"],                     "tier": "cfo_act"},
    {"name": "Department of Energy",                       "handles": ["DOE-ISC"],                  "tier": "cfo_act"},
    {"name": "Department of Health and Human Services",    "handles": ["DHHS"],                     "tier": "cfo_act"},
    {"name": "Department of Homeland Security",            "handles": ["DHS-CIO"],                  "tier": "cfo_act"},
    {"name": "Department of Housing and Urban Development",["HUD-NOC"],                             "tier": "cfo_act"},
    {"name": "Department of the Interior",                 "handles": ["USDOI"],                    "tier": "cfo_act"},
    {"name": "Department of Justice",                      "handles": ["USDOJ"],                    "tier": "cfo_act"},
    {"name": "Department of Labor",                        "handles": ["USDOL"],                    "tier": "cfo_act"},
    {"name": "Department of State",                        "handles": ["USDOS-3"],                  "tier": "cfo_act"},
    {"name": "Department of Transportation",               "handles": ["USDOT"],                    "tier": "cfo_act"},
    {"name": "Department of the Treasury",                 "handles": ["USDTREASURY"],              "tier": "cfo_act"},
    {"name": "Department of Veterans Affairs",             "handles": ["VA-NOIT"],                  "tier": "cfo_act"},
    {"name": "Environmental Protection Agency",            "handles": ["EPA-CCSO"],                 "tier": "cfo_act"},
    {"name": "General Services Administration",            "handles": ["USGSA"],                    "tier": "cfo_act"},
    {"name": "National Aeronautics and Space Administration", "handles": ["NASA-ARC-NOC"],          "tier": "cfo_act"},
    {"name": "National Science Foundation",                "handles": ["NSF-NOC"],                  "tier": "cfo_act"},
    {"name": "Nuclear Regulatory Commission",              "handles": ["NRC-OCIO"],                 "tier": "cfo_act"},
    {"name": "Office of Personnel Management",             "handles": ["USAOPMS"],                  "tier": "cfo_act"},
    {"name": "Small Business Administration",              "handles": ["SBA-OCIO"],                 "tier": "cfo_act"},
    {"name": "Social Security Administration",             "handles": ["SSA-INTERNET"],             "tier": "cfo_act"},
    {"name": "U.S. Agency for International Development",  "handles": ["USAID-NOC"],                "tier": "cfo_act"},
]

# Note: ARIN org handles above are starting points and will be expanded
# by the CAIDA AS2Org cross-reference in Task 4. Treat this list as a
# seed, not a complete truth — handles must be verified against ARIN.
```

- [ ] **Step 4: Create `usg_ipv6/corpus/arin.py`**

```python
import time
import requests

ARIN_BASE = "https://whois.arin.net/rest"

def fetch_asns_for_org(org_handle: str, *, max_retries: int = 3) -> list[int]:
    url = f"{ARIN_BASE}/org/{org_handle}/asns.json"
    for attempt in range(max_retries):
        resp = requests.get(url, headers={"Accept": "application/json"}, timeout=10)
        if resp.status_code == 404:
            return []
        if resp.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("asns", {}).get("asn", [])
        if isinstance(raw, str):
            raw = [raw]
        return [int(a) for a in raw]
    return []

def search_orgs_by_name(name: str) -> list[dict]:
    url = f"{ARIN_BASE}/orgs;name={requests.utils.quote(name)}.json"
    resp = requests.get(url, headers={"Accept": "application/json"}, timeout=10)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()
    orgs = data.get("orgs", {}).get("orgRef", [])
    if isinstance(orgs, dict):
        orgs = [orgs]
    return [{"handle": o["@handle"], "name": o["@name"]} for o in orgs]
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/corpus/test_arin.py -v
```

Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add usg_ipv6/corpus/ tests/corpus/
git commit -m "feat: ARIN WHOIS corpus seed fetcher"
```

---

### Task 3: Corpus — CAIDA AS2Org parser

**Files:**
- Create: `usg_ipv6/corpus/caida.py`
- Create: `tests/corpus/test_caida.py`
- Create: `tests/fixtures/as2org_sample.txt`

**Interfaces:**
- Consumes: nothing (standalone parser)
- Produces: `download_as2org(dest_dir: str) -> str` — downloads latest AS2Org file, returns local path
- Produces: `parse_as2org(filepath: str) -> dict[int, dict]` — returns `{asn: {org_id, org_name, country}}`

- [ ] **Step 1: Create fixture file `tests/fixtures/as2org_sample.txt`**

```text
# format: aut|changed|aut_name|org_id|opaque_id|source
# format: org|changed|name|country|org_id|source
# start of aut lines
396260|20200101|USGSA-AS|USGSA|a1b2c3|ARIN
5722|20190615|NASA-AS|NASA-ARC|d4e5f6|ARIN
# start of org lines
USGSA|20200101|General Services Administration|US|a1b2c3|ARIN
NASA-ARC|20190615|National Aeronautics and Space Administration|US|d4e5f6|ARIN
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/corpus/test_caida.py
import pytest
from pathlib import Path
from usg_ipv6.corpus.caida import parse_as2org

FIXTURES = Path(__file__).parent.parent / "fixtures"

def test_parse_as2org_returns_asn_mapping():
    result = parse_as2org(str(FIXTURES / "as2org_sample.txt"))
    assert 396260 in result
    assert result[396260]["org_name"] == "General Services Administration"
    assert result[396260]["country"] == "US"

def test_parse_as2org_maps_all_asns():
    result = parse_as2org(str(FIXTURES / "as2org_sample.txt"))
    assert 5722 in result
    assert result[5722]["org_name"] == "National Aeronautics and Space Administration"

def test_unknown_format_version_raises():
    import tempfile, os
    content = "# format: UNKNOWN_FORMAT_VERSION\n396260|20200101|X|Y|Z|ARIN\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(content)
        path = f.name
    try:
        with pytest.raises(ValueError, match="Unknown AS2Org format"):
            parse_as2org(path)
    finally:
        os.unlink(path)
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
uv run pytest tests/corpus/test_caida.py -v
```

Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 4: Create `usg_ipv6/corpus/caida.py`**

```python
import gzip
import requests
from pathlib import Path

CAIDA_AS2ORG_URL = (
    "https://publicdata.caida.org/datasets/as-organizations/"
    "{year}/{date}.as-org2info.txt.gz"
)
KNOWN_AUT_FORMAT = "aut|changed|aut_name|org_id|opaque_id|source"
KNOWN_ORG_FORMAT = "org|changed|name|country|org_id|source"

def download_as2org(dest_dir: str) -> str:
    from datetime import date, timedelta
    today = date.today()
    # CAIDA publishes quarterly; try recent dates
    for delta in range(0, 120, 30):
        candidate = today - timedelta(days=delta)
        url = CAIDA_AS2ORG_URL.format(
            year=candidate.year,
            date=candidate.strftime("%Y%m%d"),
        )
        resp = requests.head(url, timeout=10)
        if resp.status_code == 200:
            break
    else:
        raise RuntimeError("Could not locate a recent CAIDA AS2Org file")
    dest = Path(dest_dir) / Path(url).name
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
    return str(dest)

def parse_as2org(filepath: str) -> dict[int, dict]:
    opener = gzip.open if filepath.endswith(".gz") else open
    orgs: dict[str, dict] = {}
    asns: list[tuple[int, str]] = []
    aut_format_ok = False
    org_format_ok = False

    with opener(filepath, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                if "format:" in line:
                    fmt = line.split("format:", 1)[1].strip()
                    if fmt == KNOWN_AUT_FORMAT:
                        aut_format_ok = True
                    elif fmt == KNOWN_ORG_FORMAT:
                        org_format_ok = True
                    elif fmt not in (KNOWN_AUT_FORMAT, KNOWN_ORG_FORMAT):
                        raise ValueError(f"Unknown AS2Org format: {fmt!r}")
                continue
            parts = line.split("|")
            if len(parts) == 6:
                try:
                    asn = int(parts[0])
                    org_id = parts[3]
                    asns.append((asn, org_id))
                except ValueError:
                    # org line: org_id|changed|name|country|opaque|source
                    org_id = parts[0]
                    org_name = parts[2]
                    country = parts[3]
                    orgs[org_id] = {"org_name": org_name, "country": country}

    result: dict[int, dict] = {}
    for asn, org_id in asns:
        if org_id in orgs:
            result[asn] = {"org_id": org_id, **orgs[org_id]}
    return result
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/corpus/test_caida.py -v
```

Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add usg_ipv6/corpus/caida.py tests/corpus/test_caida.py tests/fixtures/
git commit -m "feat: CAIDA AS2Org parser"
```

---

### Task 4: Corpus — build, merge, tier assignment, CSV output

**Files:**
- Create: `usg_ipv6/corpus/build.py`
- Create: `usg_ipv6/corpus/schema.py`
- Create: `tests/corpus/test_build.py`

**Interfaces:**
- Consumes: `fetch_asns_for_org`, `search_orgs_by_name` (Task 2); `parse_as2org` (Task 3); `CFO_ACT_AGENCIES` (Task 2); `init_schema`, `get_connection` (Task 1)
- Produces: `build_corpus(db_path: str, as2org_path: str, *, version: str, csv_path: str = "data/usg_asn_corpus.csv") -> str` — builds corpus, writes CSV to `csv_path`, loads `asn_corpus` table, returns CSV path
- Produces: `AsnRecord` dataclass

- [ ] **Step 1: Write the failing tests**

```python
# tests/corpus/test_build.py
import pytest
from pathlib import Path
from unittest.mock import patch
from usg_ipv6.corpus.build import build_corpus, assign_tier, merge_sources
from usg_ipv6.corpus.schema import AsnRecord

FIXTURES = Path(__file__).parent.parent / "fixtures"

def test_tier_deduplication_most_specific_wins():
    records = [
        AsnRecord(asn=1234, org_name="DISA Test", agency="DISA", tier="dod",
                  arin_org_handle="DISA-1", confidence="high",
                  sources=["arin"], last_verified="2026-10-11", corpus_version="2026-10-11"),
        AsnRecord(asn=1234, org_name="DISA Test", agency="DISA", tier="federal_civilian",
                  arin_org_handle="DISA-1", confidence="medium",
                  sources=["caida"], last_verified="2026-10-11", corpus_version="2026-10-11"),
    ]
    merged = merge_sources(records)
    assert len(merged) == 1
    assert merged[0].tier == "dod"

def test_assign_tier_cfo_act_beats_federal_civilian():
    tier = assign_tier("Department of Agriculture", ["cfo_act", "federal_civilian"])
    assert tier == "cfo_act"

def test_build_corpus_produces_csv(tmp_path, db):
    with patch("usg_ipv6.corpus.build.fetch_asns_for_org") as mock_fetch, \
         patch("usg_ipv6.corpus.build.parse_as2org") as mock_as2org:
        mock_fetch.return_value = [396260]
        mock_as2org.return_value = {
            396260: {"org_name": "General Services Administration", "country": "US"}
        }
        csv_path = build_corpus(
            str(tmp_path / "test.duckdb"),
            str(FIXTURES / "as2org_sample.txt"),
            version="2026-10-11",
        )
    assert Path(csv_path).exists()
    import csv
    with open(csv_path) as f:
        rows = list(csv.DictReader(f))
    assert any(int(r["asn"]) == 396260 for r in rows)

def test_build_corpus_loads_duckdb(tmp_path, db):
    with patch("usg_ipv6.corpus.build.fetch_asns_for_org") as mock_fetch, \
         patch("usg_ipv6.corpus.build.parse_as2org") as mock_as2org:
        mock_fetch.return_value = [396260]
        mock_as2org.return_value = {
            396260: {"org_name": "General Services Administration", "country": "US"}
        }
        build_corpus(str(tmp_path / "test.duckdb"), str(FIXTURES / "as2org_sample.txt"), version="2026-10-11")
    rows = db.execute("SELECT asn FROM asn_corpus WHERE asn = 396260").fetchall()
    assert len(rows) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/corpus/test_build.py -v
```

Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create `usg_ipv6/corpus/schema.py`**

```python
from dataclasses import dataclass, field

TIER_PRIORITY = {"cfo_act": 0, "dod": 1, "federal_civilian": 2}

@dataclass
class AsnRecord:
    asn: int
    org_name: str
    agency: str
    tier: str
    arin_org_handle: str
    confidence: str          # "high" | "medium" | "low"
    sources: list[str]       # ["arin", "caida", "manual"]
    last_verified: str       # "YYYY-MM-DD"
    corpus_version: str      # "YYYY-MM-DD"
```

- [ ] **Step 4: Create `usg_ipv6/corpus/build.py`**

```python
import csv
from datetime import date
from pathlib import Path

from rapidfuzz import fuzz

from usg_ipv6.corpus.arin import fetch_asns_for_org
from usg_ipv6.corpus.caida import parse_as2org
from usg_ipv6.corpus.federal_agencies import CFO_ACT_AGENCIES
from usg_ipv6.corpus.schema import AsnRecord, TIER_PRIORITY
from usg_ipv6.db import get_connection, init_schema

FUZZY_THRESHOLD = 85  # minimum score to count as a match

def assign_tier(agency_name: str, candidate_tiers: list[str]) -> str:
    return min(candidate_tiers, key=lambda t: TIER_PRIORITY.get(t, 99))

def merge_sources(records: list[AsnRecord]) -> list[AsnRecord]:
    by_asn: dict[int, AsnRecord] = {}
    for r in records:
        if r.asn not in by_asn:
            by_asn[r.asn] = r
        else:
            existing = by_asn[r.asn]
            # keep most-specific tier
            if TIER_PRIORITY.get(r.tier, 99) < TIER_PRIORITY.get(existing.tier, 99):
                by_asn[r.asn] = AsnRecord(
                    asn=r.asn,
                    org_name=existing.org_name,
                    agency=existing.agency,
                    tier=r.tier,
                    arin_org_handle=existing.arin_org_handle or r.arin_org_handle,
                    confidence=existing.confidence,
                    sources=list(set(existing.sources + r.sources)),
                    last_verified=existing.last_verified,
                    corpus_version=existing.corpus_version,
                )
            else:
                existing.sources = list(set(existing.sources + r.sources))
    return list(by_asn.values())

def _build_from_arin(version: str) -> list[AsnRecord]:
    records = []
    today = date.today().isoformat()
    for agency in CFO_ACT_AGENCIES:
        for handle in agency["handles"]:
            asns = fetch_asns_for_org(handle)
            for asn in asns:
                records.append(AsnRecord(
                    asn=asn,
                    org_name=agency["name"],
                    agency=agency["name"],
                    tier=agency["tier"],
                    arin_org_handle=handle,
                    confidence="high",
                    sources=["arin"],
                    last_verified=today,
                    corpus_version=version,
                ))
    return records

def _expand_from_caida(as2org_path: str, existing: list[AsnRecord], version: str) -> list[AsnRecord]:
    as2org = parse_as2org(as2org_path)
    known_asns = {r.asn for r in existing}
    today = date.today().isoformat()
    new_records = []
    agency_names = [a["name"] for a in CFO_ACT_AGENCIES]
    for asn, info in as2org.items():
        if asn in known_asns:
            continue
        org_name = info["org_name"]
        best_match = max(
            ((name, fuzz.token_sort_ratio(org_name, name)) for name in agency_names),
            key=lambda x: x[1],
        )
        if best_match[1] >= FUZZY_THRESHOLD:
            agency = CFO_ACT_AGENCIES[agency_names.index(best_match[0])]
            new_records.append(AsnRecord(
                asn=asn,
                org_name=org_name,
                agency=best_match[0],
                tier=agency["tier"],
                arin_org_handle="",
                confidence="medium",
                sources=["caida"],
                last_verified=today,
                corpus_version=version,
            ))
    return new_records

def build_corpus(db_path: str, as2org_path: str, *, version: str) -> str:
    arin_records = _build_from_arin(version)
    caida_records = _expand_from_caida(as2org_path, arin_records, version)
    all_records = merge_sources(arin_records + caida_records)

    # Write CSV
    csv_path = Path("data") / "usg_asn_corpus.csv"
    csv_path.parent.mkdir(exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "asn", "org_name", "agency", "tier", "arin_org_handle",
            "confidence", "sources", "last_verified", "corpus_version",
        ])
        writer.writeheader()
        for r in all_records:
            writer.writerow({
                "asn": r.asn,
                "org_name": r.org_name,
                "agency": r.agency,
                "tier": r.tier,
                "arin_org_handle": r.arin_org_handle,
                "confidence": r.confidence,
                "sources": ",".join(r.sources),
                "last_verified": r.last_verified,
                "corpus_version": r.corpus_version,
            })

    # Load into DuckDB
    conn = get_connection(db_path)
    init_schema(conn)
    conn.execute("DELETE FROM asn_corpus")
    conn.executemany(
        """INSERT INTO asn_corpus VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(r.asn, r.org_name, r.agency, r.tier, r.arin_org_handle,
          r.confidence, ",".join(r.sources), r.last_verified, r.corpus_version)
         for r in all_records],
    )
    return str(csv_path)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/corpus/ -v
```

Expected: PASS (all corpus tests)

- [ ] **Step 6: Commit**

```bash
git add usg_ipv6/corpus/ tests/corpus/test_build.py
git commit -m "feat: corpus builder with ARIN + CAIDA merge and tier assignment"
```

---

### Task 5: Pipeline — RouteViews MRT fetch + manifest

**Files:**
- Create: `usg_ipv6/pipeline/routeviews.py`
- Create: `tests/pipeline/test_routeviews.py`

**Interfaces:**
- Consumes: `get_connection`, `init_schema` (Task 1)
- Produces: `rib_url(collector: str, dt: datetime) -> str` — constructs RouteViews archive URL
- Produces: `fetch_rib(url: str, dest_dir: str, conn: DuckDBPyConnection) -> str | None` — downloads MRT file, records in `snapshots` manifest, returns local path or None if already processed

- [ ] **Step 1: Write the failing tests**

```python
# tests/pipeline/test_routeviews.py
import responses as resp
import pytest
from datetime import datetime
from usg_ipv6.pipeline.routeviews import rib_url, fetch_rib

def test_rib_url_format():
    dt = datetime(2024, 3, 15, 8, 0)
    url = rib_url("route-views2", dt)
    assert "route-views2" in url
    assert "2024.03" in url
    assert "rib.20240315.0800" in url

@resp.activate
def test_fetch_rib_downloads_file(tmp_path, db):
    url = rib_url("route-views2", datetime(2024, 3, 15, 8, 0))
    fake_data = b"MRT_FAKE_DATA" * 100
    resp.add(resp.GET, url, body=fake_data, status=200,
             headers={"Content-Length": str(len(fake_data))})
    path = fetch_rib(url, str(tmp_path), db)
    assert path is not None
    assert (tmp_path / path.split("/")[-1]).exists()
    row = db.execute("SELECT status FROM snapshots WHERE source_url = ?", [url]).fetchone()
    assert row[0] == "downloaded"

@resp.activate
def test_partial_download_not_marked_done(tmp_path, db):
    url = rib_url("route-views2", datetime(2024, 3, 15, 10, 0))
    fake_data = b"PARTIAL" * 10
    # Content-Length says 10000 but we only return 70 bytes
    resp.add(resp.GET, url, body=fake_data, status=200,
             headers={"Content-Length": "10000"})
    path = fetch_rib(url, str(tmp_path), db)
    assert path is None
    row = db.execute("SELECT status FROM snapshots WHERE source_url = ?", [url]).fetchone()
    assert row is None or row[0] == "failed"

@resp.activate
def test_fetch_rib_skips_already_processed(tmp_path, db):
    url = rib_url("route-views2", datetime(2024, 3, 15, 12, 0))
    db.execute("""
        INSERT INTO snapshots VALUES (?, 'route-views2', ?, 1000, 'processed', NOW())
    """, [datetime(2024, 3, 15, 12, 0), url])
    path = fetch_rib(url, str(tmp_path), db)
    assert path is None
    assert len(resp.calls) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/pipeline/test_routeviews.py -v
```

Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create `usg_ipv6/pipeline/routeviews.py`**

```python
import os
import requests
import duckdb
from datetime import datetime
from pathlib import Path

RV_BASE = "http://archive.routeviews.org/{collector}/bgpdata/{year_month}/RIBS"

def rib_url(collector: str, dt: datetime) -> str:
    year_month = dt.strftime("%Y.%m")
    filename = f"rib.{dt.strftime('%Y%m%d.%H%M')}.bz2"
    return f"{RV_BASE.format(collector=collector, year_month=year_month)}/{filename}"

def _snapshot_ts(url: str) -> datetime:
    name = url.split("/")[-1]  # rib.20240315.0800.bz2
    parts = name.split(".")
    return datetime.strptime(f"{parts[1]}.{parts[2]}", "%Y%m%d.%H%M")

def _collector_from_url(url: str) -> str:
    parts = url.split("/")
    return parts[3] if len(parts) > 3 else "unknown"

def fetch_rib(url: str, dest_dir: str, conn: duckdb.DuckDBPyConnection) -> str | None:
    snapshot_ts = _snapshot_ts(url)
    collector = _collector_from_url(url)

    existing = conn.execute(
        "SELECT status FROM snapshots WHERE source_url = ?", [url]
    ).fetchone()
    if existing and existing[0] == "processed":
        return None

    dest = Path(dest_dir) / url.split("/")[-1]
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            expected = int(r.headers.get("Content-Length", 0))
            with open(dest, "wb") as f:
                received = 0
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
                    received += len(chunk)
        if expected > 0 and received < expected:
            conn.execute("""
                INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, NULL)
            """, [snapshot_ts, collector, url, received, "failed"])
            dest.unlink(missing_ok=True)
            return None
        conn.execute("""
            INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, NULL)
        """, [snapshot_ts, collector, url, received, "downloaded"])
        return str(dest)
    except Exception:
        conn.execute("""
            INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, NULL)
        """, [snapshot_ts, collector, url, 0, "failed"])
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/pipeline/test_routeviews.py -v
```

Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add usg_ipv6/pipeline/routeviews.py tests/pipeline/
git commit -m "feat: RouteViews MRT fetcher with download manifest"
```

---

### Task 6: Pipeline — MRT parse → Parquet

**Files:**
- Create: `usg_ipv6/pipeline/parse.py`
- Create: `tests/pipeline/test_parse.py`
- Create: `tests/fixtures/sample.mrt` (binary fixture — generated in step 1)

**Interfaces:**
- Consumes: nothing (standalone parser)
- Produces: `parse_mrt(mrt_path: str, parquet_path: str) -> int` — parses MRT file, writes Parquet, returns record count

- [ ] **Step 1: Create a minimal MRT fixture using mrtparse**

```python
# Run this once to generate the fixture (not a test — a setup script):
# uv run python scripts/gen_fixture.py
# Place output at tests/fixtures/sample.mrt

# Since creating a real MRT binary is complex, use a small real RouteViews
# RIB slice: download 100 lines of rib.latest from RouteViews and save.
# Alternative: use mrtparse to write a minimal synthetic MRT.
# For the test suite, mock the Reader to return synthetic data instead.
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/pipeline/test_parse.py
import pytest
import pyarrow.parquet as pq
from pathlib import Path
from unittest.mock import patch, MagicMock
from usg_ipv6.pipeline.parse import parse_mrt

def _make_mock_entry(prefix, origin_asn, af):
    entry = MagicMock()
    entry.err = 0
    entry.data = {
        "type": {13: "TABLE_DUMP_V2"},
        "subtype": {2 if af == 4 else 4: "RIB"},
        "prefix": prefix,
        "rib_entries": [{
            "peer_index": 0,
            "path_attributes": {
                "as_path": [[origin_asn]],
            }
        }],
    }
    return entry

@patch("usg_ipv6.pipeline.parse.Reader")
def test_parse_mrt_writes_parquet(mock_reader, tmp_path):
    mock_reader.return_value = [
        _make_mock_entry("192.0.2.0/24", 396260, 4),
        _make_mock_entry("2001:db8::/32", 396260, 6),
    ]
    out = str(tmp_path / "out.parquet")
    count = parse_mrt("fake.mrt", out)
    assert count == 2
    table = pq.read_table(out)
    assert set(table.schema.names) >= {"prefix", "origin_asn", "af", "prefix_len"}

@patch("usg_ipv6.pipeline.parse.Reader")
def test_ipv4_mapped_prefixes_filtered(mock_reader, tmp_path):
    mock_reader.return_value = [
        _make_mock_entry("::ffff:192.0.2.0/120", 396260, 6),  # IPv4-mapped
        _make_mock_entry("64:ff9b::/96", 396260, 6),           # NAT64
        _make_mock_entry("2001:db8::/32", 396260, 6),          # legitimate
    ]
    out = str(tmp_path / "out.parquet")
    count = parse_mrt("fake.mrt", out)
    assert count == 1
    table = pq.read_table(out)
    prefixes = table.column("prefix").to_pylist()
    assert "::ffff:192.0.2.0/120" not in prefixes
    assert "64:ff9b::/96" not in prefixes
    assert "2001:db8::/32" in prefixes
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
uv run pytest tests/pipeline/test_parse.py -v
```

Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 4: Create `usg_ipv6/pipeline/parse.py`**

```python
import ipaddress
import pyarrow as pa
import pyarrow.parquet as pq
from mrtparse import Reader

TABLE_DUMP_V2 = 13
RIB_IPV4_UNICAST = 2
RIB_IPV6_UNICAST = 4

IPV4_MAPPED_PREFIXES = [
    ipaddress.ip_network("::ffff:0:0/96"),
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
]

def _is_ipv4_mapped(prefix: str) -> bool:
    try:
        net = ipaddress.ip_network(prefix, strict=False)
        if net.version != 6:
            return False
        return any(net.subnet_of(m) or net == m for m in IPV4_MAPPED_PREFIXES)
    except ValueError:
        return False

def _origin_asn(rib_entry: dict) -> int | None:
    for attr in rib_entry.get("path_attributes", {}).values() if isinstance(
        rib_entry.get("path_attributes"), dict
    ) else []:
        pass
    attrs = rib_entry.get("path_attributes", {})
    as_path = attrs.get("as_path", [])
    if as_path and isinstance(as_path[-1], list) and as_path[-1]:
        return int(as_path[-1][-1])
    return None

def parse_mrt(mrt_path: str, parquet_path: str) -> int:
    rows = []
    for entry in Reader(mrt_path):
        if entry.err:
            continue
        d = entry.data
        type_val = list(d.get("type", {}).keys())
        if not type_val or type_val[0] != TABLE_DUMP_V2:
            continue
        subtype_val = list(d.get("subtype", {}).keys())
        if not subtype_val or subtype_val[0] not in (RIB_IPV4_UNICAST, RIB_IPV6_UNICAST):
            continue
        af = 4 if subtype_val[0] == RIB_IPV4_UNICAST else 6
        prefix = d.get("prefix", "")
        if af == 6 and _is_ipv4_mapped(prefix):
            continue
        prefix_len = int(prefix.split("/")[1]) if "/" in prefix else 0
        for rib_entry in d.get("rib_entries", []):
            origin = _origin_asn(rib_entry)
            if origin is None:
                continue
            rows.append({
                "prefix": prefix,
                "origin_asn": origin,
                "prefix_len": prefix_len,
                "af": af,
            })

    schema = pa.schema([
        pa.field("prefix", pa.string()),
        pa.field("origin_asn", pa.int32()),
        pa.field("prefix_len", pa.int16()),
        pa.field("af", pa.int8()),
    ])
    table = pa.table(
        {k: [r[k] for r in rows] for k in ["prefix", "origin_asn", "prefix_len", "af"]},
        schema=schema,
    )
    pq.write_table(table, parquet_path)
    return len(rows)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/pipeline/test_parse.py -v
```

Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add usg_ipv6/pipeline/parse.py tests/pipeline/test_parse.py
git commit -m "feat: MRT parser with IPv4-mapped prefix filtering"
```

---

### Task 7: Pipeline — normalize, filter to corpus ASNs, load DuckDB

**Files:**
- Create: `usg_ipv6/pipeline/normalize.py`
- Create: `tests/pipeline/test_normalize.py`

**Interfaces:**
- Consumes: `parse_mrt` output Parquet (Task 6); `asn_corpus` DuckDB table (Task 4); `snapshots` table (Task 5); `get_connection` (Task 1)
- Produces: `load_parquet(parquet_path: str, snapshot_ts: datetime, collector: str, conn: DuckDBPyConnection) -> int` — filters to corpus ASNs, inserts into `prefix_records`, marks snapshot as `processed`, returns inserted count

- [ ] **Step 1: Write the failing tests**

```python
# tests/pipeline/test_normalize.py
import pytest
import pyarrow as pa
import pyarrow.parquet as pq
from datetime import datetime
from usg_ipv6.pipeline.normalize import load_parquet

def _write_parquet(path, rows):
    schema = pa.schema([
        pa.field("prefix", pa.string()),
        pa.field("origin_asn", pa.int32()),
        pa.field("prefix_len", pa.int16()),
        pa.field("af", pa.int8()),
    ])
    table = pa.table({k: [r[k] for r in rows] for k in schema.names}, schema=schema)
    pq.write_table(table, path)

def test_load_parquet_filters_to_corpus_asns(tmp_path, db):
    db.execute("INSERT INTO asn_corpus VALUES (396260,'GSA','GSA','cfo_act','USGSA','high','arin','2026-10-11','2026-10-11')")
    pq_path = str(tmp_path / "test.parquet")
    _write_parquet(pq_path, [
        {"prefix": "192.0.2.0/24", "origin_asn": 396260, "prefix_len": 24, "af": 4},
        {"prefix": "10.0.0.0/8",   "origin_asn": 99999,  "prefix_len": 8,  "af": 4},
    ])
    ts = datetime(2024, 3, 15, 8, 0)
    db.execute("INSERT INTO snapshots VALUES (?, 'route-views2', 'http://x', 1000, 'downloaded', NULL)", [ts])
    count = load_parquet(pq_path, ts, "route-views2", db)
    assert count == 1
    rows = db.execute("SELECT origin_asn FROM prefix_records").fetchall()
    assert rows == [(396260,)]

def test_load_parquet_marks_snapshot_processed(tmp_path, db):
    db.execute("INSERT INTO asn_corpus VALUES (396260,'GSA','GSA','cfo_act','USGSA','high','arin','2026-10-11','2026-10-11')")
    pq_path = str(tmp_path / "empty.parquet")
    _write_parquet(pq_path, [])
    ts = datetime(2024, 3, 15, 10, 0)
    db.execute("INSERT INTO snapshots VALUES (?, 'route-views2', 'http://y', 1000, 'downloaded', NULL)", [ts])
    load_parquet(pq_path, ts, "route-views2", db)
    row = db.execute("SELECT status FROM snapshots WHERE snapshot_ts = ?", [ts]).fetchone()
    assert row[0] == "processed"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/pipeline/test_normalize.py -v
```

Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create `usg_ipv6/pipeline/normalize.py`**

```python
import duckdb
import pyarrow.parquet as pq
from datetime import datetime

def load_parquet(
    parquet_path: str,
    snapshot_ts: datetime,
    collector: str,
    conn: duckdb.DuckDBPyConnection,
) -> int:
    corpus_asns = {
        row[0] for row in conn.execute("SELECT asn FROM asn_corpus").fetchall()
    }
    table = pq.read_table(parquet_path)
    df = table.to_pydict()

    rows_to_insert = [
        (snapshot_ts, collector, 0, df["prefix"][i], df["origin_asn"][i],
         df["prefix_len"][i], df["af"][i])
        for i in range(len(df["prefix"]))
        if df["origin_asn"][i] in corpus_asns
    ]

    if rows_to_insert:
        conn.executemany(
            "INSERT INTO prefix_records VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows_to_insert,
        )

    conn.execute("""
        UPDATE snapshots SET status = 'processed', processed_at = NOW()
        WHERE snapshot_ts = ?
    """, [snapshot_ts])

    return len(rows_to_insert)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/pipeline/test_normalize.py -v
```

Expected: PASS (2 tests)

- [ ] **Step 5: Run the full test suite**

```bash
uv run pytest -v
```

Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add usg_ipv6/pipeline/normalize.py tests/pipeline/test_normalize.py
git commit -m "feat: normalize and load parsed MRT records into DuckDB"
```

---

### Task 8: Pipeline — CAIDA pfx2as corroborating load

**Files:**
- Create: `usg_ipv6/pipeline/caida_pfx2as.py`
- Create: `tests/pipeline/test_caida_pfx2as.py`
- Create: `tests/fixtures/pfx2as_sample.txt`

**Interfaces:**
- Consumes: `get_connection`, `init_schema` (Task 1)
- Produces: `download_pfx2as(dest_dir: str, date: str) -> str` — downloads CAIDA pfx2as file for given date
- Produces: `load_pfx2as(filepath: str, snapshot_date: str, conn: DuckDBPyConnection) -> int` — parses and loads into `caida_pfx2as` table

- [ ] **Step 1: Create fixture `tests/fixtures/pfx2as_sample.txt`**

```text
# CAIDA RouteViews Prefix-to-AS
# generated: 20240315
192.0.2.0/24|396260
2001:db8::/32|396260
10.0.0.0/8|7018
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/pipeline/test_caida_pfx2as.py
import pytest
from pathlib import Path
from usg_ipv6.pipeline.caida_pfx2as import load_pfx2as

FIXTURES = Path(__file__).parent.parent / "fixtures"

def test_load_pfx2as_inserts_records(db):
    count = load_pfx2as(str(FIXTURES / "pfx2as_sample.txt"), "2024-03-15", db)
    assert count == 3
    rows = db.execute("SELECT COUNT(*) FROM caida_pfx2as").fetchone()
    assert rows[0] == 3

def test_load_pfx2as_sets_address_family(db):
    load_pfx2as(str(FIXTURES / "pfx2as_sample.txt"), "2024-03-15", db)
    v6 = db.execute(
        "SELECT COUNT(*) FROM caida_pfx2as WHERE af = 6"
    ).fetchone()[0]
    assert v6 == 1
    v4 = db.execute(
        "SELECT COUNT(*) FROM caida_pfx2as WHERE af = 4"
    ).fetchone()[0]
    assert v4 == 2
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
uv run pytest tests/pipeline/test_caida_pfx2as.py -v
```

Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 4: Create `usg_ipv6/pipeline/caida_pfx2as.py`**

```python
import ipaddress
import duckdb
import requests
from pathlib import Path

PFX2AS_URL = (
    "https://publicdata.caida.org/datasets/routing/routeviews-prefix2as/"
    "{year}/{month}/routeviews-rv2-{date}-1200.pfx2as.gz"
)

def download_pfx2as(dest_dir: str, date: str) -> str:
    year, month, day = date.split("-")
    url = PFX2AS_URL.format(year=year, month=month, date=f"{year}{month}{day}")
    dest = Path(dest_dir) / url.split("/")[-1]
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
    return str(dest)

def load_pfx2as(filepath: str, snapshot_date: str, conn: duckdb.DuckDBPyConnection) -> int:
    import gzip
    opener = gzip.open if filepath.endswith(".gz") else open
    rows = []
    with opener(filepath, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 2:
                continue
            prefix_str = parts[0].strip()
            asn_str = parts[1].strip().split("_")[0]  # handle multi-origin "1234_5678"
            try:
                net = ipaddress.ip_network(prefix_str, strict=False)
                asn = int(asn_str)
                af = net.version
                rows.append((snapshot_date, prefix_str, net.prefixlen, asn, af))
            except (ValueError, IndexError):
                continue
    if rows:
        conn.executemany(
            "INSERT INTO caida_pfx2as VALUES (?, ?, ?, ?, ?)", rows
        )
    return len(rows)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/pipeline/test_caida_pfx2as.py -v
```

Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add usg_ipv6/pipeline/caida_pfx2as.py tests/pipeline/test_caida_pfx2as.py tests/fixtures/pfx2as_sample.txt
git commit -m "feat: CAIDA pfx2as corroborating data loader"
```

---

### Task 9: Makefile, backfill runner, integration smoke test

**Files:**
- Create: `Makefile` (full version)
- Create: `usg_ipv6/pipeline/runner.py`
- Create: `tests/test_integration.py`

**Interfaces:**
- Consumes: all pipeline modules (Tasks 2–8)
- Produces: `run_incremental(db_path: str, collector: str, *, limit: int = 1) -> dict` — fetches and processes the most recent unprocessed RIB snapshot
- Produces: `run_backfill(db_path: str, collector: str, since: str, until: str | None) -> dict` — processes all snapshots in a date range

- [ ] **Step 1: Write the failing integration test**

```python
# tests/test_integration.py
import pytest
from unittest.mock import patch, MagicMock
from usg_ipv6.pipeline.runner import run_incremental
from usg_ipv6.db import get_connection, init_schema

@patch("usg_ipv6.pipeline.runner.fetch_rib")
@patch("usg_ipv6.pipeline.runner.parse_mrt")
@patch("usg_ipv6.pipeline.runner.load_parquet")
def test_run_incremental_end_to_end(mock_load, mock_parse, mock_fetch, tmp_path):
    db_path = str(tmp_path / "test.duckdb")
    conn = get_connection(db_path)
    init_schema(conn)
    conn.execute("""
        INSERT INTO asn_corpus VALUES
        (396260,'GSA','GSA','cfo_act','USGSA','high','arin','2026-10-11','2026-10-11')
    """)
    conn.close()

    mock_fetch.return_value = str(tmp_path / "rib.20240315.0800.bz2")
    mock_parse.return_value = 5
    mock_load.return_value = 3

    result = run_incremental(db_path, "route-views2", limit=1)
    assert result["processed"] == 1
    assert mock_fetch.called
    assert mock_parse.called
    assert mock_load.called
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_integration.py -v
```

Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create `usg_ipv6/pipeline/runner.py`**

```python
import os
from datetime import datetime, timedelta
from pathlib import Path

from usg_ipv6.db import get_connection, init_schema
from usg_ipv6.pipeline.routeviews import rib_url, fetch_rib
from usg_ipv6.pipeline.parse import parse_mrt
from usg_ipv6.pipeline.normalize import load_parquet

DEFAULT_COLLECTOR = "route-views2"
RAW_DIR = Path("data/raw")
PROCESSED_DIR = Path("data/processed")

def _ensure_dirs():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

def run_incremental(db_path: str, collector: str = DEFAULT_COLLECTOR, *, limit: int = 1) -> dict:
    _ensure_dirs()
    conn = get_connection(db_path)
    init_schema(conn)
    processed = 0
    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    candidate = now

    while processed < limit:
        url = rib_url(collector, candidate)
        raw_path = fetch_rib(url, str(RAW_DIR), conn)
        if raw_path:
            parquet_path = str(PROCESSED_DIR / (Path(raw_path).stem + ".parquet"))
            parse_mrt(raw_path, parquet_path)
            snapshot_ts = datetime.strptime(
                Path(raw_path).name.split(".bz2")[0], "rib.%Y%m%d.%H%M"
            )
            load_parquet(parquet_path, snapshot_ts, collector, conn)
            Path(raw_path).unlink(missing_ok=True)
            processed += 1
        candidate -= timedelta(hours=2)
        if candidate < datetime(2003, 1, 1):
            break

    return {"processed": processed}

def run_backfill(
    db_path: str,
    collector: str = DEFAULT_COLLECTOR,
    since: str = "2020-01-01",
    until: str | None = None,
) -> dict:
    _ensure_dirs()
    conn = get_connection(db_path)
    init_schema(conn)
    start = datetime.strptime(since, "%Y-%m-%d")
    end = datetime.strptime(until, "%Y-%m-%d") if until else datetime.utcnow()
    processed = 0
    candidate = start

    while candidate <= end:
        for hour in (0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22):
            dt = candidate.replace(hour=hour)
            url = rib_url(collector, dt)
            raw_path = fetch_rib(url, str(RAW_DIR), conn)
            if raw_path:
                parquet_path = str(PROCESSED_DIR / (Path(raw_path).stem + ".parquet"))
                parse_mrt(raw_path, parquet_path)
                snapshot_ts = datetime.strptime(
                    Path(raw_path).name.split(".bz2")[0], "rib.%Y%m%d.%H%M"
                )
                load_parquet(parquet_path, snapshot_ts, collector, conn)
                Path(raw_path).unlink(missing_ok=True)
                processed += 1
        candidate += timedelta(days=1)

    return {"processed": processed}
```

- [ ] **Step 4: Create full `Makefile`**

```makefile
DB_PATH ?= data/usg_ipv6.duckdb
COLLECTOR ?= route-views2
SINCE ?= 2020-01-01

.PHONY: setup run backfill corpus test lint

setup:
    uv sync --dev
    mkdir -p data/raw data/processed
    uv run python -c "from usg_ipv6.db import get_connection, init_schema; init_schema(get_connection('$(DB_PATH)'))"

corpus:
    uv run python -m usg_ipv6.corpus.build --db $(DB_PATH) --version $$(date +%Y-%m-%d)

run:
    uv run python -c "from usg_ipv6.pipeline.runner import run_incremental; run_incremental('$(DB_PATH)', '$(COLLECTOR)')"

backfill:
    uv run python -c "from usg_ipv6.pipeline.runner import run_backfill; run_backfill('$(DB_PATH)', '$(COLLECTOR)', '$(SINCE)')"

test:
    uv run pytest -v

lint:
    uv run ruff check usg_ipv6/ tests/
```

> **Note:** Makefiles require hard tabs for recipe lines. Replace the 4-space indentation above with a single tab character when writing the actual `Makefile`.

- [ ] **Step 5: Add `__main__` entry to `usg_ipv6/corpus/build.py`** (for `make corpus`)

Add at the bottom of `build.py`:

```python
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/usg_ipv6.duckdb")
    parser.add_argument("--as2org", default=None)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    if args.as2org is None:
        from usg_ipv6.corpus.caida import download_as2org
        args.as2org = download_as2org("data/raw")
    csv_path = build_corpus(args.db, args.as2org, version=args.version)
    print(f"Corpus written to {csv_path}")
```

- [ ] **Step 6: Run the full test suite**

```bash
uv run pytest -v
```

Expected: all tests pass

- [ ] **Step 7: Verify Makefile setup target runs cleanly**

```bash
make setup
```

Expected: dependencies installed, directories created, DuckDB schema initialised with no errors

- [ ] **Step 8: Commit**

```bash
git add Makefile usg_ipv6/pipeline/runner.py tests/test_integration.py
git commit -m "feat: pipeline runner with incremental and backfill modes"
```

---

## Plan 2 Preview

The next plan will cover:

- `usg_ipv6/queries/` — analysis functions (`ipv6_adoption_over_time`, `mandate_progress`, `reachability_vs_announced`, etc.)
- CAIDA Ark IPv6 Topology ingest (`ark_reachability` table)
- `usg-ipv6` CLI (Typer)
- Jupyter notebooks (`01_corpus_exploration` through `06_caida_corroboration`)
- GitHub Pages web UI (DuckDB-WASM)
- GitHub Actions workflows (`corpus-update.yml`, `pipeline-run.yml`, `publish.yml`, `backfill.yml`)
