import pytest
import pyarrow.parquet as pq
from unittest.mock import patch, MagicMock
from usg_ipv6.pipeline.parse import parse_mrt

TABLE_DUMP_V2 = 13
RIB_IPV4_UNICAST = 2
RIB_IPV6_UNICAST = 4


def _make_mock_entry(prefix, origin_asn, af):
    entry = MagicMock()
    entry.err = 0
    subtype_key = RIB_IPV4_UNICAST if af == 4 else RIB_IPV6_UNICAST
    entry.data = {
        "type": {TABLE_DUMP_V2: "TABLE_DUMP_V2"},
        "subtype": {subtype_key: "RIB"},
        "prefix": prefix,
        "rib_entries": [{
            "path_attributes": {
                "as_path": [[origin_asn]],
            }
        }],
    }
    return entry


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
