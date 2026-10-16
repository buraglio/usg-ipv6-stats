"""Generate a static HTML report page from the DuckDB."""

import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = "data/usg_ipv6.duckdb"
DEFAULT_OUT = "web/index.html"

_TIER_LABELS = {
    "cfo_act": "CFO Act",
    "dod": "DoD",
    "doe_lab": "DOE Lab",
    "federal_civilian": "Federal",
    "ic": "Intel Community",
    "independent": "Independent",
    "judicial": "Judicial",
    "legislative": "Legislative",
}

_TIER_DESCRIPTIONS = {
    "cfo_act": "The 24 cabinet-level and largest executive branch agencies named in the Chief Financial Officers Act of 1990.",
    "dod": "Department of Defense components and commands.",
    "doe_lab": "Department of Energy national laboratories and ESnet (the DOE science network backbone).",
    "federal_civilian": "Independent federal agencies, commissions, and government corporations.",
    "ic": "Intelligence Community elements.",
    "independent": "Independent federal agencies and commissions.",
    "judicial": "Federal judicial branch entities.",
    "legislative": "Legislative branch agencies and support organizations.",
}


def _esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def _tier_label(t: str) -> str:
    return _TIER_LABELS.get(t, _esc(t))


def generate(db_path: str = DEFAULT_DB, out_path: str = DEFAULT_OUT) -> None:
    import duckdb

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    if not Path(db_path).exists():
        Path(out_path).write_text(_page_no_data(generated_at), encoding="utf-8")
        print(f"No DuckDB found — wrote placeholder to {out_path}")
        return

    conn = duckdb.connect(db_path, read_only=True)

    # ── Aggregate snapshot trend (chart data) ────────────────────────────────
    series_rows = conn.execute("""
        SELECT snapshot_ts::DATE::VARCHAR AS date,
               af,
               COUNT(DISTINCT prefix)     AS prefixes,
               COUNT(DISTINCT origin_asn) AS asns
        FROM prefix_records
        GROUP BY date, af
        ORDER BY date, af
    """).fetchall()

    # Build {date -> {4: {p,a}, 6: {p,a}}} for chart
    _dates_set: dict = {}
    for date, af, prefixes, asns in series_rows:
        _dates_set.setdefault(date, {})[af] = {"p": prefixes, "a": asns}
    chart_dates = sorted(_dates_set)
    # Use None (→ JSON null) when an AF has no data for a given date so Chart.js
    # spanGaps can skip the missing point rather than drawing a line to zero.
    chart_v6_prefixes = [_dates_set[d].get(6, {}).get("p") for d in chart_dates]
    chart_v4_prefixes = [_dates_set[d].get(4, {}).get("p") for d in chart_dates]
    chart_v6_asns = [_dates_set[d].get(6, {}).get("a") for d in chart_dates]
    chart_v4_asns = [_dates_set[d].get(4, {}).get("a") for d in chart_dates]

    # ── Latest-snapshot summary ───────────────────────────────────────────────
    # IPv4 (route-views2) and IPv6 (route-views6) ingest on separate schedules,
    # so each af has its own latest snapshot_ts.  Using a single MAX() would
    # show only whichever collector ran most recently.
    latest_ts_rows = conn.execute("""
        SELECT af, MAX(snapshot_ts) AS latest
        FROM prefix_records
        GROUP BY af
    """).fetchall()
    latest_ts_by_af: dict = {af: ts for af, ts in latest_ts_rows}
    latest_ts4 = latest_ts_by_af.get(4)
    latest_ts6 = latest_ts_by_af.get(6)
    # Keep a single sentinel for gating downstream queries that need any data.
    latest_ts = latest_ts4 or latest_ts6

    corpus_count = conn.execute("SELECT COUNT(*) FROM asn_corpus").fetchone()[0]

    totals: dict = {}
    for af, ts in latest_ts_by_af.items():
        row = conn.execute("""
            SELECT af,
                   COUNT(DISTINCT prefix)     AS prefixes,
                   COUNT(DISTINCT origin_asn) AS asns
            FROM prefix_records WHERE af = ? AND snapshot_ts = ?
            GROUP BY af
        """, [af, ts]).fetchone()
        if row:
            totals[af] = row

    # Routing-active: corpus ASNs visible in either the latest IPv4 or IPv6 snapshot.
    # Used as the adoption denominator — matches APNIC/NIST methodology and excludes
    # registered-but-inactive ASNs (e.g. DoD DISN-internal networks).
    if latest_ts_by_af:
        af_union = " UNION ALL ".join(
            f"SELECT DISTINCT origin_asn FROM prefix_records "
            f"WHERE af = {af} AND snapshot_ts = '{ts}'"
            for af, ts in latest_ts_by_af.items()
        )
        row = conn.execute(f"""
            SELECT COUNT(DISTINCT origin_asn)
            FROM ({af_union}) active
            WHERE origin_asn IN (SELECT asn FROM asn_corpus)
        """).fetchone()
        routing_active_count = row[0] if row else 0
    else:
        routing_active_count = 0

    # ── By-tier ──────────────────────────────────────────────────────────────
    # LEFT JOIN from corpus so every tier appears, even those with no current
    # routing data (e.g. DoD DISN-internal ASNs with 0 public BGP presence).
    if latest_ts_by_af:
        af_clauses = " UNION ALL ".join(
            f"SELECT * FROM prefix_records WHERE af = {af} AND snapshot_ts = '{ts}'"
            for af, ts in latest_ts_by_af.items()
        )
        by_tier = conn.execute(f"""
            WITH latest AS ({af_clauses})
            SELECT c.tier,
                   pr.af,
                   COALESCE(COUNT(DISTINCT pr.prefix), 0)     AS prefixes,
                   COALESCE(COUNT(DISTINCT pr.origin_asn), 0) AS asns,
                   COUNT(DISTINCT c.asn) AS corpus_asns
            FROM asn_corpus c
            LEFT JOIN latest pr ON pr.origin_asn = c.asn
            GROUP BY c.tier, pr.af
            ORDER BY MIN(
                CASE c.tier
                  WHEN 'cfo_act'          THEN 0
                  WHEN 'dod'              THEN 1
                  WHEN 'doe_lab'          THEN 2
                  WHEN 'federal_civilian' THEN 3
                  WHEN 'legislative'      THEN 4
                  WHEN 'judicial'         THEN 5
                  ELSE 9 END
            ), pr.af NULLS LAST
        """).fetchall()
    else:
        by_tier = []

    # ── Corpus ───────────────────────────────────────────────────────────────
    corpus_rows = conn.execute("""
        SELECT asn, agency, tier, confidence, org_name, sources, last_verified
        FROM asn_corpus ORDER BY tier, agency, asn
    """).fetchall()

    # ── Per-ASN current prefixes (for ASN explorer) ───────────────────────────
    # Prefer caida_pfx2as for each AF (proper CIDR notation via ip_network()).
    # Fall back to prefix_records for AFs missing from caida_pfx2as — rv2 IPv4
    # downloads are non-fatal so the table may have only af=6 rows.
    caida_afs = {
        row[0]
        for row in conn.execute("SELECT DISTINCT af FROM caida_pfx2as").fetchall()
    }
    clauses = []
    for af in (4, 6):
        if af in caida_afs:
            clauses.append(
                f"SELECT DISTINCT asn AS origin_asn, {af} AS af, prefix"
                f" FROM caida_pfx2as"
                f" WHERE asn IN (SELECT asn FROM asn_corpus) AND af = {af}"
                f" AND snapshot_date = (SELECT MAX(snapshot_date) FROM caida_pfx2as WHERE af = {af})"
            )
        elif af in (latest_ts_by_af or {}):
            clauses.append(
                f"SELECT DISTINCT origin_asn, {af} AS af, prefix"
                f" FROM prefix_records"
                f" WHERE origin_asn IN (SELECT asn FROM asn_corpus)"
                f" AND af = {af} AND snapshot_ts = '{latest_ts_by_af[af]}'"
            )
    if clauses:
        asn_prefix_rows = conn.execute(
            " UNION ALL ".join(clauses) + " ORDER BY origin_asn, af, prefix"
        ).fetchall()
    elif latest_ts_by_af:
        af_clauses_asn = " UNION ALL ".join(
            f"SELECT * FROM prefix_records WHERE af = {af} AND snapshot_ts = '{ts}'"
            for af, ts in latest_ts_by_af.items()
        )
        asn_prefix_rows = conn.execute(f"""
            SELECT DISTINCT origin_asn, af, prefix
            FROM ({af_clauses_asn})
            WHERE origin_asn IN (SELECT asn FROM asn_corpus)
            ORDER BY origin_asn, af, prefix
        """).fetchall()
    else:
        asn_prefix_rows = []

    # Group prefixes per ASN
    asn_prefixes: dict = {}
    for asn, af, prefix in asn_prefix_rows:
        d = asn_prefixes.setdefault(str(asn), {"v4": [], "v6": []})
        d["v4" if af == 4 else "v6"].append(prefix)

    # ── Per-ASN history (counts per date) ────────────────────────────────────
    asn_history_rows = conn.execute("""
        SELECT origin_asn, snapshot_ts::DATE::VARCHAR AS date, af,
               COUNT(DISTINCT prefix) AS cnt
        FROM prefix_records
        WHERE origin_asn IN (SELECT asn FROM asn_corpus)
        GROUP BY origin_asn, date, af
        ORDER BY origin_asn, date
    """).fetchall()

    asn_history: dict = {}
    for asn, date, af, cnt in asn_history_rows:
        h = asn_history.setdefault(str(asn), {})
        e = h.setdefault(date, {"v4": 0, "v6": 0})
        e["v4" if af == 4 else "v6"] = cnt

    # ── Snapshot inventory ────────────────────────────────────────────────────
    snap_rows = conn.execute("""
        SELECT collector,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'processed' THEN 1 ELSE 0 END) AS processed,
               SUM(CASE WHEN status = 'failed'    THEN 1 ELSE 0 END) AS failed,
               MIN(snapshot_ts)::VARCHAR AS earliest,
               MAX(snapshot_ts)::VARCHAR AS latest
        FROM snapshots
        GROUP BY collector ORDER BY collector
    """).fetchall()

    # ── IPv6 reachability ─────────────────────────────────────────────────────
    ark_count = conn.execute("SELECT COUNT(*) FROM ark_reachability").fetchone()[0]
    ark_data = None
    if ark_count > 0:
        mdate = conn.execute(
            "SELECT MAX(measurement_date) FROM ark_reachability"
        ).fetchone()[0]
        pfx2as_date = conn.execute(
            "SELECT pfx2as_snapshot_date FROM ark_reachability "
            "WHERE measurement_date = ? LIMIT 1", [mdate]
        ).fetchone()[0]
        totals_ark = conn.execute("""
            SELECT
              COUNT(*) FILTER (WHERE ipv6_reachable = TRUE  AND prefixes_in_scope > 0),
              COUNT(*) FILTER (WHERE ipv6_reachable = FALSE AND prefixes_in_scope > 0),
              COUNT(*) FILTER (WHERE prefixes_in_scope = 0)
            FROM ark_reachability WHERE measurement_date = ?
        """, [mdate]).fetchone()
        gap_rows = conn.execute("""
            SELECT ar.asn, c.agency, c.tier, ar.addresses_observed, ar.prefixes_in_scope
            FROM ark_reachability ar
            JOIN asn_corpus c ON ar.asn = c.asn
            WHERE ar.measurement_date = ?
              AND ar.ipv6_reachable = FALSE AND ar.prefixes_in_scope > 0
            ORDER BY c.tier, c.agency
        """, [mdate]).fetchall()
        ark_data = {
            "mdate": str(mdate),
            "pfx2as_date": str(pfx2as_date),
            "reachable": totals_ark[0],
            "not_reachable": totals_ark[1],
            "indeterminate": totals_ark[2],
            "gap_rows": gap_rows,
        }

    # ── IPv6 Hitlist ──────────────────────────────────────────────────────────
    hitlist_data = None
    hitlist_asn_map: dict = {}
    try:
        hitlist_count = conn.execute(
            "SELECT COUNT(*) FROM hitlist_observations"
        ).fetchone()[0]
    except Exception:
        hitlist_count = 0

    if hitlist_count > 0:
        obs_date = conn.execute(
            "SELECT MAX(observation_date) FROM hitlist_observations"
        ).fetchone()[0]

        summary = conn.execute("""
            SELECT SUM(responsive_count), SUM(aliased_count),
                   COUNT(CASE WHEN responsive_count > 0 THEN 1 END),
                   MAX(hitlist_date)
            FROM hitlist_observations WHERE observation_date = ?
        """, [obs_date]).fetchone()

        agency_rows = conn.execute("""
            SELECT c.agency, c.tier, ho.asn,
                   ho.responsive_count, ho.aliased_count,
                   ho.responsive_count - ho.aliased_count AS likely_real
            FROM hitlist_observations ho
            JOIN asn_corpus c ON ho.asn = c.asn
            WHERE ho.observation_date = ?
            ORDER BY ho.responsive_count DESC, c.agency
        """, [obs_date]).fetchall()

        if latest_ts:
            gap_rows_hitlist = conn.execute("""
                SELECT c.agency, c.tier, ho.asn
                FROM hitlist_observations ho
                JOIN asn_corpus c ON ho.asn = c.asn
                WHERE ho.observation_date = ?
                  AND ho.responsive_count = 0
                  AND ho.asn IN (
                      SELECT DISTINCT origin_asn FROM prefix_records
                      WHERE af = 6 AND snapshot_ts = ?
                  )
                ORDER BY c.tier, c.agency
            """, [obs_date, latest_ts]).fetchall()
        else:
            gap_rows_hitlist = []

        trend_rows = conn.execute("""
            SELECT observation_date::VARCHAR AS date,
                   SUM(responsive_count) AS responsive,
                   SUM(aliased_count)    AS aliased
            FROM hitlist_observations
            GROUP BY observation_date
            ORDER BY observation_date
        """).fetchall()

        hitlist_data = {
            "obs_date": str(obs_date),
            "hitlist_date": str(summary[3]),
            "total_responsive": summary[0] or 0,
            "total_aliased": summary[1] or 0,
            "asns_with_responses": summary[2] or 0,
            "corpus_count": corpus_count,
            "agency_rows": agency_rows,
            "gap_rows": gap_rows_hitlist,
            "trend_rows": trend_rows,
        }

        asn_hl_rows = conn.execute("""
            SELECT asn, responsive_count, aliased_count
            FROM hitlist_observations WHERE observation_date = ?
        """, [obs_date]).fetchall()
        hitlist_asn_map = {
            str(r[0]): {"responsive": r[1], "aliased": r[2]}
            for r in asn_hl_rows
        }

    # ── Enrichment data ───────────────────────────────────────────────────────
    enrichment_data: dict | None = None
    rpki_by_asn: dict = {}
    try:
        tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
        enrich: dict = {}

        if "rpki_validity" in tables:
            row = conn.execute("""
                SELECT check_date::VARCHAR,
                       COUNT(*) FILTER (WHERE status = 'valid')     AS valid_,
                       COUNT(*) FILTER (WHERE status = 'invalid')   AS invalid_,
                       COUNT(*) FILTER (WHERE status = 'not_found') AS not_found
                FROM rpki_validity
                WHERE check_date = (SELECT MAX(check_date) FROM rpki_validity)
                GROUP BY check_date
            """).fetchone()
            if row:
                enrich["rpki"] = {
                    "date": row[0], "valid": row[1], "invalid": row[2], "not_found": row[3],
                }
            for r in conn.execute("""
                SELECT origin_asn,
                       COUNT(*) FILTER (WHERE status = 'valid')     AS v,
                       COUNT(*) FILTER (WHERE status = 'invalid')   AS i,
                       COUNT(*) FILTER (WHERE status = 'not_found') AS nf
                FROM rpki_validity
                WHERE check_date = (SELECT MAX(check_date) FROM rpki_validity)
                GROUP BY origin_asn
            """).fetchall():
                rpki_by_asn[str(r[0])] = {"v": r[1], "i": r[2], "nf": r[3]}

        if "domain_services" in tables:
            row = conn.execute("""
                SELECT observation_date::VARCHAR,
                       COUNT(DISTINCT domain)  AS domains,
                       COUNT(DISTINCT ipv6_address) FILTER (WHERE matched_prefix IS NOT NULL) AS matched_addrs,
                       COUNT(DISTINCT matched_prefix) FILTER (WHERE matched_prefix IS NOT NULL) AS matched_pfx
                FROM domain_services
                WHERE observation_date = (SELECT MAX(observation_date) FROM domain_services)
                GROUP BY observation_date
            """).fetchone()
            if row:
                enrich["domain"] = {
                    "date": row[0], "domains": row[1],
                    "matched_addrs": row[2], "matched_pfx": row[3],
                }
            src_rows = conn.execute("""
                SELECT source, COUNT(DISTINCT domain) AS domains,
                       COUNT(*) FILTER (WHERE matched_prefix IS NOT NULL) AS matches
                FROM domain_services
                WHERE observation_date = (SELECT MAX(observation_date) FROM domain_services)
                GROUP BY source ORDER BY source
            """).fetchall()
            enrich["domain_sources"] = [(r[0], r[1], r[2]) for r in src_rows]

        if "nist_deploymon" in tables:
            row = conn.execute("""
                SELECT measurement_date::VARCHAR,
                       COUNT(DISTINCT domain) AS domains,
                       SUM(v6_func_count) FILTER (WHERE service = 'dns')  AS dns_func,
                       SUM(v6_func_count) FILTER (WHERE service = 'mail') AS mail_func,
                       SUM(v6_func_count) FILTER (WHERE service = 'web')  AS web_func,
                       SUM(CASE WHEN dnssec_chained THEN 1 ELSE 0 END) AS dnssec_chained
                FROM nist_deploymon
                WHERE measurement_date = (SELECT MAX(measurement_date) FROM nist_deploymon)
                GROUP BY measurement_date
            """).fetchone()
            if row:
                enrich["nist"] = {
                    "date": row[0], "domains": row[1],
                    "dns_func": row[2] or 0, "mail_func": row[3] or 0,
                    "web_func": row[4] or 0, "dnssec_chained": row[5] or 0,
                }

        if "dnssec_status" in tables:
            row = conn.execute("""
                SELECT check_date::VARCHAR,
                       COUNT(*)                            AS total,
                       COUNT(*) FILTER (WHERE has_dnskey)  AS signed,
                       COUNT(*) FILTER (WHERE has_ds)      AS delegated,
                       COUNT(*) FILTER (WHERE ad_validated) AS validated
                FROM dnssec_status
                WHERE check_date = (SELECT MAX(check_date) FROM dnssec_status)
                GROUP BY check_date
            """).fetchone()
            if row:
                enrich["dnssec"] = {
                    "date": row[0], "total": row[1],
                    "signed": row[2], "delegated": row[3], "validated": row[4],
                }
            algo_rows = conn.execute("""
                SELECT algorithms, COUNT(*) AS cnt
                FROM dnssec_status
                WHERE check_date = (SELECT MAX(check_date) FROM dnssec_status)
                  AND algorithms IS NOT NULL
                GROUP BY algorithms ORDER BY cnt DESC LIMIT 5
            """).fetchall()
            enrich["dnssec_algos"] = [(r[0], r[1]) for r in algo_rows]

        if "bgp_communities" in tables:
            row = conn.execute("""
                SELECT snapshot_ts::VARCHAR,
                       COUNT(DISTINCT community)             AS unique_communities,
                       COUNT(DISTINCT prefix)                AS prefixes_with_comm,
                       COUNT(*) FILTER (WHERE community LIKE '%:666') AS blackhole_rows
                FROM bgp_communities
                WHERE snapshot_ts = (SELECT MAX(snapshot_ts) FROM bgp_communities)
                GROUP BY snapshot_ts
            """).fetchone()
            if row:
                enrich["communities"] = {
                    "date": row[0][:10], "unique": row[1],
                    "prefixes": row[2], "blackhole": row[3],
                }

        if enrich:
            enrichment_data = enrich
    except Exception:
        pass

    conn.close()

    # ── Build embedded JSON for client-side JS ────────────────────────────────
    js_data = json.dumps({
        "dates": chart_dates,
        "v6_prefixes": chart_v6_prefixes,
        "v4_prefixes": chart_v4_prefixes,
        "v6_asns": chart_v6_asns,
        "v4_asns": chart_v4_asns,
        "asn_prefixes": asn_prefixes,
        "asn_history": asn_history,
        "hitlist_asns": hitlist_asn_map,
        "hitlist_dates": [r[0] for r in hitlist_data["trend_rows"]] if hitlist_data else [],
        "hitlist_responsive": [r[1] for r in hitlist_data["trend_rows"]] if hitlist_data else [],
        "rpki_by_asn": rpki_by_asn,
    }, separators=(",", ":"))

    html_out = _page(
        generated_at=generated_at,
        latest_ts=latest_ts,
        corpus_count=corpus_count,
        routing_active_count=routing_active_count,
        corpus_rows=corpus_rows,
        snap_rows=snap_rows,
        totals=totals,
        by_tier=by_tier,
        ark_data=ark_data,
        hitlist_data=hitlist_data,
        enrichment_data=enrichment_data,
        js_data=js_data,
    )
    Path(out_path).write_text(html_out, encoding="utf-8")
    print(f"Report written to {out_path}")


# ── HTML assembly ─────────────────────────────────────────────────────────────

def _page_no_data(generated_at: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>USG IPv6 Routing Statistics</title>
<style>{_CSS}</style></head><body>
<header>
  <h1>USG IPv6 Routing Statistics</h1>
  <p class="subtitle">IPv4 and IPv6 prefix deployment for US Government Autonomous Systems</p>
  <p class="meta">Generated: {_esc(generated_at)}</p>
</header>
<main>
  <div class="placeholder">
    <h3>No data yet</h3>
    <p>Run the <strong>RouteViews MRT Ingest</strong> workflow to populate data, then the
    <strong>CAIDA Ark IPv6 Reachability Ingest</strong> workflow for reachability metrics.</p>
  </div>
</main>
<footer><p>Source: <a href="https://github.com/buraglio/usg-ipv6-stats">github.com/buraglio/usg-ipv6-stats</a></p></footer>
</body></html>"""


def _page(
    generated_at, latest_ts, corpus_count, routing_active_count, corpus_rows,
    snap_rows, totals, by_tier, ark_data, hitlist_data, enrichment_data, js_data,
) -> str:
    snap_label = f"Latest snapshot: {latest_ts}" if latest_ts else "No routing data yet"

    ipv6 = totals.get(6)
    ipv4 = totals.get(4)

    # Adoption % = IPv6-announcing ASNs / routing-active ASNs (IPv4 or IPv6 in latest snapshot).
    # Registered-but-inactive ASNs (e.g. DoD DISN-internal networks not visible in public BGP)
    # are excluded from the denominator, matching APNIC/NIST/Google methodology.
    ipv6_asn_count = ipv6[2] if ipv6 else 0
    adoption_pct = (f"{100 * ipv6_asn_count / routing_active_count:.1f}%"
                    if routing_active_count else "—")

    def _card(cls, label, value, sub=""):
        return (f'<div class="stat-card {cls}">'
                f'<div class="label">{label}</div>'
                f'<div class="value">{value}</div>'
                f'{"<div class=sub>" + sub + "</div>" if sub else ""}</div>')

    cards = "".join([
        _card("ipv6", "IPv6 Adoption",
              adoption_pct if routing_active_count else "—",
              f"{ipv6_asn_count} of {routing_active_count} routing-active ASNs"
              if routing_active_count else "no data"),
        _card("ipv6b", "IPv6 Prefixes",
              f"{ipv6[1]:,}" if ipv6 else "—",
              "visible in BGP table" if ipv6 else "run RouteViews ingest"),
        _card("ipv4", "IPv4 Prefixes",
              f"{ipv4[1]:,}" if ipv4 else "—",
              f"from {ipv4[2]:,} ASNs" if ipv4 else "no data"),
        _card("corpus", "Corpus ASNs",
              f"{corpus_count:,}",
              f"{routing_active_count:,} routing-active in BGP"),
    ])

    # ── By-tier table ─────────────────────────────────────────────────────────
    if by_tier:
        tier_body_parts = []
        for r in by_tier:
            tier, af, prefixes, asns, corpus_asns = r
            if af is None:
                # Tier exists in corpus but has no routing data in latest snapshot
                tier_body_parts.append(
                    f"<tr class='tier-inactive'>"
                    f"<td>{_esc(_tier_label(tier))}</td>"
                    f"<td><span class='tag inact'>not routing</span></td>"
                    f"<td class='num muted'>0</td>"
                    f"<td class='num muted'>0 / {corpus_asns:,}</td></tr>"
                )
            else:
                tier_body_parts.append(
                    f"<tr><td>{_esc(_tier_label(tier))}</td>"
                    f"<td>{_af_tag(af)}</td>"
                    f"<td class='num'>{prefixes:,}</td>"
                    f"<td class='num'>{asns:,}</td></tr>"
                )
        tier_tbl = _table(["Tier", "AF", "Prefixes", "ASNs (routing / corpus)"], "".join(tier_body_parts))
    else:
        tier_tbl = "<p class='empty'>No routing data yet — run the RouteViews MRT Ingest workflow.</p>"

    # ── Snapshot inventory ─────────────────────────────────────────────────────
    if snap_rows:
        snap_body = "".join(
            f"<tr><td><code>{_esc(r[0])}</code></td>"
            f"<td class='num'>{r[1]:,}</td><td class='num'>{r[2]:,}</td>"
            f"<td class='num'>{r[3]:,}</td>"
            f"<td>{_esc(r[4] or '—')}</td><td>{_esc(r[5] or '—')}</td></tr>"
            for r in snap_rows
        )
        snap_tbl = _table(
            ["Collector", "Total Snapshots", "Processed", "Failed", "Earliest", "Latest"],
            snap_body,
        )
    else:
        snap_tbl = "<p class='empty'>No snapshots recorded yet.</p>"

    # ── IPv6 reachability section ─────────────────────────────────────────────
    ark_section = _ark_section(ark_data)

    # ── Hitlist section ───────────────────────────────────────────────────────
    hl_section = _hitlist_section(hitlist_data)

    # ── Corpus table (used by ASN explorer JS too) ────────────────────────────
    corpus_body = ""
    if corpus_rows:
        for r in corpus_rows:
            asn, agency, tier, conf, org, sources, verified = r
            corpus_body += (
                f"<tr class='asn-row' data-asn='{_esc(asn)}' "
                f"data-agency='{_esc(agency).lower()}' "
                f"data-tier='{_esc(tier)}' "
                f"onclick='showASN({_esc(asn)})'>"
                f"<td><code>{_esc(asn)}</code></td>"
                f"<td>{_esc(agency)}</td>"
                f"<td><span class='tier-tag tier-{_esc(tier)}'>{_tier_label(tier)}</span></td>"
                f"<td>{_esc(conf)}</td>"
                f"<td class='small'>{_esc(org)}</td>"
                f"<td class='small'>{_esc(verified)}</td></tr>"
            )
        corpus_tbl = _table(
            ["ASN", "Agency", "Tier", "Confidence", "Registered Org", "Verified"],
            corpus_body,
            id="corpus-table",
        )
    else:
        corpus_tbl = "<p class='empty'>Run the Corpus Update workflow to populate the ASN list.</p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>USG IPv6 Routing Statistics</title>
<style>{_CSS}</style>
</head>
<body>

<header>
  <h1>USG IPv6 Routing Statistics</h1>
  <p class="subtitle">IPv4 and IPv6 prefix deployment tracking for US Government Autonomous Systems</p>
  <p class="meta">{_esc(snap_label)}&nbsp;&nbsp;·&nbsp;&nbsp;Generated: {_esc(generated_at)}</p>
</header>

<nav>
  <a href="#overview">Overview</a>
  <a href="#trends">Trends</a>
  <a href="#by-tier">By Tier</a>
  <a href="#reachability">IPv6 Reachability</a>
  <a href="#hitlist">Live Endpoints</a>
  <a href="#enrichment">Enrichment</a>
  <a href="#asn-explorer">ASN Explorer</a>
  <a href="#data-sources">Data Sources</a>
  <a href="#disclaimer">Disclaimer</a>
</nav>

<div class="data-notice">
  All statistics are derived from public data sources. See the
  <a href="#data-sources">Data Sources</a> section for details.
  This dashboard strives for accuracy, but figures may temporarily skew
  during database rebuilds or historical backfill runs.
</div>

<main>

<section id="overview">
  <h2>Overview</h2>
  <p class="section-blurb">
    This dashboard tracks IPv4 and IPv6 prefix announcements for US Government
    autonomous systems (ASNs) as observed in the global BGP routing table via
    <a href="http://www.routeviews.org/">RouteViews</a> collectors. Prefix counts
    reflect distinct network blocks visible at the time of the snapshot.
    An ASN is counted as "IPv6 active" when it originates at least one IPv6 prefix.
    The IPv6 adoption percentage is measured against <em>routing-active</em> ASNs —
    those originating at least one prefix (IPv4 or IPv6) in the most recent snapshot —
    matching the methodology used by
    <a href="https://stats.labs.apnic.net/ipv6/">APNIC</a> and
    <a href="https://www.nist.gov/publications/measuring-ipv6-adoption">NIST</a>.
    ASNs registered in ARIN but not visible in the public BGP table
    (for example, DoD networks operating on the classified DISN) remain in the
    corpus for completeness but are excluded from the adoption denominator.
  </p>
  <div class="stat-grid">{cards}</div>
</section>

<section id="trends">
  <h2>Prefix Announcement Trends</h2>
  <p class="section-blurb">
    IPv6 and IPv4 prefix counts over each ingested RouteViews snapshot.
    A rising IPv6 line indicates growing IPv6 deployment across the USG corpus.
    Toggle address families using the legend.
  </p>
  <div class="chart-wrap card">
    <div class="chart-toolbar">
      <button id="trend-zoom-reset" class="zoom-reset-btn" onclick="if(trendChart)trendChart.resetZoom()" hidden>Reset zoom</button>
    </div>
    <canvas id="trend-chart" height="120"></canvas>
    <p class="chart-hint">Drag to zoom &nbsp;·&nbsp; scroll to zoom &nbsp;·&nbsp; double-click to reset</p>
  </div>
</section>

<section id="by-tier">
  <h2>Deployment by Tier</h2>
  <p class="section-blurb">
    ASNs in the corpus are grouped by federal tier.
    <strong>CFO Act</strong> agencies are the 24 cabinet-level departments named in
    the Chief Financial Officers Act of 1990 — the primary focus of OMB IPv6 mandates
    (M-21-07 and successors). Additional tiers (DoD, intelligence community, independent
    agencies) are added as the corpus expands.
  </p>
  {tier_tbl}
</section>

{ark_section}

{hl_section}

{_enrichment_section(enrichment_data)}

<section id="asn-explorer">
  <h2>ASN Explorer</h2>
  <p class="section-blurb">
    Browse all {corpus_count:,} ASNs in the tracking corpus
    ({routing_active_count:,} currently routing-active in the BGP table).
    Search by agency name or ASN number, then click any row to see
    that ASN's current prefix announcements and historical trend.
    ASNs with no prefix history are registered in ARIN but not visible
    in public RouteViews data.
  </p>
  <div class="search-bar">
    <input type="search" id="asn-search" placeholder="Search agency or ASN…"
           oninput="filterASNs(this.value)" autocomplete="off">
    <span id="asn-count" class="asn-count"></span>
    <button id="asn-dark-toggle" class="dark-toggle" onclick="toggleDarkASNs()">Show dark ASNs</button>
  </div>

  <div id="asn-detail" class="asn-detail" style="display:none">
    <div class="asn-detail-header">
      <strong id="detail-title"></strong>
      <button onclick="closeASN()" class="close-btn">✕ Close</button>
    </div>
    <div class="asn-detail-body">
      <div class="asn-prefixes">
        <div>
          <h4>IPv6 Prefixes <span id="detail-v6-count" class="count-badge v6"></span></h4>
          <div id="detail-v6" class="prefix-list"></div>
        </div>
        <div>
          <h4>IPv4 Prefixes <span id="detail-v4-count" class="count-badge v4"></span></h4>
          <div id="detail-v4" class="prefix-list"></div>
        </div>
      </div>
      <div id="detail-hitlist" class="detail-hitlist" style="display:none">
        <h4>Live IPv6 endpoints (Hitlist)</h4>
        <div class="hitlist-chips">
          <span class="hchip responsive">Responsive: <strong id="detail-hl-responsive">—</strong></span>
          <span class="hchip aliased">Aliased: <strong id="detail-hl-aliased">—</strong></span>
          <span class="hchip real">Likely real: <strong id="detail-hl-real">—</strong></span>
        </div>
      </div>
      <div id="detail-rpki" class="detail-hitlist" style="display:none">
        <h4>RPKI Validity (latest check)</h4>
        <div class="hitlist-chips">
          <span class="hchip rpki-valid">Valid ROA: <strong id="detail-rpki-valid">—</strong></span>
          <span class="hchip rpki-invalid">Invalid: <strong id="detail-rpki-invalid">—</strong></span>
          <span class="hchip rpki-nf">No ROA: <strong id="detail-rpki-nf">—</strong></span>
        </div>
      </div>
      <div class="asn-chart-wrap">
        <h4>Prefix announcement history</h4>
        <canvas id="asn-chart" height="80"></canvas>
      </div>
    </div>
  </div>

  <div class="card" style="overflow-x:auto">
    {corpus_tbl}
  </div>
</section>

<section id="data-sources">
  <h2>Data Sources &amp; Snapshot Inventory</h2>
  <p class="section-blurb">
    BGP routing table snapshots (MRT RIB dumps) are fetched from
    <a href="http://www.routeviews.org/">RouteViews</a> collectors.
    <code>route-views2</code> provides the global IPv4 table;
    <code>route-views6</code> provides the global IPv6 table.
    Each snapshot captures a full BGP RIB at a point in time, filtered to
    prefixes originated by ASNs in the USG corpus.
    Prefix-to-ASN attribution uses
    <a href="https://www.caida.org/catalog/datasets/routeviews-prefix2as/">CAIDA pfx2as</a>
    derived from the same RouteViews data.
    The USG ASN corpus is built from
    <a href="https://www.arin.net/resources/registry/whois/">ARIN WHOIS</a> records and
    <a href="https://www.caida.org/catalog/datasets/as-organizations/">CAIDA AS2Org</a>
    organization data, with manual verification for ambiguous entries.
  </p>
  {snap_tbl}
</section>

<section id="disclaimer">
  <h2>Disclaimer</h2>
  <div class="disclaimer-box">
    <p>
      All data presented on this dashboard is derived entirely from <strong>publicly
      available data sources</strong>, including RouteViews MRT RIB dumps, ARIN WHOIS
      records, CAIDA AS2Org and prefix-to-ASN datasets, and the TU Munich IPv6 Hitlist.
      No non-public, classified, or proprietary data is used. See the
      <a href="#data-sources">Data Sources</a> section for the full inventory of
      ingested snapshots.
    </p>
    <p>
      This dashboard strives for accuracy, but statistics may temporarily reflect
      incomplete or transitional states in the following circumstances:
    </p>
    <ul>
      <li>
        <strong>Historical backfill runs</strong> — when older snapshots are being
        ingested to extend the trend charts, intermediate states will show fewer
        data points than the final result.
      </li>
      <li>
        <strong>Database rebuilds</strong> — if the underlying DuckDB is rebuilt
        from scratch or re-seeded, prefix counts and adoption percentages may
        appear lower than normal until all collectors have re-ingested current data.
      </li>
      <li>
        <strong>Corpus updates</strong> — when ASNs are added to or removed from
        the tracking corpus, adoption rates will shift to reflect the new set of
        monitored autonomous systems.
      </li>
    </ul>
    <p>
      The ASN corpus is maintained on a best-effort basis. ASN-to-agency mappings
      are derived from ARIN registration data and CAIDA organization records, which
      may lag real-world organizational changes. Some registered ASNs may not be
      visible in the public BGP table (for example, DoD networks operating on the
      classified DISN backbone) and are excluded from adoption rate calculations.
    </p>
    <p class="disclaimer-source">
      Source code and methodology:
      <a href="https://github.com/buraglio/usg-ipv6-stats">github.com/buraglio/usg-ipv6-stats</a>
    </p>
  </div>
</section>

</main>

<footer>
  <p>
    Source data:
    <a href="http://www.routeviews.org/">RouteViews MRT</a> ·
    <a href="https://www.arin.net/resources/registry/whois/">ARIN WHOIS</a> ·
    <a href="https://www.caida.org/catalog/datasets/as-organizations/">CAIDA AS2Org</a> ·
    <a href="https://publicdata.caida.org/datasets/routing/routeviews6-prefix2as/">CAIDA pfx2as rv6</a> ·
    <a href="https://ipv6hitlist.github.io/">TU Munich IPv6 Hitlist</a> ·
    <a href="https://github.com/cisagov/dotgov-data">CISA dotgov-data</a> ·
    <a href="https://usgv6-deploymon.nist.gov/cgi-bin/generate-gov">NIST USGv6</a> ·
    <a href="https://crt.sh">crt.sh CT</a> ·
    <a href="https://rpki.cloudflare.com">Cloudflare RPKI</a>
  </p>
  <p style="margin-top:0.4rem">
    <a href="https://github.com/buraglio/usg-ipv6-stats">github.com/buraglio/usg-ipv6-stats</a>
  </p>
</footer>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"
  crossorigin="anonymous" referrerpolicy="no-referrer"></script>
<script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"
  crossorigin="anonymous" referrerpolicy="no-referrer"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"
  crossorigin="anonymous" referrerpolicy="no-referrer"></script>
<script>
const DATA = {js_data};

// Chart instances — declared before any IIFE so TDZ can't block references
let asnChart = null;
let trendChart = null;
let hitlistChart = null;

// ── Trend chart ───────────────────────────────────────────────────────────────
(function() {{
  const ctx = document.getElementById('trend-chart');
  if (!ctx || !DATA.dates.length) {{
    if (ctx) ctx.parentElement.innerHTML =
      '<p class="empty" style="padding:2rem">No snapshot data yet — run the RouteViews MRT Ingest workflow.</p>';
    return;
  }}
  trendChart = new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: DATA.dates,
      datasets: [
        {{
          label: 'IPv6 Prefixes',
          data: DATA.v6_prefixes,
          borderColor: '#0969da',
          backgroundColor: 'rgba(9,105,218,0.08)',
          tension: 0.3, fill: true, pointRadius: 4, spanGaps: true,
        }},
        {{
          label: 'IPv4 Prefixes',
          data: DATA.v4_prefixes,
          borderColor: '#1a7f37',
          backgroundColor: 'rgba(26,127,55,0.06)',
          tension: 0.3, fill: true, pointRadius: 4, spanGaps: true,
        }},
        {{
          label: 'IPv6 ASNs',
          data: DATA.v6_asns,
          borderColor: '#8250df',
          borderDash: [5,3],
          backgroundColor: 'transparent',
          tension: 0.3, fill: false, pointRadius: 3, spanGaps: true,
        }},
      ],
    }},
    options: {{
      responsive: true,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ position: 'top' }},
        tooltip: {{ callbacks: {{
          label: ctx => ` ${{ctx.dataset.label}}: ${{ctx.parsed.y.toLocaleString()}}`
        }} }},
        zoom: {{
          zoom: {{
            drag: {{ enabled: true, backgroundColor: 'rgba(9,105,218,0.12)' }},
            wheel: {{ enabled: true, speed: 0.08 }},
            pinch: {{ enabled: true }},
            mode: 'x',
            onZoomComplete: () => {{ document.getElementById('trend-zoom-reset').hidden = false; }},
          }},
          pan: {{ enabled: true, mode: 'x' }},
        }},
      }},
      scales: {{
        y: {{ beginAtZero: true, ticks: {{ precision: 0 }} }},
        x: {{ ticks: {{ maxRotation: 45 }} }},
      }},
    }},
  }});
  ctx.addEventListener('dblclick', () => {{
    trendChart.resetZoom();
    document.getElementById('trend-zoom-reset').hidden = true;
  }});
}})();

// ── Hitlist trend chart ───────────────────────────────────────────────────────
(function() {{
  const ctx = document.getElementById('hitlist-trend-chart');
  if (!ctx) return;
  if (!DATA.hitlist_dates.length) {{
    ctx.parentElement.innerHTML =
      '<p class="empty" style="padding:2rem">No hitlist trend data yet.</p>';
    return;
  }}
  hitlistChart = new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: DATA.hitlist_dates,
      datasets: [{{
        label: 'Responsive IPv6 Addresses',
        data: DATA.hitlist_responsive,
        borderColor: '#0969da',
        backgroundColor: 'rgba(9,105,218,0.08)',
        tension: 0.3, fill: true, pointRadius: 5,
      }}],
    }},
    options: {{
      responsive: true,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ position: 'top' }},
        tooltip: {{ callbacks: {{
          label: ctx => ` ${{ctx.dataset.label}}: ${{ctx.parsed.y.toLocaleString()}}`
        }} }},
        zoom: {{
          zoom: {{
            drag: {{ enabled: true, backgroundColor: 'rgba(9,105,218,0.12)' }},
            wheel: {{ enabled: true, speed: 0.08 }},
            pinch: {{ enabled: true }},
            mode: 'x',
            onZoomComplete: () => {{ document.getElementById('hitlist-zoom-reset').hidden = false; }},
          }},
          pan: {{ enabled: true, mode: 'x' }},
        }},
      }},
      scales: {{
        y: {{ beginAtZero: true, ticks: {{ precision: 0 }} }},
        x: {{ ticks: {{ maxRotation: 45 }} }},
      }},
    }},
  }});
  ctx.addEventListener('dblclick', () => {{
    hitlistChart.resetZoom();
    document.getElementById('hitlist-zoom-reset').hidden = true;
  }});
}})();

// ── ASN row pills + active/dark classification ────────────────────────────────
let _showDarkASNs = false;

(function() {{
  let darkCount = 0;
  document.querySelectorAll('#corpus-table .asn-row').forEach(row => {{
    const asn = row.dataset.asn;
    const pfx = DATA.asn_prefixes[asn] || {{v4: [], v6: []}};
    const isActive = pfx.v6.length > 0 || pfx.v4.length > 0;
    row.dataset.active = isActive ? '1' : '0';
    if (!isActive) {{
      row.style.display = 'none';
      darkCount++;
    }}
    const cell = row.querySelector('td');
    if (pfx.v6.length) {{
      cell.insertAdjacentHTML('beforeend',
        `<span class="af-pill v6-pill" title="${{pfx.v6.length}} IPv6 prefix${{pfx.v6.length !== 1 ? 'es' : ''}} announced">✓ v6</span>`);
    }}
    if (pfx.v4.length) {{
      cell.insertAdjacentHTML('beforeend',
        `<span class="af-pill v4-pill" title="${{pfx.v4.length}} IPv4 prefix${{pfx.v4.length !== 1 ? 'es' : ''}} announced">● v4</span>`);
    }}
  }});
  const btn = document.getElementById('asn-dark-toggle');
  if (btn) {{
    if (darkCount) {{
      btn.textContent = `Show dark ASNs (${{darkCount}})`;
    }} else {{
      btn.hidden = true;
    }}
  }}
  const active = document.querySelectorAll('#corpus-table .asn-row[data-active="1"]').length;
  const badge = document.getElementById('asn-count');
  if (badge) badge.textContent = `${{active}} active ASNs`;
}})();

// ── ASN explorer ──────────────────────────────────────────────────────────────
function toggleDarkASNs() {{
  _showDarkASNs = !_showDarkASNs;
  const btn = document.getElementById('asn-dark-toggle');
  if (btn) {{
    const darkCount = document.querySelectorAll('#corpus-table .asn-row[data-active="0"]').length;
    btn.textContent = _showDarkASNs
      ? `Active only (hide ${{darkCount}} dark)`
      : `Show dark ASNs (${{darkCount}})`;
    btn.classList.toggle('showing-dark', _showDarkASNs);
  }}
  filterASNs(document.getElementById('asn-search').value);
}}

function filterASNs(q) {{
  const rows = document.querySelectorAll('#corpus-table .asn-row');
  q = q.toLowerCase().trim();
  let shown = 0;
  rows.forEach(row => {{
    const isDark = row.dataset.active === '0';
    const matchesSearch = !q ||
      row.dataset.asn.includes(q) ||
      row.dataset.agency.includes(q) ||
      row.dataset.tier.includes(q);
    const visible = matchesSearch && (_showDarkASNs || !isDark);
    row.style.display = visible ? '' : 'none';
    if (visible) shown++;
  }});
  const total = rows.length;
  const active = document.querySelectorAll('#corpus-table .asn-row[data-active="1"]').length;
  const badge = document.getElementById('asn-count');
  if (!badge) return;
  if (q) {{
    badge.textContent = `${{shown}} of ${{_showDarkASNs ? total : active}} ASNs`;
  }} else {{
    badge.textContent = _showDarkASNs ? `${{total}} ASNs` : `${{active}} active ASNs`;
  }}
}}

function showASN(asn) {{
  const key = String(asn);
  const detail = document.getElementById('asn-detail');
  const title = document.getElementById('detail-title');
  const v6el = document.getElementById('detail-v6');
  const v4el = document.getElementById('detail-v4');
  const v6cnt = document.getElementById('detail-v6-count');
  const v4cnt = document.getElementById('detail-v4-count');

  // Find agency name from the clicked row
  const row = document.querySelector(`.asn-row[data-asn="${{asn}}"]`);
  const agency = row ? row.querySelector('td:nth-child(2)').textContent : `ASN ${{asn}}`;
  title.textContent = `${{agency}} (ASN ${{asn}})`;

  const pfxData = DATA.asn_prefixes[key] || {{v4: [], v6: []}};
  v6cnt.textContent = pfxData.v6.length;
  v4cnt.textContent = pfxData.v4.length;

  const renderList = (el, prefixes, af) => {{
    if (!prefixes.length) {{
      el.innerHTML = `<span class="empty-inline">No ${{af === 6 ? 'IPv6' : 'IPv4'}} prefixes in latest snapshot</span>`;
      return;
    }}
    el.innerHTML = prefixes.map(p =>
      `<code class="prefix">${{p}}</code>`
    ).join('');
  }};
  renderList(v6el, pfxData.v6, 6);
  renderList(v4el, pfxData.v4, 4);

  // Hitlist live-endpoint data
  const hlBox = document.getElementById('detail-hitlist');
  const hlData = DATA.hitlist_asns[key];
  if (hlData !== undefined) {{
    document.getElementById('detail-hl-responsive').textContent = hlData.responsive.toLocaleString();
    document.getElementById('detail-hl-aliased').textContent    = hlData.aliased.toLocaleString();
    document.getElementById('detail-hl-real').textContent       = (hlData.responsive - hlData.aliased).toLocaleString();
    hlBox.style.display = '';
  }} else {{
    hlBox.style.display = 'none';
  }}

  // Show detail panel. It is position:sticky so it clings to the top of the
  // viewport as the user scrolls — scroll the clicked row into view instead of
  // jumping to the top of the section.
  detail.style.display = 'block';
  requestAnimationFrame(() => {{
    if (row) row.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
  }});

  // RPKI validity
  const rpkiBox = document.getElementById('detail-rpki');
  const rpki = DATA.rpki_by_asn && DATA.rpki_by_asn[key];
  if (rpki !== undefined) {{
    document.getElementById('detail-rpki-valid').textContent   = rpki.v.toLocaleString();
    document.getElementById('detail-rpki-invalid').textContent = rpki.i.toLocaleString();
    document.getElementById('detail-rpki-nf').textContent      = rpki.nf.toLocaleString();
    rpkiBox.style.display = '';
  }} else {{
    rpkiBox.style.display = 'none';
  }}

  // History chart
  const hist = DATA.asn_history[key] || {{}};
  const histDates = Object.keys(hist).sort();
  const histV6 = histDates.map(d => hist[d].v6 || 0);
  const histV4 = histDates.map(d => hist[d].v4 || 0);

  if (asnChart) {{ asnChart.destroy(); asnChart = null; }}
  const chartCtx = document.getElementById('asn-chart');
  if (histDates.length) {{
    try {{
      asnChart = new Chart(chartCtx, {{
        type: 'bar',
        data: {{
          labels: histDates,
          datasets: [
            {{ label: 'IPv6', data: histV6, backgroundColor: 'rgba(9,105,218,0.7)' }},
            {{ label: 'IPv4', data: histV4, backgroundColor: 'rgba(26,127,55,0.5)' }},
          ],
        }},
        options: {{
          responsive: true,
          plugins: {{ legend: {{ position: 'top' }} }},
          scales: {{ y: {{ beginAtZero: true, ticks: {{ precision: 0 }} }} }},
        }},
      }});
      chartCtx.style.display = '';
    }} catch(e) {{
      chartCtx.style.display = 'none';
    }}
  }} else {{
    chartCtx.style.display = 'none';
  }}

  // Highlight selected row
  document.querySelectorAll('.asn-row').forEach(r => r.classList.remove('selected'));
  if (row) row.classList.add('selected');
}}

function closeASN() {{
  document.getElementById('asn-detail').style.display = 'none';
  document.querySelectorAll('.asn-row').forEach(r => r.classList.remove('selected'));
  if (asnChart) {{ asnChart.destroy(); asnChart = null; }}
}}

</script>

</body>
</html>"""


def _enrichment_section(data: dict | None) -> str:
    no_data_msg = """
<section id="enrichment">
  <h2>Active-Prefix Enrichment</h2>
  <p class="section-blurb">
    BGP routing table entries prove a prefix is <em>announced</em>, not that it is
    <em>operationally active</em>. Five independent enrichment signals cross-reference
    announced prefixes against observable internet activity to distinguish maintained
    infrastructure from routing-table artifacts.
    TCP services (name servers, mail relays, HTTPS endpoints) are visible through
    firewalls that silently drop ICMP — the primary blind spot of the TU Munich hitlist
    scanner. RPKI ROA validity confirms intentional, operator-authorized announcements.
  </p>
  <p class="empty">
    No enrichment data ingested yet.
    Trigger the <strong>enrichment-dotgov-dns</strong>, <strong>enrichment-nist</strong>,
    <strong>enrichment-cert-transparency</strong>, or <strong>enrichment-rpki</strong>
    workflows to populate this section.
  </p>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:1rem;margin-top:1.25rem">
    {cards}
  </div>
</section>"""

    def _ecard(color, title, body, footer=""):
        footer_html = (
            f'<div style="font-size:0.78rem;color:var(--muted);margin-top:0.6rem">{footer}</div>'
            if footer else ""
        )
        return (f'<div class="stat-card" style="border-left-color:{color}">'
                f'<div class="label">{title}</div>'
                f'<div style="font-size:0.82rem;color:var(--muted);margin-top:0.35rem;line-height:1.55">{body}</div>'
                f'{footer_html}'
                f'</div>')

    placeholder_cards = "".join([
        _ecard("#0969da", ".gov DNS Enumeration",
               "Resolves NS, MX, and AAAA records for all registered federal .gov domains. "
               "Captures TCP-only services invisible to ICMP probes.",
               "<strong>Cadence:</strong> Daily · <strong>Table:</strong> <code>domain_services</code>"),
        _ecard("#8250df", "NIST USGv6 Monitor",
               "Per-domain IPv6 service metrics (DNS / mail / web) and DNSSEC status from the "
               "NIST deployment monitor.",
               "<strong>Cadence:</strong> Daily · <strong>Table:</strong> <code>nist_deploymon</code>"),
        _ecard("#1a7f37", "Certificate Transparency",
               "TLS certs issued to *.gov subdomains resolved to AAAA and matched to corpus prefixes. "
               "Cert date = reliable last-active lower bound.",
               "<strong>Cadence:</strong> Weekly · <strong>Table:</strong> <code>domain_services</code>"),
        _ecard("#cf222e", "RPKI Validity",
               "Cloudflare RPKI API classification of corpus prefix / origin-AS pairs as "
               "valid, invalid, or not-found.",
               "<strong>Cadence:</strong> Weekly · <strong>Table:</strong> <code>rpki_validity</code>"),
        _ecard("#9a6700", "BGP Communities",
               "Standard and large BGP communities extracted from MRT RIB entries per ingest. "
               "Identifies blackhole-tagged and traffic-engineered prefixes.",
               "<strong>Cadence:</strong> Per BGP ingest · <strong>Table:</strong> <code>bgp_communities</code>"),
    ])

    if data is None:
        return no_data_msg.format(cards=placeholder_cards)

    def _card(cls, label, value, sub=""):
        return (f'<div class="stat-card {cls}">'
                f'<div class="label">{label}</div>'
                f'<div class="value">{value}</div>'
                f'{"<div class=sub>" + sub + "</div>" if sub else ""}</div>')

    stat_cards = []

    rpki = data.get("rpki")
    if rpki:
        total_rpki = rpki["valid"] + rpki["invalid"] + rpki["not_found"]
        covered = rpki["valid"] + rpki["invalid"]
        # Headline: % of current prefix/ASN pairs with ANY ROA coverage (valid or invalid).
        # Pairs with no ROA get "not_found" — common for older USG prefixes predating RPKI.
        cov_pct = f"{100 * covered / total_rpki:.1f}%" if total_rpki else "—"
        # Of covered pairs: how many are actually valid?
        valid_of_cov = (f"{100 * rpki['valid'] / covered:.0f}% valid"
                        if covered else "none covered")
        card_cls = "ok" if rpki["valid"] > rpki["invalid"] else "indet"
        stat_cards.append(_card(card_cls, "RPKI ROA Coverage", cov_pct,
                                f"{rpki['valid']:,} valid · {rpki['invalid']:,} invalid "
                                f"({valid_of_cov} of covered) · "
                                f"{rpki['not_found']:,} no ROA · as of {rpki['date']}"))

    dom = data.get("domain")
    if dom:
        stat_cards.append(_card("ipv6b", ".gov IPv6 Services", f"{dom['matched_addrs']:,}",
                                f"IPv6 addresses in corpus prefixes from {dom['domains']:,} "
                                f".gov domains ({dom['date']})"))

    nist = data.get("nist")
    if nist:
        stat_cards.append(_card("ipv6", "NIST v6-Functional",
                                str(nist["dns_func"] + nist["mail_func"] + nist["web_func"]),
                                f"DNS {nist['dns_func']} · mail {nist['mail_func']} · "
                                f"web {nist['web_func']} · DNSSEC chained {nist['dnssec_chained']} "
                                f"({nist['date']})"))

    dnssec = data.get("dnssec")
    if dnssec:
        val_pct = (f"{100 * dnssec['validated'] / dnssec['total']:.0f}%"
                   if dnssec["total"] else "—")
        stat_cards.append(_card("ok", ".gov DNSSEC Validated", val_pct,
                                f"{dnssec['validated']:,} AD-validated · "
                                f"{dnssec['signed']:,} signed · "
                                f"{dnssec['delegated']:,} DS delegated "
                                f"of {dnssec['total']:,} domains ({dnssec['date']})"))

    comm = data.get("communities")
    if comm:
        bh_label = (f"{comm['blackhole']:,} blackhole-tagged" if comm["blackhole"]
                    else "no blackhole tags")
        stat_cards.append(_card("indet" if comm["blackhole"] else "corpus",
                                "BGP Communities",
                                f"{comm['unique']:,}",
                                f"unique communities on {comm['prefixes']:,} prefixes · "
                                f"{bh_label} ({comm['date']})"))

    src_rows = data.get("domain_sources", [])
    if src_rows:
        src_body = "".join(
            f"<tr><td><code>{_esc(r[0])}</code></td>"
            f"<td class='num'>{r[1]:,}</td>"
            f"<td class='num'>{r[2]:,}</td></tr>"
            for r in src_rows
        )
        src_tbl = _table(["Source", ".gov Domains Scanned", "Corpus Prefix Matches"], src_body)
    else:
        src_tbl = ""

    stat_grid = f'<div class="stat-grid">{"".join(stat_cards)}</div>' if stat_cards else ""

    # DNSSEC detail block
    dnssec_detail = ""
    if dnssec := data.get("dnssec"):
        total = dnssec["total"] or 1
        def _dpct(n): return f"{100 * n / total:.1f}%"
        stages = [
            ("Zone signed (DNSKEY)", dnssec["signed"]),
            ("Delegation signed (DS at parent)", dnssec["delegated"]),
            ("Full chain validated (AD bit)", dnssec["validated"]),
        ]
        stage_rows = "".join(
            f"<tr><td>{label}</td>"
            f"<td class='num'>{n:,}</td>"
            f"<td class='num'>{_dpct(n)}</td>"
            f"<td style='padding-left:0.5rem;min-width:120px'>"
            f"<div style='background:var(--border);border-radius:4px;height:10px;overflow:hidden'>"
            f"<div style='background:#1a7f37;width:{_dpct(n)};height:100%'></div></div></td></tr>"
            for label, n in stages
        )
        algo_rows_html = ""
        for alg_str, cnt in data.get("dnssec_algos", []):
            # RFC 8624 algorithm names
            names = {
                "1": "RSA/MD5", "5": "RSA/SHA-1", "7": "RSASHA1-NSEC3",
                "8": "RSA/SHA-256", "10": "RSA/SHA-512",
                "12": "GOST R 34.10-2001", "13": "ECDSA/P-256/SHA-256",
                "14": "ECDSA/P-384/SHA-384", "15": "Ed25519", "16": "Ed448",
            }
            labels = ", ".join(
                names.get(a.strip(), f"Alg {a.strip()}") for a in alg_str.split(",")
            )
            algo_rows_html += (
                f"<tr><td><code>{_esc(alg_str)}</code></td>"
                f"<td style='padding-left:0.5rem;font-size:0.82rem;color:var(--muted)'>{labels}</td>"
                f"<td class='num'>{cnt:,}</td></tr>"
            )
        algo_section = (
            f"<h4 style='font-size:0.875rem;font-weight:600;margin:1rem 0 0.4rem'>"
            f"DNSKEY Algorithm Distribution</h4>"
            f"{_table(['Algorithm(s)', 'Name(s)', 'Domains'], algo_rows_html)}"
            if algo_rows_html else ""
        )
        dnssec_detail = (
            f"<h3 style='margin:1.5rem 0 0.5rem;font-size:1rem;font-weight:600'>"
            f".gov DNSSEC Coverage ({dnssec['date']})</h3>"
            f"<p style='font-size:0.83rem;color:var(--muted);margin-bottom:0.75rem'>"
            f"DNSSEC validation chain: zone must be signed (DNSKEY), delegation must be "
            f"recorded at the parent (DS record), and a validating resolver must confirm "
            f"the full chain (AD flag). Each stage requires the previous.</p>"
            f"<div class='card' style='overflow-x:auto;margin-bottom:0.5rem'>"
            f"{_table(['Stage', 'Domains', 'Coverage', ''], stage_rows)}"
            f"{algo_section}</div>"
        )

    corpus_match_note = (
        "<p style='font-size:0.82rem;color:var(--muted);margin:0.5rem 0 0'>"
        "<strong>Why corpus matches may be zero or low:</strong> "
        "Federal .gov domains widely use commercial cloud providers — AWS Route&nbsp;53, "
        "Akamai, Cloudflare, and others — for DNS, mail, and web infrastructure. "
        "Those providers' IPv6 address space is outside the government-operated BGP corpus "
        "by design. Zero corpus matches for NIST or DNS-enumeration service records is "
        "expected and does <em>not</em> indicate missing data; it reflects the industry-wide "
        "shift to cloud-hosted service delivery."
        "</p>"
    )

    return f"""
<section id="enrichment">
  <h2>Active-Prefix Enrichment</h2>
  <p class="section-blurb">
    BGP routing table entries prove a prefix is <em>announced</em>, not that it is
    <em>operationally active</em>. Five independent enrichment signals cross-reference
    announced prefixes against observable internet activity — including DNS records,
    TLS certificate issuance, RPKI ROA coverage, and BGP community tags — to distinguish
    maintained infrastructure from routing-table artifacts. TCP-based signals capture
    hosts that firewalls hide from ICMP probes.
  </p>
  {stat_grid}
  {"<h3 style='margin:1.25rem 0 0.5rem;font-size:1rem;font-weight:600'>Domain service coverage by enrichment source</h3><div class='card' style='overflow-x:auto;margin-bottom:0.5rem'>" + src_tbl + "</div>" + corpus_match_note if src_tbl else ""}
  {dnssec_detail}
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:1rem;margin-top:1.25rem">
    {placeholder_cards}
  </div>
</section>"""


def _hitlist_section(hitlist_data) -> str:
    if hitlist_data is None:
        return """
<section id="hitlist">
  <h2>Live IPv6 Endpoints (Hitlist)</h2>
  <p class="section-blurb">
    The <a href="https://ipv6hitlist.github.io">TU Munich IPv6 Hitlist</a> provides
    weekly lists of IPv6 addresses verified to respond to active probes — a stronger
    signal than a BGP route existing. No hitlist data has been ingested yet.
  </p>
  <p class="empty">
    Run <code>make hitlist</code> or trigger the
    <strong>IPv6 Hitlist Ingest</strong> workflow to populate this section.
    Requires <code>route-views6</code> pfx2as data (run RouteViews ingest with
    <code>collector: route-views6</code> first).
  </p>
</section>"""

    d = hitlist_data
    likely_real = d["total_responsive"] - d["total_aliased"]
    resp_pct = (f"{100 * d['asns_with_responses'] / d['corpus_count']:.1f}%"
                if d["corpus_count"] else "—")

    def _card(cls, label, value, sub=""):
        return (f'<div class="stat-card {cls}">'
                f'<div class="label">{label}</div>'
                f'<div class="value">{value}</div>'
                f'{"<div class=sub>" + sub + "</div>" if sub else ""}</div>')

    cards = "".join([
        _card("ipv6", "ASNs with Live Hosts", resp_pct,
              f"{d['asns_with_responses']} of {d['corpus_count']} corpus ASNs"),
        _card("ipv6b", "Responsive Addresses",
              f"{d['total_responsive']:,}", "verified probe responses"),
        _card("ipv4", "Likely Real Hosts",
              f"{likely_real:,}", "non-aliased responsive addresses"),
        _card("indet", "Aliased",
              f"{d['total_aliased']:,}", "in aliased prefixes (router artifacts)"),
    ])

    # ── Per-agency table ──────────────────────────────────────────────────────
    if d["agency_rows"]:
        _cols = ["Agency", "Tier", "ASN", "Responsive", "Likely Real", "Aliased"]
        active_rows = [r for r in d["agency_rows"] if r[3] > 0]
        zero_rows   = [r for r in d["agency_rows"] if r[3] == 0]

        def _agency_tr(r):
            return (
                f"<tr>"
                f"<td>{_esc(r[0])}</td>"
                f"<td><span class='tier-tag tier-{_esc(r[1])}'>{_tier_label(r[1])}</span></td>"
                f"<td><code>{_esc(r[2])}</code></td>"
                f"<td class='num'>{r[3]:,}</td>"
                f"<td class='num'>{r[5]:,}</td>"
                f"<td class='num'>{r[4]:,}</td>"
                f"</tr>"
            )

        active_tbl = _table(_cols, "".join(_agency_tr(r) for r in active_rows)) if active_rows else ""
        if zero_rows:
            zero_tbl = _table(_cols, "".join(_agency_tr(r) for r in zero_rows))
            zero_block = (
                f"<details style='margin-top:0.5rem'>"
                f"<summary style='cursor:pointer;padding:0.4rem 0;font-size:0.875rem;"
                f"color:var(--muted);user-select:none'>"
                f"{len(zero_rows)} agenc{'y' if len(zero_rows)==1 else 'ies'} with 0 responsive addresses</summary>"
                f"<div style='overflow-x:auto;margin-top:0.5rem'>{zero_tbl}</div>"
                f"</details>"
            )
        else:
            zero_block = ""
        agency_tbl = active_tbl + zero_block
    else:
        agency_tbl = "<p class='empty'>No per-agency data available.</p>"

    # ── Gap: BGP IPv6 but zero hitlist responses ──────────────────────────────
    if d["gap_rows"]:
        gap_body = "".join(
            f"<tr><td>{_esc(r[0])}</td>"
            f"<td><span class='tier-tag tier-{_esc(r[1])}'>{_tier_label(r[1])}</span></td>"
            f"<td><code>{_esc(r[2])}</code></td></tr>"
            for r in d["gap_rows"]
        )
        gap_content = _table(["Agency", "Tier", "ASN"], gap_body)
        gap_note = (f"{len(d['gap_rows'])} ASN(s) advertise IPv6 BGP prefixes "
                    f"but had zero hitlist-responsive addresses")
    else:
        gap_content = "<p class='empty'>All assessed ASNs have at least one responsive IPv6 address. 🎉</p>"
        gap_note = "No gap ASNs detected"

    return f"""
<section id="hitlist">
  <h2>Live IPv6 Endpoints (Hitlist)</h2>
  <p class="section-blurb">
    The <a href="https://ipv6hitlist.github.io">TU Munich IPv6 Hitlist</a> publishes
    weekly lists of IPv6 addresses that responded to active probes — a stronger
    signal than BGP reachability. Observation date:
    <strong>{_esc(d['obs_date'])}</strong> · Hitlist file date:
    <strong>{_esc(d['hitlist_date'])}</strong>.
    <em>Aliased</em> addresses fall in prefixes where any address responds
    (router artifacts); <em>likely real</em> excludes these.
  </p>
  <div class="stat-grid">{cards}</div>

  <h3 style="margin:1.25rem 0 0.5rem;font-size:1rem;font-weight:600">
    Responsive addresses by agency
  </h3>
  <div class="card" style="overflow-x:auto;margin-bottom:1.5rem">{agency_tbl}</div>

  <h3 style="margin:1.25rem 0 0.5rem;font-size:1rem;font-weight:600">
    Responsive address trend
  </h3>
  <p class="section-blurb" style="margin-bottom:0.75rem">
    Total responsive IPv6 addresses across all corpus ASNs per weekly hitlist observation.
  </p>
  <div class="chart-wrap card" style="margin-bottom:1.5rem">
    <div class="chart-toolbar">
      <button id="hitlist-zoom-reset" class="zoom-reset-btn" onclick="if(hitlistChart)hitlistChart.resetZoom()" hidden>Reset zoom</button>
    </div>
    <canvas id="hitlist-trend-chart" height="100"></canvas>
    <p class="chart-hint">Drag to zoom &nbsp;·&nbsp; scroll to zoom &nbsp;·&nbsp; double-click to reset</p>
  </div>

  <h3 style="margin:1.25rem 0 0.5rem;font-size:1rem;font-weight:600">
    Gap: IPv6 BGP prefix but zero responsive addresses
  </h3>
  <p class="section-blurb" style="margin-bottom:0.75rem">
    {_esc(gap_note)}. These agencies announce IPv6 routes but no
    addresses in those prefixes responded to hitlist probes.
  </p>
  {gap_content}
</section>"""


def _ark_section(ark_data) -> str:
    if ark_data is None:
        return """
<section id="reachability">
  <h2>IPv6 BGP Reachability</h2>
  <p class="section-blurb">
    Reachability is determined by whether a corpus ASN originates IPv6 prefixes
    in the RouteViews BGP table. This mirrors the methodology used by
    <a href="https://www.nist.gov/publications/measuring-ipv6-adoption">NIST</a>,
    <a href="https://stats.labs.apnic.net/ipv6/">APNIC</a>, and Google IPv6 stats.
    An ASN with no IPv6 prefix announcements is marked "not reachable."
    Indeterminate means the ASN has no IPv6 pfx2as coverage.
  </p>
  <p class="empty">
    No reachability data yet. Trigger the <strong>CAIDA Ark IPv6 Reachability Ingest</strong>
    workflow (which runs after RouteViews ingest populates prefix data).
  </p>
</section>"""

    d = ark_data
    total_assessed = d["reachable"] + d["not_reachable"]
    adoption_pct = (f"{100 * d['reachable'] / total_assessed:.1f}%"
                    if total_assessed else "—")

    def _card(cls, label, value, sub=""):
        return (f'<div class="stat-card {cls}">'
                f'<div class="label">{label}</div>'
                f'<div class="value">{value}</div>'
                f'{"<div class=sub>" + sub + "</div>" if sub else ""}</div>')

    cards = "".join([
        _card("ipv6", "IPv6 Reachable", adoption_pct,
              f"{d['reachable']} of {total_assessed} with IPv6 BGP prefixes"),
        _card("ok", "ASNs Reachable", str(d["reachable"]),
              "originate ≥1 IPv6 prefix"),
        _card("gap", "Not Reachable", str(d["not_reachable"]),
              "have IPv6 prefixes but none announced"),
        _card("indet", "Indeterminate", str(d["indeterminate"]),
              "no pfx2as IPv6 coverage"),
    ])

    if d["gap_rows"]:
        gap_body = "".join(
            f"<tr><td><code>{_esc(r[0])}</code></td><td>{_esc(r[1])}</td>"
            f"<td><span class='tier-tag tier-{_esc(r[2])}'>{_tier_label(r[2])}</span></td>"
            f"<td class='num'>{r[3]:,}</td><td class='num'>{r[4]:,}</td></tr>"
            for r in d["gap_rows"]
        )
        gap_content = _table(
            ["ASN", "Agency", "Tier", "Prefixes Observed", "Prefixes in Scope"],
            gap_body,
        )
    else:
        gap_content = "<p class='empty'>No gap ASNs — all assessed ASNs are IPv6 reachable. 🎉</p>"

    return f"""
<section id="reachability">
  <h2>IPv6 BGP Reachability</h2>
  <p class="section-blurb">
    Reachability is computed from CAIDA pfx2as data sourced from RouteViews.
    An ASN is IPv6-reachable if it originates at least one IPv6 prefix visible
    in the BGP routing table. This is the methodology used by
    <a href="https://www.nist.gov/publications/measuring-ipv6-adoption">NIST</a>,
    <a href="https://stats.labs.apnic.net/ipv6/">APNIC</a>, and Google IPv6 stats.
    Measurement date: <strong>{_esc(d['mdate'])}</strong> ·
    pfx2as snapshot: <strong>{_esc(d['pfx2as_date'])}</strong>
  </p>
  <div class="stat-grid">{cards}</div>
  <h3 style="margin:1.25rem 0 0.75rem;font-size:1rem;font-weight:600">
    Gap: ASNs with IPv6 scope but no BGP announcement
  </h3>
  <p class="section-blurb" style="margin-bottom:0.75rem">
    These ASNs have IPv6 address space allocated (visible in pfx2as) but are not
    currently originating IPv6 routes in the global BGP table.
  </p>
  {gap_content}
</section>"""


def _af_tag(af: int) -> str:
    cls = "v6" if af == 6 else "v4"
    label = f"IPv{af}"
    return f'<span class="tag {cls}">{label}</span>'


def _table(headers: list, body_html: str, id: str = "") -> str:
    ths = "".join(f"<th>{h}</th>" for h in headers)
    id_attr = f' id="{id}"' if id else ""
    return (f'<table{id_attr}>'
            f'<thead><tr>{ths}</tr></thead>'
            f'<tbody>{body_html}</tbody>'
            f'</table>')


_CSS = """
:root {
  --bg: #f0f2f5;
  --surface: #ffffff;
  --border: #d0d7de;
  --text: #1f2328;
  --muted: #656d76;
  --accent: #0969da;
  --header-bg: #0d1117;
  --header-text: #e6edf3;
  --th-bg: #21262d;
  --th-text: #e6edf3;
  --even-row: #f6f8fa;
  --nav-bg: #161b22;
  --nav-text: #c9d1d9;
  --select-bg: rgba(9,105,218,0.08);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.5; font-size: 15px;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

header {
  background: var(--header-bg); color: var(--header-text);
  padding: 2.5rem 1.5rem 2rem;
}
header h1 { font-size: 1.8rem; font-weight: 700; }
header .subtitle { color: #8b949e; margin-top: 0.4rem; font-size: 0.95rem; }
header .meta { color: #6e7681; margin-top: 0.75rem; font-size: 0.85rem; }

nav {
  background: var(--nav-bg); padding: 0 1.5rem;
  display: flex; gap: 0; overflow-x: auto; white-space: nowrap;
  border-bottom: 1px solid #30363d;
  position: sticky; top: 0; z-index: 10;
}
nav a {
  color: var(--nav-text); padding: 0.75rem 1rem; display: block;
  font-size: 0.875rem; border-bottom: 2px solid transparent;
  transition: color 0.15s, border-color 0.15s;
}
nav a:hover { color: #fff; border-bottom-color: var(--accent); text-decoration: none; }

main { max-width: 1100px; margin: 2rem auto; padding: 0 1rem; }
section { margin-bottom: 3rem; scroll-margin-top: 3rem; }
h2 {
  font-size: 1.15rem; font-weight: 600; margin-bottom: 0.75rem;
  padding-bottom: 0.5rem; border-bottom: 2px solid var(--border);
}
h3 { font-size: 1rem; font-weight: 600; }
.section-blurb {
  font-size: 0.9rem; color: var(--muted); margin-bottom: 1.25rem;
  max-width: 72ch; line-height: 1.6;
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 1rem; margin-bottom: 1.5rem;
}
.stat-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 1.25rem 1.5rem; border-left: 4px solid var(--border);
}
.stat-card.ipv6  { border-left-color: #0969da; }
.stat-card.ipv6b { border-left-color: #54aeff; }
.stat-card.ipv4  { border-left-color: #1a7f37; }
.stat-card.corpus { border-left-color: #8250df; }
.stat-card.ok    { border-left-color: #1a7f37; }
.stat-card.gap   { border-left-color: #cf222e; }
.stat-card.indet { border-left-color: #9a6700; }
.stat-card .label {
  font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--muted); font-weight: 600;
}
.stat-card .value { font-size: 2.25rem; font-weight: 700; margin-top: 0.2rem; }
.stat-card .sub { font-size: 0.82rem; color: var(--muted); margin-top: 0.25rem; }

.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; overflow-x: auto; }
.chart-wrap { padding: 1.25rem; }
.chart-toolbar { display: flex; justify-content: flex-end; min-height: 1.6rem; margin-bottom: 0.25rem; }
.zoom-reset-btn {
  font-size: 0.78rem; padding: 0.2rem 0.65rem; border-radius: 4px; cursor: pointer;
  border: 1px solid var(--border); background: var(--surface); color: var(--muted);
}
.zoom-reset-btn:hover { background: var(--th-bg); color: var(--fg); }
.chart-hint { font-size: 0.75rem; color: var(--muted); text-align: center; margin-top: 0.4rem; }

table { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
th {
  background: var(--th-bg); color: var(--th-text); font-weight: 600;
  text-align: left; padding: 0.6rem 0.85rem; white-space: nowrap;
}
td { padding: 0.5rem 0.85rem; border-bottom: 1px solid var(--border); }
tr:last-child td { border-bottom: none; }
tr:nth-child(even) td { background: var(--even-row); }
.asn-row { cursor: pointer; transition: background 0.1s; }
.asn-row:hover td { background: #ddf4ff !important; }
.asn-row.selected td { background: var(--select-bg) !important; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.small { font-size: 0.8rem; color: var(--muted); }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85em; }

.tag {
  display: inline-block; border-radius: 3px;
  padding: 0.1rem 0.45rem; font-size: 0.78rem; font-weight: 500;
}
.tag.v6   { background: #ddf4ff; color: #0550ae; }
.tag.v4   { background: #dafbe1; color: #116329; }
.tag.inact{ background: #f0f2f5; color: #8b949e; border: 1px solid #d0d7de; }
.muted    { color: var(--muted); }
tr.tier-inactive td { color: var(--muted); }

.tier-tag {
  display: inline-block; border-radius: 12px;
  padding: 0.1rem 0.6rem; font-size: 0.78rem; font-weight: 500;
  background: #f0f2f5; color: #656d76; border: 1px solid #d0d7de;
}
.tier-tag.tier-cfo_act         { background: #ddf4ff; color: #0550ae; border-color: #b6e3ff; }
.tier-tag.tier-dod             { background: #ffd8b2; color: #953800; border-color: #ffc680; }
.tier-tag.tier-doe_lab         { background: #e6f0ff; color: #1a5276; border-color: #aac8f0; }
.tier-tag.tier-federal_civilian{ background: #d2f9d2; color: #116329; border-color: #a8f0a8; }
.tier-tag.tier-ic              { background: #f3d2ff; color: #6639ba; border-color: #e2b8ff; }
.tier-tag.tier-judicial        { background: #fff3cd; color: #7d4e00; border-color: #ffd966; }
.tier-tag.tier-legislative     { background: #ffe0e0; color: #9e2a2b; border-color: #ffb3b3; }

.empty { color: var(--muted); padding: 1rem 0; font-size: 0.9rem; }
.empty-inline { color: var(--muted); font-size: 0.85rem; font-style: italic; }
.placeholder {
  text-align: center; padding: 4rem 2rem; color: var(--muted);
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
}
.placeholder h3 { margin-bottom: 0.5rem; color: var(--text); }

.search-bar {
  display: flex; align-items: center; gap: 0.75rem; margin-bottom: 1rem;
}
.search-bar input {
  flex: 1; max-width: 360px;
  padding: 0.55rem 0.85rem; border: 1px solid var(--border); border-radius: 6px;
  font-size: 0.9rem; background: var(--surface); color: var(--text);
  outline: none; transition: border-color 0.15s;
}
.search-bar input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(9,105,218,0.15); }
.asn-count { font-size: 0.85rem; color: var(--muted); }
.dark-toggle {
  font-size: 0.8rem; padding: 0.3rem 0.7rem;
  border-radius: 6px; border: 1px solid var(--border);
  background: var(--surface); color: var(--muted);
  cursor: pointer; white-space: nowrap; line-height: 1.4;
}
.dark-toggle:hover { border-color: var(--accent); color: var(--text); }
.dark-toggle.showing-dark { background: var(--even-row); color: var(--text); border-color: var(--muted); }

.asn-detail {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; margin-bottom: 1rem; overflow: hidden;
  box-shadow: 0 4px 16px rgba(0,0,0,0.06);
  position: sticky; top: 3.5rem; z-index: 50;
}
.asn-detail-header {
  background: var(--th-bg); color: var(--th-text);
  padding: 0.75rem 1rem; display: flex; justify-content: space-between; align-items: center;
  font-size: 0.95rem;
}
.close-btn {
  background: none; border: 1px solid #444; color: var(--th-text);
  padding: 0.25rem 0.65rem; border-radius: 4px; cursor: pointer; font-size: 0.8rem;
}
.close-btn:hover { background: rgba(255,255,255,0.1); }
.asn-detail-body { padding: 1.25rem; }
.asn-prefixes { display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; margin-bottom: 1.25rem; }
.asn-prefixes h4 { font-size: 0.875rem; font-weight: 600; margin-bottom: 0.5rem; display: flex; align-items: center; gap: 0.4rem; }
.prefix-list { display: flex; flex-wrap: wrap; gap: 0.35rem; }
.prefix { background: var(--even-row); border: 1px solid var(--border); border-radius: 4px; padding: 0.2rem 0.5rem; font-size: 0.82rem; }
.count-badge { border-radius: 10px; padding: 0.1rem 0.5rem; font-size: 0.72rem; font-weight: 600; }
.count-badge.v6 { background: #ddf4ff; color: #0550ae; }
.count-badge.v4 { background: #dafbe1; color: #116329; }
.asn-chart-wrap h4 { font-size: 0.875rem; font-weight: 600; margin-bottom: 0.6rem; }

.detail-hitlist { margin-bottom: 1rem; }
.detail-hitlist h4 { font-size: 0.875rem; font-weight: 600; margin-bottom: 0.5rem; }
.hitlist-chips { display: flex; flex-wrap: wrap; gap: 0.5rem; }
.hchip {
  padding: 0.3rem 0.75rem; border-radius: 20px; font-size: 0.82rem;
  border: 1px solid var(--border);
}
.hchip.responsive    { background: #ddf4ff; color: #0550ae; border-color: #b6e3ff; }
.hchip.aliased       { background: #fff8c5; color: #9a6700; border-color: #e3b341; }
.hchip.real          { background: #dafbe1; color: #116329; border-color: #56d364; }
.hchip.rpki-valid    { background: #dafbe1; color: #116329; border-color: #56d364; }
.hchip.rpki-invalid  { background: #ffebe9; color: #cf222e; border-color: #ff8182; }
.hchip.rpki-nf       { background: var(--even-row); color: var(--muted); border-color: var(--border); }

.af-pill {
  display: inline-block; font-size: 0.65rem; font-weight: 700;
  padding: 0.1rem 0.4rem; border-radius: 8px;
  margin-left: 0.35rem; vertical-align: middle; line-height: 1.5;
  border: 1px solid transparent; white-space: nowrap;
}
.v6-pill { background: #dafbe1; color: #116329; border-color: #56d364; }
.v4-pill { background: #ddf4ff; color: #0550ae; border-color: #b6e3ff; }

.data-notice {
  background: #fffbea; border-bottom: 1px solid #d4a72c;
  color: #633c01; font-size: 0.84rem; padding: 0.55rem 1.5rem;
  text-align: center; line-height: 1.5;
}
.data-notice a { color: #633c01; font-weight: 600; text-decoration: underline; }
.data-notice a:hover { color: #3d2400; }

.disclaimer-box {
  background: var(--surface); border: 1px solid var(--border);
  border-left: 4px solid var(--muted);
  border-radius: 8px; padding: 1.5rem 1.75rem;
  font-size: 0.9rem; line-height: 1.7;
  display: flex; flex-direction: column; gap: 0.9rem;
}
.disclaimer-box ul {
  margin: 0; padding-left: 1.4rem; display: flex; flex-direction: column; gap: 0.5rem;
}
.disclaimer-box li { line-height: 1.6; }
.disclaimer-source { color: var(--muted); font-size: 0.85rem; margin-top: 0.25rem; }

footer {
  text-align: center; padding: 2rem 1rem;
  color: var(--muted); font-size: 0.82rem;
  border-top: 1px solid var(--border); margin-top: 2rem;
}

@media (max-width: 640px) {
  header { padding: 1.5rem 1rem; }
  .stat-card .value { font-size: 1.75rem; }
  th, td { padding: 0.45rem 0.6rem; }
  .asn-prefixes { grid-template-columns: 1fr; }
  .search-bar input { max-width: 100%; }
}
"""

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Generate static HTML report from DuckDB.")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--out", default=DEFAULT_OUT)
    args = p.parse_args()
    generate(args.db, args.out)
