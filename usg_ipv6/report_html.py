"""Generate a static HTML report page from the DuckDB."""

import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = "data/usg_ipv6.duckdb"
DEFAULT_OUT = "web/index.html"

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

main { max-width: 1100px; margin: 2rem auto; padding: 0 1rem; }
section { margin-bottom: 2.5rem; }
h2 {
    font-size: 1.1rem; font-weight: 600; margin-bottom: 1rem;
    padding-bottom: 0.5rem; border-bottom: 2px solid var(--border);
}

.stat-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 1rem; margin-bottom: 1.5rem;
}
.stat-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 1.25rem 1.5rem;
}
.stat-card .label {
    font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--muted); font-weight: 600;
}
.stat-card .value { font-size: 2.25rem; font-weight: 700; margin-top: 0.2rem; }
.stat-card .sub { font-size: 0.82rem; color: var(--muted); margin-top: 0.2rem; }
.stat-card.ipv6 { border-left: 4px solid #0969da; }
.stat-card.ipv4 { border-left: 4px solid #1a7f37; }
.stat-card.corpus { border-left: 4px solid #8250df; }
.stat-card.ts { border-left: 4px solid #bf8700; }

.card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; overflow-x: auto;
}
table { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
th {
    background: var(--th-bg); color: var(--th-text); font-weight: 600;
    text-align: left; padding: 0.6rem 0.85rem; white-space: nowrap;
}
td { padding: 0.5rem 0.85rem; border-bottom: 1px solid var(--border); }
tr:last-child td { border-bottom: none; }
tr:nth-child(even) td { background: var(--even-row); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.tag {
    display: inline-block; border-radius: 3px;
    padding: 0.1rem 0.45rem; font-size: 0.78rem; font-weight: 500;
}
.tag.v6 { background: #ddf4ff; color: #0550ae; }
.tag.v4 { background: #dafbe1; color: #116329; }

.placeholder {
    text-align: center; padding: 4rem 2rem; color: var(--muted);
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
}
.placeholder h3 { margin-bottom: 0.5rem; color: var(--text); }

footer {
    text-align: center; padding: 2rem 1rem;
    color: var(--muted); font-size: 0.82rem;
    border-top: 1px solid var(--border); margin-top: 2rem;
}

@media (max-width: 640px) {
    header { padding: 1.5rem 1rem; }
    .stat-card .value { font-size: 1.75rem; }
    th, td { padding: 0.45rem 0.6rem; }
}
"""


def _af_tag(af: int) -> str:
    cls = "v6" if af == 6 else "v4"
    return f'<span class="tag {cls}">IPv{af}</span>'


def _rows(headers: list[str], body_html: str) -> str:
    ths = "".join(f"<th>{h}</th>" for h in headers)
    return f"""<div class="card"><table>
<thead><tr>{ths}</tr></thead>
<tbody>
{body_html}
</tbody>
</table></div>"""


def _ark_reachability_section(conn) -> str:
    """Generate HTML for the Ark reachability section, or a placeholder."""
    count = conn.execute("SELECT COUNT(*) FROM ark_reachability").fetchone()[0]
    if count == 0:
        return """
        <section id="ark">
          <h2>IPv6 Reachability (CAIDA Ark)</h2>
          <p class="muted" style="color:var(--muted)">Ark reachability data not yet loaded.
          Run <code>usg ark ingest</code> or trigger the Ark Ingest workflow.</p>
        </section>
        """

    mdate = conn.execute(
        "SELECT MAX(measurement_date) FROM ark_reachability"
    ).fetchone()[0]
    pfx2as_date = conn.execute(
        "SELECT pfx2as_snapshot_date FROM ark_reachability "
        "WHERE measurement_date = ? LIMIT 1", [mdate]
    ).fetchone()[0]

    totals = conn.execute("""
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

    reachable, not_reachable, indet = totals
    gap_rows_html = "".join(
        f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td>"
        f"<td class='num'>{r[3]}</td><td class='num'>{r[4]}</td></tr>"
        for r in gap_rows
    )
    if gap_rows:
        gap_table = _rows(
            ["ASN", "Agency", "Tier", "Addresses Observed", "Prefixes in Scope"],
            gap_rows_html,
        )
    else:
        gap_table = "<p style='color:var(--muted)'>No gap ASNs — all assessed ASNs are reachable.</p>"

    return f"""
    <section id="ark">
      <h2>IPv6 Reachability (CAIDA Ark)</h2>
      <p style="color:var(--muted);margin-bottom:1rem;font-size:0.9rem">
        Measurement date: <strong>{mdate}</strong> &nbsp;|&nbsp;
        pfx2as attribution: <strong>{pfx2as_date}</strong>
      </p>
      <div class="stat-grid">
        <div class="stat-card">
          <div class="label">Reachable</div>
          <div class="value" style="color:#1a7f37">{reachable}</div>
        </div>
        <div class="stat-card">
          <div class="label">Not Reachable</div>
          <div class="value" style="color:#cf222e">{not_reachable}</div>
        </div>
        <div class="stat-card">
          <div class="label">Indeterminate</div>
          <div class="value" style="color:#9a6700">{indet}</div>
          <div class="sub">no pfx2as IPv6 prefixes</div>
        </div>
      </div>
      <h3 style="margin-top:1.5rem;margin-bottom:0.75rem;font-size:1rem;font-weight:600">
        Gap: BGP IPv6 announced but not Ark-reachable
      </h3>
      {gap_table}
    </section>
    """


def generate(db_path: str = DEFAULT_DB, out_path: str = DEFAULT_OUT) -> None:
    import duckdb

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    if not Path(db_path).exists():
        html = _page(generated_at, None, 0, [], [], [], [], "")
        Path(out_path).write_text(html, encoding="utf-8")
        print(f"No DuckDB found — wrote placeholder to {out_path}")
        return

    conn = duckdb.connect(db_path)

    latest_ts = conn.execute(
        "SELECT MAX(snapshot_ts) FROM prefix_records"
    ).fetchone()[0]

    corpus_count = conn.execute("SELECT COUNT(*) FROM asn_corpus").fetchone()[0]

    corpus_rows = conn.execute("""
        SELECT asn, agency, tier, confidence, org_name, sources, last_verified
        FROM asn_corpus ORDER BY tier, agency, asn
    """).fetchall()

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

    if latest_ts:
        totals = conn.execute("""
            SELECT af,
                   COUNT(DISTINCT prefix)     AS prefixes,
                   COUNT(DISTINCT origin_asn) AS asns
            FROM prefix_records WHERE snapshot_ts = ?
            GROUP BY af ORDER BY af
        """, [latest_ts]).fetchall()

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
        totals, by_tier = [], []

    ark_section = _ark_reachability_section(conn)
    conn.close()

    html = _page(generated_at, latest_ts, corpus_count, corpus_rows,
                 snap_rows, totals, by_tier, ark_section)
    Path(out_path).write_text(html, encoding="utf-8")
    print(f"Report written to {out_path}")


def _page(
    generated_at: str,
    latest_ts,
    corpus_count: int,
    corpus_rows: list,
    snap_rows: list,
    totals: list,
    by_tier: list,
    ark_section: str = "",
) -> str:
    snap_label = f"Snapshot: {latest_ts}" if latest_ts else "No routing data yet"

    # ── Stat cards ──────────────────────────────────────────────────────────
    ipv4 = next((r for r in totals if r[0] == 4), None)
    ipv6 = next((r for r in totals if r[0] == 6), None)

    def _card(cls, label, value, sub):
        return (f'<div class="stat-card {cls}">'
                f'<div class="label">{label}</div>'
                f'<div class="value">{value}</div>'
                f'<div class="sub">{sub}</div></div>')

    cards = "".join([
        _card("ipv6", "IPv6 Prefixes",
              f"{ipv6[1]:,}" if ipv6 else "—",
              f"from {ipv6[2]:,} ASNs" if ipv6 else "no data yet"),
        _card("ipv4", "IPv4 Prefixes",
              f"{ipv4[1]:,}" if ipv4 else "—",
              f"from {ipv4[2]:,} ASNs" if ipv4 else "no data yet"),
        _card("corpus", "Corpus ASNs",
              f"{corpus_count:,}",
              "tracked organizations"),
        _card("ts", "Latest Snapshot",
              f'<span style="font-size:1rem">{latest_ts or "—"}</span>',
              "UTC" if latest_ts else "run ingest workflow"),
    ])
    stat_grid = f'<div class="stat-grid">{cards}</div>'

    # ── Summary table ────────────────────────────────────────────────────────
    if totals:
        summary_body = "".join(
            f"<tr><td>{_af_tag(r[0])}</td>"
            f"<td class='num'>{r[1]:,}</td>"
            f"<td class='num'>{r[2]:,}</td></tr>"
            for r in totals
        )
    else:
        summary_body = "<tr><td colspan='3'>Run the RouteViews MRT Ingest workflow to populate data.</td></tr>"

    summary_tbl = _rows(["Address Family", "Prefixes", "Origin ASNs"], summary_body)

    # ── By-tier table ────────────────────────────────────────────────────────
    if by_tier:
        tier_body = "".join(
            f"<tr><td>{r[0]}</td><td>{_af_tag(r[1])}</td>"
            f"<td class='num'>{r[2]:,}</td><td class='num'>{r[3]:,}</td></tr>"
            for r in by_tier
        )
    else:
        tier_body = "<tr><td colspan='4'>No data yet.</td></tr>"

    tier_tbl = _rows(["Tier", "AF", "Prefixes", "ASNs"], tier_body)

    # ── Snapshot inventory ───────────────────────────────────────────────────
    if snap_rows:
        snap_body = "".join(
            f"<tr><td>{r[0]}</td>"
            f"<td class='num'>{r[1]:,}</td>"
            f"<td class='num'>{r[2]:,}</td>"
            f"<td class='num'>{r[3]:,}</td>"
            f"<td>{r[4] or '—'}</td><td>{r[5] or '—'}</td></tr>"
            for r in snap_rows
        )
    else:
        snap_body = "<tr><td colspan='6'>No snapshots recorded yet.</td></tr>"

    snap_tbl = _rows(
        ["Collector", "Total", "Processed", "Failed", "Earliest", "Latest"],
        snap_body,
    )

    # ── Corpus table ─────────────────────────────────────────────────────────
    if corpus_rows:
        corpus_body = "".join(
            f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td>"
            f"<td>{r[3]}</td><td>{r[4]}</td><td>{r[5]}</td><td>{r[6]}</td></tr>"
            for r in corpus_rows
        )
    else:
        corpus_body = "<tr><td colspan='7'>Run the Corpus Update workflow.</td></tr>"

    corpus_tbl = _rows(
        ["ASN", "Agency", "Tier", "Confidence", "Org Name", "Sources", "Verified"],
        corpus_body,
    )

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
  <p class="subtitle">IPv4 and IPv6 prefix deployment for US Government Autonomous Systems</p>
  <p class="meta">Updated: {generated_at}&nbsp;&nbsp;·&nbsp;&nbsp;{snap_label}</p>
</header>
<main>

<section id="summary">
  <h2>Summary</h2>
  {stat_grid}
  {summary_tbl}
</section>

<section id="by-tier">
  <h2>By Tier and Address Family</h2>
  {tier_tbl}
</section>

<section id="snapshots">
  <h2>Snapshot Inventory</h2>
  {snap_tbl}
</section>

{ark_section}

<section id="corpus">
  <h2>ASN Corpus ({corpus_count:,} ASNs)</h2>
  {corpus_tbl}
</section>

</main>
<footer>
  <p>
    Source data:
    <a href="http://www.routeviews.org/">RouteViews MRT</a> ·
    <a href="https://www.arin.net/resources/registry/whois/">ARIN WHOIS</a> ·
    <a href="https://www.caida.org/catalog/datasets/as-organizations/">CAIDA AS2Org</a> ·
    <a href="https://github.com/buraglio/usg-ipv6-stats">GitHub</a>
  </p>
</footer>
</body>
</html>"""


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Generate static HTML report from DuckDB.")
    p.add_argument("--db", default=DEFAULT_DB, help="Path to DuckDB file.")
    p.add_argument("--out", default=DEFAULT_OUT, help="Output HTML path.")
    args = p.parse_args()
    generate(args.db, args.out)
