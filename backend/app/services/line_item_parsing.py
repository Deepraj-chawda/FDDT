"""
Deterministic line-item reading from the OCR result, applied on top of the
LLM extraction (app/services/llm_service.py) before the arithmetic checks in
app/services/field_validation_service.py run.

The extraction model reads charge lines well in general but gets two
layouts wrong in ways that break the arithmetic checks:

  - Table invoices with ONE data row, where every charge is a column ("Name |
    Grade | Tuition Fees | Books | ... | 5% Vat | Total Fees"): the columns
    are not always read as line items, so there is no subtotal to check.
    `parse_single_row_tables` treats each numeric column whose header is not
    a tax/total column as a line item (label = header, amount = cell) and
    maps the tax and total columns to `tax_amount` / `amount`.

  - "Description | Amount" tables where OCR stacked several rows into one
    cell: the amounts of four fee lines read as one cell ("12,000 9,000 9,000
    6,000") beside four description rows, or several descriptions in one
    cell, one per line. `parse_stacked_amount_tables` pairs the n-th
    description with the n-th amount by position — only when the counts
    agree — and maps a "Total" row to `amount`.

  - Text invoices with "<rate> PER <unit> X <qty> <unit>(S) = <amount>"
    lines, where a line that mentions both a PAID and a DUE period ("TERM 1
    (September) PAID (October-November-December) DUE") was dropped from the
    payable sum as if it were paid. `parse_rate_quantity_lines` reads each
    such line exactly as printed (rate, quantity, printed total), its PAID/
    DUE status, and the periods it lists. A line is "paid" — kept in the
    list but not summed — only when it is marked PAID and nothing on it is
    DUE.

An LLM line whose own OCR line (its description and printed total) says PAID
and nothing DUE ("1. REGISTRATION FEES: 500 PAID") is marked paid the same
way.

Both parsers only ever copy numbers that are printed; nothing is recomputed.
Their line items replace the matching LLM ones (same printed total) and keep
every other LLM line, so a document neither parser recognises is left
exactly as the model extracted it. Each line item records its `source`
("table", "text_pattern" or "llm") and `status` ("paid", "due" or None), and
is stored with the rest of `documents.extracted_fields`.

`enrich_extracted_fields` also labels form-template dates and removes
signature ink read as words from person names
(app/services/extraction_postprocess.py), and records `pdf_info` (Producer, Creator, Title,
CreationDate and ModDate of the PDF), used by the rescan and document-date
checks and cross-case linking.
"""
from __future__ import annotations

import copy
import io
import re
from typing import Any

_NUMBER = r"\d{1,3}(?:,\s?\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_RATE_QTY_RE = re.compile(
    rf"(?P<rate>{_NUMBER})\s*(?:[A-Z]{{3}}\s+)?PER\s+(?P<unit>[A-Z]+?)S?\s*[X×*]\s*"
    rf"(?P<qty>\d+(?:\.\d+)?)\s*(?P<qty_unit>[A-Z]+)?\s*=\s*(?P<total>{_NUMBER})",
    re.IGNORECASE,
)
# The start of a numbered item ("2. FEE FOR TERM 1 ...", "3) Books").
_ITEM_START_RE = re.compile(r"^\s*\d{1,2}\s*[.)]\s+\S")
# How many lines above a rate line its item description may start.
_MAX_BLOCK_LINES = 4
_PAID_RE = re.compile(r"\bPAID\b", re.IGNORECASE)
_DUE_RE = re.compile(r"\bDUE\b", re.IGNORECASE)
# "(October-November-December) DUE", "(September) PAID".
_PERIOD_GROUP_RE = re.compile(r"\(([^()]*)\)\s*(PAID|DUE)?", re.IGNORECASE)
_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_MONTH_RE = re.compile(r"\b(" + "|".join(m[:3] for m in _MONTHS) + r")[a-z]*\.?", re.IGNORECASE)
# Where a description stops: the line's own detail ("NUMBER OF DAYS ...", "FEE:").
_DESCRIPTION_END_RE = re.compile(r"\s+(?:NUMBER OF\b|FEE\s*:)", re.IGNORECASE)

# Single-row table headers.
_TOTAL_HEADER_RE = re.compile(r"\b(?:grand\s+)?total\b|\bnet\s+payable\b|\bamount\s+due\b|\bbalance\s+due\b", re.IGNORECASE)
_TAX_HEADER_RE = re.compile(r"\b(?:vat|tax|gst)\b", re.IGNORECASE)
_SUBTOTAL_HEADER_RE = re.compile(r"\bsub\s*-?\s*total\b", re.IGNORECASE)
_RATE_IN_HEADER_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_CELL_NUMBER_RE = re.compile(rf"^\s*(?:[A-Z]{{3}}\s*)?({_NUMBER})\s*(?:[A-Z]{{3}})?\s*$")
# Stacked "Description | Amount" tables.
_DESCRIPTION_HEADER_RE = re.compile(r"\b(?:description|particulars|details?|items?)\b", re.IGNORECASE)
_AMOUNT_HEADER_RE = re.compile(r"\b(?:amount|amt|price|value|aed|usd)\b", re.IGNORECASE)
_AMOUNTS_CELL_RE = re.compile(rf"^\s*(?:(?:[A-Z]{{3}}\s*)?(?:{_NUMBER})\s*)+$")
_AMOUNT_IN_CELL_RE = re.compile(_NUMBER)
# A one-row table is only read as charges with a total column plus at least
# this many other numeric columns (a "Description | Amount" table is a
# one-line item list, not a row of charges).
_MIN_TABLE_CHARGE_COLUMNS = 2


def _number(text: str) -> float:
    return float(re.sub(r"[,\s]", "", text))


def _months(text: str) -> list[str]:
    return [_MONTHS[[m[:3] for m in _MONTHS].index(m.group(1).lower())].capitalize() for m in _MONTH_RE.finditer(text)]


def _block_for(lines: list[str], index: int) -> list[str]:
    """The lines of the item a rate line at `index` belongs to: back to the
    nearest numbered item start (at most _MAX_BLOCK_LINES above), else the
    line itself."""
    for back in range(0, _MAX_BLOCK_LINES + 1):
        i = index - back
        if i < 0:
            break
        if _ITEM_START_RE.match(lines[i]):
            return lines[i : index + 1]
        if back and _RATE_QTY_RE.search(lines[i]):
            break  # the previous rate line belongs to the previous item
    return [lines[index]]


def _description(block: list[str], match: re.Match) -> str:
    text = re.sub(r"^\s*\d{1,2}\s*[.)]\s*", "", block[0])
    if len(block) == 1:  # the rate line itself: what precedes the rate
        text = text[: match.start()]
    text = _DESCRIPTION_END_RE.split(text, maxsplit=1)[0]
    text = re.sub(r"\s*(?:FEE\s*:)?\s*$", "", text, flags=re.IGNORECASE).strip(" :-")
    return text or match.group(0)


def _periods(block_text: str) -> dict[str, list[str]]:
    """Months listed in parentheses, by the PAID/DUE marker after them.
    Groups with no marker are "unmarked"."""
    out: dict[str, list[str]] = {"paid": [], "due": [], "unmarked": []}
    for group in _PERIOD_GROUP_RE.finditer(block_text):
        months = _months(group.group(1))
        if months:
            out[(group.group(2) or "unmarked").lower()].extend(months)
    return out


def parse_rate_quantity_lines(ocr_text: str | None) -> list[dict[str, Any]]:
    """Every "<rate> PER <unit> X <qty> <unit>(S) = <amount>" line in the text,
    as line items (numbers as printed)."""
    if not ocr_text:
        return []
    lines = ocr_text.splitlines()
    items: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        for match in _RATE_QTY_RE.finditer(line):
            block = _block_for(lines, index)
            block_text = " ".join(block)
            # Status words belong to this item: drop the "PER WEEK"/rate text itself.
            paid, due = bool(_PAID_RE.search(block_text)), bool(_DUE_RE.search(block_text))
            status = "paid" if paid and not due else "due" if due else None
            periods = _periods(block_text)
            items.append(
                {
                    "description": _description(block, match),
                    "quantity": float(match.group("qty")),
                    "unit_price": _number(match.group("rate")),
                    "discount": None,
                    "line_total": _number(match.group("total")),
                    "line_total_raw_text": match.group("total"),
                    "counts_toward_total": status != "paid",
                    "confidence": 1.0,
                    "uncertain": False,
                    "unit": match.group("unit").upper(),
                    "status": status,
                    "paid_periods": periods["paid"],
                    "due_periods": periods["due"],
                    "unmarked_periods": periods["unmarked"],
                    "printed_text": match.group(0),
                    "source": "text_pattern",
                }
            )
    return items


def _table_grid(table: Any) -> dict[int, dict[int, str]]:
    cells = table.cells if hasattr(table, "cells") else table.get("cells", [])
    grid: dict[int, dict[int, str]] = {}
    for cell in cells:
        grid.setdefault(cell["row_index"], {})[cell["column_index"]] = (cell.get("content") or "").strip()
    return grid


def parse_single_row_tables(tables: list[Any] | None) -> dict[str, Any] | None:
    """Charges laid out as the columns of a one-row table. Returns
    {"line_items", "tax_amount", "tax_rate", "total", "subtotal"} (each None
    when absent), or None when no table has that shape."""
    for table in tables or []:
        grid = _table_grid(table)
        if len(grid) != 2:
            continue
        header, row = grid[min(grid)], grid[max(grid)]
        items: list[dict[str, Any]] = []
        found: dict[str, Any] = {"tax_amount": None, "tax_rate": None, "total": None, "subtotal": None}
        for col in sorted(header):
            label, cell = header[col], row.get(col, "")
            number = _CELL_NUMBER_RE.match(cell)
            if not label or not number:
                continue
            amount = _number(number.group(1))
            if _SUBTOTAL_HEADER_RE.search(label):
                found["subtotal"] = (amount, cell)
            elif _TOTAL_HEADER_RE.search(label):
                found["total"] = (amount, cell)
            elif _TAX_HEADER_RE.search(label):
                found["tax_amount"] = (amount, cell)
                rate = _RATE_IN_HEADER_RE.search(label)
                if rate:
                    found["tax_rate"] = (float(rate.group(1)), label)
            else:
                items.append(
                    {
                        "description": label,
                        "quantity": None,
                        "unit_price": None,
                        "discount": None,
                        "line_total": amount,
                        "line_total_raw_text": cell,
                        "counts_toward_total": True,
                        "confidence": 1.0,
                        "uncertain": False,
                        "status": None,
                        "source": "table",
                    }
                )
        if found["total"] is not None and len(items) >= _MIN_TABLE_CHARGE_COLUMNS:
            return {"line_items": items, **found}
    return None


def _stacked_columns(header: dict[int, str]) -> tuple[int, int] | None:
    """(description column, amount column) of a header row, if it has both."""
    description = next((c for c in sorted(header) if _DESCRIPTION_HEADER_RE.search(header[c])), None)
    amount = next(
        (c for c in sorted(header) if c != description and _AMOUNT_HEADER_RE.search(header[c])), None
    )
    return (description, amount) if description is not None and amount is not None else None


def parse_stacked_amount_tables(tables: list[Any] | None) -> dict[str, Any] | None:
    """Line items of a "Description | Amount" table whose rows OCR stacked
    into shared cells (module docstring). Returns {"line_items", "total"}, or
    None when no table has that shape."""
    for table in tables or []:
        grid = _table_grid(table)
        rows = sorted(grid)
        for position, header_row in enumerate(rows):
            columns = _stacked_columns(grid[header_row])
            if columns is None:
                continue
            description_col, amount_col = columns
            descriptions: list[str] = []
            amounts: list[str] = []
            total = None
            shaped = True
            stacked = False
            for row in rows[position + 1 :]:
                label = grid[row].get(description_col, "")
                cell = grid[row].get(amount_col, "")
                if cell and not _AMOUNTS_CELL_RE.match(cell):
                    shaped = False
                    break
                values = _AMOUNT_IN_CELL_RE.findall(cell)
                if _TOTAL_HEADER_RE.search(label) or _SUBTOTAL_HEADER_RE.search(label) or _TAX_HEADER_RE.search(label):
                    if _TOTAL_HEADER_RE.search(label) and not _SUBTOTAL_HEADER_RE.search(label) and len(values) == 1:
                        total = (_number(values[0]), cell)
                    break
                lines = [line.strip() for line in label.splitlines() if line.strip()]
                stacked = stacked or len(values) > 1 or len(lines) > 1
                descriptions.extend(lines)
                amounts.extend(values)
            if not shaped or not stacked or len(descriptions) < 2 or len(descriptions) != len(amounts):
                continue
            items = [
                {
                    "description": description,
                    "quantity": None,
                    "unit_price": None,
                    "discount": None,
                    "line_total": _number(raw),
                    "line_total_raw_text": raw,
                    "counts_toward_total": True,
                    "confidence": 1.0,
                    "uncertain": False,
                    "status": None,
                    "source": "table",
                }
                for description, raw in zip(descriptions, amounts)
            ]
            return {"line_items": items, "total": total}
    return None


# --- multi-column line-item tables ------------------------------------------------
# "Description | Price | Discount | Net Price | VAT % | VAT Amount | Total
# Price": every row carries a value per column, and each column has its own
# total row ("Total Net Price", "VAT", "Total Price"). The columns are read
# separately and never mixed: a row's Net Price is not its Total Price.

# Column roles, by header (checked in this order).
_COLUMN_ROLES: list[tuple[str, re.Pattern]] = [
    ("total", re.compile(r"\btotal\b", re.IGNORECASE)),
    ("vat", re.compile(r"\b(?:vat|tax|gst)\b(?!.*%)", re.IGNORECASE)),
    ("net", re.compile(r"\bnet\b", re.IGNORECASE)),
    ("discount", re.compile(r"\bdisc(?:ount)?\b", re.IGNORECASE)),
    ("price", re.compile(r"\b(?:price|amount|amt|rate|value|gross)\b", re.IGNORECASE)),
]
_PERCENT_HEADER_RE = re.compile(r"%|\brate\b", re.IGNORECASE)
# Column total rows, by label: role -> pattern.
_COLUMN_TOTAL_LABELS: dict[str, re.Pattern] = {
    "net": re.compile(r"^\W*(?:total\s+net(?:\s+(?:price|amount))?|net\s+total|sub\s*-?\s*total)\W*$", re.IGNORECASE),
    "vat": re.compile(r"^\W*(?:total\s+)?(?:vat|tax|gst)(?:\s+amount)?(?:\s*\(?\s*\d+(?:\.\d+)?\s*%\s*\)?)?\W*$", re.IGNORECASE),
    "total": re.compile(r"^\W*(?:grand\s+)?total(?:\s+(?:price|amount|incl\.?\s*vat))?\W*$", re.IGNORECASE),
}
# OCR reads a printed 0 as D or O in light print ("D.DD").
_OCR_ZERO_RE = re.compile(r"^-?[\dDO][\dDO,.\s]*$")
_COMMA_DECIMAL_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})*,\d{2}$")


def _cell_amount(cell: str) -> float | None:
    """A table cell's number, allowing OCR's D/O for 0 and a comma typed for
    the decimal point ("131,900,00"); None when the cell is not a number."""
    text = (cell or "").strip().replace(" ", "")
    if not text or not _OCR_ZERO_RE.match(text):
        return None
    text = text.replace("D", "0").replace("O", "0")
    if _COMMA_DECIMAL_RE.match(text):
        text = text[: text.rfind(",")] + "." + text[text.rfind(",") + 1 :]
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _column_roles(header: dict[int, str]) -> dict[int, str]:
    roles: dict[int, str] = {}
    for col in sorted(header):
        label = header[col]
        if _PERCENT_HEADER_RE.search(label) and not re.search(r"\bamount\b", label, re.IGNORECASE):
            continue  # the rate column ("VAT 5%"), not an amount
        role = next((name for name, pattern in _COLUMN_ROLES if pattern.search(label)), None)
        if role and role not in roles.values():
            roles[col] = role
    return roles


def parse_multi_column_tables(tables: list[Any] | None) -> dict[str, Any] | None:
    """Line items of a table with separate amount columns (net / VAT / total
    and the like), each row's values kept per column in `columns`, plus the
    column totals printed in label/value rows ("Total Net Price 120,007.50").
    Returns {"line_items", "column_totals"}, or None when no table has that
    shape (at least a total or net column and one more amount column)."""
    for table in tables or []:
        grid = _table_grid(table)
        rows = sorted(grid)
        for position, header_row in enumerate(rows):
            header = grid[header_row]
            roles = _column_roles(header)
            if len(roles) < 3 or not ({"net", "total"} & set(roles.values())):
                continue
            description_col = next(
                (c for c in sorted(header) if _DESCRIPTION_HEADER_RE.search(header[c])), min(header)
            )
            items = []
            for row in rows[position + 1 :]:
                label = grid[row].get(description_col, "").strip()
                if not label or _TOTAL_HEADER_RE.search(label) or _SUBTOTAL_HEADER_RE.search(label):
                    break
                columns = {role: _cell_amount(grid[row].get(col, "")) for col, role in roles.items()}
                columns = {role: value for role, value in columns.items() if value is not None}
                if not columns:
                    continue
                line_total = columns.get("net", columns.get("total"))
                if line_total is None:
                    continue
                items.append({
                    "description": label,
                    "quantity": None,
                    "unit_price": None,
                    "discount": None,
                    "line_total": line_total,
                    "line_total_raw_text": grid[row].get(
                        next(c for c, r in roles.items() if r == ("net" if "net" in columns else "total")), ""
                    ),
                    "columns": columns,
                    "column_headers": {role: header[col] for col, role in roles.items()},
                    "counts_toward_total": True,
                    "confidence": 1.0,
                    "uncertain": False,
                    "status": None,
                    "source": "table_columns",
                })
            if len(items) >= 2:
                return {"line_items": items, "column_totals": _column_totals(tables)}
    return None


def _column_totals(tables: list[Any] | None) -> dict[str, dict[str, Any]]:
    """{role: {"label", "value"}} from label/value rows of any table."""
    found: dict[str, dict[str, Any]] = {}
    for table in tables or []:
        for cells in _table_grid(table).values():
            ordered = [cells[c] for c in sorted(cells)]
            for label, value in zip(ordered, ordered[1:]):
                amount = _cell_amount(value)
                if amount is None:
                    continue
                for role, pattern in _COLUMN_TOTAL_LABELS.items():
                    if role not in found and pattern.match(label.strip()):
                        found[role] = {"label": label.strip(), "value": amount}
    return found


def _row_key(description: Any) -> str:
    return re.sub(r"\W+", " ", str(description or "")).casefold().strip()[:30]


def _same_amount(a: Any, b: Any) -> bool:
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) < 0.005


def _printed_status(item: dict[str, Any], lines: list[str]) -> str | None:
    """PAID/DUE as printed on the OCR line that carries this item's
    description and printed total ("1. REGISTRATION FEES: 500 PAID")."""
    description = (item.get("description") or "").strip().casefold()
    raw_total = (item.get("line_total_raw_text") or "").strip()
    if len(description) < 4 or not raw_total:
        return None
    for line in lines:
        folded = line.casefold()
        if description in folded and re.search(rf"(?<![\d,.]){re.escape(raw_total)}(?![\d,]|\.\d)", line):
            paid, due = bool(_PAID_RE.search(line)), bool(_DUE_RE.search(line))
            return "paid" if paid and not due else "due" if due and not paid else None
    return None


def _merge(llm_items: list[dict[str, Any]], parsed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parsed lines replace the LLM line with the same printed total (keeping
    its location on the page); unmatched LLM lines are kept as they were.
    Idempotent: a parsed line already there (fields enriched before) is
    replaced, not added again."""
    merged = [dict(item, source=item.get("source") or "llm") for item in llm_items]
    for item in parsed:
        for i, existing in enumerate(merged):
            same_line = (
                existing["source"] == item["source"]
                and existing.get("description") == item["description"]
                and _same_amount(existing.get("line_total"), item["line_total"])
            )
            if same_line or (existing["source"] == "llm" and _same_amount(existing.get("line_total"), item["line_total"])):
                located = {k: existing[k] for k in ("bounding_box",) if k in existing}
                merged[i] = {**item, **located}
                break
        else:
            merged.append(item)
    return merged


def _set_core(core: dict[str, Any], name: str, value: float, raw: str, *, currency: bool = True) -> None:
    """Fill a core field the extraction left empty (never overwrite one)."""
    field = core.get(name) or {}
    if field.get("value") is not None:
        return
    field = {**field, "value": value, "raw_text": raw, "confidence": 1.0, "uncertain": False}
    if currency:
        field.setdefault("currency", None)
    core[name] = field


def pdf_info(pdf_bytes: bytes | None) -> dict[str, str] | None:
    """Producer, Creator and Title as written, and the file's CreationDate and
    ModDate as ISO 8601 (from the Info dictionary, else XMP)."""
    if not pdf_bytes:
        return None
    try:
        import pikepdf

        from app.services.forensics.metadata_forensics import _parse_pdf_date, _parse_xmp_date

        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            info = pdf.docinfo
            out = {
                key: str(info.get(f"/{key}")).replace("\x00", "").strip()
                for key in ("Producer", "Creator", "Title")
                if info.get(f"/{key}") is not None
            }
            dates = {key: _parse_pdf_date(info.get(f"/{key}")) for key in ("CreationDate", "ModDate")}
            if not all(dates.values()):
                xmp_keys = {"CreationDate": "CreateDate", "ModDate": "ModifyDate"}
                try:
                    with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
                        for key, xmp_key in xmp_keys.items():
                            dates[key] = dates[key] or _parse_xmp_date(meta.get(f"xmp:{xmp_key}"))
                except Exception:  # noqa: BLE001 - a malformed XMP packet: Info dates only
                    pass
            out.update({key: value.isoformat() for key, value in dates.items() if value})
        from app.services.forensics.pdf_facts import raster_image_count

        # How many raster images the pages draw: a scanner watermark on a
        # file with none was typed in (field validation, fake_scan_watermark).
        images = raster_image_count(pdf_bytes)
        if images is not None:
            out["image_count"] = images
        return out
    except Exception:  # noqa: BLE001 - metadata is a nice-to-have here
        return None


def enrich_extracted_fields(
    extracted_fields: dict[str, Any],
    *,
    ocr_text: str | None = None,
    ocr_tables: list[Any] | None = None,
    pdf_bytes: bytes | None = None,
    ocr_pages: list[Any] | None = None,
    signature_regions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A copy of `extracted_fields` with line items read from the OCR result
    where a known layout is found (module docstring), the extraction
    clean-up (form-template dates; signature ink in names — from OCR's
    handwriting marks in `ocr_pages` and any detected `signature_regions`),
    and `pdf_info`."""
    from app.services.extraction_postprocess import label_form_template_dates, strip_signature_text

    fields = copy.deepcopy(extracted_fields or {})
    llm_items = [i for i in fields.get("line_items") or [] if isinstance(i, dict)]
    core = fields.setdefault("core_fields", {})

    parsed: list[dict[str, Any]] = []
    columns = parse_multi_column_tables(ocr_tables)
    if columns is not None:
        # The column table replaces the extraction's lines for the same rows
        # (the model mixes columns: one row's Net Price, another's Total).
        names = {_row_key(item["description"]) for item in columns["line_items"]}
        llm_items = [i for i in llm_items if _row_key(i.get("description")) not in names and i.get("source") != "table_columns"]
        parsed.extend(columns["line_items"])
        if columns["column_totals"]:
            fields["column_totals"] = columns["column_totals"]
    table = parse_single_row_tables(ocr_tables) if columns is None else None
    if table is not None:
        parsed.extend(table["line_items"])
        if table["total"] is not None:
            _set_core(core, "amount", *table["total"])
        if table["tax_amount"] is not None:
            _set_core(core, "tax_amount", *table["tax_amount"])
        if table["tax_rate"] is not None:
            _set_core(core, "tax_rate", *table["tax_rate"], currency=False)
        if table["subtotal"] is not None:
            _set_core(core, "subtotal", *table["subtotal"])
    stacked = parse_stacked_amount_tables(ocr_tables) if table is None and columns is None else None
    if stacked is not None:
        parsed.extend(stacked["line_items"])
        if stacked["total"] is not None:
            _set_core(core, "amount", *stacked["total"])
    parsed.extend(parse_rate_quantity_lines(ocr_text))

    if parsed:
        fields["line_items"] = _merge(llm_items, parsed)
    elif llm_items:
        fields["line_items"] = [dict(item, source=item.get("source") or "llm") for item in llm_items]
    lines = (ocr_text or "").splitlines()
    for item in fields.get("line_items") or []:
        if item.get("source") != "llm" or item.get("status"):
            continue
        status = _printed_status(item, lines)
        if status is not None:
            item["status"] = status
            if status == "paid":
                item["counts_toward_total"] = False
    label_form_template_dates(fields, ocr_text)
    strip_signature_text(fields, ocr_pages, signature_regions)
    info = pdf_info(pdf_bytes)
    if info is not None:
        fields["pdf_info"] = info
    return fields
