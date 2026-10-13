"""Tests for the IPv6 Hitlist ingest pipeline."""

import lzma
from datetime import datetime

import pytest

from usg_ipv6.db import get_connection, init_schema
from usg_ipv6.pipeline.hitlist import (
    _build_aliased_lpm,
    _last_modified_to_date,
    _scan_responsive,
    run_hitlist_backfill,
    run_hitlist_ingest,
)
from usg_ipv6.pipeline.lpm import build_lpm_index


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _corpus_row(asn, agency="NASA"):
    return (asn, agency, agency, "cfo_act", f"US{asn}", "high", "arin",
            "2024-03-15", "2024-03-15")


def _seed(conn, pfx2as_rows, corpus_rows):
    for row in pfx2as_rows:
        conn.execute("INSERT INTO caida_pfx2as VALUES (?, ?, ?, ?, ?)", row)
    for row in corpus_rows:
        conn.execute("INSERT INTO asn_corpus VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row)


def _make_xz(path, lines):
    """Write lines to an lzma-compressed text file."""
    with lzma.open(path, "wt") as f:
        f.write("\n".join(lines) + "\n")
    return str(path)


def _db_and_files(tmp_path, pfx2as_rows, corpus_rows, responsive_lines, aliased_lines=None):
    """Create a seeded DB and fake hitlist files; return (db_path, resp_path, aliased_path)."""
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed(conn, pfx2as_rows, corpus_rows)
    conn.close()

    resp_path = _make_xz(tmp_path / "responsive.txt.xz", responsive_lines)
    aliased_path = _make_xz(tmp_path / "aliased.txt.xz", aliased_lines or [])
    return str(tmp_path / "test.duckdb"), resp_path, aliased_path


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------

def test_last_modified_to_date_valid():
    result = _last_modified_to_date("Wed, 01 Jan 2025 12:00:00 GMT")
    assert result == "2025-01-01"


def test_last_modified_to_date_invalid():
    assert _last_modified_to_date("") is None
    assert _last_modified_to_date("not a date") is None


def test_build_aliased_lpm_ipv4_filtered(tmp_path):
    aliased_path = _make_xz(
        tmp_path / "aliased.txt.xz",
        ["2001:db8::/32", "192.0.2.0/24", "# comment", ""],
    )
    lpm = _build_aliased_lpm(aliased_path)
    # Only the IPv6 prefix should be indexed
    import ipaddress
    addr_in = ipaddress.IPv6Address("2001:db8::1")
    addr_out = ipaddress.IPv6Address("2001:db9::1")
    from usg_ipv6.pipeline.lpm import lpm_lookup_entry
    assert lpm_lookup_entry(addr_in, lpm) is not None
    assert lpm_lookup_entry(addr_out, lpm) is None


def test_build_aliased_lpm_empty_file(tmp_path):
    aliased_path = _make_xz(tmp_path / "aliased.txt.xz", [])
    lpm = _build_aliased_lpm(aliased_path)
    assert lpm == {}


def test_scan_responsive_matches_gov_prefix(tmp_path):
    gov_lpm = build_lpm_index([("2001:db8::/32", 64496)])
    aliased_lpm = {}
    resp_path = _make_xz(
        tmp_path / "r.txt.xz",
        ["2001:db8::1", "2001:db8::2", "2001:db9::1"],  # last is outside
    )
    responsive, aliased, rows, total = _scan_responsive(
        resp_path, gov_lpm, aliased_lpm, "2024-03-15", "2024-03-14"
    )
    assert total == 3
    assert responsive[64496] == 2
    assert aliased[64496] == 0
    assert len(rows) == 2
    assert rows[0][2] == 64496


def test_scan_responsive_marks_aliased(tmp_path):
    gov_lpm = build_lpm_index([("2001:db8::/32", 64496)])
    aliased_lpm = build_lpm_index([("2001:db8::/32", 0)])  # entire prefix is aliased
    resp_path = _make_xz(tmp_path / "r.txt.xz", ["2001:db8::1"])
    _, aliased, rows, _ = _scan_responsive(
        resp_path, gov_lpm, aliased_lpm, "2024-03-15", "2024-03-14"
    )
    assert aliased[64496] == 1
    assert rows[0][4] is True  # is_aliased


# ---------------------------------------------------------------------------
# Integration tests for run_hitlist_ingest
# ---------------------------------------------------------------------------

def test_address_in_corpus_prefix_counted(tmp_path):
    db, resp, aliased = _db_and_files(
        tmp_path,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[_corpus_row(64496)],
        responsive_lines=["2001:db8::1", "2001:db8::2"],
    )
    result = run_hitlist_ingest(
        db, date="2024-03-15",
        _responsive_file=resp, _aliased_file=aliased, _hitlist_date="2024-03-14"
    )

    assert result["skipped"] is False
    assert result["matched_addresses"] == 2
    assert result["asns_with_responses"] == 1

    conn = get_connection(db)
    row = conn.execute(
        "SELECT responsive_count, aliased_count FROM hitlist_observations "
        "WHERE observation_date = '2024-03-15' AND asn = 64496"
    ).fetchone()
    assert row[0] == 2
    assert row[1] == 0


def test_address_outside_corpus_prefix_not_counted(tmp_path):
    db, resp, aliased = _db_and_files(
        tmp_path,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[_corpus_row(64496)],
        responsive_lines=["2001:db9::1", "::1"],  # neither in 2001:db8::/32
    )
    result = run_hitlist_ingest(
        db, date="2024-03-15",
        _responsive_file=resp, _aliased_file=aliased, _hitlist_date="2024-03-14"
    )

    assert result["matched_addresses"] == 0
    conn = get_connection(db)
    row = conn.execute(
        "SELECT responsive_count FROM hitlist_observations "
        "WHERE observation_date = '2024-03-15' AND asn = 64496"
    ).fetchone()
    assert row[0] == 0


def test_aliased_addresses_flagged(tmp_path):
    db, resp, _ = _db_and_files(
        tmp_path,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[_corpus_row(64496)],
        responsive_lines=["2001:db8::1"],
    )
    aliased = _make_xz(tmp_path / "aliased.txt.xz", ["2001:db8::/32"])
    result = run_hitlist_ingest(
        db, date="2024-03-15",
        _responsive_file=resp, _aliased_file=aliased, _hitlist_date="2024-03-14"
    )

    conn = get_connection(db)
    row = conn.execute(
        "SELECT responsive_count, aliased_count FROM hitlist_observations "
        "WHERE observation_date = '2024-03-15' AND asn = 64496"
    ).fetchone()
    assert row[0] == 1
    assert row[1] == 1

    addr_row = conn.execute(
        "SELECT is_aliased FROM hitlist_addresses "
        "WHERE observation_date = '2024-03-15' AND asn = 64496"
    ).fetchone()
    assert addr_row[0] is True


def test_all_corpus_asns_get_a_row(tmp_path):
    db, resp, aliased = _db_and_files(
        tmp_path,
        pfx2as_rows=[
            ("2024-03-15", "2001:db8::/32", 32, 64496, 6),
            ("2024-03-15", "2001:db9::/32", 32, 64497, 6),
        ],
        corpus_rows=[_corpus_row(64496), _corpus_row(64497, "DHS")],
        responsive_lines=["2001:db8::1"],  # only 64496 has a match
    )
    run_hitlist_ingest(
        db, date="2024-03-15",
        _responsive_file=resp, _aliased_file=aliased, _hitlist_date="2024-03-14"
    )

    conn = get_connection(db)
    rows = {r[0]: r[1] for r in conn.execute(
        "SELECT asn, responsive_count FROM hitlist_observations "
        "WHERE observation_date = '2024-03-15' ORDER BY asn"
    ).fetchall()}
    assert len(rows) == 2
    assert rows[64496] == 1
    assert rows[64497] == 0


def test_matched_addresses_written_to_hitlist_addresses(tmp_path):
    db, resp, aliased = _db_and_files(
        tmp_path,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[_corpus_row(64496)],
        responsive_lines=["2001:db8::1", "2001:db8::2", "2001:db8::3"],
    )
    run_hitlist_ingest(
        db, date="2024-03-15",
        _responsive_file=resp, _aliased_file=aliased, _hitlist_date="2024-03-14"
    )

    conn = get_connection(db)
    count = conn.execute(
        "SELECT COUNT(*) FROM hitlist_addresses WHERE observation_date = '2024-03-15'"
    ).fetchone()[0]
    assert count == 3


def test_ingest_idempotent(tmp_path):
    db, resp, aliased = _db_and_files(
        tmp_path,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[_corpus_row(64496)],
        responsive_lines=["2001:db8::1"],
    )
    run_hitlist_ingest(
        db, date="2024-03-15",
        _responsive_file=resp, _aliased_file=aliased, _hitlist_date="2024-03-14"
    )
    result = run_hitlist_ingest(
        db, date="2024-03-15",
        _responsive_file=resp, _aliased_file=aliased, _hitlist_date="2024-03-14"
    )

    assert result["skipped"] is True
    conn = get_connection(db)
    count = conn.execute("SELECT COUNT(*) FROM hitlist_observations").fetchone()[0]
    assert count == 1  # not duplicated


def test_raises_when_no_ipv6_pfx2as(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed(conn,
        pfx2as_rows=[("2024-03-15", "192.0.2.0/24", 24, 64496, 4)],  # IPv4 only
        corpus_rows=[_corpus_row(64496)],
    )
    conn.close()
    resp = _make_xz(tmp_path / "r.txt.xz", [])
    aliased = _make_xz(tmp_path / "a.txt.xz", [])

    with pytest.raises(RuntimeError, match="IPv6 pfx2as"):
        run_hitlist_ingest(
            str(tmp_path / "test.duckdb"), date="2024-03-15",
            _responsive_file=resp, _aliased_file=aliased, _hitlist_date="2024-03-14"
        )


def test_raises_when_no_ipv6_corpus_prefixes(tmp_path):
    """IPv6 pfx2as exists but none of the prefixes belong to corpus ASNs."""
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 99999, 6)],  # ASN not in corpus
        corpus_rows=[_corpus_row(64496)],
    )
    conn.close()
    resp = _make_xz(tmp_path / "r.txt.xz", [])
    aliased = _make_xz(tmp_path / "a.txt.xz", [])

    with pytest.raises(RuntimeError, match="corpus prefixes"):
        run_hitlist_ingest(
            str(tmp_path / "test.duckdb"), date="2024-03-15",
            _responsive_file=resp, _aliased_file=aliased, _hitlist_date="2024-03-14"
        )


def test_raises_when_corpus_empty(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[],
    )
    conn.close()
    resp = _make_xz(tmp_path / "r.txt.xz", [])
    aliased = _make_xz(tmp_path / "a.txt.xz", [])

    with pytest.raises(RuntimeError, match="asn_corpus"):
        run_hitlist_ingest(
            str(tmp_path / "test.duckdb"), date="2024-03-15",
            _responsive_file=resp, _aliased_file=aliased, _hitlist_date="2024-03-14"
        )


def test_backfill_processes_date_range(tmp_path, monkeypatch):
    db, resp, aliased = _db_and_files(
        tmp_path,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[_corpus_row(64496)],
        responsive_lines=["2001:db8::1"],
    )

    import usg_ipv6.pipeline.hitlist as hl
    monkeypatch.setattr(hl, "download_hitlist", lambda dest_dir, timeout=300: {
        "responsive_path": resp,
        "aliased_path": aliased,
        "hitlist_date": "2024-03-14",
    })

    result = run_hitlist_backfill(
        db,
        datetime(2024, 3, 13),
        datetime(2024, 3, 15),
        dest_dir=str(tmp_path),
    )

    assert result["processed"] == 3
    assert result["skipped"] == 0
    conn = get_connection(db)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT observation_date FROM hitlist_observations ORDER BY observation_date"
    ).fetchall()]
    assert len(dates) == 3


def test_backfill_skips_already_loaded_dates(tmp_path, monkeypatch):
    db, resp, aliased = _db_and_files(
        tmp_path,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[_corpus_row(64496)],
        responsive_lines=["2001:db8::1"],
    )
    # Pre-load one date
    run_hitlist_ingest(
        db, date="2024-03-15",
        _responsive_file=resp, _aliased_file=aliased, _hitlist_date="2024-03-14"
    )

    import usg_ipv6.pipeline.hitlist as hl
    monkeypatch.setattr(hl, "download_hitlist", lambda dest_dir, timeout=300: {
        "responsive_path": resp,
        "aliased_path": aliased,
        "hitlist_date": "2024-03-14",
    })

    result = run_hitlist_backfill(
        db,
        datetime(2024, 3, 15),
        datetime(2024, 3, 15),
        dest_dir=str(tmp_path),
    )
    assert result["skipped"] == 1
    assert result["processed"] == 0
