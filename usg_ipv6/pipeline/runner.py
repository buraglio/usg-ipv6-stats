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


def _sampled_datetimes(since: datetime, until: datetime, sample: str) -> list[datetime]:
    """Generate checkpoint datetimes for a given sample cadence.

    sample values:
      "quarterly" — Jan 1, Apr 1, Jul 1, Oct 1 of each year
      "monthly"   — 1st of each month
      "annual"    — Jan 1 of each year
      "all"       — every 2-hour RouteViews slot (same as _candidate_datetimes)
    """
    if sample == "all":
        return _candidate_datetimes(since, until)

    candidates: list[datetime] = []

    if sample == "quarterly":
        quarter_starts = [(1, 1), (4, 1), (7, 1), (10, 1)]
        year = since.year
        while True:
            for month, day in quarter_starts:
                dt = datetime(year, month, day, 0, 0, 0)
                if dt < since:
                    continue
                if dt > until:
                    return sorted(candidates)
                candidates.append(dt)
            year += 1

    elif sample == "monthly":
        year, month = since.year, since.month
        while True:
            dt = datetime(year, month, 1, 0, 0, 0)
            if dt > until:
                break
            if dt >= since:
                candidates.append(dt)
            month += 1
            if month > 12:
                month, year = 1, year + 1

    elif sample == "annual":
        year = since.year
        while True:
            dt = datetime(year, 1, 1, 0, 0, 0)
            if dt > until:
                break
            if dt >= since:
                candidates.append(dt)
            year += 1

    else:
        raise ValueError(f"Unknown sample value: {sample!r}. Use 'all', 'quarterly', 'monthly', or 'annual'.")

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
    sample: str = "all",
) -> dict:
    """Fetch and process RIB snapshots for a collector in [since, until].

    sample controls which timestamps are attempted:
      "all"       — every 2-hour RouteViews slot (original behaviour)
      "quarterly" — Jan 1, Apr 1, Jul 1, Oct 1 of each year
      "monthly"   — 1st of each month at midnight
      "annual"    — Jan 1 of each year at midnight
    """
    conn = get_connection(db_path)
    init_schema(conn)
    processed = 0
    candidates = _sampled_datetimes(since, until, sample)

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
