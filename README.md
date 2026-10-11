# USG IPv6 Statistics

Measures IPv4/IPv6 prefix deployment over time for US Government ASNs, using RouteViews BGP data as the primary source and CAIDA datasets for corroboration. Focused on the OMB M-21-07 mandate period (2020–present).

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Usage

```bash
# Build the USG ASN corpus (ARIN WHOIS + CAIDA AS2Org)
make corpus

# Fetch and process the most recent RIB snapshot
make run

# Backfill a date range
make backfill SINCE=2021-01-01 UNTIL=2024-12-31

# Run tests
make test
```

## Data sources

- **RouteViews** — MRT RIB dumps (primary BGP data)
- **CAIDA RouteViews pfx2as** — prefix-to-ASN mapping (corroborating)
- **CAIDA AS2Org** — ASN-to-organization mapping (corpus construction)
- **ARIN WHOIS REST API** — authoritative ASN registration data

## Project layout

```text
usg_ipv6/
  corpus/     # USG ASN corpus construction (ARIN + CAIDA)
  pipeline/   # MRT fetch, parse, normalize, CAIDA loaders, runner
  db.py       # DuckDB connection and schema
tests/        # pytest suite (29 tests)
data/         # DuckDB store and raw downloads (gitignored)
docs/         # Design spec and implementation plan
```

## Background

Tracks USG compliance with [OMB Memorandum M-21-07](https://www.whitehouse.gov/wp-content/uploads/2020/11/M-21-07.pdf), which directed federal agencies to adopt IPv6-only networking by FY2025.
