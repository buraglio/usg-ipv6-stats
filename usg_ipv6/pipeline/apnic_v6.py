"""
APNIC IPv6 Client Capability Measurement
=========================================
Fetches per-ASN daily IPv6 client capability data from APNIC's ad-based
measurement system (stats.labs.apnic.net) and stores it in apnic_v6_stats.

APNIC methodology: a small image is served with two URLs — one IPv4-only and
one IPv6-only. The fraction of users who successfully fetch the IPv6-only
image is "capable"; the fraction who prefer IPv6 (happy eyeballs winner) is
"preferred". This measures client/user IPv6 capability, not server-side
infrastructure deployment.

Applicability note: government ASNs are infrastructure operators, not consumer
ISPs. Most corpus ASNs have fewer than 3 measured client sessions per day
(seen_30 < 3), making per-ASN percentages statistically marginal. Data is
stored for all corpus ASNs where APNIC has records; a minimum seen_30 ≥ 5
threshold is recommended before interpreting capability percentages.

Data cadence: APNIC updates nightly (~00:00 UTC). Incremental runs (default)
fetch only routing-present corpus ASNs and insert records newer than the last
stored date. Backfill runs load the full available history (back to ~2013)
for all corpus ASNs.
"""

import argparse
import json
import logging
import sys
import time
import urllib.request
from typing import Optional

from usg_ipv6.db import get_connection, init_schema

log = logging.getLogger(__name__)

_APNIC_URL = "https://stats.labs.apnic.net/cgi-bin/json-table-v6.pl?x=US{asn}"
_DELAY = 0.1   # seconds between requests — stay polite
_TIMEOUT = 15  # seconds per HTTP request


def _fetch(asn: int) -> list[dict]:
    """Return APNIC daily records for one ASN, or [] on error."""
    url = _APNIC_URL.format(asn=asn)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "usg-ipv6-research/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = resp.read()
        return json.loads(body).get("data", [])
    except Exception as exc:
        log.warning("APNIC fetch failed AS%d: %s", asn, exc)
        return []


def run_apnic_v6(
    db_path: str,
    backfill: bool = False,
    asns: Optional[list[int]] = None,
) -> dict:
    """
    Fetch APNIC IPv6 client capability data for corpus ASNs and store results.

    Parameters
    ----------
    db_path:
        Path to the DuckDB database file.
    backfill:
        When True, target all corpus ASNs and load their full APNIC history.
        When False (default), target only routing-present ASNs and insert
        only records newer than what is already stored.
    asns:
        Explicit list of ASNs to process; overrides backfill selection logic.

    Returns
    -------
    dict with summary counts suitable for JSON logging.
    """
    conn = get_connection(db_path)
    init_schema(conn)

    # ── Determine target ASNs ─────────────────────────────────────────────────
    if asns is not None:
        target_asns = list(asns)
    elif backfill:
        target_asns = [
            r[0]
            for r in conn.execute(
                "SELECT asn FROM asn_corpus ORDER BY asn"
            ).fetchall()
        ]
    else:
        # Routing-present ASNs from the latest CAIDA pfx2as snapshot
        target_asns = [
            r[0]
            for r in conn.execute("""
                SELECT DISTINCT c.asn
                FROM caida_pfx2as p
                JOIN asn_corpus c ON c.asn = p.asn
                WHERE p.af = 6
                  AND p.snapshot_date = (
                      SELECT MAX(snapshot_date) FROM caida_pfx2as WHERE af = 6
                  )
                ORDER BY c.asn
            """).fetchall()
        ]

    log.info(
        "APNIC enrichment: %d ASNs to fetch (backfill=%s)", len(target_asns), backfill
    )

    # ── Max stored date per ASN (skip already-known records) ──────────────────
    max_dates: dict[int, str] = {
        int(asn): str(d)
        for asn, d in conn.execute(
            "SELECT asn, MAX(observation_date) FROM apnic_v6_stats GROUP BY asn"
        ).fetchall()
    }

    total_seen = 0
    total_inserted = 0
    asns_with_data = 0
    asns_no_data = 0

    for asn in target_asns:
        records = _fetch(asn)
        time.sleep(_DELAY)

        if not records:
            asns_no_data += 1
            continue

        asns_with_data += 1
        cutoff = max_dates.get(asn)  # None on first run → insert everything

        rows = []
        for rec in records:
            obs_date = rec.get("date")
            if not obs_date:
                continue
            if cutoff is not None and obs_date <= cutoff:
                continue  # already stored
            raw = rec.get("raw", {})
            rows.append((
                asn,
                obs_date,
                int(raw.get("seen", 0)),
                int(raw.get("capable", 0)),
                float(raw.get("capable_pc", 0.0)),
                int(raw.get("preferred", 0)),
                float(raw.get("preferred_pc", 0.0)),
            ))

        total_seen += len(records)

        if rows:
            conn.executemany("""
                INSERT OR IGNORE INTO apnic_v6_stats
                    (asn, observation_date, seen, capable, capable_pc,
                     preferred, preferred_pc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, rows)
            total_inserted += len(rows)
            log.debug("AS%d: +%d records", asn, len(rows))

    conn.close()

    return {
        "asns_targeted": len(target_asns),
        "asns_with_data": asns_with_data,
        "asns_no_data": asns_no_data,
        "records_seen": total_seen,
        "records_inserted": total_inserted,
        "backfill": backfill,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="APNIC IPv6 client capability enrichment")
    parser.add_argument("--db", default="data/usg_ipv6.duckdb", help="DuckDB path")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Load full history for all corpus ASNs (slow; run once)",
    )
    args = parser.parse_args()

    result = run_apnic_v6(db_path=args.db, backfill=args.backfill)
    import json as _json
    print(_json.dumps(result, indent=2))
    sys.exit(0)
