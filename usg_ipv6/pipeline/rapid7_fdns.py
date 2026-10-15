"""
Rapid7 Sonar Forward DNS enrichment pipeline (stub)
=====================================================
Rapid7's Project Sonar publishes a weekly Forward DNS (FDNS) dataset
containing AAAA (and A, MX, NS, CNAME, TXT) records for ~3.6 billion
domain names.  The dataset is free for security researchers but requires
a Rapid7 Insight account and acceptance of their data-sharing terms.

Access:
  1. Register at https://www.rapid7.com/research/project-sonar/
  2. Accept the terms at https://opendata.rapid7.com/ (now sonardata.rapid7.com)
  3. Download the FDNS_AAAA dataset (~10-30 GB gzipped JSON-lines)

Dataset format (one JSON object per line):
  {"timestamp": "...", "name": "host.agency.gov", "type": "aaaa",
   "value": "2001:db8::1"}

Pipeline sketch:
  1. Download latest FDNS_AAAA file from the Sonar S3 bucket
     (URL pattern: https://opendata.rapid7.com/sonar.fdns_v2/YYYY-MM-DD-<hash>-fdns_aaaa.json.gz)
  2. Stream-decompress and filter lines where:
       - name ends with ".gov"
       - type == "aaaa"
  3. For each matching record, check whether the value (AAAA address)
     falls within a corpus-tracked prefix using _match_prefix().
  4. Insert matches into domain_services with source='rapid7_fdns'.

Advantage over active DNS queries: Rapid7 has already done the querying
from a globally distributed scanner fleet, giving you sub-domain coverage
that a targeted dotgov-DNS run would miss (vendor portals, CDN origins,
internal hostnames that happen to have public DNS records).

To implement: set RAPID7_API_KEY in the repo's GitHub Actions secrets
and add the download step to the workflow below.
"""

import logging

log = logging.getLogger(__name__)


def run_rapid7_fdns(db_path: str, fdns_file: str, observation_date=None) -> dict:
    """
    Process a locally-downloaded Rapid7 FDNS AAAA file.

    fdns_file: path to the downloaded .json.gz file.
    Returns a summary dict with rows_checked, rows_inserted, matches.
    """
    import gzip
    import json
    import ipaddress
    from datetime import date as date_cls

    from usg_ipv6.db import get_connection, init_schema
    from usg_ipv6.pipeline.dotgov_dns import _build_prefix_nets, _match_prefix

    obs_date = observation_date or date_cls.today()
    conn = get_connection(db_path)
    init_schema(conn)

    corpus_prefixes = conn.execute(
        "SELECT DISTINCT prefix, origin_asn FROM prefix_records WHERE af = 6"
    ).fetchall()
    nets = _build_prefix_nets(corpus_prefixes)

    conn.execute(
        "DELETE FROM domain_services WHERE observation_date = ? AND source = 'rapid7_fdns'",
        [obs_date],
    )

    rows_checked = 0
    svc_rows: list[tuple] = []

    with gzip.open(fdns_file, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "aaaa":
                continue
            name = rec.get("name", "").lower()
            if not name.endswith(".gov"):
                continue
            addr = rec.get("value", "")
            rows_checked += 1

            pfx, asn = _match_prefix(addr, nets)
            # Extract second-level domain for the domain column
            parts = name.rstrip(".").split(".")
            domain = ".".join(parts[-2:]) if len(parts) >= 2 else name
            svc_rows.append((obs_date, domain, "fdns", name, addr, pfx, asn, "rapid7_fdns"))

    matches = sum(1 for r in svc_rows if r[5] is not None)
    if svc_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO domain_services VALUES (?,?,?,?,?,?,?,?)",
            svc_rows,
        )

    conn.close()
    log.info("Rapid7 FDNS: %d AAAA records checked, %d corpus matches", rows_checked, matches)
    return {"rows_checked": rows_checked, "rows_inserted": len(svc_rows), "matches": matches}
