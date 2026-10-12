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


if __name__ == "__main__":
    app()
