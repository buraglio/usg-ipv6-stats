"""
IPv6 Hitlist ingest pipeline.

Downloads the TU Munich IPv6 Hitlist responsive-addresses and
aliased-prefixes files, then longest-prefix-matches each responsive
address against IPv6 prefixes belonging to corpus ASNs (from caida_pfx2as).

A corpus ASN's IPv6 space is considered "in active use" when at least one
address within its routed prefix responds to ICMP/TCP/UDP probes from the
hitlist measurement.  This is a stronger signal than BGP reachability: it
requires a live host, not just a route.

Data source: https://ipv6hitlist.github.io/
Files are updated weekly at a fixed URL (no date in filename).
The actual data date is read from the HTTP Last-Modified response header.
"""

import datetime
import ipaddress
import lzma
from collections import defaultdict
from pathlib import Path
from typing import Optional

import requests

from usg_ipv6.db import get_connection, init_schema
from usg_ipv6.pipeline.lpm import LpmIndex, build_lpm_index, lpm_lookup_entry

_BASE_URL = "https://alcatraz.net.in.tum.de/ipv6-hitlist-service/open"
_RESPONSIVE_URL = f"{_BASE_URL}/responsive-addresses.txt.xz"
_ALIASED_URL = f"{_BASE_URL}/aliased-prefixes.txt.xz"


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _fetch(url: str, dest: Path, *, timeout: int = 300) -> str:
    """Stream-download url to dest. Returns the Last-Modified header value."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        last_mod = r.headers.get("Last-Modified", "")
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
    return last_mod


def _last_modified_to_date(last_mod: str) -> Optional[str]:
    """Parse HTTP Last-Modified header → ISO date string, or None on failure."""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(last_mod).date().isoformat()
    except Exception:
        return None


def download_hitlist(dest_dir: str, *, timeout: int = 300) -> dict:
    """
    Download responsive-addresses.txt.xz and aliased-prefixes.txt.xz.

    Returns a dict with keys:
      responsive_path  — local path to the responsive-addresses file
      aliased_path     — local path to the aliased-prefixes file
      hitlist_date     — ISO date of the data (from Last-Modified, or today)
    """
    d = Path(dest_dir)
    resp_lm = _fetch(_RESPONSIVE_URL, d / "hitlist-responsive.txt.xz", timeout=timeout)
    _fetch(_ALIASED_URL, d / "hitlist-aliased-prefixes.txt.xz", timeout=timeout)
    hitlist_date = _last_modified_to_date(resp_lm) or datetime.date.today().isoformat()
    return {
        "responsive_path": str(d / "hitlist-responsive.txt.xz"),
        "aliased_path": str(d / "hitlist-aliased-prefixes.txt.xz"),
        "hitlist_date": hitlist_date,
    }


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------

def _build_aliased_lpm(filepath: str) -> LpmIndex:
    """Build an LPM index from the aliased-prefixes file (IPv6 prefixes only)."""
    prefixes: list[tuple[str, int]] = []
    with lzma.open(filepath, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                net = ipaddress.ip_network(line, strict=False)
                if net.version == 6:
                    prefixes.append((str(net), 0))  # asn=0; only presence matters
            except ValueError:
                continue
    return build_lpm_index(prefixes) if prefixes else {}


def _scan_responsive(
    responsive_path: str,
    gov_lpm: LpmIndex,
    aliased_lpm: LpmIndex,
    observation_date: str,
    hitlist_date: str,
) -> tuple[dict[int, int], dict[int, int], list[tuple], int]:
    """
    Stream through responsive-addresses, match against government LPM index.

    Returns:
      responsive_count  — {asn: total matched addresses}
      aliased_count     — {asn: matched addresses that are also in aliased prefixes}
      address_rows      — rows for hitlist_addresses table
      total_scanned     — total non-blank, non-comment lines processed
    """
    responsive_count: dict[int, int] = defaultdict(int)
    aliased_count: dict[int, int] = defaultdict(int)
    address_rows: list[tuple] = []
    total_scanned = 0

    with lzma.open(responsive_path, "rt") as fh:
        for line in fh:
            addr_str = line.strip()
            if not addr_str or addr_str.startswith("#"):
                continue
            total_scanned += 1
            try:
                addr = ipaddress.IPv6Address(addr_str)
            except ValueError:
                continue

            entry = lpm_lookup_entry(addr, gov_lpm)
            if entry is None:
                continue

            asn, matched_prefix = entry
            is_aliased = bool(aliased_lpm and lpm_lookup_entry(addr, aliased_lpm) is not None)
            responsive_count[asn] += 1
            if is_aliased:
                aliased_count[asn] += 1
            address_rows.append(
                (observation_date, addr_str, asn, matched_prefix, is_aliased, hitlist_date)
            )

    return responsive_count, aliased_count, address_rows, total_scanned


# ---------------------------------------------------------------------------
# Public pipeline functions
# ---------------------------------------------------------------------------

def run_hitlist_ingest(
    db_path: str,
    *,
    date: Optional[str] = None,
    dest_dir: str = "data/raw",
    timeout: int = 300,
    _responsive_file: Optional[str] = None,
    _aliased_file: Optional[str] = None,
    _hitlist_date: Optional[str] = None,
) -> dict:
    """
    Download the IPv6 Hitlist and match responsive addresses against corpus
    IPv6 prefixes from caida_pfx2as (af=6).

    Requires at least one IPv6 pfx2as snapshot with af=6 in the DB.  If
    none is present, raises RuntimeError — run RouteViews ingest with
    collector=route-views6 first.

    Parameters prefixed with ``_`` are for testing: pass pre-built local
    file paths to skip network downloads in unit tests.
    """
    conn = get_connection(db_path)
    init_schema(conn)

    observation_date = date or datetime.date.today().isoformat()

    existing = conn.execute(
        "SELECT COUNT(*) FROM hitlist_observations WHERE observation_date = ?",
        [observation_date],
    ).fetchone()[0]
    if existing > 0:
        conn.close()
        return {
            "observation_date": observation_date,
            "hitlist_date": None,
            "total_scanned": 0,
            "matched_addresses": 0,
            "asns_with_responses": 0,
            "skipped": True,
        }

    # Require IPv6 pfx2as data for corpus ASNs
    pfx2as_date_row = conn.execute(
        "SELECT MAX(snapshot_date) FROM caida_pfx2as WHERE af = 6"
    ).fetchone()
    if pfx2as_date_row is None or pfx2as_date_row[0] is None:
        conn.close()
        raise RuntimeError(
            "No IPv6 pfx2as data — run RouteViews ingest with collector=route-views6 first"
        )
    pfx2as_date = str(pfx2as_date_row[0])

    corpus_asns: set[int] = {
        r[0] for r in conn.execute("SELECT asn FROM asn_corpus").fetchall()
    }
    if not corpus_asns:
        conn.close()
        raise RuntimeError("asn_corpus is empty — run corpus update first")

    pfx_rows = conn.execute(
        """
        SELECT DISTINCT prefix, asn FROM caida_pfx2as
        WHERE af = 6 AND snapshot_date = ?
          AND asn IN (SELECT asn FROM asn_corpus)
        """,
        [pfx2as_date],
    ).fetchall()
    if not pfx_rows:
        conn.close()
        raise RuntimeError(
            "No IPv6 corpus prefixes in caida_pfx2as — check route-views6 data"
        )

    gov_lpm = build_lpm_index([(row[0], row[1]) for row in pfx_rows])

    if _responsive_file and _aliased_file:
        responsive_path = _responsive_file
        aliased_path = _aliased_file
        hitlist_date = _hitlist_date or datetime.date.today().isoformat()
    else:
        print("Downloading IPv6 Hitlist responsive addresses …", flush=True)
        dl = download_hitlist(dest_dir, timeout=timeout)
        responsive_path = dl["responsive_path"]
        aliased_path = dl["aliased_path"]
        hitlist_date = dl["hitlist_date"]
        print(f"Hitlist data date: {hitlist_date}", flush=True)

    print("Loading aliased-prefixes index …", flush=True)
    aliased_lpm = _build_aliased_lpm(aliased_path)

    print("Scanning hitlist for government IPv6 addresses …", flush=True)
    responsive_count, aliased_count, address_rows, total_scanned = _scan_responsive(
        responsive_path, gov_lpm, aliased_lpm, observation_date, hitlist_date
    )
    print(
        f"Scanned {total_scanned:,} addresses; "
        f"found {len(address_rows):,} government matches across "
        f"{sum(1 for c in responsive_count.values() if c > 0)} ASNs",
        flush=True,
    )

    for asn in sorted(corpus_asns):
        conn.execute(
            """
            INSERT INTO hitlist_observations
                (observation_date, asn, responsive_count, aliased_count, hitlist_date)
            VALUES (?, ?, ?, ?, ?)
            """,
            [observation_date, asn,
             responsive_count.get(asn, 0),
             aliased_count.get(asn, 0),
             hitlist_date],
        )

    if address_rows:
        conn.executemany(
            """
            INSERT INTO hitlist_addresses
                (observation_date, address, asn, matched_prefix, is_aliased, hitlist_date)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            address_rows,
        )

    conn.close()
    return {
        "observation_date": observation_date,
        "hitlist_date": hitlist_date,
        "total_scanned": total_scanned,
        "matched_addresses": len(address_rows),
        "asns_with_responses": sum(1 for c in responsive_count.values() if c > 0),
        "skipped": False,
    }


def run_hitlist_backfill(
    db_path: str,
    since: datetime.datetime,
    until: datetime.datetime,
    *,
    dest_dir: str = "data/raw",
    timeout: int = 300,
) -> dict:
    """
    Run hitlist ingest for each day in [since, until], skipping loaded dates.

    The open hitlist tier publishes only the current week's snapshot (no
    historical archive).  Backfill stamps the current file for each requested
    date — useful for initial population or after corpus changes.
    """
    processed = skipped = failed = 0
    current = since.date() if hasattr(since, "date") else since
    end = until.date() if hasattr(until, "date") else until

    # Download once; reuse across all dates
    print("Downloading hitlist for backfill …", flush=True)
    dl = download_hitlist(dest_dir, timeout=timeout)

    while current <= end:
        date_str = current.isoformat()
        try:
            result = run_hitlist_ingest(
                db_path,
                date=date_str,
                dest_dir=dest_dir,
                _responsive_file=dl["responsive_path"],
                _aliased_file=dl["aliased_path"],
                _hitlist_date=dl["hitlist_date"],
            )
            if result["skipped"]:
                skipped += 1
            else:
                processed += 1
        except Exception as exc:
            print(f"  {date_str}: {exc}", flush=True)
            failed += 1
        current += datetime.timedelta(days=1)

    return {"processed": processed, "skipped": skipped, "failed": failed}
