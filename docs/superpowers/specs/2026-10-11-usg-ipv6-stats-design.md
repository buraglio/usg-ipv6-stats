# USG IPv6 Statistics — Design Spec

**Date:** 2026-10-11  
**Status:** Approved for implementation planning  
**Scope:** Research paper + analyst toolkit, with a path to hosted web service

---

## 1. Goals & Success Criteria

Produce a reproducible, citable dataset and analysis toolkit measuring IPv4 vs IPv6 prefix deployment for US Government ASNs over time, with emphasis on the period since the 2020 OMB M-21-07 IPv6 mandate.

**Success criteria:**
- A curated, versioned USG ASN corpus publishable as a standalone research artifact
- Reproducible longitudinal analysis from RouteViews data back to mandate date (2020) and all-time
- An analyst toolkit (CLI + notebooks) usable by researchers without running the full pipeline
- A static web query interface requiring no server infrastructure
- A clear upgrade path to a REST API when needed

**Out of scope (initial):** traffic/utilization metrics (netflow, telescope) are placeholders; secondary metric integration is deferred until methodology is confirmed.

---

## 2. Overall Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Data Ingest Layer                    │
│  RouteViews (primary)  │  RIPE RIS  │  CAIDA  │  IRR   │
└───────────────┬─────────────────────────────────────────┘
                │ MRT dumps / APIs
┌───────────────▼─────────────────────────────────────────┐
│                  Normalize & Parse                       │
│   MRT parser → prefix records → USG ASN filter          │
└───────────────┬─────────────────────────────────────────┘
                │ structured records
┌───────────────▼─────────────────────────────────────────┐
│             DuckDB Analytical Store                      │
│  prefixes │ asn_corpus │ snapshots │ secondary_metrics   │
└───────────┬─────────────────────────────────────────────┘
            │
     ┌──────┴──────┐──────────────┐
     ▼             ▼              ▼
  CLI/Python    Notebooks     FastAPI (future)
  (analyst      (paper         │
   toolkit)      figures)      ├── REST endpoints
                               └── Web query UI
```

**Key principle:** DuckDB is the single source of truth. All consumers — notebooks, CLI, and future API — query DuckDB directly. No data logic leaks into the presentation layer.

---

## 3. Data Pipeline

### 3.1 Ingest

**Primary source:** RouteViews MRT table dumps (RIBs), available at `archive.routeviews.org`. Snapshots published every 2 hours; historical data available from the early 2000s.

**Corroborating sources:** RIPE RIS, CAIDA AS-Organization dataset, ARIN IRR. Used for corpus validation and secondary analysis, not as primary prefix data.

**Raw file handling:** MRT dumps are fetched ephemerally during pipeline runs and discarded after parsing. Only processed Parquet files and the DuckDB store are retained.

### 3.2 Parse

**Parser:** `bgpkit-parser` (Rust-based, Python bindings). Fastest available MRT parser; handles RouteViews format cleanly.

Each RIB snapshot produces a Parquet file with schema:

```
snapshot_ts   TIMESTAMP
collector     VARCHAR
peer_asn      INTEGER
prefix        VARCHAR
origin_asn    INTEGER
prefix_len    INTEGER
af            INTEGER   -- 4 or 6
```

### 3.3 Normalize & Filter

After parsing, records are filtered to ASNs present in the USG corpus. Per-snapshot aggregates computed:

- IPv4 prefix count per ASN
- IPv6 prefix count per ASN
- Dual-stack flag (announces both AF)

### 3.4 Storage Layout

```
data/
  raw/          ← ephemeral MRT downloads (not committed)
  processed/    ← per-snapshot Parquet files
  usg_ipv6.duckdb
```

**DuckDB tables:**

| Table | Description |
|---|---|
| `snapshots` | One row per RIB collection event, processing status |
| `prefix_records` | Filtered parsed records for USG ASNs only |
| `asn_corpus` | Curated USG ASN list with agency metadata and tier |
| `secondary_metrics` | Placeholder; schema TBD pending netflow/telescope methodology |

### 3.5 Backfill & Incremental Runs

A Makefile drives pipeline execution:

```
make backfill SINCE=2020-01   # historical ingest
make run                       # incremental (skips processed snapshots)
make setup                     # install deps, initialize DuckDB
```

Processed-snapshot manifest stored in DuckDB; incremental runs skip already-parsed snapshots.

---

## 4. USG ASN Corpus

The corpus is a publishable research artifact and is cited separately from the analysis. Every pipeline run records the corpus version used for reproducibility.

### 4.1 Construction Methodology

**Step 1 — Seed:** Query ARIN WHOIS REST API for organizations matching known federal identifiers (org handles containing `USGOV`, `DOD`, `NASA`, `HHS`, `DHS`, etc.) and name-based matching against the 24 CFO Act agencies and major DoD components.

**Step 2 — Expand:** Cross-reference against CAIDA AS-Organization dataset and BGP.tools AS descriptions using fuzzy name matching. ASNs found in multiple sources are marked high-confidence.

**Step 3 — Validate:** Cross-check against public agency network disclosures (DISA documentation, GSA network inventory where available). Flag discrepancies for manual review.

### 4.2 Scope Tiers

| Tier | Description |
|---|---|
| `cfo_act` | 24 CFO Act agencies — primary OMB M-21-07 scope |
| `federal_civilian` | Broader civilian federal including smaller agencies |
| `dod` | DoD / DISA / military service branches |
| `all_usg` | Union of all tiers |

Analysis can be reported at any tier level. The paper reports primarily at `cfo_act` with `all_usg` as supplementary.

### 4.3 Corpus Schema

```
asn             INTEGER
org_name        VARCHAR
agency          VARCHAR
tier            VARCHAR     -- cfo_act | federal_civilian | dod | all_usg
arin_org_handle VARCHAR
confidence      VARCHAR     -- high | medium | low
sources         VARCHAR[]   -- arin | caida | bgptools | manual
last_verified   DATE
corpus_version  VARCHAR
```

The corpus lives as `data/usg_asn_corpus.csv` in the repo, with dated version tags on each release.

### 4.4 Automated Maintenance (GitHub Actions)

A weekly workflow re-queries ARIN WHOIS and cross-checks CAIDA AS-org data, then opens a pull request with proposed additions or removals for human review before merge.

---

## 5. Analysis & Query Layer

All query logic lives in a Python package (`usg_ipv6/`) shared by the CLI, notebooks, and future API. No analysis logic is duplicated across consumers.

### 5.1 Package Structure

```
usg_ipv6/
  db.py              ← DuckDB connection management
  queries/
    deployment.py    ← prefix counts, dual-stack ratios, trends
    corpus.py        ← ASN lookup, tier filtering
    snapshots.py     ← time-series slicing, snapshot inventory
    secondary.py     ← placeholder for netflow/telescope metrics
  models.py          ← dataclasses for typed query results
```

### 5.2 Core Analysis Functions

| Function | Description |
|---|---|
| `ipv6_adoption_over_time(tier, since)` | Time-series of IPv6 prefix count as % of total, by corpus tier |
| `per_agency_status(snapshot_ts)` | Point-in-time dual-stack status per agency |
| `mandate_progress(since=2020)` | Change in IPv6 deployment since OMB M-21-07 |
| `compare_to_global(snapshot_ts)` | USG ratios vs global BGP table as baseline |
| `top_movers(window)` | Agencies with largest IPv6 growth in a given time window |

### 5.3 CLI Toolkit

Built with Typer. Installed as `usg-ipv6` command.

```bash
usg-ipv6 status --tier cfo_act --date 2024-01-01
usg-ipv6 trend --agency DHS --since 2020 --format csv
usg-ipv6 compare --snapshot latest --output table
usg-ipv6 corpus list --tier dod
```

Output formats: `table` (terminal), `csv`, `json`, `parquet`.

---

## 6. Outputs

### 6.1 Analyst Toolkit

```
toolkit/
  notebooks/
    01_corpus_exploration.ipynb
    02_longitudinal_trends.ipynb
    03_agency_comparison.ipynb
    04_global_baseline.ipynb
    05_secondary_metrics.ipynb      ← placeholder
  data/
    usg_asn_corpus.csv
    snapshots.duckdb                ← fetched from GitHub Releases
```

Notebooks are self-contained: `git clone` → `make setup` → all figures reproduce from the published DuckDB artifact.

### 6.2 Research Paper Figures

`paper/figures/` contains standalone scripts (not notebooks) that generate publication-quality plots from the query layer. Separated from exploratory notebooks to keep the paper pipeline stable across revisions.

### 6.3 Web Service — Phase 1 (GitHub Pages + DuckDB-WASM)

- Static site hosted on GitHub Pages
- DuckDB-WASM runs entirely in the browser
- Fetches Parquet files from GitHub Releases on demand
- Users select agency, tier, date range; results rendered as table and chart
- Zero hosting cost; ships with the repo

### 6.4 Web Service — Phase 2 (REST API)

When server-side execution is needed:

- FastAPI app wrapping `usg_ipv6/queries/`
- Endpoints mirror the CLI: `GET /v1/trend`, `GET /v1/status`, `GET /v1/corpus`
- Deploy to Cloudflare Workers + R2, or a small VPS
- Phase 1 web UI switches from WASM to API calls with minimal frontend changes

---

## 7. Automation (GitHub Actions)

| Workflow | Trigger | Action |
|---|---|---|
| `corpus-update.yml` | Weekly cron | Re-query ARIN + CAIDA; open PR with proposed corpus changes |
| `pipeline-run.yml` | Weekly cron + manual dispatch | Fetch latest RouteViews snapshots, parse, update DuckDB |
| `publish.yml` | On merge to main | Upload DuckDB to GitHub Releases; rebuild GitHub Pages |
| `backfill.yml` | Manual dispatch (`SINCE=` input) | Historical ingest |
| `report.yml` | Manual dispatch | Generate summary stats; update README badge |

---

## 8. Technology Choices

| Component | Technology | Rationale |
|---|---|---|
| MRT parsing | `bgpkit-parser` (Python bindings) | Fastest available; RouteViews-compatible |
| Analytical store | DuckDB | Single-file, no server, SQL, Parquet-native; MotherDuck upgrade path |
| CLI | Typer | Lightweight, typed, auto-docs |
| Notebooks | Jupyter | Standard for research reproducibility |
| Web UI | DuckDB-WASM + GitHub Pages | No server required for Phase 1 |
| Future API | FastAPI | Same query functions, minimal wrapping |
| Automation | GitHub Actions | Zero additional infrastructure |
| Data artifacts | GitHub Releases | 2GB limit; sufficient for USG-filtered DuckDB |

---

## 9. Open Questions / Deferred Decisions

- **Secondary metrics:** Netflow and telescope-based utilization metrics (potentially CAIDA telescope) are placeholders. Methodology TBD after primary pipeline is stable.
- **Global baseline data:** `compare_to_global()` requires a non-filtered RouteViews parse. Storage implications to be assessed.
- **Phase 2 hosting:** Cloudflare Workers vs VPS decision deferred until Phase 1 traffic patterns are understood.
- **Corpus update frequency:** Weekly automation proposed; cadence may be adjusted based on how often USG ASN assignments actually change.
