import duckdb
import pytest
from usg_ipv6.db import get_connection, init_schema


def test_init_schema_creates_tables(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    tables = conn.execute("SHOW TABLES").fetchall()
    names = {t[0] for t in tables}
    assert "asn_corpus" in names
    assert "snapshots" in names
    assert "prefix_records" in names
    assert "caida_pfx2as" in names
    assert "ark_reachability" in names


def test_init_schema_is_idempotent(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    init_schema(conn)  # must not raise


def test_snapshots_allows_same_timestamp_different_collectors(tmp_path):
    from datetime import datetime
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    ts = datetime(2024, 3, 15, 8, 0, 0)
    conn.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, NULL)",
                 [ts, "route-views2", "http://rv2/rib.bz2", 1000, "processed"])
    conn.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, NULL)",
                 [ts, "route-views6", "http://rv6/rib.bz2", 2000, "processed"])
    count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert count == 2
