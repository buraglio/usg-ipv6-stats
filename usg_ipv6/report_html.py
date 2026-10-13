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
    "ic": "Intel Community",
    "independent": "Independent",
}

_TIER_DESCRIPTIONS = {
    "cfo_act": "The 24 cabinet-level and largest executive branch agencies named in the Chief Financial Officers Act of 1990.",
    "dod": "Department of Defense components and commands.",
    "ic": "Intelligence Community elements.",
    "independent": "Independent federal agencies and commissions.",
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
    chart_v6_prefixes = [_dates_set[d].get(6, {}).get("p", 0) for d in chart_dates]
    chart_v4_prefixes = [_dates_set[d].get(4, {}).get("p", 0) for d in chart_dates]
    chart_v6_asns = [_dates_set[d].get(6, {}).get("a", 0) for d in chart_dates]
    chart_v4_asns = [_dates_set[d].get(4, {}).get("a", 0) for d in chart_dates]

    # ── Latest-snapshot summary ───────────────────────────────────────────────
    latest_ts = conn.execute(
        "SELECT MAX(snapshot_ts) FROM prefix_records"
    ).fetchone()[0]

    corpus_count = conn.execute("SELECT COUNT(*) FROM asn_corpus").fetchone()[0]

    if latest_ts:
        totals = {r[0]: r for r in conn.execute("""
            SELECT af,
                   COUNT(DISTINCT prefix)     AS prefixes,
                   COUNT(DISTINCT origin_asn) AS asns
            FROM prefix_records WHERE snapshot_ts = ?
            GROUP BY af
        """, [latest_ts]).fetchall()}
    else:
        totals = {}

    # ── By-tier ──────────────────────────────────────────────────────────────
    if latest_ts:
        by_tier = conn.execute("""
            SELECT c.tier, pr.af,
                   COUNT(DISTINCT pr.prefix)     AS prefixes,
                   COUNT(DISTINCT pr.origin_asn) AS asns
            FROM prefix_records pr
            JOIN asn_corpus c ON pr.origin_asn = c.asn
            WHERE pr.snapshot_ts = ?
            GROUP BY c.tier, pr.af ORDER BY c.tier, pr.af
        """, [latest_ts]).fetchall()
    else:
        by_tier = []

    # ── Corpus ───────────────────────────────────────────────────────────────
    corpus_rows = conn.execute("""
        SELECT asn, agency, tier, confidence, org_name, sources, last_verified
        FROM asn_corpus ORDER BY tier, agency, asn
    """).fetchall()

    # ── Per-ASN current prefixes (for ASN explorer) ───────────────────────────
    if latest_ts:
        asn_prefix_rows = conn.execute("""
            SELECT origin_asn, af, prefix
            FROM prefix_records
            WHERE snapshot_ts = ?
              AND origin_asn IN (SELECT asn FROM asn_corpus)
            ORDER BY origin_asn, af, prefix
        """, [latest_ts]).fetchall()
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
    }, separators=(",", ":"))

    html_out = _page(
        generated_at=generated_at,
        latest_ts=latest_ts,
        corpus_count=corpus_count,
        corpus_rows=corpus_rows,
        snap_rows=snap_rows,
        totals=totals,
        by_tier=by_tier,
        ark_data=ark_data,
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
    generated_at, latest_ts, corpus_count, corpus_rows,
    snap_rows, totals, by_tier, ark_data, js_data,
) -> str:
    snap_label = f"Latest snapshot: {latest_ts}" if latest_ts else "No routing data yet"

    ipv6 = totals.get(6)
    ipv4 = totals.get(4)

    # Adoption % = corpus ASNs with any IPv6 / total corpus ASNs
    ipv6_asn_count = ipv6[2] if ipv6 else 0
    adoption_pct = f"{100 * ipv6_asn_count / corpus_count:.1f}%" if corpus_count else "—"

    def _card(cls, label, value, sub=""):
        return (f'<div class="stat-card {cls}">'
                f'<div class="label">{label}</div>'
                f'<div class="value">{value}</div>'
                f'{"<div class=sub>" + sub + "</div>" if sub else ""}</div>')

    cards = "".join([
        _card("ipv6", "IPv6 Adoption",
              adoption_pct if corpus_count else "—",
              f"{ipv6_asn_count} of {corpus_count} corpus ASNs" if corpus_count else "no data"),
        _card("ipv6b", "IPv6 Prefixes",
              f"{ipv6[1]:,}" if ipv6 else "—",
              "visible in BGP table" if ipv6 else "run RouteViews ingest"),
        _card("ipv4", "IPv4 Prefixes",
              f"{ipv4[1]:,}" if ipv4 else "—",
              f"from {ipv4[2]:,} ASNs" if ipv4 else "no data"),
        _card("corpus", "Corpus ASNs",
              f"{corpus_count:,}",
              "tracked USG organizations"),
    ])

    # ── By-tier table ─────────────────────────────────────────────────────────
    if by_tier:
        tier_body = "".join(
            f"<tr><td>{_esc(_tier_label(r[0]))}</td>"
            f"<td>{_af_tag(r[1])}</td>"
            f"<td class='num'>{r[2]:,}</td>"
            f"<td class='num'>{r[3]:,}</td></tr>"
            for r in by_tier
        )
        tier_tbl = _table(["Tier", "AF", "Prefixes", "ASNs"], tier_body)
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
  <a href="#asn-explorer">ASN Explorer</a>
  <a href="#data-sources">Data Sources</a>
</nav>

<main>

<section id="overview">
  <h2>Overview</h2>
  <p class="section-blurb">
    This dashboard tracks IPv4 and IPv6 prefix announcements for US Government
    autonomous systems (ASNs) as observed in the global BGP routing table via
    <a href="http://www.routeviews.org/">RouteViews</a> collectors. Prefix counts
    reflect distinct network blocks visible at the time of the snapshot.
    An ASN is counted as "IPv6 active" when it originates at least one IPv6 prefix.
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
    <canvas id="trend-chart" height="120"></canvas>
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

<section id="asn-explorer">
  <h2>ASN Explorer</h2>
  <p class="section-blurb">
    Browse all {corpus_count:,} ASNs in the tracking corpus.
    Search by agency name or ASN number, then click any row to see
    that ASN's current prefix announcements and historical trend.
  </p>
  <div class="search-bar">
    <input type="search" id="asn-search" placeholder="Search agency or ASN…"
           oninput="filterASNs(this.value)" autocomplete="off">
    <span id="asn-count" class="asn-count"></span>
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
  </p>
  {snap_tbl}
</section>

</main>

<footer>
  <p>
    Source data:
    <a href="http://www.routeviews.org/">RouteViews MRT</a> ·
    <a href="https://www.arin.net/resources/registry/whois/">ARIN WHOIS</a> ·
    <a href="https://www.caida.org/catalog/datasets/as-organizations/">CAIDA AS2Org</a> ·
    <a href="https://publicdata.caida.org/datasets/routing/routeviews6-prefix2as/">CAIDA pfx2as rv6</a>
  </p>
  <p style="margin-top:0.4rem">
    <a href="https://github.com/buraglio/usg-ipv6-stats">github.com/buraglio/usg-ipv6-stats</a>
  </p>
</footer>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.3/chart.umd.min.js"
  integrity="sha512-ejHEoMeGdLBm7sRe6MShI/kGU09bIVpOSyX6P1Vm7IHwvdRRh3bjKFOtU7K2NRcKFJX41MiWVKdEWRsQv9gg=="
  crossorigin="anonymous" referrerpolicy="no-referrer"></script>
<script>
const DATA = {js_data};

// ── Trend chart ───────────────────────────────────────────────────────────────
(function() {{
  const ctx = document.getElementById('trend-chart');
  if (!ctx || !DATA.dates.length) {{
    if (ctx) ctx.parentElement.innerHTML =
      '<p class="empty" style="padding:2rem">No snapshot data yet — run the RouteViews MRT Ingest workflow.</p>';
    return;
  }}
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: DATA.dates,
      datasets: [
        {{
          label: 'IPv6 Prefixes',
          data: DATA.v6_prefixes,
          borderColor: '#0969da',
          backgroundColor: 'rgba(9,105,218,0.08)',
          tension: 0.3, fill: true, pointRadius: 4,
        }},
        {{
          label: 'IPv4 Prefixes',
          data: DATA.v4_prefixes,
          borderColor: '#1a7f37',
          backgroundColor: 'rgba(26,127,55,0.06)',
          tension: 0.3, fill: true, pointRadius: 4,
        }},
        {{
          label: 'IPv6 ASNs',
          data: DATA.v6_asns,
          borderColor: '#8250df',
          borderDash: [5,3],
          backgroundColor: 'transparent',
          tension: 0.3, fill: false, pointRadius: 3,
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
      }},
      scales: {{
        y: {{ beginAtZero: true, ticks: {{ precision: 0 }} }},
        x: {{ ticks: {{ maxRotation: 45 }} }},
      }},
    }},
  }});
}})();

// ── ASN explorer ──────────────────────────────────────────────────────────────
let asnChart = null;

function filterASNs(q) {{
  const rows = document.querySelectorAll('#corpus-table .asn-row');
  q = q.toLowerCase().trim();
  let shown = 0;
  rows.forEach(row => {{
    const match = !q ||
      row.dataset.asn.includes(q) ||
      row.dataset.agency.includes(q) ||
      row.dataset.tier.includes(q);
    row.style.display = match ? '' : 'none';
    if (match) shown++;
  }});
  const badge = document.getElementById('asn-count');
  if (badge) badge.textContent = q ? `${{shown}} of ${{rows.length}} ASNs` : `${{rows.length}} ASNs`;
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

  // History chart
  const hist = DATA.asn_history[key] || {{}};
  const histDates = Object.keys(hist).sort();
  const histV6 = histDates.map(d => hist[d].v6 || 0);
  const histV4 = histDates.map(d => hist[d].v4 || 0);

  if (asnChart) {{ asnChart.destroy(); asnChart = null; }}
  const chartCtx = document.getElementById('asn-chart');
  if (histDates.length) {{
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
  }} else {{
    chartCtx.style.display = 'none';
  }}

  detail.style.display = 'block';
  detail.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});

  // Highlight selected row
  document.querySelectorAll('.asn-row').forEach(r => r.classList.remove('selected'));
  if (row) row.classList.add('selected');
}}

function closeASN() {{
  document.getElementById('asn-detail').style.display = 'none';
  document.querySelectorAll('.asn-row').forEach(r => r.classList.remove('selected'));
  if (asnChart) {{ asnChart.destroy(); asnChart = null; }}
}}

// Initialize count badge
(function() {{
  const rows = document.querySelectorAll('#corpus-table .asn-row');
  const badge = document.getElementById('asn-count');
  if (badge) badge.textContent = `${{rows.length}} ASNs`;
}})();
</script>

</body>
</html>"""


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
.tag.v6 { background: #ddf4ff; color: #0550ae; }
.tag.v4 { background: #dafbe1; color: #116329; }

.tier-tag {
  display: inline-block; border-radius: 12px;
  padding: 0.1rem 0.6rem; font-size: 0.78rem; font-weight: 500;
  background: #f0f2f5; color: #656d76; border: 1px solid #d0d7de;
}
.tier-tag.tier-cfo_act { background: #ddf4ff; color: #0550ae; border-color: #b6e3ff; }
.tier-tag.tier-dod     { background: #ffd8b2; color: #953800; border-color: #ffc680; }
.tier-tag.tier-ic      { background: #f3d2ff; color: #6639ba; border-color: #e2b8ff; }

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

.asn-detail {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; margin-bottom: 1rem; overflow: hidden;
  box-shadow: 0 4px 16px rgba(0,0,0,0.06);
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
