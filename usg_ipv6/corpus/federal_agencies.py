AGENCY_SEEDS = [
    # ── CFO Act agencies (24 cabinet / largest executive) ──────────────────
    {"name": "Department of Agriculture",                      "handles": ["USDA", "USDAAGSEC"],        "tier": "cfo_act"},
    {"name": "Department of Commerce",                         "handles": ["USDOC"],                    "tier": "cfo_act"},
    {"name": "Department of Defense",                          "handles": ["USDDD", "DNIC"],            "tier": "dod"},
    {"name": "Department of Education",                        "handles": ["USED"],                     "tier": "cfo_act"},
    {"name": "Department of Energy",                           "handles": ["DOE-ISC"],                  "tier": "cfo_act"},
    {"name": "Department of Health and Human Services",        "handles": ["DHHS"],                     "tier": "cfo_act"},
    {"name": "Department of Homeland Security",                "handles": ["DHS-CIO", "DHS-37", "DHS-39"], "tier": "cfo_act"},
    {"name": "Department of Housing and Urban Development",    "handles": ["HUD-NOC"],                  "tier": "cfo_act"},
    {"name": "Department of the Interior",                     "handles": ["USDOI"],                    "tier": "cfo_act"},
    {"name": "Department of Justice",                          "handles": ["USDOJ", "DEPAR-9", "DEPART-31", "FCJIS"], "tier": "cfo_act"},
    {"name": "Department of Labor",                            "handles": ["USDOL"],                    "tier": "cfo_act"},
    {"name": "Department of State",                            "handles": ["USDOS-3"],                  "tier": "cfo_act"},
    {"name": "Department of Transportation",                   "handles": ["USDOT"],                    "tier": "cfo_act"},
    {"name": "Department of the Treasury",                     "handles": ["USDTREASURY"],              "tier": "cfo_act"},
    {"name": "Department of Veterans Affairs",                 "handles": ["VA-NOIT"],                  "tier": "cfo_act"},
    {"name": "Environmental Protection Agency",                "handles": ["EPA-CCSO"],                 "tier": "cfo_act"},
    {"name": "General Services Administration",                "handles": ["USGSA"],                    "tier": "cfo_act"},
    {"name": "National Aeronautics and Space Administration",  "handles": ["NASA-ARC-NOC", "NASA", "NDSND", "NGSFC"], "tier": "cfo_act"},
    {"name": "National Science Foundation",                    "handles": ["NSF-NOC", "NSF", "NSF-15"], "tier": "cfo_act"},
    {"name": "Nuclear Regulatory Commission",                  "handles": ["NRC-OCIO"],                 "tier": "cfo_act"},
    {"name": "Office of Personnel Management",                 "handles": ["USAOPMS"],                  "tier": "cfo_act"},
    {"name": "Small Business Administration",                  "handles": ["SBA-OCIO"],                 "tier": "cfo_act"},
    {"name": "Social Security Administration",                 "handles": ["SSA-INTERNET"],             "tier": "cfo_act"},
    {"name": "U.S. Agency for International Development",      "handles": ["USAID-NOC"],                "tier": "cfo_act"},

    # ── DoD components ─────────────────────────────────────────────────────
    # DOD-NIC and DISA-HOSTMAST are deprecated (RDAP 404); replaced by USDDD/DNIC and DISA-24.
    {"name": "Defense Advanced Research Projects Agency",      "handles": ["DARPA-2"],                  "tier": "dod"},
    {"name": "Defense Information Systems Agency",             "handles": ["DISA-24"],                  "tier": "dod"},
    {"name": "Department of the Air Force",                    "handles": ["USAF", "7ESG", "AFSC-43"],  "tier": "dod"},
    {"name": "Department of the Army",                         "handles": ["DA-131"],                   "tier": "dod"},
    {"name": "Department of the Navy",                         "handles": ["DNH-5", "DNH-6", "NIWCA"], "tier": "dod"},
    {"name": "National Geospatial-Intelligence Agency",        "handles": ["NGA-30", "NGA-24"],         "tier": "dod"},
    {"name": "US Marine Corps",                                "handles": ["UNOC"],                     "tier": "dod"},
    {"name": "US Special Operations Command",                  "handles": ["SOC-22"],                   "tier": "dod"},

    # ── DOE national laboratories and ESnet ───────────────────────────────
    # Handles confirmed via ARIN RDAP. Several are non-obvious (THELE-44=SLAC,
    # CEBAFS=JLab, IRDSD-1=NETL, SRP-1=Savannah River Site). ESNET collides
    # with a commercial ISP — the correct DOE ESnet handle is ENSN.
    {"name": "Ames National Laboratory",                       "handles": ["AMESLA"],                   "tier": "doe_lab"},
    {"name": "Argonne National Laboratory",                    "handles": ["ANLB", "ANL-13"],           "tier": "doe_lab"},
    {"name": "Brookhaven National Laboratory",                 "handles": ["BNL"],                      "tier": "doe_lab"},
    {"name": "Fermi National Accelerator Laboratory",          "handles": ["FERMIL"],                   "tier": "doe_lab"},
    {"name": "Idaho National Laboratory",                      "handles": ["INL-12"],                   "tier": "doe_lab"},
    {"name": "Lawrence Berkeley National Laboratory",          "handles": ["LBNL"],                     "tier": "doe_lab"},
    {"name": "Lawrence Livermore National Laboratory",         "handles": ["LLNL-1"],                   "tier": "doe_lab"},
    {"name": "Los Alamos National Laboratory",                 "handles": ["LANL-2"],                   "tier": "doe_lab"},
    {"name": "National Energy Technology Laboratory",          "handles": ["IRDSD-1"],                  "tier": "doe_lab"},
    {"name": "National Renewable Energy Laboratory",           "handles": ["NREL"],                     "tier": "doe_lab"},
    {"name": "Oak Ridge National Laboratory",                  "handles": ["ORNL"],                     "tier": "doe_lab"},
    {"name": "Pacific Northwest National Laboratory",          "handles": ["PNNL", "BMI-1"],            "tier": "doe_lab"},
    {"name": "Princeton Plasma Physics Laboratory",            "handles": ["PPPL"],                     "tier": "doe_lab"},
    {"name": "Sandia National Laboratories",                   "handles": ["SNL", "SNL-3"],             "tier": "doe_lab"},
    {"name": "Savannah River National Laboratory",             "handles": ["SRP-1"],                    "tier": "doe_lab"},
    {"name": "SLAC National Accelerator Laboratory",           "handles": ["THELE-44"],                 "tier": "doe_lab"},
    {"name": "Thomas Jefferson National Accelerator Facility", "handles": ["CEBAFS"],                   "tier": "doe_lab"},
    {"name": "Energy Sciences Network (ESnet)",                "handles": ["ENSN"],                     "tier": "doe_lab"},

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
