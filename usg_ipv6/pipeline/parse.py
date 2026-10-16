import ipaddress
import pyarrow as pa
import pyarrow.parquet as pq
from mrtparse import Reader

TABLE_DUMP_V2 = 13
RIB_IPV4_UNICAST = 2
RIB_IPV6_UNICAST = 4

_IPV4_MAPPED = [
    ipaddress.ip_network("::ffff:0:0/96"),
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
]


def _is_ipv4_mapped(prefix: str) -> bool:
    try:
        net = ipaddress.ip_network(prefix, strict=False)
        return net.version == 6 and any(
            net.subnet_of(m) or net == m for m in _IPV4_MAPPED
        )
    except ValueError:
        return False


def _origin_asn(rib_entry: dict) -> int | None:
    for attr in rib_entry.get("path_attributes", []):
        if 2 not in attr.get("type", {}):
            continue
        for seg in reversed(attr.get("value", [])):
            values = seg.get("value", []) if isinstance(seg, dict) else []
            if values:
                return int(values[-1])
    return None


def _communities(rib_entry: dict) -> list[str]:
    """
    Extract standard BGP communities (type 8) from a RIB entry.
    Returns a list of "asn:value" strings, e.g. ["65000:100", "65000:200"].
    Large communities (type 32) are formatted as "asn:ldp1:ldp2".
    """
    result: list[str] = []
    for attr in rib_entry.get("path_attributes", []):
        attr_type_keys = list(attr.get("type", {}).keys())
        if not attr_type_keys:
            continue
        attr_type = attr_type_keys[0]

        # Standard community (RFC 1997)
        if attr_type == 8:
            for comm in attr.get("value", []):
                if isinstance(comm, dict):
                    asn_part = comm.get("asn", 0)
                    val_part = comm.get("value", 0)
                    result.append(f"{asn_part}:{val_part}")
                elif isinstance(comm, int):
                    result.append(f"{(comm >> 16) & 0xFFFF}:{comm & 0xFFFF}")

        # Large community (RFC 8092)
        elif attr_type == 32:
            for comm in attr.get("value", []):
                if isinstance(comm, dict):
                    ga = comm.get("global_administrator", 0)
                    ldp1 = comm.get("local_data_part_1", 0)
                    ldp2 = comm.get("local_data_part_2", 0)
                    result.append(f"{ga}:{ldp1}:{ldp2}")

    return result


def parse_mrt(mrt_path: str, parquet_path: str) -> int:
    rows: list[dict] = []
    for entry in Reader(mrt_path):
        if entry.err:
            continue
        d = entry.data
        type_keys = list(d.get("type", {}).keys())
        if not type_keys or type_keys[0] != TABLE_DUMP_V2:
            continue
        subtype_keys = list(d.get("subtype", {}).keys())
        if not subtype_keys or subtype_keys[0] not in (RIB_IPV4_UNICAST, RIB_IPV6_UNICAST):
            continue
        af = 4 if subtype_keys[0] == RIB_IPV4_UNICAST else 6
        prefix = d.get("prefix", "")
        if af == 6 and _is_ipv4_mapped(prefix):
            continue
        # mrtparse stores the prefix length in d["length"], not embedded in the
        # prefix string.  Reconstruct CIDR notation so ip_network() parses it
        # correctly; bare "2001:400::" parses as a /128 host address.
        prefix_len = d.get("length", int(prefix.split("/")[1]) if "/" in prefix else 0)
        if prefix_len and "/" not in prefix:
            prefix = f"{prefix}/{prefix_len}"
        for rib_entry in d.get("rib_entries", []):
            origin = _origin_asn(rib_entry)
            if origin is None:
                continue
            comms = _communities(rib_entry)
            rows.append({
                "prefix": prefix,
                "origin_asn": origin,
                "prefix_len": prefix_len,
                "af": af,
                "communities": ",".join(comms) if comms else "",
            })

    schema = pa.schema([
        pa.field("prefix", pa.string()),
        pa.field("origin_asn", pa.int64()),
        pa.field("prefix_len", pa.int16()),
        pa.field("af", pa.int8()),
        pa.field("communities", pa.string()),
    ])
    table = pa.table(
        {k: [r[k] for r in rows] for k in ["prefix", "origin_asn", "prefix_len", "af", "communities"]},
        schema=schema,
    )
    pq.write_table(table, parquet_path)
    return len(rows)
