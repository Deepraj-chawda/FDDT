# Reference cases — re-assessment after the check accuracy fixes

CASE-DE627FA2 (case2.pdf, The Gulf International Private Academy) and CASE-DB43653A (case1.pdf, Headstart Nursery)
were re-assessed on 2026-10-06 with `scripts/reassess_case.py`. Each re-assessment is a **new** row in
`case_risk_assessments`; the earlier assessments, and the reports generated from them, are unchanged.

| | CASE-DE627FA2 | CASE-DB43653A |
|---|---|---|
| Old assessment | `8addbf56…` — **90 / High** | `7eb4e90e…` — **100 / High** (raw 175) |
| Old report | `377efb18…`, `05897c06…` | `d362f6b1…` |
| Re-assessment (2026-10-06) | `dee3e835…` — 60 / High (raw 60) | `36a022ae…` — 100 / High (raw 170) |
| Re-assessment report | `f6cea708-2b41-486b-b2d3-1a85e300f713` (12 pages) | `8dd35ff0-b093-4a6a-8934-a3e6afb990d3` (13 pages) |
| **Current code** | **40 / Medium** (0 without the earlier upload) | **100 / High, raw 150** (110 without the earlier upload) |

The 2026-10-06 re-assessments also included an issuer fee-comparison rule (+20 on each case) that was later
removed: what a payer pays is not a fraud signal, so FDDT judges only a document's internal consistency and
forensics. Those two assessments and their reports are kept as they were (no past case is re-scored); the tables
below give the results of the current code, which `tests/test_golden_reference_cases.py` asserts.

Report PDFs (old and new) are in `sample-documents/tampered_test_samples/<report id>.pdf`.

**How it was re-assessed:** OCR was run again, and every deterministic check was re-run with the current code
(metadata, ELA, copy-move, font, duplicate, line-item reading, field validation, issuer verification). The model
outputs already recorded for each case — the LLM field extraction, the vision review and the signature/stamp
detection — were reused, not requested again, so the comparison shows what the changed checks do on the same
evidence. The vision review was passed through the new region filters.

## CASE-DE627FA2 — The Gulf International Private Academy (KG2, 2023-24, total 57,182.50 AED)

| Rule | Old | Current | Why |
|---|---|---|---|
| duplicate.cross_case_match | Flag 40 (v1) | Flag 40 (v1) | Same file as CASE-CF609781 (`sample21.pdf`). The finding now says the SHA-256 is identical — "the exact same file re-uploaded" — and who submitted that case. |
| metadata.javascript_or_openaction | Flag 10 (v1) | **Pass** (v2) | The OpenAction is `[page 1 /Fit]`, a view setting; no JavaScript/launch/link-out action. |
| metadata.rescan_conflict | — | Pass | No phone-scanner watermark. |
| issuer.not_in_registry | Flag 15 (v1) | **Not checked** (v2) | testcompany's issuer registry has no entries, so there is nothing to check the issuer against (0 points). With a registry entry it would match: the "ACADEMYO" OCR misread is normalised and the Arabic and Latin parts are matched separately. |
| field.tax_rate_mismatch | Flag 15 (v1) | **Pass** (v2) | "Tax applies to {Uniform, Technology Fee} = 850.00: 5% of it is 42.50." |
| field.line_item_arithmetic_mismatch | Skipped | Skipped | Table columns have no quantity × rate to check. |
| field.subtotal_line_item_mismatch | Pass | Pass (v2) | 5 lines (read from the fee table) = 57,140.00 = total 57,182.50 − tax 42.50. |
| field.amount_in_words_mismatch | Skipped | Skipped | No amount in words. |
| field.period_quantity_mismatch | — | Skipped | No billing months listed. |
| field.iban_trn_validation | — | Pass | IBAN AE250030000260337020001: 23 chars, mod-97 valid, bank code 003 = ADCB as printed, contains account 260337020001. TRN 100216382000003: 15 digits, starts 100. |
| font.inconsistency_scanned | Pass | Pass (v2) | — |
| visual.alignment_inconsistency | Flag 10 (v1) | **Pass** (v2) | "Signatures misaligned with the typed baseline": about the detected signatures (covers 50% of one); signatures never follow the baseline. Kept in the review as info. |
| ELA / copy-move / AI-generation / signature presence | Pass | Pass | Unchanged. |
| Cross-case links (note, not scored) | — | CASE-DB43653A, CASE-37B33CBB, CASE-6CB8F7E1, CASE-6CE43E0C, CASE-942C8493, CASE-A7705426, CASE-CF609781 | Same scanning device (PPS - SFO2-A-MFP01, serial 00206BE9FB63) and same parent (Ahmed Salem (Abdulla) AlKindi); CASE-A7705426 / CF609781 also share family number 1245. |
| **Score** | **90 / High** | **40 / Medium** | Duplicate only; 0 without the earlier upload of the same file. |

## CASE-DB43653A — Headstart Nursery (AY 2022-23, total 55,500 AED)

| Rule | Old | Current | Why |
|---|---|---|---|
| duplicate.cross_case_match | Flag 40 (v1) | Flag 40 (v1) | Same file as CASE-37B33CBB (`sample_font20.pdf`); identical SHA-256, submitter shown. |
| metadata.javascript_or_openaction | Flag 10 (v1) | **Pass** (v2) | `[page 1 /Fit]` only. |
| metadata.rescan_conflict | — | **Flag 10** (v1) | "Scanned with CamScanner" on a PDF produced by Develop ineo+ 759 / PPS - SFO2-A-MFP01: printed and re-scanned, so edits made before printing are invisible to ELA. |
| issuer.not_in_registry | Flag 15 (v1) | **Not checked** (v2) | Empty registry: nothing to check against (0 points). |
| field.tax_rate_mismatch | Skipped | Skipped | No tax. |
| field.line_item_arithmetic_mismatch | Flag 25 (v1) | Flag 25 (v1) | Term 3: 3 × 6,000.00 = 18,000.00, printed 16,600.00. |
| field.subtotal_line_item_mismatch | Flag 15 (v1): "33,100 vs 55,500, diff 22,400" | **Flag 15** (v2) | "Line items = 55,100.00, stated total 55,500.00, difference 400.00." Term 1 is now summed (it is DUE for Oct–Dec); the registration fee is listed but not summed (marked PAID). |
| field.amount_in_words_mismatch | Flag 25 (v1) | Flag 25 (v1) | Words 20,500.00 vs figures 55,500.00. |
| field.period_quantity_mismatch | — | **Flag 10** (v1) | Term 1: 3 months listed as due (October, November, December; September marked paid), × 4 charged. |
| field.iban_trn_validation | — | Pass | IBAN AE450030010364749020001: 23 chars, mod-97 valid, bank code 003 = ADCB as printed, contains account 10364749020001. |
| font.inconsistency_scanned | Flag 25 (v1), 8 words | Flag 25 (v2), 9 words | "6,000" now included (OCR listed "Segoe UI, Arial, Helvetica": the grotesque class was second in the list); "4" and "MONTH" kept. |
| visual.alignment_inconsistency | Flag 10 (v1) | **Pass** (v2) | "Student box tilted": the box's text runs at the same angle as the page band beside it (the page is warped by the phone capture before it was printed) — scan skew, not a placed element. |
| visual.sharpness_inconsistency | Flag 10 (v1) | **Pass** (v2) | About the detected stamp/signature; stamp ink and pen strokes are softer than print. |
| ELA / copy-move / AI-generation / signature presence | Pass | Pass | Unchanged. |
| Cross-case links (note, not scored) | — | CASE-DE627FA2 and the five uploads of the same two files | Same scanning device and parent; CASE-37B33CBB / 6CB8F7E1 / 6CE43E0C / 942C8493 also share student ID HSN1278. |
| **Score** | **100 / High** (raw 175) | **100 / High** (raw 150) | Duplicate 40, amount in words 25, Term 3 line item 25, font 25, subtotal 15, rescan 10, period/quantity 10; 110 without the earlier upload of the same file. |
