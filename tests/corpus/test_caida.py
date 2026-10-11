import os
import tempfile
import pytest
from pathlib import Path
from usg_ipv6.corpus.caida import parse_as2org

FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_parse_as2org_returns_asn_mapping():
    result = parse_as2org(str(FIXTURES / "as2org_sample.txt"))
    assert 396260 in result
    assert result[396260]["org_name"] == "General Services Administration"
    assert result[396260]["country"] == "US"


def test_parse_as2org_maps_all_asns():
    result = parse_as2org(str(FIXTURES / "as2org_sample.txt"))
    assert 5722 in result
    assert result[5722]["org_name"] == "National Aeronautics and Space Administration"


def test_unknown_format_version_raises():
    content = "# format: UNKNOWN_FORMAT_VERSION\n396260|20200101|X|Y|Z|ARIN\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(content)
        path = f.name
    try:
        with pytest.raises(ValueError, match="Unknown AS2Org format"):
            parse_as2org(path)
    finally:
        os.unlink(path)
