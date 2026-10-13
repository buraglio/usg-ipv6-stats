from dataclasses import dataclass, field

TIER_PRIORITY = {"cfo_act": 0, "dod": 1, "federal_civilian": 2, "legislative": 3, "judicial": 4}


@dataclass
class AsnRecord:
    asn: int
    org_name: str
    agency: str
    tier: str
    arin_org_handle: str
    confidence: str      # "high" | "medium" | "low"
    sources: list[str]   # ["arin", "caida", "manual"]
    last_verified: str   # "YYYY-MM-DD"
    corpus_version: str  # "YYYY-MM-DD"
