import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import duckdb
import typer

DEFAULT_DB = "data/usg_ipv6.duckdb"

app = typer.Typer(
    name="usg",
    help="USG IPv6 statistics — query the local DuckDB or run the ingest pipeline.",
    no_args_is_help=True,
)
run_app = typer.Typer(help="Run ingest pipeline operations.", no_args_is_help=True)
app.add_typer(run_app, name="run", help="Run ingest pipeline operations.")

ark_app = typer.Typer(help="CAIDA Ark IPv6 reachability commands.", no_args_is_help=True)
app.add_typer(ark_app, name="ark", help="CAIDA Ark IPv6 reachability commands.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open(db: str) -> duckdb.DuckDBPyConnection:
    if not Path(db).exists():
        typer.echo(f"Error: database not found: {db}", err=True)
        typer.echo(
            "Run 'usg run ingest' or trigger the RouteViews MRT Ingest workflow first.",
            err=True,
        )
        raise typer.Exit(1)
    return duckdb.connect(db)


def _table(headers: list[str], rows: list[tuple], *, max_col: int = 45) -> None:
    if not rows:
        typer.echo("(no results)")
        return
    str_rows = [[str(c)[:max_col] for c in row] for row in rows]
    widths = [max(len(h), max((len(r[i]) for r in str_rows), default=0))
              for i, h in enumerate(headers)]
    gap = "  "
    typer.echo(gap.join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    typer.echo(gap.join("-" * w for w in widths))
    for row in str_rows:
        typer.echo(gap.join(c.ljust(widths[i]) for i, c in enumerate(row)))


def _latest_snapshot(conn: duckdb.DuckDBPyConnection) -> object:
    ts = conn.execute("SELECT MAX(snapshot_ts) FROM prefix_records").fetchone()[0]
    if ts is None:
        typer.echo("No prefix records found. Run 'usg run ingest' first.", err=True)
        raise typer.Exit(1)
    return ts


# ---------------------------------------------------------------------------
# usg stats
# ---------------------------------------------------------------------------

@app.command()
def stats(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Path to DuckDB file."),
    as_json: bool = typer.Option(False, "--json", is_flag=True, help="Output as JSON."),
) -> None:
    """IPv4/IPv6 prefix and ASN summary for the most recent snapshot."""
    conn = _open(db)
    snap = _latest_snapshot(conn)

    corpus_count = conn.execute("SELECT COUNT(*) FROM asn_corpus").fetchone()[0]

    totals = conn.execute("""
        SELECT af,
               COUNT(DISTINCT prefix)     AS prefixes,
               COUNT(DISTINCT origin_asn) AS asns
        FROM prefix_records
        WHERE snapshot_ts = ?
        GROUP BY af ORDER BY af
    """, [snap]).fetchall()

    by_tier = conn.execute("""
        SELECT c.tier, pr.af,
               COUNT(DISTINCT pr.prefix)     AS prefixes,
               COUNT(DISTINCT pr.origin_asn) AS asns
        FROM prefix_records pr
        JOIN asn_corpus c ON pr.origin_asn = c.asn
        WHERE pr.snapshot_ts = ?
        GROUP BY c.tier, pr.af
        ORDER BY c.tier, pr.af
    """, [snap]).fetchall()
    conn.close()

    if as_json:
        typer.echo(json.dumps({
            "snapshot_ts": str(snap),
            "corpus_asns": corpus_count,
            "totals": [{"af": r[0], "prefixes": r[1], "asns": r[2]} for r in totals],
            "by_tier": [{"tier": r[0], "af": r[1], "prefixes": r[2], "asns": r[3]}
                        for r in by_tier],
        }, indent=2))
        return

    typer.echo(f"Snapshot : {snap}")
    typer.echo(f"Corpus   : {corpus_count:,} ASNs\n")
    typer.echo("All USG:")
    _table(["AF", "Prefixes", "ASNs"],
           [(f"IPv{r[0]}", f"{r[1]:,}", f"{r[2]:,}") for r in totals])
    typer.echo()
    typer.echo("By tier:")
    _table(["Tier", "AF", "Prefixes", "ASNs"],
           [(r[0], f"IPv{r[1]}", f"{r[2]:,}", f"{r[3]:,}") for r in by_tier])


# ---------------------------------------------------------------------------
# usg corpus
# ---------------------------------------------------------------------------

@app.command()
def corpus(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Path to DuckDB file."),
    asn: Optional[int] = typer.Option(None, "--asn", help="Filter by ASN."),
    tier: Optional[str] = typer.Option(None, "--tier", help="Filter by tier (1, 2, independent)."),
    agency: Optional[str] = typer.Option(None, "--agency", help="Partial agency name match."),
    limit: int = typer.Option(100, "--limit", help="Max rows to display."),
    as_json: bool = typer.Option(False, "--json", is_flag=True, help="Output as JSON."),
) -> None:
    """List ASNs in the corpus, with optional filters."""
    conn = _open(db)

    clauses: list[str] = []
    params: list[object] = []
    if asn is not None:
        clauses.append("asn = ?")
        params.append(asn)
    if tier is not None:
        clauses.append("tier = ?")
        params.append(tier)
    if agency is not None:
        clauses.append("agency ILIKE ?")
        params.append(f"%{agency}%")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"""
        SELECT asn, agency, tier, confidence, org_name, sources, last_verified
        FROM asn_corpus {where}
        ORDER BY tier, agency, asn
        LIMIT ?
    """, params + [limit]).fetchall()
    conn.close()

    if as_json:
        typer.echo(json.dumps([
            {"asn": r[0], "agency": r[1], "tier": r[2], "confidence": r[3],
             "org_name": r[4], "sources": r[5], "last_verified": str(r[6])}
            for r in rows
        ], indent=2))
        return

    typer.echo(f"{len(rows)} ASN(s)\n")
    _table(["ASN", "Agency", "Tier", "Confidence", "Org Name", "Sources", "Verified"],
           [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows])


# ---------------------------------------------------------------------------
# usg prefixes
# ---------------------------------------------------------------------------

@app.command()
def prefixes(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Path to DuckDB file."),
    af: Optional[int] = typer.Option(None, "--af", help="Address family: 4 or 6."),
    tier: Optional[str] = typer.Option(None, "--tier", help="Filter by tier."),
    asn: Optional[int] = typer.Option(None, "--asn", help="Filter by origin ASN."),
    snapshot: Optional[str] = typer.Option(None, "--snapshot", help="Snapshot timestamp (default: latest)."),
    limit: int = typer.Option(50, "--limit", help="Max rows to display."),
    as_json: bool = typer.Option(False, "--json", is_flag=True, help="Output as JSON."),
) -> None:
    """List prefix records for a snapshot, optionally filtered."""
    conn = _open(db)
    snap = snapshot or _latest_snapshot(conn)

    clauses: list[str] = ["pr.snapshot_ts = ?"]
    params: list[object] = [snap]
    if af is not None:
        clauses.append("pr.af = ?")
        params.append(af)
    if asn is not None:
        clauses.append("pr.origin_asn = ?")
        params.append(asn)
    if tier is not None:
        clauses.append("c.tier = ?")
        params.append(tier)

    rows = conn.execute(f"""
        SELECT pr.prefix, pr.af, pr.origin_asn, COALESCE(c.agency, '—'), COALESCE(c.tier, '—')
        FROM prefix_records pr
        LEFT JOIN asn_corpus c ON pr.origin_asn = c.asn
        WHERE {' AND '.join(clauses)}
        GROUP BY pr.prefix, pr.af, pr.origin_asn, c.agency, c.tier
        ORDER BY pr.af, pr.prefix
        LIMIT ?
    """, params + [limit]).fetchall()
    conn.close()

    if as_json:
        typer.echo(json.dumps([
            {"prefix": r[0], "af": r[1], "origin_asn": r[2], "agency": r[3], "tier": r[4]}
            for r in rows
        ], indent=2))
        return

    typer.echo(f"Snapshot: {snap}  (up to {limit} rows)\n")
    _table(["Prefix", "AF", "Origin ASN", "Agency", "Tier"],
           [(r[0], f"IPv{r[1]}", r[2], r[3], r[4]) for r in rows])


# ---------------------------------------------------------------------------
# usg snapshots
# ---------------------------------------------------------------------------

@app.command()
def snapshots(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Path to DuckDB file."),
    collector: Optional[str] = typer.Option(None, "--collector", help="Filter by collector."),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status (processed, failed, skipped)."),
    limit: int = typer.Option(50, "--limit", help="Max rows to display."),
    as_json: bool = typer.Option(False, "--json", is_flag=True, help="Output as JSON."),
) -> None:
    """List processed RIB snapshots."""
    conn = _open(db)

    clauses: list[str] = []
    params: list[object] = []
    if collector:
        clauses.append("collector = ?")
        params.append(collector)
    if status:
        clauses.append("status = ?")
        params.append(status)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"""
        SELECT snapshot_ts, collector, status, byte_count, processed_at
        FROM snapshots {where}
        ORDER BY snapshot_ts DESC
        LIMIT ?
    """, params + [limit]).fetchall()
    conn.close()

    if as_json:
        typer.echo(json.dumps([
            {"snapshot_ts": str(r[0]), "collector": r[1], "status": r[2],
             "byte_count": r[3], "processed_at": str(r[4])}
            for r in rows
        ], indent=2))
        return

    typer.echo(f"{len(rows)} snapshot(s)\n")
    _table(["Snapshot", "Collector", "Status", "Bytes", "Processed At"],
           [(r[0], r[1], r[2], f"{r[3]:,}" if r[3] else "—", r[4] or "—") for r in rows])


# ---------------------------------------------------------------------------
# usg run ingest
# ---------------------------------------------------------------------------

@run_app.command("ingest")
def run_ingest(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Path to DuckDB file."),
    collector: str = typer.Option("route-views2", "--collector", help="RouteViews collector name."),
    limit: int = typer.Option(1, "--limit", help="Max snapshots to fetch and process."),
) -> None:
    """Fetch and process recent RIB snapshots (incremental)."""
    from usg_ipv6.pipeline.runner import run_incremental
    typer.echo(f"Ingest: collector={collector}, limit={limit}, db={db}")
    result = run_incremental(db, collector, limit=limit)
    typer.echo(f"Done. Processed: {result['processed']}")


# ---------------------------------------------------------------------------
# usg run backfill
# ---------------------------------------------------------------------------

@run_app.command("backfill")
def run_backfill(
    since: str = typer.Argument(..., help="Start date, YYYY-MM-DD."),
    until: str = typer.Argument(..., help="End date, YYYY-MM-DD."),
    db: str = typer.Option(DEFAULT_DB, "--db", help="Path to DuckDB file."),
    collector: str = typer.Option("route-views2", "--collector", help="RouteViews collector name."),
) -> None:
    """Backfill RIB snapshots between two dates (inclusive)."""
    from usg_ipv6.pipeline.runner import run_backfill as _backfill
    try:
        since_dt = datetime.fromisoformat(since)
        until_dt = datetime.fromisoformat(until)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Backfill: collector={collector}, {since} → {until}, db={db}")
    result = _backfill(db, collector, since_dt, until_dt)
    typer.echo(f"Done. Processed: {result['processed']}")


# ---------------------------------------------------------------------------
# usg ark ingest
# ---------------------------------------------------------------------------

@ark_app.command("ingest")
def ark_ingest(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Path to DuckDB file."),
    date: Optional[str] = typer.Option(
        None, "--date", help="Measurement date (YYYY-MM-DD). Default: latest available."
    ),
    dest_dir: str = typer.Option("data/raw", "--dest-dir", help="Download directory."),
) -> None:
    """Ingest Ark IPv6 reachability data for one date."""
    from usg_ipv6.pipeline.ark import run_ark_ingest
    typer.echo(f"Ark ingest: date={date or 'latest'}, db={db}")
    result = run_ark_ingest(db, date=date, dest_dir=dest_dir)
    if result["skipped"]:
        typer.echo(f"Skipped — {result['date']} already loaded.")
    else:
        typer.echo(
            f"Done. date={result['date']}  parsed={result['addresses_parsed']:,}"
            f"  asns_updated={result['asns_updated']}"
            f"  pfx2as={result['pfx2as_snapshot_date']}"
        )


# ---------------------------------------------------------------------------
# usg ark backfill
# ---------------------------------------------------------------------------

@ark_app.command("backfill")
def ark_backfill(
    since: str = typer.Argument(..., help="Start date, YYYY-MM-DD."),
    until: str = typer.Argument(..., help="End date, YYYY-MM-DD."),
    db: str = typer.Option(DEFAULT_DB, "--db", help="Path to DuckDB file."),
    dest_dir: str = typer.Option("data/raw", "--dest-dir", help="Download directory."),
) -> None:
    """Backfill Ark reachability data between two dates (inclusive)."""
    from usg_ipv6.pipeline.ark import run_ark_backfill
    try:
        since_dt = datetime.fromisoformat(since)
        until_dt = datetime.fromisoformat(until)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Ark backfill: {since} → {until}, db={db}")
    result = run_ark_backfill(db, since_dt, until_dt, dest_dir=dest_dir)
    typer.echo(
        f"Done. processed={result['processed']}  skipped={result['skipped']}"
        f"  failed={result['failed']}"
    )


# ---------------------------------------------------------------------------
# usg ark stats
# ---------------------------------------------------------------------------

@ark_app.command("stats")
def ark_stats(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Path to DuckDB file."),
    date: Optional[str] = typer.Option(None, "--date", help="Measurement date (default: latest)."),
    as_json: bool = typer.Option(False, "--json", is_flag=True, help="Output as JSON."),
) -> None:
    """Reachability summary for the most recent (or given) Ark measurement date."""
    conn = _open(db)
    mdate = date or conn.execute(
        "SELECT MAX(measurement_date) FROM ark_reachability"
    ).fetchone()[0]
    if mdate is None:
        typer.echo("No Ark data loaded. Run 'usg ark ingest' first.", err=True)
        raise typer.Exit(1)

    pfx2as_date = conn.execute(
        "SELECT pfx2as_snapshot_date FROM ark_reachability WHERE measurement_date = ? LIMIT 1",
        [mdate],
    ).fetchone()
    pfx2as_str = str(pfx2as_date[0]) if pfx2as_date else "unknown"

    totals = conn.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE ipv6_reachable = TRUE AND prefixes_in_scope > 0) AS reachable,
            COUNT(*) FILTER (WHERE ipv6_reachable = FALSE AND prefixes_in_scope > 0) AS not_reachable,
            COUNT(*) FILTER (WHERE prefixes_in_scope = 0) AS indeterminate
        FROM ark_reachability WHERE measurement_date = ?
        """,
        [mdate],
    ).fetchone()

    by_tier = conn.execute(
        """
        SELECT c.tier,
            COUNT(*) FILTER (WHERE ar.ipv6_reachable = TRUE AND ar.prefixes_in_scope > 0),
            COUNT(*) FILTER (WHERE ar.ipv6_reachable = FALSE AND ar.prefixes_in_scope > 0)
        FROM ark_reachability ar
        JOIN asn_corpus c ON ar.asn = c.asn
        WHERE ar.measurement_date = ?
        GROUP BY c.tier ORDER BY c.tier
        """,
        [mdate],
    ).fetchall()

    avg_div = conn.execute(
        """
        SELECT AVG(CASE WHEN addresses_observed > 0
                        THEN asn_divergence_count * 1.0 / addresses_observed
                        ELSE NULL END) * 100
        FROM ark_reachability WHERE measurement_date = ?
        """,
        [mdate],
    ).fetchone()[0]
    conn.close()

    if as_json:
        typer.echo(json.dumps({
            "measurement_date": str(mdate),
            "pfx2as_snapshot_date": pfx2as_str,
            "reachable": totals[0], "not_reachable": totals[1],
            "indeterminate": totals[2],
            "avg_divergence_pct": round(avg_div or 0, 2),
            "by_tier": [{"tier": r[0], "reachable": r[1], "not_reachable": r[2]}
                        for r in by_tier],
        }, indent=2))
        return

    typer.echo(f"Ark measurement : {mdate}")
    typer.echo(f"pfx2as used     : {pfx2as_str}\n")
    typer.echo(f"Reachable       : {totals[0]}")
    typer.echo(f"Not reachable   : {totals[1]}")
    typer.echo(f"Indeterminate   : {totals[2]}  (prefixes_in_scope = 0)\n")
    typer.echo("By tier:")
    _table(["Tier", "Reachable", "Not Reachable"],
           [(r[0], r[1], r[2]) for r in by_tier])
    if avg_div is not None:
        typer.echo(f"\nAttribution divergence: {avg_div:.1f}% (ark_asn ≠ our_asn)")


# ---------------------------------------------------------------------------
# usg ark gap
# ---------------------------------------------------------------------------

@ark_app.command("gap")
def ark_gap(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Path to DuckDB file."),
    tier: Optional[str] = typer.Option(None, "--tier", help="Filter by tier."),
    date: Optional[str] = typer.Option(None, "--date", help="Measurement date (default: latest)."),
    limit: int = typer.Option(100, "--limit", help="Max rows."),
    as_json: bool = typer.Option(False, "--json", is_flag=True, help="Output as JSON."),
) -> None:
    """ASNs with BGP IPv6 prefixes but no Ark reachability (excludes indeterminate)."""
    conn = _open(db)
    mdate = date or conn.execute(
        "SELECT MAX(measurement_date) FROM ark_reachability"
    ).fetchone()[0]
    if mdate is None:
        typer.echo("No Ark data loaded. Run 'usg ark ingest' first.", err=True)
        raise typer.Exit(1)

    tier_clause = "AND c.tier = ?" if tier else ""
    params: list[object] = [mdate] + ([tier] if tier else []) + [limit]
    rows = conn.execute(
        f"""
        SELECT ar.asn, c.agency, c.tier, ar.addresses_observed, ar.prefixes_in_scope
        FROM ark_reachability ar
        JOIN asn_corpus c ON ar.asn = c.asn
        WHERE ar.measurement_date = ?
          AND ar.ipv6_reachable = FALSE
          AND ar.prefixes_in_scope > 0
          {tier_clause}
        ORDER BY c.tier, c.agency
        LIMIT ?
        """,
        params,
    ).fetchall()
    conn.close()

    if as_json:
        typer.echo(json.dumps([
            {"asn": r[0], "agency": r[1], "tier": r[2],
             "addresses_observed": r[3], "prefixes_in_scope": r[4]}
            for r in rows
        ], indent=2))
        return

    typer.echo(f"Gap (measurement: {mdate})  {len(rows)} ASN(s)\n")
    _table(
        ["ASN", "Agency", "Tier", "Addr Observed", "Prefixes"],
        [(r[0], r[1], r[2], r[3], r[4]) for r in rows],
    )


if __name__ == "__main__":
    app()
