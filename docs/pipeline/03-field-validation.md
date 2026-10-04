# 03 — Field validation

**What it detects:** internally inconsistent documents — the kind of arithmetic or date mistake that
falsified or hand-edited documents often contain. It is **pure rule logic over one document's own
extracted fields**: no I/O, no model, no database.

**Code:** `services/field_validation_service.py` (`validate_fields`), wrapped by
`tasks/document_checks.py` (`run_document_checks`), which stores a `document_checks` row with
`check_type = field_validation`. Highlight regions come from `services/field_regions.py` and
`services/field_exception_service.py`.

## Sub-checks

`validate_fields(extracted_fields) -> {"result": "pass"|"flag", "details": {...}}` runs four
independent sub-checks; the overall result is `flag` if **any** sub-check is `flag`.

| Sub-check | Flags when | Skipped when |
|---|---|---|
| `date_in_future` | the document's `date` core field is after today | no parseable date |
| `subtotal_line_item_consistency` | `subtotal` ≠ sum of additional fields that look like per-line-item totals (name contains `item`, value hint `total`/`amount`) | no subtotal, or no such fields |
| `total_tax_consistency` | `amount` ≠ `subtotal + tax_amount` (or ≠ `subtotal` when no tax line is shown) | no subtotal |
| `reference_number_format` | the reference number is not a plausible identifier (regex `^[A-Za-z0-9](?:[A-Za-z0-9\-/_. #]*[A-Za-z0-9])?$`) | — |

Line-item detection is deliberately loose because extraction is template-free: line items have no
fixed field name.

## Output shape (real example)

```jsonc
{
  "result": "flag",
  "details": {
    "date_in_future": {"status": "pass", "reason": "Document date 2026-08-25 is not in the future."},
    "total_tax_consistency": {
      "status": "flag",
      "reason": "Total 197500.00 does not match subtotal 180000.00 + tax 9000.00 (expected 189000.00).",
      "regions": [
        {"field": "amount", "label": "Total amount", "value": "197,500.00 AED",
         "caption": "Field exception: Total amount (197,500.00 AED vs expected 189,000.00)",
         "bounding_box": {"page": 1, "x": 0.7821, "y": 0.5081, "width": 0.1321, "height": 0.0119}}
      ]
    },
    "reference_number_format": {"status": "pass", "reason": "…"},
    "subtotal_line_item_consistency": {"status": "pass", "reason": "Subtotal matches the sum of 3 line-item field(s)."}
  }
}
```

Unlike the forensic checks, `details` is a **dict keyed by sub-check**, not a list of findings. Each
sub-check is `{status, reason, regions?}` where `status` is `pass`, `flag` or `skipped` (no value to check, or unparseable); `regions` (only on `flag`) are drawn as **purple field
exception** boxes over the PDF. If a field's position could not be located, the exception is still
reported without a box. Rows stored before locations existed are re-derived at read time
(`with_field_regions`) without modifying the stored row.

## Known limitations

- Depends entirely on extraction quality; a wrong extracted value produces a wrong flag (or a miss).
- Both the subtotal-vs-line-items and the total-vs-subtotal+tax comparisons tolerate
  `max(0.01, 1 % of the compared amount)`; a document whose rounding differs by more than that will flag.
- `date_in_future` compares against `date.today()` on the worker's machine, not the case's submission
  date, so a worker with a wrong clock can mis-flag.

## Risk rules fed

| `rule_id` | Sub-check | Weight | Severity |
|---|---|---:|---|
| `field.date_in_future` | `date_in_future` | 25 | high |
| `field.subtotal_line_item_mismatch` | `subtotal_line_item_consistency` | 15 | medium |
| `field.total_tax_mismatch` | `total_tax_consistency` | 20 | high |
| `field.reference_number_malformed` | `reference_number_format` | 6 | low |

(Weights are the template defaults; each company's Reviewer L2s can change them for their company — see
[11](11-risk-scoring-engine.md).)
