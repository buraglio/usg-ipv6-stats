"""
NIST USGv6 Deployment Monitor scraper
=======================================
Scrapes https://usgv6-deploymon.nist.gov/cgi-bin/generate-gov and parses
the per-.gov-domain IPv6 deployment metrics for DNS, mail, and web services,
plus DNSSEC status.  Results are stored in the nist_deploymon table.

For each domain the NIST page reports three service blocks in the form:
    [n] a/b/c [STATUS]
where n = server count, a = servers with AAAA, b = IPv6-reachable,
c = offering the service over IPv6, STATUS ∈ {O, I, M, L}.

DNSSEC is represented as a three-letter code S/V/C (Signed / Valid / Chained)
or a dash for each dimension.

After parsing the NIST table this module also resolves the AAAA addresses of
each measured host and checks them against corpus prefixes, appending results
to domain_services with source='nist'.
"""

import ipaddress
import logging
import re
from datetime import date as date_cls

import requests

from usg_ipv6.pipeline.dotgov_dns import _build_prefix_nets, _match_prefix, _resolve_aaaa

log = logging.getLogger(__name__)

NIST_URL = "https://usgv6-deploymon.nist.gov/cgi-bin/generate-gov"

# Matches: domain cell, org cell, then DNS / Mail / Web blocks, percentage, DNSSEC
_ROW_RE = re.compile(
    r'<tr[^>]*>.*?'
    r'<td[^>]*><a[^>]*>([^<]+)</a></td>'    # domain
    r'.*?<td[^>]*>([^<]*)</td>'              # org
    r'(.*?)</tr>',
    re.S,
)
_BLOCK_RE = re.compile(
    r'\[(\d+)\]\s+(\d+)/(\d+)/(\d+)\s+\[([OIML\-])\]'
)
_DNSSEC_RE = re.compile(r'\b([S\-])/([V\-])/([C\-])\b')


def _parse_page(html: str) -> list[dict]:
    """
    Parse the NIST USGv6 HTML page into a list of per-domain records.
    Each record has: domain, org, dns/mail/web dicts, dnssec dict.
    """
    records = []
    # Find all table rows that contain a domain link
    for row_m in re.finditer(
        r'<tr[^>]*>(.*?)</tr>', html, re.S
    ):
        row_html = row_m.group(1)

        # Extract domain from first anchor
        domain_m = re.search(r'<a[^>]*href=["\'][^"\']*["\']>([^<]+)</a>', row_html)
        if not domain_m:
            continue
        domain = domain_m.group(1).strip().lower().rstrip(".")
        if not domain.endswith(".gov"):
            continue

        # Extract org from second td
        tds = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.S)
        org = re.sub(r'<[^>]+>', '', tds[1]).strip() if len(tds) > 1 else ""

        # Extract up to 3 metric blocks (DNS, Mail, Web in order)
        blocks = _BLOCK_RE.findall(row_html)
        service_data = {}
        for i, service in enumerate(("dns", "mail", "web")):
            if i < len(blocks):
                n, a, b, c, status = blocks[i]
                service_data[service] = {
                    "server_count": int(n),
                    "v6_addr_count": int(a),
                    "v6_reach_count": int(b),
                    "v6_func_count": int(c),
                    "status": status if status != "-" else None,
                }
            else:
                service_data[service] = None

        # DNSSEC
        dnssec_m = _DNSSEC_RE.search(row_html)
        dnssec = {
            "signed": dnssec_m.group(1) == "S" if dnssec_m else None,
            "valid": dnssec_m.group(2) == "V" if dnssec_m else None,
            "chained": dnssec_m.group(3) == "C" if dnssec_m else None,
        }

        records.append({"domain": domain, "org": org, "services": service_data, "dnssec": dnssec})

    return records


def run_nist_deploymon(db_path: str, measurement_date: date_cls | None = None) -> dict:
    """
    Scrape the NIST USGv6 page and store results.

    Returns a summary dict with domains_parsed and rows_inserted.
    """
    from usg_ipv6.db import get_connection, init_schema

    meas_date = measurement_date or date_cls.today()

    r = requests.get(NIST_URL, timeout=60)
    r.raise_for_status()
    records = _parse_page(r.text)
    log.info("NIST: parsed %d domain records", len(records))

    conn = get_connection(db_path)
    init_schema(conn)

    corpus_prefixes = conn.execute(
        "SELECT DISTINCT prefix, origin_asn FROM prefix_records WHERE af = 6"
    ).fetchall()
    nets = _build_prefix_nets(corpus_prefixes)

    # Clear old data for this date
    conn.execute("DELETE FROM nist_deploymon WHERE measurement_date = ?", [meas_date])
    conn.execute(
        "DELETE FROM domain_services WHERE observation_date = ? AND source = 'nist'",
        [meas_date],
    )

    nist_rows = []
    svc_rows = []

    for rec in records:
        domain = rec["domain"]
        org = rec["org"]
        dnssec = rec["dnssec"]
        for service, sdata in rec["services"].items():
            if sdata is None:
                continue
            nist_rows.append((
                meas_date, domain, org, service,
                sdata["server_count"], sdata["v6_addr_count"],
                sdata["v6_reach_count"], sdata["v6_func_count"],
                sdata["status"],
                dnssec["signed"], dnssec["valid"], dnssec["chained"],
            ))

        # For each service with v6 addresses, attempt to resolve the actual hosts
        # via NS / MX / A records and intersect with corpus prefixes
        rec_type_map = {"dns": "ns", "mail": "mx", "web": "web"}
        for service, sdata in rec["services"].items():
            if not sdata or sdata["v6_addr_count"] == 0:
                continue
            rec_type = rec_type_map[service]
            # Re-resolve the hosts for this service
            resolve_targets: list[str] = []
            if service == "dns":
                try:
                    import dns.resolver
                    ans = dns.resolver.resolve(domain, "NS", lifetime=5)
                    resolve_targets = [str(r.target).rstrip(".") for r in ans]
                except Exception:
                    pass
            elif service == "mail":
                try:
                    import dns.resolver
                    ans = dns.resolver.resolve(domain, "MX", lifetime=5)
                    resolve_targets = [str(r.exchange).rstrip(".") for r in ans]
                except Exception:
                    pass
            elif service == "web":
                resolve_targets = [domain, f"www.{domain}"]

            for hostname in resolve_targets:
                for addr in _resolve_aaaa(hostname):
                    pfx, asn = _match_prefix(addr, nets)
                    svc_rows.append((
                        meas_date, domain, rec_type, hostname, addr, pfx, asn, "nist"
                    ))

    if nist_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO nist_deploymon VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            nist_rows,
        )
    if svc_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO domain_services VALUES (?,?,?,?,?,?,?,?)",
            svc_rows,
        )

    conn.close()
    matches = sum(1 for r in svc_rows if r[5] is not None)
    log.info("NIST: %d metric rows, %d service rows, %d corpus matches",
             len(nist_rows), len(svc_rows), matches)
    return {
        "domains_parsed": len(records),
        "metric_rows": len(nist_rows),
        "service_rows": len(svc_rows),
        "matches": matches,
    }
