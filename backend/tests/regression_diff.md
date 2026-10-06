# Regression diff — check accuracy fixes (A1–A5, B1–B3, C1–C3, C5, D)

## How this was produced

`tests/regression/harness.py` replays every check and the built-in risk rules on a fixed input set, once on the
unmodified code (`tests/baseline/`, commit b14bb4c) and once on the current code (`tests/regression_after/`):

- **Corpus:** every distinct file (by SHA-256) among the stored, fully processed documents — 38 files covering all
  575 stored documents, including `sample21.pdf` (CASE-CF609781 = case2.pdf) and `sample_font20.pdf`
  (CASE-37B33CBB = case1.pdf), the Sample6–18 positive-control files, and the Arabic/English test invoices. The
  `upload-tests/` files are upload-rejection fixtures that never reach the pipeline; the other PDFs in
  `tampered_test_samples/` named by a report ID are FDDT case reports, not documents.
- **Inputs, captured once:** the PDF, one Azure Layout OCR result per file, and the stored LLM extraction, vision
  review, signature/stamp detection and duplicate result of a representative document (a real company's copy
  preferred over load-test copies). Both runs replay the same inputs, so the diff contains code changes only — no
  model noise. Determinism was verified: a second baseline run differed in 0 of 38 files.
- **Not replayed:** the issuer LLM fallback (fuzzy matching only, in both runs) and duplicate detection (the stored
  result is passed through; D only changes the finding text, covered by unit tests).
- **Registry state:** each company's issuer registry as it is in the database; `testcompany`'s is empty.

## Verdict: no unexplained changes

| Category (allowed list) | Where | Count |
|---|---|---|
| OpenAction flag gone, the OpenAction is a destination array | case2, sample_font20 (`[page /Fit]`) | 2 |
| Issuer flag → "not checked": no relevant registry entries | every testcompany document that flagged before (its registry is empty) and QA Alpha Trading's (empty registry) | 20 |
| Tax flag gone, explained by a line-item subset | case2: {Uniform, Technology Fee} = 850 | 1 |
| Vision finding not counted: signature/stamp region | case2 alignment (signatures), sample_font20 sharpness (stamp) | 2 |
| Vision finding not counted: matches the page skew | sample_font20 "student box slightly rotated" (−1.70° vs −1.85° just above) | 1 |
| New rule results (C1–C3) | IBAN/TRN on 18 synthetic samples (every one a 24-char AE IBAN failing mod-97, plus TRNs not starting 100 on the Sample16/17 invoices); rescan + period/quantity on sample_font20 | 20 |
| Subtotal flag where the difference exceeds rounding | sample_font20: same flag, now 55,100 vs 55,500 (diff 400) instead of 33,100 vs 55,500 | 1 (wording) |
| B3 font re-check | sample_font20: "6,000" added; the other 8 findings ("4", "MONTH" included) unchanged | 1 |
| JavaScript finding wording | Sample16/17 (real JavaScript): still flagged, now names the action | 6 (text only) |

Every other document change is the three new sub-checks appearing as `skipped`/`pass`.

**One regression was found and fixed during the work:** an early run dropped vision alignment findings on
Sample15_English_Mismatch_Invoice, Sample16_English_Case_Invoice and Sample17_English_AllFlags_Invoice (a retyped
row sitting off the baseline). The skew filter matched them because their text said "misaligned" and digital pages
are at 0°. Fixed in commit "fix(A4): judge only rotation findings by page skew"; all three findings are kept and the
three phrasings are regression tests in `tests/test_visual_region_filters.py`.

## Positive controls

Every rule firing for a real signal in the baseline — JavaScript actions, font edits (text layer and OCR), ELA
regions, copy-move clusters, arithmetic (line items, subtotal, total/tax, amount in words), duplicates, editing
software, modification dates, incremental saves — still fires with the same weight: **80 of 82 kept**. The 2 lost
are the A1 false positives above (view-setting OpenAction on case1/case2).

ELA, copy-move, AI-generation, signature/stamp presence and date-in-future results are identical on all 38 files.

Score decreases, each explained by allowed changes only:

| Document | Before | After | Why |
|---|---|---|---|
| case2.pdf (CASE-DE627FA2) | 90 | 40 | −10 OpenAction, −15 issuer not checked, −15 tax (subset), −10 alignment (signatures) |
| Sample18_English_FontMismatch_Invoice | 61 | 46 | −15 issuer not checked. The font-edit finding (positive control) still scores 40. |
| Sample6 English invoice / evidence | 55 | 40 | −15 issuer not checked (testcompany) |
| Sample7 Arabic invoice / evidence | 55 | 40 | −15 issuer not checked (testcompany) |
| Sample8 English invoice / evidence | 21 | 6 | −15 issuer not checked (QA Alpha Trading) |
| valid.pdf | 83 | 68 | −15 issuer not checked (testcompany) |

Score increases are all C1 IBAN/TRN flags on synthetic samples whose IBANs are genuinely invalid.

---

## Generated per-document diff

Before: `tests/baseline` — after: `tests/regression_after`.

38 document(s) changed, 0 unchanged.

| Document | Cases | Score before | Score after | Rules removed | Rules added |
|---|---|---|---|---|---|
| case2.pdf (`e9c3ea682c74`) | 3 | 90 | 40 | field.tax_rate_mismatch, issuer.not_in_registry, metadata.javascript_or_openaction, visual.alignment_inconsistency | — |
| evidence.pdf (`8aa9dee3b984`) | 18 | 15 | 15 | issuer.not_in_registry | field.iban_trn_validation |
| invoice.pdf (`839b8362704e`) | 17 | 55 | 55 | issuer.not_in_registry | field.iban_trn_validation |
| Sample10_English_Case_Evidence.pdf (`34dbb9df1bca`) | 1 | 40 | 40 | — | — |
| Sample10_English_Case_Invoice.pdf (`96058cadc567`) | 1 | 0 | 0 | — | — |
| Sample11_Arabic_Case_Evidence.pdf (`242574c83124`) | 1 | 0 | 0 | — | — |
| Sample11_Arabic_Case_Invoice.pdf (`5373b29fef82`) | 1 | 65 | 65 | — | — |
| Sample12_English_Case_Evidence.pdf (`94d51aa4f3d7`) | 11 | 100 | 100 | — | field.iban_trn_validation |
| Sample12_English_Case_Invoice.pdf (`bc41a171e784`) | 14 | 100 | 100 | — | field.iban_trn_validation |
| Sample13_Arabic_cp_ela_Evidence.pdf (`16be1bd21ae0`) | 6 | 100 | 100 | issuer.not_in_registry | field.iban_trn_validation |
| Sample13_Arabic_cp_ela_Invoice.pdf (`d440edcb8b6c`) | 5 | 100 | 100 | issuer.not_in_registry | field.iban_trn_validation |
| Sample14_English_Case_Evidence.pdf (`9c7a76cca6f2`) | 4 | 55 | 70 | — | field.iban_trn_validation |
| Sample14_English_Case_Invoice.pdf (`60ba1dd3298a`) | 3 | 40 | 55 | — | field.iban_trn_validation |
| Sample15_Arabic_Case_Evidence.pdf (`e6ac7601cb8d`) | 3 | 55 | 70 | — | field.iban_trn_validation |
| Sample15_Arabic_Case_Invoice.pdf (`4441a05e1d9c`) | 3 | 55 | 70 | — | field.iban_trn_validation |
| Sample15_English_Mismatch_Evidence.pdf (`eb5b436da2bd`) | 1 | 16 | 16 | — | — |
| Sample15_English_Mismatch_Invoice.pdf (`1649dee021e6`) | 1 | 61 | 61 | — | — |
| Sample16_English_Case_Evidence.pdf (`313d2c40901b`) | 2 | 100 | 100 | issuer.not_in_registry | field.iban_trn_validation |
| Sample16_English_Case_Evidence.pdf (`33795008c45b`) | 1 | 97 | 100 | — | field.iban_trn_validation |
| Sample16_English_Case_Invoice.pdf (`674555cf71cc`) | 2 | 100 | 100 | issuer.not_in_registry | field.iban_trn_validation |
| Sample16_English_Case_Invoice.pdf (`e058a63e7c72`) | 1 | 100 | 100 | — | field.iban_trn_validation |
| Sample17_English_AllFlags_Evidence.pdf (`c1092110a513`) | 5 | 100 | 100 | issuer.not_in_registry | field.iban_trn_validation |
| Sample17_English_AllFlags_Invoice.pdf (`d180d0151abe`) | 5 | 100 | 100 | issuer.not_in_registry | field.iban_trn_validation |
| Sample18_English_FontMismatch_Invoice.pdf (`34d1da2c0aab`) | 3 | 61 | 46 | issuer.not_in_registry | — |
| Sample6_English_Case_Evidence.pdf (`40be67ac124d`) | 15 | 55 | 40 | issuer.not_in_registry | — |
| Sample6_English_Case_Invoice.pdf (`7234c70f39ca`) | 18 | 55 | 40 | issuer.not_in_registry | — |
| Sample7_Arabic_Case_Evidence.pdf (`d4305e0d10b1`) | 15 | 55 | 40 | issuer.not_in_registry | — |
| Sample7_Arabic_Case_Invoice.pdf (`c06c5e8200e1`) | 19 | 55 | 40 | issuer.not_in_registry | — |
| Sample8_English_Case_Evidence.pdf (`36aea3d57061`) | 14 | 21 | 6 | issuer.not_in_registry | — |
| Sample8_English_Case_Invoice.pdf (`98675fec0dfd`) | 14 | 21 | 6 | issuer.not_in_registry | — |
| Sample9_Arabic_Case_Evidence.pdf (`42f16b572ae1`) | 13 | 21 | 21 | — | — |
| Sample9_Arabic_Case_Invoice.pdf (`4df3381d567b`) | 12 | 0 | 0 | — | — |
| sample_font20.pdf (`910eb0b646a4`) | 5 | 100 | 100 | issuer.not_in_registry, metadata.javascript_or_openaction, visual.alignment_inconsistency, visual.sharpness_inconsistency | field.period_quantity_mismatch, metadata.rescan_conflict |
| Test_Arabic_Tampered_Invoice.pdf (`3c6030544d85`) | 1 | 87 | 87 | — | — |
| Test_English_Tampered_Invoice.pdf (`ca40d3081340`) | 1 | 87 | 87 | — | — |
| valid.pdf (`82efdc39f20c`) | 4 | 83 | 68 | issuer.not_in_registry | — |
| إيصال.pdf (`1b8931d47d25`) | 15 | 15 | 15 | issuer.not_in_registry | field.iban_trn_validation |
| فاتورة.pdf (`cef1371f4eaa`) | 13 | 15 | 15 | issuer.not_in_registry | field.iban_trn_validation |

## case2.pdf (`e9c3ea682c74`)

Representative case CASE-DE627FA2 (testcompany); cases: CASE-A7705426, CASE-CF609781, CASE-DE627FA2.

Score 90 → 40 (raw 90 → 40).

- rule **removed** `field.tax_rate_mismatch` (−15): 'case2.pdf': Tax 42.50 is not 5% of the total less tax 57,140.00 (expected 2,857.00).
- rule **removed** `issuer.not_in_registry` (−15): Issuer 'أكاديمية الخليج الدولية الخاصة THE GULF INTERNATIONAL PRIVATE ACADEMYO' on 'case2.pdf' did not match any known issuer in the registry.
- rule **removed** `metadata.javascript_or_openaction` (−10): 'case2.pdf' embeds JavaScript or an open-action — unusual for a business document.
- rule **removed** `visual.alignment_inconsistency` (−10): 'case2.pdf': the vision-model review reports misaligned text. Page 1 — Text alignment: The signatures at the bottom are misaligned and have different baselines compared to the typed text around them, notably in the Registration Department and Accounts Department areas (approx. bounding box x:0.1, y:0.8, width:0.4, height:0.15). This is a vision-model judgment, not pixel-level analysis — a probabilistic signal, not a definitive finding.

Check-level changes:
- `field_validation` `result`: **flag** → **pass**
- `field_validation` `sub:iban_trn_validation`: **—** → **pass | IBAN AE250030000260337020001: checksum (mod 97) valid, bank code 003 = ADCB, matching the printed bank name, contains the printed account number 260337020001; TRN 100216382000003: 15 digits, starts with 100.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `field_validation` `sub:tax_rate_consistency`: **flag | Tax 42.50 is not 5% of the total less tax 57,140.00 (expected 2,857.00).** → **pass | Tax applies to {Uniform, Technology Fee} = 850.00: 5% of it is 42.50 (the other line items are not taxed).**
- `issuer_verification` `result`: **flag** → **not_checked**
- `metadata_forensics` `finding:javascript_or_openaction#0`: **medium | This PDF contains an OpenAction and/or embedded JavaScript — unusual for a business document.** → **—**
- `metadata_forensics` `result`: **flag** → **pass**
- `visual_inconsistency_review` `finding:metadata_forensics_correlation#1`: **high | metadata_forensics also flagged this document — independent techniques (file-structure/metadata analysis vs. a vision model's visual read) agreeing is a stronger combined signal than either alone.** → **—**
- `visual_inconsistency_review` `finding:visual_text_alignment#0`: **high | Page 1 — Text alignment: The signatures at the bottom are misaligned and have different baselines compared to the typed text around them, notably in the Registration Department and Accounts Department areas (approx. bounding box x:0.1, y:0.8, width:0.4, height:0.15). This is a vision-model judgment, not pixel-level analysis — a probabilistic signal, not a definitive finding.** → **—**
- `visual_inconsistency_review` `result`: **flag** → **pass**

## evidence.pdf (`8aa9dee3b984`)

Representative case CASE-081A5D03 (testcompany); cases: CASE-081A5D03, CASE-0A9DF515, CASE-1A1C20EE, CASE-1FD06F9D, CASE-240E7414, CASE-25321E95, CASE-2A52737A, CASE-2CD1AE60, CASE-3121FEE1, CASE-4831DD54, CASE-4F6F8BFE, CASE-74E159DB, CASE-ACC0FDB8, CASE-ADFCD2C5, CASE-D1F1BE94, CASE-D24FEB59, CASE-D7C8DCCA, CASE-E3AB20DE.

Score 15 → 15 (raw 15 → 15).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'FIRST ABU DHABI BANK (FAB)' on 'evidence.pdf' did not match any known issuer in the registry.
- rule **added** `field.iban_trn_validation` (+15): 'evidence.pdf': Invalid payment identifier(s): IBAN AE2900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.

Check-level changes:
- `field_validation` `result`: **pass** → **flag**
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE2900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**

## invoice.pdf (`839b8362704e`)

Representative case CASE-081A5D03 (testcompany); cases: CASE-05AAD4C7, CASE-081A5D03, CASE-1FD06F9D, CASE-2AE16040, CASE-33FE90A8, CASE-3ED18F86, CASE-44AECE73, CASE-4831DD54, CASE-55BF92C9, CASE-74E159DB, CASE-76DABAF5, CASE-AAFF956B, CASE-ACC0FDB8, CASE-ADFCD2C5, CASE-D1F1BE94, CASE-D24FEB59, CASE-F3DDDC12.

Score 55 → 55 (raw 55 → 55).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'AL-SUWAIDI NUCLEAR & PRECISION INSTRUMENTATION LLC' on 'invoice.pdf' did not match any known issuer in the registry.
- rule **added** `field.iban_trn_validation` (+15): 'invoice.pdf': Invalid payment identifier(s): IBAN AE2900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.

Check-level changes:
- `field_validation` `result`: **pass** → **flag**
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE2900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**

## Sample10_English_Case_Evidence.pdf (`34dbb9df1bca`)

Representative case CASE-47954182 (Default Company); cases: CASE-47954182.

Score 40 → 40 (raw 40 → 40).


Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**

## Sample10_English_Case_Invoice.pdf (`96058cadc567`)

Representative case CASE-47954182 (Default Company); cases: CASE-47954182.

Score 0 → 0 (raw 0 → 0).


Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**

## Sample11_Arabic_Case_Evidence.pdf (`242574c83124`)

Representative case CASE-0A108B8F (Default Company); cases: CASE-0A108B8F.

Score 0 → 0 (raw 0 → 0).


Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**

## Sample11_Arabic_Case_Invoice.pdf (`5373b29fef82`)

Representative case CASE-0A108B8F (Default Company); cases: CASE-0A108B8F.

Score 65 → 65 (raw 65 → 65).


Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **pass | TRN 100293819400003: 15 digits, starts with 100.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**

## Sample12_English_Case_Evidence.pdf (`94d51aa4f3d7`)

Representative case CASE-3249B03F (Default Company); cases: CASE-0C6F41F9, CASE-1BD2A11D, CASE-3249B03F, CASE-4C085E81, CASE-5F2D3700, CASE-6A2EA2ED, CASE-6C73A607, CASE-6FCE2F6C, CASE-70DA1113, CASE-7EA12803, CASE-B4034CC7.

Score 100 → 100 (raw 160 → 175).

- rule **added** `field.iban_trn_validation` (+15): 'Sample12_English_Case_Evidence.pdf': Invalid payment identifier(s): IBAN AE2900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.

Check-level changes:
- `field_validation` `result`: **pass** → **flag**
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE2900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**

## Sample12_English_Case_Invoice.pdf (`bc41a171e784`)

Representative case CASE-1D925E31 (Default Company); cases: CASE-0319E379, CASE-0C6F41F9, CASE-1BD2A11D, CASE-1CAEC35B, CASE-1D925E31, CASE-3249B03F, CASE-4C085E81, CASE-5F2D3700, CASE-6A2EA2ED, CASE-6C73A607, CASE-6FCE2F6C, CASE-70DA1113, CASE-7EA12803, CASE-B4034CC7.

Score 100 → 100 (raw 145 → 160).

- rule **added** `field.iban_trn_validation` (+15): 'Sample12_English_Case_Invoice.pdf': Invalid payment identifier(s): IBAN AE2900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.

Check-level changes:
- `field_validation` `result`: **pass** → **flag**
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE2900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**

## Sample13_Arabic_cp_ela_Evidence.pdf (`16be1bd21ae0`)

Representative case CASE-C76B35B8 (testcompany); cases: CASE-0861360C, CASE-370CE650, CASE-91206BB0, CASE-C76B35B8, CASE-D768259F, CASE-E4B31A87.

Score 100 → 100 (raw 160 → 160).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'بنك أبوظبي الأول | First Abu Dhabi Bank (FAB)' on 'Sample13_Arabic_cp_ela_Evidence.pdf' did not match any known issuer in the registry.
- rule **added** `field.iban_trn_validation` (+15): 'Sample13_Arabic_cp_ela_Evidence.pdf': Invalid payment identifier(s): IBAN AE4400100000001049182901: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.

Check-level changes:
- `field_validation` `result`: **pass** → **flag**
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE4400100000001049182901: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**

## Sample13_Arabic_cp_ela_Invoice.pdf (`d440edcb8b6c`)

Representative case CASE-C76B35B8 (testcompany); cases: CASE-0861360C, CASE-370CE650, CASE-91206BB0, CASE-C76B35B8, CASE-E4B31A87.

Score 100 → 100 (raw 160 → 160).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'شركة الظفرة للخدمات الهندسية والسلامة النووية والبيئية ذ.م.م' on 'Sample13_Arabic_cp_ela_Invoice.pdf' did not match any known issuer in the registry.
- rule **added** `field.iban_trn_validation` (+15): 'Sample13_Arabic_cp_ela_Invoice.pdf': Invalid payment identifier(s): IBAN AE4400100000001049182901: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.

Check-level changes:
- `field_validation` `result`: **pass** → **flag**
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE4400100000001049182901: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**

## Sample14_English_Case_Evidence.pdf (`9c7a76cca6f2`)

Representative case CASE-B5C72B6D (Default Company); cases: CASE-1D925E31, CASE-3C9A9933, CASE-900B2CF6, CASE-B5C72B6D.

Score 55 → 70 (raw 55 → 70).

- rule **added** `field.iban_trn_validation` (+15): 'Sample14_English_Case_Evidence.pdf': Invalid payment identifier(s): IBAN AE2900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.

Check-level changes:
- `field_validation` `result`: **pass** → **flag**
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE2900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**

## Sample14_English_Case_Invoice.pdf (`60ba1dd3298a`)

Representative case CASE-B5C72B6D (Default Company); cases: CASE-3C9A9933, CASE-900B2CF6, CASE-B5C72B6D.

Score 40 → 55 (raw 40 → 55).

- rule **added** `field.iban_trn_validation` (+15): 'Sample14_English_Case_Invoice.pdf': Invalid payment identifier(s): IBAN AE2900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.

Check-level changes:
- `field_validation` `result`: **pass** → **flag**
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE2900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**

## Sample15_Arabic_Case_Evidence.pdf (`e6ac7601cb8d`)

Representative case CASE-97EAD181 (Default Company); cases: CASE-8D8C00E1, CASE-94B0146F, CASE-97EAD181.

Score 55 → 70 (raw 55 → 70).

- rule **added** `field.iban_trn_validation` (+15): 'Sample15_Arabic_Case_Evidence.pdf': Invalid payment identifier(s): IBAN AE4400100000001049182901: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.

Check-level changes:
- `field_validation` `result`: **pass** → **flag**
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE4400100000001049182901: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**

## Sample15_Arabic_Case_Invoice.pdf (`4441a05e1d9c`)

Representative case CASE-97EAD181 (Default Company); cases: CASE-8D8C00E1, CASE-94B0146F, CASE-97EAD181.

Score 55 → 70 (raw 55 → 70).

- rule **added** `field.iban_trn_validation` (+15): 'Sample15_Arabic_Case_Invoice.pdf': Invalid payment identifier(s): IBAN AE4400100000001049182901: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.

Check-level changes:
- `field_validation` `result`: **pass** → **flag**
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE4400100000001049182901: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**

## Sample15_English_Mismatch_Evidence.pdf (`eb5b436da2bd`)

Representative case CASE-30720157 (Default Company); cases: CASE-30720157.

Score 16 → 16 (raw 16 → 16).


Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**

## Sample15_English_Mismatch_Invoice.pdf (`1649dee021e6`)

Representative case CASE-30720157 (Default Company); cases: CASE-30720157.

Score 61 → 61 (raw 61 → 61).


Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**

## Sample16_English_Case_Evidence.pdf (`313d2c40901b`)

Representative case CASE-86E98F94 (testcompany); cases: CASE-86E98F94, CASE-FF777FB2.

Score 100 → 100 (raw 197 → 197).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'MERIDIAN COMMERCIAL BANK' on 'Sample16_English_Case_Evidence.pdf' did not match any known issuer in the registry.
- rule **added** `field.iban_trn_validation` (+15): 'Sample16_English_Case_Evidence.pdf': Invalid payment identifier(s): IBAN AE9900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.

Check-level changes:
- `field_validation` `result`: **pass** → **flag**
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE9900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**
- `metadata_forensics` `finding:javascript_or_openaction#5`: **medium | This PDF contains an OpenAction and/or embedded JavaScript — unusual for a business document.** → **medium | This PDF contains executable or outward-reaching content — a document-level JavaScript name tree (1 script(s)); JavaScript action in /OpenAction — unusual for a business document.**

## Sample16_English_Case_Evidence.pdf (`33795008c45b`)

Representative case CASE-D63AC666 (Default Company); cases: CASE-D63AC666.

Score 97 → 100 (raw 97 → 112).

- rule **added** `field.iban_trn_validation` (+15): 'Sample16_English_Case_Evidence.pdf': Invalid payment identifier(s): IBAN AE9900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.

Check-level changes:
- `field_validation` `result`: **pass** → **flag**
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE9900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `metadata_forensics` `finding:javascript_or_openaction#5`: **medium | This PDF contains an OpenAction and/or embedded JavaScript — unusual for a business document.** → **medium | This PDF contains executable or outward-reaching content — a document-level JavaScript name tree (1 script(s)); JavaScript action in /OpenAction — unusual for a business document.**

## Sample16_English_Case_Invoice.pdf (`674555cf71cc`)

Representative case CASE-86E98F94 (testcompany); cases: CASE-86E98F94, CASE-FF777FB2.

Score 100 → 100 (raw 291 → 291).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'NORTHGAITE ENGINEERING SOLUTONS LLC' on 'Sample16_English_Case_Invoice.pdf' did not match any known issuer in the registry.
- rule **added** `field.iban_trn_validation` (+15): 'Sample16_English_Case_Invoice.pdf': Invalid payment identifier(s): IBAN AE9900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails | TRN 991001928400003: does not start with 100, as UAE TRNs do | TRN 994012841900003: does not start with 100, as UAE TRNs do.

Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE9900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails | TRN 991001928400003: does not start with 100, as UAE TRNs do | TRN 994012841900003: does not start with 100, as UAE TRNs do.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**
- `metadata_forensics` `finding:javascript_or_openaction#5`: **medium | This PDF contains an OpenAction and/or embedded JavaScript — unusual for a business document.** → **medium | This PDF contains executable or outward-reaching content — a document-level JavaScript name tree (1 script(s)); JavaScript action in /OpenAction — unusual for a business document.**

## Sample16_English_Case_Invoice.pdf (`e058a63e7c72`)

Representative case CASE-D63AC666 (Default Company); cases: CASE-D63AC666.

Score 100 → 100 (raw 157 → 172).

- rule **added** `field.iban_trn_validation` (+15): 'Sample16_English_Case_Invoice.pdf': Invalid payment identifier(s): IBAN AE9900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails | TRN 991001928400003: does not start with 100, as UAE TRNs do | TRN 994012841900003: does not start with 100, as UAE TRNs do.

Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE9900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails | TRN 991001928400003: does not start with 100, as UAE TRNs do | TRN 994012841900003: does not start with 100, as UAE TRNs do.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `metadata_forensics` `finding:javascript_or_openaction#5`: **medium | This PDF contains an OpenAction and/or embedded JavaScript — unusual for a business document.** → **medium | This PDF contains executable or outward-reaching content — a document-level JavaScript name tree (1 script(s)); JavaScript action in /OpenAction — unusual for a business document.**

## Sample17_English_AllFlags_Evidence.pdf (`c1092110a513`)

Representative case CASE-70FDDB1B (testcompany); cases: CASE-70FDDB1B, CASE-A11F243F, CASE-CDB578EF, CASE-E83EE27E, CASE-F9AB6C21.

Score 100 → 100 (raw 242 → 242).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'MERIDIAN COMMERCIAL BANK' on 'Sample17_English_AllFlags_Evidence.pdf' did not match any known issuer in the registry.
- rule **added** `field.iban_trn_validation` (+15): 'Sample17_English_AllFlags_Evidence.pdf': Invalid payment identifier(s): IBAN AE9900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.

Check-level changes:
- `field_validation` `result`: **pass** → **flag**
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE9900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**
- `metadata_forensics` `finding:javascript_or_openaction#8`: **medium | This PDF contains an OpenAction and/or embedded JavaScript — unusual for a business document.** → **medium | This PDF contains executable or outward-reaching content — a document-level JavaScript name tree (1 script(s)); JavaScript action in /OpenAction — unusual for a business document.**

## Sample17_English_AllFlags_Invoice.pdf (`d180d0151abe`)

Representative case CASE-70FDDB1B (testcompany); cases: CASE-70FDDB1B, CASE-A11F243F, CASE-CDB578EF, CASE-E83EE27E, CASE-F9AB6C21.

Score 100 → 100 (raw 336 → 336).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'NORTHGAITE ENGINEERING SOLUTONS LLC' on 'Sample17_English_AllFlags_Invoice.pdf' did not match any known issuer in the registry.
- rule **added** `field.iban_trn_validation` (+15): 'Sample17_English_AllFlags_Invoice.pdf': Invalid payment identifier(s): IBAN AE9900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails | TRN 991001928400003: does not start with 100, as UAE TRNs do | TRN 994012841900003: does not start with 100, as UAE TRNs do.

Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE9900300000010482910482: 24 characters, but AE IBANs have 23; checksum (mod 97) fails | TRN 991001928400003: does not start with 100, as UAE TRNs do | TRN 994012841900003: does not start with 100, as UAE TRNs do.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**
- `metadata_forensics` `finding:javascript_or_openaction#8`: **medium | This PDF contains an OpenAction and/or embedded JavaScript — unusual for a business document.** → **medium | This PDF contains executable or outward-reaching content — a document-level JavaScript name tree (1 script(s)); JavaScript action in /OpenAction — unusual for a business document.**

## Sample18_English_FontMismatch_Invoice.pdf (`34d1da2c0aab`)

Representative case CASE-589ECE66 (testcompany); cases: CASE-2EDE9D85, CASE-589ECE66, CASE-7C24EBB6.

Score 61 → 46 (raw 61 → 46).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'TECHSOURCE SOLUTIONS INC. Enterprise IT Procurement & Corporate Accounts' on 'Sample18_English_FontMismatch_Invoice.pdf' did not match any known issuer in the registry.

Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**

## Sample6_English_Case_Evidence.pdf (`40be67ac124d`)

Representative case CASE-8B6028CA (testcompany); cases: CASE-1FD06F9D, CASE-2C1C7304, CASE-4A072B50, CASE-574B5439, CASE-61637697, CASE-74E159DB, CASE-8AAF3E05, CASE-8B6028CA, CASE-9B9B9220, CASE-AC10D420, CASE-ACC0FDB8, CASE-CE205D2D, CASE-D1F1BE94, CASE-D24FEB59, CASE-EB84476F.

Score 55 → 40 (raw 55 → 40).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'OAKRIDGE INTERNATIONAL ACADEMY' on 'Sample6_English_Case_Evidence.pdf' did not match any known issuer in the registry.

Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**

## Sample6_English_Case_Invoice.pdf (`7234c70f39ca`)

Representative case CASE-8B6028CA (testcompany); cases: CASE-1A2AFD37, CASE-1FD06F9D, CASE-411482CB, CASE-538BCDC0, CASE-574B5439, CASE-726AEDC4, CASE-74E159DB, CASE-77E916DC, CASE-8AAF3E05, CASE-8B6028CA, CASE-AC10D420, CASE-ACC0FDB8, CASE-D1F1BE94, CASE-D24FEB59, CASE-DD1EAD4F, CASE-F1B6653B, CASE-F4B6AB95, CASE-FD2E14E2.

Score 55 → 40 (raw 55 → 40).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'OAKRIDGE INTERNATIONAL ACADEMY' on 'Sample6_English_Case_Invoice.pdf' did not match any known issuer in the registry.

Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**

## Sample7_Arabic_Case_Evidence.pdf (`d4305e0d10b1`)

Representative case CASE-3D126D3A (testcompany); cases: CASE-0EF1FAD5, CASE-1F20A083, CASE-1FD06F9D, CASE-2CBAED37, CASE-3511D44E, CASE-3D126D3A, CASE-609B87E8, CASE-67041F47, CASE-6C792535, CASE-74E159DB, CASE-ACC0FDB8, CASE-CF679EE6, CASE-D1F1BE94, CASE-D24FEB59, CASE-F1B2FE14.

Score 55 → 40 (raw 55 → 40).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'البنك الأهلي السعودي' on 'Sample7_Arabic_Case_Evidence.pdf' did not match any known issuer in the registry.

Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**

## Sample7_Arabic_Case_Invoice.pdf (`c06c5e8200e1`)

Representative case CASE-3D126D3A (testcompany); cases: CASE-11F0CAA6, CASE-1C88EFE7, CASE-1FD06F9D, CASE-2897F393, CASE-30D16008, CASE-3511D44E, CASE-3D126D3A, CASE-4B6F6223, CASE-5A8B6D87, CASE-6C792535, CASE-71A8323E, CASE-74E159DB, CASE-8BDDEB82, CASE-99421D4E, CASE-ACC0FDB8, CASE-CF679EE6, CASE-D1F1BE94, CASE-D24FEB59, CASE-FF8CB078.

Score 55 → 40 (raw 55 → 40).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'فندق وأجنحة قصر الواحة الفاخر (ذ.م.م) قطاع الضيافة وخدمات المؤتمرات ورجال الأعمال - فئة ٥ نجوم طريق الملك فهد، حي الصحافة، الرياض، المملكة العربية السعودية' on 'Sample7_Arabic_Case_Invoice.pdf' did not match any known issuer in the registry.

Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**

## Sample8_English_Case_Evidence.pdf (`36aea3d57061`)

Representative case CASE-8A645E93 (QA Alpha Trading); cases: CASE-1963B797, CASE-1DD6E9B1, CASE-1FD06F9D, CASE-2BEE0A11, CASE-3C2E2429, CASE-74E159DB, CASE-8A645E93, CASE-9F98716A, CASE-A3AD8556, CASE-ACC0FDB8, CASE-D1F1BE94, CASE-D24FEB59, CASE-DFB293D7, CASE-E0EE112A.

Score 21 → 6 (raw 21 → 6).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'GLOBAL MERCHANT NETWORK Corporate Card Settlement & POS Clearing Services' on 'Sample8_English_Case_Evidence.pdf' did not match any known issuer in the registry.

Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**

## Sample8_English_Case_Invoice.pdf (`98675fec0dfd`)

Representative case CASE-8A645E93 (QA Alpha Trading); cases: CASE-135A6D81, CASE-1963B797, CASE-1FD06F9D, CASE-242525DA, CASE-7209C19A, CASE-74E159DB, CASE-8A645E93, CASE-9D294094, CASE-A3AD8556, CASE-A5719CE2, CASE-ACC0FDB8, CASE-C3083696, CASE-D1F1BE94, CASE-D24FEB59.

Score 21 → 6 (raw 21 → 6).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'TECHSOURCE SOLUTIONS INC. Enterprise IT Procurement & Corporate Accounts' on 'Sample8_English_Case_Invoice.pdf' did not match any known issuer in the registry.

Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**

## Sample9_Arabic_Case_Evidence.pdf (`42f16b572ae1`)

Representative case CASE-A4E8ACD1 (Default Company); cases: CASE-1FD06F9D, CASE-46C19894, CASE-60AD8C23, CASE-6BBA52DE, CASE-74E159DB, CASE-7C39E3E0, CASE-9053DEF2, CASE-A4E8ACD1, CASE-ACC0FDB8, CASE-D1F1BE94, CASE-D24FEB59, CASE-DD6D8831, CASE-E96FC740.

Score 21 → 21 (raw 21 → 21).


Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**

## Sample9_Arabic_Case_Invoice.pdf (`4df3381d567b`)

Representative case CASE-D768259F (Default Company); cases: CASE-0FC5EB2A, CASE-1224A817, CASE-1FD06F9D, CASE-5099BE34, CASE-70B666D0, CASE-74E159DB, CASE-ACC0FDB8, CASE-C7F2C2C7, CASE-D1F1BE94, CASE-D24FEB59, CASE-D768259F, CASE-F1495539.

Score 0 → 0 (raw 0 → 0).


Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**

## sample_font20.pdf (`910eb0b646a4`)

Representative case CASE-6CE43E0C (testcompany); cases: CASE-37B33CBB, CASE-6CB8F7E1, CASE-6CE43E0C, CASE-942C8493, CASE-DB43653A.

Score 100 → 100 (raw 175 → 150).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'HEADSTART NURSERY' on 'sample_font20.pdf' did not match any known issuer in the registry.
- rule **removed** `metadata.javascript_or_openaction` (−10): 'sample_font20.pdf' embeds JavaScript or an open-action — unusual for a business document.
- rule **removed** `visual.alignment_inconsistency` (−10): 'sample_font20.pdf': the vision-model review reports misaligned text. Page 1 — Text alignment: The text in the boxed section with student name and ID is slightly rotated and not aligned with the rest of the text on the page, which is horizontally aligned consistently. This is a vision-model judgment, not pixel-level analysis — a probabilistic signal, not a definitive finding.
- rule **removed** `visual.sharpness_inconsistency` (−10): 'sample_font20.pdf': the vision-model review reports a region with different sharpness. Page 1 — Resolution / sharpness consistency: The signature and stamp near the bottom center appear slightly blurrier and less sharp compared to the rest of the text, which is crisp and clear. This is a vision-model judgment, not pixel-level analysis — a probabilistic signal, not a definitive finding.
- rule **added** `field.period_quantity_mismatch` (+10): 'sample_font20.pdf': line 2 (FEE FOR TERM 1 (September) PAID (Octobe…): 3 months listed as due (October, November, December; September marked paid), × 4 charged.
- rule **added** `metadata.rescan_conflict` (+10): 'sample_font20.pdf': Printed and re-scanned: the page carries a phone-scanner watermark ('Scanned with CamScanner') but the PDF was produced by an office scanner (Develop ineo+ 759 / PPS - SFO2-A-MFP01). Digital edits made before printing would not be visible to error level analysis.

Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **pass | IBAN AE450030010364749020001: checksum (mod 97) valid, bank code 003 = ADCB, matching the printed bank name, contains the printed account number 10364749020001.**
- `field_validation` `sub:line_item_arithmetic`: **flag | 1 line item(s) do not add up: line 4 (FEE FOR TERM 3 (April-May-June)): 3 × 6,000.00 = 18,000.00, but the line total printed is 16,600.00.** → **flag | 1 line item(s) do not add up: line 4 (FEE FOR TERM 3 (April-May-June) DUE): 3 × 6,000.00 = 18,000.00, but the line total printed is 16,600.00.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **flag | line 2 (FEE FOR TERM 1 (September) PAID (Octobe…): 3 months listed as due (October, November, December; September marked paid), × 4 charged.**
- `field_validation` `sub:rescan_conflict`: **—** → **flag | Printed and re-scanned: the page carries a phone-scanner watermark ('Scanned with CamScanner') but the PDF was produced by an office scanner (Develop ineo+ 759 / PPS - SFO2-A-MFP01). Digital edits made before printing would not be visible to error level analysis.**
- `field_validation` `sub:subtotal_line_item_consistency`: **flag | The line items add up to 33,100.00 (16,500.00 + 16,600.00), but the document states the total 55,500.00 — a difference of 22,400.00.** → **flag | Line items = 55,100.00, stated total 55,500.00, difference 400.00 (summed: 22,000.00 + 16,500.00 + 16,600.00). Not summed: line 1 (REGISTRATION FEES): 500.00 (marked paid).**
- `font_consistency` `finding:font_inconsistency#8`: **medium | Page 1 (scanned): '55,500' looks like a different font (grotesque sans) from the text around it (humanist sans), per OCR font recognition — it may have been typed in or pasted before the page was printed or scanned.** → **medium | Page 1 (scanned): '6,000' looks like a different font (grotesque sans) from the text around it (humanist sans), per OCR font recognition — it may have been typed in or pasted before the page was printed or scanned. Found on a second look at this page's numbers: OCR lists grotesque sans among the likely fonts for it (Segoe UI, Arial, Helvetica, sans-serif), as for the other amounts flagged here.**
- `font_consistency` `finding:font_inconsistency#9`: **—** → **medium | Page 1 (scanned): '55,500' looks like a different font (grotesque sans) from the text around it (humanist sans), per OCR font recognition — it may have been typed in or pasted before the page was printed or scanned.**
- `issuer_verification` `result`: **flag** → **not_checked**
- `metadata_forensics` `finding:javascript_or_openaction#0`: **medium | This PDF contains an OpenAction and/or embedded JavaScript — unusual for a business document.** → **—**
- `metadata_forensics` `result`: **flag** → **pass**
- `visual_inconsistency_review` `finding:visual_resolution_sharpness_consistency#1`: **high | Page 1 — Resolution / sharpness consistency: The signature and stamp near the bottom center appear slightly blurrier and less sharp compared to the rest of the text, which is crisp and clear. This is a vision-model judgment, not pixel-level analysis — a probabilistic signal, not a definitive finding.** → **—**
- `visual_inconsistency_review` `finding:visual_text_alignment#0`: **high | Page 1 — Text alignment: The text in the boxed section with student name and ID is slightly rotated and not aligned with the rest of the text on the page, which is horizontally aligned consistently. This is a vision-model judgment, not pixel-level analysis — a probabilistic signal, not a definitive finding.** → **—**
- `visual_inconsistency_review` `result`: **flag** → **pass**

## Test_Arabic_Tampered_Invoice.pdf (`3c6030544d85`)

Representative case CASE-78B37E39 (Default Company); cases: CASE-78B37E39.

Score 87 → 87 (raw 87 → 87).


Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **skipped | PDF producer information not recorded for this document.**

## Test_English_Tampered_Invoice.pdf (`ca40d3081340`)

Representative case CASE-BE0DC4A1 (Default Company); cases: CASE-BE0DC4A1.

Score 87 → 87 (raw 87 → 87).


Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **skipped | PDF producer information not recorded for this document.**

## valid.pdf (`82efdc39f20c`)

Representative case CASE-0B726DE7 (testcompany); cases: CASE-0B726DE7, CASE-C38287CD, CASE-D7C8DCCA, CASE-DFFD48FF.

Score 83 → 68 (raw 83 → 68).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'Upload test' on 'valid.pdf' did not match any known issuer in the registry.

Check-level changes:
- `field_validation` `sub:iban_trn_validation`: **—** → **skipped | No IBAN or UAE TRN on the document.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **skipped | PDF producer information not recorded for this document.**
- `issuer_verification` `result`: **flag** → **not_checked**

## إيصال.pdf (`1b8931d47d25`)

Representative case CASE-6573A030 (testcompany); cases: CASE-047F0E67, CASE-0B78C29C, CASE-0CAB55E0, CASE-1FD06F9D, CASE-290A1E3D, CASE-3C2ADEBB, CASE-6573A030, CASE-74E159DB, CASE-938C3870, CASE-ACC0FDB8, CASE-AEE6C99D, CASE-D1F1BE94, CASE-D24FEB59, CASE-ED0D638F, CASE-F88C88FC.

Score 15 → 15 (raw 15 → 15).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'بنك أبوظبي الأول | First Abu Dhabi Bank (FAB)' on 'إيصال.pdf' did not match any known issuer in the registry.
- rule **added** `field.iban_trn_validation` (+15): 'إيصال.pdf': Invalid payment identifier(s): IBAN AE4400100000001049182901: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.

Check-level changes:
- `field_validation` `result`: **pass** → **flag**
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE4400100000001049182901: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**

## فاتورة.pdf (`cef1371f4eaa`)

Representative case CASE-6573A030 (testcompany); cases: CASE-1FD06F9D, CASE-2C6DDCB7, CASE-46C19894, CASE-622804E7, CASE-6573A030, CASE-74E159DB, CASE-7A08201F, CASE-888F7C89, CASE-ACC0FDB8, CASE-BC06B819, CASE-D1F1BE94, CASE-D24FEB59, CASE-DCBA5CC6.

Score 15 → 15 (raw 15 → 15).

- rule **removed** `issuer.not_in_registry` (−15): Issuer 'شركة الظفرة للخدمات الهندسية والسلامة النووية والبيئية ذ.م.م' on 'فاتورة.pdf' did not match any known issuer in the registry.
- rule **added** `field.iban_trn_validation` (+15): 'فاتورة.pdf': Invalid payment identifier(s): IBAN AE4400100000001049182901: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.

Check-level changes:
- `field_validation` `result`: **pass** → **flag**
- `field_validation` `sub:iban_trn_validation`: **—** → **flag | Invalid payment identifier(s): IBAN AE4400100000001049182901: 24 characters, but AE IBANs have 23; checksum (mod 97) fails.**
- `field_validation` `sub:period_quantity_consistency`: **—** → **skipped | No line lists its billing months and a month multiplier.**
- `field_validation` `sub:rescan_conflict`: **—** → **pass | No phone-scanner watermark on the page.**
- `issuer_verification` `result`: **flag** → **not_checked**

