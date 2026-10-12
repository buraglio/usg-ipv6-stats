import requests
import duckdb
from datetime import datetime
from pathlib import Path

# route-views2 (the original collector) lives at the archive root.
# All other collectors have their name as a path prefix.
_RV_BASE_DEFAULT = "http://archive.routeviews.org/bgpdata/{year_month}/RIBS"
_RV_BASE_NAMED   = "http://archive.routeviews.org/{collector}/bgpdata/{year_month}/RIBS"


def rib_url(collector: str, dt: datetime) -> str:
    year_month = dt.strftime("%Y.%m")
    filename = f"rib.{dt.strftime('%Y%m%d.%H%M')}.bz2"
    if collector == "route-views2":
        base = _RV_BASE_DEFAULT.format(year_month=year_month)
    else:
        base = _RV_BASE_NAMED.format(collector=collector, year_month=year_month)
    return f"{base}/{filename}"


def _snapshot_ts(url: str) -> datetime:
    name = url.split("/")[-1]  # rib.20240315.0800.bz2
    parts = name.split(".")
    return datetime.strptime(f"{parts[1]}.{parts[2]}", "%Y%m%d.%H%M")


def fetch_rib(
    url: str, dest_dir: str, conn: duckdb.DuckDBPyConnection, *, collector: str
) -> str | None:
    snapshot_ts = _snapshot_ts(url)

    existing = conn.execute(
        "SELECT status FROM snapshots WHERE source_url = ?", [url]
    ).fetchone()
    if existing and existing[0] == "processed":
        return None

    dest = Path(dest_dir) / url.split("/")[-1]
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            expected = int(r.headers.get("Content-Length", 0))
            received = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
                    received += len(chunk)

        if expected > 0 and received < expected:
            conn.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, NULL)",
                [snapshot_ts, collector, url, received, "failed"],
            )
            dest.unlink(missing_ok=True)
            return None

        conn.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, NULL)",
            [snapshot_ts, collector, url, received, "downloaded"],
        )
        return str(dest)
    except Exception:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, NULL)",
            [snapshot_ts, collector, url, 0, "failed"],
        )
        return None
