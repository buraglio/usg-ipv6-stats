import responses as resp
import pytest
from datetime import datetime
from usg_ipv6.pipeline.routeviews import rib_url, fetch_rib


def test_rib_url_format_route_views2_uses_root_path():
    dt = datetime(2024, 3, 15, 8, 0)
    url = rib_url("route-views2", dt)
    # route-views2 is the default collector — stored at the archive root, no subdirectory
    assert "archive.routeviews.org/bgpdata/" in url
    assert "/route-views2/" not in url
    assert "2024.03" in url
    assert "rib.20240315.0800" in url


def test_rib_url_format_named_collector_includes_collector():
    dt = datetime(2024, 3, 15, 8, 0)
    url = rib_url("route-views6", dt)
    assert "archive.routeviews.org/route-views6/bgpdata/" in url
    assert "2024.03" in url
    assert "rib.20240315.0800" in url


@resp.activate
def test_fetch_rib_downloads_file(tmp_path, db):
    url = rib_url("route-views2", datetime(2024, 3, 15, 8, 0))
    fake_data = b"MRT_FAKE_DATA" * 100
    resp.add(resp.GET, url, body=fake_data, status=200,
             headers={"Content-Length": str(len(fake_data))})
    path = fetch_rib(url, str(tmp_path), db, collector="route-views2")
    assert path is not None
    from pathlib import Path
    assert Path(path).exists()
    row = db.execute("SELECT status FROM snapshots WHERE source_url = ?", [url]).fetchone()
    assert row[0] == "downloaded"


@resp.activate
def test_partial_download_not_marked_done(tmp_path, db):
    url = rib_url("route-views2", datetime(2024, 3, 15, 10, 0))
    fake_data = b"PARTIAL" * 10
    resp.add(resp.GET, url, body=fake_data, status=200,
             headers={"Content-Length": "10000"})
    path = fetch_rib(url, str(tmp_path), db, collector="route-views2")
    assert path is None
    row = db.execute("SELECT status FROM snapshots WHERE source_url = ?", [url]).fetchone()
    assert row is None or row[0] == "failed"


@resp.activate
def test_fetch_rib_skips_already_processed(tmp_path, db):
    url = rib_url("route-views2", datetime(2024, 3, 15, 12, 0))
    db.execute(
        "INSERT INTO snapshots VALUES (?, 'route-views2', ?, 1000, 'processed', NOW())",
        [datetime(2024, 3, 15, 12, 0), url],
    )
    path = fetch_rib(url, str(tmp_path), db, collector="route-views2")
    assert path is None
    assert len(resp.calls) == 0
