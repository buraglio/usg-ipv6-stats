import time
import warnings
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ARIN_BASE = "https://whois.arin.net/rest"
_USER_AGENT = (
    "usg-ipv6-stats/1.0 (research; https://github.com/buraglio/usg-ipv6-stats)"
)


def _session() -> requests.Session:
    s = requests.Session()
    # Retry transport-level errors (connection resets, read errors) and 5xx.
    # 429 is handled in the caller so it can back off independently.
    retry = Retry(
        total=3,
        connect=2,
        read=2,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False,
        allowed_methods=frozenset(["GET", "HEAD"]),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update({"Accept": "application/json", "User-Agent": _USER_AGENT})
    return s


def arin_reachable(timeout: int = 5) -> bool:
    """Return True if whois.arin.net is reachable from this network.

    Used as a fast pre-flight check so callers can skip all ARIN queries
    when running in environments (e.g. GH Actions) where ARIN blocks access.
    """
    try:
        resp = requests.get(
            f"{ARIN_BASE}/org/ARIN/asns.json",
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            timeout=timeout,
        )
        # Any HTTP response — even 404 — means the TLS handshake succeeded
        return resp.status_code in (200, 404, 400, 403)
    except requests.exceptions.RequestException:
        return False


def fetch_asns_for_org(org_handle: str, *, max_retries: int = 3) -> list[int]:
    url = f"{ARIN_BASE}/org/{org_handle}/asns.json"
    sess = _session()
    for attempt in range(max_retries):
        try:
            resp = sess.get(url, timeout=15)
        except requests.exceptions.ConnectionError as exc:
            wait = 2 ** attempt
            warnings.warn(
                f"fetch_asns_for_org: connection error for {org_handle} "
                f"(attempt {attempt + 1}/{max_retries}): {exc}; retrying in {wait}s",
                UserWarning,
                stacklevel=2,
            )
            time.sleep(wait)
            continue
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
    sess = _session()
    resp = sess.get(url, timeout=15)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()
    orgs = data.get("orgs", {}).get("orgRef", [])
    if isinstance(orgs, dict):
        orgs = [orgs]
    return [{"handle": o["@handle"], "name": o["@name"]} for o in orgs]
