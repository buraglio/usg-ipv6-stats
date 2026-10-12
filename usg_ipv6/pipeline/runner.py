import tempfile
from datetime import datetime, timedelta, timezone

from usg_ipv6.db import get_connection, init_schema
from usg_ipv6.pipeline.routeviews import fetch_rib, rib_url
from usg_ipv6.pipeline.parse import parse_mrt
from usg_ipv6.pipeline.normalize import load_parquet


def _candidate_datetimes(since: datetime, until: datetime) -> list[datetime]:
    """Generate 2-hour RIB timestamps (0000, 0200, …, 2200) between since and until."""
    candidates = []
    current = since.replace(hour=0, minute=0, second=0, microsecond=0)
    while current <= until:
        for hour in range(0, 24, 2):
            dt = current.replace(hour=hour)
            if since <= dt <= until:
                candidates.append(dt)
        current += timedelta(days=1)
    return sorted(candidates)


def run_incremental(
    db_path: str,
    collector: str,
    *,
    limit: int = 1,
) -> dict:
    """Fetch and process up to `limit` recent RIB snapshots for a collector."""
    conn = get_connection(db_path)
    init_schema(conn)
    processed = 0
    # Walk backwards from now, most recent first
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    candidates = list(reversed(_candidate_datetimes(now - timedelta(days=7), now)))

    with tempfile.TemporaryDirectory() as tmp_dir:
        for dt in candidates:
            if processed >= limit:
                break
            url = rib_url(collector, dt)
            mrt_path = fetch_rib(url, tmp_dir, conn, collector=collector)
            if mrt_path is None:
                continue
            parquet_path = mrt_path + ".parquet"
            parse_mrt(mrt_path, parquet_path)
            snapshot_ts = dt.replace(second=0, microsecond=0)
            load_parquet(parquet_path, snapshot_ts, collector, conn)
            processed += 1

    conn.close()
    return {"processed": processed}


def run_backfill(
    db_path: str,
    collector: str,
    since: datetime,
    until: datetime,
) -> dict:
    """Fetch and process all RIB snapshots for a collector in [since, until]."""
    conn = get_connection(db_path)
    init_schema(conn)
    processed = 0
    candidates = _candidate_datetimes(since, until)

    with tempfile.TemporaryDirectory() as tmp_dir:
        for dt in candidates:
            url = rib_url(collector, dt)
            mrt_path = fetch_rib(url, tmp_dir, conn, collector=collector)
            if mrt_path is None:
                continue
            parquet_path = mrt_path + ".parquet"
            parse_mrt(mrt_path, parquet_path)
            snapshot_ts = dt.replace(second=0, microsecond=0)
            load_parquet(parquet_path, snapshot_ts, collector, conn)
            processed += 1

    conn.close()
    return {"processed": processed}
