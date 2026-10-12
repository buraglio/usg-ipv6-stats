import gzip
import requests
from pathlib import Path

CAIDA_AS2ORG_URL = (
    "https://publicdata.caida.org/datasets/as-organizations/"
    "{date}.as-org2info.txt.gz"
)
KNOWN_AUT_FORMAT = "aut|changed|aut_name|org_id|opaque_id|source"
KNOWN_ORG_FORMAT = "org|changed|name|country|org_id|source"


def download_as2org(dest_dir: str) -> str:
    from datetime import date

    # CAIDA publishes AS2Org on the 1st of each month.  Try the current month
    # then walk back up to 6 months to handle the window before each release.
    today = date.today()
    candidates = []
    year, month = today.year, today.month
    for _ in range(6):
        candidates.append(date(year, month, 1))
        month -= 1
        if month == 0:
            month = 12
            year -= 1

    url = None
    for candidate in candidates:
        probe = CAIDA_AS2ORG_URL.format(date=candidate.strftime("%Y%m%d"))
        resp = requests.head(probe, timeout=10)
        if resp.status_code == 200:
            url = probe
            break

    if url is None:
        raise RuntimeError("Could not locate a recent CAIDA AS2Org file")

    dest = Path(dest_dir) / url.split("/")[-1]
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
    return str(dest)


def parse_as2org(filepath: str) -> dict[int, dict]:
    opener = gzip.open if filepath.endswith(".gz") else open
    orgs: dict[str, dict] = {}
    asns: list[tuple[int, str]] = []

    with opener(filepath, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                if "format:" in line:
                    fmt = line.split("format:", 1)[1].strip()
                    if fmt not in (KNOWN_AUT_FORMAT, KNOWN_ORG_FORMAT):
                        raise ValueError(f"Unknown AS2Org format: {fmt!r}")
                continue
            parts = line.split("|")
            if len(parts) != 6:
                continue
            try:
                # aut line: asn|changed|aut_name|org_id|opaque_id|source
                asn = int(parts[0])
                org_id = parts[3]
                asns.append((asn, org_id))
            except ValueError:
                # org line: org_id|changed|name|country|opaque_id|source
                org_id = parts[0]
                org_name = parts[2]
                country = parts[3]
                orgs[org_id] = {"org_name": org_name, "country": country}

    return {
        asn: {"org_id": org_id, **orgs[org_id]}
        for asn, org_id in asns
        if org_id in orgs
    }
