import csv
import pytest
from pathlib import Path
from unittest.mock import patch
from usg_ipv6.corpus.build import build_corpus, assign_tier, merge_sources
from usg_ipv6.corpus.schema import AsnRecord
from usg_ipv6.db import get_connection

FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_tier_deduplication_most_specific_wins():
    records = [
        AsnRecord(asn=1234, org_name="DISA Test", agency="DISA", tier="dod",
                  arin_org_handle="DISA-1", confidence="high",
                  sources=["arin"], last_verified="2026-10-11", corpus_version="2026-10-11"),
        AsnRecord(asn=1234, org_name="DISA Test", agency="DISA", tier="federal_civilian",
                  arin_org_handle="DISA-1", confidence="medium",
                  sources=["caida"], last_verified="2026-10-11", corpus_version="2026-10-11"),
    ]
    merged = merge_sources(records)
    assert len(merged) == 1
    assert merged[0].tier == "dod"


def test_assign_tier_cfo_act_beats_federal_civilian():
    tier = assign_tier("Department of Agriculture", ["cfo_act", "federal_civilian"])
    assert tier == "cfo_act"


def test_build_corpus_produces_csv(tmp_path):
    db_path = str(tmp_path / "corpus.duckdb")
    csv_path = str(tmp_path / "corpus.csv")
    with patch("usg_ipv6.corpus.build.fetch_asns_for_org") as mock_fetch, \
         patch("usg_ipv6.corpus.build.parse_as2org") as mock_as2org:
        mock_fetch.return_value = [396260]
        mock_as2org.return_value = {
            396260: {"org_name": "General Services Administration", "country": "US"}
        }
        result = build_corpus(db_path, str(FIXTURES / "as2org_sample.txt"),
                              version="2026-10-11", csv_path=csv_path)
    assert Path(result).exists()
    with open(result) as f:
        rows = list(csv.DictReader(f))
    assert any(int(r["asn"]) == 396260 for r in rows)


def test_build_corpus_loads_duckdb(tmp_path):
    db_path = str(tmp_path / "corpus.duckdb")
    csv_path = str(tmp_path / "corpus.csv")
    with patch("usg_ipv6.corpus.build.fetch_asns_for_org") as mock_fetch, \
         patch("usg_ipv6.corpus.build.parse_as2org") as mock_as2org:
        mock_fetch.return_value = [396260]
        mock_as2org.return_value = {
            396260: {"org_name": "General Services Administration", "country": "US"}
        }
        build_corpus(db_path, str(FIXTURES / "as2org_sample.txt"),
                     version="2026-10-11", csv_path=csv_path)
    conn = get_connection(db_path)
    rows = conn.execute("SELECT asn FROM asn_corpus WHERE asn = 396260").fetchall()
    assert len(rows) == 1
