"""
RPKI validity enrichment pipeline
====================================
Downloads the Cloudflare RPKI VRP dump (rpki.cloudflare.com/rpki.json)
once and evaluates every corpus prefix/origin-AS pair locally.

No per-prefix API calls, no rate-limit delays.  Typical runtime: <2 min
(~30 s download + index build + in-memory lookup for all pairs).

Signal meanings:
- valid      → an explicit ROA authorizes this AS for this prefix/length.
- invalid    → a ROA covers this prefix but for a different AS or max-length.
- not_found  → no ROA covers this prefix at all (common for older USG prefixes).
"""

import ipaddress
import logging
from datetime import date as date_cls

import requests

log = logging.getLogger(__name__)

CF_VRP_URL = "https://rpki.cloudflare.com/rpki.json"
# Fallback: OpenBSD rpki-client public export (same schema)
FALLBACK_VRP_URL = "https://console.rpki-client.org/vrps.json"


def _download_vrps() -> list[dict]:
    """Download the full VRP list from Cloudflare; fall back to rpki-client."""
    for url in (CF_VRP_URL, FALLBACK_VRP_URL):
        try:
            log.info("RPKI: downloading VRP dump from %s", url)
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            data = r.json()
            roas = data.get("roas", [])
            if roas:
                log.info("RPKI: got %d VRPs", len(roas))
                return roas
        except Exception as exc:
            log.warning("RPKI: download failed from %s: %s", url, exc)
    return []


def _build_index(roas: list[dict]) -> tuple[dict, dict]:
    """
    Build fast-lookup dicts for supernet matching.

    Index key:  (masked_network_address_int, prefix_len)
    Index value: list of (origin_asn, max_length)

    For a corpus prefix C/clen, the covering VRPs are found by iterating
    vlen = 0..clen, masking C's address to vlen bits, and looking up
    (masked, vlen) in the index.  O(prefix_len) dict lookups per pair.
    """
    v4: dict = {}
    v6: dict = {}

    for roa in roas:
        try:
            net = ipaddress.ip_network(roa["prefix"], strict=False)
            asn = int(str(roa["asn"]).lstrip("ASas"))
            max_len = int(roa["maxLength"])
        except Exception:
            continue

        plen = net.prefixlen
        p_int = int(net.network_address)
        bucket = v4 if isinstance(net, ipaddress.IPv4Network) else v6
        bucket.setdefault((p_int, plen), []).append((asn, max_len))

    return v4, v6


def _classify(prefix: str, asn: int, v4_idx: dict, v6_idx: dict) -> tuple[str, int | None]:
    """
    Return (status, roa_max_length) for a prefix/origin-AS pair per RFC 6811.
    No network calls.

    - valid:     a VRP authorizes this ASN for this prefix/length.
    - invalid:   a VRP covers this address space but not this ASN/length.
    - not_found: no VRP covers this address space at all.
    """
    try:
        net = ipaddress.ip_network(prefix, strict=False)
    except ValueError:
        return "not_found", None

    bits = 128 if isinstance(net, ipaddress.IPv6Network) else 32
    idx = v6_idx if bits == 128 else v4_idx
    c_int = int(net.network_address)
    c_len = net.prefixlen

    # covering_any: VRPs whose prefix covers our address space (maxLength may fail)
    # covering_ok:  VRPs that fully authorize this announcement
    covering_any: list[tuple[int, int]] = []
    covering_ok: list[tuple[int, int]] = []

    for vlen in range(c_len + 1):
        mask = ((1 << bits) - 1) ^ ((1 << (bits - vlen)) - 1)
        masked = c_int & mask
        for vasn, max_len in idx.get((masked, vlen), []):
            covering_any.append((vasn, max_len))
            if c_len <= max_len:
                covering_ok.append((vasn, max_len))

    if not covering_any:
        return "not_found", None
    valid = [(vasn, ml) for vasn, ml in covering_ok if vasn == asn]
    if valid:
        return "valid", valid[0][1]
    return "invalid", None


def run_rpki_validity(
    db_path: str,
    check_date: date_cls | None = None,
    af: int = 6,
) -> dict:
    """
    Classify RPKI validity for all corpus-announced prefixes.

    Downloads the Cloudflare VRP dump once and evaluates pairs locally.
    af=6 (default) checks IPv6 only; af=4 for IPv4; af=0 for both.
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

    log.info("RPKI: evaluating %d prefix/origin-AS pairs (af=%s)", len(pairs), af)

    roas = _download_vrps()
    if not roas:
        conn.close()
        return {"pairs_checked": 0, "by_status": {}, "error": "VRP download failed"}

    v4_idx, v6_idx = _build_index(roas)
    log.info("RPKI: index built — %d v4 entries, %d v6 entries", len(v4_idx), len(v6_idx))

    conn.execute("DELETE FROM rpki_validity WHERE check_date = ?", [cdate])

    rows = []
    counts: dict[str, int] = {}

    for prefix, asn in pairs:
        status, roa_max_len = _classify(prefix, int(asn), v4_idx, v6_idx)
        rows.append((cdate, prefix, int(asn), status, roa_max_len))
        counts[status] = counts.get(status, 0) + 1

    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO rpki_validity VALUES (?,?,?,?,?)",
            rows,
        )

    conn.close()
    log.info("RPKI: %s", counts)
    return {"pairs_checked": len(pairs), "by_status": counts}
