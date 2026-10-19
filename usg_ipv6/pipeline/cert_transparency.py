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

Architecture (concurrent — avoids CI timeout):
  1. Fetch crt.sh cert lists for all domains concurrently (_CRT_WORKERS).
  2. Collect all unique hostnames from recent certs across all domains.
  3. Resolve all unique hostnames to AAAA concurrently (_DNS_WORKERS).
  Sequential-per-domain fetching with a per-request sleep previously
  caused 120-minute CI timeouts on the ~1,500-domain federal list.

API: https://crt.sh/?q=%.gov&output=json (no auth, no rate-limit key).
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
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
RECENT_DAYS = 365   # only consider certs not-before within this window
_CRT_WORKERS = 8    # concurrent crt.sh fetch threads (polite toward crt.sh)
_DNS_WORKERS = 20   # concurrent AAAA resolution threads


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

    Phase 1: fetch cert lists for all domains concurrently.
    Phase 2: resolve all unique hostnames to AAAA concurrently.
    This replaces the sequential-per-domain loop that caused CI timeouts.

    Returns summary dict: domains_checked, certs_found, hostnames_resolved,
    rows_inserted, matches.
    """
    from usg_ipv6.db import get_connection, init_schema

    obs_date = observation_date or date_cls.today()
    cutoff = date_cls.fromordinal(obs_date.toordinal() - recent_days)

    conn = get_connection(db_path)
    init_schema(conn)

    corpus_prefixes = conn.execute("""
        SELECT DISTINCT c.prefix, c.asn AS origin_asn
        FROM caida_pfx2as c
        JOIN asn_corpus ac ON ac.asn = c.asn
        WHERE c.af = 6
          AND c.snapshot_date = (SELECT MAX(snapshot_date) FROM caida_pfx2as WHERE af = 6)
    """).fetchall()
    nets = _build_prefix_nets(corpus_prefixes)

    domains = _fetch_federal_domains()
    log.info("CT: fetching certs for %d .gov domains (concurrent, %d workers)",
             len(domains), _CRT_WORKERS)

    # ── Phase 1: fetch all cert lists concurrently ────────────────────────────
    # Maps domain → list of recent cert dicts.
    domain_certs: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=_CRT_WORKERS) as pool:
        futures = {pool.submit(_fetch_certs, d): d for d in domains}
        for fut in as_completed(futures):
            domain = futures[fut]
            try:
                all_certs = fut.result()
                domain_certs[domain] = [c for c in all_certs if _is_recent(c, cutoff)]
            except Exception as exc:
                log.warning("CT fetch error for %s: %s", domain, exc)
                domain_certs[domain] = []

    # Collect all unique hostnames across all domains.
    total_certs = sum(len(certs) for certs in domain_certs.values())
    hostname_to_domain: dict[str, str] = {}
    for domain, certs in domain_certs.items():
        for cert in certs:
            for hostname in _extract_names(cert):
                if hostname not in hostname_to_domain:
                    hostname_to_domain[hostname] = domain

    log.info("CT: %d recent certs, %d unique hostnames to resolve",
             total_certs, len(hostname_to_domain))

    # ── Phase 2: resolve all unique hostnames concurrently ───────────────────
    hostname_addrs: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=_DNS_WORKERS) as pool:
        futures = {pool.submit(_resolve_aaaa, h): h for h in hostname_to_domain}
        for fut in as_completed(futures):
            hostname = futures[fut]
            try:
                hostname_addrs[hostname] = fut.result()
            except Exception as exc:
                log.debug("CT AAAA resolution error for %s: %s", hostname, exc)
                hostname_addrs[hostname] = []

    # ── Build rows and store ──────────────────────────────────────────────────
    conn.execute(
        "DELETE FROM domain_services WHERE observation_date = ? AND source = 'ct'",
        [obs_date],
    )

    svc_rows: list[tuple] = []
    for hostname, domain in hostname_to_domain.items():
        for addr in hostname_addrs.get(hostname, []):
            pfx, asn = _match_prefix(addr, nets)
            svc_rows.append((obs_date, domain, "ct", hostname, addr, pfx, asn, "ct"))

    matches = sum(1 for r in svc_rows if r[5] is not None)
    if svc_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO domain_services VALUES (?,?,?,?,?,?,?,?)",
            svc_rows,
        )

    conn.close()
    log.info(
        "CT: %d domains, %d recent certs, %d unique hostnames, %d rows, %d corpus matches",
        len(domains), total_certs, len(hostname_to_domain), len(svc_rows), matches,
    )
    return {
        "domains_checked": len(domains),
        "certs_found": total_certs,
        "hostnames_resolved": len(hostname_to_domain),
        "rows_inserted": len(svc_rows),
        "matches": matches,
    }
