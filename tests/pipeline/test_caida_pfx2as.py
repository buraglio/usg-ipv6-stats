import pytest
from pathlib import Path
from usg_ipv6.pipeline.caida_pfx2as import load_pfx2as

FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_load_pfx2as_inserts_records(db):
    count = load_pfx2as(str(FIXTURES / "pfx2as_sample.txt"), "2024-03-15", db)
    assert count == 3
    rows = db.execute("SELECT COUNT(*) FROM caida_pfx2as").fetchone()
    assert rows[0] == 3


def test_load_pfx2as_sets_address_family(db):
    load_pfx2as(str(FIXTURES / "pfx2as_sample.txt"), "2024-03-15", db)
    v6 = db.execute("SELECT COUNT(*) FROM caida_pfx2as WHERE af = 6").fetchone()[0]
    assert v6 == 1
    v4 = db.execute("SELECT COUNT(*) FROM caida_pfx2as WHERE af = 4").fetchone()[0]
    assert v4 == 2
