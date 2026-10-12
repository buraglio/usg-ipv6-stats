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
        prefix_len = int(prefix.split("/")[1]) if "/" in prefix else 0
        for rib_entry in d.get("rib_entries", []):
            origin = _origin_asn(rib_entry)
            if origin is None:
                continue
            rows.append({
                "prefix": prefix,
                "origin_asn": origin,
                "prefix_len": prefix_len,
                "af": af,
            })

    schema = pa.schema([
        pa.field("prefix", pa.string()),
        pa.field("origin_asn", pa.int64()),
        pa.field("prefix_len", pa.int16()),
        pa.field("af", pa.int8()),
    ])
    table = pa.table(
        {k: [r[k] for r in rows] for k in ["prefix", "origin_asn", "prefix_len", "af"]},
        schema=schema,
    )
    pq.write_table(table, parquet_path)
    return len(rows)
