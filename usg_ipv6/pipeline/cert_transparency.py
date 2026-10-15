"""
Certificate Transparency enrichment pipeline
=============================================
Queries crt.sh for TLS certificates issued to *.gov subdomains, then
resolves each certificate SAN to AAAA records and checks whether those
addresses fall inside corpus-tracked IPv6 prefixes.

Why CT logs beat active scanning for some hosts:
- A cert issued last week for internal-api.agency.gov with an AAAA in
  our prefix means the prefix is being actively maintained.
- CT exposes subdomains not present in public DNS (pre-issuance SCTs,
  wildcard expansion, internal hostnames on public CAs).
- Cert validity window gives a reliable "last active no earlier than"
  timestamp.

API: https://crt.sh/?q=%.gov&output=json (no auth, no rate-limit key,
     but be polite — query per registered domain from the dotgov CSV
     rather than a single massive wildcard query).
"""

import ipaddress
import logging
import time
from datetime import date as date_cls, datetime

import requests

from usg_ipv6.pipeline.dotgov_dns import (
    _build_prefix_nets,
    _fetch_federal_domains,
    _match_prefix,
    _resolve_aaaa,
)

log = logging.getLogger(__name__)

CRT_SH_URL = "https://crt.sh/"
REQUEST_DELAY = 0.5   # seconds between crt.sh requests (polite)
RECENT_DAYS = 365     # only consider certs not-before within this window


def _fetch_certs(domain: str) -> list[dict]:
    """Return crt.sh cert records for all SANs under a domain."""
    try:
        r = requests.get(
            CRT_SH_URL,
            params={"q": f"%.{domain}", "output": "json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.debug("crt.sh query failed for %s: %s", domain, exc)
        return []


def _extract_names(cert: dict) -> list[str]:
    """Extract unique hostnames from the name_value field (newline-separated SANs)."""
    raw = cert.get("name_value", "")
    names = set()
    for line in raw.splitlines():
        name = line.strip().lower().lstrip("*.")
        if name and "." in name:
            names.add(name)
    return list(names)


def _is_recent(cert: dict, cutoff: date_cls) -> bool:
    raw = cert.get("not_before", "")
    try:
        nb = datetime.strptime(raw[:10], "%Y-%m-%d").date()
        return nb >= cutoff
    except Exception:
        return False


def run_cert_transparency(
    db_path: str,
    observation_date: date_cls | None = None,
    recent_days: int = RECENT_DAYS,
) -> dict:
    """
    Query crt.sh for *.gov certs, resolve SANs to AAAA, match corpus prefixes.

    Returns summary dict with domains_checked, certs_found, rows_inserted, matches.
    """
    from usg_ipv6.db import get_connection, init_schema

    obs_date = observation_date or date_cls.today()
    cutoff = date_cls.fromordinal(obs_date.toordinal() - recent_days)

    conn = get_connection(db_path)
    init_schema(conn)

    corpus_prefixes = conn.execute(
        "SELECT DISTINCT prefix, origin_asn FROM prefix_records WHERE af = 6"
    ).fetchall()
    nets = _build_prefix_nets(corpus_prefixes)

    domains = _fetch_federal_domains()
    log.info("CT: checking %d .gov domains against crt.sh", len(domains))

    conn.execute(
        "DELETE FROM domain_services WHERE observation_date = ? AND source = 'ct'",
        [obs_date],
    )

    svc_rows: list[tuple] = []
    total_certs = 0
    seen_hostnames: set[str] = set()

    for domain in domains:
        certs = _fetch_certs(domain)
        recent = [c for c in certs if _is_recent(c, cutoff)]
        total_certs += len(recent)

        for cert in recent:
            for hostname in _extract_names(cert):
                if hostname in seen_hostnames:
                    continue
                seen_hostnames.add(hostname)
                for addr in _resolve_aaaa(hostname):
                    pfx, asn = _match_prefix(addr, nets)
                    svc_rows.append((
                        obs_date, domain, "ct", hostname, addr, pfx, asn, "ct"
                    ))

        time.sleep(REQUEST_DELAY)

    matches = sum(1 for r in svc_rows if r[5] is not None)
    if svc_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO domain_services VALUES (?,?,?,?,?,?,?,?)",
            svc_rows,
        )

    conn.close()
    log.info(
        "CT: %d domains, %d recent certs, %d unique hostnames resolved, %d corpus matches",
        len(domains), total_certs, len(seen_hostnames), matches,
    )
    return {
        "domains_checked": len(domains),
        "certs_found": total_certs,
        "hostnames_resolved": len(seen_hostnames),
        "rows_inserted": len(svc_rows),
        "matches": matches,
    }
