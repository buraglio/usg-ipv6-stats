from dataclasses import dataclass, field

TIER_PRIORITY = {"cfo_act": 0, "dod": 1, "doe_lab": 2, "federal_civilian": 3, "legislative": 4, "judicial": 5}


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
