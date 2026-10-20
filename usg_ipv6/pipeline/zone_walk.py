"""
DNS Zone-Walking enrichment pipeline
=====================================
Enumerates FQDNs in .gov zones using AXFR attempts, NSEC zone-walking,
and wordlist enumeration, then resolves AAAA records to discover active
IPv6 hosts on corpus networks.

Discovery pipeline per zone:
  1. AXFR attempt   — try a zone transfer; complete zone if permitted.
                      A permitted AXFR is itself a security finding.
  2. Zone classify  — probe for NSEC/NSEC3/unsigned via a nonexistent query.
  3. NSEC walk      — iterative NSEC-record chain walk for NSEC-signed zones;
                      yields every FQDN in the zone (complete enumeration).
  4. Wordlist enum  — for NSEC3 and unsigned zones, query ~120 federal-context
                      hostname labels (vpn, portal, mail, ns1, …).

Why zone-walking matters for IPv6 measurement:
  - Federal .gov domains are DNSSEC-signed by OMB M-08-23 mandate, so many
    use NSEC records that are inherently walking-vulnerable.
  - NSEC-signed zones that host AAAA records expose every configured hostname,
    including management interfaces, internal APIs, and VPN concentrators
    invisible to ICMP hitlists and prefix-offset Atlas sweeps.
  - draft-fbw-dnsop-dnszonehop identifies zone-walking as a primary IPv6
    address discovery vector for federal networks under OMB M-21-07.

Security note: zone-walking queries only publicly available DNS data.
  Authoritative nameservers publish NSEC records by design; walking them
  is equivalent to iterative public DNS lookups. AXFR uses the standard
  zone-transfer mechanism defined in RFC 5936; a server that refuses is
  not harmed.

Zone names and discovered FQDNs are stored in the database for analysis
but are not surfaced on the public dashboard. Only aggregate counts are
displayed.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as date_cls

import dns.flags
import dns.message
import dns.query
import dns.rdatatype
import dns.resolver
import dns.zone

from usg_ipv6.pipeline.dotgov_dns import (
    _build_prefix_nets,
    _fetch_federal_domains,
    _match_prefix,
)

log = logging.getLogger(__name__)

_NS_WORKERS   = 20   # concurrent NS-IP resolution threads
_WALK_WORKERS = 8    # concurrent zone-walk / wordlist threads
_AAAA_WORKERS = 30   # concurrent AAAA resolution threads
_MAX_NSEC_NAMES = 10_000  # hard cap per zone

# Federal-context hostname wordlist for NSEC3 and unsigned zones.
_WORDLIST: list[str] = [
    # DNS
    "ns", "ns1", "ns2", "ns3", "ns4", "dns", "resolver", "recursive",
    # Mail
    "mail", "mail1", "mail2", "mx", "mx1", "mx2", "smtp", "smtp1", "smtp2",
    "relay", "mailrelay", "owa", "exchange", "autodiscover", "imap", "pop",
    # Web / portal
    "www", "web", "portal", "intranet", "extranet", "www2", "web2",
    "services", "api", "apps", "app", "gateway",
    # Identity / SSO
    "sso", "adfs", "login", "auth", "idp", "saml", "sts", "cas", "ldap",
    "ad", "dc", "dc1", "dc2",
    # Remote access
    "vpn", "vpn1", "vpn2", "ras", "remote", "ssl-vpn", "citrix", "rdp",
    "jump", "jumphost", "bastion",
    # File transfer
    "ftp", "sftp", "files",
    # Infrastructure
    "ntp", "ntp1", "ntp2", "syslog", "radius", "tacacs", "dhcp",
    "proxy", "proxy1", "firewall", "fw", "fw1", "router", "switch",
    "gw", "mgmt", "management", "monitor",
    # Monitoring / ops
    "nagios", "zabbix", "cacti", "grafana", "snmp", "netflow",
    "siem", "splunk",
    # Dev / staging
    "dev", "staging", "test", "uat", "qa", "prod", "pre-prod",
    # Source control / collab
    "git", "svn", "jira", "confluence", "sharepoint", "gitlab",
    # Federal-specific
    "nipr", "sipr", "dts", "emss", "eauth", "hris", "ehr",
    "scanner", "admin", "support", "helpdesk", "servicedesk",
    # Misc
    "backup", "bkp", "archive", "secure", "internal", "corp",
    "dkim", "spf", "cert", "ocsp", "crl",
]


# ── Authoritative NS resolution ───────────────────────────────────────────────

def _get_ns_ips(domain: str) -> list[str]:
    """Return up to 2 IPv4 addresses of authoritative nameservers for domain."""
    ips: list[str] = []
    try:
        ns_names = [
            str(r.target).rstrip(".")
            for r in dns.resolver.resolve(domain, "NS", lifetime=10)
        ]
    except Exception:
        return []
    for ns in ns_names[:4]:
        try:
            for rdata in dns.resolver.resolve(ns, "A", lifetime=5):
                ips.append(str(rdata.address))
                break
        except Exception:
            pass
        if len(ips) >= 2:
            break
    return ips


# ── AXFR zone transfer ────────────────────────────────────────────────────────

def _attempt_axfr(domain: str, ns_ip: str) -> list[str]:
    """
    Attempt a zone transfer (AXFR) from ns_ip for domain.
    Returns list of FQDNs if successful, empty list on refusal or error.
    """
    try:
        xfr = dns.query.xfr(ns_ip, domain, timeout=15, lifetime=30)
        zone_obj = dns.zone.from_xfr(xfr)
        names: list[str] = []
        for node_name in zone_obj.nodes:
            label = str(node_name)
            fqdn = domain if label == "@" else f"{label}.{domain}"
            names.append(fqdn)
        return names
    except Exception:
        return []


# ── Zone classification ────────────────────────────────────────────────────────

def _classify_zone(domain: str, ns_ip: str) -> str:
    """
    Probe with a nonexistent query to determine DNSSEC record type.
    Returns 'nsec', 'nsec3', 'unsigned', or 'error'.
    """
    probe = f"zz9zz9-walk-probe.{domain}"
    try:
        req = dns.message.make_query(probe, dns.rdatatype.A, want_dnssec=True)
        resp = dns.query.udp(req, ns_ip, timeout=5)
        for section in (resp.answer, resp.authority, resp.additional):
            for rrset in section:
                if rrset.rdtype == dns.rdatatype.NSEC:
                    return "nsec"
                if rrset.rdtype == dns.rdatatype.NSEC3:
                    return "nsec3"
        # Signed but NSEC3 not surfaced in authority — check for DNSKEY
        req2 = dns.message.make_query(domain, dns.rdatatype.DNSKEY, want_dnssec=True)
        resp2 = dns.query.udp(req2, ns_ip, timeout=5)
        for rrset in resp2.answer:
            if rrset.rdtype == dns.rdatatype.DNSKEY:
                return "nsec3"
        return "unsigned"
    except Exception as exc:
        log.debug("classify_zone %s via %s: %s", domain, ns_ip, exc)
        return "error"


# ── NSEC iterative walk ────────────────────────────────────────────────────────

def _walk_nsec(domain: str, ns_ip: str) -> list[str]:
    """
    Iteratively walk NSEC records on the authoritative nameserver.
    Queries NSEC type at each known name; the NSEC record's next_name
    field gives the following name in the zone's canonical ordering.
    Returns list of all FQDNs discovered in the zone.
    """
    zone_lower = domain.lower()
    names: list[str] = []
    seen_lower: set[str] = set()

    current = domain + "."  # start at the apex

    for _ in range(_MAX_NSEC_NAMES):
        try:
            req = dns.message.make_query(current, dns.rdatatype.NSEC, want_dnssec=True)
            resp = dns.query.udp(req, ns_ip, timeout=5)

            next_name: str | None = None
            current_lower = current.rstrip(".").lower()

            for section in (resp.answer, resp.authority):
                if next_name:
                    break
                for rrset in section:
                    if rrset.rdtype != dns.rdatatype.NSEC:
                        continue
                    if str(rrset.name).rstrip(".").lower() == current_lower:
                        for rr in rrset:
                            next_name = str(rr.next).rstrip(".")
                            break
                        break

            if not next_name:
                break

            next_lower = next_name.lower()

            # Wrapped back to apex — walk complete
            if next_lower == zone_lower:
                break

            # Escaped the zone
            if not (next_lower == zone_lower or next_lower.endswith("." + zone_lower)):
                break

            # Duplicate — broken NSEC chain
            if next_lower in seen_lower:
                break

            seen_lower.add(next_lower)
            names.append(next_name)
            current = next_name + "."

        except Exception as exc:
            log.debug("NSEC walk step %s (ns=%s): %s", current, ns_ip, exc)
            break

    log.info("NSEC walk %s: %d names", domain, len(names))
    return names


# ── Wordlist enumeration ───────────────────────────────────────────────────────

def _query_wordlist(domain: str) -> list[str]:
    """
    Query each label in _WORDLIST against domain.
    Returns FQDNs that exist (any record type — we probe AAAA later).
    Used for NSEC3 and unsigned zones.
    """
    found: list[str] = []
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 4

    for label in _WORDLIST:
        fqdn = f"{label}.{domain}"
        try:
            # Probe with ANY to catch names that exist but have only A records
            resolver.resolve(fqdn, "AAAA")
            found.append(fqdn)
        except dns.resolver.NXDOMAIN:
            pass
        except dns.resolver.NoAnswer:
            # Name exists but no AAAA — still discovered, include for completeness
            found.append(fqdn)
        except Exception:
            pass

    return found


# ── AAAA resolution ────────────────────────────────────────────────────────────

def _resolve_aaaa(hostname: str) -> list[str]:
    try:
        answers = dns.resolver.resolve(hostname, "AAAA", lifetime=5)
        return [str(r.address) for r in answers]
    except Exception:
        return []


# ── Per-domain orchestration ───────────────────────────────────────────────────

def _process_domain(domain: str) -> dict:
    """
    Run the full zone-walk pipeline for one domain.
    Returns a dict with zone_type, axfr_permitted, names, discovery_method.
    """
    result: dict = {
        "domain": domain,
        "zone_type": "error",
        "axfr_permitted": False,
        "names": [],
        "discovery_method": "none",
    }

    ns_ips = _get_ns_ips(domain)
    if not ns_ips:
        return result

    ns_ip = ns_ips[0]

    # Step 1: AXFR attempt
    axfr_names = _attempt_axfr(domain, ns_ip)
    if axfr_names:
        result.update(
            axfr_permitted=True,
            zone_type="axfr",
            names=axfr_names,
            discovery_method="axfr",
        )
        return result

    # Step 2: Classify the zone
    zone_type = _classify_zone(domain, ns_ip)
    result["zone_type"] = zone_type

    # Step 3: Walk or wordlist based on type
    if zone_type == "nsec":
        result["names"] = _walk_nsec(domain, ns_ip)
        result["discovery_method"] = "nsec_walk"
    elif zone_type in ("nsec3", "unsigned"):
        result["names"] = _query_wordlist(domain)
        result["discovery_method"] = "wordlist"

    return result


# ── Main entry point ──────────────────────────────────────────────────────────

def run_zone_walk(
    db_path: str,
    scan_date: date_cls | None = None,
    domain_limit: int | None = None,
) -> dict:
    """
    Run the DNS zone-walk enrichment pipeline against all .gov domains.

    Zone names and discovered FQDNs are stored in zone_walk_meta and
    zone_walk_hosts. Only aggregate counts are returned and shown on the
    dashboard.

    Returns summary dict: domains_processed, axfr_zones, nsec_zones,
    nsec3_zones, unsigned_zones, error_zones, names_discovered,
    aaaa_resolved, corpus_matched, unique_corpus_addresses.
    """
    from usg_ipv6.db import get_connection, init_schema

    sd = scan_date or date_cls.today()
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
    if domain_limit:
        domains = domains[:domain_limit]

    log.info("Zone walk: %d .gov domains, %d walk workers, %d AAAA workers",
             len(domains), _WALK_WORKERS, _AAAA_WORKERS)

    # ── Phase 1+2: classify and walk all domains concurrently ─────────────────
    domain_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=_WALK_WORKERS) as pool:
        futures = {pool.submit(_process_domain, d): d for d in domains}
        for fut in as_completed(futures):
            try:
                domain_results.append(fut.result())
            except Exception as exc:
                log.warning("Zone walk failed for %s: %s", futures[fut], exc)

    # ── Phase 3: resolve AAAA for all discovered names concurrently ───────────
    discovery_items: list[tuple[str, str, str]] = [
        (dr["domain"], dr["discovery_method"], name)
        for dr in domain_results
        for name in dr["names"]
    ]

    log.info("Zone walk: resolving AAAA for %d FQDNs (%d workers)",
             len(discovery_items), _AAAA_WORKERS)

    host_rows: list[tuple] = []
    with ThreadPoolExecutor(max_workers=_AAAA_WORKERS) as pool:
        aaaa_futures = {
            pool.submit(_resolve_aaaa, fqdn): (domain, method, fqdn)
            for domain, method, fqdn in discovery_items
        }
        for fut in as_completed(aaaa_futures):
            domain, method, fqdn = aaaa_futures[fut]
            try:
                for addr in fut.result():
                    pfx, asn = _match_prefix(addr, nets)
                    host_rows.append((sd, domain, fqdn, addr, pfx, asn, method))
            except Exception:
                pass

    # ── Build per-domain counts for zone_walk_meta ────────────────────────────
    domain_name_count: dict[str, int] = {dr["domain"]: len(dr["names"]) for dr in domain_results}
    domain_aaaa_count: dict[str, int] = {}
    domain_corpus_count: dict[str, int] = {}
    for sd_, dom, fqdn, addr, pfx, asn, method in host_rows:
        domain_aaaa_count[dom] = domain_aaaa_count.get(dom, 0) + 1
        if pfx:
            domain_corpus_count[dom] = domain_corpus_count.get(dom, 0) + 1

    meta_rows: list[tuple] = [
        (
            sd,
            dr["domain"],
            dr["zone_type"],
            dr["axfr_permitted"],
            domain_name_count.get(dr["domain"], 0),
            domain_aaaa_count.get(dr["domain"], 0),
            domain_corpus_count.get(dr["domain"], 0),
        )
        for dr in domain_results
    ]

    # ── Persist ───────────────────────────────────────────────────────────────
    conn.execute("DELETE FROM zone_walk_meta  WHERE scan_date = ?", [sd])
    conn.execute("DELETE FROM zone_walk_hosts WHERE scan_date = ?", [sd])

    if meta_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO zone_walk_meta VALUES (?,?,?,?,?,?,?)",
            meta_rows,
        )
    if host_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO zone_walk_hosts VALUES (?,?,?,?,?,?,?)",
            host_rows,
        )
    conn.close()

    # ── Summary (no zone names, no FQDNs) ────────────────────────────────────
    type_counts = {}
    for dr in domain_results:
        type_counts[dr["zone_type"]] = type_counts.get(dr["zone_type"], 0) + 1

    unique_corpus_addrs = len({r[3] for r in host_rows if r[4] is not None})

    summary = {
        "scan_date":              sd.isoformat(),
        "domains_processed":      len(domain_results),
        "axfr_zones":             type_counts.get("axfr", 0),
        "nsec_zones":             type_counts.get("nsec", 0),
        "nsec3_zones":            type_counts.get("nsec3", 0),
        "unsigned_zones":         type_counts.get("unsigned", 0),
        "error_zones":            type_counts.get("error", 0),
        "names_discovered":       sum(domain_name_count.values()),
        "aaaa_resolved":          len(host_rows),
        "corpus_matched":         sum(domain_corpus_count.values()),
        "unique_corpus_addresses": unique_corpus_addrs,
    }
    log.info("Zone walk complete: %s", summary)
    return summary
