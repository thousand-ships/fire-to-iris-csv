# fire-to-iris-csv

Converts an IRS FIRE file (the fixed-width `.txt` described in IRS Publication 1220) into CSV files you can upload to the IRIS Taxpayer Portal. It handles **1099-NEC and 1099-MISC, tax year 2025**, and refuses everything else.

FIRE stops taking files on 19 November 2026. If your accounting system can only produce a FIRE file (for example Acumatica before 2026 R2, or the "IRS 1099 FIRE" report in on-premises Business Central), this tool turns it into IRIS CSVs on your own machine.

**Not affiliated with or endorsed by the IRS**, or by any accounting software vendor.
**Not yet tested against a real IRIS portal upload** (see "What hasn't been tested" below).
**Built and maintained by AI agents.** Free, MIT licence.

If you'd rather not use a tool, `guide.html` in this repo walks through doing it by hand with the portal's own CSV template.

- **Private.** One Python file using only the standard library, or one HTML page that runs in your browser. Neither makes network calls; the page's Content-Security-Policy blocks all connections. Full TINs appear only in the CSV files. The report and the terminal output mask them, including any run of 9 or more digits in a free-text sample.
- **Amounts are never altered or dropped silently.** Each payment box is totalled three ways: the FIRE B records, the FIRE C record control totals, and the CSVs as re-read from text. This is checked per form and again per payer. If any of the three differ, the tool refuses. A FIRE amount with no IRIS column is listed in the report and blocks the conversion unless you pass `--allow-unmapped`.
- **Every record that can't map cleanly is listed.** `conversion-report.txt` names each problem record by its position and sequence number: wrong record lengths, corrected returns, foreign addresses, unsupported form types and blank payee TINs. It's written even when the file is refused.
- **Batches.** Output is split into CSVs of at most 250 records each, not counting the header (IRS Pub 5717 Rev. 9-2026, p.20). Change this with `--batch-size`; use `--batch-size 100` if the portal asks for smaller files.

## Disclaimer

This is **not tax advice** and not an IRS product. It is not affiliated with or endorsed by the IRS. The CSVs are a draft converted from your file, and you are responsible for what you file. Check every row in the IRIS portal before you submit; the portal validates uploads and may reject rows. MIT licence, no warranty.

## TCC warning

To upload CSVs to the IRIS Taxpayer Portal you need **your own IRIS TCC** (Transmitter Control Code). A FIRE TCC does not work in IRIS, and approval of a new IRIS TCC can take up to 45 days. If you don't have an IRIS TCC, your options are a provider that files under its own TCC, or applying now (IRS Publication 5903).

## Use: command line (Python 3.8+)

```
python3 fire_to_iris.py YOUR_FIRE_FILE.txt -o iris-out \
    --payer-tin-type EIN --tin-type-2 SSN --names-as-business
```

The output in `iris-out/` is `YOUR_FIRE_FILE_1099-NEC_batch001_of_00N.csv` (and the same for 1099-MISC), plus `conversion-report.txt`.

Exit codes: `0` converted; `1` converted, but some values break IRIS format rules or a payee TIN is blank (listed in the report, values left unchanged); `2` refused, no CSVs written. The report is written in all three cases.

### The three required flags

Pub 1220 doesn't record three things IRIS needs. The tool won't guess them, so you pass them:

| Flag | Why |
|---|---|
| `--payer-tin-type EIN\|SSN` | IRIS needs a payer TIN type, but the FIRE A record holds only the 9 digits. Applies to every payer in the file. |
| `--tin-type-2 SSN\|ITIN\|ATIN` | FIRE's TIN type `2` covers SSN, ITIN and ATIN alike, but IRIS needs to know which. Applies to every type-2 payee. Only asked for when the file has type-2 payees. |
| `--names-as-business` | FIRE stores each name as one free-text line, with no business/individual flag. With this flag, every name goes into IRIS "Business or Entity Name Line 1/2" with Name Type `B`. |

Other options:
- `--batch-size N`: records per CSV, 1-250 (default 250).
- `--allow-unmapped`: write CSVs even when some amounts have no IRIS column. They stay listed, and you key them by hand.
- `--skip-unsupported`: convert the NEC/MISC payers and skip other form types (they are listed), instead of refusing the whole file.

## Use: browser

Open `index.html` from disk in any modern browser. No server and no internet are needed. Pick the file, set the same three choices, then download the CSVs and the report. The page runs the same logic and tables as the command-line tool.

## What it refuses (by design)

- Form types other than 1099-NEC and 1099-MISC (the message names the form, for example "1099-INT").
- Tax years other than 2025. The IRS hasn't published the tax-year-2026 IRIS templates yet; adding them is a table change.
- Corrected returns, foreign addresses and transfer-agent issuers.
- Negative or non-numeric amounts.
- Files with bad record lengths, sequence errors, or control totals that don't match the B records.
- A payer coded for the Combined Federal/State Filing Program without a K record for each state its B records use.
- Files that are not plain ASCII (Pub 1220 p.37). The message gives the byte offset, record and position.

Names, addresses and account numbers that start with `=`, `+`, `-` or `@` are written unchanged but flagged (exit 1), because a spreadsheet may read them as formulas.

## Gap codes in the report

Where the IRS documents don't say how a FIRE field maps to IRIS, the tool doesn't guess. The report cites these codes:

- **G1**: 1099-MISC amount code E ("Section 409A income") has no matching IRIS column. Listed, not converted. Code G (prior-year NEC) is also not converted.
- **G2**: payer TIN type, the SSN/ITIN/ATIN choice and business vs individual names are file-wide flags (above). No per-payee overrides.
- **G3**: Special Data Entries, and state or local withholding on records without a Combined Federal/State code, are listed rather than converted (IRIS needs a state for them).
- **G4 / G5**: foreign addresses and transfer agents are refused.
- **G6**: only tax year 2025 has tables.
- **G7**: header text, UTF-8, CRLF and quoting follow the IRS guideline PDFs, not the template file inside the portal (which needs a login).
- **G8**: corrected returns are refused.
- **G9**: a phone extension beyond 10 digits is listed as unmapped.
- **G10**: 1099-NEC Box 3 is always empty (a TY2025 FIRE file can't carry it).
- **G11**: one IRIS row per FIRE B record, so a payee split across two states stays as two rows.

## Spec sources

The converter uses only these IRS documents. Code comments cite their page numbers and IRIS column letters.
- Pub 1220 Rev. 5-2026 (TY2025): https://www.irs.gov/pub/irs-pdf/p1220.pdf
- IRIS CSV template formatting guidelines, TY2025, 1099-NEC (columns A-BB) and 1099-MISC (columns A-BL): https://www.irs.gov/pub/irs-efile/ty2025-template-formatting-guidelines.zip
- Pub 5717 Rev. 9-2026, p.20 (250 records per CSV): https://www.irs.gov/pub/irs-pdf/p5717.pdf

All year-specific layout lives in the `TABLES` dict in `fire_to_iris.py`; `index.html` embeds the same tables.

## What hasn't been tested

- **No real IRIS upload.** Nobody has uploaded these CSVs to the IRIS Taxpayer Portal yet, so the portal could still reject them. The header text is copied from the IRS guideline PDFs, not from the template you download inside the portal.
- **No real FIRE file.** Every test file was synthetic, built from the Pub 1220 layout. An export from your accounting system might use fields in ways the test files don't.
- **No tax year 2026.** The new 2026 boxes (NEC 1b-1d, MISC 13a/13b/14) aren't covered.

If you try it on a real file, an issue saying what happened (no TINs or names, please) helps more than anything else.
