import time
import warnings
import requests

ARIN_BASE = "https://whois.arin.net/rest"


def fetch_asns_for_org(org_handle: str, *, max_retries: int = 3) -> list[int]:
    url = f"{ARIN_BASE}/org/{org_handle}/asns.json"
    for attempt in range(max_retries):
        resp = requests.get(url, headers={"Accept": "application/json"}, timeout=10)
        if resp.status_code == 404:
            return []
        if resp.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("asns", {}).get("asn", [])
        if isinstance(raw, str):
            raw = [raw]
        return [int(a) for a in raw]
    warnings.warn(
        f"fetch_asns_for_org: all {max_retries} retries exhausted for {org_handle}; returning []",
        UserWarning,
        stacklevel=2,
    )
    return []


def search_orgs_by_name(name: str) -> list[dict]:
    url = f"{ARIN_BASE}/orgs;name={requests.utils.quote(name)}.json"
    resp = requests.get(url, headers={"Accept": "application/json"}, timeout=10)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()
    orgs = data.get("orgs", {}).get("orgRef", [])
    if isinstance(orgs, dict):
        orgs = [orgs]
    return [{"handle": o["@handle"], "name": o["@name"]} for o in orgs]
