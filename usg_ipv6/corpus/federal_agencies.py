CFO_ACT_AGENCIES = [
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
]

# Note: org handles above are starting points for ARIN WHOIS queries.
# The CAIDA AS2Org cross-reference in corpus/build.py expands coverage.
# Treat this list as a seed — handles must be verified against ARIN.
