import gzip
import ipaddress
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from usg_ipv6.pipeline.ark import ARK_BASE_URL, download_nodes_as, parse_nodes_as

FIXTURES = Path(__file__).parent.parent / "fixtures"
SAMPLE = str(FIXTURES / "nodes_as_sample.nodes.as.gz")


def test_parse_valid_lines():
    results = list(parse_nodes_as(SAMPLE))
    assert len(results) == 5


def test_parse_correct_address_and_asn():
    results = list(parse_nodes_as(SAMPLE))
    addr, asn = results[0]
    assert addr == ipaddress.IPv6Address("2001:db8::1")
    assert asn == 64496


def test_parse_star_asn_yields_none():
    results = list(parse_nodes_as(SAMPLE))
    star_record = next(r for r in results if r[0] == ipaddress.IPv6Address("2001:db9::1"))
    assert star_record[1] is None


def test_parse_bad_asn_yields_none():
    results = list(parse_nodes_as(SAMPLE))
    bad_record = next(r for r in results if r[0] == ipaddress.IPv6Address("2001:db8::3"))
    assert bad_record[1] is None


def test_parse_skips_ipv4_mapped():
    results = list(parse_nodes_as(SAMPLE))
    addrs = [r[0] for r in results]
    assert ipaddress.IPv6Address("::ffff:102:304") not in addrs


def test_parse_skips_non_n_records():
    results = list(parse_nodes_as(SAMPLE))
    assert len(results) == 5


def test_download_raises_when_all_days_404(tmp_path):
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("HTTP 404")
    mock_resp.__enter__ = lambda s: mock_resp
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch("usg_ipv6.pipeline.ark.requests.get", return_value=mock_resp):
        with pytest.raises(FileNotFoundError, match="2024-03-15"):
            download_nodes_as("2024-03-15", str(tmp_path))


def test_download_returns_cached_file(tmp_path):
    date_str = "20240315"
    existing = tmp_path / f"team-1.ipv6.{date_str}.nodes.as.gz"
    existing.write_bytes(b"dummy")
    with patch("usg_ipv6.pipeline.ark.requests.get") as mock_get:
        result = download_nodes_as("2024-03-15", str(tmp_path))
        mock_get.assert_not_called()
    assert result == str(existing)


# --- Integration tests for run_ark_ingest ---

from collections import defaultdict
from datetime import datetime
from unittest.mock import patch

from usg_ipv6.db import get_connection, init_schema
from usg_ipv6.pipeline.ark import run_ark_ingest, run_ark_backfill


def _seed_db(conn, pfx2as_rows, corpus_rows):
    for row in pfx2as_rows:
        conn.execute("INSERT INTO caida_pfx2as VALUES (?, ?, ?, ?, ?)", row)
    for row in corpus_rows:
        conn.execute(
            "INSERT INTO asn_corpus VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row
        )


def _make_nodes_gz(tmp_path, name, content):
    p = tmp_path / name
    with gzip.open(p, "wt") as f:
        f.write(content)
    return str(p)


def test_ingest_produces_reachable_row(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[(64496, "GSA", "GSA", "cfo_act", "USGSA", "high", "arin",
                      "2024-03-15", "2024-03-15")],
    )
    nodes_file = _make_nodes_gz(tmp_path, "team-1.ipv6.20240315.nodes.as.gz",
                                 "N 2001:db8::1 64496\n")
    conn.close()

    with patch("usg_ipv6.pipeline.ark.download_nodes_as", return_value=nodes_file):
        result = run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15",
                                dest_dir=str(tmp_path))

    assert result["skipped"] is False
    assert result["addresses_parsed"] == 1
    conn = get_connection(str(tmp_path / "test.duckdb"))
    row = conn.execute(
        "SELECT ipv6_reachable, addresses_observed, prefixes_in_scope "
        "FROM ark_reachability WHERE asn = 64496"
    ).fetchone()
    assert row[0] is True
    assert row[1] == 1
    assert row[2] == 1


def test_ingest_produces_not_reachable_row(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[(64496, "GSA", "GSA", "cfo_act", "USGSA", "high", "arin",
                      "2024-03-15", "2024-03-15")],
    )
    nodes_file = _make_nodes_gz(tmp_path, "team-1.ipv6.20240315.nodes.as.gz",
                                 "N 2001:db9::1 99999\n")
    conn.close()

    with patch("usg_ipv6.pipeline.ark.download_nodes_as", return_value=nodes_file):
        run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15",
                       dest_dir=str(tmp_path))

    conn = get_connection(str(tmp_path / "test.duckdb"))
    row = conn.execute(
        "SELECT ipv6_reachable, addresses_observed, prefixes_in_scope "
        "FROM ark_reachability WHERE asn = 64496"
    ).fetchone()
    assert row[0] is False
    assert row[1] == 0
    assert row[2] == 1


def test_ingest_indeterminate_asn_has_zero_prefixes_in_scope(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[],
        corpus_rows=[(64496, "GSA", "GSA", "cfo_act", "USGSA", "high", "arin",
                      "2024-03-15", "2024-03-15")],
    )
    # IPv4-only pfx2as row — no IPv6 prefixes for corpus ASN
    conn.execute(
        "INSERT INTO caida_pfx2as VALUES ('2024-03-15', '192.0.2.0/24', 24, 64496, 4)"
    )
    conn.close()
    nodes_file = _make_nodes_gz(tmp_path, "team-1.ipv6.20240315.nodes.as.gz", "")

    with patch("usg_ipv6.pipeline.ark.download_nodes_as", return_value=nodes_file):
        run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15",
                       dest_dir=str(tmp_path))

    conn = get_connection(str(tmp_path / "test.duckdb"))
    count = conn.execute("SELECT COUNT(*) FROM ark_reachability").fetchone()[0]
    assert count == 0  # no IPv6 pfx2as rows → nothing written


def test_ingest_idempotent(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[(64496, "GSA", "GSA", "cfo_act", "USGSA", "high", "arin",
                      "2024-03-15", "2024-03-15")],
    )
    conn.close()
    nodes_file = _make_nodes_gz(tmp_path, "team-1.ipv6.20240315.nodes.as.gz",
                                 "N 2001:db8::1 64496\n")

    with patch("usg_ipv6.pipeline.ark.download_nodes_as", return_value=nodes_file):
        run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15",
                       dest_dir=str(tmp_path))
        result = run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15",
                                dest_dir=str(tmp_path))

    assert result["skipped"] is True
    conn = get_connection(str(tmp_path / "test.duckdb"))
    count = conn.execute("SELECT COUNT(*) FROM ark_reachability").fetchone()[0]
    assert count == 1


def test_ingest_raises_when_no_ipv6_pfx2as(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn, pfx2as_rows=[], corpus_rows=[])
    conn.close()
    nodes_file = _make_nodes_gz(tmp_path, "team-1.ipv6.20240315.nodes.as.gz", "")

    with patch("usg_ipv6.pipeline.ark.download_nodes_as", return_value=nodes_file):
        with pytest.raises(RuntimeError, match="caida_pfx2as"):
            run_ark_ingest(str(tmp_path / "test.duckdb"), date="2024-03-15",
                           dest_dir=str(tmp_path))


def test_backfill_skips_already_loaded(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    _seed_db(conn,
        pfx2as_rows=[("2024-03-15", "2001:db8::/32", 32, 64496, 6)],
        corpus_rows=[(64496, "GSA", "GSA", "cfo_act", "USGSA", "high", "arin",
                      "2024-03-15", "2024-03-15")],
    )
    conn.execute(
        "INSERT INTO ark_reachability VALUES ('2024-03-15', 64496, TRUE, 1, 1, '2024-03-15', 0)"
    )
    conn.close()
    nodes_file = _make_nodes_gz(tmp_path, "team-1.ipv6.20240315.nodes.as.gz",
                                 "N 2001:db8::1 64496\n")

    with patch("usg_ipv6.pipeline.ark.download_nodes_as", return_value=nodes_file):
        result = run_ark_backfill(
            str(tmp_path / "test.duckdb"),
            datetime(2024, 3, 15), datetime(2024, 3, 15),
            dest_dir=str(tmp_path),
        )

    assert result["skipped"] == 1
    assert result["processed"] == 0
