# Regression diff

Before: `tests/regression_after_7ac` — after: `tests/regression_after_cols`.

3 document(s) changed, 47 unchanged.

| Document | Cases | Score before | Score after | Rules removed | Rules added |
|---|---|---|---|---|---|
| case10School  educational document.pdf (`6f2775326148`) | 1 | 100 | 100 | — | — |
| case12School  educational document.pdf (`5ba396aed871`) | 1 | 37 | 37 | — | — |
| caseimage2.pdf (`713a033844ef`) | 1 | 50 | 35 | field.subtotal_line_item_mismatch | — |

## case10School  educational document.pdf (`6f2775326148`)

Representative case CASE-39CB18BF (testcompany); cases: CASE-39CB18BF.

Score 100 → 100 (raw 110.0 → 110.0).


Check-level changes:
- `field_validation` `sub:subtotal_line_item_consistency`: **pass | The 6 line item(s) add up to the total 51,475.00 less tax 0.00 (51,475.00).** → **pass | Each column adds up to its own total: Net Amount column 51,475.00 = total; Vat column 0.00 = tax amount.**

## case12School  educational document.pdf (`5ba396aed871`)

Representative case CASE-3CCC350B (testcompany); cases: CASE-3CCC350B.

Score 37 → 37 (raw 37.0 → 37.0).


Check-level changes:
- `field_validation` `sub:subtotal_line_item_consistency`: **pass | The 7 line item(s) add up to the subtotal 70,360.00.** → **pass | Each column adds up to its own total: VAT column 0.00 = Total Tax; Total Amount column 70,360.00 = amount.**

## caseimage2.pdf (`713a033844ef`)

Representative case CASE-9EAF4471 (testcompany); cases: CASE-9EAF4471.

Score 50 → 35 (raw 50.0 → 35.0).

- rule **removed** `field.subtotal_line_item_mismatch` (−15): 'caseimage2.pdf': Line items = 126,007.87, stated subtotal 120,007.50, difference 6,000.37 (summed: 138,495.00 + -10,387.13 + -2,100.00).

Check-level changes:
- `field_validation` `result`: **flag** → **pass**
- `field_validation` `sub:subtotal_line_item_consistency`: **flag | Line items = 126,007.87, stated subtotal 120,007.50, difference 6,000.37 (summed: 138,495.00 + -10,387.13 + -2,100.00).** → **pass | Each column adds up to its own total: Net Price column 120,007.50 = Total Net Price; VAT Amount column 6,000.37 = VAT; Total Price column 126,007.87 = Total Price.**

