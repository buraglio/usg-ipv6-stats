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
- [Corpus: source matching methodology and known limitations](#corpus-source-matching-methodology-and-known-limitations)
- [Pipeline: incremental run](#pipeline-incremental-run)
- [Pipeline: backfill](#pipeline-backfill)
- [Ark reachability](#ark-reachability)
- [IPv6 Hitlist: live endpoint detection](#ipv6-hitlist-live-endpoint-detection)
- [Active-prefix enrichment](#active-prefix-enrichment)
- [DNS zone walk enrichment](#dns-zone-walk-enrichment)
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

# 4. Ingest the TU Munich IPv6 Hitlist responsive-address file
make hitlist

# 5. Query with the CLI
uv run usg stats
uv run usg corpus --tier cfo_act
uv run usg prefixes --af 6
uv run usg ark stats
uv run usg ark gap
uv run usg hitlist stats
```

For historical BGP analysis, replace step 2 with `make backfill SINCE=2021-01-01 UNTIL=2024-12-31`.
For historical Ark analysis, replace step 3 with `make ark-backfill SINCE=2021-01-01 UNTIL=2024-12-31`.
For historical Hitlist analysis, replace step 4 with `make hitlist-backfill SINCE=2024-01-01 UNTIL=2024-12-31`.

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

### `usg hitlist ingest`

Download and match the current TU Munich IPv6 Hitlist responsive-address file against corpus prefixes. Requires `caida_pfx2as` to contain IPv6 (af=6) rows (the `route-views6` collector must have been run first, or the hitlist GitHub Actions workflow handles this automatically).

```bash
uv run usg hitlist ingest                      # today as observation date
uv run usg hitlist ingest --date 2024-10-01   # specific observation date
uv run usg hitlist ingest --dest-dir data/raw  # custom download directory
```

### `usg hitlist backfill`

Stamp a single downloaded hitlist file across multiple observation dates. The file is downloaded once; the same responsive-address data is recorded under each date in the range.

```bash
uv run usg hitlist backfill 2024-01-01 2024-12-31
uv run usg hitlist backfill 2024-01-01 2024-12-31 --dest-dir data/raw
```

### `usg hitlist stats`

Responsive-address counts per agency for a given observation date, ordered by count descending.

```bash
uv run usg hitlist stats                          # most recent date
uv run usg hitlist stats --date 2024-10-01       # specific date
uv run usg hitlist stats --asn 11974             # filter to one ASN
uv run usg hitlist stats --min-count 10          # hide ASNs below threshold
uv run usg hitlist stats --json
```

Example output:

```text
Observation date : 2024-10-04
Hitlist date     : 2024-10-04
Corpus ASNs      : 72

Agency                    ASN     Responsive  Aliased
------------------------  ------  ----------  -------
Dept. of State            11974   142         0
NASA                      297     87          12
GSA                       5117    43          0
...
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
| `make hitlist` | Download and ingest the current TU Munich IPv6 Hitlist file |
| `make hitlist-backfill` | Stamp the hitlist across a date range |
| `make test` | Run the full test suite |
| `make lint` | Syntax-check all production modules |

All targets accept variable overrides on the command line (see [Configuration variables](#configuration-variables)).

---

## GitHub Actions

Eight workflows automate the full pipeline in CI. All require a GitHub token with `contents: write` (provided automatically by `GITHUB_TOKEN`).

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

### IPv6 Hitlist Ingest (`hitlist-ingest.yml`)

Downloads the TU Munich IPv6 Hitlist responsive-address file, matches each address against corpus ASN prefixes via LPM, and writes per-ASN responsive counts to `hitlist_observations` and matched addresses to `hitlist_addresses`.

- **Scheduled**: Fridays 07:00 UTC (after RouteViews Monday and Ark Wednesday, so pfx2as IPv6 data is current)
- **Manual dispatch**: accepts `date` (single observation date), `since`/`until` (backfill range)

**Requires:** `caida_pfx2as` must contain IPv6 (af=6) rows. The workflow automatically downloads CAIDA pfx2as rv6 data before the hitlist step if none exist for the current month.

**What it does:**

1. Downloads the existing DuckDB from the `data-latest` release (`|| true` for first-run safety).
2. Loads CAIDA pfx2as rv6 IPv6 data into the DB, skipping if current-month data already exists.
3. Downloads `responsive-addresses.txt.xz` and `aliased-prefixes.txt.xz` from `alcatraz.net.in.tum.de`.
4. Builds an LPM index from corpus IPv6 prefixes, then streams all responsive addresses, matching each against the index.
5. Writes per-ASN counts to `hitlist_observations` (one row per corpus ASN per observation date) and matched addresses to `hitlist_addresses`.
6. Addresses in aliased prefixes are flagged `is_aliased=TRUE` rather than excluded — the dashboard can show both total responsive and likely-real-host counts.
7. Re-uploads the updated DuckDB and triggers the GitHub Pages rebuild.

**Aliased prefixes:** When a prefix is "aliased," any address within it responds to probes (a router answers for the whole block). These are counted separately as `aliased_count` so over-counting from inflated host counts is visible.

### Active-prefix Enrichment Workflows

Five enrichment workflows run independently of the BGP ingests and write to separate tables. Each downloads the existing DuckDB from `data-latest`, runs its enrichment pass, and re-uploads.

#### .gov DNS Enumeration (`enrichment-dotgov-dns.yml`)

- **Scheduled**: daily 06:42 UTC
- **Manual dispatch**: accepts `observation_date` (YYYY-MM-DD, default today)
- Fetches the full [CISA dotgov-data](https://github.com/cisagov/dotgov-data) CSV of all registered federal `.gov` domains. For each domain, resolves NS, MX, and apex+www AAAA records concurrently (20 workers). Addresses that fall inside corpus IPv6 prefixes are written to `domain_services` with `source='dotgov_dns'`.
- **Why:** Name servers, mail servers, and web servers communicate over TCP (ports 53, 25, 443) and are visible through firewalls that silently drop ICMP — the primary blind spot of the TU Munich hitlist scanner.

#### NIST USGv6 Deployment Monitor (`enrichment-nist.yml`)

- **Scheduled**: daily 20:17 UTC (after NIST publishes their ~15:00 ET update)
- **Manual dispatch**: no inputs required
- Scrapes [usgv6-deploymon.nist.gov](https://usgv6-deploymon.nist.gov/cgi-bin/generate-gov) for per-domain IPv6 service metrics: server count, v6-addressed, v6-reachable, v6-functional for DNS / mail / web, plus DNSSEC status (Signed / Valid / Chained). Measured hostnames are resolved to AAAA and matched against corpus prefixes. Results go to `nist_deploymon` and `domain_services` (`source='nist'`).
- **Why:** NIST measures active TCP reachability from a fixed government vantage point; DNSSEC chaining is a strong signal that a domain is actively maintained.

#### Certificate Transparency (`enrichment-cert-transparency.yml`)

- **Scheduled**: weekly Mondays 03:23 UTC
- **Manual dispatch**: accepts `recent_days` (default 365)
- Queries [crt.sh](https://crt.sh) for all TLS certificates issued to `*.gov` subdomains within the lookback window. Subject Alternative Names (SANs) are resolved to AAAA and matched against corpus prefixes. Results land in `domain_services` (`source='ct'`).
- **Why:** Certificate issuance is a reliable last-active lower bound for a hostname. CT logs also surface subdomains that do not appear in public DNS zone data.

#### RPKI Validity (`enrichment-rpki.yml`)

- **Scheduled**: weekly Tuesdays 04:11 UTC
- **Manual dispatch**: accepts `af` (4, 6, or 0 for both; default 6)
- Checks every corpus prefix / origin-AS pair against the [Cloudflare RPKI validity API](https://rpki.cloudflare.com/api/v1/validity/), falling back to the RIPE Stat API. Results — `valid`, `invalid`, or `not_found` — are stored in `rpki_validity` along with the ROA max-length.
- **Why:** A `valid` ROA means the operator explicitly authorized this origin AS for this prefix, a strong signal of intentional, maintained operation. `invalid` may indicate route leaks or stale announcements worth investigation.

#### DNS Zone Walk (`enrichment-zone-walk.yml`)

- **Scheduled**: weekly Wednesdays 14:00 UTC
- **Manual dispatch**: accepts `domain_limit` (integer; blank = all ~1,200 domains) and `observation_date` (YYYY-MM-DD; blank = today)
- **Timeout**: 120 minutes
- Runs a multi-phase DNS enumeration pipeline against every registered federal `.gov` domain. Results land in `zone_walk_meta` (per-domain summary) and `zone_walk_hosts` (per-resolved-address detail). Zone names and individual FQDNs are stored in the database but not surfaced on the public dashboard — only aggregate counts are displayed.
- **Why:** OMB M-08-23 mandates DNSSEC signing for all `.gov` zones. Zones that use NSEC records (rather than NSEC3 hashing) are inherently enumerable — every hostname in the zone is discoverable through iterative NSEC chain walking. This surfaces management interfaces, VPN concentrators, and internal APIs that are invisible to ICMP hitlists and prefix-offset Atlas sweeps. See [DNS zone walk enrichment](#dns-zone-walk-enrichment) for full methodology.

**Pipeline steps:**

1. Downloads existing DuckDB; ensures CAIDA pfx2as IPv6 data is present (downloads rv6 if the current month's data is absent).
2. Fetches the full CISA dotgov-data domain list (~1,200 `.gov` domains).
3. For each domain, runs the four-phase walk (see [DNS zone walk enrichment](#dns-zone-walk-enrichment)).
4. Resolves all discovered FQDNs concurrently for AAAA records (30 workers).
5. Matches resolved addresses against corpus IPv6 prefixes via LPM.
6. Writes per-domain rows to `zone_walk_meta` and per-address rows to `zone_walk_hosts`.
7. Re-uploads the updated DuckDB and triggers the GitHub Pages rebuild.

### RIPE Atlas Prefix Sweep (`atlas-sweep.yml`)

Discovers live IPv6 addresses within corpus prefixes that evade ICMP-based probing (hitlist, Ark) due to federal firewall policy, using multi-type Atlas measurements from US-based probes.

- **Scheduled**: Saturdays 10:00 UTC (after the Friday hitlist run)
- **Manual dispatch**: accepts `probe_count` (default 20), `country_code` (default `US`), `measurement_labels` (comma-separated subset of `ping,tcp_443,tcp_80,tcp_53,traceroute`; default all five), `dry_run` (estimate credits without creating measurements), and `observation_date`
- **Requires:** `RIPE_ATLAS_API_KEY` GitHub Actions secret with *Create User Defined Measurements* scope; `caida_pfx2as` must contain IPv6 rows

**What it does:**

1. Downloads the existing DuckDB from the `data-latest` release.
2. Verifies CAIDA pfx2as IPv6 data is present for the current month (downloads rv6 if not).
3. Prints a credit estimate before creating any measurements.
4. For each corpus IPv6 prefix (530 distinct prefixes as of October 2026), generates up to 10 candidate target addresses using low-numbered host-part offsets (::1, ::2, ::a, ::53, ::64, ::7b, etc.) plus any seeds from `domain_services` or `hitlist_addresses` that fall within the prefix.
5. Issues each measurement type (ping / TCP 443 / TCP 80 / TCP 53 / traceroute) per target from 20 US-based Atlas probes.
6. Parses results: responsive targets are stored in `atlas_observations`; intermediate router addresses discovered via traceroute within corpus prefixes are stored in `atlas_hop_addresses`.
7. Re-uploads the updated DuckDB and triggers the GitHub Pages rebuild (skipped on dry run).

**Measurement credit estimate** (default settings): ~4.35M credits per run (530 prefixes × 10 targets × 20 probes × 41 credits/target across all five types). Use `dry_run=true` to verify before committing.

### Deploy GitHub Pages (`pages.yml`)

Generates the static HTML dashboard and deploys to GitHub Pages.

- Triggers: after each RouteViews MRT Ingest, Ark Reachability Ingest, Corpus Update, or Hitlist Ingest run; on push to `main`; and on manual dispatch
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

1. Pre-flight check: attempts to reach ARIN WHOIS (`whois.arin.net`). GitHub Actions IP ranges (Azure) are blocked by ARIN, so this check always fails in CI and ARIN queries are skipped entirely.
2. If ARIN is reachable (local run only): queries the ARIN WHOIS REST API `/org/{handle}/asns.json` for each seed handle. **This returns very few results in practice** — see [Corpus: source matching methodology and known limitations](#corpus-source-matching-methodology-and-known-limitations).
3. Downloads the most recent CAIDA AS2Org file (published on the 1st of each month; walks back up to 6 months to find the latest available).
4. **Primary match — exact ARIN handle:** For each ASN in the AS2Org file, checks whether its `org_id` matches a seed handle. CAIDA formats `org_id` as `{ARIN-HANDLE}-ARIN` or `{ARIN-HANDLE}-Z-ARIN` (e.g., our handle `ORNL` → CAIDA `org_id` `ORNL-Z-ARIN`). Matches produce `confidence: high` records with `arin_org_handle` populated.
5. **Fallback — fuzzy name match:** For ASNs that did not match by handle, fuzzy-matches the CAIDA org name string against the agency seed list (threshold: 85/100 token-sort ratio). Matches produce `confidence: medium` records.
6. Deduplicates by ASN, keeping the highest-priority tier when an ASN appears under multiple agencies (see tier table below).
7. Writes `data/usg_asn_corpus.csv` and loads the `asn_corpus` table in the DuckDB file.

**Tier hierarchy (higher rows take precedence on conflict):**

| Tier | Description |
| ------ | ----------- |
| `cfo_act` | 24 CFO Act civilian cabinet agencies |
| `dod` | Department of Defense and components (DARPA, DISA, service branches, NGA, SOCOM) |
| `doe_lab` | DOE national laboratory complex and ESnet |
| `federal_civilian` | Independent federal agencies and commissions |
| `legislative` | Legislative branch entities |
| `judicial` | Judicial branch entities |

**Confidence levels:**

| Level | Source |
| ------- | -------- |
| `high` | CAIDA AS2Org with exact ARIN org handle match, or direct ARIN WHOIS query (local runs only) |
| `medium` | CAIDA AS2Org fuzzy org-name match (threshold 85/100) |

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

## Corpus: source matching methodology and known limitations

This section describes why ASN discovery is harder than it looks and documents known issues that affect corpus completeness and accuracy. Researchers building on this dataset should read it.

### Why ARIN's `/asns` endpoint is insufficient

The ARIN WHOIS REST API provides `/org/{handle}/asns.json`, which returns ASNs *directly registered to that org handle*. In practice, this endpoint returns almost nothing for US government agencies:

- **Agencies register address space, not ASN numbers, under their primary org handle.** ORNL, Fermilab, LBNL, and most other labs register IPv4/IPv6 address blocks (nets) under their handle, but their AS numbers are registered under separate, often obscure child or customer handles, or under the network operator (ESnet, Internet2, DISA) rather than the lab itself.
- **ARIN blocks GitHub Actions IP ranges.** Azure IP space (used by GitHub Actions) is blocked by ARIN. The `arin_reachable()` pre-flight check detects this and short-circuits all ARIN queries immediately rather than burning through retry backoff for every seed handle. All CI runs are therefore CAIDA-only.
- **Even running locally, `/asns` returns empty.** Testing locally against representative handles: `ORNL` → 404 (no ASNs), `FERMIL` → 404, but both return 200 on `/nets` (they have registered address space). The ASNs that originate their prefixes are registered elsewhere. The ARIN WHOIS REST `/asns` path is structurally the wrong query for how DoD and DOE networks are organized.

### How CAIDA AS2Org matching works

The CAIDA AS2Org file (published monthly) maps each ASN to an organization. It has two record types:

- **AUT lines** (6 fields): `asn | changed | aut_name | org_id | opaque_id | source`
- **ORG lines** (5 or 6 fields): `org_id | changed | org_name | country | source`

The `org_id` in AUT lines is a key into the ORG table. Crucially, **CAIDA's `org_id` is the ARIN org handle with a suffix appended**: either `-ARIN` or `-Z-ARIN`. For example:

| ARIN handle | CAIDA org_id |
| ----------- | ------------ |
| `ORNL` | `ORNL-Z-ARIN` |
| `BNL` | `BNL-ARIN` *and* `BNL-Z-ARIN` (two separate entries) |
| `FERMIL` | `FERMIL-Z-ARIN` |
| `DARPA-2` | `DARPA-2-ARIN` |
| `SNL-3` | `SNL-3-Z-ARIN` |
| `THELE-44` (SLAC) | `THELE-44-Z-ARIN` |
| `NASA` | `NASA-ARIN` *and* `NASA-Z-ARIN` |

The corpus builder constructs lookup keys for both suffix forms and checks each CAIDA ASN's `org_id` against them. Matches are treated as `confidence: high` because they are exact handle matches rather than approximate name matches.

The `-Z-` form appears to denote customer allocations or sub-registrations in ARIN's internal data model; the practical effect is that an org may appear under both the plain `-ARIN` and `-Z-ARIN` forms, and the corpus correctly collects ASNs from both.

### Fuzzy name matching: fallback and known mismatches

For ASNs whose CAIDA `org_id` does not match any seed handle, the builder falls back to fuzzy string matching: each CAIDA `org_name` string is compared to the seed agency names using `rapidfuzz.fuzz.token_sort_ratio` with a threshold of 85/100. This catches agencies whose ASNs are registered under different org handles but where the org name is recognizable.

Known cases where the CAIDA-registered name differs substantially from the canonical agency name:

| Agency | Seed name | CAIDA org_name | Match path |
| ------- | --------- | -------------- | ---------- |
| National Renewable Energy Laboratory | `National Renewable Energy Laboratory` | `National Laboratory of the Rockies` | Handle match (`NREL-ARIN`) |
| Pacific Northwest National Laboratory | `Pacific Northwest National Laboratory` | `BATTELLE MEMORIAL INSTITUTE` | Handle match (`BMI-1-ARIN`) |
| Corporation for National and Community Service | `Corporation for National and Community Service` | `AmeriCorps` | Handle match (`CNCS-ARIN`) |
| Energy Sciences Network | `Energy Sciences Network (ESnet)` | `ESnet` | Handle match (`ENSN-Z-ARIN`) |

For PNNL and NREL, fuzzy name matching would fail entirely — the CAIDA-registered names share almost no tokens with the official agency names. These are only found because the ARIN handle is in the seed list and CAIDA's `org_id` encodes it.

The fuzzy fallback captures agencies like USAF components, Army installations, and Navy activities where the CAIDA org name is a recognizable variant (e.g., "Air Force Systems Networking" for `7ESG-ARIN`, which matches the "Department of the Air Force" seed via handle).

### NOC handles vs. primary org handles

Several seed handles were originally chosen as NOC or ISP-facing handles (e.g., `NASA-ARC-NOC`, `NSF-NOC`, `DHS-CIO`) rather than the organization's primary ARIN registration. These NOC handles do not appear in CAIDA's AS2Org at all — CAIDA uses the organization-level handle, not sub-handles for specific network operations centers. The seed list includes supplemental handles that do match CAIDA:

| Agency | Primary seed handle | CAIDA-matched handle(s) |
| ------- | ------------------- | ----------------------- |
| NASA | `NASA-ARC-NOC` | `NASA`, `NDSND` (Deep Space Network), `NGSFC` (Goddard) |
| NSF | `NSF-NOC` | `NSF`, `NSF-15` |
| DHS | `DHS-CIO` | `DHS-37`, `DHS-39` |
| DOJ | `USDOJ` | `DEPAR-9`, `DEPART-31`, `FCJIS` (FBI CJIS) |
| DoD (main) | `USDDD`, `DNIC` | `USDDD-ARIN` (1,000+ ASNs in the DoD block pool) |

The original NOC handles are kept in the seed list as documentation of the ARIN registration structure and to benefit from any ARIN-direct query that succeeds locally. Only the supplemental handles produce CAIDA matches.

### Known handle collisions

Some bare handle strings are registered to unrelated organizations in ARIN. These were avoided or corrected during seed list construction:

| Handle | Registered to (in ARIN) | Intended agency | Resolution |
| ------- | ----------------------- | --------------- | ---------- |
| `ESNET` | Commercial ISP (ESnet, Inc.) | DOE Energy Sciences Network | Use `ENSN` instead |
| `ANL` | Different organization | Argonne National Laboratory | Use `ANLB` and `ANL-13` |
| `DOE` | Delaware Office Equipment | Department of Energy | Use `DOE-ISC` |
| `NGA` | National Gallery of Art | National Geospatial-Intelligence Agency | Use `NGA-30`, `NGA-24` |
| `FEC` | FedEx Corporation | Federal Election Commission | Use `FEC-105` |
| `NLRB` | NYS Law Reporting Bureau | National Labor Relations Board | Use `NLRB-4` |

Researchers extending the seed list should verify every new handle against the ARIN RDAP endpoint (`https://rdap.arin.net/registry/entity/{HANDLE}`) before assuming it belongs to the intended agency. Short handles (3–4 characters) have a high collision rate.

### Non-obvious handle-to-agency mappings

Several DOE lab handles are opaque and were confirmed via ARIN RDAP:

| Handle | Registered org name | Lab |
| ------- | ------------------- | --- |
| `THELE-44` | SLAC National Accelerator Laboratory | SLAC |
| `CEBAFS` | Thomas Jefferson National Accelerator Facility | Jefferson Lab |
| `IRDSD-1` | DOE Office of Fossil Energy (NETL) | National Energy Technology Lab |
| `SRP-1` | Savannah River Site | Savannah River National Lab |
| `BMI-1` | BATTELLE MEMORIAL INSTITUTE | Pacific Northwest National Lab (operated by Battelle) |
| `ENSN` | ESnet | Energy Sciences Network (DOE ESnet) |
| `ANL-13` | Argonne National Laboratory | Argonne (supplemental to `ANLB`) |

`BMI-1` (Battelle) warrants a note: Battelle Memorial Institute is the management and operations contractor for PNNL and historically for other labs. The ARIN registration is under Battelle's corporate name, not the lab. AS32513, the only ASN under `BMI-1-ARIN` in CAIDA, is confirmed as PNNL's AS.

### Deprecated handles

Two handles previously in the seed list were removed after ARIN RDAP returned 404:

| Handle | Replacement |
| ------- | ----------- |
| `DOD-NIC` | `DNIC` |
| `DISA-HOSTMAST` | `DISA-24` |

### Corpus completeness caveats

The corpus is a best-effort enumeration, not an authoritative registry. Known gaps:

- **DoD sub-commands and tactical networks:** DISA, Army, Navy, Air Force, and Marine Corps each have routing infrastructure spread across dozens of registered handles not in the seed list. The DoD block pool (`USDDD-ARIN`) captures the bulk (1,000+ ASNs), but tactical and joint-command ASNs may be missing.
- **Contractor-operated networks:** Some federal IT is operated by contractors under the contractor's ASN. Those ASNs appear in BGP tables under the contractor org, not the agency.
- **Classified or non-public registrations:** Some USG infrastructure intentionally obscures ownership. These are invisible to ARIN WHOIS and CAIDA alike.
- **Intelligence community:** CIA, NSA, DIA, and similar agencies are not in the seed list. Their publicly routed ASNs (if any) are not tracked.

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

## IPv6 Hitlist: live endpoint detection

The [TU Munich IPv6 Hitlist Service](https://ipv6hitlist.github.io) publishes weekly lists of IPv6 addresses that responded to active probes. This is a fundamentally stronger signal than BGP reachability: a responsive address is a live host that answered, not just a route that exists.

**BGP vs. Ark vs. Hitlist — three distinct questions:**

| Source | Question answered |
| ------- | ------------------ |
| RouteViews BGP | Does this agency *advertise* IPv6 prefixes? |
| CAIDA Ark | Are IPv6 addresses in those prefixes *reachable* from CAIDA vantage points? |
| TU Munich Hitlist | Are specific IPv6 addresses *actively in use* (responded to probes)? |

**Prerequisite:** `caida_pfx2as` must contain IPv6 (af=6) rows. Running `make run --collector route-views6` (or the RouteViews ingest GitHub Action with collector `route-views6`) loads IPv6 pfx2as data. The hitlist GitHub Actions workflow handles this automatically before each run.

```bash
# Ingest the current hitlist file
make hitlist

# Backfill a date range (downloads once, stamps each date)
make hitlist-backfill SINCE=2024-01-01 UNTIL=2024-12-31

# Responsive-address summary by agency
uv run usg hitlist stats

# Show counts for a specific date
uv run usg hitlist stats --date 2024-10-04
```

**Data cadence:** The hitlist is updated weekly. The static download URL is updated in-place; the file date is read from the HTTP `Last-Modified` header and stored as `hitlist_date`. The `observation_date` is the date the ingest ran (or the date you supply for backfills).

**Aliased prefixes:** Some prefixes are "aliased" — a router or network device responds to probes for every address in the block, artificially inflating apparent host counts. The hitlist publishes a companion `aliased-prefixes.txt.xz` file. Addresses matched to aliased prefixes are flagged `is_aliased=TRUE` in `hitlist_addresses` and their count is tracked separately as `aliased_count` in `hitlist_observations`. They are not excluded — you can choose to filter them in queries.

---

## Active-prefix enrichment

BGP routing table entries prove that a prefix is *announced*, not that it is *operationally active*. A block can be advertised by a defunct agency, inherited by a successor, or covered by a stale ROA with no live hosts behind it. The enrichment layer cross-references announced prefixes against several independent signals to distinguish maintained infrastructure from routing-table artifacts.

### Why the TU Munich hitlist is not sufficient alone

The TU Munich IPv6 Hitlist sends ICMPv6 echo requests from a single vantage point. Firewalls that drop ICMP — common on federal perimeters — cause reachable hosts to appear offline. Hitlist coverage is strong for open infrastructure but misses:

- Hosts reachable only over TCP (name servers, mail servers, HTTPS endpoints)
- Prefixes that are routed but carry no directly-pingable addresses (transit, management, VPN)
- Hosts behind middleboxes that rate-limit or drop probes from unknown sources

### Enrichment sources

| Source | Signal class | Cadence | Why it helps |
| ------ | ----------- | ------- | ----------- |
| **CISA dotgov-data DNS enumeration** | NS / MX / AAAA for all `.gov` domains | Daily | TCP services on port 53, 25, 443 visible through ICMP-blocking firewalls |
| **NIST USGv6 Deployment Monitor** | Per-domain IPv6 service metrics + DNSSEC status | Daily | Independent TCP reachability measurement; DNSSEC chain = actively maintained zone |
| **Certificate Transparency (crt.sh)** | TLS certs issued to `*.gov` subdomains in last year | Weekly | Cert issuance date = reliable last-active lower bound; reveals subdomains absent from public DNS |
| **RPKI validity (Cloudflare / RIPE Stat)** | ROA coverage for each prefix / origin-AS pair | Weekly | `valid` ROA = operator explicitly authorized this prefix, strong maintenance signal |
| **DNS zone walk** | FQDN enumeration via NSEC chain walk + wordlist + AXFR | Weekly (Wed) | Discovers management interfaces, VPN concentrators, and internal APIs invisible to ICMP hitlists; AXFR success is itself a security finding |
| **RIPE Atlas prefix sweep** | ICMPv6 ping + TCP/443/80/53 + traceroute per prefix | Weekly (Sat) | Multi-type probing finds hosts behind ICMP-blocking firewalls; traceroute hops reveal routers even when end-hosts are firewalled |
| **Rapid7 FDNS AAAA** *(stub)* | Forward DNS AAAA records for the whole `.gov` namespace | On-demand | Broadest hostname coverage; requires Rapid7 Insight account |
| **BGP community tags** | Standard and large communities on RIB entries | Per BGP ingest | Blackhole (`65535:666`) and traffic-engineering tags identify filtered or deprioritized prefixes |

### Enrichment methodology

Each enrichment source resolves hostnames or checks prefix metadata independently. Results are stored in the `domain_services`, `nist_deploymon`, `rpki_validity`, and `bgp_communities` tables (see [Database schema](#database-schema) below). A prefix is considered "likely active" when it has at least one corroborating signal from an enrichment source that is independent of the BGP table itself.

### Setting up RIPE Atlas

`usg_ipv6/pipeline/ripe_atlas.py` is a full multi-type active measurement pipeline. To enable it:

1. Register at [atlas.ripe.net](https://atlas.ripe.net/) and create an API key with *Create User Defined Measurements* scope.
2. Add the key as the `RIPE_ATLAS_API_KEY` GitHub Actions secret.
3. The `atlas-sweep.yml` workflow runs automatically each Saturday. Run `workflow_dispatch` with `dry_run=true` first to verify credit cost before the first live sweep.

**Rapid7 FDNS** (`usg_ipv6/pipeline/rapid7_fdns.py`): Download a Sonar FDNS AAAA `.json.gz` file from [opendata.rapid7.com](https://opendata.rapid7.com/sonar.fdns_v2/) (requires free Insight account). Pass the local path to `run_rapid7_fdns(db_path, fdns_path=...)`.

---

## DNS zone walk enrichment

DNS zone walking discovers every hostname configured in a `.gov` zone by exploiting the enumerable structure of NSEC-signed DNSSEC zones. OMB M-08-23 directed federal agencies to DNSSEC-sign all `.gov` zones, which inadvertently makes many of them completely enumerable. This pipeline turns that property into an IPv6 address discovery signal.

### Why zone walking matters for IPv6 measurement

- ICMP-based hitlists and prefix-offset Atlas sweeps can only find addresses that respond to probes from known vantage points. Management interfaces, VPN concentrators, internal APIs, and mail infrastructure typically live behind firewalls that drop unsolicited probes.
- NSEC-signed zones publish their full hostname list in DNS by design — querying NSEC records iteratively yields every FQDN in the zone, which can then be resolved for AAAA records.
- `draft-fbw-dnsop-dnszonehop` identifies zone walking as the primary IPv6 address discovery vector for federal networks operating under OMB M-21-07.
- AXFR zone transfers, when not properly restricted, return the entire zone in a single query — both a richer data source and a security misconfiguration.

### Four-phase pipeline per zone

Each `.gov` domain is processed through four sequential phases:

#### Phase 1 — AXFR zone transfer attempt

A standard DNS zone transfer (RFC 5936) is attempted against each authoritative nameserver. If the server permits it, the entire zone is returned as a single response containing every record — the most complete possible enumeration. A permitted AXFR is itself a security misconfiguration: authoritative servers should restrict zone transfers to known secondary nameservers. AXFR-permitted zones are flagged `axfr_permitted=TRUE` in `zone_walk_meta` and displayed with a warning on the dashboard.

#### Phase 2 — Zone classification

If AXFR is refused, the zone is probed with a deliberate NXDOMAIN query for a label that cannot exist (`zz9zz9-walk-probe.<domain>`). The DNSSEC denial-of-existence record type in the response reveals the zone's signing configuration:

| Response record | Zone type | Implication |
| --------------- | --------- | ----------- |
| `NSEC` in authority | `nsec` | Zone uses unhashed NSEC — fully walkable |
| `NSEC3` in authority | `nsec3` | Zone uses hashed NSEC3 — walking yields hashes, not names |
| `DNSKEY` present but no NSEC/NSEC3 | `nsec3` | Signed zone, NSEC3 not surfaced in this response |
| No DNSSEC records | `unsigned` | Zone is not DNSSEC-signed |
| Timeout or no response | `error` | NS unreachable or unresponsive |

#### Phase 3 — NSEC iterative chain walk (NSEC zones only)

For zones classified as `nsec`, the pipeline performs a complete iterative walk of the NSEC chain. Each NSEC record's `next` field names the following owner name in the zone's canonical ordering; querying NSEC at each successive name eventually wraps back to the zone apex, at which point the walk is complete. This yields every FQDN in the zone with no guessing — the result is equivalent to a zone transfer in terms of name coverage. A hard cap of 10,000 names per zone prevents runaway walks on unusually large zones.

Discovery method stored in `zone_walk_hosts`: `nsec_walk`.

#### Phase 4 — Wordlist enumeration (NSEC3 and unsigned zones)

Zones using NSEC3 hashing (or no DNSSEC at all) cannot be walked iteratively. Instead, each label in a ~120-entry federal-context wordlist is queried as a subdomain. The wordlist covers common federal hostname patterns: DNS servers (`ns1`, `resolver`), mail infrastructure (`mail`, `smtp`, `owa`, `autodiscover`), remote access (`vpn`, `ssl-vpn`, `citrix`, `rdp`, `bastion`), identity services (`adfs`, `sso`, `ldap`, `dc`), infrastructure (`ntp`, `dhcp`, `proxy`, `mgmt`), monitoring (`grafana`, `splunk`, `siem`), and federal-specific labels (`nipr`, `sipr`, `eauth`, `hris`).

A label is recorded if the DNS response is not NXDOMAIN — meaning the name exists even if it currently has no AAAA record.

Discovery method stored in `zone_walk_hosts`: `wordlist`.

### AAAA resolution and corpus matching

After all four phases complete across all zones (8 concurrent walk workers), all discovered FQDNs are resolved concurrently for AAAA records (30 workers). Each resolved address is matched against corpus IPv6 prefixes via the LPM index. Only addresses that fall inside a corpus prefix are considered corpus matches; addresses on commercial cloud providers (AWS, Akamai, Cloudflare) will not match and are not counted as corpus hits.

### What the counts mean

| Count | Source table | What it measures |
| ----- | ------------ | ---------------- |
| Zones (NSEC / NSEC3 / unsigned / AXFR) | `zone_walk_meta` | How each `.gov` zone was classified and what method was used |
| FQDNs discovered (`names_discovered`) | `zone_walk_meta` | Total hostnames enumerated across all phases |
| With AAAA (`aaaa_resolved`) | `zone_walk_meta` | Discovered FQDNs that resolved to at least one IPv6 address |
| Corpus matches (`corpus_matched`) | `zone_walk_meta` | AAAA addresses that fall inside a corpus prefix |
| Unique corpus addresses | Computed | Deduplicated corpus IPv6 addresses across all zones |

**Why corpus matches are typically low:** Federal `.gov` domains increasingly use commercial CDN and cloud DNS providers (AWS Route 53, Akamai, Cloudflare) for web and mail infrastructure. Those providers' address space is not in the government-operated corpus. Zero or low corpus matches for a given zone does not indicate missing data — it reflects cloud-hosted service delivery.

### Security note

Zone walking queries only publicly available DNS data. Authoritative nameservers publish NSEC records by design; iterative NSEC queries are equivalent to ordinary recursive DNS lookups. AXFR uses the zone-transfer mechanism defined in RFC 5936; a server that refuses is not harmed. Zone names and individual FQDNs discovered by the pipeline are stored in the database for research analysis but are not surfaced on the public dashboard — only aggregate counts by zone type are displayed.

---

## Database schema

The analytical store is a single DuckDB file at `data/usg_ipv6.duckdb`. All tables are created automatically on first run. ASN columns use `BIGINT` to accommodate the full 32-bit unsigned ASN range (0–4,294,967,295).

### `asn_corpus`

| Column | Type | Description |
| -------- | ------ | ------------- |
| `asn` | BIGINT PK | Autonomous System Number |
| `org_name` | VARCHAR | Organization name from ARIN or CAIDA |
| `agency` | VARCHAR | Matched CFO Act agency name |
| `tier` | VARCHAR | `cfo_act`, `dod`, `doe_lab`, `federal_civilian`, `legislative`, or `judicial` |
| `arin_org_handle` | VARCHAR | ARIN org handle as stored in CAIDA (e.g. `ORNL-Z-ARIN`); empty for fuzzy-name-matched entries |
| `confidence` | VARCHAR | `high` (exact CAIDA org_id handle match, or direct ARIN WHOIS query) or `medium` (CAIDA fuzzy name match) |
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

### `hitlist_observations`

One row per (observation date, ASN). Written by `make hitlist` / `usg hitlist ingest`.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `observation_date` | DATE PK | Date the ingest ran (or the backfill date supplied) |
| `asn` | BIGINT PK | Autonomous System Number |
| `responsive_count` | INTEGER | Total hitlist addresses matched to this ASN's prefixes |
| `aliased_count` | INTEGER | Subset of responsive addresses that fall in aliased prefixes |
| `hitlist_date` | DATE | Date of the hitlist file used (from HTTP Last-Modified) |

### `hitlist_addresses`

One row per matched (observation date, address). Provides address-level detail for queries.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `observation_date` | DATE PK | Date the ingest ran |
| `address` | VARCHAR PK | IPv6 address that responded to probes |
| `asn` | BIGINT | ASN that owns the matched prefix |
| `matched_prefix` | VARCHAR | Corpus IPv6 prefix the address fell within |
| `is_aliased` | BOOLEAN | TRUE if the address's prefix is in the aliased-prefixes list |
| `hitlist_date` | DATE | Date of the hitlist file used |

### `nist_deploymon`

One row per (measurement date, domain). Written by `enrichment-nist.yml`.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `measurement_date` | DATE PK | Date of the NIST scrape |
| `domain` | VARCHAR PK | Federal `.gov` domain |
| `org` | VARCHAR | Registered organization |
| `service` | VARCHAR PK | `dns`, `mail`, or `web` |
| `server_count` | INTEGER | Total servers measured for this service |
| `v6_addr_count` | INTEGER | Servers with at least one IPv6 address |
| `v6_reach_count` | INTEGER | Servers reachable over IPv6 |
| `v6_func_count` | INTEGER | Servers where IPv6 service is functional |
| `status` | VARCHAR | NIST status code (`O`, `I`, `M`, `L`, or `-`) |
| `dnssec_signed` | BOOLEAN | Zone is DNSSEC-signed |
| `dnssec_valid` | BOOLEAN | DNSSEC signature validates |
| `dnssec_chained` | BOOLEAN | DNSSEC chain of trust complete |

### `domain_services`

One row per (observation date, domain, record type, hostname). Written by all domain-level enrichment sources.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `observation_date` | DATE PK | Date the record was observed |
| `domain` | VARCHAR PK | Federal `.gov` domain |
| `record_type` | VARCHAR PK | `NS`, `MX`, `AAAA`, `atlas_dns`, or `CT` |
| `hostname` | VARCHAR PK | Resolved hostname |
| `ipv6_address` | VARCHAR | IPv6 address that resolved from this hostname |
| `matched_prefix` | VARCHAR | Corpus IPv6 prefix the address falls within (NULL if no match) |
| `matched_asn` | BIGINT | Corpus ASN owning that prefix (NULL if no match) |
| `source` | VARCHAR | `dotgov_dns`, `nist`, `ct`, `ripe_atlas`, or `rapid7` |

### `rpki_validity`

One row per (check date, prefix, origin ASN). Written by `enrichment-rpki.yml`.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `check_date` | DATE PK | Date the RPKI check ran |
| `prefix` | VARCHAR PK | CIDR prefix string |
| `origin_asn` | BIGINT PK | Origin AS from the BGP table |
| `status` | VARCHAR | `valid`, `invalid`, or `not_found` |
| `roa_max_length` | INTEGER | ROA max-length (NULL when status ≠ `valid`) |

### `atlas_observations`

One row per (observation date, target address, measurement type). Written by `atlas-sweep.yml` / `run_prefix_sweep()`.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `observation_date` | DATE PK | Date the sweep ran |
| `target_address` | VARCHAR PK | IPv6 address that was probed |
| `matched_prefix` | VARCHAR | Corpus IPv6 prefix the address falls within |
| `origin_asn` | BIGINT | Corpus ASN owning that prefix |
| `measurement_type` | VARCHAR PK | `ping`, `tcp_443`, `tcp_80`, `tcp_53`, or `traceroute` |
| `responsive` | BOOLEAN | TRUE if ≥1 probe received a response |
| `rtt_ms` | DOUBLE | Median RTT across responding probes (NULL if none responded) |
| `probe_count` | INTEGER | Number of Atlas probes used |
| `responding_probes` | INTEGER | Count of probes that received a response |
| `msm_id` | BIGINT | Atlas measurement ID (for result verification) |

### `atlas_hop_addresses`

Router addresses discovered inside corpus prefixes via traceroute hop extraction. These addresses respond to traceroute even when the end-hosts at the same prefix are firewalled against ICMPv6 echo and TCP probes.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `observation_date` | DATE PK | Date the sweep ran |
| `hop_address` | VARCHAR PK | IPv6 router/hop address found in traceroute results |
| `matched_prefix` | VARCHAR | Corpus IPv6 prefix the hop address falls within |
| `origin_asn` | BIGINT | Corpus ASN owning that prefix |
| `probe_target` | VARCHAR PK | Target address of the measurement that produced this hop |
| `msm_id` | BIGINT | Atlas measurement ID |

### `bgp_communities`

One row per (snapshot, collector, prefix, origin ASN, community string). Written during BGP MRT normalization.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `snapshot_ts` | TIMESTAMP PK | Source snapshot timestamp |
| `collector` | VARCHAR PK | Source collector |
| `prefix` | VARCHAR PK | CIDR prefix string |
| `origin_asn` | BIGINT PK | Origin ASN |
| `community` | VARCHAR PK | Community string: `asn:value` (standard) or `asn:ld1:ld2` (large) |

### `zone_walk_meta`

One row per (scan date, domain). Per-domain summary written by `enrichment-zone-walk.yml`.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `scan_date` | DATE PK | Date the zone walk ran |
| `domain` | VARCHAR PK | Federal `.gov` domain |
| `zone_type` | VARCHAR | `nsec`, `nsec3`, `axfr`, `unsigned`, or `error` — see [DNS zone walk enrichment](#dns-zone-walk-enrichment) |
| `axfr_permitted` | BOOLEAN | TRUE if the authoritative server permitted a zone transfer (security finding) |
| `names_discovered` | INTEGER | Total FQDNs found in this zone via the walk/wordlist/AXFR |
| `aaaa_resolved` | INTEGER | FQDNs that resolved to at least one AAAA record |
| `corpus_matched` | INTEGER | AAAA addresses that fell inside a corpus IPv6 prefix |

### `zone_walk_hosts`

One row per (scan date, domain, hostname, IPv6 address). Per-address detail written by `enrichment-zone-walk.yml`. Zone names and FQDNs are stored here but not surfaced on the public dashboard.

| Column | Type | Description |
| -------- | ------ | ------------- |
| `scan_date` | DATE PK | Date the zone walk ran |
| `domain` | VARCHAR PK | Federal `.gov` parent domain |
| `hostname` | VARCHAR PK | Discovered FQDN |
| `ipv6_address` | VARCHAR PK | IPv6 address that resolved from this hostname |
| `matched_prefix` | VARCHAR | Corpus IPv6 prefix the address falls within (NULL if no match) |
| `matched_asn` | BIGINT | Corpus ASN owning that prefix (NULL if no match) |
| `discovery_method` | VARCHAR | `nsec_walk`, `axfr`, `wordlist`, or `none` |

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

-- Hitlist: responsive address counts per agency (most recent observation)
SELECT c.agency, c.tier, ho.asn,
       ho.responsive_count, ho.aliased_count,
       ho.responsive_count - ho.aliased_count AS likely_real,
       ho.hitlist_date
FROM hitlist_observations ho
JOIN asn_corpus c ON ho.asn = c.asn
WHERE ho.observation_date = (SELECT MAX(observation_date) FROM hitlist_observations)
ORDER BY ho.responsive_count DESC;

-- Hitlist: agencies with IPv6 prefixes but zero responsive addresses
SELECT c.agency, c.tier, c.asn
FROM asn_corpus c
WHERE c.asn IN (
    SELECT DISTINCT asn FROM caida_pfx2as WHERE af = 6
)
AND c.asn NOT IN (
    SELECT asn FROM hitlist_observations
    WHERE observation_date = (SELECT MAX(observation_date) FROM hitlist_observations)
      AND responsive_count > 0
)
ORDER BY c.tier, c.agency;

-- Hitlist: all matched addresses for a specific ASN
SELECT address, matched_prefix, is_aliased, hitlist_date
FROM hitlist_addresses
WHERE observation_date = (SELECT MAX(observation_date) FROM hitlist_addresses)
  AND asn = 297
ORDER BY address;

-- Atlas: responsive addresses by ASN (most recent sweep, any measurement type)
SELECT c.agency, c.tier, ao.origin_asn,
       COUNT(DISTINCT ao.target_address) AS responsive_addresses,
       COUNT(DISTINCT ao.measurement_type) AS measurement_types_responding
FROM atlas_observations ao
JOIN asn_corpus c ON ao.origin_asn = c.asn
WHERE ao.observation_date = (SELECT MAX(observation_date) FROM atlas_observations)
  AND ao.responsive = TRUE
GROUP BY c.agency, c.tier, ao.origin_asn
ORDER BY responsive_addresses DESC;

-- Atlas: addresses found only via TCP (not visible to ICMP/hitlist)
SELECT DISTINCT ao.target_address, ao.matched_prefix, ao.origin_asn,
       c.agency, ao.measurement_type, ao.rtt_ms
FROM atlas_observations ao
JOIN asn_corpus c ON ao.origin_asn = c.asn
WHERE ao.observation_date = (SELECT MAX(observation_date) FROM atlas_observations)
  AND ao.measurement_type IN ('tcp_443', 'tcp_80', 'tcp_53')
  AND ao.responsive = TRUE
  AND ao.target_address NOT IN (
      SELECT target_address FROM atlas_observations
      WHERE measurement_type = 'ping' AND responsive = TRUE
        AND observation_date = (SELECT MAX(observation_date) FROM atlas_observations)
  )
ORDER BY ao.origin_asn, ao.target_address;

-- Atlas: router hop addresses discovered inside corpus prefixes via traceroute
SELECT ah.hop_address, ah.matched_prefix, ah.origin_asn, c.agency,
       COUNT(*) AS seen_in_n_traceroutes
FROM atlas_hop_addresses ah
JOIN asn_corpus c ON ah.origin_asn = c.asn
WHERE ah.observation_date = (SELECT MAX(observation_date) FROM atlas_hop_addresses)
GROUP BY ah.hop_address, ah.matched_prefix, ah.origin_asn, c.agency
ORDER BY seen_in_n_traceroutes DESC;

-- Zone walk: per-zone-type summary for the most recent scan
SELECT zone_type,
       COUNT(*)              AS zones,
       SUM(names_discovered) AS fqdns_found,
       SUM(aaaa_resolved)    AS with_aaaa,
       SUM(corpus_matched)   AS corpus_matches
FROM zone_walk_meta
WHERE scan_date = (SELECT MAX(scan_date) FROM zone_walk_meta)
GROUP BY zone_type
ORDER BY fqdns_found DESC;

-- Zone walk: domains that permitted AXFR (security finding)
SELECT domain, names_discovered, aaaa_resolved, corpus_matched
FROM zone_walk_meta
WHERE scan_date = (SELECT MAX(scan_date) FROM zone_walk_meta)
  AND axfr_permitted = TRUE
ORDER BY names_discovered DESC;

-- Zone walk: corpus IPv6 addresses discovered via zone walk (most recent scan)
SELECT h.domain, h.hostname, h.ipv6_address, h.matched_prefix,
       c.agency, h.discovery_method
FROM zone_walk_hosts h
JOIN asn_corpus c ON h.matched_asn = c.asn
WHERE h.scan_date = (SELECT MAX(scan_date) FROM zone_walk_hosts)
ORDER BY c.agency, h.domain, h.hostname;

-- Zone walk: unique corpus IPv6 addresses per agency
SELECT c.agency, c.tier, COUNT(DISTINCT h.ipv6_address) AS corpus_addrs
FROM zone_walk_hosts h
JOIN asn_corpus c ON h.matched_asn = c.asn
WHERE h.scan_date = (SELECT MAX(scan_date) FROM zone_walk_hosts)
  AND h.matched_prefix IS NOT NULL
GROUP BY c.agency, c.tier
ORDER BY corpus_addrs DESC;
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

### Hitlist pipeline

```python
from datetime import datetime
from usg_ipv6.pipeline.hitlist import run_hitlist_ingest, run_hitlist_backfill

# Ingest the current hitlist file (observation_date defaults to today)
result = run_hitlist_ingest("data/usg_ipv6.duckdb")
print(result)
# {"observation_date": "2024-10-04", "hitlist_date": "2024-10-04",
#  "total_scanned": 4821037, "matched_addresses": 312, "asns_with_responses": 14,
#  "skipped": False}

# Ingest with a specific observation date
result = run_hitlist_ingest("data/usg_ipv6.duckdb", date="2024-01-15")

# Backfill a date range (downloads once, stamps each date in range)
result = run_hitlist_backfill(
    "data/usg_ipv6.duckdb",
    datetime(2024, 1, 1),
    datetime(2024, 12, 31),
    dest_dir="data/raw",
)
print(result)  # {"processed": 366, "skipped": 0, "failed": 0}
```

### RIPE Atlas pipeline

```python
from usg_ipv6.pipeline.ripe_atlas import run_prefix_sweep, estimate_credits, _PROBE_OFFSETS

# Estimate cost before committing
cost = estimate_credits(
    prefix_count=530,
    targets_per_prefix=len(_PROBE_OFFSETS),  # 10
    probe_count=20,
    measurement_labels=None,  # None = all five types
)
print(f"Estimated credits: {cost:,}")  # ~4,346,000

# Dry run: generate targets and estimate without creating any measurements
result = run_prefix_sweep(
    db_path="data/usg_ipv6.duckdb",
    api_key="",  # not used in dry_run mode
    dry_run=True,
)
print(result)
# {"prefixes": 530, "targets": 5300, "estimated_credits": 4346000, "measurements_created": 0, "dry_run": True}

# Live sweep (requires RIPE Atlas API key with measurement creation scope)
result = run_prefix_sweep(
    db_path="data/usg_ipv6.duckdb",
    api_key="YOUR_ATLAS_API_KEY",
    probe_count=20,
    country_code="US",
    measurement_labels=["ping", "tcp_443"],  # subset to reduce cost
)
print(result)
# {"prefixes": 530, "targets": 5300, "measurements_created": ...,
#  "unique_responsive_addresses": ..., "unique_hop_addresses": ...}
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

### Zone walk pipeline

```python
from datetime import date
from usg_ipv6.pipeline.zone_walk import run_zone_walk

# Walk all ~1,200 federal .gov domains (takes up to ~90 minutes)
result = run_zone_walk("data/usg_ipv6.duckdb")
print(result)
# {"scan_date": "2026-10-19", "domains_processed": 1187,
#  "axfr_zones": 2, "nsec_zones": 312, "nsec3_zones": 498,
#  "unsigned_zones": 201, "error_zones": 174,
#  "names_discovered": 48210, "aaaa_resolved": 9831,
#  "corpus_matched": 412, "unique_corpus_addresses": 287}

# Test run against a small subset
result = run_zone_walk("data/usg_ipv6.duckdb", domain_limit=50)

# Override scan date (for backfill or replay)
result = run_zone_walk("data/usg_ipv6.duckdb", scan_date=date(2026, 10, 1))
```

**Requires:** `caida_pfx2as` must contain IPv6 (af=6) rows for corpus prefix matching. The GitHub Actions workflow ensures this automatically; for local runs, run `make run COLLECTOR=route-views6` first.

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
| [TU Munich IPv6 Hitlist](https://ipv6hitlist.github.io) | Verified responsive IPv6 addresses (weekly) | Public HTTP, no auth |
| [CISA dotgov-data](https://github.com/cisagov/dotgov-data) | Full list of registered federal `.gov` domains; domain list for zone walk | Public GitHub, no auth |
| [NIST USGv6 Deployment Monitor](https://usgv6-deploymon.nist.gov/cgi-bin/generate-gov) | Per-domain IPv6 service metrics and DNSSEC status (enrichment) | Public HTTP, no auth |
| [crt.sh Certificate Transparency](https://crt.sh) | TLS certificates issued to `*.gov` subdomains (enrichment) | Public REST API, no auth |
| [Cloudflare RPKI API](https://rpki.cloudflare.com/api/v1/validity/) | RPKI ROA validity for corpus prefix / ASN pairs (enrichment) | Public REST API, no auth |
| [RIPE Stat RPKI API](https://stat.ripe.net/docs/02.data-api/) | RPKI fallback when Cloudflare API is unavailable (enrichment) | Public REST API, no auth |
| [Rapid7 Sonar FDNS](https://opendata.rapid7.com/sonar.fdns_v2/) | Forward DNS AAAA records for `.gov` (enrichment stub) | Requires Insight account |
| [RIPE Atlas](https://atlas.ripe.net/api/v2/) | Multi-type IPv6 prefix sweep (ping, TCP, traceroute) from US probes | Requires API key + credits |

**ARIN in CI:** ARIN WHOIS is unreachable from GitHub Actions IP ranges. A fast pre-flight check (`arin_reachable()`) detects this and skips ARIN queries rather than burning through retries, falling back to CAIDA-only corpus building.

---

## Configuration variables

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `DB_PATH` | `data/usg_ipv6.duckdb` | Path to the DuckDB analytical store |
| `COLLECTOR` | `route-views2` | RouteViews collector name |
| `SINCE` | `2021-01-01` | Backfill start date (ISO 8601) |
| `UNTIL` | `2024-12-31` | Backfill end date (ISO 8601) |
| `AS2ORG` | *(empty — auto-download)* | Path to a local CAIDA AS2Org file; if unset, the most recent monthly file is downloaded automatically |

---

## Project layout

```text
usg_ipv6/
  db.py                  # DuckDB connection and schema initialisation
  cli.py                 # usg CLI (typer): stats, corpus, prefixes, snapshots, run, ark, hitlist
  report_html.py         # Static HTML report generator for GitHub Pages
  corpus/
    federal_agencies.py  # Seed list: 90+ agencies across 6 tiers with verified ARIN handles
    arin.py              # ARIN WHOIS REST client + arin_reachable() pre-flight
    caida.py             # CAIDA AS2Org downloader and parser (monthly cadence)
    schema.py            # AsnRecord dataclass, tier priority constants
    build.py             # Corpus builder: ARIN + CAIDA merge, CSV + DuckDB output
  pipeline/
    routeviews.py        # RouteViews URL construction and MRT downloader
    parse.py             # MRT TABLE_DUMP_V2 parser → Parquet; BGP community extraction (type 8 + 32)
    normalize.py         # Parquet → prefix_records + bgp_communities (filtered to corpus ASNs)
    caida_pfx2as.py      # CAIDA pfx2as downloader and loader
    runner.py            # Orchestrator: run_incremental, run_backfill
    lpm.py               # Stdlib-only longest-prefix-match engine (IPv6)
    ark.py               # CAIDA Ark downloader, parser, and ingest orchestrator
    hitlist.py           # TU Munich IPv6 Hitlist downloader, matcher, and ingest orchestrator
    dotgov_dns.py        # CISA dotgov-data DNS AAAA enumeration (NS / MX / web) → domain_services
    nist_deploymon.py    # NIST USGv6 scraper (IPv6 service metrics + DNSSEC) → nist_deploymon, domain_services
    cert_transparency.py # crt.sh SAN resolution for *.gov certs → domain_services
    rpki_validity.py     # Cloudflare / RIPE RPKI ROA validation → rpki_validity
    zone_walk.py         # DNS zone walk: AXFR + NSEC chain walk + wordlist → zone_walk_meta / zone_walk_hosts
    rapid7_fdns.py       # Stub: Rapid7 Sonar FDNS AAAA ingest (requires Insight account)
    ripe_atlas.py        # RIPE Atlas prefix sweep: ping/TCP/traceroute per corpus IPv6 prefix

.github/workflows/
  corpus-update.yml             # Weekly corpus rebuild; manual dispatch commits to main
  routeviews-ingest.yml         # Weekly MRT ingest; updates data-latest release
  routeviews-backfill.yml       # Manual historical backfill
  ark-ingest.yml                # Wednesday Ark IPv6 reachability ingest
  hitlist-ingest.yml            # Friday IPv6 Hitlist responsive-address ingest
  atlas-sweep.yml               # Saturday RIPE Atlas prefix sweep (requires RIPE_ATLAS_API_KEY)
  enrichment-dotgov-dns.yml     # Daily .gov DNS AAAA enumeration (06:42 UTC)
  enrichment-nist.yml           # Daily NIST USGv6 monitor scrape (20:17 UTC)
  enrichment-cert-transparency.yml # Weekly crt.sh Certificate Transparency (Mon 03:23 UTC)
  enrichment-rpki.yml           # Weekly RPKI ROA validity check (Tue 04:11 UTC)
  enrichment-zone-walk.yml      # Weekly DNS zone walk: AXFR + NSEC + wordlist (Wed 14:00 UTC)
  publish.yml                   # Versioned corpus release on push to main
  pages.yml                     # Static HTML dashboard → GitHub Pages

tests/
  conftest.py            # Shared pytest fixtures (in-memory DuckDB)
  corpus/                # Unit tests for corpus modules
  pipeline/              # Unit tests for pipeline modules
    test_hitlist.py      # 18 network-free tests for hitlist ingest
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

## License

[LICENSE](LICENSE.md)

## AI-Assisted Development Notice

This software contains components generated by Large Language Model (LLM) or machine intelligence platforms. AI tools were used to assist in various stages of the development lifecycle. A human reviewed all AI-generated content. Code generated by LLMs may occasionally contain subtle logical errors or inefficiencies that were not detected during review.
