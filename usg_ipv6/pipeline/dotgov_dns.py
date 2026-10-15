"""
dotgov DNS enumeration pipeline
================================
Fetches all federal .gov domains from the CISA dotgov-data repo, then
resolves NS, MX, and web (apex + www) AAAA records for each domain.
Addresses that fall within a corpus-tracked IPv6 prefix are recorded in
the domain_services table — giving operational confirmation that the
prefix hosts live DNS/mail/web infrastructure, which is a much stronger
signal than a BGP route alone (TCP services pass through firewalls that
block ICMP, evading the TU Munich hitlist).

Data source: https://github.com/cisagov/dotgov-data (updated daily)
"""

import csv
import io
import ipaddress
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as date_cls

import dns.resolver
import requests

log = logging.getLogger(__name__)

DOTGOV_CSV_URL = (
    "https://raw.githubusercontent.com/cisagov/dotgov-data/main/current-federal.csv"
)
DNS_TIMEOUT = 5      # seconds per query
MAX_WORKERS = 20     # concurrent DNS threads
RECORD_TYPES = ("ns", "mx", "web")


def _fetch_federal_domains() -> list[str]:
    r = requests.get(DOTGOV_CSV_URL, timeout=30)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    # Column is "Domain Name" in the CISA CSV
    domains = []
    for row in reader:
        d = (row.get("Domain Name") or row.get("domain_name") or "").strip().lower()
        if d:
            domains.append(d)
    return domains


def _resolve_aaaa(hostname: str) -> list[str]:
    try:
        answers = dns.resolver.resolve(hostname, "AAAA", lifetime=DNS_TIMEOUT)
        return [str(r.address) for r in answers]
    except Exception:
        return []


def _resolve_ns(domain: str) -> list[str]:
    try:
        answers = dns.resolver.resolve(domain, "NS", lifetime=DNS_TIMEOUT)
        return [str(r.target).rstrip(".") for r in answers]
    except Exception:
        return []


def _resolve_mx(domain: str) -> list[str]:
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=DNS_TIMEOUT)
        return [str(r.exchange).rstrip(".") for r in answers]
    except Exception:
        return []


def _build_prefix_nets(corpus_prefixes: list[tuple]) -> list[tuple]:
    """Pre-parse corpus prefix strings into (network, asn) tuples."""
    nets = []
    for prefix, asn in corpus_prefixes:
        try:
            nets.append((ipaddress.ip_network(prefix, strict=False), asn, prefix))
        except ValueError:
            pass
    return nets


def _match_prefix(addr: str, nets: list[tuple]) -> tuple[str | None, int | None]:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None, None
    for net, asn, prefix_str in nets:
        if ip in net:
            return prefix_str, asn
    return None, None


def _process_domain(domain: str, nets: list[tuple], obs_date: date_cls) -> list[tuple]:
    rows = []

    # NS records
    for ns_host in _resolve_ns(domain):
        for addr in _resolve_aaaa(ns_host):
            pfx, asn = _match_prefix(addr, nets)
            rows.append((obs_date, domain, "ns", ns_host, addr, pfx, asn, "dotgov_dns"))

    # MX records
    for mx_host in _resolve_mx(domain):
        for addr in _resolve_aaaa(mx_host):
            pfx, asn = _match_prefix(addr, nets)
            rows.append((obs_date, domain, "mx", mx_host, addr, pfx, asn, "dotgov_dns"))

    # Web: apex + www
    for hostname in [domain, f"www.{domain}"]:
        for addr in _resolve_aaaa(hostname):
            pfx, asn = _match_prefix(addr, nets)
            rows.append((obs_date, domain, "web", hostname, addr, pfx, asn, "dotgov_dns"))

    return rows


def run_dotgov_dns(db_path: str, observation_date: date_cls | None = None) -> dict:
    """
    Enumerate .gov domains and store IPv6 service observations.

    Returns a summary dict with domains_checked, rows_inserted, matches.
    """
    from usg_ipv6.db import get_connection, init_schema

    obs_date = observation_date or date_cls.today()

    conn = get_connection(db_path)
    init_schema(conn)

    corpus_prefixes = conn.execute(
        "SELECT DISTINCT prefix, origin_asn FROM prefix_records WHERE af = 6"
    ).fetchall()

    if not corpus_prefixes:
        log.warning("No IPv6 corpus prefixes found — skipping dotgov DNS enumeration")
        conn.close()
        return {"domains_checked": 0, "rows_inserted": 0, "matches": 0}

    nets = _build_prefix_nets(corpus_prefixes)
    domains = _fetch_federal_domains()
    log.info("Checking %d .gov domains against %d corpus prefixes", len(domains), len(nets))

    all_rows: list[tuple] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_process_domain, d, nets, obs_date): d for d in domains
        }
        for future in as_completed(futures):
            try:
                all_rows.extend(future.result())
            except Exception as exc:
                log.warning("Domain %s failed: %s", futures[future], exc)

    matches = sum(1 for r in all_rows if r[5] is not None)

    conn.execute(
        "DELETE FROM domain_services WHERE observation_date = ? AND source = 'dotgov_dns'",
        [obs_date],
    )
    if all_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO domain_services VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            all_rows,
        )

    conn.close()
    log.info(
        "dotgov DNS: %d domains, %d rows, %d corpus matches",
        len(domains), len(all_rows), matches,
    )
    return {"domains_checked": len(domains), "rows_inserted": len(all_rows), "matches": matches}
