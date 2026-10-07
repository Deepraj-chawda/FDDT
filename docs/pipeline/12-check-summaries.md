# 12 — Short check summaries

**What:** the short, readable form of every check — what the case page shows first and what the report
puts first, ahead of the full explanation.

**Code:** `services/check_summaries.py` (`summarize_check`, `summarize_document_checks`,
`risk_reason_short`). Computed when read from the stored results, so cases checked before it existed read
the same way; nothing here changes a result or a score.

## Shape

```jsonc
{
  "headline": "3 issues: Text retyped, Totals retyped, Amounts retyped",
  "items": [{
    "severity": "high",
    "title": "Amounts retyped",
    "text": "1,450.00 ×2 · 16,450.00 ×2 · 2,300.00 ×2 · 13,350.00 ×4 — in a second copy of Times New Roman (a digits-only copy); 4,575.00 in the same column is original",
    "detail": "Page 1: '1,450.00' is set in a second embedded copy of … (9 more of the same kind.)",
    "page": 1, "findings": ["font_subset_split"],
    "image_png_base64": null, "hint": null
  }],
  "notes": ["1 of 1 page(s) analysed; 2 font families: times, courier."]
}
```

- **One item per problem**, in one line with the actual values. Findings with one cause are grouped
  (17 font findings → 3 items); `detail` is the first full explanation, plus how many more of the same kind.
- **Notes** are info lines (what was searched, what passed, why a check was limited).
- **Headline**: "N issues: …" when something is scored; "Limited — …", "Not applicable — …",
  "Not checked — …", "Failed to run: …" or "No issues found" otherwise.
- The stamp sub-checks of field validation (stamp names the issuer, stamp typed into the file, no
  signature either) are shown with **signature / stamp detection**, not field validation.
- Passed sub-checks worth reading are spelled out as notes ("Dates in order: dated 17 Aug 2023 →
  printed 18 Aug 2023 → PDF created 21 Aug 2023", "Installments add up: …"); the rest are listed as
  "Passed: …".
- A check whose only findings are low (shown, not scored) reads **Review** in the report, not Flag.
- A not-applicable check gives its stored reason ("Not applicable — born-digital, no images").

## Where it is used

| Place | Short first | Full text |
|---|---|---|
| Case page — each check card | headline under the title; items with **Why?** | "Technical details" (raw findings) |
| Case page — explainable findings, decision dialog | rule title + short line | **Why?** (the stored reason) |
| Case queue / risk banner | top 3 rule titles (+N more) | — |
| Report §3 Explainable Findings | **title — short line** | the stored reason, grey |
| Report §4 All Checks | headline + one bullet per item / note | — |
| Report §5 Exceptions | **title — short line** (one row per item, all its highlights) | first explanation, grey |

API: `DocumentCheckSummary.summary`; `RiskReasonSchema.title` / `.short`.
