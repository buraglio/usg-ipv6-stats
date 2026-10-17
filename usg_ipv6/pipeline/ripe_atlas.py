"""
RIPE Atlas enrichment pipeline
================================
Uses RIPE Atlas to discover live IPv6 addresses within USG-allocated
prefixes that evade ICMP-based probing (TU Munich hitlist, CAIDA Ark)
due to firewall policy.

Strategy
--------
Per run (bounded by prefix_batch_size):

1. Select the N oldest-probed corpus IPv6 prefixes (those never probed
   come first, then those probed longest ago), so full corpus coverage
   accumulates over successive weekly runs.
2. Generate candidate target addresses per prefix (low-numbered host-part
   offsets plus seeds from domain_services / hitlist_addresses).
3. Batch-submit all measurements concurrently (ThreadPoolExecutor).
4. Sleep once for wait_minutes (default 15) while Atlas runs all probes.
5. Batch-fetch all results concurrently.
6. Parse and store: atlas_observations (per target/type response) and
   atlas_hop_addresses (router addresses found inside corpus prefixes
   via traceroute hop extraction).

This three-phase approach completes in wall-clock time proportional to
wait_minutes + network overhead, regardless of measurement count —
avoiding the sequential create-poll-store loop that causes CI timeouts.

Credit estimate (defaults: 100 prefixes, 5 targets, 5 probes, ping+trace):
  100 × 5 × 5 × (1 + 10) = 27,500 credits per run
  530 prefixes cycled at 100/week ≈ 6 weeks for full corpus coverage.

API key
-------
Requires RIPE_ATLAS_API_KEY with "Create User Defined Measurements" scope.
Set as a GitHub Actions secret named RIPE_ATLAS_API_KEY.
"""

import ipaddress
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as date_cls
from typing import Iterator

import requests

log = logging.getLogger(__name__)

ATLAS_BASE = "https://atlas.ripe.net/api/v2"

# Measurement types available.
# Each entry: (label, atlas_type, extra_params, credits_per_probe)
_MEASUREMENT_SPECS: list[tuple[str, str, dict, int]] = [
    ("ping",      "ping",       {"af": 6, "packets": 3},                                 1),
    ("tcp_443",   "traceroute", {"af": 6, "protocol": "TCP", "paris": 0,
                                 "response_timeout": 4000, "size": 0},                  10),
    ("tcp_80",    "traceroute", {"af": 6, "protocol": "TCP", "paris": 0,
                                 "response_timeout": 4000, "size": 0},                  10),
    ("tcp_53",    "traceroute", {"af": 6, "protocol": "TCP", "paris": 0,
                                 "response_timeout": 4000, "size": 0},                  10),
    ("traceroute","traceroute", {"af": 6, "protocol": "ICMP", "paris": 16,
                                 "response_timeout": 4000, "size": 0, "max_hops": 32},  10),
]

_TCP_PORTS = {"tcp_443": 443, "tcp_80": 80, "tcp_53": 53}

# Default measurement types: ping finds open hosts, traceroute finds router
# hops even through firewalls.  TCP types are opt-in for deeper sweeps.
_DEFAULT_LABELS = ["ping", "traceroute"]

# Low-numbered host-part offsets to probe per prefix.
# Covers routers (::1, ::2), anycast (::a), DNS (::53), NTP (::7b), etc.
_PROBE_OFFSETS = [1, 2, 0xa, 0x10, 0x53]

# Max concurrent HTTP workers for submission and result fetching.
_HTTP_WORKERS = 8


# ── Target generation ─────────────────────────────────────────────────────────

def _generate_targets(prefix: str, extra_addrs: list[str] | None = None) -> list[str]:
    """Return candidate IPv6 addresses to probe within a prefix."""
    try:
        net = ipaddress.ip_network(prefix, strict=False)
    except ValueError:
        return []

    seen: set[str] = set()
    targets: list[str] = []
    base = int(net.network_address)
    host_bits = 128 - net.prefixlen

    for offset in _PROBE_OFFSETS:
        if offset >= (1 << host_bits):
            break
        addr = str(ipaddress.ip_address(base + offset))
        if addr not in seen:
            seen.add(addr)
            targets.append(addr)

    for addr in (extra_addrs or []):
        try:
            ip = ipaddress.ip_address(addr)
            if ip in net and addr not in seen:
                seen.add(addr)
                targets.append(addr)
        except ValueError:
            pass

    return targets


def _corpus_extra_addresses(conn) -> dict[str, list[str]]:
    """Return {prefix: [addr, ...]} from domain_services and hitlist_addresses."""
    result: dict[str, list[str]] = {}
    for query in (
        "SELECT ipv6_address, matched_prefix FROM domain_services WHERE matched_prefix IS NOT NULL",
        "SELECT address, matched_prefix FROM hitlist_addresses WHERE matched_prefix IS NOT NULL",
    ):
        try:
            for addr, pfx in conn.execute(query).fetchall():
                if pfx:
                    result.setdefault(pfx, []).append(addr)
        except Exception:
            pass
    return result


# ── Probe selection ───────────────────────────────────────────────────────────

def _get_probes(
    api_key: str,
    country_code: str = "US",
    limit: int = 5,
) -> list[int]:
    """Return Atlas probe IDs: connected, IPv6-capable, in the requested country."""
    params: dict = {
        "country_code": country_code,
        "status": 1,
        "tags": "system-ipv6-works",
        "format": "json",
        "is_anchor": True,
        "page_size": limit,
    }
    r = requests.get(
        f"{ATLAS_BASE}/probes/",
        params=params,
        headers={"Authorization": f"Key {api_key}"},
        timeout=30,
    )
    r.raise_for_status()
    ids = [p["id"] for p in r.json().get("results", [])]

    if len(ids) < limit:
        params.pop("is_anchor")
        params["page_size"] = limit - len(ids)
        r = requests.get(
            f"{ATLAS_BASE}/probes/",
            params=params,
            headers={"Authorization": f"Key {api_key}"},
            timeout=30,
        )
        r.raise_for_status()
        for p in r.json().get("results", []):
            if p["id"] not in ids:
                ids.append(p["id"])

    return ids[:limit]


# ── Measurement creation ──────────────────────────────────────────────────────

def _create_measurement(
    api_key: str,
    label: str,
    msm_type: str,
    target: str,
    probe_ids: list[int],
    extra: dict,
) -> int | None:
    """Create a one-off Atlas measurement; return its ID or None on failure."""
    port = _TCP_PORTS.get(label)
    defn: dict = {
        "type": msm_type,
        "af": 6,
        "target": target,
        "description": f"USG-IPv6 {label}: {target}",
        "is_oneoff": True,
        **extra,
    }
    if port:
        defn["port"] = port

    payload = {
        "definitions": [defn],
        "probes": [{
            "type": "probes",
            "value": ",".join(str(p) for p in probe_ids),
            "requested": len(probe_ids),
        }],
    }
    try:
        r = requests.post(
            f"{ATLAS_BASE}/measurements/",
            json=payload,
            headers={"Authorization": f"Key {api_key}"},
            timeout=30,
        )
        r.raise_for_status()
        msm_id = r.json()["measurements"][0]
        log.debug("Created %s → msm %s", label, msm_id)
        return msm_id
    except Exception as exc:
        log.warning("Atlas create failed (%s %s): %s", label, target, exc)
        return None


# ── Result fetching ───────────────────────────────────────────────────────────

def _fetch_results(api_key: str, msm_id: int) -> list[dict]:
    """Fetch results for a completed measurement (no polling — caller waits)."""
    try:
        r = requests.get(
            f"{ATLAS_BASE}/measurements/{msm_id}/results/",
            headers={"Authorization": f"Key {api_key}"},
            params={"format": "json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("Atlas fetch failed for %s: %s", msm_id, exc)
        return []


# ── Result parsing ────────────────────────────────────────────────────────────

def _parse_ping(results: list[dict]) -> tuple[int, float | None]:
    responding = 0
    rtts: list[float] = []
    for r in results:
        avg = r.get("avg")
        if avg is not None and avg > 0:
            responding += 1
            rtts.append(float(avg))
    median = sorted(rtts)[len(rtts) // 2] if rtts else None
    return responding, median


def _parse_tcp(results: list[dict]) -> tuple[int, float | None]:
    responding = 0
    rtts: list[float] = []
    for r in results:
        for probe in (r.get("result") or [{}])[-1:]:
            for p in probe.get("result", [{}]) if isinstance(probe, dict) else [{}]:
                rtt = p.get("rtt") if isinstance(p, dict) else None
                if rtt is not None and rtt > 0:
                    responding += 1
                    rtts.append(float(rtt))
                    break
    median = sorted(rtts)[len(rtts) // 2] if rtts else None
    return responding, median


def _parse_traceroute_hops(
    results: list[dict],
    corpus_nets: list[tuple],
) -> set[str]:
    """Extract hop addresses within corpus prefixes from traceroute results."""
    found: set[str] = set()
    for r in results:
        for hop in r.get("result", []):
            for probe in hop.get("result", [{}]):
                addr = probe.get("from") if isinstance(probe, dict) else None
                if not addr:
                    continue
                try:
                    ip = ipaddress.ip_address(addr)
                    if ip.version != 6:
                        continue
                except ValueError:
                    continue
                for net, _asn, _pfx in corpus_nets:
                    if ip in net:
                        found.add(addr)
                        break
    return found


def _build_corpus_nets(conn) -> list[tuple[ipaddress.IPv6Network, int, str]]:
    rows = conn.execute("""
        SELECT DISTINCT c.prefix, c.asn
        FROM caida_pfx2as c
        JOIN asn_corpus ac ON ac.asn = c.asn
        WHERE c.af = 6
          AND c.snapshot_date = (SELECT MAX(snapshot_date) FROM caida_pfx2as WHERE af = 6)
    """).fetchall()
    nets = []
    for pfx, asn in rows:
        try:
            nets.append((ipaddress.ip_network(pfx, strict=False), int(asn), pfx))
        except ValueError:
            pass
    return nets


def _match_corpus(addr: str, nets: list[tuple]) -> tuple[str | None, int | None]:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None, None
    for net, asn, pfx in nets:
        if ip in net:
            return pfx, asn
    return None, None


# ── Credit estimator ──────────────────────────────────────────────────────────

def estimate_credits(
    prefix_count: int,
    targets_per_prefix: int,
    probe_count: int,
    measurement_labels: list[str] | None = None,
) -> int:
    specs = {label: credits for label, _, _, credits in _MEASUREMENT_SPECS}
    labels = measurement_labels or _DEFAULT_LABELS
    cost_per_target = sum(specs[l] for l in labels if l in specs)
    return prefix_count * targets_per_prefix * probe_count * cost_per_target


# ── Main sweep ────────────────────────────────────────────────────────────────

def run_prefix_sweep(
    db_path: str,
    api_key: str,
    observation_date: date_cls | None = None,
    probe_count: int = 5,
    country_code: str = "US",
    measurement_labels: list[str] | None = None,
    prefix_batch_size: int = 100,
    wait_minutes: int = 15,
    dry_run: bool = False,
) -> dict:
    """
    Sweep a batch of corpus IPv6 prefixes with Atlas measurements.

    Selects the oldest-probed prefixes first so full corpus coverage
    accumulates across successive weekly runs.  Submits all measurements
    concurrently, waits once (wait_minutes), then fetches all results
    concurrently.  Completes in wall-clock time ≈ wait_minutes + network
    overhead, regardless of measurement count.

    Parameters
    ----------
    probe_count       Atlas probes per measurement (default 5).
    prefix_batch_size Prefixes to sweep per run (default 100).
    wait_minutes      Fixed wait after submitting all measurements (default 15).
    measurement_labels  Labels to run; default: ["ping", "traceroute"].
    dry_run           Estimate credits without creating any measurements.
    """
    from usg_ipv6.db import get_connection, init_schema

    obs_date = observation_date or date_cls.today()
    labels = measurement_labels or _DEFAULT_LABELS
    specs = {label: (t, e, c) for label, t, e, c in _MEASUREMENT_SPECS}

    conn = get_connection(db_path)
    init_schema(conn)

    corpus_nets = _build_corpus_nets(conn)
    extra_by_prefix = _corpus_extra_addresses(conn)

    # Select oldest-probed prefixes: never-probed first, then by last observation.
    prefix_rows = conn.execute("""
        SELECT c.prefix, c.asn, MAX(ao.observation_date) AS last_probed
        FROM caida_pfx2as c
        JOIN asn_corpus ac ON ac.asn = c.asn
        LEFT JOIN atlas_observations ao ON ao.matched_prefix = c.prefix
        WHERE c.af = 6
          AND c.snapshot_date = (SELECT MAX(snapshot_date) FROM caida_pfx2as WHERE af = 6)
        GROUP BY c.prefix, c.asn
        ORDER BY last_probed ASC NULLS FIRST, c.prefix
        LIMIT ?
    """, [prefix_batch_size]).fetchall()

    all_targets: list[tuple[str, str, int]] = []
    for pfx, asn, _last in prefix_rows:
        for target in _generate_targets(pfx, extra_by_prefix.get(pfx)):
            all_targets.append((target, pfx, asn))

    est = estimate_credits(len(prefix_rows), len(_PROBE_OFFSETS), probe_count, labels)
    log.info(
        "Atlas sweep: %d prefixes, %d targets, %d probes, types=%s — est. %d credits",
        len(prefix_rows), len(all_targets), probe_count, labels, est,
    )

    if dry_run:
        conn.close()
        return {
            "prefixes": len(prefix_rows),
            "targets": len(all_targets),
            "estimated_credits": est,
            "measurements_created": 0,
            "dry_run": True,
        }

    probe_ids = _get_probes(api_key, country_code=country_code, limit=probe_count)
    if not probe_ids:
        conn.close()
        log.error("No Atlas probes found for country_code=%s", country_code)
        return {"error": "no_probes", "country_code": country_code}

    # ── Phase 1: submit all measurements concurrently ──────────────────────────
    # Maps (target, label) → msm_id for the fetch phase.
    pending: dict[tuple[str, str], int] = {}

    def _submit(target: str, pfx: str, asn: int, label: str) -> tuple[str, str, int | None]:
        msm_type, extra, _ = specs[label]
        msm_id = _create_measurement(api_key, label, msm_type, target, probe_ids, extra)
        return target, label, msm_id

    work = [(t, p, a, l) for t, p, a in all_targets for l in labels]
    with ThreadPoolExecutor(max_workers=_HTTP_WORKERS) as pool:
        futures = {pool.submit(_submit, t, p, a, l): (t, p, a) for t, p, a, l in work}
        for fut in as_completed(futures):
            target, pfx, asn = futures[fut]
            try:
                t, label, msm_id = fut.result()
                if msm_id is not None:
                    pending[(t, label)] = msm_id
            except Exception as exc:
                log.warning("Submit error for %s: %s", target, exc)

    log.info("Submitted %d measurements; waiting %d min for completion…",
             len(pending), wait_minutes)

    # ── Phase 2: wait once for all measurements to complete ───────────────────
    time.sleep(wait_minutes * 60)

    # ── Phase 3: fetch all results concurrently ───────────────────────────────
    # Build a lookup from msm_id back to (target, label, prefix, asn).
    msm_meta: dict[int, tuple[str, str, str, int]] = {}
    for (target, label), msm_id in pending.items():
        # Find prefix/asn for this target
        pfx_match, asn_match = _match_corpus(target, corpus_nets)
        if pfx_match is None:
            # Fall back to the prefix row we generated from
            for t2, p2, a2 in all_targets:
                if t2 == target:
                    pfx_match, asn_match = p2, a2
                    break
        if pfx_match and asn_match:
            msm_meta[msm_id] = (target, label, pfx_match, asn_match)

    raw_results: dict[int, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=_HTTP_WORKERS) as pool:
        futures = {pool.submit(_fetch_results, api_key, msm_id): msm_id
                   for msm_id in msm_meta}
        for fut in as_completed(futures):
            msm_id = futures[fut]
            try:
                raw_results[msm_id] = fut.result()
            except Exception as exc:
                log.warning("Fetch error for msm %s: %s", msm_id, exc)
                raw_results[msm_id] = []

    # ── Phase 4: parse and store ──────────────────────────────────────────────
    obs_rows: list[tuple] = []
    hop_rows: list[tuple] = []

    for msm_id, results in raw_results.items():
        if msm_id not in msm_meta:
            continue
        target, label, pfx, asn = msm_meta[msm_id]

        if label == "ping":
            responding, rtt = _parse_ping(results)
        elif label in _TCP_PORTS:
            responding, rtt = _parse_tcp(results)
        elif label == "traceroute":
            responding, rtt = _parse_tcp(results)
            for hop_addr in _parse_traceroute_hops(results, corpus_nets):
                hop_pfx, hop_asn = _match_corpus(hop_addr, corpus_nets)
                hop_rows.append((obs_date, hop_addr, hop_pfx, hop_asn, target, msm_id))
        else:
            continue

        obs_rows.append((
            obs_date, target, pfx, asn, label,
            responding > 0, rtt, len(probe_ids), responding, msm_id,
        ))

    if obs_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO atlas_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
            obs_rows,
        )
    if hop_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO atlas_hop_addresses VALUES (?,?,?,?,?,?)",
            hop_rows,
        )
    conn.close()

    responsive = sum(1 for r in obs_rows if r[5])
    unique_responsive = len({r[1] for r in obs_rows if r[5]})
    unique_hops = len({r[1] for r in hop_rows})

    log.info(
        "Sweep done: %d measurements, %d/%d responsive, "
        "%d unique responsive addresses, %d unique hop addresses",
        len(pending), responsive, len(obs_rows), unique_responsive, unique_hops,
    )
    return {
        "prefixes": len(prefix_rows),
        "targets": len(all_targets),
        "measurements_created": len(pending),
        "observations": len(obs_rows),
        "responsive_observations": responsive,
        "unique_responsive_addresses": unique_responsive,
        "unique_hop_addresses": unique_hops,
    }


def run_ripe_atlas_dns(
    db_path: str,
    api_key: str,
    targets: list[str] | None = None,
    observation_date: date_cls | None = None,
    probe_count: int = 5,
) -> dict:
    """Run Atlas AAAA lookups for hostnames lacking corpus prefix matches."""
    from usg_ipv6.db import get_connection, init_schema
    from usg_ipv6.pipeline.dotgov_dns import _build_prefix_nets, _match_prefix

    obs_date = observation_date or date_cls.today()
    conn = get_connection(db_path)
    init_schema(conn)

    if targets is None:
        try:
            rows = conn.execute(
                "SELECT DISTINCT hostname FROM domain_services "
                "WHERE matched_prefix IS NULL LIMIT 200"
            ).fetchall()
            targets = [r[0] for r in rows]
        except Exception:
            targets = []

    if not targets:
        conn.close()
        return {"targets": 0, "measurements": 0, "new_matches": 0}

    corpus_prefixes = conn.execute("""
        SELECT DISTINCT c.prefix, c.asn AS origin_asn
        FROM caida_pfx2as c
        JOIN asn_corpus ac ON ac.asn = c.asn
        WHERE c.af = 6
          AND c.snapshot_date = (SELECT MAX(snapshot_date) FROM caida_pfx2as WHERE af = 6)
    """).fetchall()
    nets = _build_prefix_nets(corpus_prefixes)

    probe_ids = _get_probes(api_key, limit=probe_count)
    if not probe_ids:
        conn.close()
        return {"targets": len(targets), "measurements": 0, "new_matches": 0}

    # Submit all DNS measurements concurrently
    def _submit_dns(hostname: str) -> tuple[str, int | None]:
        payload = {
            "definitions": [{
                "type": "dns",
                "description": f"USG IPv6 AAAA: {hostname}",
                "af": 6,
                "query_class": "IN",
                "query_type": "AAAA",
                "query_argument": hostname,
                "use_probe_resolver": True,
                "resolve_on_probe": True,
                "is_oneoff": True,
            }],
            "probes": [{
                "type": "probes",
                "value": ",".join(str(p) for p in probe_ids),
                "requested": len(probe_ids),
            }],
        }
        try:
            r = requests.post(
                f"{ATLAS_BASE}/measurements/",
                json=payload,
                headers={"Authorization": f"Key {api_key}"},
                timeout=30,
            )
            r.raise_for_status()
            return hostname, r.json()["measurements"][0]
        except Exception as exc:
            log.warning("Atlas DNS create failed for %s: %s", hostname, exc)
            return hostname, None

    pending_dns: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=_HTTP_WORKERS) as pool:
        for hostname, msm_id in pool.map(_submit_dns, targets):
            if msm_id is not None:
                pending_dns[hostname] = msm_id

    time.sleep(5 * 60)  # DNS measurements complete quickly

    new_matches = 0
    for hostname, msm_id in pending_dns.items():
        results = _fetch_results(api_key, msm_id)
        for probe_result in results:
            for rs in probe_result.get("resultset", [{}]):
                for answer in rs.get("result", {}).get("ANSWERS", []):
                    if answer.get("TYPE") == "AAAA":
                        addr = answer.get("RDATA", "")
                        pfx, asn = _match_prefix(addr, nets)
                        if pfx:
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
    return {
        "targets": len(targets),
        "measurements": len(pending_dns),
        "new_matches": new_matches,
    }
