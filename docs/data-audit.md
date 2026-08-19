# Data Audit — Contact Research tab

**Source:** `data/input/contact-research-2026-08-12.csv` (export of the [Courtesy Email Spreadsheet](https://docs.google.com/spreadsheets/d/1mJG-_-mx6Ngf9BvtCfE19iqIKZk8ru1pEfw3tLY5Znc/), tab gid `1639410838`, exported 2026-08-12).
Sheet owner: `matthew@taulersmith.com`. The sheet is private — API 401s without auth; readable via the Google Drive connector in this Claude session; programmatic write-back needs a service account (see SPEC §4).

## Structure

- Row 1: formula row; Row 2: header. Columns:
  `Trello Card | Plaintiff First | Plaintiff Last | Defendant | Email ("Anything Legal, CEO, CFO, COO, Privacy") | Demand Letter Date | Website | Notes | Status | Completed | Trello Status`
- `Email` and `Website` are **vlookup formulas** pulling from a `Trello` tab (`Trello!C:AA`, cols 21/22/24/25) — write-back must not clobber formula columns (SPEC: separate Enriched tab).
- The spreadsheet also contains at least an **MJS Launch** tab (send tracking: Respond Date, Status=Sent, Sent Date, Sender) downstream of this tab.
- Key candidates: **Trello card URL** (unique, stable — chosen) and defendant name (also unique today).

## Counts (as of this export)

| Metric | Count |
|---|---|
| Logical data rows | 639 (550 real + **89 empty placeholder rows** with `Trello Status = "List Name"` — formulas only, ignore) |
| Real defendant rows (all have website) | **550** |
| Rows WITH emails | **287** — status: 152 Ready to Send, 135 Pending Review |
| **Work queue (defendant + website, no emails)** | **263** — 260 Pending Review, 3 Emails Did Not Work |
| Emails present | ~1,223 total; ~98% person-style, ~2% generic (`privacy@`, `legal@`, …) |
| Emails per completed row | mode 7 (range 1–10) |
| Completed-by | Jakob 172 · James 8 · Rob 3 (manual researchers) |
| Rows with size/revenue notes | 10 ("Less than $10 Million" etc.) |

## Quirks the pipeline must handle (with examples from the data)

1. **Corporate email domain ≠ brand website domain**
   - USRx LLC → site `urbanskinrx.com`, emails `@axnygroup.com`
   - Pharmavite LLC → site `uqora.com` (brand), emails `@uqora.com` not parent
   - Oxford Industries → site `jackrogersusa.com`, emails `@jackrogersusa.com` (brand of public parent)
   - Sightline Media → site `armytimes.com`, emails `@sightlinemediagroup.com`
   - Therabody → emails `@therabodycorp.com` vs site `therabody.com`
2. **Parent companies / acquisitions** (from Notes): "Fermented Sciences II… owned by Juneshine, Inc.", "Sonder was acquired in July 2026 by UpN…" — entity resolution matters and data can go stale.
3. **No corporate email**: "Gmail addresses only" (Off The Hook YS Inc.) — flag, don't guess.
4. **Bounce feedback**: 3 rows "Emails Did Not Work" with the failed addresses in Notes (aimmedia.com, makeupbymario.com, teslarati.com) — these become per-domain blocklists + re-runs.
5. **Size screening in Notes**: "Less than $10 Million", "Under $4 Million" — the team already researches size; a `size_flag` column from provider firmographics automates it.
6. **Non-company defendants**: forums ("BackYard Chickens — This seems like an online forum"), possible wrong-defendant rows ("bhg.com is Better Homes…") — pipeline should surface anomalies, not force answers.
7. **Naming-convention variety across the 287 done rows** — the free training set: `{first}@` dominant at small DTC brands; `{first}.{last}@`, `{f}{last}@`, `{first}{l}@` all present. One typo caught in existing data (`wendy@rarebe**ty.com (typo in source)`) — verification would have flagged it.

## Implications baked into the SPEC

- Learn per-domain patterns from the 287 completed rows before spending any credits (SPEC §3 Stage 1/3, §7 back-test).
- Stage 1 must resolve entity/parent/email-domain before any person search.
- Verification is mandatory; catch-all guesses are tiered, never "verified".
- Anomalies (forum, wrong defendant, Gmail-only) get status flags for human review instead of fabricated contacts.
