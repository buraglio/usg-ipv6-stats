import pytest
import pyarrow.parquet as pq
from unittest.mock import patch, MagicMock
from usg_ipv6.pipeline.parse import parse_mrt, _origin_asn

TABLE_DUMP_V2 = 13
RIB_IPV4_UNICAST = 2
RIB_IPV6_UNICAST = 4


def _make_rib_entry(origin_asn: int) -> dict:
    """Return a rib_entry dict matching real mrtparse TABLE_DUMP_V2 output."""
    return {
        "path_attributes": [
            {
                "flag": 64,
                "type": {2: "AS_PATH"},
                "value": [
                    {"type": {2: "AS_SEQUENCE"}, "value": [7018, origin_asn]},
                ],
            }
        ]
    }


def _make_mock_entry(prefix, origin_asn, af):
    entry = MagicMock()
    entry.err = 0
    subtype_key = RIB_IPV4_UNICAST if af == 4 else RIB_IPV6_UNICAST
    entry.data = {
        "type": {TABLE_DUMP_V2: "TABLE_DUMP_V2"},
        "subtype": {subtype_key: "RIB"},
        "prefix": prefix,
        "rib_entries": [_make_rib_entry(origin_asn)],
    }
    return entry


def test_origin_asn_parses_real_mrtparse_format():
    rib_entry = _make_rib_entry(396260)
    assert _origin_asn(rib_entry) == 396260


def test_origin_asn_returns_none_when_no_as_path():
    assert _origin_asn({"path_attributes": []}) is None


@patch("usg_ipv6.pipeline.parse.Reader")
def test_parse_mrt_writes_parquet(mock_reader, tmp_path):
    mock_reader.return_value = [
        _make_mock_entry("192.0.2.0/24", 396260, 4),
        _make_mock_entry("2001:db8::/32", 396260, 6),
    ]
    out = str(tmp_path / "out.parquet")
    count = parse_mrt("fake.mrt", out)
    assert count == 2
    table = pq.read_table(out)
    assert set(table.schema.names) >= {"prefix", "origin_asn", "af", "prefix_len"}


@patch("usg_ipv6.pipeline.parse.Reader")
def test_ipv4_mapped_prefixes_filtered(mock_reader, tmp_path):
    mock_reader.return_value = [
        _make_mock_entry("::ffff:192.0.2.0/120", 396260, 6),
        _make_mock_entry("64:ff9b::/96", 396260, 6),
        _make_mock_entry("2001:db8::/32", 396260, 6),
    ]
    out = str(tmp_path / "out.parquet")
    count = parse_mrt("fake.mrt", out)
    assert count == 1
    table = pq.read_table(out)
    prefixes = table.column("prefix").to_pylist()
    assert "::ffff:192.0.2.0/120" not in prefixes
    assert "64:ff9b::/96" not in prefixes
    assert "2001:db8::/32" in prefixes
