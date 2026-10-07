# Regression diff

Before: `tests/regression_before_ghost` — after: `tests/regression_after_ghost`.

50 document(s) changed, 0 unchanged.

| Document | Cases | Score before | Score after | Rules removed | Rules added |
|---|---|---|---|---|---|
| case10School  educational document.pdf (`6f2775326148`) | 1 | 15 | 100 | — | content.deleted_ghost_block, content.replaced_ghost_line, font.inconsistency, metadata.editable_text_over_scan, metadata.producer_scrubbed |
| case11School  educational document.pdf (`624ebf7b6992`) | 1 | 48 | 48 | — | — |
| case12School  educational document.pdf (`5ba396aed871`) | 1 | 37 | 37 | — | — |
| case14School  educational document.pdf (`bd91d951965e`) | 2 | 65 | 65 | — | — |
| case2.pdf (`e9c3ea682c74`) | 3 | 40 | 40 | — | — |
| case3School  educational document.pdf (`0fe36ed31d2e`) | 1 | 40 | 40 | — | — |
| case4School  educational document.pdf (`3aeb1d125f98`) | 1 | 80 | 80 | — | — |
| case5School  educational document.pdf (`895a259e007d`) | 7 | 100 | 100 | — | — |
| case7School  educational document.pdf (`c1a11cef261c`) | 3 | 96 | 96 | — | — |
| case8School  educational document.pdf (`14926b7fca21`) | 1 | 50 | 50 | — | — |
| case9 School  educational document.pdf (`6e31ba79c300`) | 1 | 48 | 48 | — | — |
| caseimage1.pdf (`1d2ff2211bce`) | 1 | 59 | 59 | — | — |
| caseimage2.pdf (`713a033844ef`) | 1 | 50 | 50 | — | — |
| evidence.pdf (`8aa9dee3b984`) | 18 | 15 | 15 | — | — |
| invoice.pdf (`839b8362704e`) | 17 | 55 | 55 | — | — |
| Sample10_English_Case_Evidence.pdf (`34dbb9df1bca`) | 1 | 40 | 40 | — | — |
| Sample10_English_Case_Invoice.pdf (`96058cadc567`) | 1 | 0 | 0 | — | — |
| Sample11_Arabic_Case_Evidence.pdf (`242574c83124`) | 1 | 0 | 0 | — | — |
| Sample11_Arabic_Case_Invoice.pdf (`5373b29fef82`) | 1 | 60 | 60 | — | — |
| Sample12_English_Case_Evidence.pdf (`94d51aa4f3d7`) | 11 | 100 | 100 | — | — |
| Sample12_English_Case_Invoice.pdf (`bc41a171e784`) | 14 | 100 | 100 | — | — |
| Sample13_Arabic_cp_ela_Evidence.pdf (`16be1bd21ae0`) | 6 | 100 | 100 | — | — |
| Sample13_Arabic_cp_ela_Invoice.pdf (`d440edcb8b6c`) | 5 | 100 | 100 | — | — |
| Sample14_English_Case_Evidence.pdf (`9c7a76cca6f2`) | 4 | 70 | 70 | — | — |
| Sample14_English_Case_Invoice.pdf (`60ba1dd3298a`) | 3 | 55 | 55 | — | — |
| Sample15_Arabic_Case_Evidence.pdf (`e6ac7601cb8d`) | 3 | 70 | 70 | — | — |
| Sample15_Arabic_Case_Invoice.pdf (`4441a05e1d9c`) | 3 | 70 | 70 | — | — |
| Sample15_English_Mismatch_Evidence.pdf (`eb5b436da2bd`) | 1 | 16 | 16 | — | — |
| Sample15_English_Mismatch_Invoice.pdf (`1649dee021e6`) | 1 | 61 | 61 | — | — |
| Sample16_English_Case_Evidence.pdf (`313d2c40901b`) | 2 | 100 | 100 | — | — |
| Sample16_English_Case_Evidence.pdf (`33795008c45b`) | 1 | 70 | 70 | — | — |
| Sample16_English_Case_Invoice.pdf (`674555cf71cc`) | 2 | 100 | 100 | — | — |
| Sample16_English_Case_Invoice.pdf (`e058a63e7c72`) | 1 | 100 | 100 | — | — |
| Sample17_English_AllFlags_Evidence.pdf (`c1092110a513`) | 5 | 100 | 100 | — | — |
| Sample17_English_AllFlags_Invoice.pdf (`d180d0151abe`) | 5 | 100 | 100 | — | — |
| Sample18_English_FontMismatch_Invoice.pdf (`34d1da2c0aab`) | 3 | 46 | 46 | — | — |
| Sample6_English_Case_Evidence.pdf (`40be67ac124d`) | 15 | 40 | 40 | — | — |
| Sample6_English_Case_Invoice.pdf (`7234c70f39ca`) | 18 | 40 | 40 | — | — |
| Sample7_Arabic_Case_Evidence.pdf (`d4305e0d10b1`) | 15 | 40 | 40 | — | — |
| Sample7_Arabic_Case_Invoice.pdf (`c06c5e8200e1`) | 19 | 40 | 40 | — | — |
| Sample8_English_Case_Evidence.pdf (`36aea3d57061`) | 14 | 6 | 6 | — | — |
| Sample8_English_Case_Invoice.pdf (`98675fec0dfd`) | 14 | 6 | 6 | — | — |
| Sample9_Arabic_Case_Evidence.pdf (`42f16b572ae1`) | 13 | 21 | 21 | — | — |
| Sample9_Arabic_Case_Invoice.pdf (`4df3381d567b`) | 12 | 0 | 0 | — | — |
| sample_font20.pdf (`910eb0b646a4`) | 5 | 100 | 100 | — | — |
| Test_Arabic_Tampered_Invoice.pdf (`3c6030544d85`) | 1 | 87 | 87 | — | — |
| Test_English_Tampered_Invoice.pdf (`ca40d3081340`) | 1 | 87 | 87 | — | — |
| valid.pdf (`82efdc39f20c`) | 4 | 68 | 68 | — | — |
| إيصال.pdf (`1b8931d47d25`) | 15 | 15 | 15 | — | — |
| فاتورة.pdf (`cef1371f4eaa`) | 13 | 15 | 15 | — | — |

## case10School  educational document.pdf (`6f2775326148`)

Representative case CASE-39CB18BF (testcompany); cases: CASE-39CB18BF.

Score 15 → 100 (raw 15.0 → 110.0).

- rule **added** `content.deleted_ghost_block` (+25): 'case10School  educational document.pdf': 1 block(s) of text were deleted after the scan was converted to editable text — their faint traces remain in the scanned background. Page 1: a block of 6 line(s) of faint text remains in the scanned background (x 30–270 pt, y 388–480 pt from the top) with no live text on top of it: content that was on the page when it was converted to editable text, and was deleted afterwards.
- rule **added** `content.replaced_ghost_line` (+10): 'case10School  educational document.pdf': 1 line(s) were shortened or rewritten after the scan was converted to editable text — the trace of the original runs on past the text. Page 1: the faint trace of the original text in the scan runs on 90 pt past the live text 'Registration Fees' on its line (x 155–196 pt, y 219–227 pt from the top). That line was longer when the scan was converted to editable text, and has been shortened or rewritten since.
- rule **added** `font.inconsistency` (+40): 'case10School  educational document.pdf': 17 piece(s) of text are set in a different font, at a different size, or in a second copy of the same font, from the text around them — a sign they were typed in after the document was produced. Page 1: 'AL REEM' is set in a second embedded copy of TimesNewRoman,Bold (subset MONKPZ; the page's main copy is ZEDDUK) — 'SALEH MOHAMED ALHARTHI' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.
- rule **added** `metadata.editable_text_over_scan` (+10): 'case10School  educational document.pdf' is a scan converted to editable text — its words and numbers can be retyped or deleted like a word-processor file. Page(s) 1: a page-sized scan image with visible, editable text drawn on top of it (in ordinary subset fonts, as Acrobat's own editor or a re-saved converted file leaves them) — a scan converted to editable text. A normal searchable scan keeps its OCR text invisible; this structure is what lets a scan's words and numbers be retyped or deleted.
- rule **added** `metadata.producer_scrubbed` (+10): 'case10School  educational document.pdf': the names of the tools that wrote it were removed from its metadata. The XMP metadata was written by Adobe's own library (Adobe XMP Core 9.1-c001 79.2a0d8d9), yet Producer, Creator, CreatorTool and the edit history are all missing. Adobe software always records its name and saves there, so they were removed afterwards — hiding which tool last changed the file.

Check-level changes:
- `font_consistency` `finding:font_subset_split#0`: **—** → **high | Page 1: 'AL REEM' is set in a second embedded copy of TimesNewRoman,Bold (subset MONKPZ; the page's main copy is ZEDDUK) — 'SALEH MOHAMED ALHARTHI' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#1`: **—** → **high | Page 1: '2820' is set in a second embedded copy of TimesNewRoman,Bold (subset MONKPZ; the page's main copy is ZEDDUK) — 'SALEH MOHAMED ALHARTHI' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#10`: **—** → **high | Page 1: '13,350.00' is set in a second embedded copy of TimesNewRoman (subset JIXQHL; the page's main copy is WFMPEQ) — this copy holds only the characters , . 0 1 2 3 4 5 6; the same column's '4,575.00' is set in the main copy; 'Term 2 Fees' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#11`: **—** → **high | Page 1: '13,350.00' is set in a second embedded copy of TimesNewRoman (subset JIXQHL; the page's main copy is WFMPEQ) — this copy holds only the characters , . 0 1 2 3 4 5 6; the same column's '4,575.00' is set in the main copy; 'Term 3 Fees' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#12`: **—** → **high | Page 1: '1,450.00' is set in a second embedded copy of TimesNewRoman (subset JIXQHL; the page's main copy is WFMPEQ) — this copy holds only the characters , . 0 1 2 3 4 5 6; the same column's '4,575.00' is set in the main copy; 'Registration Fees' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#13`: **—** → **high | Page 1: '16,450.00' is set in a second embedded copy of TimesNewRoman (subset JIXQHL; the page's main copy is WFMPEQ) — this copy holds only the characters , . 0 1 2 3 4 5 6; the same column's '4,575.00' is set in the main copy; 'Term 1 Fees' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#14`: **—** → **high | Page 1: '2,300.00' is set in a second embedded copy of TimesNewRoman (subset JIXQHL; the page's main copy is WFMPEQ) — this copy holds only the characters , . 0 1 2 3 4 5 6; the same column's '4,575.00' is set in the main copy; 'Books Fees' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#15`: **—** → **high | Page 1: '13,350.00' is set in a second embedded copy of TimesNewRoman (subset JIXQHL; the page's main copy is WFMPEQ) — this copy holds only the characters , . 0 1 2 3 4 5 6; the same column's '4,575.00' is set in the main copy; 'Term 2 Fees' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#16`: **—** → **high | Page 1: '13,350.00' is set in a second embedded copy of TimesNewRoman (subset JIXQHL; the page's main copy is WFMPEQ) — this copy holds only the characters , . 0 1 2 3 4 5 6; the same column's '4,575.00' is set in the main copy; 'Term 3 Fees' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#2`: **—** → **high | Page 1: 'PRE-KG-NA' is set in a second embedded copy of TimesNewRoman,Bold (subset MONKPZ; the page's main copy is ZEDDUK) — 'SALEH MOHAMED ALHARTHI' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#3`: **—** → **high | Page 1: '51,475.00' is set in a second embedded copy of TimesNewRoman,Bold (subset AKJOLD; the page's main copy is ZEDDUK) — '0' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#4`: **—** → **high | Page 1: 'Fifty-One Thousand Four Hundred Seventy-Five Only' is set in a second embedded copy of TimesNewRoman,Bold (subset AKJOLD; the page's main copy is ZEDDUK) — 'Amount in words AED:' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#5`: **—** → **high | Page 1: '51,475.00' is set in a second embedded copy of TimesNewRoman,Bold (subset AKJOLD; the page's main copy is ZEDDUK) — '0' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#6`: **—** → **high | Page 1: '51,475.00' is set in a second embedded copy of TimesNewRoman,Bold (subset AKJOLD; the page's main copy is ZEDDUK) — 'Amount in words AED:' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#7`: **—** → **high | Page 1: '1,450.00' is set in a second embedded copy of TimesNewRoman (subset JIXQHL; the page's main copy is WFMPEQ) — this copy holds only the characters , . 0 1 2 3 4 5 6; the same column's '4,575.00' is set in the main copy; 'Registration Fees' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#8`: **—** → **high | Page 1: '16,450.00' is set in a second embedded copy of TimesNewRoman (subset JIXQHL; the page's main copy is WFMPEQ) — this copy holds only the characters , . 0 1 2 3 4 5 6; 'Term 1 Fees' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `finding:font_subset_split#9`: **—** → **high | Page 1: '2,300.00' is set in a second embedded copy of TimesNewRoman (subset JIXQHL; the page's main copy is WFMPEQ) — this copy holds only the characters , . 0 1 2 3 4 5 6; the same column's '4,575.00' is set in the main copy; 'Books Fees' on the same line is set in the main copy. A document exported in one pass embeds each font once; a further copy of the same font appears when text is typed in during a later edit.**
- `font_consistency` `result`: **pass** → **flag**
- `ghost_content` `finding:ghost_deleted_block#1`: **—** → **high | Page 1: a block of 6 line(s) of faint text remains in the scanned background (x 30–270 pt, y 388–480 pt from the top) with no live text on top of it: content that was on the page when it was converted to editable text, and was deleted afterwards.**
- `ghost_content` `finding:ghost_replaced_line#0`: **—** → **medium | Page 1: the faint trace of the original text in the scan runs on 90 pt past the live text 'Registration Fees' on its line (x 155–196 pt, y 219–227 pt from the top). That line was longer when the scan was converted to editable text, and has been shortened or rewritten since.**
- `ghost_content` `result`: **None** → **flag**
- `metadata_forensics` `finding:editable_text_over_scan#1`: **low | Page(s) 1: visible, editable text drawn over a page-sized image. Shown for review, not scored: a digitally produced document drawn on a full-page letterhead image is built the same way.** → **—**
- `metadata_forensics` `finding:editable_text_over_scan#2`: **—** → **medium | Page(s) 1: a page-sized scan image with visible, editable text drawn on top of it (in ordinary subset fonts, as Acrobat's own editor or a re-saved converted file leaves them) — a scan converted to editable text. A normal searchable scan keeps its OCR text invisible; this structure is what lets a scan's words and numbers be retyped or deleted.**
- `metadata_forensics` `finding:mod_date_after_creation_date#0`: **high | The file was modified after it was created: ModDate is 8 days, 20:46:20 after CreationDate (more than the 1-second timestamp tolerance).** → **—**
- `metadata_forensics` `finding:mod_date_after_creation_date#1`: **—** → **high | The file was modified after it was created: ModDate is 8 days, 20:46:20 after CreationDate (more than the 1-second timestamp tolerance).**
- `metadata_forensics` `finding:producer_scrubbed#0`: **—** → **medium | The XMP metadata was written by Adobe's own library (Adobe XMP Core 9.1-c001 79.2a0d8d9), yet Producer, Creator, CreatorTool and the edit history are all missing. Adobe software always records its name and saves there, so they were removed afterwards — hiding which tool last changed the file.**

## case11School  educational document.pdf (`624ebf7b6992`)

Representative case CASE-7AC9B30F (testcompany); cases: CASE-7AC9B30F.

Score 48 → 48 (raw 48.0 → 48.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## case12School  educational document.pdf (`5ba396aed871`)

Representative case CASE-3CCC350B (testcompany); cases: CASE-3CCC350B.

Score 37 → 37 (raw 37.0 → 37.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## case14School  educational document.pdf (`bd91d951965e`)

Representative case CASE-866F937F (testcompany); cases: CASE-0D7E8A5E, CASE-866F937F.

Score 65 → 65 (raw 65.0 → 65.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## case2.pdf (`e9c3ea682c74`)

Representative case CASE-DE627FA2 (testcompany); cases: CASE-A7705426, CASE-CF609781, CASE-DE627FA2.

Score 40 → 40 (raw 40.0 → 40.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## case3School  educational document.pdf (`0fe36ed31d2e`)

Representative case CASE-3ABE8978 (testcompany); cases: CASE-3ABE8978.

Score 40 → 40 (raw 40.0 → 40.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## case4School  educational document.pdf (`3aeb1d125f98`)

Representative case CASE-9B219083 (testcompany); cases: CASE-9B219083.

Score 80 → 80 (raw 80.0 → 80.0).


Check-level changes:
- `ghost_content` `result`: **None** → **pass**
- `metadata_forensics` `finding:editable_text_over_scan#3`: **medium | Page(s) 1: a page-sized scan image with its text re-created on top as visible, editable text in converter-generated fonts ("-NNNN" subsets) — a scan converted to editable text. A normal searchable scan keeps its OCR text invisible; this structure is what lets a scan's numbers be retyped.** → **medium | Page(s) 1: a page-sized scan image with visible, editable text drawn on top of it in converter-generated fonts ("-NNNN" subsets) — a scan converted to editable text. A normal searchable scan keeps its OCR text invisible; this structure is what lets a scan's words and numbers be retyped or deleted.**

## case5School  educational document.pdf (`895a259e007d`)

Representative case CASE-5B2FDA1E (testcompany); cases: CASE-1CEDFDBF, CASE-1F849C4D, CASE-54D5A2C6, CASE-5B2FDA1E, CASE-91A3A945, CASE-D8CFFC09, CASE-F6F5FE76.

Score 100 → 100 (raw 140.0 → 140.0).


Check-level changes:
- `ghost_content` `result`: **None** → **pass**
- `metadata_forensics` `finding:editable_text_over_scan#3`: **medium | Page(s) 1: a page-sized scan image with its text re-created on top as visible, editable text in converter-generated fonts ("-NNNN" subsets) — a scan converted to editable text. A normal searchable scan keeps its OCR text invisible; this structure is what lets a scan's numbers be retyped.** → **medium | Page(s) 1: a page-sized scan image with visible, editable text drawn on top of it in converter-generated fonts ("-NNNN" subsets) — a scan converted to editable text. A normal searchable scan keeps its OCR text invisible; this structure is what lets a scan's words and numbers be retyped or deleted.**

## case7School  educational document.pdf (`c1a11cef261c`)

Representative case CASE-2745F56C (testcompany); cases: CASE-2745F56C, CASE-4B297080, CASE-F64B3E0F.

Score 96 → 96 (raw 96.0 → 96.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## case8School  educational document.pdf (`14926b7fca21`)

Representative case CASE-B70765EC (testcompany); cases: CASE-B70765EC.

Score 50 → 50 (raw 50.0 → 50.0).


Check-level changes:
- `ghost_content` `result`: **None** → **pass**
- `metadata_forensics` `finding:editable_text_over_scan#2`: **medium | Page(s) 1: a page-sized scan image with its text re-created on top as visible, editable text in converter-generated fonts ("-NNNN" subsets) — a scan converted to editable text. A normal searchable scan keeps its OCR text invisible; this structure is what lets a scan's numbers be retyped.** → **medium | Page(s) 1: a page-sized scan image with visible, editable text drawn on top of it in converter-generated fonts ("-NNNN" subsets) — a scan converted to editable text. A normal searchable scan keeps its OCR text invisible; this structure is what lets a scan's words and numbers be retyped or deleted.**

## case9 School  educational document.pdf (`6e31ba79c300`)

Representative case CASE-346FA23E (testcompany); cases: CASE-346FA23E.

Score 48 → 48 (raw 48.0 → 48.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## caseimage1.pdf (`1d2ff2211bce`)

Representative case CASE-1630D946 (testcompany); cases: CASE-1630D946.

Score 59 → 59 (raw 59.0 → 59.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## caseimage2.pdf (`713a033844ef`)

Representative case CASE-9EAF4471 (testcompany); cases: CASE-9EAF4471.

Score 50 → 50 (raw 50.0 → 50.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## evidence.pdf (`8aa9dee3b984`)

Representative case CASE-081A5D03 (testcompany); cases: CASE-081A5D03, CASE-0A9DF515, CASE-1A1C20EE, CASE-1FD06F9D, CASE-240E7414, CASE-25321E95, CASE-2A52737A, CASE-2CD1AE60, CASE-3121FEE1, CASE-4831DD54, CASE-4F6F8BFE, CASE-74E159DB, CASE-ACC0FDB8, CASE-ADFCD2C5, CASE-D1F1BE94, CASE-D24FEB59, CASE-D7C8DCCA, CASE-E3AB20DE.

Score 15 → 15 (raw 15.0 → 15.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## invoice.pdf (`839b8362704e`)

Representative case CASE-081A5D03 (testcompany); cases: CASE-05AAD4C7, CASE-081A5D03, CASE-1FD06F9D, CASE-2AE16040, CASE-33FE90A8, CASE-3ED18F86, CASE-44AECE73, CASE-4831DD54, CASE-55BF92C9, CASE-74E159DB, CASE-76DABAF5, CASE-AAFF956B, CASE-ACC0FDB8, CASE-ADFCD2C5, CASE-D1F1BE94, CASE-D24FEB59, CASE-F3DDDC12.

Score 55 → 55 (raw 55.0 → 55.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample10_English_Case_Evidence.pdf (`34dbb9df1bca`)

Representative case CASE-47954182 (Default Company); cases: CASE-47954182.

Score 40 → 40 (raw 40.0 → 40.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample10_English_Case_Invoice.pdf (`96058cadc567`)

Representative case CASE-47954182 (Default Company); cases: CASE-47954182.

Score 0 → 0 (raw 0.0 → 0.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample11_Arabic_Case_Evidence.pdf (`242574c83124`)

Representative case CASE-0A108B8F (Default Company); cases: CASE-0A108B8F.

Score 0 → 0 (raw 0.0 → 0.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample11_Arabic_Case_Invoice.pdf (`5373b29fef82`)

Representative case CASE-0A108B8F (Default Company); cases: CASE-0A108B8F.

Score 60 → 60 (raw 60.0 → 60.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample12_English_Case_Evidence.pdf (`94d51aa4f3d7`)

Representative case CASE-3249B03F (Default Company); cases: CASE-0C6F41F9, CASE-1BD2A11D, CASE-3249B03F, CASE-4C085E81, CASE-5F2D3700, CASE-6A2EA2ED, CASE-6C73A607, CASE-6FCE2F6C, CASE-70DA1113, CASE-7EA12803, CASE-B4034CC7.

Score 100 → 100 (raw 170.0 → 170.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample12_English_Case_Invoice.pdf (`bc41a171e784`)

Representative case CASE-1D925E31 (Default Company); cases: CASE-0319E379, CASE-0C6F41F9, CASE-1BD2A11D, CASE-1CAEC35B, CASE-1D925E31, CASE-3249B03F, CASE-4C085E81, CASE-5F2D3700, CASE-6A2EA2ED, CASE-6C73A607, CASE-6FCE2F6C, CASE-70DA1113, CASE-7EA12803, CASE-B4034CC7.

Score 100 → 100 (raw 155.0 → 155.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample13_Arabic_cp_ela_Evidence.pdf (`16be1bd21ae0`)

Representative case CASE-C76B35B8 (testcompany); cases: CASE-0861360C, CASE-370CE650, CASE-91206BB0, CASE-C76B35B8, CASE-D768259F, CASE-E4B31A87.

Score 100 → 100 (raw 155.0 → 155.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample13_Arabic_cp_ela_Invoice.pdf (`d440edcb8b6c`)

Representative case CASE-C76B35B8 (testcompany); cases: CASE-0861360C, CASE-370CE650, CASE-91206BB0, CASE-C76B35B8, CASE-E4B31A87.

Score 100 → 100 (raw 155.0 → 155.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample14_English_Case_Evidence.pdf (`9c7a76cca6f2`)

Representative case CASE-B5C72B6D (Default Company); cases: CASE-1D925E31, CASE-3C9A9933, CASE-900B2CF6, CASE-B5C72B6D.

Score 70 → 70 (raw 70.0 → 70.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample14_English_Case_Invoice.pdf (`60ba1dd3298a`)

Representative case CASE-B5C72B6D (Default Company); cases: CASE-3C9A9933, CASE-900B2CF6, CASE-B5C72B6D.

Score 55 → 55 (raw 55.0 → 55.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample15_Arabic_Case_Evidence.pdf (`e6ac7601cb8d`)

Representative case CASE-97EAD181 (Default Company); cases: CASE-8D8C00E1, CASE-94B0146F, CASE-97EAD181.

Score 70 → 70 (raw 70.0 → 70.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample15_Arabic_Case_Invoice.pdf (`4441a05e1d9c`)

Representative case CASE-97EAD181 (Default Company); cases: CASE-8D8C00E1, CASE-94B0146F, CASE-97EAD181.

Score 70 → 70 (raw 70.0 → 70.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample15_English_Mismatch_Evidence.pdf (`eb5b436da2bd`)

Representative case CASE-30720157 (Default Company); cases: CASE-30720157.

Score 16 → 16 (raw 16.0 → 16.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample15_English_Mismatch_Invoice.pdf (`1649dee021e6`)

Representative case CASE-30720157 (Default Company); cases: CASE-30720157.

Score 61 → 61 (raw 61.0 → 61.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample16_English_Case_Evidence.pdf (`313d2c40901b`)

Representative case CASE-86E98F94 (testcompany); cases: CASE-86E98F94, CASE-FF777FB2.

Score 100 → 100 (raw 155.0 → 155.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample16_English_Case_Evidence.pdf (`33795008c45b`)

Representative case CASE-D63AC666 (Default Company); cases: CASE-D63AC666.

Score 70 → 70 (raw 70.0 → 70.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample16_English_Case_Invoice.pdf (`674555cf71cc`)

Representative case CASE-86E98F94 (testcompany); cases: CASE-86E98F94, CASE-FF777FB2.

Score 100 → 100 (raw 249.0 → 249.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample16_English_Case_Invoice.pdf (`e058a63e7c72`)

Representative case CASE-D63AC666 (Default Company); cases: CASE-D63AC666.

Score 100 → 100 (raw 130.0 → 130.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample17_English_AllFlags_Evidence.pdf (`c1092110a513`)

Representative case CASE-70FDDB1B (testcompany); cases: CASE-70FDDB1B, CASE-A11F243F, CASE-CDB578EF, CASE-E83EE27E, CASE-F9AB6C21.

Score 100 → 100 (raw 155.0 → 155.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample17_English_AllFlags_Invoice.pdf (`d180d0151abe`)

Representative case CASE-70FDDB1B (testcompany); cases: CASE-70FDDB1B, CASE-A11F243F, CASE-CDB578EF, CASE-E83EE27E, CASE-F9AB6C21.

Score 100 → 100 (raw 249.0 → 249.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample18_English_FontMismatch_Invoice.pdf (`34d1da2c0aab`)

Representative case CASE-589ECE66 (testcompany); cases: CASE-2EDE9D85, CASE-589ECE66, CASE-7C24EBB6.

Score 46 → 46 (raw 46.0 → 46.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample6_English_Case_Evidence.pdf (`40be67ac124d`)

Representative case CASE-8B6028CA (testcompany); cases: CASE-1FD06F9D, CASE-2C1C7304, CASE-4A072B50, CASE-574B5439, CASE-61637697, CASE-74E159DB, CASE-8AAF3E05, CASE-8B6028CA, CASE-9B9B9220, CASE-AC10D420, CASE-ACC0FDB8, CASE-CE205D2D, CASE-D1F1BE94, CASE-D24FEB59, CASE-EB84476F.

Score 40 → 40 (raw 40.0 → 40.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample6_English_Case_Invoice.pdf (`7234c70f39ca`)

Representative case CASE-8B6028CA (testcompany); cases: CASE-1A2AFD37, CASE-1FD06F9D, CASE-411482CB, CASE-538BCDC0, CASE-574B5439, CASE-726AEDC4, CASE-74E159DB, CASE-77E916DC, CASE-8AAF3E05, CASE-8B6028CA, CASE-AC10D420, CASE-ACC0FDB8, CASE-D1F1BE94, CASE-D24FEB59, CASE-DD1EAD4F, CASE-F1B6653B, CASE-F4B6AB95, CASE-FD2E14E2.

Score 40 → 40 (raw 40.0 → 40.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample7_Arabic_Case_Evidence.pdf (`d4305e0d10b1`)

Representative case CASE-3D126D3A (testcompany); cases: CASE-0EF1FAD5, CASE-1F20A083, CASE-1FD06F9D, CASE-2CBAED37, CASE-3511D44E, CASE-3D126D3A, CASE-609B87E8, CASE-67041F47, CASE-6C792535, CASE-74E159DB, CASE-ACC0FDB8, CASE-CF679EE6, CASE-D1F1BE94, CASE-D24FEB59, CASE-F1B2FE14.

Score 40 → 40 (raw 40.0 → 40.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample7_Arabic_Case_Invoice.pdf (`c06c5e8200e1`)

Representative case CASE-3D126D3A (testcompany); cases: CASE-11F0CAA6, CASE-1C88EFE7, CASE-1FD06F9D, CASE-2897F393, CASE-30D16008, CASE-3511D44E, CASE-3D126D3A, CASE-4B6F6223, CASE-5A8B6D87, CASE-6C792535, CASE-71A8323E, CASE-74E159DB, CASE-8BDDEB82, CASE-99421D4E, CASE-ACC0FDB8, CASE-CF679EE6, CASE-D1F1BE94, CASE-D24FEB59, CASE-FF8CB078.

Score 40 → 40 (raw 40.0 → 40.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample8_English_Case_Evidence.pdf (`36aea3d57061`)

Representative case CASE-8A645E93 (QA Alpha Trading); cases: CASE-1963B797, CASE-1DD6E9B1, CASE-1FD06F9D, CASE-2BEE0A11, CASE-3C2E2429, CASE-74E159DB, CASE-8A645E93, CASE-9F98716A, CASE-A3AD8556, CASE-ACC0FDB8, CASE-D1F1BE94, CASE-D24FEB59, CASE-DFB293D7, CASE-E0EE112A.

Score 6 → 6 (raw 6.0 → 6.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample8_English_Case_Invoice.pdf (`98675fec0dfd`)

Representative case CASE-8A645E93 (QA Alpha Trading); cases: CASE-135A6D81, CASE-1963B797, CASE-1FD06F9D, CASE-242525DA, CASE-7209C19A, CASE-74E159DB, CASE-8A645E93, CASE-9D294094, CASE-A3AD8556, CASE-A5719CE2, CASE-ACC0FDB8, CASE-C3083696, CASE-D1F1BE94, CASE-D24FEB59.

Score 6 → 6 (raw 6.0 → 6.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample9_Arabic_Case_Evidence.pdf (`42f16b572ae1`)

Representative case CASE-A4E8ACD1 (Default Company); cases: CASE-1FD06F9D, CASE-46C19894, CASE-60AD8C23, CASE-6BBA52DE, CASE-74E159DB, CASE-7C39E3E0, CASE-9053DEF2, CASE-A4E8ACD1, CASE-ACC0FDB8, CASE-D1F1BE94, CASE-D24FEB59, CASE-DD6D8831, CASE-E96FC740.

Score 21 → 21 (raw 21.0 → 21.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Sample9_Arabic_Case_Invoice.pdf (`4df3381d567b`)

Representative case CASE-D768259F (Default Company); cases: CASE-0FC5EB2A, CASE-1224A817, CASE-1FD06F9D, CASE-5099BE34, CASE-70B666D0, CASE-74E159DB, CASE-ACC0FDB8, CASE-C7F2C2C7, CASE-D1F1BE94, CASE-D24FEB59, CASE-D768259F, CASE-F1495539.

Score 0 → 0 (raw 0.0 → 0.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## sample_font20.pdf (`910eb0b646a4`)

Representative case CASE-6CE43E0C (testcompany); cases: CASE-37B33CBB, CASE-6CB8F7E1, CASE-6CE43E0C, CASE-942C8493, CASE-DB43653A.

Score 100 → 100 (raw 150.0 → 150.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Test_Arabic_Tampered_Invoice.pdf (`3c6030544d85`)

Representative case CASE-78B37E39 (Default Company); cases: CASE-78B37E39.

Score 87 → 87 (raw 87.0 → 87.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## Test_English_Tampered_Invoice.pdf (`ca40d3081340`)

Representative case CASE-BE0DC4A1 (Default Company); cases: CASE-BE0DC4A1.

Score 87 → 87 (raw 87.0 → 87.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## valid.pdf (`82efdc39f20c`)

Representative case CASE-0B726DE7 (testcompany); cases: CASE-0B726DE7, CASE-C38287CD, CASE-D7C8DCCA, CASE-DFFD48FF.

Score 68 → 68 (raw 68.0 → 68.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## إيصال.pdf (`1b8931d47d25`)

Representative case CASE-6573A030 (testcompany); cases: CASE-047F0E67, CASE-0B78C29C, CASE-0CAB55E0, CASE-1FD06F9D, CASE-290A1E3D, CASE-3C2ADEBB, CASE-6573A030, CASE-74E159DB, CASE-938C3870, CASE-ACC0FDB8, CASE-AEE6C99D, CASE-D1F1BE94, CASE-D24FEB59, CASE-ED0D638F, CASE-F88C88FC.

Score 15 → 15 (raw 15.0 → 15.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

## فاتورة.pdf (`cef1371f4eaa`)

Representative case CASE-6573A030 (testcompany); cases: CASE-1FD06F9D, CASE-2C6DDCB7, CASE-46C19894, CASE-622804E7, CASE-6573A030, CASE-74E159DB, CASE-7A08201F, CASE-888F7C89, CASE-ACC0FDB8, CASE-BC06B819, CASE-D1F1BE94, CASE-D24FEB59, CASE-DCBA5CC6.

Score 15 → 15 (raw 15.0 → 15.0).


Check-level changes:
- `ghost_content` `result`: **None** → **not_applicable**

