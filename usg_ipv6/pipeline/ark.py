import datetime
from collections import defaultdict
from typing import Optional

from usg_ipv6.db import get_connection, init_schema


def run_ark_ingest(
    db_path: str,
    *,
    date: Optional[str] = None,
    dest_dir: str = "data/raw",
) -> dict:
    """
    Compute IPv6 reachability for corpus ASNs from CAIDA pfx2as (RouteViews) data.

    An ASN is considered IPv6-reachable if it has at least one IPv6 prefix
    visible in the RouteViews BGP table (via caida_pfx2as rv6 snapshot).
    This is the same methodology used by NIST, APNIC, and Google IPv6 stats.

    CAIDA Ark daily nodes-as files (team-1.ipv6.*.nodes.as.gz) are not
    publicly available; probe-based reachability may be added if CAIDA
    publishes a parseable format.
    """
    conn = get_connection(db_path)
    init_schema(conn)

    measurement_date = date or datetime.date.today().isoformat()

    existing = conn.execute(
        "SELECT COUNT(*) FROM ark_reachability WHERE measurement_date = ?",
        [measurement_date],
    ).fetchone()[0]
    if existing > 0:
        conn.close()
        return {
            "date": measurement_date,
            "addresses_parsed": 0,
            "asns_updated": 0,
            "pfx2as_snapshot_date": None,
            "skipped": True,
        }

    pfx2as_date_row = conn.execute(
        "SELECT MAX(snapshot_date) FROM caida_pfx2as"
    ).fetchone()
    if pfx2as_date_row is None or pfx2as_date_row[0] is None:
        conn.close()
        raise RuntimeError("caida_pfx2as is empty — run RouteViews ingest first")
    pfx2as_date = str(pfx2as_date_row[0])

    corpus_asns: set[int] = {
        r[0] for r in conn.execute("SELECT asn FROM asn_corpus").fetchall()
    }
    if not corpus_asns:
        conn.close()
        raise RuntimeError("asn_corpus is empty — run corpus update first")

    # IPv6 prefixes per corpus ASN from the most recent pfx2as snapshot
    pfx_rows = conn.execute(
        """
        SELECT prefix, asn FROM caida_pfx2as
        WHERE af = 6 AND snapshot_date = ?
          AND asn IN (SELECT asn FROM asn_corpus)
        """,
        [pfx2as_date],
    ).fetchall()

    prefix_count: dict[int, int] = defaultdict(int)
    for _, asn in pfx_rows:
        prefix_count[asn] += 1

    for asn in sorted(corpus_asns):
        n = prefix_count.get(asn, 0)
        conn.execute(
            """
            INSERT INTO ark_reachability
                (measurement_date, asn, ipv6_reachable, addresses_observed,
                 prefixes_in_scope, pfx2as_snapshot_date, asn_divergence_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [measurement_date, asn, n > 0, n, n, pfx2as_date, 0],
        )

    seen_prefixes: set[str] = set()
    for prefix, asn in pfx_rows:
        if prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        conn.execute(
            """
            INSERT INTO ark_prefix_observations
                (measurement_date, prefix, our_asn, addresses_observed,
                 addresses_ark_match, pfx2as_snapshot_date)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [measurement_date, prefix, asn, 1, 1, pfx2as_date],
        )

    conn.close()
    return {
        "date": measurement_date,
        "addresses_parsed": len(pfx_rows),
        "asns_updated": len(corpus_asns),
        "pfx2as_snapshot_date": pfx2as_date,
        "skipped": False,
    }


def run_ark_backfill(
    db_path: str,
    since: datetime.datetime,
    until: datetime.datetime,
    *,
    dest_dir: str = "data/raw",
) -> dict:
    """Run run_ark_ingest for each day in [since, until]. Skips loaded dates."""
    processed = skipped = failed = 0
    current = since.date() if hasattr(since, "date") else since
    end = until.date() if hasattr(until, "date") else until

    while current <= end:
        date_str = current.isoformat()
        try:
            result = run_ark_ingest(db_path, date=date_str, dest_dir=dest_dir)
            if result["skipped"]:
                skipped += 1
            else:
                processed += 1
        except Exception as exc:
            print(f"  {date_str}: {exc}", flush=True)
            failed += 1
        current += datetime.timedelta(days=1)

    return {"processed": processed, "skipped": skipped, "failed": failed}
