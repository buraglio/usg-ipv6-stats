AGENCY_SEEDS = [
    # ── CFO Act agencies (24 cabinet / largest executive) ──────────────────
    {"name": "Department of Agriculture",                      "handles": ["USDA", "USDAAGSEC"],        "tier": "cfo_act"},
    {"name": "Department of Commerce",                         "handles": ["USDOC"],                    "tier": "cfo_act"},
    {"name": "Department of Defense",                          "handles": ["DOD-NIC", "DISA-HOSTMAST"], "tier": "dod"},
    {"name": "Department of Education",                        "handles": ["USED"],                     "tier": "cfo_act"},
    {"name": "Department of Energy",                           "handles": ["DOE-ISC"],                  "tier": "cfo_act"},
    {"name": "Department of Health and Human Services",        "handles": ["DHHS"],                     "tier": "cfo_act"},
    {"name": "Department of Homeland Security",                "handles": ["DHS-CIO"],                  "tier": "cfo_act"},
    {"name": "Department of Housing and Urban Development",    "handles": ["HUD-NOC"],                  "tier": "cfo_act"},
    {"name": "Department of the Interior",                     "handles": ["USDOI"],                    "tier": "cfo_act"},
    {"name": "Department of Justice",                          "handles": ["USDOJ"],                    "tier": "cfo_act"},
    {"name": "Department of Labor",                            "handles": ["USDOL"],                    "tier": "cfo_act"},
    {"name": "Department of State",                            "handles": ["USDOS-3"],                  "tier": "cfo_act"},
    {"name": "Department of Transportation",                   "handles": ["USDOT"],                    "tier": "cfo_act"},
    {"name": "Department of the Treasury",                     "handles": ["USDTREASURY"],              "tier": "cfo_act"},
    {"name": "Department of Veterans Affairs",                 "handles": ["VA-NOIT"],                  "tier": "cfo_act"},
    {"name": "Environmental Protection Agency",                "handles": ["EPA-CCSO"],                 "tier": "cfo_act"},
    {"name": "General Services Administration",                "handles": ["USGSA"],                    "tier": "cfo_act"},
    {"name": "National Aeronautics and Space Administration",  "handles": ["NASA-ARC-NOC"],             "tier": "cfo_act"},
    {"name": "National Science Foundation",                    "handles": ["NSF-NOC"],                  "tier": "cfo_act"},
    {"name": "Nuclear Regulatory Commission",                  "handles": ["NRC-OCIO"],                 "tier": "cfo_act"},
    {"name": "Office of Personnel Management",                 "handles": ["USAOPMS"],                  "tier": "cfo_act"},
    {"name": "Small Business Administration",                  "handles": ["SBA-OCIO"],                 "tier": "cfo_act"},
    {"name": "Social Security Administration",                 "handles": ["SSA-INTERNET"],             "tier": "cfo_act"},
    {"name": "U.S. Agency for International Development",      "handles": ["USAID-NOC"],                "tier": "cfo_act"},

    # ── DoD components ─────────────────────────────────────────────────────
    {"name": "Department of the Army",                         "handles": ["DA-131"],                   "tier": "dod"},
    {"name": "Department of the Air Force",                    "handles": ["USAF"],                     "tier": "dod"},

    # ── Independent / federal civilian agencies ────────────────────────────
    {"name": "Board of Governors of the Federal Reserve",      "handles": ["FEDER-67"],                 "tier": "federal_civilian"},
    {"name": "Commodity Futures Trading Commission",           "handles": ["CFTC-1"],                   "tier": "federal_civilian"},
    {"name": "Consumer Financial Protection Bureau",           "handles": ["CFPB-6"],                   "tier": "federal_civilian"},
    {"name": "Corporation for National and Community Service", "handles": ["CNCS"],                     "tier": "federal_civilian"},
    {"name": "Equal Employment Opportunity Commission",        "handles": ["EEOC"],                     "tier": "federal_civilian"},
    {"name": "Federal Communications Commission",              "handles": ["FCC"],                      "tier": "federal_civilian"},
    {"name": "Federal Deposit Insurance Corporation",          "handles": ["FDIC"],                     "tier": "federal_civilian"},
    {"name": "Federal Election Commission",                    "handles": ["FEC-105"],                  "tier": "federal_civilian"},
    {"name": "Federal Energy Regulatory Commission",           "handles": ["FERC"],                     "tier": "federal_civilian"},
    {"name": "Federal Trade Commission",                       "handles": ["FTC"],                      "tier": "federal_civilian"},
    {"name": "National Archives and Records Administration",   "handles": ["NARA"],                     "tier": "federal_civilian"},
    {"name": "National Credit Union Administration",           "handles": ["NCUA"],                     "tier": "federal_civilian"},
    {"name": "National Labor Relations Board",                 "handles": ["NLRB-4"],                   "tier": "federal_civilian"},
    {"name": "National Transportation Safety Board",           "handles": ["NTSB-1"],                   "tier": "federal_civilian"},
    {"name": "Peace Corps",                                    "handles": ["PC-1234"],                  "tier": "federal_civilian"},
    {"name": "Pension Benefit Guaranty Corporation",           "handles": ["PBGC"],                     "tier": "federal_civilian"},
    {"name": "Securities and Exchange Commission",             "handles": ["SEC-73"],                   "tier": "federal_civilian"},
    {"name": "Selective Service System",                       "handles": ["SSS-15"],                   "tier": "federal_civilian"},
    {"name": "Tennessee Valley Authority",                     "handles": ["TVA"],                      "tier": "federal_civilian"},
    {"name": "U.S. Agency for Global Media",                   "handles": ["USAGM", "VOICEO"],          "tier": "federal_civilian"},
    {"name": "U.S. International Trade Commission",            "handles": ["USITC-2"],                  "tier": "federal_civilian"},
    {"name": "U.S. Postal Service",                            "handles": ["USPS-3"],                   "tier": "federal_civilian"},

    # ── Legislative branch ─────────────────────────────────────────────────
    {"name": "Architect of the Capitol",                       "handles": ["ARCHIT"],                   "tier": "legislative"},
    {"name": "Congressional Budget Office",                    "handles": ["CBO"],                      "tier": "legislative"},
    {"name": "Government Accountability Office",               "handles": ["GAO-2"],                    "tier": "legislative"},
    {"name": "Government Publishing Office",                   "handles": ["GPO-10"],                   "tier": "legislative"},
    {"name": "Library of Congress",                            "handles": ["LCITS"],                    "tier": "legislative"},
    {"name": "U.S. Capitol Police",                            "handles": ["USCP-15"],                  "tier": "legislative"},
    {"name": "U.S. House of Representatives",                  "handles": ["ISUHR"],                    "tier": "legislative"},
    {"name": "U.S. Senate",                                    "handles": ["USSAA"],                    "tier": "legislative"},

    # ── Judicial branch ────────────────────────────────────────────────────
    {"name": "Administrative Office of the U.S. Courts",       "handles": ["AOUSC"],                   "tier": "judicial"},
]

# Backward-compatibility alias — existing code that imports CFO_ACT_AGENCIES still works.
CFO_ACT_AGENCIES = AGENCY_SEEDS

# Note: org handles above are starting points for ARIN WHOIS queries.
# The CAIDA AS2Org cross-reference in corpus/build.py expands coverage.
# Treat this list as a seed — handles must be verified against ARIN.
