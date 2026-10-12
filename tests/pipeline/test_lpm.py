import ipaddress
import pytest
from usg_ipv6.pipeline.lpm import build_lpm_index, lpm_lookup, lpm_lookup_entry


def test_exact_match():
    index = build_lpm_index([("2001:db8::/32", 64496)])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db8::1"), index) == 64496


def test_longest_prefix_wins():
    index = build_lpm_index([
        ("2001:db8::/32", 64496),
        ("2001:db8:1::/48", 64497),
    ])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db8:1::1"), index) == 64497


def test_shorter_prefix_fallback():
    index = build_lpm_index([
        ("2001:db8::/32", 64496),
        ("2001:db8:1::/48", 64497),
    ])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db8:2::1"), index) == 64496


def test_no_match_returns_none():
    index = build_lpm_index([("2001:db8::/32", 64496)])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db9::1"), index) is None


def test_host_route_128():
    addr = ipaddress.IPv6Address("2001:db8::1")
    index = build_lpm_index([(f"{addr}/128", 64496)])
    assert lpm_lookup(addr, index) == 64496


def test_default_route_0():
    index = build_lpm_index([("::/0", 64496)])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db8::1"), index) == 64496


def test_boundary_first_address():
    index = build_lpm_index([("2001:db8::/64", 64496)])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db8::"), index) == 64496


def test_boundary_last_address():
    index = build_lpm_index([("2001:db8::/64", 64496)])
    last = ipaddress.IPv6Address("2001:db8::ffff:ffff:ffff:ffff")
    assert lpm_lookup(last, index) == 64496


def test_empty_index_returns_none():
    index = build_lpm_index([])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db8::1"), index) is None


def test_non_ipv6_prefix_raises():
    with pytest.raises(ValueError, match="IPv6"):
        build_lpm_index([("192.0.2.0/24", 64496)])


def test_tie_breaking_first_inserted_wins():
    index = build_lpm_index([
        ("2001:db8::/32", 64496),
        ("2001:db8::/32", 64497),
    ])
    assert lpm_lookup(ipaddress.IPv6Address("2001:db8::1"), index) == 64496


def test_lookup_entry_returns_cidr():
    index = build_lpm_index([("2001:db8::/32", 64496)])
    entry = lpm_lookup_entry(ipaddress.IPv6Address("2001:db8::1"), index)
    assert entry is not None
    asn, cidr = entry
    assert asn == 64496
    assert cidr == "2001:db8::/32"


def test_lookup_entry_no_match_returns_none():
    index = build_lpm_index([("2001:db8::/32", 64496)])
    assert lpm_lookup_entry(ipaddress.IPv6Address("::1"), index) is None
