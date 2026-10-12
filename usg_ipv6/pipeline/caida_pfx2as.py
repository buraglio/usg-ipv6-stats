import gzip
import ipaddress
import duckdb
import requests
from pathlib import Path

PFX2AS_URL = (
    "https://publicdata.caida.org/datasets/routing/routeviews-prefix2as/"
    "{year}/{month}/routeviews-{variant}-{date}-1200.pfx2as.gz"
)


def download_pfx2as(dest_dir: str, date: str, variant: str = "rv6") -> str:
    """Download a CAIDA pfx2as file. variant='rv6' for IPv6 (default), 'rv2' for IPv4."""
    year, month, day = date.split("-")
    url = PFX2AS_URL.format(year=year, month=month, date=f"{year}{month}{day}", variant=variant)
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
            parts = line.split("|")
            if len(parts) < 2:
                continue
            prefix_str = parts[0].strip()
            # handle multi-origin "1234_5678" — take first ASN
            asn_str = parts[1].strip().split("_")[0]
            try:
                net = ipaddress.ip_network(prefix_str, strict=False)
                asn = int(asn_str)
                rows.append((snapshot_date, prefix_str, net.prefixlen, asn, net.version))
            except (ValueError, IndexError):
                continue
    if rows:
        conn.executemany("INSERT INTO caida_pfx2as VALUES (?, ?, ?, ?, ?)", rows)
    return len(rows)
