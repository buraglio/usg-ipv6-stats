import gzip
import ipaddress
import datetime
from collections import defaultdict
from pathlib import Path
from typing import Iterator, Optional

import requests

from usg_ipv6.db import get_connection, init_schema
from usg_ipv6.pipeline.lpm import build_lpm_index, lpm_lookup_entry

ARK_BASE_URL = (
    "https://publicdata.caida.org/datasets/topology/ark/ipv6/"
    "probe-data/team-1/{year}/{month:02d}/"
)
ARK_FILE_PATTERN = "team-1.ipv6.{date}.nodes.as.gz"

_IPV4_MAPPED_NET = ipaddress.ip_network("::ffff:0.0.0.0/96")


def download_nodes_as(date: str, dest_dir: str) -> str:
    """
    Download Ark IPv6 nodes-as file for `date` (YYYY-MM-DD).
    Walks back up to 6 more days if that date is not yet published.
    Returns local file path. Raises FileNotFoundError if nothing found.
    """
    year, month, day = date.split("-")
    base_date = datetime.date(int(year), int(month), int(day))
    Path(dest_dir).mkdir(parents=True, exist_ok=True)

    for days_back in range(7):
        attempt = base_date - datetime.timedelta(days=days_back)
        date_str = attempt.strftime("%Y%m%d")
        filename = ARK_FILE_PATTERN.format(date=date_str)
        dest = Path(dest_dir) / filename
        if dest.exists():
            return str(dest)
        url = (
            ARK_BASE_URL.format(year=attempt.year, month=attempt.month)
            + filename
        )
        try:
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
            return str(dest)
        except Exception:
            continue

    raise FileNotFoundError(
        f"No Ark nodes-as file found for {date} or the 6 preceding days"
    )


def parse_nodes_as(
    filepath: str,
) -> Iterator[tuple[ipaddress.IPv6Address, Optional[int]]]:
    """
    Yield (IPv6Address, ark_asn) pairs from a nodes-as .gz file.
    ark_asn is None for '*' annotation or non-numeric values.
    Skips comments, non-'N' records, and IPv4-mapped addresses.
    """
    opener = gzip.open if filepath.endswith(".gz") else open
    with opener(filepath, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or not line.startswith("N "):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                addr = ipaddress.ip_address(parts[1])
            except ValueError:
                continue
            if not isinstance(addr, ipaddress.IPv6Address):
                continue
            if addr in _IPV4_MAPPED_NET:
                continue
            ark_asn: Optional[int] = None
            if len(parts) >= 3 and parts[2] != "*":
                try:
                    ark_asn = int(parts[2])
                except ValueError:
                    pass
            yield addr, ark_asn


def _date_from_filepath(filepath: str) -> str:
    """Extract YYYY-MM-DD from a nodes-as filename like team-1.ipv6.YYYYMMDD.nodes.as.gz"""
    stem = Path(filepath).name
    parts = stem.split(".")
    raw = parts[2]  # YYYYMMDD segment
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def run_ark_ingest(
    db_path: str,
    *,
    date: Optional[str] = None,
    dest_dir: str = "data/raw",
) -> dict:
    """Ingest a single Ark measurement date into ark_reachability."""
    conn = get_connection(db_path)
    init_schema(conn)

    target = date or datetime.date.today().isoformat()

    # Skip if already loaded
    existing = conn.execute(
        "SELECT COUNT(*) FROM ark_reachability WHERE measurement_date = ?",
        [target],
    ).fetchone()[0]
    if existing > 0:
        conn.close()
        return {"date": target, "addresses_parsed": 0, "asns_updated": 0,
                "pfx2as_snapshot_date": None, "skipped": True}

    # Require pfx2as data (any AF) — empty table means RouteViews ingest hasn't run
    pfx2as_date_row = conn.execute(
        "SELECT MAX(snapshot_date) FROM caida_pfx2as"
    ).fetchone()
    if pfx2as_date_row is None or pfx2as_date_row[0] is None:
        conn.close()
        raise RuntimeError(
            "caida_pfx2as is empty — run RouteViews ingest first"
        )
    pfx2as_date = str(pfx2as_date_row[0])

    # Load corpus ASNs
    corpus_asns: set[int] = {
        r[0] for r in conn.execute("SELECT asn FROM asn_corpus").fetchall()
    }

    # Load USG IPv6 prefixes from pfx2as snapshot
    pfx_rows = conn.execute(
        """
        SELECT prefix, asn FROM caida_pfx2as
        WHERE af = 6 AND snapshot_date = ?
          AND asn IN (SELECT asn FROM asn_corpus)
        """,
        [pfx2as_date],
    ).fetchall()

    index = build_lpm_index([(r[0], r[1]) for r in pfx_rows])

    # Count pfx2as prefixes per corpus ASN (prefixes_in_scope)
    prefix_count: dict[int, int] = defaultdict(int)
    for _, asn in pfx_rows:
        prefix_count[asn] += 1

    # Download and parse
    filepath = download_nodes_as(target, dest_dir)
    actual_date = _date_from_filepath(filepath)

    # If lookback resolved a different date, guard against duplicate inserts
    if actual_date != target:
        dup = conn.execute(
            "SELECT COUNT(*) FROM ark_reachability WHERE measurement_date = ?",
            [actual_date],
        ).fetchone()[0]
        if dup > 0:
            conn.close()
            return {"date": actual_date, "addresses_parsed": 0, "asns_updated": 0,
                    "pfx2as_snapshot_date": pfx2as_date, "skipped": True}

    # Accumulate per-ASN and per-prefix
    per_asn: dict[int, dict] = {}
    per_prefix: dict[str, dict] = {}
    total_parsed = 0

    for addr, ark_asn in parse_nodes_as(filepath):
        total_parsed += 1
        entry = lpm_lookup_entry(addr, index)
        our_asn: Optional[int] = entry[0] if entry else None
        our_prefix: Optional[str] = entry[1] if entry else None

        in_scope = (our_asn is not None) or (ark_asn in corpus_asns)
        if not in_scope:
            continue

        key_asn = our_asn if our_asn is not None else ark_asn
        if key_asn not in per_asn:
            per_asn[key_asn] = {"observed": 0, "divergence": 0}
        per_asn[key_asn]["observed"] += 1
        if our_asn is not None and ark_asn is not None and our_asn != ark_asn:
            per_asn[key_asn]["divergence"] += 1

        if our_prefix is not None:
            if our_prefix not in per_prefix:
                per_prefix[our_prefix] = {"asn": our_asn, "observed": 0, "match": 0}
            per_prefix[our_prefix]["observed"] += 1
            if ark_asn is not None and ark_asn == our_asn:
                per_prefix[our_prefix]["match"] += 1

    # Write ark_reachability for all corpus ASNs that have IPv6 pfx2as prefixes
    for asn, n_prefixes in prefix_count.items():
        data = per_asn.get(asn, {"observed": 0, "divergence": 0})
        conn.execute(
            """
            INSERT INTO ark_reachability
                (measurement_date, asn, ipv6_reachable, addresses_observed,
                 prefixes_in_scope, pfx2as_snapshot_date, asn_divergence_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [actual_date, asn, data["observed"] > 0, data["observed"],
             n_prefixes, pfx2as_date, data["divergence"]],
        )

    # Write indeterminate rows for corpus ASNs with no IPv6 pfx2as coverage
    for asn in sorted(corpus_asns - set(prefix_count.keys())):
        conn.execute(
            """
            INSERT INTO ark_reachability
                (measurement_date, asn, ipv6_reachable, addresses_observed,
                 prefixes_in_scope, pfx2as_snapshot_date, asn_divergence_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [actual_date, asn, False, 0, 0, pfx2as_date, 0],
        )

    # Write ark_prefix_observations
    for prefix, pdata in per_prefix.items():
        conn.execute(
            """
            INSERT INTO ark_prefix_observations
                (measurement_date, prefix, our_asn, addresses_observed,
                 addresses_ark_match, pfx2as_snapshot_date)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [actual_date, prefix, pdata["asn"], pdata["observed"],
             pdata["match"], pfx2as_date],
        )

    conn.close()
    return {
        "date": actual_date,
        "addresses_parsed": total_parsed,
        "asns_updated": len(prefix_count),
        "pfx2as_snapshot_date": pfx2as_date,
        "skipped": False,
    }


def run_ark_backfill(
    db_path: str,
    since: datetime.datetime,
    until: datetime.datetime,
    *,
    dest_dir: str = "data/raw",
) -> dict:
    """Ingest all daily Ark files in [since, until]. Skips already-loaded dates."""
    processed = skipped = failed = 0
    current = since.date() if hasattr(since, "date") else since
    end = until.date() if hasattr(until, "date") else until

    while current <= end:
        date_str = current.isoformat()
        try:
            result = run_ark_ingest(db_path, date=date_str, dest_dir=dest_dir)
            if result["skipped"]:
                skipped += 1
            else:
                processed += 1
        except Exception as exc:
            print(f"  {date_str}: {exc}", flush=True)
            failed += 1
        current += datetime.timedelta(days=1)

    return {"processed": processed, "skipped": skipped, "failed": failed}
