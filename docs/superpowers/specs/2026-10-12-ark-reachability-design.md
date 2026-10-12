# Phase 2: CAIDA Ark IPv6 Reachability — Design Spec

**Goal:** Ingest CAIDA Ark IPv6 active-measurement data to produce a per-ASN reachability verdict for every US Government corpus member, enabling direct comparison of BGP-advertised IPv6 prefixes against observed active reachability — the key figure for OMB M-21-07 compliance research.

**Motivation:** BGP routing table analysis (Phase 1) shows which prefixes are *announced*. It cannot distinguish a correctly-functioning IPv6 deployment from a routing announcement backed by a black-hole. CAIDA Ark probes from ~200 global vantage points provide independent ground truth. The gap between announced and reachable is the primary finding of the research paper.

**Attribution approach:** Option C hybrid. Parse the Ark nodes-as file (which carries both the observed IPv6 address and Ark's own AS annotation). Re-annotate each address using our own CAIDA RouteViews pfx2as dataset via longest-prefix match (LPM). Store both attributions. Divergences between the two are themselves a data product.

**Reproducibility requirement:** Every reachability verdict row records which pfx2as snapshot was used for attribution (`pfx2as_snapshot_date`). A reviewer can re-run the exact same LPM with the same pfx2as snapshot and get the same result.

---

## 1. Data source

### CAIDA Ark IPv6 topology dataset

- Base URL: `https://publicdata.caida.org/datasets/topology/ark/ipv6/probe-data/team-1/{year}/{month:02d}/`
- File pattern: `team-1.ipv6.{YYYYMMDD}.nodes.as.gz`
- Publish cadence: daily; typically available 1–2 days after the measurement date
- File size: ~20–80 MB compressed per day
- Content: one line per observed IPv6 address across all Ark vantage points for that day

### Line format

```text
N <ipv6_address> <as_number>
```

- `N` = node record type (skip any line not starting with `N `)
- `<ipv6_address>` = full IPv6 address as observed in a traceroute response
- `<as_number>` = AS number from Ark's internal pfx2as annotation, or `*` if unknown

**Availability:** Files go back to approximately 2008. For this project the backfill period of interest is January 2021 – present (the OMB M-21-07 mandate period).

---

## 2. New Python modules

### `usg_ipv6/pipeline/lpm.py`

Longest-prefix match engine. No external dependencies — uses Python `ipaddress` stdlib only.

**Public API:**

```python
LpmIndex = dict[int, dict[int, int]]  # {prefix_len: {network_addr_int: asn}}

def build_lpm_index(prefixes: list[tuple[str, int]]) -> LpmIndex:
    """
    Build an LPM index from (cidr_str, asn) pairs.
    IPv6 only. Raises ValueError on any non-IPv6 prefix.
    """

def lpm_lookup(addr: ipaddress.IPv6Address, index: LpmIndex) -> int | None:
    """
    Return the ASN of the longest matching prefix, or None if no match.
    Checks prefix lengths from longest (128) to shortest (0).
    Tie-breaking (two equal-length prefixes covering the same address): returns
    whichever match was inserted first into the index dict. This is a degenerate
    case in a valid routing table but can occur in pfx2as data; the behaviour is
    documented here so it can be tested and reproduced.
    """
```

**Implementation note:** For each prefix length present in the index, mask the address to that length and check for membership in O(1). The USG corpus has at most a few hundred IPv6 prefixes across a small number of distinct prefix lengths (~5–15), making each lookup O(n_prefix_lengths) ≈ O(15) in practice.

### `usg_ipv6/pipeline/ark.py`

Downloader, parser, and ingest orchestrator for Ark data.

**Public API:**

```python
ARK_BASE_URL = (
    "https://publicdata.caida.org/datasets/topology/ark/ipv6/"
    "probe-data/team-1/{year}/{month:02d}/"
)
ARK_FILE_PATTERN = "team-1.ipv6.{date}.nodes.as.gz"

def download_nodes_as(date: str, dest_dir: str) -> str:
    """
    Download the nodes-as file for a given date (YYYY-MM-DD).
    Walks back up to 7 days if the exact date is not yet published.
    Returns the local file path of the downloaded file.
    Raises FileNotFoundError if no file is found within the lookback window.
    """

def parse_nodes_as(
    filepath: str,
) -> Iterator[tuple[ipaddress.IPv6Address, int | None]]:
    """
    Yield (IPv6Address, ark_asn) pairs from a nodes-as .gz file.
    ark_asn is None when the Ark annotation is '*' or missing/malformed.
    Skips comment lines, non-'N' records, and IPv4-mapped IPv6 addresses
    (::ffff:0.0.0.0/96) silently — those are traceroute artifacts and
    cannot meaningfully match IPv6 prefixes.
    """

def run_ark_ingest(
    db_path: str,
    *,
    date: str | None = None,
    dest_dir: str = "data/raw",
) -> dict:
    """
    Ingest a single Ark measurement date. If date is None, use the most
    recent available date (today, walking back up to 7 days).
    Returns {"date": str, "addresses_parsed": int, "asns_updated": int,
             "pfx2as_snapshot_date": str, "skipped": bool}.
    skipped=True means the date was already loaded (idempotent).
    """

def run_ark_backfill(
    db_path: str,
    since: datetime,
    until: datetime,
    *,
    dest_dir: str = "data/raw",
) -> dict:
    """
    Ingest all daily Ark files in [since, until]. Already-loaded dates
    are skipped. Returns {"processed": int, "skipped": int, "failed": int}.
    """
```

**Ingest algorithm for `run_ark_ingest`:**

1. Check `ark_reachability` for an existing row with `measurement_date = date`; if found, return `skipped=True`.
2. Identify the most recent `caida_pfx2as` snapshot date in the DB for `af=6`. Abort with a clear error if none exists — the pfx2as table must be populated before Ark ingest runs.
3. Load all IPv6 `(prefix, asn)` rows from `caida_pfx2as` where `asn IN (SELECT asn FROM asn_corpus)`.
4. Build LPM index from those prefixes.
5. Build a set `corpus_asns` from `asn_corpus`.
6. Download the nodes-as file for `date`.
7. Stream the file. For each `(addr, ark_asn)`:
   - `our_asn = lpm_lookup(addr, index)`
   - `in_scope = (our_asn is not None) or (ark_asn in corpus_asns)`
   - If `in_scope`: accumulate into `per_asn[our_asn or ark_asn]` and `per_prefix` dicts
   - Track agreement: if `our_asn is not None and ark_asn is not None and our_asn != ark_asn`: increment `divergence_count`
8. For every corpus ASN that has `prefixes_in_scope > 0` but zero `addresses_observed`: upsert `ipv6_reachable=FALSE`.
9. For every ASN with `addresses_observed > 0`: upsert `ipv6_reachable=TRUE`.
10. Upsert per-prefix rows into `ark_prefix_observations`.
11. Return summary dict.

**Handling `prefixes_in_scope = 0`:** A corpus ASN with no IPv6 prefixes in the pfx2as snapshot is excluded from the FALSE verdict. Its row is inserted with `ipv6_reachable=FALSE, prefixes_in_scope=0` to mark it as indeterminate, not as a confirmed black-hole. Analysis queries must filter on `prefixes_in_scope > 0`.

---

## 3. Schema

`db.py` `init_schema()` is updated with the new tables. The existing `ark_reachability` table is replaced (it is currently empty in production). The migration drops the old table and creates the new schema; `init_schema()` handles this with a version-check pattern.

```sql
CREATE TABLE IF NOT EXISTS ark_reachability (
    measurement_date      DATE    NOT NULL,
    asn                   BIGINT  NOT NULL,
    ipv6_reachable        BOOLEAN NOT NULL,
    addresses_observed    INTEGER NOT NULL DEFAULT 0,
    prefixes_in_scope     INTEGER NOT NULL DEFAULT 0,
    pfx2as_snapshot_date  DATE    NOT NULL,
    asn_divergence_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (measurement_date, asn)
);

CREATE TABLE IF NOT EXISTS ark_prefix_observations (
    measurement_date      DATE    NOT NULL,
    prefix                VARCHAR NOT NULL,
    our_asn               BIGINT  NOT NULL,
    addresses_observed    INTEGER NOT NULL,
    addresses_ark_match   INTEGER NOT NULL,
    pfx2as_snapshot_date  DATE    NOT NULL,
    PRIMARY KEY (measurement_date, prefix)
);
```

**Migration:** `init_schema()` detects the old 3-column `ark_reachability` by checking `information_schema.columns`. If it finds the old schema (only 3 columns), it drops and recreates the table before continuing. This migration runs once, automatically, on first use of a DB built under the old schema.

---

## 4. CLI additions

A new `ark` sub-app is added to `cli.py` and registered as `app.add_typer(ark_app, name="ark")`.

```
usg ark ingest                          # most recent available date
usg ark ingest --date 2024-03-15        # specific date
usg ark ingest --dest-dir data/raw      # custom download directory
usg ark backfill 2021-01-01 2024-12-31  # date range
usg ark stats                           # summary for most recent measurement date
usg ark stats --date 2024-03-15         # historical
usg ark stats --json
usg ark gap                             # ASNs with BGP IPv6 but ipv6_reachable=FALSE
usg ark gap --tier cfo_act
usg ark gap --date 2024-03-15
usg ark gap --json
```

**`usg ark stats` output (human-readable):**

```
Ark measurement : 2026-10-10
pfx2as used     : 2026-10-01
Corpus ASNs assessed: 68 of 72  (4 indeterminate — no IPv6 pfx2as prefixes)

Reachability:
  IPv6 reachable   : 41  (60.3%)
  Not reachable    : 27  (39.7%)

By tier:
Tier              Assessed  Reachable  Not Reachable
cfo_act           24        18         6
dod               12        9          3
federal_civilian  32        14         18

Attribution divergence: 2.1% of observed addresses had ark_asn ≠ our_asn
```

**`usg ark gap` output:**

```
ASNs with BGP IPv6 prefixes but no Ark reachability (measurement: 2026-10-10)
(excludes indeterminate: prefixes_in_scope = 0)

ASN     Agency               Tier              IPv6 Prefixes  Addresses Observed
------  -------------------  ----------------  -------------  ------------------
3356    Dept of State        cfo_act           3              0
...
```

---

## 5. GitHub Actions workflow

**File:** `.github/workflows/ark-ingest.yml`
**Name:** `CAIDA Ark IPv6 Reachability Ingest`

```yaml
on:
  schedule:
    - cron: '0 9 * * 3'   # Wednesday 09:00 UTC — weekly cadence, offset from RouteViews (Monday)
  workflow_dispatch:
    inputs:
      date:
        description: Single measurement date (YYYY-MM-DD). Leave blank for latest available.
        required: false
        default: ''
      since:
        description: Backfill start date (YYYY-MM-DD). Used with `until` for range ingests.
        required: false
        default: ''
      until:
        description: Backfill end date (YYYY-MM-DD). Defaults to today when `since` is set.
        required: false
        default: ''

permissions:
  contents: write
```

**Steps:**
1. `actions/checkout@v4`
2. `astral-sh/setup-uv@v5` (Python 3.11, cache on `uv.lock`)
3. `uv sync --frozen`
4. `mkdir -p data/raw`
5. Download DuckDB from `data-latest` release (`|| true` — bootstrap-safe)
6. Python step (env + quoted heredoc pattern): determines `mode` (single / backfill / latest) from inputs, calls `run_ark_ingest` or `run_ark_backfill`
7. Upload updated DuckDB to `data-latest` (`gh release upload --clobber || gh release create`)
8. Trigger `pages.yml` via `gh workflow run pages.yml` (so dashboard rebuilds with new reachability data)

The workflow follows the same env + quoted heredoc (`<< 'PYEOF'`) pattern established in the existing ingest workflows to avoid shell/Python variable scoping bugs.

---

## 6. Report HTML additions

`report_html.py` gains a new section rendered between the existing "Snapshots" inventory and the "Corpus" table.

**New section: "IPv6 Reachability (CAIDA Ark)"**

- Stat cards: `Reachable`, `Not Reachable`, `Indeterminate` counts for the most recent Ark measurement date
- Methodology note: "Attribution: CAIDA RouteViews pfx2as snapshot {pfx2as_snapshot_date}. Addresses with pfx2as_snapshot_date = 0 are excluded from the reachability verdict."
- Gap table: columns `ASN | Agency | Tier | IPv6 Prefixes (BGP) | Addresses Observed | Divergence %`; rows where `ipv6_reachable = FALSE AND prefixes_in_scope > 0`
- Graceful degradation: if `ark_reachability` table has zero rows, render a placeholder card reading "Ark reachability data not yet loaded — run `usg ark ingest` or trigger the Ark Ingest workflow."

---

## 7. Error handling

| Condition | Behaviour |
| --- | --- |
| Ark file not yet published for requested date | Walk back up to 7 days; if still not found, raise `FileNotFoundError` with clear message naming the URL tried |
| `caida_pfx2as` table empty / no IPv6 rows | Abort with `RuntimeError("caida_pfx2as has no IPv6 rows — run RouteViews ingest first")` |
| Malformed node line in nodes-as file | Skip silently; increment a warning counter; print total skipped at end |
| `ark_asn = '*'` (unknown) | Set `ark_asn = None`; address still contributes to `addresses_observed` if our LPM matches |
| Date already loaded in `ark_reachability` | Return `skipped=True` without re-downloading or re-inserting |
| Network timeout on download | `requests` timeout=120s; let the exception propagate to the workflow step, which fails the step with a clear error |

---

## 8. Testing

**`tests/pipeline/test_lpm.py`**
- Exact /128 match returns correct ASN
- Longer prefix wins over shorter prefix covering the same address
- Address outside all prefixes returns None
- /0 default route matches any address (edge case)
- Address at the boundary of a /64 (first and last address)
- IPv6 only; passing an IPv4 prefix to `build_lpm_index` raises `ValueError`

**`tests/pipeline/test_ark.py`**
- `parse_nodes_as`: parses valid `N addr asn` line correctly
- `parse_nodes_as`: skips comment lines (`#`)
- `parse_nodes_as`: skips non-`N` record types
- `parse_nodes_as`: `*` ark_asn yields `None`
- `parse_nodes_as`: malformed line (missing asn field) yields `None` for ark_asn
- `parse_nodes_as`: IPv4-mapped address (`::ffff:1.2.3.4`) is skipped entirely
- `download_nodes_as`: raises `FileNotFoundError` when all 7 lookback days return HTTP 404 (mock `requests.get` to always 404)
- `run_ark_ingest` integration: fixture nodes-as (gzipped in-memory) + fixture pfx2as in in-memory DuckDB → correct `ark_reachability` rows (TRUE, FALSE, and indeterminate cases all present)
- `run_ark_ingest`: idempotent — running twice on same date does not duplicate rows
- `run_ark_backfill`: skips already-loaded dates, processes new ones

**`tests/test_ark_gap.py`**
- The SQL underlying `usg ark gap` returns only ASNs with `ipv6_reachable=FALSE AND prefixes_in_scope > 0`
- ASNs with `prefixes_in_scope=0` are excluded even if `ipv6_reachable=FALSE`
- ASNs with `ipv6_reachable=TRUE` are excluded

---

## 9. Constraints and open questions

**Constraints:**
- No new Python package dependencies. LPM via stdlib `ipaddress` only.
- All public functions callable both from CLI and from GitHub Actions Python steps without modification.
- `run_ark_ingest` and `run_ark_backfill` return plain dicts (JSON-serialisable) so workflow steps can print results with `json.dumps`.
- Backfills must be resumable across GitHub Actions job-limit boundaries (6 hours). Already-loaded dates are skipped automatically.

**Open questions (deferred to Phase 3):**
- **"Not probed" vs "not reachable":** The nodes-as file only contains addresses that *responded* to probes. An ASN with `addresses_observed=0` may be unprobed rather than unreachable. CAIDA's separate Ark hitlists dataset would enable this distinction but is out of scope for Phase 2.
- **Historical pfx2as alignment:** For backfills, ideally the pfx2as snapshot date should match the Ark measurement date (use January 2021 pfx2as for January 2021 Ark data). Phase 2 uses the pfx2as snapshot closest to the Ark date that is already in the DB. A future enhancement could automate historical pfx2as backfill in lockstep with Ark backfill.
- **Vantage-point coverage metadata:** Recording which Ark vantage points probed which prefixes would enable coverage-adjusted reachability scores. Not in scope for Phase 2.
