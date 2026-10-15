"""
RPKI validity enrichment pipeline
====================================
For every IPv6 prefix currently in prefix_records, queries the Cloudflare
RPKI validity API to determine whether the origin AS is covered by a valid
Route Origin Authorization (ROA).

Signal value:
- VALID   → an explicit ROA authorizes this AS to announce this prefix.
            Strong signal of intentional, maintained operation.
- INVALID → a ROA exists but covers a different origin AS or max-length.
            Could indicate a route leak, hijack, or stale announcement.
- NOT_FOUND → no ROA covers this prefix at all. Common for older USG
              prefixes that have not yet deployed RPKI.

API: https://rpki.cloudflare.com/api/v1/validity/{asn}/{prefix}
No auth required.  Rate-limit politely (~5 req/s).

Fallback: if Cloudflare is unreachable, the pipeline attempts the RIPE
Stat RPKI validity API (data.stat.ripe.net).
"""

import ipaddress
import logging
import time
from datetime import date as date_cls

import requests

log = logging.getLogger(__name__)

CF_RPKI_URL = "https://rpki.cloudflare.com/api/v1/validity/{asn}/{prefix}"
RIPE_RPKI_URL = (
    "https://stat.ripe.net/data/rpki-validation/data.json?resource={asn}&prefix={prefix}"
)
REQUEST_DELAY = 0.2   # ~5 req/s
MAX_RETRIES = 2


def _check_cloudflare(asn: int, prefix: str) -> dict | None:
    url = CF_RPKI_URL.format(asn=asn, prefix=prefix)
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()
            vr = data.get("validity", {})
            return {
                "status": vr.get("state", "not-found").lower().replace(" ", "-"),
                "roa_max_length": (
                    vr.get("VRPs", {}).get("matched", [{}])[0].get("max_length")
                    if vr.get("VRPs", {}).get("matched") else None
                ),
            }
        except Exception as exc:
            if attempt < MAX_RETRIES - 1:
                time.sleep(1)
            else:
                log.debug("Cloudflare RPKI failed for %s %s: %s", asn, prefix, exc)
    return None


def _check_ripe(asn: int, prefix: str) -> dict | None:
    url = RIPE_RPKI_URL.format(asn=asn, prefix=prefix)
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        status = data.get("data", {}).get("status", "not-found").lower()
        return {"status": status, "roa_max_length": None}
    except Exception as exc:
        log.debug("RIPE RPKI failed for %s %s: %s", asn, prefix, exc)
        return None


def _check_validity(asn: int, prefix: str) -> dict:
    result = _check_cloudflare(asn, prefix)
    if result is None:
        result = _check_ripe(asn, prefix)
    if result is None:
        result = {"status": "error", "roa_max_length": None}
    return result


def run_rpki_validity(
    db_path: str,
    check_date: date_cls | None = None,
    af: int = 6,
) -> dict:
    """
    Check RPKI validity for all corpus-announced prefixes.

    af=6 (default) checks only IPv6; set af=4 for IPv4 or af=0 for both.
    Returns a summary dict with totals by status.
    """
    from usg_ipv6.db import get_connection, init_schema

    cdate = check_date or date_cls.today()

    conn = get_connection(db_path)
    init_schema(conn)

    af_filter = "" if af == 0 else f"AND pr.af = {af}"
    pairs = conn.execute(f"""
        SELECT DISTINCT pr.prefix, pr.origin_asn
        FROM prefix_records pr
        WHERE pr.origin_asn IN (SELECT asn FROM asn_corpus)
        {af_filter}
    """).fetchall()

    log.info("RPKI: checking %d prefix/origin-AS pairs", len(pairs))
    conn.execute("DELETE FROM rpki_validity WHERE check_date = ?", [cdate])

    rows = []
    counts: dict[str, int] = {}

    for prefix, asn in pairs:
        result = _check_validity(int(asn), prefix)
        status = result["status"]
        rows.append((cdate, prefix, int(asn), status, result["roa_max_length"]))
        counts[status] = counts.get(status, 0) + 1
        time.sleep(REQUEST_DELAY)

    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO rpki_validity VALUES (?,?,?,?,?)",
            rows,
        )

    conn.close()
    log.info("RPKI: %s", counts)
    return {"pairs_checked": len(pairs), "by_status": counts}
