import ipaddress
from typing import Optional

# {prefix_len: {network_addr_int: (asn, cidr_str)}}
LpmIndex = dict[int, dict[int, tuple[int, str]]]


def build_lpm_index(prefixes: list[tuple[str, int]]) -> LpmIndex:
    """Build LPM index from (cidr_str, asn) pairs. IPv6 only."""
    index: LpmIndex = {}
    for cidr, asn in prefixes:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid prefix {cidr!r}: {exc}") from exc
        if net.version != 6:
            raise ValueError(f"Only IPv6 prefixes are supported, got {cidr!r}")
        pl = net.prefixlen
        net_int = int(net.network_address)
        # First-inserted wins on tie (same prefix length + network, different ASN)
        index.setdefault(pl, {}).setdefault(net_int, (asn, cidr))
    return index


def lpm_lookup_entry(
    addr: ipaddress.IPv6Address, index: LpmIndex
) -> Optional[tuple[int, str]]:
    """Return (asn, cidr_str) for the longest-matching prefix, or None."""
    addr_int = int(addr)
    for prefix_len in sorted(index.keys(), reverse=True):
        if prefix_len == 0:
            mask = 0
        else:
            mask = ((1 << 128) - 1) ^ ((1 << (128 - prefix_len)) - 1)
        net_int = addr_int & mask
        if net_int in index[prefix_len]:
            return index[prefix_len][net_int]
    return None


def lpm_lookup(addr: ipaddress.IPv6Address, index: LpmIndex) -> Optional[int]:
    """Return ASN for the longest-matching prefix, or None."""
    entry = lpm_lookup_entry(addr, index)
    return entry[0] if entry is not None else None
