from datetime import datetime

import pytest

from usg_ipv6.db import get_connection, init_schema
from usg_ipv6.pipeline.ark import run_ark_ingest, run_ark_backfill


def _seed_db(conn, pfx2as_rows, corpus_rows):
    for row in pfx2as_rows:
        conn.execute("INSERT INTO caida_pfx2as VALUES (?, ?, ?, ?, ?)", row)
    for row in corpus_rows:
        conn.execute("INSERT INTO asn_corpus VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row)


def _corpus_row(asn, agency="GSA"):
    return (asn, agency, agency, "cfo_act", f"US{agency}", "high", "arin",
            "2024-03-15", "2024-03-15")


def test_asn_with_ipv6_pfx2as_is_reachable(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[_corpus_row(64496)],
    )
    conn.close()

    result = run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15")

    assert result["skipped"] is False
    assert result["date"] == "2024-03-15"
    conn = get_connection(str(tmp_path / "test.duckdb"))
    row = conn.execute(
        "SELECT ipv6_reachable, addresses_observed, prefixes_in_scope "
        "FROM ark_reachability WHERE asn = 64496"
    ).fetchone()
    assert row[0] is True
    assert row[1] == 1
    assert row[2] == 1


def test_asn_without_ipv6_pfx2as_is_not_reachable(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[("2024-03-15", "192.0.2.0/24", 24, 64496, 4)],  # IPv4 only
        corpus_rows=[_corpus_row(64496)],
    )
    conn.close()

    run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15")

    conn = get_connection(str(tmp_path / "test.duckdb"))
    row = conn.execute(
        "SELECT ipv6_reachable, addresses_observed, prefixes_in_scope "
        "FROM ark_reachability WHERE asn = 64496"
    ).fetchone()
    assert row[0] is False
    assert row[1] == 0
    assert row[2] == 0


def test_all_corpus_asns_get_a_row(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[_corpus_row(64496), _corpus_row(64497, "DHS")],
    )
    conn.close()

    run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15")

    conn = get_connection(str(tmp_path / "test.duckdb"))
    rows = {r[0]: r for r in conn.execute(
        "SELECT asn, ipv6_reachable, prefixes_in_scope FROM ark_reachability ORDER BY asn"
    ).fetchall()}
    assert len(rows) == 2
    assert rows[64496][1] is True
    assert rows[64496][2] == 1
    assert rows[64497][1] is False
    assert rows[64497][2] == 0


def test_multiple_prefixes_per_asn(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[
            ("2024-03-15", "2001:db8::/32", 32, 64496, 6),
            ("2024-03-15", "2001:db9::/32", 32, 64496, 6),
            ("2024-03-15", "2001:dba::/32", 32, 64496, 6),
        ],
        corpus_rows=[_corpus_row(64496)],
    )
    conn.close()

    result = run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15")

    conn = get_connection(str(tmp_path / "test.duckdb"))
    row = conn.execute(
        "SELECT addresses_observed, prefixes_in_scope FROM ark_reachability WHERE asn = 64496"
    ).fetchone()
    assert row[0] == 3
    assert row[1] == 3
    assert result["addresses_parsed"] == 3


def test_prefix_observations_written(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[
            ("2024-03-15", "2001:db8::/32", 32, 64496, 6),
            ("2024-03-15", "2001:db9::/32", 32, 64496, 6),
        ],
        corpus_rows=[_corpus_row(64496)],
    )
    conn.close()

    run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15")

    conn = get_connection(str(tmp_path / "test.duckdb"))
    count = conn.execute(
        "SELECT COUNT(*) FROM ark_prefix_observations WHERE measurement_date = '2024-03-15'"
    ).fetchone()[0]
    assert count == 2


def test_ingest_idempotent(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[_corpus_row(64496)],
    )
    conn.close()

    run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15")
    result = run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15")

    assert result["skipped"] is True
    conn = get_connection(str(tmp_path / "test.duckdb"))
    count = conn.execute("SELECT COUNT(*) FROM ark_reachability").fetchone()[0]
    assert count == 1


def test_raises_when_pfx2as_empty(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn, pfx2as_rows=[], corpus_rows=[_corpus_row(64496)])
    conn.close()

    with pytest.raises(RuntimeError, match="caida_pfx2as"):
        run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15")


def test_raises_when_corpus_empty(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[],
    )
    conn.close()

    with pytest.raises(RuntimeError, match="asn_corpus"):
        run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15")


def test_backfill_processes_date_range(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[_corpus_row(64496)],
    )
    conn.close()

    result = run_ark_backfill(
        str(tmp_path / "test.duckdb"),
        datetime(2024, 3, 13),
        datetime(2024, 3, 15),
    )

    assert result["processed"] == 3
    assert result["skipped"] == 0
    conn = get_connection(str(tmp_path / "test.duckdb"))
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT measurement_date FROM ark_reachability ORDER BY measurement_date"
    ).fetchall()]
    assert len(dates) == 3


def test_backfill_skips_already_loaded_dates(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[_corpus_row(64496)],
    )
    conn.execute(
        "INSERT INTO ark_reachability VALUES ('2024-03-15', 64496, TRUE, 1, 1, '2024-03-15', 0)"
    )
    conn.close()

    result = run_ark_backfill(
        str(tmp_path / "test.duckdb"),
        datetime(2024, 3, 15),
        datetime(2024, 3, 15),
    )

    assert result["skipped"] == 1
    assert result["processed"] == 0
