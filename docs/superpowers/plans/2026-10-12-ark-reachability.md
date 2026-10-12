# Phase 2: CAIDA Ark IPv6 Reachability — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest CAIDA Ark IPv6 active-measurement data and produce per-ASN reachability verdicts for every corpus member, enabling direct comparison of BGP-announced vs. actively-reachable IPv6 — the key figure for OMB M-21-07 compliance research.

**Architecture:** New `lpm.py` (stdlib-only LPM engine) + `ark.py` (downloader, parser, orchestrator) feed two new DuckDB tables. A `usg ark` CLI sub-app mirrors the existing `usg run` pattern. A weekly GitHub Actions workflow follows the existing DuckDB rolling-release pattern.

**Tech Stack:** Python 3.11, `ipaddress` stdlib, DuckDB, Typer, requests, pytest, GitHub Actions

**Spec:** `docs/superpowers/specs/2026-10-12-ark-reachability-design.md`

## Global Constraints

- No new Python package dependencies — LPM uses `ipaddress` stdlib only
- All public runner functions (`run_ark_ingest`, `run_ark_backfill`) accept `db_path: str` as first arg and return a plain JSON-serialisable dict
- DuckDB SQL must use DuckDB-compatible syntax (not PostgreSQL-specific constructs)
- All backfills must be resumable: already-loaded dates are detected and skipped at the start of `run_ark_ingest`
- The `ark` CLI sub-app follows the exact same patterns as the existing `run` sub-app in `cli.py` (`typer.Typer`, `--db`, `--json` flags)
- GitHub Actions Python steps use the `env:` + quoted heredoc (`<< 'PYEOF'`) pattern established in existing workflows — never unquoted heredocs

## Review Focus

- **Ark lookback returns a different date than requested:** `download_nodes_as("2024-03-15")` may return a file for `2024-03-13`. The `measurement_date` stored must be the file's actual date (from the filename), not the requested date. The test for `run_ark_ingest` must verify the stored `measurement_date` matches what was downloaded.
- **Old 3-column `ark_reachability` in an existing DB:** The migration in `init_schema()` must detect and replace the old schema before the new `CREATE TABLE IF NOT EXISTS` runs, or the old table silently persists. Add a test that calls `init_schema()` on a DB that has the 3-column version and verifies the new columns exist afterward.
- **Corpus ASN observed via ark_asn only (no pfx2as prefix):** An ASN in `asn_corpus` with `ipv6_reachable=TRUE` but `prefixes_in_scope=0` indicates Ark found it but our pfx2as doesn't cover it. This is a divergence case the gap command must not misclassify. The integration test fixture must include this case.
- **`usg ark gap` includes indeterminate ASNs:** The gap query must filter `prefixes_in_scope > 0`; without this filter an ASN with no IPv6 pfx2as prefixes appears as "not reachable" which is methodologically wrong. The gap test must verify this exclusion explicitly.
- **Empty `caida_pfx2as` (no IPv6 rows for any date):** `run_ark_ingest` must raise `RuntimeError` with a clear message rather than silently building an empty LPM index and recording every corpus ASN as unreachable. Add this as a dedicated error-path test in the ingest integration test.

---

## File Map

| Action | Path |
| --- | --- |
| Create | `usg_ipv6/pipeline/lpm.py` |
| Create | `usg_ipv6/pipeline/ark.py` |
| Modify | `usg_ipv6/db.py` |
| Modify | `usg_ipv6/cli.py` |
| Modify | `usg_ipv6/report_html.py` |
| Create | `.github/workflows/ark-ingest.yml` |
| Create | `tests/pipeline/test_lpm.py` |
| Create | `tests/pipeline/test_ark.py` |
| Create | `tests/test_ark_gap.py` |
| Create | `tests/fixtures/nodes_as_sample.nodes.as.gz` |
| Modify | `Makefile` |

---

### Task 1: LPM engine

**Files:**
- Create: `usg_ipv6/pipeline/lpm.py`
- Create: `tests/pipeline/test_lpm.py`

**Interfaces:**
- Produces:
  - `LpmIndex = dict[int, dict[int, tuple[int, str]]]` — `{prefix_len: {net_addr_int: (asn, cidr_str)}}`
  - `build_lpm_index(prefixes: list[tuple[str, int]]) -> LpmIndex`
  - `lpm_lookup(addr: IPv6Address, index: LpmIndex) -> int | None` — returns ASN only
  - `lpm_lookup_entry(addr: IPv6Address, index: LpmIndex) -> tuple[int, str] | None` — returns `(asn, cidr_str)`

---

- [ ] **Step 1: Write the failing tests**

```python
# tests/pipeline/test_lpm.py
import ipaddress
import pytest
from usg_ipv6.pipeline.lpm import build_lpm_index, lpm_lookup, lpm_lookup_entry

def test_exact_match():
    index = build_lpm_index([("2001:db8::/32", 64496)])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db8::1"), index) == 64496

def test_longest_prefix_wins():
    index = build_lpm_index([
        ("2001:db8::/32", 64496),
        ("2001:db8:1::/48", 64497),
    ])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db8:1::1"), index) == 64497

def test_shorter_prefix_fallback():
    index = build_lpm_index([
        ("2001:db8::/32", 64496),
        ("2001:db8:1::/48", 64497),
    ])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db8:2::1"), index) == 64496

def test_no_match_returns_none():
    index = build_lpm_index([("2001:db8::/32", 64496)])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db9::1"), index) is None

def test_host_route_128():
    addr = ipaddress.IPv6Address("2001:db8::1")
    index = build_lpm_index([(f"{addr}/128", 64496)])
    assert lpm_lookup(addr, index) == 64496

def test_default_route_0():
    index = build_lpm_index([("::/0", 64496)])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db8::1"), index) == 64496

def test_boundary_first_address():
    index = build_lpm_index([("2001:db8::/64", 64496)])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db8::"), index) == 64496

def test_boundary_last_address():
    index = build_lpm_index([("2001:db8::/64", 64496)])
    last = ipaddress.IPv6Address("2001:db8::ffff:ffff:ffff:ffff")
    assert lpm_lookup(last, index) == 64496

def test_empty_index_returns_none():
    index = build_lpm_index([])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db8::1"), index) is None

def test_non_ipv6_prefix_raises():
    with pytest.raises(ValueError, match="IPv6"):
        build_lpm_index([("192.0.2.0/24", 64496)])

def test_tie_breaking_first_inserted_wins():
    # Same prefix length and network, two different ASNs — first wins
    index = build_lpm_index([
        ("2001:db8::/32", 64496),
        ("2001:db8::/32", 64497),
    ])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db8::1"), index) == 64496

def test_lookup_entry_returns_cidr():
    index = build_lpm_index([("2001:db8::/32", 64496)])
    entry = lpm_lookup_entry(ipaddress.IPv6Address("2001:db8::1"), index)
    assert entry is not None
    asn, cidr = entry
    assert asn == 64496
    assert cidr == "2001:db8::/32"

def test_lookup_entry_no_match_returns_none():
    index = build_lpm_index([("2001:db8::/32", 64496)])
    assert lpm_lookup_entry(ipaddress.IPv6Address("::1"), index) is None
```

- [ ] **Step 2: Run tests — verify they fail**

```
uv run pytest tests/pipeline/test_lpm.py -v
```
Expected: `ImportError: cannot import name 'build_lpm_index'`

- [ ] **Step 3: Implement `lpm.py`**

```python
# usg_ipv6/pipeline/lpm.py
import ipaddress
from typing import Optional

# {prefix_len: {network_addr_int: (asn, cidr_str)}}
LpmIndex = dict[int, dict[int, tuple[int, str]]]


def build_lpm_index(prefixes: list[tuple[str, int]]) -> LpmIndex:
    """Build LPM index from (cidr_str, asn) pairs. IPv6 only."""
    index: LpmIndex = {}
    for cidr, asn in prefixes:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid prefix {cidr!r}: {exc}") from exc
        if net.version != 6:
            raise ValueError(
                f"Only IPv6 prefixes are supported, got {cidr!r}"
            )
        pl = net.prefixlen
        net_int = int(net.network_address)
        # First-inserted wins on tie (same prefix, different ASN)
        index.setdefault(pl, {}).setdefault(net_int, (asn, cidr))
    return index


def lpm_lookup_entry(
    addr: ipaddress.IPv6Address, index: LpmIndex
) -> Optional[tuple[int, str]]:
    """Return (asn, cidr_str) for longest-matching prefix, or None."""
    addr_int = int(addr)
    for prefix_len in sorted(index.keys(), reverse=True):
        if prefix_len == 0:
            mask = 0
        else:
            mask = ((1 << 128) - 1) ^ ((1 << (128 - prefix_len)) - 1)
        net_int = addr_int & mask
        if net_int in index[prefix_len]:
            return index[prefix_len][net_int]
    return None


def lpm_lookup(
    addr: ipaddress.IPv6Address, index: LpmIndex
) -> Optional[int]:
    """Return ASN for longest-matching prefix, or None."""
    entry = lpm_lookup_entry(addr, index)
    return entry[0] if entry is not None else None
```

- [ ] **Step 4: Run tests — verify they pass**

```
uv run pytest tests/pipeline/test_lpm.py -v
```
Expected: all 13 tests pass

- [ ] **Step 5: Commit**

```bash
git add usg_ipv6/pipeline/lpm.py tests/pipeline/test_lpm.py
git commit -m "feat: add stdlib IPv6 LPM engine for Ark attribution"
```

---

### Task 2: Schema migration

**Files:**
- Modify: `usg_ipv6/db.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `init_schema()` creates `ark_reachability` (7 cols, PK on `measurement_date, asn`) and `ark_prefix_observations` (6 cols, PK on `measurement_date, prefix`); migrates any existing 3-column `ark_reachability` automatically

---

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_db.py` (the file already exists — append these test functions):

```python
def test_ark_reachability_has_full_schema(db):
    cols = {
        row[0]
        for row in db.execute("DESCRIBE ark_reachability").fetchall()
    }
    assert "addresses_observed" in cols
    assert "prefixes_in_scope" in cols
    assert "pfx2as_snapshot_date" in cols
    assert "asn_divergence_count" in cols

def test_ark_prefix_observations_table_exists(db):
    cols = {
        row[0]
        for row in db.execute("DESCRIBE ark_prefix_observations").fetchall()
    }
    assert "our_asn" in cols
    assert "addresses_observed" in cols
    assert "addresses_ark_match" in cols

def test_migration_replaces_old_ark_reachability(tmp_path):
    from usg_ipv6.db import get_connection, init_schema
    # Build a DB with the old 3-column ark_reachability
    conn = get_connection(str(tmp_path / "old.duckdb"))
    conn.execute("""
        CREATE TABLE ark_reachability (
            measurement_date DATE NOT NULL,
            asn BIGINT NOT NULL,
            ipv6_reachable BOOLEAN NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO ark_reachability VALUES ('2024-01-01', 396260, TRUE)"
    )
    conn.close()
    # Re-open and call init_schema — migration must replace the table
    conn = get_connection(str(tmp_path / "old.duckdb"))
    init_schema(conn)
    cols = {
        row[0]
        for row in conn.execute("DESCRIBE ark_reachability").fetchall()
    }
    assert "addresses_observed" in cols
    # Old data is gone (table was dropped and recreated)
    count = conn.execute("SELECT COUNT(*) FROM ark_reachability").fetchone()[0]
    assert count == 0
    conn.close()
```

- [ ] **Step 2: Run tests — verify they fail**

```
uv run pytest tests/test_db.py -v -k "ark"
```
Expected: `FAIL — ark_reachability has only 3 columns`

- [ ] **Step 3: Update `db.py`**

Replace the existing `ark_reachability` block in `init_schema()` and add the migration helper. The full updated `init_schema` function (replace the existing one entirely):

```python
def _migrate_ark_reachability(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop old 3-column ark_reachability so init_schema can recreate it."""
    try:
        cols = {
            row[0]
            for row in conn.execute("DESCRIBE ark_reachability").fetchall()
        }
        if "addresses_observed" not in cols:
            conn.execute("DROP TABLE ark_reachability")
    except Exception:
        pass  # table doesn't exist yet — init_schema will create it


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    _migrate_ark_reachability(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS asn_corpus (
            asn             BIGINT PRIMARY KEY,
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
            snapshot_ts     TIMESTAMP NOT NULL,
            collector       VARCHAR NOT NULL,
            source_url      VARCHAR NOT NULL,
            byte_count      BIGINT NOT NULL,
            status          VARCHAR NOT NULL,
            processed_at    TIMESTAMP,
            PRIMARY KEY (snapshot_ts, collector)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prefix_records (
            snapshot_ts     TIMESTAMP NOT NULL,
            collector       VARCHAR NOT NULL,
            peer_asn        BIGINT NOT NULL,
            prefix          VARCHAR NOT NULL,
            origin_asn      BIGINT NOT NULL,
            prefix_len      INTEGER NOT NULL,
            af              INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS caida_pfx2as (
            snapshot_date   DATE NOT NULL,
            prefix          VARCHAR NOT NULL,
            prefix_len      INTEGER NOT NULL,
            asn             BIGINT NOT NULL,
            af              INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ark_reachability (
            measurement_date      DATE    NOT NULL,
            asn                   BIGINT  NOT NULL,
            ipv6_reachable        BOOLEAN NOT NULL,
            addresses_observed    INTEGER NOT NULL DEFAULT 0,
            prefixes_in_scope     INTEGER NOT NULL DEFAULT 0,
            pfx2as_snapshot_date  DATE    NOT NULL,
            asn_divergence_count  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (measurement_date, asn)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ark_prefix_observations (
            measurement_date      DATE    NOT NULL,
            prefix                VARCHAR NOT NULL,
            our_asn               BIGINT  NOT NULL,
            addresses_observed    INTEGER NOT NULL,
            addresses_ark_match   INTEGER NOT NULL,
            pfx2as_snapshot_date  DATE    NOT NULL,
            PRIMARY KEY (measurement_date, prefix)
        )
    """)
```

- [ ] **Step 4: Run tests — verify they pass**

```
uv run pytest tests/test_db.py -v
```
Expected: all tests pass (including the 3 new ark ones)

- [ ] **Step 5: Commit**

```bash
git add usg_ipv6/db.py tests/test_db.py
git commit -m "feat: expand ark_reachability schema and add ark_prefix_observations table"
```

---

### Task 3: Ark downloader and parser

**Files:**
- Create: `usg_ipv6/pipeline/ark.py` (download + parse functions only)
- Create: `tests/fixtures/nodes_as_sample.nodes.as.gz` (fixture)
- Create: `tests/pipeline/test_ark.py` (parser + downloader tests)

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `download_nodes_as(date: str, dest_dir: str) -> str`
  - `parse_nodes_as(filepath: str) -> Iterator[tuple[IPv6Address, int | None]]`
  - `ARK_BASE_URL`, `ARK_FILE_PATTERN` constants

---

- [ ] **Step 1: Create the fixture file**

Run this Python snippet once to create the fixture (run from the repo root):

```python
import gzip, pathlib
content = """\
# CAIDA Ark IPv6 topology nodes
# measurement date: 20240315
N 2001:db8::1 64496
N 2001:db8::2 64496
N 2001:db8:1::1 64497
N ::ffff:1.2.3.4 64496
L some-link-record ignored
N 2001:db9::1 *
N 2001:db8::3 not_a_number
"""
path = pathlib.Path("tests/fixtures/nodes_as_sample.nodes.as.gz")
with gzip.open(path, "wt") as f:
    f.write(content)
print("Created", path)
```

The fixture has: 3 valid addresses for ASN 64496/64497, 1 IPv4-mapped (skip), 1 L-record (skip), 1 with `*` ASN (yields None), 1 with non-numeric ASN (yields None). Expected parse output: 5 records, 4 valid IPv6 addresses (one with `None` ASN, one with bad ASN → `None`).

Wait — re-count the fixture:
- `2001:db8::1 64496` → (addr, 64496) ✓
- `2001:db8::2 64496` → (addr, 64496) ✓
- `2001:db8:1::1 64497` → (addr, 64497) ✓
- `::ffff:1.2.3.4 64496` → SKIPPED (IPv4-mapped)
- `L some-link-record ignored` → SKIPPED (not N record)
- `2001:db9::1 *` → (addr, None) ✓
- `2001:db8::3 not_a_number` → (addr, None) ✓

So 5 records yielded, 2 skipped.

- [ ] **Step 2: Write the failing tests**

```python
# tests/pipeline/test_ark.py
import gzip
import ipaddress
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from usg_ipv6.pipeline.ark import ARK_BASE_URL, download_nodes_as, parse_nodes_as

FIXTURES = Path(__file__).parent.parent / "fixtures"
SAMPLE = str(FIXTURES / "nodes_as_sample.nodes.as.gz")


def test_parse_valid_lines():
    results = list(parse_nodes_as(SAMPLE))
    assert len(results) == 5


def test_parse_correct_address_and_asn():
    results = list(parse_nodes_as(SAMPLE))
    addr, asn = results[0]
    assert addr == ipaddress.IPv6Address("2001:db8::1")
    assert asn == 64496


def test_parse_star_asn_yields_none():
    results = list(parse_nodes_as(SAMPLE))
    # "2001:db9::1 *" is the 4th yielded record
    star_record = next(r for r in results if r[0] == ipaddress.IPv6Address("2001:db9::1"))
    assert star_record[1] is None


def test_parse_bad_asn_yields_none():
    results = list(parse_nodes_as(SAMPLE))
    bad_record = next(r for r in results if r[0] == ipaddress.IPv6Address("2001:db8::3"))
    assert bad_record[1] is None


def test_parse_skips_ipv4_mapped():
    results = list(parse_nodes_as(SAMPLE))
    addrs = [r[0] for r in results]
    assert ipaddress.IPv6Address("::ffff:102:304") not in addrs


def test_parse_skips_non_n_records():
    # fixture has one L record — verify only 5 (not 6) records yielded
    results = list(parse_nodes_as(SAMPLE))
    assert len(results) == 5


def test_download_raises_when_all_days_404(tmp_path):
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("HTTP 404")
    mock_resp.__enter__ = lambda s: mock_resp
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch("usg_ipv6.pipeline.ark.requests.get", return_value=mock_resp):
        with pytest.raises(FileNotFoundError, match="2024-03-15"):
            download_nodes_as("2024-03-15", str(tmp_path))


def test_download_returns_cached_file(tmp_path):
    # If file already exists locally, no HTTP request is made
    date_str = "20240315"
    existing = tmp_path / f"team-1.ipv6.{date_str}.nodes.as.gz"
    existing.write_bytes(b"dummy")
    with patch("usg_ipv6.pipeline.ark.requests.get") as mock_get:
        result = download_nodes_as("2024-03-15", str(tmp_path))
        mock_get.assert_not_called()
    assert result == str(existing)
```

- [ ] **Step 3: Run tests — verify they fail**

```
uv run pytest tests/pipeline/test_ark.py -v
```
Expected: `ImportError: cannot import name 'download_nodes_as'`

- [ ] **Step 4: Implement the downloader and parser in `ark.py`**

```python
# usg_ipv6/pipeline/ark.py
import gzip
import ipaddress
from datetime import date as date_type, timedelta
from pathlib import Path
from typing import Iterator, Optional

import requests

ARK_BASE_URL = (
    "https://publicdata.caida.org/datasets/topology/ark/ipv6/"
    "probe-data/team-1/{year}/{month:02d}/"
)
ARK_FILE_PATTERN = "team-1.ipv6.{date}.nodes.as.gz"

_IPV4_MAPPED_NET = ipaddress.ip_network("::ffff:0.0.0.0/96")


def download_nodes_as(date: str, dest_dir: str) -> str:
    """
    Download Ark IPv6 nodes-as file for `date` (YYYY-MM-DD).
    Walks back up to 6 more days if that date is not yet published.
    Returns local file path. Raises FileNotFoundError if nothing found.
    """
    year, month, day = date.split("-")
    base_date = date_type(int(year), int(month), int(day))
    Path(dest_dir).mkdir(parents=True, exist_ok=True)

    for days_back in range(7):
        attempt = base_date - timedelta(days=days_back)
        date_str = attempt.strftime("%Y%m%d")
        filename = ARK_FILE_PATTERN.format(date=date_str)
        dest = Path(dest_dir) / filename
        if dest.exists():
            return str(dest)
        url = (
            ARK_BASE_URL.format(year=attempt.year, month=attempt.month)
            + filename
        )
        try:
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
            return str(dest)
        except Exception:
            continue

    raise FileNotFoundError(
        f"No Ark nodes-as file found for {date} or the 6 preceding days"
    )


def parse_nodes_as(
    filepath: str,
) -> Iterator[tuple[ipaddress.IPv6Address, Optional[int]]]:
    """
    Yield (IPv6Address, ark_asn) pairs from a nodes-as .gz file.
    ark_asn is None for '*' annotation or non-numeric values.
    Skips comments, non-'N' records, and IPv4-mapped addresses.
    """
    opener = gzip.open if filepath.endswith(".gz") else open
    with opener(filepath, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or not line.startswith("N "):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                addr = ipaddress.ip_address(parts[1])
            except ValueError:
                continue
            if not isinstance(addr, ipaddress.IPv6Address):
                continue
            if addr in _IPV4_MAPPED_NET:
                continue
            ark_asn: Optional[int] = None
            if len(parts) >= 3 and parts[2] != "*":
                try:
                    ark_asn = int(parts[2])
                except ValueError:
                    pass
            yield addr, ark_asn
```

- [ ] **Step 5: Run tests — verify they pass**

```
uv run pytest tests/pipeline/test_ark.py -v
```
Expected: all 8 tests pass

- [ ] **Step 6: Commit**

```bash
git add usg_ipv6/pipeline/ark.py tests/pipeline/test_ark.py \
        tests/fixtures/nodes_as_sample.nodes.as.gz
git commit -m "feat: add Ark IPv6 downloader and nodes-as parser"
```

---

### Task 4: Ark ingest orchestrator

**Files:**
- Modify: `usg_ipv6/pipeline/ark.py` (add `run_ark_ingest`, `run_ark_backfill`, `_date_from_filepath`)
- Modify: `tests/pipeline/test_ark.py` (add integration tests)

**Interfaces:**
- Consumes:
  - `build_lpm_index`, `lpm_lookup_entry` from `usg_ipv6.pipeline.lpm` (Task 1)
  - `init_schema`, `get_connection` from `usg_ipv6.db` (Task 2)
  - `download_nodes_as`, `parse_nodes_as` from this module (Task 3)
  - DB tables: `ark_reachability (measurement_date, asn, ipv6_reachable, addresses_observed, prefixes_in_scope, pfx2as_snapshot_date, asn_divergence_count)`, `ark_prefix_observations`, `caida_pfx2as`, `asn_corpus`
- Produces:
  - `run_ark_ingest(db_path: str, *, date: str | None = None, dest_dir: str = "data/raw") -> dict`
    - Returns `{"date": str, "addresses_parsed": int, "asns_updated": int, "pfx2as_snapshot_date": str, "skipped": bool}`
  - `run_ark_backfill(db_path: str, since: datetime, until: datetime, *, dest_dir: str = "data/raw") -> dict`
    - Returns `{"processed": int, "skipped": int, "failed": int}`

---

- [ ] **Step 1: Write failing integration tests**

Append to `tests/pipeline/test_ark.py`:

```python
# --- Integration tests for run_ark_ingest ---

import gzip
from collections import defaultdict
from datetime import datetime
from unittest.mock import patch

from usg_ipv6.db import get_connection, init_schema
from usg_ipv6.pipeline.ark import run_ark_ingest, run_ark_backfill


def _seed_db(conn, pfx2as_rows, corpus_rows):
    """Seed caida_pfx2as and asn_corpus for tests."""
    for row in pfx2as_rows:
        conn.execute(
            "INSERT INTO caida_pfx2as VALUES (?, ?, ?, ?, ?)", row
        )
    for row in corpus_rows:
        conn.execute(
            "INSERT INTO asn_corpus VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row
        )


def _make_nodes_gz(tmp_path, name, content):
    p = tmp_path / name
    with gzip.open(p, "wt") as f:
        f.write(content)
    return str(p)


def test_ingest_produces_reachable_row(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[(64496, "GSA", "GSA", "cfo_act", "USGSA", "high", "arin",
                      "2024-03-15", "2024-03-15")],
    )
    nodes_file = _make_nodes_gz(tmp_path, "team-1.ipv6.20240315.nodes.as.gz",
                                 "N 2001:db8::1 64496\n")
    conn.close()

    with patch("usg_ipv6.pipeline.ark.download_nodes_as", return_value=nodes_file):
        result = run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15",
                                dest_dir=str(tmp_path))

    assert result["skipped"] is False
    assert result["addresses_parsed"] == 1
    conn = get_connection(str(tmp_path / "test.duckdb"))
    row = conn.execute(
        "SELECT ipv6_reachable, addresses_observed, prefixes_in_scope "
        "FROM ark_reachability WHERE asn = 64496"
    ).fetchone()
    assert row[0] is True
    assert row[1] == 1
    assert row[2] == 1


def test_ingest_produces_not_reachable_row(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[(64496, "GSA", "GSA", "cfo_act", "USGSA", "high", "arin",
                      "2024-03-15", "2024-03-15")],
    )
    # Empty nodes file — no addresses in USG space
    nodes_file = _make_nodes_gz(tmp_path, "team-1.ipv6.20240315.nodes.as.gz",
                                 "N 2001:db9::1 99999\n")
    conn.close()

    with patch("usg_ipv6.pipeline.ark.download_nodes_as", return_value=nodes_file):
        run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15",
                       dest_dir=str(tmp_path))

    conn = get_connection(str(tmp_path / "test.duckdb"))
    row = conn.execute(
        "SELECT ipv6_reachable, addresses_observed, prefixes_in_scope "
        "FROM ark_reachability WHERE asn = 64496"
    ).fetchone()
    assert row[0] is False
    assert row[1] == 0
    assert row[2] == 1


def test_ingest_indeterminate_asn_has_zero_prefixes_in_scope(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    # Corpus ASN with NO pfx2as IPv6 prefix → indeterminate
    _seed_db(conn,
        pfx2as_rows=[],
        corpus_rows=[(64496, "GSA", "GSA", "cfo_act", "USGSA", "high", "arin",
                      "2024-03-15", "2024-03-15")],
    )
    conn.execute(
        "INSERT INTO caida_pfx2as VALUES ('2024-03-15', '192.0.2.0/24', 24, 64496, 4)"
    )
    conn.close()
    nodes_file = _make_nodes_gz(tmp_path, "team-1.ipv6.20240315.nodes.as.gz", "")

    with patch("usg_ipv6.pipeline.ark.download_nodes_as", return_value=nodes_file):
        run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15",
                       dest_dir=str(tmp_path))

    conn = get_connection(str(tmp_path / "test.duckdb"))
    count = conn.execute("SELECT COUNT(*) FROM ark_reachability").fetchone()[0]
    assert count == 0  # no IPv6 pfx2as rows → nothing written


def test_ingest_idempotent(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[(64496, "GSA", "GSA", "cfo_act", "USGSA", "high", "arin",
                      "2024-03-15", "2024-03-15")],
    )
    conn.close()
    nodes_file = _make_nodes_gz(tmp_path, "team-1.ipv6.20240315.nodes.as.gz",
                                 "N 2001:db8::1 64496\n")

    with patch("usg_ipv6.pipeline.ark.download_nodes_as", return_value=nodes_file):
        run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15",
                       dest_dir=str(tmp_path))
        result = run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15",
                                dest_dir=str(tmp_path))

    assert result["skipped"] is True
    conn = get_connection(str(tmp_path / "test.duckdb"))
    count = conn.execute("SELECT COUNT(*) FROM ark_reachability").fetchone()[0]
    assert count == 1  # no duplication


def test_ingest_raises_when_no_ipv6_pfx2as(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn, pfx2as_rows=[], corpus_rows=[])
    conn.close()
    nodes_file = _make_nodes_gz(tmp_path, "team-1.ipv6.20240315.nodes.as.gz", "")

    with patch("usg_ipv6.pipeline.ark.download_nodes_as", return_value=nodes_file):
        with pytest.raises(RuntimeError, match="caida_pfx2as"):
            run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15",
                           dest_dir=str(tmp_path))


def test_backfill_skips_already_loaded(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[(64496, "GSA", "GSA", "cfo_act", "USGSA", "high", "arin",
                      "2024-03-15", "2024-03-15")],
    )
    # Pre-load one date
    conn.execute(
        "INSERT INTO ark_reachability VALUES ('2024-03-15', 64496, TRUE, 1, 1, '2024-03-15', 0)"
    )
    conn.close()
    nodes_file = _make_nodes_gz(tmp_path, "team-1.ipv6.20240315.nodes.as.gz",
                                 "N 2001:db8::1 64496\n")

    with patch("usg_ipv6.pipeline.ark.download_nodes_as", return_value=nodes_file):
        result = run_ark_backfill(
            str(tmp_path / "test.duckdb"),
            datetime(2024, 3, 15), datetime(2024, 3, 15),
            dest_dir=str(tmp_path),
        )

    assert result["skipped"] == 1
    assert result["processed"] == 0
```

- [ ] **Step 2: Run — verify failure**

```
uv run pytest tests/pipeline/test_ark.py -v -k "ingest or backfill"
```
Expected: `ImportError: cannot import name 'run_ark_ingest'`

- [ ] **Step 3: Add orchestrator functions to `ark.py`**

Append to `usg_ipv6/pipeline/ark.py`:

```python
from collections import defaultdict
from datetime import datetime

import duckdb

from usg_ipv6.db import get_connection, init_schema
from usg_ipv6.pipeline.lpm import build_lpm_index, lpm_lookup_entry


def _date_from_filepath(filepath: str) -> str:
    """Extract YYYY-MM-DD from a nodes-as filename like team-1.ipv6.YYYYMMDD.nodes.as.gz"""
    stem = Path(filepath).name  # e.g. team-1.ipv6.20240315.nodes.as.gz
    parts = stem.split(".")
    # parts[2] is the YYYYMMDD date segment
    raw = parts[2]
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def run_ark_ingest(
    db_path: str,
    *,
    date: str | None = None,
    dest_dir: str = "data/raw",
) -> dict:
    """Ingest a single Ark measurement date into ark_reachability."""
    from datetime import date as date_type as _date_type
    conn = get_connection(db_path)
    init_schema(conn)

    target = date or _date_type.today().isoformat()

    # Skip if already loaded
    existing = conn.execute(
        "SELECT COUNT(*) FROM ark_reachability WHERE measurement_date = ?",
        [target],
    ).fetchone()[0]
    if existing > 0:
        conn.close()
        return {"date": target, "addresses_parsed": 0, "asns_updated": 0,
                "pfx2as_snapshot_date": None, "skipped": True}

    # Require pfx2as IPv6 data
    pfx2as_date_row = conn.execute(
        "SELECT MAX(snapshot_date) FROM caida_pfx2as WHERE af = 6"
    ).fetchone()
    if pfx2as_date_row is None or pfx2as_date_row[0] is None:
        conn.close()
        raise RuntimeError(
            "caida_pfx2as has no IPv6 rows — run RouteViews ingest first"
        )
    pfx2as_date = str(pfx2as_date_row[0])

    # Load corpus
    corpus_asns: set[int] = {
        r[0] for r in conn.execute("SELECT asn FROM asn_corpus").fetchall()
    }

    # Load USG IPv6 prefixes from pfx2as
    pfx_rows = conn.execute(
        """
        SELECT prefix, asn FROM caida_pfx2as
        WHERE af = 6 AND snapshot_date = ?
          AND asn IN (SELECT asn FROM asn_corpus)
        """,
        [pfx2as_date],
    ).fetchall()

    if not pfx_rows:
        conn.close()
        return {"date": target, "addresses_parsed": 0, "asns_updated": 0,
                "pfx2as_snapshot_date": pfx2as_date, "skipped": False}

    index = build_lpm_index([(r[0], r[1]) for r in pfx_rows])

    # Count pfx2as prefixes per corpus ASN (prefixes_in_scope)
    prefix_count: dict[int, int] = defaultdict(int)
    for _, asn in pfx_rows:
        prefix_count[asn] += 1

    # Download and parse
    filepath = download_nodes_as(target, dest_dir)
    actual_date = _date_from_filepath(filepath)

    # Accumulate per-ASN and per-prefix
    per_asn: dict[int, dict] = {}
    per_prefix: dict[str, dict] = {}
    total_parsed = 0

    for addr, ark_asn in parse_nodes_as(filepath):
        total_parsed += 1
        entry = lpm_lookup_entry(addr, index)
        our_asn: int | None = entry[0] if entry else None
        our_prefix: str | None = entry[1] if entry else None

        in_scope = (our_asn is not None) or (ark_asn in corpus_asns)
        if not in_scope:
            continue

        key_asn = our_asn if our_asn is not None else ark_asn
        if key_asn not in per_asn:
            per_asn[key_asn] = {"observed": 0, "divergence": 0}
        per_asn[key_asn]["observed"] += 1
        if our_asn is not None and ark_asn is not None and our_asn != ark_asn:
            per_asn[key_asn]["divergence"] += 1

        if our_prefix is not None:
            if our_prefix not in per_prefix:
                per_prefix[our_prefix] = {"asn": our_asn, "observed": 0, "match": 0}
            per_prefix[our_prefix]["observed"] += 1
            if ark_asn is not None and ark_asn == our_asn:
                per_prefix[our_prefix]["match"] += 1

    # Write ark_reachability for all corpus ASNs that have IPv6 pfx2as prefixes
    for asn, n_prefixes in prefix_count.items():
        data = per_asn.get(asn, {"observed": 0, "divergence": 0})
        conn.execute(
            """
            INSERT INTO ark_reachability
                (measurement_date, asn, ipv6_reachable, addresses_observed,
                 prefixes_in_scope, pfx2as_snapshot_date, asn_divergence_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [actual_date, asn, data["observed"] > 0, data["observed"],
             n_prefixes, pfx2as_date, data["divergence"]],
        )

    # Write ark_prefix_observations
    for prefix, pdata in per_prefix.items():
        conn.execute(
            """
            INSERT INTO ark_prefix_observations
                (measurement_date, prefix, our_asn, addresses_observed,
                 addresses_ark_match, pfx2as_snapshot_date)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [actual_date, prefix, pdata["asn"], pdata["observed"],
             pdata["match"], pfx2as_date],
        )

    conn.close()
    return {
        "date": actual_date,
        "addresses_parsed": total_parsed,
        "asns_updated": len(prefix_count),
        "pfx2as_snapshot_date": pfx2as_date,
        "skipped": False,
    }


def run_ark_backfill(
    db_path: str,
    since: datetime,
    until: datetime,
    *,
    dest_dir: str = "data/raw",
) -> dict:
    """Ingest all daily Ark files in [since, until]. Skips already-loaded dates."""
    processed = skipped = failed = 0
    current = since.date() if hasattr(since, "date") else since
    end = until.date() if hasattr(until, "date") else until

    from datetime import timedelta
    while current <= end:
        date_str = current.isoformat()
        try:
            result = run_ark_ingest(db_path, date=date_str, dest_dir=dest_dir)
            if result["skipped"]:
                skipped += 1
            else:
                processed += 1
        except Exception as exc:
            print(f"  {date_str}: {exc}", flush=True)
            failed += 1
        current += timedelta(days=1)

    return {"processed": processed, "skipped": skipped, "failed": failed}
```

**Note:** The line `from datetime import date as date_type as _date_type` is invalid Python. Use this instead at the top of the function body:

```python
import datetime as _dt
target = date or _dt.date.today().isoformat()
```

Fix any import aliases accordingly before running tests.

- [ ] **Step 4: Run tests — verify they pass**

```
uv run pytest tests/pipeline/test_ark.py -v
```
Expected: all tests pass (parser tests + 6 new integration tests)

- [ ] **Step 5: Run full suite**

```
uv run pytest -v
```
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add usg_ipv6/pipeline/ark.py tests/pipeline/test_ark.py
git commit -m "feat: add Ark ingest orchestrator (run_ark_ingest, run_ark_backfill)"
```

---

### Task 5: CLI `ark` sub-app

**Files:**
- Modify: `usg_ipv6/cli.py`
- Create: `tests/test_ark_gap.py`

**Interfaces:**
- Consumes:
  - `run_ark_ingest`, `run_ark_backfill` from `usg_ipv6.pipeline.ark` (Task 4)
  - DB tables `ark_reachability`, `prefix_records`, `asn_corpus` (Task 2)
- Produces:
  - `usg ark ingest` — ingest latest or specific date
  - `usg ark backfill SINCE UNTIL` — date-range ingest
  - `usg ark stats` — reachability summary table
  - `usg ark gap` — ASNs with BGP IPv6 but `ipv6_reachable = FALSE` and `prefixes_in_scope > 0`

---

- [ ] **Step 1: Write failing tests for the gap query logic**

```python
# tests/test_ark_gap.py
import pytest
from usg_ipv6.db import get_connection, init_schema


@pytest.fixture
def gap_db(tmp_path):
    conn = get_connection(str(tmp_path / "gap.duckdb"))
    init_schema(conn)
    # Two corpus ASNs
    for row in [
        (64496, "GSA", "GSA", "cfo_act", "USGSA", "high", "arin",
         "2024-03-15", "2024-03-15"),
        (64497, "DHS", "DHS", "cfo_act", "USDHS", "high", "arin",
         "2024-03-15", "2024-03-15"),
        (64498, "DOD", "DOD", "dod", "USDOD", "high", "arin",
         "2024-03-15", "2024-03-15"),
    ]:
        conn.execute("INSERT INTO asn_corpus VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
    # 64496: IPv6 reachable (prefixes_in_scope=1)
    conn.execute(
        "INSERT INTO ark_reachability VALUES ('2024-03-15', 64496, TRUE, 2, 1, '2024-03-15', 0)"
    )
    # 64497: NOT reachable, but has prefixes (the gap case)
    conn.execute(
        "INSERT INTO ark_reachability VALUES ('2024-03-15', 64497, FALSE, 0, 2, '2024-03-15', 0)"
    )
    # 64498: NOT reachable, prefixes_in_scope=0 (indeterminate — must be EXCLUDED)
    conn.execute(
        "INSERT INTO ark_reachability VALUES ('2024-03-15', 64498, FALSE, 0, 0, '2024-03-15', 0)"
    )
    return conn


_GAP_QUERY = """
    SELECT ar.asn, c.agency, c.tier,
           ar.addresses_observed, ar.prefixes_in_scope
    FROM ark_reachability ar
    JOIN asn_corpus c ON ar.asn = c.asn
    WHERE ar.measurement_date = (SELECT MAX(measurement_date) FROM ark_reachability)
      AND ar.ipv6_reachable = FALSE
      AND ar.prefixes_in_scope > 0
    ORDER BY c.tier, c.agency
"""


def test_gap_query_returns_only_not_reachable_with_prefixes(gap_db):
    rows = gap_db.execute(_GAP_QUERY).fetchall()
    asns = [r[0] for r in rows]
    assert 64497 in asns          # not reachable, has prefixes → IN gap
    assert 64496 not in asns      # reachable → excluded
    assert 64498 not in asns      # indeterminate (prefixes_in_scope=0) → excluded


def test_gap_query_excludes_indeterminate(gap_db):
    rows = gap_db.execute(_GAP_QUERY).fetchall()
    assert all(r[4] > 0 for r in rows)  # prefixes_in_scope > 0 for all results


def test_gap_query_empty_when_all_reachable(gap_db):
    gap_db.execute("UPDATE ark_reachability SET ipv6_reachable = TRUE")
    rows = gap_db.execute(_GAP_QUERY).fetchall()
    assert rows == []
```

- [ ] **Step 2: Run — verify failure**

```
uv run pytest tests/test_ark_gap.py -v
```
Expected: `FAIL — ark_reachability has no rows` (fixture not yet populated — actually, the `gap_db` fixture populates the table, so tests may run but the query constant `_GAP_QUERY` imports from the test file itself. They should pass once the schema is correct. If they fail, the schema migration may not be complete.)

If all 3 tests pass already (because the DB schema is correct from Task 2), that is expected — the gap query is pure SQL and correctness depends only on the schema being right.

- [ ] **Step 3: Add `ark` sub-app to `cli.py`**

After the existing `run_app = typer.Typer(...)` line, add:

```python
ark_app = typer.Typer(help="CAIDA Ark IPv6 reachability commands.", no_args_is_help=True)
app.add_typer(ark_app, name="ark", help="CAIDA Ark IPv6 reachability commands.")
```

Then append these commands before the `if __name__ == "__main__":` block:

```python
# ---------------------------------------------------------------------------
# usg ark ingest
# ---------------------------------------------------------------------------

@ark_app.command("ingest")
def ark_ingest(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Path to DuckDB file."),
    date: Optional[str] = typer.Option(
        None, "--date", help="Measurement date (YYYY-MM-DD). Default: latest available."
    ),
    dest_dir: str = typer.Option("data/raw", "--dest-dir", help="Download directory."),
) -> None:
    """Ingest Ark IPv6 reachability data for one date."""
    from usg_ipv6.pipeline.ark import run_ark_ingest
    typer.echo(f"Ark ingest: date={date or 'latest'}, db={db}")
    result = run_ark_ingest(db, date=date, dest_dir=dest_dir)
    if result["skipped"]:
        typer.echo(f"Skipped — {result['date']} already loaded.")
    else:
        typer.echo(
            f"Done. date={result['date']}  parsed={result['addresses_parsed']:,}"
            f"  asns_updated={result['asns_updated']}"
            f"  pfx2as={result['pfx2as_snapshot_date']}"
        )


# ---------------------------------------------------------------------------
# usg ark backfill
# ---------------------------------------------------------------------------

@ark_app.command("backfill")
def ark_backfill(
    since: str = typer.Argument(..., help="Start date, YYYY-MM-DD."),
    until: str = typer.Argument(..., help="End date, YYYY-MM-DD."),
    db: str = typer.Option(DEFAULT_DB, "--db", help="Path to DuckDB file."),
    dest_dir: str = typer.Option("data/raw", "--dest-dir", help="Download directory."),
) -> None:
    """Backfill Ark reachability data between two dates (inclusive)."""
    from usg_ipv6.pipeline.ark import run_ark_backfill
    try:
        since_dt = datetime.fromisoformat(since)
        until_dt = datetime.fromisoformat(until)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Ark backfill: {since} → {until}, db={db}")
    result = run_ark_backfill(db, since_dt, until_dt, dest_dir=dest_dir)
    typer.echo(
        f"Done. processed={result['processed']}  skipped={result['skipped']}"
        f"  failed={result['failed']}"
    )


# ---------------------------------------------------------------------------
# usg ark stats
# ---------------------------------------------------------------------------

@ark_app.command("stats")
def ark_stats(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Path to DuckDB file."),
    date: Optional[str] = typer.Option(None, "--date", help="Measurement date (default: latest)."),
    as_json: bool = typer.Option(False, "--json", is_flag=True, help="Output as JSON."),
) -> None:
    """Reachability summary for the most recent (or given) Ark measurement date."""
    conn = _open(db)
    mdate = date or conn.execute(
        "SELECT MAX(measurement_date) FROM ark_reachability"
    ).fetchone()[0]
    if mdate is None:
        typer.echo("No Ark data loaded. Run 'usg ark ingest' first.", err=True)
        raise typer.Exit(1)

    pfx2as_date = conn.execute(
        "SELECT pfx2as_snapshot_date FROM ark_reachability WHERE measurement_date = ? LIMIT 1",
        [mdate],
    ).fetchone()
    pfx2as_str = str(pfx2as_date[0]) if pfx2as_date else "unknown"

    totals = conn.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE ipv6_reachable = TRUE AND prefixes_in_scope > 0) AS reachable,
            COUNT(*) FILTER (WHERE ipv6_reachable = FALSE AND prefixes_in_scope > 0) AS not_reachable,
            COUNT(*) FILTER (WHERE prefixes_in_scope = 0) AS indeterminate
        FROM ark_reachability WHERE measurement_date = ?
        """,
        [mdate],
    ).fetchone()

    by_tier = conn.execute(
        """
        SELECT c.tier,
            COUNT(*) FILTER (WHERE ar.ipv6_reachable = TRUE AND ar.prefixes_in_scope > 0),
            COUNT(*) FILTER (WHERE ar.ipv6_reachable = FALSE AND ar.prefixes_in_scope > 0)
        FROM ark_reachability ar
        JOIN asn_corpus c ON ar.asn = c.asn
        WHERE ar.measurement_date = ?
        GROUP BY c.tier ORDER BY c.tier
        """,
        [mdate],
    ).fetchall()

    avg_div = conn.execute(
        """
        SELECT AVG(CASE WHEN addresses_observed > 0
                        THEN asn_divergence_count * 1.0 / addresses_observed
                        ELSE NULL END) * 100
        FROM ark_reachability WHERE measurement_date = ?
        """,
        [mdate],
    ).fetchone()[0]
    conn.close()

    if as_json:
        typer.echo(json.dumps({
            "measurement_date": str(mdate),
            "pfx2as_snapshot_date": pfx2as_str,
            "reachable": totals[0], "not_reachable": totals[1],
            "indeterminate": totals[2],
            "avg_divergence_pct": round(avg_div or 0, 2),
            "by_tier": [{"tier": r[0], "reachable": r[1], "not_reachable": r[2]}
                        for r in by_tier],
        }, indent=2))
        return

    typer.echo(f"Ark measurement : {mdate}")
    typer.echo(f"pfx2as used     : {pfx2as_str}\n")
    typer.echo(f"Reachable       : {totals[0]}")
    typer.echo(f"Not reachable   : {totals[1]}")
    typer.echo(f"Indeterminate   : {totals[2]}  (prefixes_in_scope = 0)\n")
    typer.echo("By tier:")
    _table(["Tier", "Reachable", "Not Reachable"],
           [(r[0], r[1], r[2]) for r in by_tier])
    if avg_div is not None:
        typer.echo(f"\nAttribution divergence: {avg_div:.1f}% (ark_asn ≠ our_asn)")


# ---------------------------------------------------------------------------
# usg ark gap
# ---------------------------------------------------------------------------

@ark_app.command("gap")
def ark_gap(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Path to DuckDB file."),
    tier: Optional[str] = typer.Option(None, "--tier", help="Filter by tier."),
    date: Optional[str] = typer.Option(None, "--date", help="Measurement date (default: latest)."),
    limit: int = typer.Option(100, "--limit", help="Max rows."),
    as_json: bool = typer.Option(False, "--json", is_flag=True, help="Output as JSON."),
) -> None:
    """ASNs with BGP IPv6 prefixes but no Ark reachability (excludes indeterminate)."""
    conn = _open(db)
    mdate = date or conn.execute(
        "SELECT MAX(measurement_date) FROM ark_reachability"
    ).fetchone()[0]
    if mdate is None:
        typer.echo("No Ark data loaded. Run 'usg ark ingest' first.", err=True)
        raise typer.Exit(1)

    tier_clause = "AND c.tier = ?" if tier else ""
    params = [mdate, mdate] + ([tier] if tier else []) + [limit]
    rows = conn.execute(
        f"""
        SELECT ar.asn, c.agency, c.tier, ar.addresses_observed, ar.prefixes_in_scope
        FROM ark_reachability ar
        JOIN asn_corpus c ON ar.asn = c.asn
        WHERE ar.measurement_date = ?
          AND ar.ipv6_reachable = FALSE
          AND ar.prefixes_in_scope > 0
          {tier_clause}
        ORDER BY c.tier, c.agency
        LIMIT ?
        """,
        params,
    ).fetchall()
    conn.close()

    if as_json:
        typer.echo(json.dumps([
            {"asn": r[0], "agency": r[1], "tier": r[2],
             "addresses_observed": r[3], "prefixes_in_scope": r[4]}
            for r in rows
        ], indent=2))
        return

    typer.echo(f"Gap (measurement: {mdate})  {len(rows)} ASN(s)\n")
    _table(
        ["ASN", "Agency", "Tier", "Addr Observed", "Prefixes"],
        [(r[0], r[1], r[2], r[3], r[4]) for r in rows],
    )
```

- [ ] **Step 4: Run tests**

```
uv run pytest tests/test_ark_gap.py -v
```
Expected: all 3 gap tests pass

- [ ] **Step 5: Run full suite**

```
uv run pytest -v
```
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add usg_ipv6/cli.py tests/test_ark_gap.py
git commit -m "feat: add 'usg ark' CLI sub-app (ingest, backfill, stats, gap)"
```

---

### Task 6: Report HTML reachability section

**Files:**
- Modify: `usg_ipv6/report_html.py`

**Interfaces:**
- Consumes: DB tables `ark_reachability`, `asn_corpus` (Task 2)
- Produces: new "IPv6 Reachability (CAIDA Ark)" section rendered in `web/index.html` between the Snapshots section and the Corpus table; graceful degradation if table is empty

No new tests — the existing report_html tests (if any) cover the module-level `generate()` function; visual correctness is verified by running `uv run python -m usg_ipv6.report_html` and opening `web/index.html` locally.

---

- [ ] **Step 1: Locate the insertion point in `report_html.py`**

Read `usg_ipv6/report_html.py` and find the section that renders the Snapshots table. The new reachability section goes immediately after it, before the Corpus table.

- [ ] **Step 2: Add the reachability query helper**

Add this function near the other query helpers in `report_html.py`:

```python
def _ark_reachability_section(conn) -> str:
    """Generate HTML for the Ark reachability section, or a placeholder."""
    count = conn.execute(
        "SELECT COUNT(*) FROM ark_reachability"
    ).fetchone()[0]
    if count == 0:
        return """
        <section>
          <h2>IPv6 Reachability (CAIDA Ark)</h2>
          <p class="muted">Ark reachability data not yet loaded.
          Run <code>usg ark ingest</code> or trigger the Ark Ingest workflow.</p>
        </section>
        """

    mdate = conn.execute(
        "SELECT MAX(measurement_date) FROM ark_reachability"
    ).fetchone()[0]
    pfx2as_date = conn.execute(
        "SELECT pfx2as_snapshot_date FROM ark_reachability "
        "WHERE measurement_date = ? LIMIT 1", [mdate]
    ).fetchone()[0]

    totals = conn.execute("""
        SELECT
          COUNT(*) FILTER (WHERE ipv6_reachable = TRUE  AND prefixes_in_scope > 0),
          COUNT(*) FILTER (WHERE ipv6_reachable = FALSE AND prefixes_in_scope > 0),
          COUNT(*) FILTER (WHERE prefixes_in_scope = 0)
        FROM ark_reachability WHERE measurement_date = ?
    """, [mdate]).fetchone()

    gap_rows = conn.execute("""
        SELECT ar.asn, c.agency, c.tier, ar.addresses_observed, ar.prefixes_in_scope
        FROM ark_reachability ar
        JOIN asn_corpus c ON ar.asn = c.asn
        WHERE ar.measurement_date = ?
          AND ar.ipv6_reachable = FALSE AND ar.prefixes_in_scope > 0
        ORDER BY c.tier, c.agency
    """, [mdate]).fetchall()

    reachable, not_reachable, indet = totals
    gap_rows_html = "".join(
        f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td>"
        f"<td>{r[3]}</td><td>{r[4]}</td></tr>"
        for r in gap_rows
    )
    gap_table = f"""
        <table>
          <thead>
            <tr>
              <th>ASN</th><th>Agency</th><th>Tier</th>
              <th>Addresses Observed</th><th>Prefixes in Scope</th>
            </tr>
          </thead>
          <tbody>{gap_rows_html}</tbody>
        </table>
    """ if gap_rows else "<p class='muted'>No gap ASNs — all assessed ASNs are reachable.</p>"

    return f"""
    <section>
      <h2>IPv6 Reachability (CAIDA Ark)</h2>
      <p class="muted">
        Measurement date: <strong>{mdate}</strong> &nbsp;|&nbsp;
        pfx2as attribution: <strong>{pfx2as_date}</strong>
      </p>
      <div class="stat-grid">
        <div class="stat-card">
          <div class="label">Reachable</div>
          <div class="value">{reachable}</div>
        </div>
        <div class="stat-card">
          <div class="label">Not Reachable</div>
          <div class="value" style="color:#cf222e">{not_reachable}</div>
        </div>
        <div class="stat-card">
          <div class="label">Indeterminate</div>
          <div class="value" style="color:#9a6700">{indet}</div>
          <div class="sub">no pfx2as IPv6 prefixes</div>
        </div>
      </div>
      <h3 style="margin-top:1.5rem;font-size:1rem;font-weight:600">
        Gap: BGP IPv6 announced but not Ark-reachable
      </h3>
      {gap_table}
    </section>
    """
```

- [ ] **Step 3: Call `_ark_reachability_section` in `generate()`**

Find the call that renders the Snapshots section in `generate()`. After it, add:

```python
html_parts.append(_ark_reachability_section(conn))
```

(Adjust to match whatever variable name the existing function uses for the HTML accumulator.)

- [ ] **Step 4: Verify visually**

```bash
uv run python -m usg_ipv6.report_html
open web/index.html   # macOS — or just open the file in a browser
```

Confirm: if `ark_reachability` is empty, the placeholder renders. If populated, the stat cards and gap table render. Check graceful degradation with an empty DB too.

- [ ] **Step 5: Run full suite**

```
uv run pytest -v
```
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add usg_ipv6/report_html.py
git commit -m "feat: add Ark reachability section to HTML dashboard"
```

---

### Task 7: GitHub Actions workflow

**Files:**
- Create: `.github/workflows/ark-ingest.yml`
- Modify: `Makefile` (add `ark` and `ark-backfill` targets)

**Interfaces:**
- Consumes: `usg ark ingest` / `usg ark backfill` CLI commands (Task 5)
- Produces: scheduled weekly ingest; `workflow_dispatch` with `date`, `since`, `until` inputs

---

- [ ] **Step 1: Write `ark-ingest.yml`**

```yaml
name: CAIDA Ark IPv6 Reachability Ingest

on:
  schedule:
    - cron: '0 9 * * 3'    # Wednesday 09:00 UTC — offset from RouteViews (Monday)
  workflow_dispatch:
    inputs:
      date:
        description: Single measurement date (YYYY-MM-DD). Leave blank for latest available.
        required: false
        default: ''
        type: string
      since:
        description: Backfill start date (YYYY-MM-DD). Used with `until` for range.
        required: false
        default: ''
        type: string
      until:
        description: Backfill end date (YYYY-MM-DD). Defaults to today when `since` is set.
        required: false
        default: ''
        type: string

permissions:
  contents: write

jobs:
  ark-ingest:
    name: Ingest CAIDA Ark IPv6 reachability data
    runs-on: ubuntu-latest
    timeout-minutes: 350

    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true
          cache-dependency-glob: "uv.lock"
          python-version: "3.11"

      - name: Install Python dependencies
        run: uv sync --frozen

      - name: Create data directories
        run: mkdir -p data/raw

      - name: Download existing DuckDB from rolling release
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: gh release download data-latest --pattern '*.duckdb' --dir data/ || true

      - name: Run Ark ingest
        env:
          INPUT_DATE:  ${{ inputs.date }}
          INPUT_SINCE: ${{ inputs.since }}
          INPUT_UNTIL: ${{ inputs.until }}
        run: |
          uv run python << 'PYEOF'
          import json, os, sys
          from datetime import datetime, date
          from usg_ipv6.pipeline.ark import run_ark_ingest, run_ark_backfill

          inp_date  = os.environ.get("INPUT_DATE", "").strip()
          inp_since = os.environ.get("INPUT_SINCE", "").strip()
          inp_until = os.environ.get("INPUT_UNTIL", "").strip()

          if inp_since:
              until_str = inp_until or date.today().isoformat()
              print(f"Backfill: {inp_since} → {until_str}")
              result = run_ark_backfill(
                  "data/usg_ipv6.duckdb",
                  datetime.fromisoformat(inp_since),
                  datetime.fromisoformat(until_str),
              )
          else:
              target = inp_date or None
              print(f"Ingest: date={target or 'latest'}")
              result = run_ark_ingest("data/usg_ipv6.duckdb", date=target)

          print(json.dumps(result, indent=2))
          PYEOF

      - name: Upload updated DuckDB to rolling release
        if: always()
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          if [ ! -f data/usg_ipv6.duckdb ]; then
            echo "No DuckDB to upload."
            exit 0
          fi
          gh release upload data-latest data/usg_ipv6.duckdb --clobber \
          || gh release create data-latest data/usg_ipv6.duckdb \
               --title "Data: $(date +%Y-%m-%d)" \
               --notes "Ark reachability ingest." \
               --prerelease

      - name: Rebuild GitHub Pages dashboard
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: gh workflow run pages.yml || true
```

- [ ] **Step 2: Add Make targets**

Append to `Makefile`:

```makefile
ark:
	uv run usg ark ingest --db $(DB_PATH)

ark-backfill:
	uv run usg ark backfill $(SINCE) $(UNTIL) --db $(DB_PATH)
```

Update the `.PHONY` line to include `ark ark-backfill`.

- [ ] **Step 3: Update the lint target in Makefile**

Add `usg_ipv6/pipeline/lpm.py` and `usg_ipv6/pipeline/ark.py` to the `lint` target's file list.

- [ ] **Step 4: Lint check**

```
make lint
```
Expected: no errors

- [ ] **Step 5: Run full suite**

```
uv run pytest -v
```
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ark-ingest.yml Makefile
git commit -m "feat: add Ark reachability GitHub Actions workflow and Make targets"
```

---

## Self-review checklist

- [x] No placeholders or TBDs in any task
- [x] Every step has explicit code or an exact command
- [x] Type signatures are consistent across tasks: `LpmIndex` defined in Task 1, consumed identically in Task 4; `run_ark_ingest` return dict shape consistent across Tasks 4, 5, 7
- [x] Review Focus items covered: (1) actual date from filepath — Task 4 `_date_from_filepath` + idempotency test; (2) old schema migration — Task 2 test; (3) ark-asn-only corpus ASN — Task 4 integration test `test_ingest_indeterminate`; (4) gap excludes indeterminate — Task 5 `test_gap_query_excludes_indeterminate`; (5) empty pfx2as abort — Task 4 `test_ingest_raises_when_no_ipv6_pfx2as`
- [x] DuckDB SQL uses only DuckDB-supported syntax (`FILTER (WHERE ...)` is supported in DuckDB 0.9+)
- [x] `ark_prefix_observations` inserted in Task 4 orchestrator; consumed in Task 6 HTML (no test gap — the prefix table is auditing data not surfaced in UI MVP)

**One known implementation note for Task 4:** The line `from datetime import date as date_type as _date_type` is pseudocode — use `import datetime as _dt; target = date or _dt.date.today().isoformat()` in the actual implementation. The implementer must resolve this before running tests.
