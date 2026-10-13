import csv
import warnings
from datetime import date
from pathlib import Path

from rapidfuzz import fuzz

from usg_ipv6.corpus.arin import fetch_asns_for_org, arin_reachable
from usg_ipv6.corpus.caida import parse_as2org
from usg_ipv6.corpus.federal_agencies import AGENCY_SEEDS
from usg_ipv6.corpus.schema import AsnRecord, TIER_PRIORITY
from usg_ipv6.db import get_connection, init_schema

FUZZY_THRESHOLD = 85


def assign_tier(agency_name: str, candidate_tiers: list[str]) -> str:
    return min(candidate_tiers, key=lambda t: TIER_PRIORITY.get(t, 99))


def merge_sources(records: list[AsnRecord]) -> list[AsnRecord]:
    by_asn: dict[int, AsnRecord] = {}
    for r in records:
        if r.asn not in by_asn:
            by_asn[r.asn] = r
        else:
            existing = by_asn[r.asn]
            if TIER_PRIORITY.get(r.tier, 99) < TIER_PRIORITY.get(existing.tier, 99):
                by_asn[r.asn] = AsnRecord(
                    asn=r.asn,
                    org_name=existing.org_name,
                    agency=existing.agency,
                    tier=r.tier,
                    arin_org_handle=existing.arin_org_handle or r.arin_org_handle,
                    confidence=existing.confidence,
                    sources=list(set(existing.sources + r.sources)),
                    last_verified=existing.last_verified,
                    corpus_version=existing.corpus_version,
                )
            else:
                existing.sources = list(set(existing.sources + r.sources))
    return list(by_asn.values())


def _build_from_arin(version: str) -> list[AsnRecord]:
    # Pre-flight: one fast connectivity check so we don't grind through
    # 60+ org handles with full retry backoff if ARIN is blocking this host.
    if not arin_reachable():
        warnings.warn(
            "ARIN WHOIS is unreachable from this host — skipping ARIN queries. "
            "Corpus will be built from CAIDA data only. "
            "Run locally or use a network that can reach whois.arin.net.",
            UserWarning,
            stacklevel=2,
        )
        return []

    today = date.today().isoformat()
    records = []
    for agency in AGENCY_SEEDS:
        for handle in agency["handles"]:
            for asn in fetch_asns_for_org(handle):
                records.append(AsnRecord(
                    asn=asn,
                    org_name=agency["name"],
                    agency=agency["name"],
                    tier=agency["tier"],
                    arin_org_handle=handle,
                    confidence="high",
                    sources=["arin"],
                    last_verified=today,
                    corpus_version=version,
                ))
    return records


def _expand_from_caida(
    as2org_path: str, existing: list[AsnRecord], version: str
) -> list[AsnRecord]:
    as2org = parse_as2org(as2org_path)
    known_asns = {r.asn for r in existing}
    today = date.today().isoformat()
    agency_names = [a["name"] for a in AGENCY_SEEDS]
    new_records = []
    for asn, info in as2org.items():
        if asn in known_asns:
            continue
        org_name = info["org_name"]
        best_name, best_score = max(
            ((name, fuzz.token_sort_ratio(org_name, name)) for name in agency_names),
            key=lambda x: x[1],
        )
        if best_score >= FUZZY_THRESHOLD:
            agency = AGENCY_SEEDS[agency_names.index(best_name)]
            new_records.append(AsnRecord(
                asn=asn,
                org_name=org_name,
                agency=best_name,
                tier=agency["tier"],
                arin_org_handle="",
                confidence="medium",
                sources=["caida"],
                last_verified=today,
                corpus_version=version,
            ))
    return new_records


def build_corpus(
    db_path: str,
    as2org_path: str,
    *,
    version: str,
    csv_path: str = "data/usg_asn_corpus.csv",
) -> str:
    arin_records = _build_from_arin(version)
    caida_records = _expand_from_caida(as2org_path, arin_records, version)
    all_records = merge_sources(arin_records + caida_records)

    out = Path(csv_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "asn", "org_name", "agency", "tier", "arin_org_handle",
            "confidence", "sources", "last_verified", "corpus_version",
        ])
        writer.writeheader()
        for r in all_records:
            writer.writerow({
                "asn": r.asn,
                "org_name": r.org_name,
                "agency": r.agency,
                "tier": r.tier,
                "arin_org_handle": r.arin_org_handle,
                "confidence": r.confidence,
                "sources": ",".join(r.sources),
                "last_verified": r.last_verified,
                "corpus_version": r.corpus_version,
            })

    conn = get_connection(db_path)
    init_schema(conn)
    conn.execute("DELETE FROM asn_corpus")
    conn.executemany(
        "INSERT INTO asn_corpus VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r.asn, r.org_name, r.agency, r.tier, r.arin_org_handle,
          r.confidence, ",".join(r.sources), r.last_verified, r.corpus_version)
         for r in all_records],
    )
    return str(out)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/usg_ipv6.duckdb")
    parser.add_argument("--as2org", default=None)
    parser.add_argument("--version", required=True)
    parser.add_argument("--csv", default="data/usg_asn_corpus.csv")
    args = parser.parse_args()

    if args.as2org is None:
        from usg_ipv6.corpus.caida import download_as2org
        args.as2org = download_as2org("data/raw")

    csv_out = build_corpus(args.db, args.as2org, version=args.version, csv_path=args.csv)
    print(f"Corpus written to {csv_out}")
