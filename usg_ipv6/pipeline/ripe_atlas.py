"""
RIPE Atlas enrichment pipeline
================================
Uses RIPE Atlas to discover live IPv6 addresses within USG-allocated
prefixes that evade ICMP-based probing (TU Munich hitlist, CAIDA Ark)
due to firewall policy.  Many federal hosts block unsolicited ICMPv6 but
respond to TCP SYN on public-facing ports, DNS queries, or reveal
intermediate router addresses via traceroute.

Strategy
--------
For each corpus IPv6 prefix:

1. Generate candidate target addresses (low-numbered, pattern-based,
   and addresses already seen in other enrichment sources).
2. Issue per-target Atlas measurements:
     - ICMPv6 ping          (1 credit/probe)  — baseline
     - TCP connect/443      (10 credits/probe) — HTTPS servers
     - TCP connect/80       (10 credits/probe) — HTTP servers
     - TCP connect/53       (10 credits/probe) — DNS over TCP
     - Traceroute (ICMPv6)  (10 credits/probe) — extracts hop addresses
3. Parse results; store responsive targets in atlas_observations.
4. Extract hop addresses from traceroutes; store in atlas_hop_addresses.
   Routers respond to traceroute even when end-hosts do not.

Credit estimate (default settings, 530 corpus prefixes, 10 targets each,
20 US probes, all 5 measurement types):
  530 × 10 targets × 20 probes × (1+10+10+10+10) credits = 4,346,000

With significant credit availability this is fine; reduce probe_count or
disable measurement types to stay within tighter budgets.

API key
-------
Requires RIPE_ATLAS_API_KEY with "Create User Defined Measurements" scope.
Set as a GitHub Actions secret named RIPE_ATLAS_API_KEY.
"""

import ipaddress
import logging
import time
from datetime import date as date_cls
from typing import Iterator

import requests

log = logging.getLogger(__name__)

ATLAS_BASE = "https://atlas.ripe.net/api/v2"

# Measurement types to run per target address.
# Each entry: (label, atlas_type, extra_params, credits_per_probe)
_MEASUREMENT_SPECS: list[tuple[str, str, dict, int]] = [
    ("ping",      "ping",       {"af": 6, "packets": 3},                                          1),
    ("tcp_443",   "traceroute", {"af": 6, "protocol": "TCP", "destination_option_size": 0,
                                 "paris": 0, "response_timeout": 4000, "size": 0},               10),
    ("tcp_80",    "traceroute", {"af": 6, "protocol": "TCP", "destination_option_size": 0,
                                 "paris": 0, "response_timeout": 4000, "size": 0},               10),
    ("tcp_53",    "traceroute", {"af": 6, "protocol": "TCP", "destination_option_size": 0,
                                 "paris": 0, "response_timeout": 4000, "size": 0},               10),
    ("traceroute","traceroute", {"af": 6, "protocol": "ICMP", "paris": 16,
                                 "response_timeout": 4000, "size": 0, "max_hops": 32},           10),
]

# Ports for TCP measurements (index matches _MEASUREMENT_SPECS order 1-3)
_TCP_PORTS = {"tcp_443": 443, "tcp_80": 80, "tcp_53": 53}

# Low-numbered address offsets to probe within each prefix (host portion).
# These catch routers (::1, ::2), anycast (::a), NTP (::7b == 123), etc.
_PROBE_OFFSETS = [1, 2, 10, 0xa, 0x10, 0x53, 0x64, 0x7b, 0x100, 0x200]


# ── Target generation ─────────────────────────────────────────────────────────

def _generate_targets(prefix: str, extra_addrs: list[str] | None = None) -> list[str]:
    """
    Return candidate IPv6 addresses to probe within a prefix.

    Combines low-numbered host-part offsets with any addresses already
    known from other enrichment sources (CT logs, dotgov DNS, etc.)
    that fall within the prefix.
    """
    try:
        net = ipaddress.ip_network(prefix, strict=False)
    except ValueError:
        return []

    seen: set[str] = set()
    targets: list[str] = []

    base = int(net.network_address)
    host_bits = 128 - net.prefixlen

    for offset in _PROBE_OFFSETS:
        # Skip offset if it would set bits outside the host portion
        if offset >= (1 << host_bits):
            break
        addr = str(ipaddress.ip_address(base + offset))
        if addr not in seen:
            seen.add(addr)
            targets.append(addr)

    # Add caller-supplied addresses (from other enrichment) that fall in prefix
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
    """
    Return {prefix: [addr, ...]} mapping extra addresses already seen in
    domain_services or hitlist_addresses that fall within corpus prefixes.
    These are high-value seeds: Atlas confirms liveness via TCP/traceroute.
    """
    result: dict[str, list[str]] = {}
    try:
        rows = conn.execute(
            "SELECT ipv6_address, matched_prefix FROM domain_services WHERE matched_prefix IS NOT NULL"
        ).fetchall()
    except Exception:
        rows = []
    for addr, pfx in rows:
        if pfx:
            result.setdefault(pfx, []).append(addr)

    try:
        rows = conn.execute(
            "SELECT address, matched_prefix FROM hitlist_addresses WHERE matched_prefix IS NOT NULL"
        ).fetchall()
    except Exception:
        rows = []
    for addr, pfx in rows:
        if pfx:
            result.setdefault(pfx, []).append(addr)

    return result


# ── Probe selection ───────────────────────────────────────────────────────────

def _get_probes(
    api_key: str,
    country_code: str = "US",
    limit: int = 20,
    anchor_only: bool = False,
) -> list[int]:
    """
    Return Atlas probe IDs: connected, IPv6-capable, in the requested country.
    Anchors are preferred (more stable, better uptime) when anchor_only=True.
    Falls back to regular probes if not enough anchors are available.
    """
    params: dict = {
        "country_code": country_code,
        "status": 1,
        "tags": "system-ipv6-works",
        "format": "json",
        "page_size": limit,
    }
    if anchor_only:
        params["is_anchor"] = True

    r = requests.get(
        f"{ATLAS_BASE}/probes/",
        params=params,
        headers={"Authorization": f"Key {api_key}"},
        timeout=30,
    )
    r.raise_for_status()
    ids = [p["id"] for p in r.json().get("results", [])]

    # Top up with non-anchor probes if needed
    if len(ids) < limit:
        params.pop("is_anchor", None)
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
        log.debug("Created %s measurement %s → id %s", label, target, msm_id)
        return msm_id
    except Exception as exc:
        log.warning("Atlas measurement creation failed (%s %s): %s", label, target, exc)
        return None


# ── Result fetching ───────────────────────────────────────────────────────────

def _poll_results(
    api_key: str,
    msm_id: int,
    timeout_s: int = 300,
    poll_interval_s: int = 15,
) -> list[dict]:
    """
    Poll until the measurement is stopped/completed, then fetch results.
    Uses exponential-ish back-off up to poll_interval_s.
    """
    headers = {"Authorization": f"Key {api_key}"}
    deadline = time.monotonic() + timeout_s
    interval = min(5, poll_interval_s)

    while time.monotonic() < deadline:
        try:
            r = requests.get(
                f"{ATLAS_BASE}/measurements/{msm_id}/",
                headers=headers,
                timeout=15,
            )
            r.raise_for_status()
            status = r.json().get("status", {}).get("name", "")
            if status in ("Stopped", "Failed"):
                break
        except Exception as exc:
            log.debug("Atlas poll error for %s: %s", msm_id, exc)
        time.sleep(interval)
        interval = min(interval * 1.5, poll_interval_s)

    try:
        r = requests.get(
            f"{ATLAS_BASE}/measurements/{msm_id}/results/",
            headers=headers,
            params={"format": "json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("Atlas results fetch failed for %s: %s", msm_id, exc)
        return []


# ── Result parsing ────────────────────────────────────────────────────────────

def _parse_ping(results: list[dict]) -> tuple[int, float | None]:
    """Return (responding_probes, median_rtt_ms) from ping results."""
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
    """
    Return (responding_probes, median_rtt_ms) for TCP traceroute used as
    a TCP connect probe.  A host is 'responding' if the final hop equals
    the target (SYN-ACK received) or if last_hop RTT is finite.
    """
    responding = 0
    rtts: list[float] = []
    for r in results:
        hops = r.get("result", [])
        if not hops:
            continue
        last = hops[-1].get("result", [{}])
        for probe in last:
            rtt = probe.get("rtt")
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
    """
    Extract intermediate hop addresses from traceroute results that fall
    within any corpus prefix.  These are router/infrastructure addresses
    that would never appear in hitlist or Ark probing.
    """
    found: set[str] = set()
    for r in results:
        for hop in r.get("result", []):
            for probe in hop.get("result", [{}]):
                addr = probe.get("from")
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


def _build_corpus_nets(
    conn,
) -> list[tuple[ipaddress.IPv6Network, int, str]]:
    """Return list of (network, asn, prefix_str) for all corpus IPv6 prefixes."""
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
    """Return (prefix, asn) for the first corpus net containing addr."""
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
    """Return an estimated credit cost before committing to a sweep."""
    specs = {label: credits for label, _, _, credits in _MEASUREMENT_SPECS}
    labels = measurement_labels or [label for label, *_ in _MEASUREMENT_SPECS]
    cost_per_target = sum(specs[l] for l in labels if l in specs)
    return prefix_count * targets_per_prefix * probe_count * cost_per_target


# ── Main runners ──────────────────────────────────────────────────────────────

def run_prefix_sweep(
    db_path: str,
    api_key: str,
    observation_date: date_cls | None = None,
    probe_count: int = 20,
    country_code: str = "US",
    measurement_labels: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Sweep all corpus IPv6 prefixes with multi-type Atlas measurements.

    For each prefix, generates candidate target addresses and runs the
    requested measurement types from US-based Atlas probes.  Stores
    responsive addresses in atlas_observations and hop addresses
    discovered via traceroute in atlas_hop_addresses.

    Parameters
    ----------
    api_key           RIPE Atlas API key with measurement creation scope.
    probe_count       Number of Atlas probes per measurement (default 20).
    country_code      ISO 3166-1 alpha-2 for probe selection (default "US").
    measurement_labels  Subset of measurement types to run; default is all
                      five (ping, tcp_443, tcp_80, tcp_53, traceroute).
    dry_run           If True, generate targets and estimate credits but
                      create no measurements.
    """
    from usg_ipv6.db import get_connection, init_schema

    obs_date = observation_date or date_cls.today()
    labels = measurement_labels or [label for label, *_ in _MEASUREMENT_SPECS]
    specs = {label: (t, e, c) for label, t, e, c in _MEASUREMENT_SPECS}

    conn = get_connection(db_path)
    init_schema(conn)

    corpus_nets = _build_corpus_nets(conn)
    extra_by_prefix = _corpus_extra_addresses(conn)

    # All distinct corpus prefixes
    prefix_rows = conn.execute("""
        SELECT DISTINCT c.prefix, c.asn, ac.tier
        FROM caida_pfx2as c
        JOIN asn_corpus ac ON ac.asn = c.asn
        WHERE c.af = 6
          AND c.snapshot_date = (SELECT MAX(snapshot_date) FROM caida_pfx2as WHERE af = 6)
        ORDER BY c.prefix
    """).fetchall()

    all_targets: list[tuple[str, str, int]] = []  # (target, prefix, asn)
    for pfx, asn, _tier in prefix_rows:
        extras = extra_by_prefix.get(pfx, [])
        for target in _generate_targets(pfx, extras):
            all_targets.append((target, pfx, asn))

    est = estimate_credits(
        len(prefix_rows),
        len(_PROBE_OFFSETS),
        probe_count,
        labels,
    )
    log.info(
        "Atlas prefix sweep: %d prefixes, %d targets, %d probes, types=%s — est. %d credits",
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

    obs_rows: list[tuple] = []
    hop_rows: list[tuple] = []
    measurements_created = 0

    for target, pfx, asn in all_targets:
        for label in labels:
            msm_type, extra, _credits = specs[label]
            msm_id = _create_measurement(api_key, label, msm_type, target, probe_ids, extra)
            if msm_id is None:
                continue
            measurements_created += 1

            results = _poll_results(api_key, msm_id)
            if not results:
                continue

            if label == "ping":
                responding, rtt = _parse_ping(results)
            elif label in _TCP_PORTS:
                responding, rtt = _parse_tcp(results)
            elif label == "traceroute":
                # For traceroute: count probes that reached the target
                responding, rtt = _parse_tcp(results)
                # Also extract intermediate hops within corpus prefixes
                for hop_addr in _parse_traceroute_hops(results, corpus_nets):
                    hop_pfx, hop_asn = _match_corpus(hop_addr, corpus_nets)
                    hop_rows.append((
                        obs_date, hop_addr, hop_pfx, hop_asn,
                        target, msm_id,
                    ))
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
        "Atlas sweep done: %d measurements, %d/%d responsive obs, "
        "%d unique responsive addresses, %d unique hop addresses",
        measurements_created, responsive, len(obs_rows),
        unique_responsive, unique_hops,
    )
    return {
        "prefixes": len(prefix_rows),
        "targets": len(all_targets),
        "measurements_created": measurements_created,
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
    probe_count: int = 10,
) -> dict:
    """
    Run Atlas AAAA lookups for hostnames that lack corpus prefix matches.

    targets: list of hostnames to probe; if None, fetches from domain_services
             where matched_prefix IS NULL.
    """
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
        log.warning("No Atlas probes found")
        return {"targets": len(targets), "measurements": 0, "new_matches": 0}

    new_matches = 0
    measurements = 0

    for hostname in targets:
        payload = {
            "definitions": [{
                "type": "dns",
                "description": f"USG IPv6 AAAA lookup: {hostname}",
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
            msm_id = r.json()["measurements"][0]
        except Exception as exc:
            log.warning("Atlas DNS measurement failed for %s: %s", hostname, exc)
            continue
        measurements += 1

        results = _poll_results(api_key, msm_id)
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
    log.info("Atlas DNS: %d targets, %d measurements, %d new corpus matches",
             len(targets), measurements, new_matches)
    return {"targets": len(targets), "measurements": measurements, "new_matches": new_matches}
