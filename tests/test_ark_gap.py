import pytest
from usg_ipv6.db import get_connection, init_schema


@pytest.fixture
def gap_db(tmp_path):
    conn = get_connection(str(tmp_path / "gap.duckdb"))
    init_schema(conn)
    for row in [
        (64496, "GSA", "GSA", "cfo_act", "USGSA", "high", "arin",
         "2024-03-15", "2024-03-15"),
        (64497, "DHS", "DHS", "cfo_act", "USDHS", "high", "arin",
         "2024-03-15", "2024-03-15"),
        (64498, "DOD", "DOD", "dod", "USDOD", "high", "arin",
         "2024-03-15", "2024-03-15"),
    ]:
        conn.execute("INSERT INTO asn_corpus VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
    # 64496: IPv6 reachable
    conn.execute(
        "INSERT INTO ark_reachability VALUES ('2024-03-15', 64496, TRUE, 2, 1, '2024-03-15', 0)"
    )
    # 64497: NOT reachable, has prefixes (the gap case)
    conn.execute(
        "INSERT INTO ark_reachability VALUES ('2024-03-15', 64497, FALSE, 0, 2, '2024-03-15', 0)"
    )
    # 64498: NOT reachable, prefixes_in_scope=0 (indeterminate — must be EXCLUDED)
    conn.execute(
        "INSERT INTO ark_reachability VALUES ('2024-03-15', 64498, FALSE, 0, 0, '2024-03-15', 0)"
    )
    return conn


_GAP_QUERY = """
    SELECT ar.asn, c.agency, c.tier,
           ar.addresses_observed, ar.prefixes_in_scope
    FROM ark_reachability ar
    JOIN asn_corpus c ON ar.asn = c.asn
    WHERE ar.measurement_date = (SELECT MAX(measurement_date) FROM ark_reachability)
      AND ar.ipv6_reachable = FALSE
      AND ar.prefixes_in_scope > 0
    ORDER BY c.tier, c.agency
"""


def test_gap_query_returns_only_not_reachable_with_prefixes(gap_db):
    rows = gap_db.execute(_GAP_QUERY).fetchall()
    asns = [r[0] for r in rows]
    assert 64497 in asns          # not reachable, has prefixes → IN gap
    assert 64496 not in asns      # reachable → excluded
    assert 64498 not in asns      # indeterminate (prefixes_in_scope=0) → excluded


def test_gap_query_excludes_indeterminate(gap_db):
    rows = gap_db.execute(_GAP_QUERY).fetchall()
    assert all(r[4] > 0 for r in rows)  # prefixes_in_scope > 0 for all results


def test_gap_query_empty_when_all_reachable(gap_db):
    gap_db.execute("UPDATE ark_reachability SET ipv6_reachable = TRUE")
    rows = gap_db.execute(_GAP_QUERY).fetchall()
    assert rows == []
