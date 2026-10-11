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
