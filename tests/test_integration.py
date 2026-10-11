from unittest.mock import patch
from datetime import datetime
from usg_ipv6.pipeline.runner import run_incremental, _candidate_datetimes
from usg_ipv6.db import get_connection, init_schema


def test_candidate_datetimes_generates_two_hourly_intervals():
    since = datetime(2024, 3, 15, 0, 0, 0)
    until = datetime(2024, 3, 15, 23, 59, 59)
    result = _candidate_datetimes(since, until)
    assert len(result) == 12
    hours = [dt.hour for dt in result]
    assert hours == list(range(0, 24, 2))


@patch("usg_ipv6.pipeline.runner.fetch_rib")
@patch("usg_ipv6.pipeline.runner.parse_mrt")
@patch("usg_ipv6.pipeline.runner.load_parquet")
def test_run_incremental_initializes_schema_on_fresh_db(mock_load, mock_parse, mock_fetch, tmp_path):
    """run_incremental must create schema on a fresh DB so fetch_rib can write."""
    db_path = str(tmp_path / "fresh.duckdb")
    mock_fetch.return_value = None
    run_incremental(db_path, "route-views2", limit=1)
    conn = get_connection(db_path)
    tables = {t[0] for t in conn.execute("SHOW TABLES").fetchall()}
    conn.close()
    assert "snapshots" in tables


@patch("usg_ipv6.pipeline.runner.fetch_rib")
@patch("usg_ipv6.pipeline.runner.parse_mrt")
@patch("usg_ipv6.pipeline.runner.load_parquet")
def test_run_incremental_end_to_end(mock_load, mock_parse, mock_fetch, tmp_path):
    db_path = str(tmp_path / "test.duckdb")
    conn = get_connection(db_path)
    init_schema(conn)
    conn.execute("""
        INSERT INTO asn_corpus VALUES
        (396260,'GSA','GSA','cfo_act','USGSA','high','arin','2026-10-11','2026-10-11')
    """)
    conn.close()

    fake_path = str(tmp_path / "rib.20240315.0800.bz2")
    open(fake_path, "wb").close()
    mock_fetch.return_value = fake_path
    mock_parse.return_value = 5
    mock_load.return_value = 3

    result = run_incremental(db_path, "route-views2", limit=1)
    assert result["processed"] == 1
    assert mock_fetch.called
    assert mock_parse.called
    assert mock_load.called
