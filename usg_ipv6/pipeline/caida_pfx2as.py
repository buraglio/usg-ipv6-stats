import gzip
import ipaddress
import duckdb
import requests
from pathlib import Path

# rv2 (IPv4) and rv6 (IPv6) live under different dataset paths on CAIDA's server.
_PFX2AS_URL_RV2 = (
    "https://publicdata.caida.org/datasets/routing/routeviews-prefix2as/"
    "{year}/{month}/routeviews-rv2-{date}-1200.pfx2as.gz"
)
_PFX2AS_URL_RV6 = (
    "https://publicdata.caida.org/datasets/routing/routeviews6-prefix2as/"
    "{year}/{month}/routeviews-rv6-{date}-1200.pfx2as.gz"
)


def download_pfx2as(dest_dir: str, date: str, variant: str = "rv6") -> str:
    """Download a CAIDA pfx2as file. variant='rv6' for IPv6 (default), 'rv2' for IPv4."""
    year, month, day = date.split("-")
    template = _PFX2AS_URL_RV6 if variant == "rv6" else _PFX2AS_URL_RV2
    url = template.format(year=year, month=month, date=f"{year}{month}{day}")
    dest = Path(dest_dir) / url.split("/")[-1]
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
    return str(dest)


def load_pfx2as(
    filepath: str, snapshot_date: str, conn: duckdb.DuckDBPyConnection
) -> int:
    opener = gzip.open if filepath.endswith(".gz") else open
    rows = []
    with opener(filepath, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # CAIDA pfx2as format: <prefix>\t<prefixlen>\t<asn>
            # Multi-origin ASNs appear as "1234_5678" — take first
            parts = line.split()
            if len(parts) < 3:
                continue
            prefix_addr, prefix_len_str, asn_str = parts[0], parts[1], parts[2]
            asn_str = asn_str.split("_")[0]
            try:
                net = ipaddress.ip_network(f"{prefix_addr}/{prefix_len_str}", strict=False)
                asn = int(asn_str)
                rows.append((snapshot_date, str(net), net.prefixlen, asn, net.version))
            except (ValueError, IndexError):
                continue
    if rows:
        conn.executemany("INSERT INTO caida_pfx2as VALUES (?, ?, ?, ?, ?)", rows)
    return len(rows)
