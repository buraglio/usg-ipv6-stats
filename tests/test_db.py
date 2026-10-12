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


def test_ark_reachability_has_full_schema(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    cols = {
        row[0]
        for row in conn.execute("DESCRIBE ark_reachability").fetchall()
    }
    assert "addresses_observed" in cols
    assert "prefixes_in_scope" in cols
    assert "pfx2as_snapshot_date" in cols
    assert "asn_divergence_count" in cols


def test_ark_prefix_observations_table_exists(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    cols = {
        row[0]
        for row in conn.execute("DESCRIBE ark_prefix_observations").fetchall()
    }
    assert "our_asn" in cols
    assert "addresses_observed" in cols
    assert "addresses_ark_match" in cols


def test_migration_replaces_old_ark_reachability(tmp_path):
    from usg_ipv6.db import get_connection, init_schema
    conn = get_connection(str(tmp_path / "old.duckdb"))
    conn.execute("""
        CREATE TABLE ark_reachability (
            measurement_date DATE NOT NULL,
            asn BIGINT NOT NULL,
            ipv6_reachable BOOLEAN NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO ark_reachability VALUES ('2024-01-01', 396260, TRUE)"
    )
    conn.close()
    conn = get_connection(str(tmp_path / "old.duckdb"))
    init_schema(conn)
    cols = {
        row[0]
        for row in conn.execute("DESCRIBE ark_reachability").fetchall()
    }
    assert "addresses_observed" in cols
    count = conn.execute("SELECT COUNT(*) FROM ark_reachability").fetchone()[0]
    assert count == 0
    conn.close()
