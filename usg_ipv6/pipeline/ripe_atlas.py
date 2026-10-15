"""
RIPE Atlas enrichment pipeline (stub)
=======================================
RIPE Atlas has ~700 probes with IPv6 connectivity, many peered close to
USG network upstreams.  A host unreachable from the TU Munich hitlist
scanner (single vantage point, ICMP-only) may be reachable from a
US-based Atlas probe that is topologically closer or uses TCP.

Two measurement types are useful here:

1. DNS AAAA lookup: from multiple Atlas probes, query each .gov hostname
   that currently has no matching corpus prefix in domain_services.
   If probes report an AAAA in a corpus prefix, the host exists even if
   it didn't appear in crt.sh or dotgov DNS queries (could be a recently
   delegated zone or a recently issued cert not yet in CT logs).

2. TCP reach test (port 443): for IPv6 addresses found via domain_services
   that do NOT appear in the hitlist (potential firewall bypass candidates),
   issue Atlas TCP reach measurements to confirm liveness.

API: https://atlas.ripe.net/api/v2/  (requires free account + credits)
  - Measurement creation: POST /api/v2/measurements/
  - Results fetch:        GET  /api/v2/measurements/{id}/results/
  - Probe selection: target US-based probes with IPv6 (status_id=1, country_code=US)

Access:
  1. Register at https://atlas.ripe.net/
  2. Generate an API key with "Create User Defined Measurements" permission
  3. Set RIPE_ATLAS_API_KEY in GitHub Actions secrets

Credits: each DNS measurement costs 1 credit per probe; TCP reach costs 10.
A sweep of 500 .gov hostnames from 10 probes = 5000 credits (free allocation
is 100,000 credits for new accounts).
"""

import logging
from datetime import date as date_cls

import requests

log = logging.getLogger(__name__)

ATLAS_BASE = "https://atlas.ripe.net/api/v2"


def _get_us_ipv6_probes(api_key: str, limit: int = 20) -> list[int]:
    """Return probe IDs for US-based, IPv6-connected, connected Atlas probes."""
    r = requests.get(
        f"{ATLAS_BASE}/probes/",
        params={
            "country_code": "US",
            "status": 1,         # connected
            "tags": "system-ipv6-works",
            "format": "json",
            "page_size": limit,
        },
        headers={"Authorization": f"Key {api_key}"},
        timeout=30,
    )
    r.raise_for_status()
    return [p["id"] for p in r.json().get("results", [])]


def _create_dns_measurement(
    api_key: str, target: str, probe_ids: list[int]
) -> int | None:
    """Create a one-off AAAA DNS measurement; return measurement ID."""
    payload = {
        "definitions": [{
            "type": "dns",
            "description": f"USG IPv6 AAAA lookup: {target}",
            "af": 6,
            "query_class": "IN",
            "query_type": "AAAA",
            "query_argument": target,
            "use_probe_resolver": False,
            "resolve_on_probe": True,
            "is_oneoff": True,
        }],
        "probes": [{"type": "probes", "value": ",".join(str(p) for p in probe_ids), "requested": len(probe_ids)}],
    }
    try:
        r = requests.post(
            f"{ATLAS_BASE}/measurements/",
            json=payload,
            headers={"Authorization": f"Key {api_key}"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["measurements"][0]
    except Exception as exc:
        log.warning("Atlas measurement creation failed for %s: %s", target, exc)
        return None


def _fetch_results(api_key: str, measurement_id: int) -> list[dict]:
    """Poll for measurement results (blocks until complete or timeout)."""
    import time
    for _ in range(30):
        r = requests.get(
            f"{ATLAS_BASE}/measurements/{measurement_id}/",
            headers={"Authorization": f"Key {api_key}"},
            timeout=10,
        )
        r.raise_for_status()
        if r.json().get("status", {}).get("name") == "Stopped":
            break
        time.sleep(10)

    r = requests.get(
        f"{ATLAS_BASE}/measurements/{measurement_id}/results/",
        headers={"Authorization": f"Key {api_key}"},
        params={"format": "json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def run_ripe_atlas_dns(
    db_path: str,
    api_key: str,
    targets: list[str] | None = None,
    observation_date: date_cls | None = None,
    probe_count: int = 10,
) -> dict:
    """
    Run Atlas AAAA lookups for hostnames that lack corpus prefix matches.

    targets: list of hostnames to probe; if None, fetches from domain_services
             where matched_prefix IS NULL.
    Returns a summary dict.
    """
    from usg_ipv6.db import get_connection, init_schema
    from usg_ipv6.pipeline.dotgov_dns import _build_prefix_nets, _match_prefix

    obs_date = observation_date or date_cls.today()
    conn = get_connection(db_path)
    init_schema(conn)

    if targets is None:
        rows = conn.execute(
            "SELECT DISTINCT hostname FROM domain_services WHERE matched_prefix IS NULL LIMIT 200"
        ).fetchall()
        targets = [r[0] for r in rows]

    if not targets:
        conn.close()
        return {"targets": 0, "measurements": 0, "new_matches": 0}

    corpus_prefixes = conn.execute(
        "SELECT DISTINCT prefix, origin_asn FROM prefix_records WHERE af = 6"
    ).fetchall()
    nets = _build_prefix_nets(corpus_prefixes)

    probe_ids = _get_us_ipv6_probes(api_key, limit=probe_count)
    if not probe_ids:
        conn.close()
        log.warning("No Atlas probes found")
        return {"targets": len(targets), "measurements": 0, "new_matches": 0}

    new_matches = 0
    measurements = 0

    for hostname in targets:
        msm_id = _create_dns_measurement(api_key, hostname, probe_ids)
        if msm_id is None:
            continue
        measurements += 1

        results = _fetch_results(api_key, msm_id)
        for probe_result in results:
            for answer in probe_result.get("resultset", [{}])[0].get("result", {}).get("ANSWERS", []):
                if answer.get("TYPE") == "AAAA":
                    addr = answer.get("RDATA", "")
                    pfx, asn = _match_prefix(addr, nets)
                    if pfx:
                        # Find the domain from domain_services
                        domain_row = conn.execute(
                            "SELECT domain FROM domain_services WHERE hostname = ? LIMIT 1",
                            [hostname],
                        ).fetchone()
                        domain = domain_row[0] if domain_row else hostname
                        conn.execute(
                            "INSERT OR REPLACE INTO domain_services VALUES (?,?,?,?,?,?,?,?)",
                            (obs_date, domain, "atlas_dns", hostname, addr, pfx, asn, "ripe_atlas"),
                        )
                        new_matches += 1

    conn.close()
    log.info("Atlas: %d targets, %d measurements, %d new corpus matches",
             len(targets), measurements, new_matches)
    return {"targets": len(targets), "measurements": measurements, "new_matches": new_matches}
