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

import dns.flags
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
    domains = []
    for row in reader:
        # Normalise keys to lowercase so "Domain name", "Domain Name", etc. all match
        lrow = {k.lower(): v for k, v in row.items()}
        d = lrow.get("domain name", "").strip().lower()
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


# Public resolvers that reliably return DNSSEC record types.  The runner's
# system resolver (Azure DNS, systemd-resolved stub, etc.) often silently
# drops DS/DNSKEY queries or doesn't set the AD bit.
_DNSSEC_NAMESERVERS = ["8.8.8.8", "1.1.1.1"]


def _dnssec_resolver() -> dns.resolver.Resolver:
    """Return a resolver configured for DNSSEC queries."""
    r = dns.resolver.Resolver(configure=False)
    r.nameservers = _DNSSEC_NAMESERVERS
    r.use_edns(0, dns.flags.DO, 4096)
    r.lifetime = DNS_TIMEOUT
    return r


def _check_dnssec(domain: str) -> dict:
    """
    Check DNSSEC status for a domain per RFC 4035.

    Returns dict with: has_ds, has_dnskey, ad_validated, algorithms.
    - has_ds:       DS record exists at the parent zone (delegation signed)
    - has_dnskey:   DNSKEY record exists at the zone apex
    - ad_validated: Authenticated Data flag set — resolver confirmed chain
    - algorithms:   comma-separated DNSKEY algorithm numbers (e.g. "13,8")
    """
    result: dict = {
        "has_ds": False,
        "has_dnskey": False,
        "ad_validated": False,
        "algorithms": None,
    }
    resolver = _dnssec_resolver()

    # DS at parent — confirms delegation is signed
    try:
        ans = resolver.resolve(domain, "DS")
        result["has_ds"] = len(list(ans)) > 0
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        pass
    except Exception as exc:
        log.debug("DNSSEC DS %s: %s", domain, exc)

    # DNSKEY at apex — zone is self-signed
    try:
        ans = resolver.resolve(domain, "DNSKEY")
        keys = list(ans)
        result["has_dnskey"] = len(keys) > 0
        if keys:
            algos = sorted({str(rr.algorithm) for rr in keys})
            result["algorithms"] = ",".join(algos)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        pass
    except Exception as exc:
        log.debug("DNSSEC DNSKEY %s: %s", domain, exc)

    # AD bit — validating resolver confirmed the full chain
    try:
        ans = resolver.resolve(domain, "SOA")
        result["ad_validated"] = bool(ans.response.flags & dns.flags.AD)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        pass
    except Exception as exc:
        log.debug("DNSSEC SOA/AD %s: %s", domain, exc)

    return result


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
    dnssec_results: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        svc_futures = {
            pool.submit(_process_domain, d, nets, obs_date): d for d in domains
        }
        dnssec_futures = {
            pool.submit(_check_dnssec, d): d for d in domains
        }
        for future in as_completed(svc_futures):
            try:
                all_rows.extend(future.result())
            except Exception as exc:
                log.warning("Domain %s svc failed: %s", svc_futures[future], exc)
        for future in as_completed(dnssec_futures):
            domain = dnssec_futures[future]
            try:
                dnssec_results[domain] = future.result()
            except Exception as exc:
                log.warning("Domain %s dnssec failed: %s", domain, exc)

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

    conn.execute(
        "DELETE FROM dnssec_status WHERE check_date = ?",
        [obs_date],
    )
    dnssec_rows = [
        (
            obs_date,
            domain,
            info.get("has_ds"),
            info.get("has_dnskey"),
            info.get("ad_validated"),
            info.get("algorithms"),
        )
        for domain, info in dnssec_results.items()
    ]
    if dnssec_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO dnssec_status VALUES (?, ?, ?, ?, ?, ?)",
            dnssec_rows,
        )

    signed = sum(1 for r in dnssec_rows if r[3])   # has_dnskey
    validated = sum(1 for r in dnssec_rows if r[4]) # ad_validated

    conn.close()
    log.info(
        "dotgov DNS: %d domains, %d svc rows, %d corpus matches; DNSSEC: %d signed, %d AD-validated",
        len(domains), len(all_rows), matches, signed, validated,
    )
    return {
        "domains_checked": len(domains),
        "rows_inserted": len(all_rows),
        "matches": matches,
        "dnssec_signed": signed,
        "dnssec_ad_validated": validated,
    }
