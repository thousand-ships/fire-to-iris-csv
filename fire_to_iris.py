#!/usr/bin/env python3
"""fire_to_iris.py - convert an IRS FIRE (Pub 1220) .txt file into IRIS Taxpayer Portal CSV files.

1099-NEC and 1099-MISC only. Standard library only. Makes no network calls: the file is read from
disk and the CSVs are written to disk, so TINs never leave this machine.

NOT TAX ADVICE. The output is a draft for you to check, not a filing. See DISCLAIMER below.

Spec sources (vendored in spec/, cited as "p1220 p.N", "NEC col X", "MISC col X"):
  - spec/p1220.pdf(.txt): IRS Publication 1220, Rev. 5-2026, for Tax Year 2025 (FIRE record layouts).
  - spec/iris-csv-1099-NEC-guidelines-TY2025.pdf(.txt): IRIS CSV columns A-BB for 1099-NEC, TY2025.
  - spec/iris-csv-1099-MISC-guidelines-TY2025.pdf(.txt): IRIS CSV columns A-BL for 1099-MISC, TY2025.
  - Records per CSV: 250, Pub 5717 Rev. 9-2026 p.20 (quoted in spec/README.md). Parameter --batch-size.
Where the spec does not say how a FIRE field maps to IRIS, the converter does not guess: it stops on
that field (or lists it as unmapped) and the gap is logged in SPEC-GAP.md.

Everything that depends on the tax year lives in the TABLES dict, keyed by tax year, so adding TY2026
is a data change: add a FIRE layout and IRIS templates for 2026 when the IRS publishes them.
"""

import argparse
import csv
import io
import os
import re
import sys
from decimal import Decimal

VERSION = "0.1.0"

DISCLAIMER = (
    "DISCLAIMER: This is not tax advice and not an IRS product. Not affiliated with or endorsed by the IRS. "
    "The CSVs are a draft converted from your "
    "FIRE file. You are responsible for what you file. Check every row in the IRIS Taxpayer Portal before "
    "you submit; the portal validates the CSV on upload and may reject rows. Provided as-is under the MIT "
    "licence, with no warranty."
)
TCC_WARNING = (
    "TCC WARNING: Uploading CSVs to the IRIS Taxpayer Portal needs your own IRIS Transmitter Control Code "
    "(TCC). Your FIRE TCC does not work in IRIS. A new IRIS TCC can take up to 45 days to be approved. If you "
    "do not have an IRIS TCC, these CSVs cannot be filed by you; use a provider that files under its own TCC, "
    "or apply for an IRIS TCC now (IRS Publication 5903)."
)
PRIVACY_NOTE = "PRIVACY: runs locally, makes no network calls; TINs are masked in the report."

RECORD_LEN = 750  # p1220 p.47 "Each record must be 750 positions."

# --------------------------------------------------------------------------------------------------
# Spec tables. One entry per tax year.
# --------------------------------------------------------------------------------------------------
# FIRE field dispositions:
#   "map"       used to build an IRIS column (see MAPPING below)
#   "control"   record type, counts, sequence numbers, payment year: checked, not output
#   "blank"     Pub 1220 says "Enter blanks"; any data there is listed as unexpected
#   "fire_only" FIRE-only data with no IRIS column; listed in the report when it holds data
#   "gap"       has an IRIS-looking home but the spec does not say how to map it; listed as unmapped

def F(start, end, name, disp, cite):
    return {"start": start, "end": end, "name": name, "disp": disp, "cite": cite}

AMOUNT_CODES = "123456789ABCDEFGHJ"  # p1220 p.77-78: Payment Amount 1-9, A-H, J (no I)


def _amount_fields():
    # p1220 p.77-78: Payment Amount 1 at 55-66 ... Payment Amount J at 259-270, 12 positions each.
    out = []
    for i, code in enumerate(AMOUNT_CODES):
        s = 55 + 12 * i
        out.append(F(s, s + 11, "Payment Amount " + code, "map", "p1220 p.77-78"))
    return out


def _control_total_fields():
    # p1220 p.139 (C) and p.141 (K): Control Total 1 at 16-33 ... Control Total J at 322-339, 18 each.
    out = []
    for i, code in enumerate(AMOUNT_CODES):
        s = 16 + 18 * i
        out.append(F(s, s + 17, "Control Total " + code, "control", "p1220 p.139/p.141"))
    return out


FIRE_P1220_REV_5_2026 = {
    "edition": "Pub 1220 Rev. 5-2026 (Tax Year 2025)",
    "T": [  # p1220 p.48-52
        F(1, 1, "Record Type", "control", "p1220 p.48"),
        F(2, 5, "Payment Year", "control", "p1220 p.48"),
        F(6, 6, "Prior Year Data Indicator", "control", "p1220 p.48"),
        F(7, 15, "Transmitter's TIN", "fire_only", "p1220 p.48"),
        F(16, 20, "Transmitter Control Code", "fire_only", "p1220 p.48"),
        F(21, 27, "Blank", "blank", "p1220 p.48"),
        F(28, 28, "Test File Indicator", "fire_only", "p1220 p.48"),
        F(29, 29, "Foreign Entity Indicator", "fire_only", "p1220 p.49"),
        F(30, 69, "Transmitter Name", "fire_only", "p1220 p.49"),
        F(70, 109, "Transmitter Name (Continuation)", "fire_only", "p1220 p.49"),
        F(110, 149, "Company Name", "fire_only", "p1220 p.49"),
        F(150, 189, "Company Name (Continuation)", "fire_only", "p1220 p.49"),
        F(190, 229, "Company Mailing Address", "fire_only", "p1220 p.49"),
        F(230, 269, "Company City", "fire_only", "p1220 p.49"),
        F(270, 271, "Company State", "fire_only", "p1220 p.49"),
        F(272, 280, "Company ZIP Code", "fire_only", "p1220 p.49"),
        F(281, 295, "Blank", "blank", "p1220 p.49"),
        F(296, 303, "Total Number of Payees", "control", "p1220 p.50"),
        F(304, 343, "Contact Name", "fire_only", "p1220 p.50"),
        F(344, 358, "Contact Telephone Number & Extension", "fire_only", "p1220 p.50"),
        F(359, 408, "Contact Email Address", "fire_only", "p1220 p.50"),
        F(409, 499, "Blank", "blank", "p1220 p.50"),
        F(500, 507, "Record Sequence Number", "control", "p1220 p.50"),
        F(508, 517, "Blank", "blank", "p1220 p.50"),
        F(518, 518, "Vendor Indicator", "fire_only", "p1220 p.51"),
        F(519, 558, "Vendor Name", "fire_only", "p1220 p.51"),
        F(559, 598, "Vendor Mailing Address", "fire_only", "p1220 p.51"),
        F(599, 638, "Vendor City", "fire_only", "p1220 p.51"),
        F(639, 640, "Vendor State", "fire_only", "p1220 p.51"),
        F(641, 649, "Vendor ZIP Code", "fire_only", "p1220 p.52"),
        F(650, 689, "Vendor Contact Name", "fire_only", "p1220 p.52"),
        F(690, 704, "Vendor Contact Telephone Number & Extension", "fire_only", "p1220 p.52"),
        F(705, 739, "Blank", "blank", "p1220 p.52"),
        F(740, 740, "Vendor Foreign Entity Indicator", "fire_only", "p1220 p.52"),
        F(741, 748, "Blank", "blank", "p1220 p.52"),
    ],
    "A": [  # p1220 p.55-72
        F(1, 1, "Record Type", "control", "p1220 p.55"),
        F(2, 5, "Payment Year", "control", "p1220 p.55"),
        F(6, 6, "Combined Federal/State Filing Program", "map", "p1220 p.55"),
        F(7, 11, "Blank", "blank", "p1220 p.55"),
        F(12, 20, "Issuer TIN", "map", "p1220 p.55"),
        F(21, 24, "Issuer Name Control", "fire_only", "p1220 p.55"),
        F(25, 25, "Last Filing Indicator", "fire_only", "p1220 p.55"),
        F(26, 27, "Type of Return", "control", "p1220 p.56"),
        F(28, 45, "Amount Codes", "control", "p1220 p.57"),
        F(46, 51, "Blank", "blank", "p1220 p.70"),
        F(52, 52, "Foreign Entity Indicator", "map", "p1220 p.70"),
        F(53, 92, "First Issuer Name Line", "map", "p1220 p.71"),
        F(93, 132, "Second Issuer Name Line", "map", "p1220 p.71"),
        F(133, 133, "Transfer Agent Indicator", "map", "p1220 p.71"),
        F(134, 173, "Issuer Shipping Address", "map", "p1220 p.71"),
        F(174, 213, "Issuer City", "map", "p1220 p.72"),
        F(214, 215, "Issuer State", "map", "p1220 p.72"),
        F(216, 224, "Issuer ZIP Code", "map", "p1220 p.72"),
        F(225, 239, "Issuer Telephone Number & Extension", "map", "p1220 p.72"),
        F(240, 499, "Blank", "blank", "p1220 p.72"),
        F(500, 507, "Record Sequence Number", "control", "p1220 p.72"),
        F(508, 748, "Blank", "blank", "p1220 p.72"),
    ],
    "B_common": [  # p1220 p.74-80; positions 1-543 are the same for all returns (p.73)
        F(1, 1, "Record Type", "control", "p1220 p.74"),
        F(2, 5, "Payment Year", "control", "p1220 p.74"),
        F(6, 6, "Corrected Return Indicator", "control", "p1220 p.74"),
        F(7, 10, "Name Control", "fire_only", "p1220 p.74"),
        F(11, 11, "Type of TIN", "map", "p1220 p.75"),
        F(12, 20, "Payee's TIN", "map", "p1220 p.75"),
        F(21, 40, "Issuer's Account Number for Payee", "map", "p1220 p.76"),
        F(41, 44, "Issuer's Office Code", "map", "p1220 p.76"),
        F(45, 54, "Blank", "blank", "p1220 p.76"),
    ] + _amount_fields() + [
        F(271, 286, "Blank", "blank", "p1220 p.78"),
        F(287, 287, "Foreign Country Indicator", "map", "p1220 p.78"),
        F(288, 327, "First Payee Name Line", "map", "p1220 p.79"),
        F(328, 367, "Second Payee Name Line", "map", "p1220 p.79"),
        F(368, 407, "Payee Mailing Address", "map", "p1220 p.79"),
        F(408, 447, "Blank", "blank", "p1220 p.79"),
        F(448, 487, "Payee City", "map", "p1220 p.79"),
        F(488, 489, "Payee State", "map", "p1220 p.80"),
        F(490, 498, "Payee ZIP Code", "map", "p1220 p.80"),
        F(499, 499, "Blank", "blank", "p1220 p.80"),
        F(500, 507, "Record Sequence Number", "control", "p1220 p.80"),
        F(508, 543, "Blank", "blank", "p1220 p.80"),
    ],
    "B_NE": [  # 1099-NEC positions 544-750, p1220 p.113-114
        F(544, 544, "Second TIN Notice", "map", "p1220 p.113"),
        F(545, 546, "Blank", "blank", "p1220 p.113"),
        F(547, 547, "Direct Sales Indicator", "map", "p1220 p.113"),
        F(548, 662, "Blank", "blank", "p1220 p.113"),
        F(663, 722, "Special Data Entries", "gap", "p1220 p.113; SPEC-GAP G3"),
        F(723, 734, "State Income Tax Withheld", "map", "p1220 p.113"),
        F(735, 746, "Local Income Tax Withheld", "map", "p1220 p.114"),
        F(747, 748, "Combined Federal/State Code", "map", "p1220 p.114"),
    ],
    "B_A": [  # 1099-MISC positions 544-750, p1220 p.112-113
        F(544, 544, "Second TIN Notice", "map", "p1220 p.112"),
        F(545, 546, "Blank", "blank", "p1220 p.112"),
        F(547, 547, "Direct Sales Indicator", "map", "p1220 p.112"),
        F(548, 548, "FATCA Filing Requirement Indicator", "map", "p1220 p.112"),
        F(549, 662, "Blank", "blank", "p1220 p.112"),
        F(663, 722, "Special Data Entries", "gap", "p1220 p.112; SPEC-GAP G3"),
        F(723, 734, "State Income Tax Withheld", "map", "p1220 p.112"),
        F(735, 746, "Local Income Tax Withheld", "map", "p1220 p.112"),
        F(747, 748, "Combined Federal/State Code", "map", "p1220 p.112"),
    ],
    "C": [  # p1220 p.139-140
        F(1, 1, "Record Type", "control", "p1220 p.139"),
        F(2, 9, "Number of Payees", "control", "p1220 p.139"),
        F(10, 15, "Blank", "blank", "p1220 p.139"),
    ] + _control_total_fields() + [
        F(340, 499, "Blank", "blank", "p1220 p.140"),
        F(500, 507, "Record Sequence Number", "control", "p1220 p.140"),
        F(508, 748, "Blank", "blank", "p1220 p.140"),
    ],
    "K": [  # p1220 p.141-142
        F(1, 1, "Record Type", "control", "p1220 p.141"),
        F(2, 9, "Number of Payees", "control", "p1220 p.141"),
        F(10, 15, "Blank", "blank", "p1220 p.141"),
    ] + _control_total_fields() + [
        F(340, 499, "Blank", "blank", "p1220 p.141"),
        F(500, 507, "Record Sequence Number", "control", "p1220 p.142"),
        F(508, 706, "Blank", "blank", "p1220 p.142"),
        F(707, 724, "State Income Tax Withheld Total", "control", "p1220 p.142"),
        F(725, 742, "Local Income Tax Withheld Total", "control", "p1220 p.142"),
        F(743, 746, "Blank", "blank", "p1220 p.142"),
        F(747, 748, "Combined Federal/State Code", "control", "p1220 p.142"),
    ],
    "F": [  # p1220 p.144
        F(1, 1, "Record Type", "control", "p1220 p.144"),
        F(2, 9, "Number of A Records", "control", "p1220 p.144"),
        F(10, 30, "Zero", "control", "p1220 p.144"),
        F(31, 49, "Blank", "blank", "p1220 p.144"),
        F(50, 57, "Total Number of Payees", "control", "p1220 p.144"),
        F(58, 499, "Blank", "blank", "p1220 p.144"),
        F(500, 507, "Record Sequence Number", "control", "p1220 p.144"),
        F(508, 748, "Blank", "blank", "p1220 p.144"),
    ],
}

# p1220 p.25, Table 1 (CF/SF code by state name) joined on state name with p.26, Table 2 (abbreviation).
CFSF_STATE_CODES = {
    "01": "AL", "04": "AZ", "05": "AR", "06": "CA", "07": "CO", "08": "CT", "10": "DE", "11": "DC",
    "13": "GA", "15": "HI", "16": "ID", "18": "IN", "20": "KS", "22": "LA", "23": "ME", "24": "MD",
    "25": "MA", "26": "MI", "27": "MN", "28": "MS", "30": "MT", "31": "NE", "34": "NJ", "35": "NM",
    "37": "NC", "38": "ND", "39": "OH", "40": "OK", "42": "PA", "44": "RI", "45": "SC", "55": "WI",
}

# IRIS CSV columns (letter, field title as printed, without the "*" that marks required fields).
IRIS_COMMON_PAYER_RECIPIENT = [  # NEC and MISC cols A-AJ are identical (NEC p.1-4, MISC p.1-4)
    ("A", "Form Type"), ("B", "Tax Year"), ("C", "Payer TIN Type"), ("D", "Payer Taxpayer ID Number"),
    ("E", "Payer Name Type"), ("F", "Payer Business or Entity Name Line 1"),
    ("G", "Payer Business or Entity Name Line 2"), ("H", "Payer First Name"), ("I", "Payer Middle Name"),
    ("J", "Payer Last Name (Surname)"), ("K", "Payer Suffix"), ("L", "Payer Country"),
    ("M", "Payer Address Line 1"), ("N", "Payer Address Line 2"), ("O", "Payer City/Town"),
    ("P", "Payer State/Province/Territory"), ("Q", "Payer ZIP/Postal Code"), ("R", "Payer Phone Type"),
    ("S", "Payer Phone"), ("T", "Recipient TIN Type"), ("U", "Recipient Taxpayer ID Number"),
    ("V", "Recipient Name Type"), ("W", "Recipient Business or Entity Name Line 1"),
    ("X", "Recipient Business or Entity Name Line 2"), ("Y", "Recipient First Name"),
    ("Z", "Recipient Middle Name"), ("AA", "Recipient Last Name (Surname)"), ("AB", "Recipient Suffix"),
    ("AC", "Recipient Country"), ("AD", "Recipient Address Line 1"), ("AE", "Recipient Address Line 2"),
    ("AF", "Recipient City/Town"), ("AG", "Recipient State/Province/Territory"),
    ("AH", "Recipient ZIP/Postal Code"), ("AI", "Office Code"), ("AJ", "Form Account Number"),
]


def _state_cols(first):
    letters = []
    n = col_index(first)
    for i in range(12):
        letters.append(col_letter(n + i))
    titles = ["State 1", "State 1 - State Tax Withheld", "State 1 - State/Payer state number",
              "State 1 - State income", "State 1 - Local income tax withheld", "State 1 - Special Data Entries",
              "State 2", "State 2 - State Tax Withheld", "State 2 - State/Payer state number",
              "State 2 - State income", "State 2 - Local income tax withheld", "State 2 - Special Data Entries"]
    return list(zip(letters, titles))


def col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def col_letter(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


IRIS_NEC_TY2025 = IRIS_COMMON_PAYER_RECIPIENT + [  # NEC guidelines TY2025 p.4-6
    ("AK", "2nd TIN Notice"),
    ("AL", "Box 1 - Nonemployee Compensation"),
    ("AM", "Box 2 - Payer made direct sales totaling $5,000 or more of consumer products to a recipient for resale"),
    ("AN", "Box 3 - Excess golden parachute payments"),
    ("AO", "Box 4 - Federal income tax withheld"),
    ("AP", "Combined Federal/State Filing (CF/SF)"),
] + _state_cols("AQ")  # AQ-BB

IRIS_MISC_TY2025 = IRIS_COMMON_PAYER_RECIPIENT + [  # MISC guidelines TY2025 p.4-7
    ("AK", "FATCA Filing Requirements"),
    ("AL", "2nd TIN Notice"),
    ("AM", "Box 1 - Rents"),
    ("AN", "Box 2 - Royalties"),
    ("AO", "Box 3 - Other income"),
    ("AP", "Box 4 - Federal income tax withheld"),
    ("AQ", "Box 5 - Fishing boat proceeds"),
    ("AR", "Box 6 - Medical and health care payments"),
    ("AS", "Box 7 - Direct sales of $5,000 or more of consumer products to a recipient for resale"),
    ("AT", "Box 8 - Substitute payments in lieu of dividends or interest"),
    ("AU", "Box 9 - Crop insurance proceeds"),
    ("AV", "Box 10 - Gross proceeds paid to an attorney"),
    ("AW", "Box 11 - Fish purchased for resale"),
    ("AX", "Box 12 - Section 409A deferrals"),
    ("AY", "Box 15 - Nonqualified deferred compensation"),
    ("AZ", "Combined Federal/State Filing (CF/SF)"),
] + _state_cols("BA")  # BA-BL

# Per form: FIRE amount code -> IRIS column, plus the indicator and state columns.
# Amount codes: p1220 p.65. Each pair below matches the IRIS column title (guideline page cited).
FORMS_TY2025 = {
    "NE": {
        "form": "1099-NEC",
        "columns": IRIS_NEC_TY2025,
        "amounts": {
            "1": ("AL", "p1220 p.65 'Nonemployee Compensation' = NEC col AL p.4"),
            "4": ("AO", "p1220 p.65 'Federal Income Tax Withheld' = NEC col AO p.4"),
        },
        # FIRE codes valid for the form but with no IRIS column, or where the mapping is a spec gap.
        "amount_gaps": {},
        "second_tin": "AK",      # p1220 p.113 pos 544 '2' -> NEC col AK p.4 Y/N
        "direct_sales": "AM",    # p1220 p.113 pos 547 '1' -> NEC col AM p.4 Y/N
        "fatca": None,
        "cfsf": "AP", "state1": "AQ", "state1_tax": "AR", "state1_local": "AU",  # NEC p.4-5
    },
    "A": {
        "form": "1099-MISC",
        "columns": IRIS_MISC_TY2025,
        "amounts": {
            "1": ("AM", "p1220 p.65 'Rents' = MISC col AM p.4"),
            "2": ("AN", "p1220 p.65 'Royalties' = MISC col AN p.4"),
            "3": ("AO", "p1220 p.65 'Other income' = MISC col AO p.4"),
            "4": ("AP", "p1220 p.65 'Federal income tax withheld' = MISC col AP p.4"),
            "5": ("AQ", "p1220 p.65 'Fishing boat proceeds' = MISC col AQ p.4"),
            "6": ("AR", "p1220 p.65 'Medical and health care payments' = MISC col AR p.5"),
            "8": ("AT", "p1220 p.65 'Substitute payments in lieu of dividends or interest' = MISC col AT p.5"),
            "A": ("AU", "p1220 p.65 'Crop insurance proceeds' = MISC col AU p.5"),
            "C": ("AV", "p1220 p.65 'Gross proceeds paid to an attorney' = MISC col AV p.5"),
            "D": ("AX", "p1220 p.65 'Section 409A deferrals' = MISC col AX p.5"),
            "F": ("AW", "p1220 p.65 'Fish purchased for resale' = MISC col AW p.5"),
        },
        "amount_gaps": {
            "E": "SPEC-GAP G1: FIRE 'Section 409A income' (p1220 p.65) vs IRIS 'Box 15 - Nonqualified deferred "
                 "compensation' (MISC col AY p.5): titles differ, neither spec maps them. Not converted.",
            "G": "FIRE 'Prior Year Nonemployee Compensation (TY2019 and earlier)' (p1220 p.65) has no IRIS column "
                 "in the TY2025 MISC template. Not converted.",
        },
        "second_tin": "AL",      # p1220 p.112 pos 544 -> MISC col AL p.4
        "direct_sales": "AS",    # p1220 p.112 pos 547 -> MISC col AS p.5
        "fatca": "AK",           # p1220 p.112 pos 548 '1' -> MISC col AK p.4
        "cfsf": "AZ", "state1": "BA", "state1_tax": "BB", "state1_local": "BE",  # MISC p.6
    },
}

TABLES = {
    # Tax year 2025: Pub 1220 Rev. 5-2026 + IRIS TY2025 template guidelines.
    # TY2026: add an entry here when the IRS publishes the TY2026 guidelines (SPEC-GAP G6).
    "2025": {"fire": FIRE_P1220_REV_5_2026, "forms": FORMS_TY2025},
}

# Type of Return codes (p1220 p.56), used to name rejected types clearly.
RETURN_TYPE_NAMES = {
    "BT": "1097-BTC", "3": "1098", "X": "1098-C", "2": "1098-E", "FP": "1098-F", "QL": "1098-Q",
    "8": "1098-T", "4": "1099-A", "B": "1099-B", "5": "1099-C", "P": "1099-CAP", "1": "1099-DIV",
    "F": "1099-G", "6": "1099-INT", "MC": "1099-K", "LS": "1099-LS", "T": "1099-LTC", "A": "1099-MISC",
    "NE": "1099-NEC", "D": "1099-OID", "7": "1099-PATR", "Q": "1099-Q", "9": "1099-R", "S": "1099-S",
    "M": "1099-SA", "SB": "1099-SB", "N": "3921", "Z": "3922", "L": "5498", "V": "5498-ESA",
    "K": "5498-SA", "W": "W-2G",
}

TIN_TYPE_2_CHOICES = ("SSN", "ITIN", "ATIN")  # p1220 p.75: code 2 covers SSN, ITIN and ATIN


# --------------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------------
class Refusal(Exception):
    """The converter will not produce CSVs for this input. Message is shown to the user.

    details: every problem found (all of them, never only the first few). The message on stderr shows up to
    REFUSAL_SHOWN; conversion-report.txt lists them all."""

    def __init__(self, msg, details=None):
        Exception.__init__(self, msg)
        self.details = list(details or [])


REFUSAL_SHOWN = 20


def _refusal(prefix, errs):
    shown = "; ".join(errs[:REFUSAL_SHOWN])
    if len(errs) > REFUSAL_SHOWN:
        shown += "; ... and %d more (all listed in conversion-report.txt)" % (len(errs) - REFUSAL_SHOWN)
    return Refusal(prefix + shown, errs)


# --------------------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------------------
def split_records(data):
    """Split raw bytes into 750-position records (p1220 p.47).

    Positions 749-750 may be blanks or CR/LF (p1220 p.52, p.72, p.114, p.140, p.144), so a file may be
    newline-delimited lines of 748-750 characters or one unbroken stream of 750-byte records.
    """
    text = data.decode("latin-1")
    errors = []
    records = []
    bad = non_ascii(text)
    if bad:
        return [], [bad]
    if "\n" in text or "\r" in text:
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        while lines and lines[-1].strip() == "":
            lines.pop()
        for i, line in enumerate(lines, 1):
            if len(line) < 748 or len(line) > RECORD_LEN:
                errors.append("line %d: length %d, expected 748-750 positions (p1220 p.47)" % (i, len(line)))
                continue
            if line[748:].strip():
                errors.append("line %d: positions 749-750 must be blanks or CR/LF (p1220 p.52)" % i)
                continue
            records.append(line.ljust(RECORD_LEN))
    else:
        if len(text) % RECORD_LEN:
            errors.append("file length %d is not a multiple of 750 (p1220 p.47)" % len(text))
        for i in range(0, len(text) - len(text) % RECORD_LEN, RECORD_LEN):
            rec = text[i:i + RECORD_LEN]
            if rec[748:].strip():
                errors.append("record %d: positions 749-750 must be blanks or CR/LF (p1220 p.52)"
                              % (i // RECORD_LEN + 1))
            records.append(rec)
    return records, errors


def _looks_utf8(text, off):
    """True if the byte at off starts a well-formed UTF-8 multi-byte sequence."""
    b = ord(text[off])
    n = 1 if 0xC2 <= b <= 0xDF else 2 if 0xE0 <= b <= 0xEF else 3 if 0xF0 <= b <= 0xF4 else 0
    tail = text[off + 1:off + 1 + n]
    return bool(n) and len(tail) == n and all(0x80 <= ord(c) <= 0xBF for c in tail)


def non_ascii(text):
    """Refusal message for the first byte outside 7-bit ASCII, or "" if the file is all ASCII.

    p1220 p.37 (Part B Sec. 4.01): "Standard ASCII code is required for all files"; p.41 (Sec. 6 items 7
    and 11) repeats it. A non-ASCII byte means the file was not written as a FIRE file expects (often a
    UTF-8 or Windows code page export), and decoding it would garble names, so the converter refuses.
    """
    m = re.search(r"[^\x00-\x7f]", text)
    if not m:
        return ""
    off = m.start()
    total = len(re.findall(r"[^\x00-\x7f]", text))
    if "\n" in text or "\r" in text:
        head = text[:off].replace("\r\n", "\n").replace("\r", "\n")
        rec = head.count("\n") + 1
        pos = off - max(text.rfind("\n", 0, off), text.rfind("\r", 0, off))
    else:
        rec = off // RECORD_LEN + 1
        pos = off % RECORD_LEN + 1
    kind = ("it looks like UTF-8; export the file as plain ASCII" if _looks_utf8(text, off)
            else "it looks like a Windows/latin-1 code page; export the file as plain ASCII")
    return ("file is not standard ASCII: byte 0x%02X at byte offset %d (record %d, position %d); %d non-ASCII "
            "byte(s) in total. Pub 1220 p.37 (Sec. 4.01) requires standard ASCII for all files (also p.41). "
            "Refusing rather than guessing the encoding, because a wrong guess garbles names: %s, "
            "or replace accented letters with plain ones." % (ord(text[off]), off, rec, pos, total, kind))


def field(rec, f):
    return rec[f["start"] - 1:f["end"]]


def parse_amount(raw, where):
    """Parse a FIRE payment amount or control total into integer cents (p1220 p.77, p.139).

    Right-most two positions are cents. A leading '+' or '-' is allowed. 1099-NEC/MISC amounts may not be
    negative (p1220 p.77: negatives only for 1099-B, 1099-OID, 1099-Q), so '-' and negative overpunch are
    refused rather than converted.
    """
    s = raw
    if s.strip() == "":
        return 0  # K record state/local totals may be blank (p1220 p.142)
    sign = 1
    if s[0] in "+-":
        sign = -1 if s[0] == "-" else 1
        s = s[1:]
    if not s.isdigit():
        raise Refusal("%s: payment amount %r is not numeric (overpunch or bad data); p1220 p.77 requires "
                      "digits, and 1099-NEC/MISC amounts may not be negative" % (where, raw))
    v = int(s) * sign
    if v < 0:
        raise Refusal("%s: negative amount %r; not allowed for 1099-NEC/MISC (p1220 p.77)" % (where, raw))
    return v


def cents_to_str(c):
    """IRIS money format: digits with at most one decimal and two places (NEC col AL p.4)."""
    return "%d.%02d" % divmod(c, 100)


def parse_transmission(records):
    """Group records into T, issuers [A, B..., C, K...], F. Structural problems become errors."""
    errors = []
    tx = {"T": None, "issuers": [], "F": None}
    cur = None
    expect_seq = 1
    for idx, rec in enumerate(records):
        rt = rec[0]
        seq = rec[499:507]
        if seq.strip().isdigit() and int(seq) != expect_seq:
            errors.append("record %d (%s): sequence number %s, expected %d (p1220 p.50)"
                          % (idx + 1, rt, seq.strip(), expect_seq))
        expect_seq += 1
        where = "record %d (%s, seq %s)" % (idx + 1, rt, seq.strip())
        if rt == "T":
            if idx != 0:
                errors.append("%s: T record must be first and only once (p1220 p.48)" % where)
            tx["T"] = rec
        elif rt == "A":
            if tx["T"] is None:
                errors.append("%s: A record before T record (p1220 p.48)" % where)
            if cur is not None and cur["C"] is None:
                errors.append("%s: new A record before the previous issuer's C record (p1220 p.54)" % where)
            cur = {"A": rec, "B": [], "C": None, "K": [], "where": where}
            tx["issuers"].append(cur)
        elif rt == "B":
            if cur is None or cur["C"] is not None:
                errors.append("%s: B record not preceded by an A record (p1220 p.54)" % where)
                continue
            cur["B"].append((rec, where))
        elif rt == "C":
            if cur is None or cur["C"] is not None:
                errors.append("%s: C record without a matching A record (p1220 p.139)" % where)
                continue
            cur["C"] = rec
        elif rt == "K":
            if cur is None or cur["C"] is None:
                errors.append("%s: K record must follow a C record (p1220 p.141)" % where)
                continue
            cur["K"].append((rec, where))
        elif rt == "F":
            if idx != len(records) - 1:
                errors.append("%s: F record must be last (p1220 p.144)" % where)
            tx["F"] = rec
        else:
            errors.append("%s: unknown record type %r (only T, A, B, C, K, F exist, p1220 p.47)" % (where, rt))
    if tx["T"] is None:
        errors.append("no T record (p1220 p.48)")
    if tx["F"] is None:
        errors.append("no F record (p1220 p.144)")
    for iss in tx["issuers"]:
        if iss["C"] is None:
            errors.append("%s: issuer has no C record (p1220 p.139)" % iss["where"])
    return tx, errors


def amount_codes(arec):
    return [c for c in arec[27:45] if c != " "]  # p1220 p.57 positions 28-45


def check_controls(tx, layout, forms):
    """Check C, K, F and T control fields against the B records (p1220 p.50, p.139, p.141, p.144).

    Stores per-issuer B sums by amount code in iss["b_sums"]. Issuers of unsupported return types are only
    counted (their amounts are never read or converted)."""
    errors = []
    total_b = 0
    ctf = {f["name"][-1]: f for f in layout["C"] if f["name"].startswith("Control Total")}
    for iss in tx["issuers"]:
        sums = {c: 0 for c in AMOUNT_CODES}
        if iss["A"][25:27].strip() not in forms:
            iss["b_sums"] = sums
            total_b += len(iss["B"])
            continue
        for rec, where in iss["B"]:
            for f in layout["B_common"]:
                if f["name"].startswith("Payment Amount"):
                    sums[f["name"][-1]] += parse_amount(field(rec, f), where + " " + f["name"])
        iss["b_sums"] = sums
        total_b += len(iss["B"])
        c = iss["C"]
        if c is None:
            continue
        n = c[1:9]
        if not n.isdigit() or int(n) != len(iss["B"]):
            errors.append("%s: C record says %s payees, file has %d B records (p1220 p.139)"
                          % (iss["where"], n.strip(), len(iss["B"])))
        for code in AMOUNT_CODES:
            ct = parse_amount(field(c, ctf[code]), "C record Control Total " + code)
            if ct != sums[code]:
                errors.append("%s: C record Control Total %s = %s but B records sum to %s (p1220 p.139)"
                              % (iss["where"], code, cents_to_str(ct), cents_to_str(sums[code])))
        # K records: per CF/SF state (p1220 p.141-142)
        rt = iss["A"][25:27].strip()
        bspec = layout.get("B_" + rt)
        for krec, kwhere in iss["K"]:
            st = krec[746:748]
            ksums = {cd: 0 for cd in AMOUNT_CODES}
            kn = 0
            ks = kl = 0
            for rec, where in iss["B"]:
                if rec[746:748] != st:
                    continue
                kn += 1
                for f in layout["B_common"]:
                    if f["name"].startswith("Payment Amount"):
                        ksums[f["name"][-1]] += parse_amount(field(rec, f), where)
                if bspec:
                    ks += _num_or_zero(rec[722:734])
                    kl += _num_or_zero(rec[734:746])
            if not krec[1:9].isdigit() or int(krec[1:9]) != kn:
                errors.append("%s: K record for state code %s says %s payees, file has %d (p1220 p.141)"
                              % (kwhere, st, krec[1:9].strip(), kn))
            for code in AMOUNT_CODES:
                kt = parse_amount(field(krec, ctf[code]), kwhere)
                if kt != ksums[code]:
                    errors.append("%s: K record (state %s) Control Total %s = %s but its B records sum to %s "
                                  "(p1220 p.141)" % (kwhere, st, code, cents_to_str(kt), cents_to_str(ksums[code])))
            for lo, hi, val, nm in ((706, 724, ks, "State"), (724, 742, kl, "Local")):
                raw = krec[lo:hi]
                if raw.strip() and _num_or_zero(raw) != val:
                    errors.append("%s: K record (state %s) %s Income Tax Withheld Total %s, B records sum to %s "
                                  "(p1220 p.142)" % (kwhere, st, nm, raw.strip(), cents_to_str(val)))
        # p1220 p.55 pos 6 Note 1: "If the Issuer 'A' Record is coded for CF/SF Program, there must be coding in
        # the Payee 'B' Records and the State Totals 'K' Records." p.141: "Submit a separate 'K' Record for each
        # state being reported." A missing K record leaves the state totals unchecked, so refuse.
        if iss["A"][5] == "1":
            kstates = set(krec[746:748] for krec, _ in iss["K"])
            coded = sorted(set(rec[746:748] for rec, _ in iss["B"] if rec[746:748].strip()))
            if not coded and not iss["K"]:
                errors.append("%s: A record pos 6 = '1' (Combined Federal/State Filing Program) but no B record has "
                              "a CF/SF state code and there are no K records. p1220 p.55 Note 1: an A record coded "
                              "for CF/SF must have coded B records and K records. Fix the export, or blank A pos 6 "
                              "if the file is not part of CF/SF." % iss["where"])
            for st in coded:
                if st not in kstates:
                    errors.append("%s: A record is coded for the Combined Federal/State Filing Program and B records "
                                  "carry CF/SF state code %s, but there is no K record for that state. p1220 p.55 "
                                  "Note 1 requires K records when the A record is coded for CF/SF, and p.141 requires "
                                  "a separate K record for each state reported." % (iss["where"], st))
    f = tx["F"]
    if f is not None:
        na = f[1:9]
        if na.strip() and (not na.isdigit() or int(na) != len(tx["issuers"])):
            errors.append("F record says %s A records, file has %d (p1220 p.144)" % (na.strip(), len(tx["issuers"])))
        npay = f[49:57]
        if npay.strip() and (not npay.isdigit() or int(npay) != total_b):
            errors.append("F record says %s payees, file has %d B records (p1220 p.144)" % (npay.strip(), total_b))
    t = tx["T"]
    if t is not None:
        tp = t[295:303]
        if tp.strip() and (not tp.isdigit() or int(tp) != total_b):
            errors.append("T record says %s payees, file has %d B records (p1220 p.50)" % (tp.strip(), total_b))
    return errors


def _num_or_zero(raw):
    s = raw.strip()
    if s.isdigit():
        return int(s)
    return 0


# --------------------------------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------------------------------
def fmt_tin(tin9, kind):
    """IRIS TIN formats: EIN XX-XXXXXXX, SSN/ITIN/ATIN XXX-XX-XXXX, UND XXXXXXXXX (NEC col D p.1, col U p.2)."""
    if kind in ("EIN", "QI-EIN"):
        return tin9[:2] + "-" + tin9[2:]
    if kind in ("SSN", "ITIN", "ATIN"):
        return tin9[:3] + "-" + tin9[3:5] + "-" + tin9[5:]
    return tin9


def mask_tin(tin):
    d = "".join(ch for ch in tin if ch.isdigit())
    return "*****" + d[-4:] if d else "(blank)"


_DIGIT_RUN = re.compile(r"[0-9](?:[- ]?[0-9]){8,}")


def mask_free_text(v):
    """Mask every run of 9+ digits (dashes or single spaces allowed between them) in text the report prints.

    Free-text FIRE fields (Special Data Entries, name lines, blank positions) may hold a TIN the filer typed
    in, e.g. 'SSN 123456789' or '12-3456789'. The report never prints one: each run becomes *****NNNN."""
    return _DIGIT_RUN.sub(lambda m: mask_tin(m.group(0)), v)


class Report:
    def __init__(self):
        self.errors = []          # refusals
        self.needs_decision = []  # ambiguous fields the user must decide
        self.unmapped = {}        # (record, field) -> {"count", "cite", "note", "examples"}
        self.unmapped_amounts = {}  # (form, code) -> cents
        self.unmapped_by_payer = {}  # (form, payer TIN digits, code) -> cents (per-payer reconciliation)
        self.validation = []      # IRIS formatting problems in output (portal will reject)
        self.warnings = []
        self.decisions_applied = []
        self.file_totals = {}     # form -> [(records, {box letter: cents})], one entry per CSV in batch order
        self.file_boxes = {}      # form -> [(box letter, "Box N")] shown next to each CSV under Files written

    def add_unmapped(self, rec_label, fname, cite, note, where, value=None, amount=False):
        """value is a sample shown in the report. Unless it is an amount the converter formatted itself, it
        is masked with mask_free_text (TEST-r1 D1: free text may hold a typed-in TIN)."""
        k = (rec_label, fname)
        e = self.unmapped.setdefault(k, {"count": 0, "cite": cite, "note": note, "examples": []})
        e["count"] += 1
        if len(e["examples"]) < 3:
            if value is not None and not amount:
                value = mask_free_text(value)
            e["examples"].append(where if value is None else "%s: %r" % (where, value))


def list_fire_only(rec, fields, rec_label, where, report, skip=()):
    """List every FIRE-only / unexpected / gap field that holds data (never silently dropped)."""
    for f in fields:
        if f["name"] in skip:
            continue
        v = field(rec, f)
        if f["disp"] == "fire_only" and v.strip():
            val = mask_tin(v) if "TIN" in f["name"] else v.strip()
            report.add_unmapped(rec_label, f["name"], f["cite"], "FIRE-only field, no IRIS column", where, val)
        elif f["disp"] == "gap" and v.strip():
            report.add_unmapped(rec_label, f["name"], f["cite"], "no clear IRIS column in the spec; not converted",
                                where, v.strip())
        elif f["disp"] == "blank" and v.strip():
            report.add_unmapped(rec_label, f["name"] + " (pos %d-%d)" % (f["start"], f["end"]), f["cite"],
                                "should be blank per Pub 1220 but holds data; not converted", where,
                                mask_free_text(v.strip())[:40])


def convert(tx, options, report):
    """Map each B record to one IRIS row. Returns {form_name: [row dict letter->value]}."""
    t = tx["T"]
    year = t[1:5]
    tables = TABLES.get(year)
    if tables is None:
        raise Refusal("FIRE payment year %r: no IRIS template in this tool for that tax year (have: %s). "
                      "SPEC-GAP G6." % (year, ", ".join(sorted(TABLES))))
    layout = tables["fire"]
    if t[5] == "P":
        report.errors.append("T record pos 6 = 'P' (prior-year data, p1220 p.48): not supported")
    if t[27] == "T":
        report.warnings.append("T record pos 28 = 'T': this is a FIRE TEST file (p1220 p.48)")
    list_fire_only(t, layout["T"], "T", "T record", report)

    out = {}
    tin2_needed = False
    for iss in tx["issuers"]:
        a = iss["A"]
        rt = a[25:27].strip()
        if a[1:5] != year:
            report.errors.append("%s: payment year %s differs from T record %s (p1220 p.48: no mixed years)"
                                 % (iss["where"], a[1:5], year))
        if rt not in tables["forms"]:
            name = RETURN_TYPE_NAMES.get(rt, "unknown code %r" % rt)
            seqs = [r[499:507].strip() for r, _ in iss["B"]]
            span = (" (B record seq %s-%s)" % (seqs[0], seqs[-1])) if seqs else ""
            msg = ("%s: Type of Return %r = Form %s (p1220 p.56). Only 1099-NEC (NE) and 1099-MISC (A) are "
                   "supported; this issuer's %d records%s were NOT converted."
                   % (iss["where"], rt, name, len(iss["B"]), span))
            if options.skip_unsupported:
                report.warnings.append(msg)
            else:
                report.errors.append(msg + " Re-run with --skip-unsupported to convert the rest.")
            continue
        spec = tables["forms"][rt]
        form = spec["form"]
        bspec = layout["B_" + rt]
        codes = amount_codes(a)
        rows = out.setdefault(form, [])

        # Issuer checks (p1220 p.70-72)
        if a[51] == "1":
            report.errors.append("%s: issuer Foreign Entity Indicator = 1 (p1220 p.70). Foreign address is free-format "
                                 "city/state/ZIP (p1220 p.71) and cannot be split into IRIS cols O-Q. SPEC-GAP G4."
                                 % iss["where"])
        if a[132] == "1":
            report.errors.append("%s: Transfer Agent Indicator = 1 (p1220 p.71): name line 2 and the address are the "
                                 "agent's, and IRIS has no transfer-agent columns. SPEC-GAP G5." % iss["where"])
        list_fire_only(a, layout["A"], "A", iss["where"], report)
        cfsf_program = a[5] == "1"  # p1220 p.55 pos 6

        payer = {}
        payer["A"] = form                                     # NEC/MISC col A p.1
        payer["B"] = year                                     # col B; p1220 p.55 pos 2-5
        ptype = options.payer_tin_type                        # col C: FIRE A record has no TIN type (G2)
        payer["C"] = ptype or ""
        ptin = a[11:20]                                       # p1220 p.55 pos 12-20
        if not ptin.isdigit():
            report.errors.append("%s: Issuer TIN %r is not 9 digits (p1220 p.55)" % (iss["where"], mask_tin(ptin)))
        payer["D"] = fmt_tin(ptin, ptype) if ptype else ptin
        payer["E"] = "B" if options.names_as_business else ""  # col E (G2)
        payer["F"] = a[52:92].strip()                         # p1220 p.71 pos 53-92 -> col F
        payer["G"] = a[92:132].strip() if a[132] != "1" else ""  # pos 93-132 continuation when TA=0 -> col G
        for k in ("H", "I", "J", "K", "N"):
            payer[k] = ""
        payer["L"] = "US"                                     # p1220 p.71: non-foreign = U.S. address
        payer["M"] = a[133:173].strip()                       # pos 134-173 -> col M
        payer["O"] = a[173:213].strip()                       # pos 174-213 -> col O
        payer["P"] = a[213:215].strip()                       # pos 214-215 -> col P
        payer["Q"] = a[215:224].strip()                       # pos 216-224 -> col Q
        phone = a[224:239].strip()                            # pos 225-239 phone & extension -> cols R,S
        if phone:
            payer["R"] = "D"
            payer["S"] = phone[:10]
            if len(phone) > 10:
                report.add_unmapped("A", "Issuer Telephone Extension", "p1220 p.72 (pos 225-239); NEC col S p.2",
                                    "IRIS Payer Phone has no extension; digits after the first 10 not converted",
                                    iss["where"], phone[10:])
        else:
            payer["R"] = payer["S"] = ""

        for rec, where in iss["B"]:
            row = {letter: "" for letter, _ in spec["columns"]}
            row.update(payer)
            if rec[1:5] != year:
                report.errors.append("%s: payment year %s differs from T record (p1220 p.74)" % (where, rec[1:5]))
            if rec[5] != " ":
                report.errors.append("%s: Corrected Return Indicator %r (p1220 p.74). Corrections are not supported; "
                                     "the TY2025 IRIS CSV guidelines have no correction column." % (where, rec[5]))
            if rec[286] == "1":
                report.errors.append("%s: Foreign Country Indicator = 1 (p1220 p.78): free-format address cannot be "
                                     "split into IRIS cols AF-AH. SPEC-GAP G4." % where)
            list_fire_only(rec, layout["B_common"] + bspec, "B (" + form + ")", where, report)

            # Recipient TIN (p1220 p.75 pos 11-20 -> cols T, U)
            ttype = rec[10]
            tin = rec[11:20]
            if tin.strip() == "":
                kind = ""
            elif ttype == "1":
                kind = "EIN"                                  # p1220 p.75 code 1 = EIN
            elif ttype == "2":
                kind = options.tin_type_2 or ""               # code 2 = SSN, ITIN or ATIN (G2)
                tin2_needed = tin2_needed or not options.tin_type_2
            elif ttype == " ":
                kind = "UND"                                  # p1220 p.75 "not determinable" = col T "UND"
            else:
                kind = ""
                report.errors.append("%s: Type of TIN %r is not 1, 2 or blank (p1220 p.75)" % (where, ttype))
            if tin.strip() and not tin.isdigit():
                report.errors.append("%s: payee TIN %s is not 9 digits (p1220 p.75)" % (where, mask_tin(tin)))
            row["T"] = kind
            row["U"] = fmt_tin(tin, kind) if kind and tin.isdigit() else tin.strip()

            row["V"] = "B" if options.names_as_business else ""  # col V (G2)
            row["W"] = rec[287:327].strip()                   # pos 288-327 -> col W
            row["X"] = rec[327:367].strip()                   # pos 328-367 -> col X
            row["AC"] = "US"                                  # pos 287 blank = U.S. address (p1220 p.78)
            row["AD"] = rec[367:407].strip()                  # pos 368-407 -> col AD
            row["AF"] = rec[447:487].strip()                  # pos 448-487 -> col AF
            row["AG"] = rec[487:489].strip()                  # pos 488-489 -> col AG
            row["AH"] = rec[489:498].strip()                  # pos 490-498 -> col AH
            row["AI"] = rec[40:44].strip()                    # pos 41-44 office code -> col AI
            row["AJ"] = rec[20:40].strip()                    # pos 21-40 account number -> col AJ

            # Indicators
            row[spec["second_tin"]] = "Y" if rec[543] == "2" else "N"   # pos 544
            row[spec["direct_sales"]] = "Y" if rec[546] == "1" else "N"  # pos 547
            if spec["fatca"]:
                row[spec["fatca"]] = "Y" if rec[547] == "1" else "N"     # MISC pos 548
            for pos, okv, nm in ((543, "2", "Second TIN Notice"), (546, "1", "Direct Sales Indicator")) + (
                    ((547, "1", "FATCA Filing Requirement Indicator"),) if spec["fatca"] else ()):
                if rec[pos] not in (" ", okv):
                    report.errors.append("%s: %s = %r, expected %r or blank (p1220 p.112-113)" % (where, nm, rec[pos], okv))

            # Payment amounts (p1220 p.77-78) -> box columns (p1220 p.65 amount codes)
            for i, code in enumerate(AMOUNT_CODES):
                raw = rec[54 + 12 * i:66 + 12 * i]
                cents = parse_amount(raw, "%s Payment Amount %s" % (where, code))
                if cents == 0:
                    continue  # p1220 p.77: unused amounts are zero -> IRIS box left empty
                if code in spec["amounts"] and code in codes:
                    row[spec["amounts"][code][0]] = cents_to_str(cents)
                else:
                    if code in spec["amount_gaps"]:
                        note = spec["amount_gaps"][code]
                    elif code not in codes:
                        note = "amount code %s not declared in the A record (p1220 p.57); not converted" % code
                    else:
                        note = "amount code %s is not a %s amount code (p1220 p.65); not converted" % (code, form)
                    report.add_unmapped("B (" + form + ")", "Payment Amount " + code, "p1220 p.77-78", note,
                                        where, cents_to_str(cents), amount=True)
                    k = (form, code)
                    report.unmapped_amounts[k] = report.unmapped_amounts.get(k, 0) + cents
                    kp = (form, ptin, code)
                    report.unmapped_by_payer[kp] = report.unmapped_by_payer.get(kp, 0) + cents

            # State fields (p1220 p.112-114 pos 723-748; Table 1 p.25)
            cf = rec[746:748]
            st_abbr = ""
            if cf.strip():
                st_abbr = CFSF_STATE_CODES.get(cf, "")
                if not st_abbr:
                    report.errors.append("%s: CF/SF code %r is not in Pub 1220 Table 1 (p1220 p.25)" % (where, cf))
                elif not cfsf_program:
                    report.errors.append("%s: CF/SF code %s on B record but A record pos 6 is not '1' (p1220 p.55)"
                                         % (where, cf))
                else:
                    row[spec["cfsf"]] = st_abbr
            st_raw, lo_raw = rec[722:734], rec[734:746]
            st_tax = _state_amount(st_raw, rt, where, "State Income Tax Withheld", report, bspec)
            lo_tax = _state_amount(lo_raw, rt, where, "Local Income Tax Withheld", report, bspec)
            if st_tax or lo_tax:
                if st_abbr and cfsf_program:
                    # The B record's state is its CF/SF state (p1220 p.24: one B record per state; K totals
                    # per state include these withholding fields, p.142) -> State 1 section.
                    row[spec["state1"]] = st_abbr
                    if st_tax:
                        row[spec["state1_tax"]] = cents_to_str(st_tax)
                    if lo_tax:
                        row[spec["state1_local"]] = cents_to_str(lo_tax)
                else:
                    for v, nm in ((st_tax, "State Income Tax Withheld"), (lo_tax, "Local Income Tax Withheld")):
                        if v:
                            report.add_unmapped("B (" + form + ")", nm, "p1220 p.112-114",
                                                "no CF/SF state code on the record, so the IRIS State 1 state is "
                                                "unknown (SPEC-GAP G3); not converted", where, cents_to_str(v),
                                                amount=True)
            rows.append({"row": row, "where": where, "seq": rec[499:507]})

    if not options.payer_tin_type and any(tables["forms"].get(i["A"][25:27].strip()) for i in tx["issuers"]):
        report.needs_decision.append(
            "--payer-tin-type EIN|SSN: IRIS col C 'Payer TIN Type' is required (NEC p.1) but the FIRE A record "
            "has no TIN type (p1220 p.55 pos 12-20). Applies to every issuer in the file. SPEC-GAP G2.")
    if tin2_needed:
        report.needs_decision.append(
            "--tin-type-2 SSN|ITIN|ATIN: FIRE Type of TIN '2' means SSN, ITIN or ATIN (p1220 p.75) but IRIS col T "
            "needs one of them (NEC p.2). Applies to every type-2 payee. SPEC-GAP G2.")
    if not options.names_as_business:
        report.needs_decision.append(
            "--names-as-business: FIRE has one free-text name line per payer/payee (p1220 p.71, p.79) and no "
            "business/individual flag; IRIS needs Name Type B or I and, for I, split first/last names (NEC cols "
            "E-K, V-AB). With this flag every name goes to Business Name Line 1/2 with Name Type 'B'. SPEC-GAP G2.")
    else:
        report.decisions_applied.append("--names-as-business: FIRE name lines -> IRIS Business Name Line 1/2, Name Type B")
    if options.payer_tin_type:
        report.decisions_applied.append("--payer-tin-type %s for every issuer" % options.payer_tin_type)
    if options.tin_type_2:
        report.decisions_applied.append("--tin-type-2 %s for every payee with FIRE Type of TIN '2'" % options.tin_type_2)
    return out


def _state_amount(raw, rt, where, name, report, bspec):
    """State/local withholding (p1220 p.112-114). MISC allows text continuation of Special Data Entries."""
    if raw.strip() == "" or raw.strip("0") == "":
        return 0
    if raw.isdigit():
        return int(raw)
    report.add_unmapped("B", name + " (text, read as Special Data Entries continuation)",
                        "p1220 p.112", "non-numeric; SPEC-GAP G3; not converted", where, raw.strip())
    return 0


# --------------------------------------------------------------------------------------------------
# IRIS output validation (does NOT change values; reports what the portal will likely reject)
# --------------------------------------------------------------------------------------------------
_NAME_B1 = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -#()&'")
_NAME_B2 = _NAME_B1 | set("/%")
_ADDR = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -/")
_CITY = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ ")


def _spaces_bad(v):
    return v != v.strip() or "  " in v


def validate_row(row, spec):
    """Rules from the TY2025 guidelines (cols A-AJ identical for NEC and MISC, p.1-4)."""
    issues = []

    def chk(letter, maxlen, charset, label):
        v = row.get(letter, "")
        if not v:
            return
        if len(v) > maxlen:
            issues.append("col %s %s: %d characters, max %d" % (letter, label, len(v), maxlen))
        if charset is not None and any(ch not in charset for ch in v):
            bad = "".join(sorted(set(ch for ch in v if ch not in charset)))
            issues.append("col %s %s: characters not allowed: %r" % (letter, label, bad))
        if _spaces_bad(v):
            issues.append("col %s %s: leading, trailing or adjacent spaces" % (letter, label))
        if v[0] in "=+-@":
            # '-' is an IRIS-allowed character (NEC p.1-3), but a spreadsheet opening the CSV may run a value
            # that starts with = + - @ as a formula (TEST-r1 N1). Flagged, value not changed.
            issues.append("col %s %s: starts with %r; a spreadsheet may read it as a formula" % (letter, label, v[0]))

    chk("F", 40, _NAME_B1, "Payer Name Line 1")              # p.1
    chk("G", 40, _NAME_B2, "Payer Name Line 2")              # p.1
    chk("M", 35, _ADDR, "Payer Address Line 1")              # p.1
    chk("O", 40, _CITY, "Payer City/Town")                   # p.2
    chk("W", 40, _NAME_B1, "Recipient Name Line 1")          # p.2
    chk("X", 40, _NAME_B2, "Recipient Name Line 2")          # p.2
    chk("AD", 35, _ADDR, "Recipient Address Line 1")         # p.3
    chk("AF", 40, _CITY, "Recipient City/Town")              # p.3
    chk("AJ", 20, None, "Form Account Number")               # p.4
    for letter, label in (("F", "Payer Name Line 1"), ("M", "Payer Address Line 1"), ("O", "Payer City/Town"),
                          ("P", "Payer State"), ("Q", "Payer ZIP"), ("AD", "Recipient Address Line 1"),
                          ("AF", "Recipient City/Town"), ("AG", "Recipient State"), ("AH", "Recipient ZIP"),
                          ("W", "Recipient Name Line 1")):
        if not row.get(letter):
            issues.append("col %s %s: required but empty" % (letter, label))
    for letter in ("Q", "AH"):                               # p.2, p.4: domestic ZIP exactly 5, 9 or 12 digits
        v = row.get(letter, "")
        if v and not (v.isdigit() and len(v) in (5, 9, 12)):
            issues.append("col %s ZIP %r: must be 5, 9 or 12 digits" % (letter, v))
    for letter in ("P", "AG"):
        v = row.get(letter, "")
        if v and not (len(v) == 2 and v.isalpha() and v.isupper()):
            issues.append("col %s state %r: must be a 2-letter abbreviation" % (letter, v))
    v = row.get("AI", "")                                    # p.4 office code exactly 4 digits
    if v and not (len(v) == 4 and v.isdigit()):
        issues.append("col AI Office Code %r: must be exactly 4 digits" % v)
    acct = row.get("AJ", "")                                 # p.4 not the same as payer/recipient TIN
    if acct and acct.replace("-", "") in (row.get("D", "").replace("-", ""), row.get("U", "").replace("-", "")):
        issues.append("col AJ Form Account Number may not equal a TIN")
    s = row.get("S", "")
    if s and not (s.isdigit() and len(s) == 10):             # p.2 domestic phone 1231231234
        issues.append("col S Payer Phone %r: domestic phone must be 10 digits" % s)
    if row.get("U") and not row.get("T"):
        issues.append("col T Recipient TIN Type: required when a TIN is entered")
    u = row.get("U", "").replace("-", "")
    if not u:
        # p1220 p.75 pos 12-20: "Required." IRIS col U has no required marker (NEC p.2), so the row is written,
        # but a missing TIN can bring IRS penalty or backup-withholding notices. Listed, never filled in.
        issues.append("col U Recipient TIN: blank. Pub 1220 p.75 says the payee TIN is required; IRIS accepts "
                      "the row, but a missing TIN can bring an IRS penalty notice. Add it before you file")
    elif len(set(u)) == 1:
        issues.append("col U Recipient TIN: every digit is the same; Pub 1220 p.75 calls this an obviously "
                      "incorrect TIN")
    if spec["form"] == "1099-NEC" and not row.get("AL") and row.get("AM") != "Y":
        issues.append("col AL/AM: Box 1 must be filled in or Box 2 checked (NEC p.4)")
    return issues


# --------------------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------------------
def csv_line(values, quote_cols):
    out = []
    for i, v in enumerate(values):
        if i in quote_cols and v:
            out.append('"' + v.replace('"', '""') + '"')  # CF/SF list must be in quotes (NEC col AP p.4)
        elif any(ch in v for ch in ',"\r\n'):
            out.append('"' + v.replace('"', '""') + '"')
        else:
            out.append(v)
    return ",".join(out)


def render_batches(form_rows, spec, batch_size):
    """Split rows into CSV texts of at most batch_size records, header row not counted (Pub 5717 p.20)."""
    cols = spec["columns"]
    header = [title for _, title in cols]
    cf_idx = {i for i, (letter, _) in enumerate(cols) if letter == spec["cfsf"]}
    batches = []
    for start in range(0, len(form_rows), batch_size):
        chunk = form_rows[start:start + batch_size]
        lines = [csv_line(header, set())]
        for r in chunk:
            lines.append(csv_line([r["row"].get(letter, "") for letter, _ in cols], cf_idx))
        batches.append("\r\n".join(lines) + "\r\n")
    return batches


def read_csv_sums(texts, spec):
    """Re-read written CSV text and sum each box column (used for the round-trip check).

    Also returns per-payer sums keyed by the digits of col D (Payer TIN): {tin: {"rows": n, letter: cents}}."""
    cols = spec["columns"]
    box_letters = {letter for letter, _ in spec["amounts"].values()}
    box_letters |= {spec["state1_tax"], spec["state1_local"]}
    sums = {letter: 0 for letter in box_letters}
    by_payer = {}
    d_idx = [letter for letter, _ in cols].index("D")
    nrows = 0
    for text in texts:
        reader = csv.reader(io.StringIO(text, newline=""))
        header = next(reader)
        if header != [t for _, t in cols]:
            raise AssertionError("header mismatch on re-read")
        for row in reader:
            nrows += 1
            if len(row) != len(cols):
                raise AssertionError("row has %d columns, template has %d" % (len(row), len(cols)))
            ptin = "".join(ch for ch in row[d_idx] if ch.isdigit())
            ps = by_payer.setdefault(ptin, dict({letter: 0 for letter in box_letters}, rows=0))
            ps["rows"] += 1
            for i, (letter, _) in enumerate(cols):
                if letter in sums and row[i]:
                    d, c = row[i].split(".")
                    v = int(d) * 100 + int(c)
                    sums[letter] += v
                    ps[letter] += v
    return sums, nrows, by_payer


def roundtrip(tx, tables, batches_by_form, report):
    """Sum of each FIRE payment-amount box == sum of the mapped IRIS column (+ listed unmapped amounts)."""
    lines = []
    ok = True  # False on any real mismatch; unmapped amounts are reported separately
    for form_code, spec in tables["forms"].items():
        form = spec["form"]
        issuers = [i for i in tx["issuers"] if i["A"][25:27].strip() == form_code]
        if not issuers:
            continue
        csv_sums, nrows, csv_by_payer = read_csv_sums(batches_by_form.get(form, []), spec)
        nb = sum(len(i["B"]) for i in issuers)
        if nrows != nb:
            ok = False
        lines.append("%s: %d FIRE B records -> %d CSV rows %s" % (form, nb, nrows, "OK" if nrows == nb else "MISMATCH"))
        for code in AMOUNT_CODES:
            fire_total = sum(i["b_sums"][code] for i in issuers)
            c_total = sum(_c_total(i["C"], code) for i in issuers)
            mapped = spec["amounts"].get(code)
            csv_total = csv_sums[mapped[0]] if mapped else 0
            unm = report.unmapped_amounts.get((form, code), 0)
            if fire_total == 0 and c_total == 0 and csv_total == 0:
                continue
            good = fire_total == c_total == csv_total + unm
            ok = ok and good
            lines.append("  Amount code %s -> %s: FIRE B sum %s, C control total %s, CSV sum %s, unmapped %s  %s"
                         % (code, ("col " + mapped[0]) if mapped else "(no IRIS column)", cents_to_str(fire_total),
                            cents_to_str(c_total), cents_to_str(csv_total), cents_to_str(unm),
                            "OK" if good and unm == 0 else ("NOT CONVERTED" if good else "MISMATCH")))
        # Per payer (issuer TIN, A pos 12-20): the same three-way check, so one payer's shortfall cannot hide
        # behind another payer's surplus. Issuers that share a TIN within a form are reconciled together.
        payers = []
        for i in issuers:
            pt = i["A"][11:20]
            if pt not in payers:
                payers.append(pt)
        for pt in payers:
            mine = [i for i in issuers if i["A"][11:20] == pt]
            ps = csv_by_payer.get("".join(ch for ch in pt if ch.isdigit()), {"rows": 0})
            pnb = sum(len(i["B"]) for i in mine)
            pname = mask_free_text(mine[0]["A"][52:92].strip())
            pok = ps["rows"] == pnb
            plines = []
            for code in AMOUNT_CODES:
                fire_total = sum(i["b_sums"][code] for i in mine)
                c_total = sum(_c_total(i["C"], code) for i in mine)
                mapped = spec["amounts"].get(code)
                csv_total = ps.get(mapped[0], 0) if mapped else 0
                unm = report.unmapped_by_payer.get((form, pt, code), 0)
                if fire_total == 0 and c_total == 0 and csv_total == 0:
                    continue
                good = fire_total == c_total == csv_total + unm
                pok = pok and good
                plines.append("    Amount code %s: FIRE B sum %s, C control total %s, CSV sum %s, unmapped %s  %s"
                              % (code, cents_to_str(fire_total), cents_to_str(c_total), cents_to_str(csv_total),
                                 cents_to_str(unm),
                                 "OK" if good and unm == 0 else ("NOT CONVERTED" if good else "MISMATCH")))
            ok = ok and pok
            lines.append("  Payer %s %s: %d FIRE B records -> %d CSV rows %s"
                         % (mask_tin(pt), pname, pnb, ps["rows"], "OK" if pok else "MISMATCH"))
            lines += plines
    return ok, lines


def file_totals(tx, tables, batches_by_form, report):
    """Per-CSV record count and per-box totals, re-read from each CSV's own text, so a filer uploading the
    files one at a time can check each upload against the portal's summary.

    Asserts that the files add up: per form, the files' records sum to the FIRE B count and each box's file
    totals sum to the FIRE B sum less any listed unmapped amount. Returns (ok, lines); lines are shown under
    ROUND-TRIP. Results go in report.file_totals / report.file_boxes."""
    ok = True
    lines = []
    for form_code, spec in tables["forms"].items():
        form = spec["form"]
        issuers = [i for i in tx["issuers"] if i["A"][25:27].strip() == form_code]
        if not issuers:
            continue
        titles = dict(spec["columns"])
        per_file = []
        for text in batches_by_form.get(form, []):
            sums, nrows, _ = read_csv_sums([text], spec)
            per_file.append((nrows, sums))
        boxes = []
        good = sum(n for n, _ in per_file) == sum(len(i["B"]) for i in issuers)
        for code in AMOUNT_CODES:
            mapped = spec["amounts"].get(code)
            if not mapped:
                continue
            letter = mapped[0]
            want = sum(i["b_sums"][code] for i in issuers) - report.unmapped_amounts.get((form, code), 0)
            got = sum(s[letter] for _, s in per_file)
            if got != want:
                good = False
            if want or got:
                boxes.append((letter, titles[letter].split(" - ")[0]))
        report.file_totals[form] = per_file
        report.file_boxes[form] = boxes
        ok = ok and good
        lines.append("%s: %d CSV file(s); per-file records and box totals add up to the form totals  %s"
                     % (form, len(per_file), "OK" if good else "MISMATCH"))
    return ok, lines


def file_line(name, nrows, sums, boxes):
    """One "Files written" line: the CSV's own record count and per-box totals."""
    parts = ["%s col %s %s" % (label, letter, cents_to_str(sums.get(letter, 0))) for letter, label in boxes]
    return "%s (%d records%s)" % (name, nrows, ("; " + ", ".join(parts)) if parts else "")


def _c_total(c, code):
    i = AMOUNT_CODES.index(code)
    return parse_amount(c[15 + 18 * i:33 + 18 * i], "C record")


def run(data, options):
    """Pure conversion: bytes in -> (batches_by_form, report, roundtrip_lines, exit_code). No I/O."""
    report = Report()
    records, errs = split_records(data)
    if errs:
        raise _refusal("not a valid FIRE file: ", errs)
    if not records:
        raise Refusal("empty file")
    tx, errs = parse_transmission(records)
    if errs:
        raise _refusal("FIRE file structure: ", errs)
    year = tx["T"][1:5]
    tables = TABLES.get(year)
    if tables is None:
        raise Refusal("FIRE payment year %r: no IRIS template in this tool for that tax year (have: %s). "
                      "See SPEC-GAP.md G6." % (year, ", ".join(sorted(TABLES))))
    report.errors.extend(check_controls(tx, tables["fire"], tables["forms"]))
    rows = convert(tx, options, report)
    batches = {}
    for form, frows in rows.items():
        spec = [s for s in tables["forms"].values() if s["form"] == form][0]
        for r in frows:
            for issue in validate_row(r["row"], spec):
                report.validation.append("%s %s (payee TIN %s): %s"
                                         % (form, r["where"], mask_tin(r["row"].get("U", "")),
                                            mask_free_text(issue)))
        batches[form] = render_batches(frows, spec, options.batch_size)
    ok, rt_lines = roundtrip(tx, tables, batches, report)
    fok, f_lines = file_totals(tx, tables, batches, report)
    rt_lines += f_lines
    if not fok:
        report.errors.append("per-file totals do not add up to the form totals (see ROUND-TRIP)")
    if report.unmapped_amounts and not options.allow_unmapped:
        report.errors.append("payment amounts with no IRIS column (listed under UNMAPPED). Converting would drop "
                             "money from the return. Re-run with --allow-unmapped only if you will key those "
                             "amounts by hand.")
    if not ok:
        report.errors.append("round-trip total check failed (see ROUND-TRIP)")
    if report.errors or report.needs_decision:
        code = 2
    elif report.validation:
        code = 1
    else:
        code = 0
    return batches, report, rt_lines, code


def render_report(report, rt_lines, written, code, batch_size):
    out = [DISCLAIMER, "", TCC_WARNING, "", PRIVACY_NOTE, ""]
    status = {0: "CONVERTED", 1: "CONVERTED WITH IRIS FORMAT WARNINGS (fix before upload)",
              2: "REFUSED - no CSVs written"}[code]
    out.append("STATUS: " + status)
    out.append("Batch size: %d records per CSV (Pub 5717 Rev. 9-2026 p.20: up to 250)" % batch_size)
    if written:
        out.append("Files written:")
        out += ["  " + w for w in written]
    for title, items in (("ERRORS", report.errors), ("DECISIONS NEEDED (spec is ambiguous; pass a flag)",
                                                      report.needs_decision),
                         ("DECISIONS APPLIED", report.decisions_applied), ("WARNINGS", report.warnings)):
        if items:
            out += ["", title + ":"] + ["  - " + i for i in items]
    out += ["", "ROUND-TRIP (per form and per payer, each payment-amount box):"] + ["  " + l for l in rt_lines]
    out += ["", "UNMAPPED / NOT CONVERTED FIRE FIELDS WITH DATA (%d):" % len(report.unmapped)]
    if not report.unmapped:
        out.append("  none")
    for (rec, name), e in sorted(report.unmapped.items()):
        out.append("  - %s record, %s [%s]: %d record(s). %s. e.g. %s"
                   % (rec, name, e["cite"], e["count"], e["note"], "; ".join(e["examples"])))
    # Every record is listed (no cap), so nothing that needs fixing is hidden in a long file.
    out += ["", "IRIS FORMAT ISSUES (%d) - values were NOT changed; fix these before upload (the portal may "
            "reject them):" % len(report.validation)]
    out += ["  - " + v for v in report.validation] or ["  none"]
    return "\n".join(out) + "\n"


def render_refusal(e):
    """Report for a file refused before conversion: lists every problem found, not only the first few."""
    out = [DISCLAIMER, "", TCC_WARNING, "", PRIVACY_NOTE, "", "STATUS: REFUSED - no CSVs written"]
    details = e.details or [str(e)]
    out += ["", "ERRORS (%d):" % len(details)] + ["  - " + d for d in details]
    return "\n".join(out) + "\n"


def tables_json():
    """The spec tables as JSON, embedded in index.html so the browser version uses the same tables."""
    import json
    return json.dumps({"TABLES": TABLES, "CFSF_STATE_CODES": CFSF_STATE_CODES, "RETURN_TYPE_NAMES": RETURN_TYPE_NAMES,
                       "DISCLAIMER": DISCLAIMER, "TCC_WARNING": TCC_WARNING, "PRIVACY_NOTE": PRIVACY_NOTE,
                       "VERSION": VERSION}, separators=(",", ":"))


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Convert a FIRE (Pub 1220) .txt file to IRIS Taxpayer Portal CSVs (1099-NEC and 1099-MISC "
                    "only). Local only, no network.", epilog=DISCLAIMER)
    p.add_argument("fire_file")
    p.add_argument("-o", "--out-dir", default="iris-csv-out")
    p.add_argument("--batch-size", type=int, default=250,
                   help="max records per CSV, header not counted (default 250, Pub 5717 Rev. 9-2026 p.20)")
    p.add_argument("--payer-tin-type", choices=("EIN", "SSN"))
    p.add_argument("--tin-type-2", choices=TIN_TYPE_2_CHOICES)
    p.add_argument("--names-as-business", action="store_true")
    p.add_argument("--allow-unmapped", action="store_true",
                   help="write CSVs even when some payment amounts have no IRIS column (they stay listed)")
    p.add_argument("--skip-unsupported", action="store_true",
                   help="convert NEC/MISC issuers and skip other return types (listed) instead of refusing")
    p.add_argument("--version", action="version", version=VERSION)
    o = p.parse_args(argv)
    if o.batch_size < 1 or o.batch_size > 250:
        p.error("--batch-size must be 1-250 (Pub 5717 Rev. 9-2026 p.20 allows up to 250)")
    print(DISCLAIMER, file=sys.stderr)
    print(TCC_WARNING, file=sys.stderr)
    try:
        with open(o.fire_file, "rb") as fh:
            data = fh.read()
    except OSError as e:
        print("\nREFUSED: cannot read input file %r: %s" % (o.fire_file, e.strerror or e), file=sys.stderr)
        return 2
    try:
        batches, report, rt_lines, code = run(data, o)
    except Refusal as e:
        print("\nREFUSED: %s" % e, file=sys.stderr)
        os.makedirs(o.out_dir, exist_ok=True)
        rp = os.path.join(o.out_dir, "conversion-report.txt")
        with open(rp, "w", encoding="utf-8") as fh:
            fh.write(render_refusal(e))
        print("Report: " + rp, file=sys.stderr)
        return 2
    os.makedirs(o.out_dir, exist_ok=True)
    written = []
    if code != 2:
        base = os.path.splitext(os.path.basename(o.fire_file))[0]
        for form, texts in batches.items():
            for i, text in enumerate(texts, 1):
                name = "%s_%s_batch%03d_of_%03d.csv" % (base, form, i, len(texts))
                with open(os.path.join(o.out_dir, name), "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
                nrows, sums = report.file_totals[form][i - 1]
                if nrows != text.count("\r\n") - 1:
                    raise AssertionError("per-file record count disagrees with the CSV text: " + name)
                written.append(file_line(name, nrows, sums, report.file_boxes[form]))
    text = render_report(report, rt_lines, written, code, o.batch_size)
    rp = os.path.join(o.out_dir, "conversion-report.txt")
    with open(rp, "w", encoding="utf-8") as fh:
        fh.write(text)
    print("\n" + text, file=sys.stderr)
    print("Report: " + rp, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
