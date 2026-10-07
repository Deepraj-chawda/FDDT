"""
Short, readable summaries of the checks: what the case page shows first and
the report puts first, ahead of each check's full explanation.

Every stored check result (whatever its shape — a findings list, a dict of
sub-checks, a fixed-key dict) becomes:

    {"headline": "2 issues: Amounts retyped, Totals retyped",
     "items": [{"severity", "title", "text", "detail", "page", "findings",
                "image_png_base64", "hint"}, ...],
     "notes": ["short line", ...]}

  - an item is one problem, in one line with the actual values ("Amounts
    retyped" — "1,450.00 · 16,450.00 · 13,350.00 ×2 in a second copy of Times
    New Roman (digits-only copy); 4,575.00 in the same column is original").
    Findings with one cause are grouped into one item (17 font findings ->
    3 items). `detail` keeps the full explanation(s) the check wrote, for a
    "why?" toggle and the report.
  - notes are info lines (what was searched, why a check was limited).

Computed when read, from the stored results, so cases scored before this
existed read the same way. Nothing here changes a result or a score.

`summarize_document_checks` does one document's checks together: the
stamp-vs-issuer sub-check of field validation is shown with signature /
stamp detection, where the reviewer looks for the stamp.
`risk_reason_short` gives a fired rule a short title and line, from the
summary of the check that fired it.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Any

from app.services.risk_rule_seed import SEED_RULES

Item = dict[str, Any]

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}
_AMOUNT_RE = re.compile(r"^[^\d]{0,4}\d{1,3}(?:,\d{3})*(?:\.\d{1,3})?$|^\d+\.\d{2}$")
_NUMBER_WORDS_RE = re.compile(r"\b(?:thousand|hundred|million|only)\b", re.IGNORECASE)
_VISION_DISCLAIMER_RE = re.compile(r"\s*This is a vision-model judgment.*$", re.DOTALL)
STAMP_SUB_CHECK = "stamp_issuer_consistency"
# Field validation's stamp sub-checks, shown with signature / stamp detection.
STAMP_SUB_CHECKS = (STAMP_SUB_CHECK, "stamp_authenticity", "synthetic_stamp_unsigned")


# --- text helpers ------------------------------------------------------------------


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def _shorten(text: Any, limit: int = 160) -> str:
    text = _clean(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def first_sentence(text: Any, limit: int = 160) -> str:
    """The first sentence of `text`, shortened to `limit` characters."""
    text = _clean(text)
    match = re.search(r"(?<=[^\d\s][.!?])\s+(?=[A-Z'\"(])", text)
    return _shorten(text[: match.start()] if match else text, limit)


def _values(values: list[str], limit: int = 8) -> str:
    """'1,450.00 · 16,450.00 · 13,350.00 ×2' (order kept, repeats counted)."""
    counts = Counter(values)
    unique = list(dict.fromkeys(values))
    parts = [f"{v} ×{counts[v]}" if counts[v] > 1 else v for v in unique[:limit]]
    if len(unique) > limit:
        parts.append(f"+{len(unique) - limit} more")
    return " · ".join(parts)


def _quote(text: Any, limit: int = 40) -> str:
    return f"'{_shorten(text, limit)}'"


def _font_name(name: Any) -> str:
    """'TimesNewRoman,Bold' -> 'Times New Roman Bold'."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(name or ""))
    return " ".join(text.replace(",", " ").replace("-", " ").split())


def _date(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value or "")
    return f"{parsed.day} {parsed.strftime('%b %Y')}"


def _gap(seconds: Any) -> str:
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return ""
    days = seconds / 86400
    if days >= 30:
        return f"{days:.0f} days"
    if days >= 1:
        hours = (seconds - int(days) * 86400) / 3600
        return f"{int(days)} day{'s' if int(days) > 1 else ''} {hours:.0f} h"
    hours = seconds / 3600
    if hours >= 1:
        return f"{hours:.0f} h"
    return f"{seconds / 60:.0f} min"


def _pages(pages: Any) -> str:
    pages = [p.get("page") if isinstance(p, dict) else p for p in pages or []]
    pages = [str(p) for p in pages if p is not None]
    return ("p." if len(pages) == 1 else "pp. ") + ", ".join(pages) if pages else ""


def _item(severity: str, title: str, text: str, findings: list[dict] | None = None, *,
          detail: str | None = None, page: Any = None, **extra: Any) -> Item:
    findings = findings or []
    details = list(dict.fromkeys(_clean(f.get("description")) for f in findings if f.get("description")))
    return {
        "severity": severity,
        "title": title,
        "text": _shorten(text, 220),
        # Findings grouped into one item explain the same thing: the first
        # explanation stands for them all.
        "detail": detail if detail is not None else (
            details[0] + (f" ({len(findings) - 1} more of the same kind.)" if len(findings) > 1 else "") if details else ""
        ),
        "page": page if page is not None else next((f.get("page") for f in findings if f.get("page")), None),
        "findings": sorted({str(f.get("finding")) for f in findings if f.get("finding")}),
        # The explanations of every finding in the item, to tie each finding
        # to its item (the report draws each one); dropped from the API.
        "members": details,
        **extra,
    }


def _worst(findings: list[dict]) -> str:
    return min((f.get("severity") or "info" for f in findings), key=lambda s: _SEVERITY_RANK.get(s, 4))


def _material(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f.get("severity") in ("medium", "high", "low")]


# --- per check type ----------------------------------------------------------------


def _font(findings: list[dict]) -> tuple[list[Item], list[str]]:
    items: list[Item] = []
    notes = [_clean(f.get("description")) for f in findings if f.get("finding") == "font_family_inventory"]

    splits: dict[str, list[dict]] = {}
    for f in findings:
        if f.get("finding") == "font_subset_split":
            splits.setdefault(str((f.get("data") or {}).get("subset")), []).append(f)
    for group in splits.values():
        data = [f.get("data") or {} for f in group]
        texts = [str(d.get("text") or "") for d in data]
        amounts = [t for t in texts if _AMOUNT_RE.match(t)]
        if len(amounts) == len(texts):
            title = "Amounts retyped"
        elif amounts and any(_NUMBER_WORDS_RE.search(t) for t in texts):
            title = "Totals retyped"
        else:
            title = "Text retyped"
        text = f"{_values([_shorten(t, 40) for t in texts])} — in a second copy of {_font_name(data[0].get('font'))}"
        if data[0].get("digits_only_subset"):
            text += " (a digits-only copy)"
        originals = list(dict.fromkeys(d["same_column_as"] for d in data if d.get("same_column_as")))
        rows = list(dict.fromkeys(d["same_row_as"] for d in data if d.get("same_row_as")))
        if originals:
            text += f"; {_values(originals, 3)} in the same column is original"
        elif rows and title == "Text retyped":
            text += f"; {_quote(rows[0])} on the same line is original"
        items.append(_item(_worst(group), title, text, group))

    mismatch: dict[tuple, list[dict]] = {}
    for f in findings:
        if f.get("finding") == "font_inconsistency" and f.get("severity") in ("low", "medium", "high"):
            d = f.get("data") or {}
            key = (d.get("source"), d.get("font") if d.get("source") == "text_layer" else d.get("family"),
                   d.get("expected_font") or d.get("expected_family"), bool(d.get("converted_scan")))
            mismatch.setdefault(key, []).append(f)
    for (source, font, expected, converted), group in mismatch.items():
        texts = [_shorten((f.get("data") or {}).get("text"), 30) for f in group]
        if converted:
            title = "Typed into converted scan"
            text = f"{_values(texts)} in the full font {_font_name(font)} among the converter's {_font_name(expected)}"
            digits = (group[0].get("data") or {}).get("original_subset_digits")
            if digits:
                text += f"; the original digits there were {' '.join(digits)}"
        elif source == "ocr":
            title = "Different font (scan, estimated)"
            text = f"{_values(texts)} look like {font}; the text around them is {expected}"
        else:
            title = "Different font"
            text = f"{_values(texts)} in {_font_name(font)}; the text around it is {expected}"
        if any((f.get("data") or {}).get("size_change") for f in group):
            text += "; a size change inside the number too"
        items.append(_item(_worst(group), title, text, group))

    sizes = [f for f in findings if f.get("finding") == "font_size_inconsistency"]
    for f in sizes:
        d = f.get("data") or {}
        sizes_pt = " / ".join(f"{s:g}" for s in d.get("sizes") or [])
        items.append(_item(f.get("severity") or "high", "Size change inside a number",
                           f"{d.get('text')}: characters at {sizes_pt} pt", [f]))
    return items, notes


def _metadata(findings: list[dict]) -> tuple[list[Item], list[str]]:
    items: list[Item] = []
    for f in findings:
        name, sev, d = f.get("finding"), f.get("severity"), f.get("data") or {}
        if sev not in ("low", "medium", "high"):
            continue
        desc = f.get("description") or ""
        if name == "mod_date_after_creation_date":
            gap = _gap(d.get("gap_seconds")) or first_sentence(desc)
            when = (f" ({_date(d.get('creation_date'))} → {_date(d.get('modified_date'))})"
                    if d.get("creation_date") and d.get("modified_date") else "")
            items.append(_item(sev, "Edited after creation", f"modified {gap} after it was created{when}", [f]))
        elif name == "editing_software_detected":
            items.append(_item(sev, "Image editor used", f"{d.get('value')} in {d.get('field')}", [f]))
        elif name == "pdf_editor_detected":
            items.append(_item(sev, "PDF editor used", f"last saved by {d.get('value')}", [f]))
        elif name == "producer_scrubbed":
            toolkit = re.sub(r"-c\d.*$", "", str(d.get("xmp_toolkit") or "Adobe").split(",")[0]).strip()
            items.append(_item(sev, "Tool names removed",
                               f"metadata written by {_shorten(toolkit, 30)}, but producer, creator and history deleted", [f]))
        elif name == "metadata_entirely_stripped":
            items.append(_item(sev, "Metadata stripped", "no Info or XMP metadata at all", [f]))
        elif name == "editable_text_over_scan":
            fonts = "converter fonts" if d.get("converter_font_pages") else "ordinary fonts"
            items.append(_item(sev, "Scan converted to editable text", f"{_pages(d.get('pages'))}, {fonts}", [f]))
        elif name == "history_scanned_document_edited":
            items.append(_item(sev, "Scanned page edited",
                               f"Acrobat 'Edit scanned document' recorded on {_date(d.get('when'))}", [f]))
        elif name == "incremental_updates_present":
            count = re.search(r"(\d+) incremental", desc)
            items.append(_item(sev, "Saved again after creation",
                               f"{count.group(1) if count else 'some'} later save(s) appended to the file", [f]))
        elif name in ("info_xmp_creation_date_mismatch", "info_xmp_mod_date_mismatch"):
            items.append(_item(sev, "Dates disagree", first_sentence(desc.split(" — ")[0], 140), [f]))
        elif name == "info_xmp_producer_mismatch":
            items.append(_item(sev, "Producer names disagree", first_sentence(desc, 140), [f]))
        elif name == "orphaned_objects":
            items.append(_item(sev, "Leftover objects", f"{d.get('count')} unreachable object(s), typical of edits", [f]))
        elif name == "javascript_or_openaction":
            items.append(_item(sev, "Script or auto-action", first_sentence(desc, 140), [f]))
        elif name == "optional_content_groups":
            items.append(_item(sev, "Hidden layers", f"{d.get('layer_count')} optional layer(s)", [f]))
        elif name == "history_editing_tool_anomaly":
            items.append(_item(sev, "Editor in the history", first_sentence(desc, 140), [f]))
        elif name == "history_edit_after_final_date":
            items.append(_item(sev, "History after the final save", first_sentence(desc, 140), [f]))
        elif name in ("history_shorter_than_revisions", "history_absent_despite_revisions"):
            items.append(_item(sev, "History missing saves", first_sentence(desc, 140), [f]))
        elif name == "web_page_origin":
            signs = []
            if d.get("no_creation_date"):
                signs.append("no creation date")
            if d.get("css_framework"):
                signs.append(f"{d['css_framework']} colours {', '.join((d.get('css_colours') or [])[:4])}")
            items.append(_item(sev, "Built as a web page",
                               f"made by {d.get('engine')} (browser print)" + (f"; {'; '.join(signs)}" if signs else ""), [f]))
        elif name == "pdf_unreadable":
            items.append(_item(sev, "Unreadable PDF", first_sentence(desc, 140), [f]))
        else:
            items.append(_item(sev, _humanize(name), first_sentence(desc, 140), [f]))
    return items, []


def _ghost(findings: list[dict]) -> tuple[list[Item], list[str]]:
    items: list[Item] = []
    notes: list[str] = []
    for f in findings:
        name, d = f.get("finding"), f.get("data") or {}
        page = f.get("page")
        if name == "ghost_deleted_block":
            hint = d.get("vision_hint")
            text = f"{d.get('lines')} line(s) of text erased after the scan was converted, p.{page}"
            if isinstance(hint, dict) and (hint.get("kinds") or hint.get("kind")):
                kind = (hint.get("kinds") or [hint.get("kind")])[0]
                agree = "" if hint.get("answers_agree", True) else ", answers differ"
                text += f" · vision guess: {kind} ({hint.get('confidence', 'low')} confidence{agree})"
            items.append(_item(f.get("severity") or "high", "Text deleted", text, [f],
                               image_png_base64=d.get("crop_png_base64"), hint=hint))
        elif name == "ghost_replaced_line":
            items.append(_item(f.get("severity") or "medium", "Line shortened",
                               f"{_quote(d.get('text'))} — the original ran ~{float(d.get('overhang_pt') or 0):.0f} pt further, p.{page}",
                               [f], image_png_base64=d.get("crop_png_base64")))
        elif name == "ghost_show_through":
            notes.append(f"Faint text on p.{page} reads better mirrored — the back of the page showing through; not scored.")
        elif name == "ghost_near_signature":
            notes.append(f"A faint trace on p.{page} lies over a signature or stamp — its ink, not deleted text; not scored.")
    return items, notes


def _ela(check_type: str, findings: list[dict]) -> tuple[list[Item], list[str]]:
    items: list[Item] = []
    for f in findings:
        name, d, sev, page = f.get("finding"), f.get("data") or {}, f.get("severity"), f.get("page")
        if sev not in ("low", "medium", "high"):
            continue
        where = f"p.{page}" if page else ""
        if name == "recompression_error_region":
            share = d.get("region_area_fraction")
            size = f" ({float(share) * 100:.1f}% of the page)" if share else ""
            items.append(_item(sev, "Possible edited area", f"higher recompression error in one area{size}, {where}", [f]))
        elif name == "anti_forensic_signal":
            items.append(_item(sev, "Smoothing detected", f"possible smoothing to hide edit traces, {where}", [f]))
        elif name == "copy_move_cluster":
            items.append(_item(sev, "Copied region",
                               f"{d.get('matched_pairs')} matching points between two areas, {where}", [f]))
        else:
            items.append(_item(sev, _humanize(name), first_sentence(f.get("description"), 140), [f]))
    return items, []


def _duplicate(findings: list[dict]) -> tuple[list[Item], list[str]]:
    items = []
    templates = [f for f in findings if f.get("finding") == "same_template_page"]
    if templates:
        d = templates[0].get("data") or {}
        differ = [r for r in d.get("field_comparison") or [] if not r.get("equal")]
        what = "; ".join(f"{r['field']} {r['mine']} vs {r['theirs']}" for r in differ[:3])
        pages = ", ".join(str(f.get("page")) for f in templates)
        items.append(_item(
            "low", "Same template, other values",
            f"p.{pages} look like {_quote(d.get('matched_document_filename'), 40)} in {d.get('matched_case_number')} "
            f"but the values differ ({what})",
            templates,
        ))
    for f in findings:
        if f.get("finding") != "near_duplicate_page":
            continue
        d = f.get("data") or {}
        where = f"{_quote(d.get('matched_document_filename'), 50)} in {d.get('matched_case_number')}"
        if d.get("verdict") == "resubmission":
            same = ", ".join(r["field"] for r in d.get("field_comparison") or [])
            items.append(_item(f.get("severity") or "high", "Submitted before",
                               f"p.{f.get('page')} = p.{d.get('matched_page')} of {where}, same {same}", [f]))
            continue
        if d.get("identical_file"):
            items.append(_item(f.get("severity") or "high", "Same file uploaded before", f"identical to {where}", [f]))
        else:
            items.append(_item(f.get("severity") or "high", "Duplicate page",
                               f"p.{f.get('page')} matches p.{d.get('matched_page')} of {where}", [f]))
    return items, []


_VISUAL_TITLES = {
    "visual_font_consistency": "Fonts look inconsistent",
    "visual_text_alignment": "Alignment looks off",
    "visual_color_contrast_consistency": "Colour / contrast differs",
    "visual_resolution_sharpness_consistency": "Sharpness differs",
    "visual_shadow_lighting_consistency": "Lighting differs",
    "ai_generation_assessment": "Possibly AI-generated",
}


def _visual(findings: list[dict]) -> tuple[list[Item], list[str]]:
    items = []
    for f in findings:
        name, sev = f.get("finding"), f.get("severity")
        if sev not in ("low", "medium", "high") or name not in _VISUAL_TITLES:
            continue
        desc = _VISION_DISCLAIMER_RE.sub("", _clean(f.get("description")))
        desc = re.sub(r"^Page \d+\s*[—:-]\s*[^:]*:\s*", "", desc)
        desc = re.sub(r"^Page \d+:\s*the model reports possible signs of AI-generated/synthetic content\s*—\s*", "", desc)
        items.append(_item(sev, _VISUAL_TITLES[name], f"{first_sentence(desc, 150)} (vision model)", [f]))
    notes = ["A vision model's judgment, run twice per page — a probabilistic signal, not pixel analysis."]
    return items, notes


_SUB_CHECKS = {
    # name: (title when flagged, short label when passed)
    "date_in_future": ("Future-dated", "date"),
    "line_item_arithmetic": ("Line item miscalculated", "line items"),
    "subtotal_line_item_consistency": ("Line items don't add up", "subtotal"),
    "total_tax_consistency": ("Total ≠ subtotal + tax", "total and tax"),
    "tax_rate_consistency": ("Wrong tax amount", "tax rate"),
    "amount_in_words_consistency": ("Amount in words differs", "amount in words"),
    "reference_number_format": ("Odd reference number", "reference number"),
    "iban_trn_validation": ("Invalid IBAN / TRN", "IBAN / TRN"),
    "rescan_conflict": ("Printed and re-scanned", "no re-scan"),
    "period_quantity_consistency": ("Months charged ≠ months due", "months charged"),
    "document_date_vs_file_creation": ("Dated after its own file", "date vs file"),
    STAMP_SUB_CHECK: ("Stamp names another organisation", "stamp names the issuer"),
    "installment_consistency": ("Installments don't add up", "installments"),
    "stamp_authenticity": ("Stamp typed into the file", "stamp is an image"),
    "synthetic_stamp_unsigned": ("No signature either", "signed"),
    "fake_scan_watermark": ("Fake scanner line", "scanner line"),
    "tax_invoice_trn": ("Tax invoice without TRN", "TRN on tax invoice"),
    "multiple_invoices": ("Two invoices for one month", "invoices in the file"),
    "per_invoice_checks": ("An invoice in the file doesn't add up", "each invoice"),
    "date_sequence": ("Dates out of order", "date order"),
}


# Passed sub-checks whose result is worth reading, not just ticking off.
_SHOWN_WHEN_PASSED = {
    "date_sequence": "Dates in order", "installment_consistency": "Installments add up",
    "multiple_invoices": "Several invoices", "per_invoice_checks": "Each invoice",
}


def _field_validation(details: dict, exclude: set[str]) -> tuple[list[Item], list[str]]:
    items: list[Item] = []
    passed: list[str] = []
    skipped: list[str] = []
    spelled: list[str] = []
    for name, sub in details.items():
        if not isinstance(sub, dict) or "status" not in sub or name in exclude:
            continue
        title, label = _SUB_CHECKS.get(name, (_humanize(name), _humanize(name).lower()))
        if sub["status"] == "flag":
            items.append(sub_check_item(name, sub))
        elif sub["status"] == "pass" and name in _SHOWN_WHEN_PASSED:
            spelled.append(f"{_SHOWN_WHEN_PASSED[name]}: {first_sentence(sub.get('reason'), 200)}")
        elif sub["status"] == "pass":
            passed.append(label)
        else:
            skipped.append(label)
    notes = list(spelled)
    if passed:
        notes.append("Passed: " + ", ".join(passed) + ".")
    if skipped:
        notes.append("Nothing to compare for: " + ", ".join(skipped) + ".")
    return items, notes


# Flagged sub-check reasons, condensed to their values. Each pattern
# matches the reason field_validation_service.py writes; anything else
# falls back to its first sentence.
_SUB_CHECK_SHORT: dict[str, list[tuple[re.Pattern, Any]]] = {
    "date_in_future": [(re.compile(r"Document date (\S+) is in the future"), lambda m: f"dated {_date(m[1])}, in the future")],
    "document_date_vs_file_creation": [(
        re.compile(r"dated (\S+), but its PDF file was created on (\S+) — (\d+) days? earlier"),
        lambda m: f"dated {_date(m[1])}, but the file was created {_date(m[2])} — {m[3]} days earlier",
    )],
    "amount_in_words_consistency": [(
        re.compile(r"is (\d[\d,]*(?:\.\d+)?) \(read from the words\), but the (.+?) in figures is (\d[\d,]*(?:\.\d+)?)"),
        lambda m: f"words say {m[1]}, the {m[2]} in figures is {m[3]}",
    )],
    "subtotal_line_item_consistency": [(
        re.compile(r"Line items = (\d[\d,]*(?:\.\d+)?), stated (\w+) (\d[\d,]*(?:\.\d+)?), difference (\d[\d,]*(?:\.\d+)?)"),
        lambda m: f"items sum to {m[1]}, the {m[2]} says {m[3]} (difference {m[4]})",
    )],
    "line_item_arithmetic": [(
        re.compile(r"do not add up: (.*)"), lambda m: m[1].replace(", but the line total printed is", ", printed"),
    )],
    "total_tax_consistency": [(
        re.compile(r"Total (\d[\d,]*(?:\.\d+)?) does not match subtotal (\d[\d,]*(?:\.\d+)?) \+ tax (\d[\d,]*(?:\.\d+)?) \(expected (\d[\d,]*(?:\.\d+)?)\)"),
        lambda m: f"total {m[1]} ≠ subtotal {m[2]} + tax {m[3]} (= {m[4]})",
    )],
    "rescan_conflict": [(
        re.compile(r"watermark \('([^']+)'\) but the PDF was produced by an office scanner \(([^)]+)\)"),
        lambda m: f"'{m[1]}' watermark on a page from an office scanner ({_shorten(m[2], 40)})",
    )],
    "installment_consistency": [(re.compile(r"split: (.*)"), lambda m: m[1].rstrip("."))],
    "fake_scan_watermark": [(re.compile(r"The page says '([^']+)'"), lambda m: f"'{m[1]}' printed, but the file has no image at all — nothing was scanned")],
    "stamp_authenticity": [(re.compile(r"is (live text and drawn lines|live text|drawn lines) in the file"), lambda m: f"the stamp is {m[1]}, not an image of an ink stamp")],
    "tax_invoice_trn": [(re.compile(r"Titled '([^']+)' but no TRN"), lambda m: f"titled '{m[1]}', but no TRN anywhere")],
    "tax_rate_consistency": [(re.compile(r"says (\d+(?:\.\d+)?)% tax, but the tax is 0\.00 on the (.+?) \((.+?) would be (.+?)\)"),
                              lambda m: f"labelled {m[1]}% but the tax is 0.00 on the {m[2]} ({m[1]}% would be {m[4]})")],
    "multiple_invoices": [(re.compile(r"(\d+) invoices in one file, (\d+) of them for ([A-Za-z]+ \d{4}): (.*?)\. All"),
                           lambda m: f"{m[2]} of the file's {m[1]} invoices are for {m[3]}: {m[4]}")],
    "date_sequence": [(re.compile(r"out of order: (.*)"), lambda m: m[1].rstrip("."))],
    STAMP_SUB_CHECK: [(
        re.compile(r"The stamp reads '(.+?)', which does not name the issuer '(.+?)' \(similarity (\d+)/100\)", re.DOTALL),
        lambda m: f"stamp reads {_quote(m[1], 50)}; issuer is {_quote(m[2], 50)} ({m[3]}/100 alike)",
    )],
}


def sub_check_item(name: str, sub: dict) -> Item:
    title = _SUB_CHECKS.get(name, (_humanize(name), ""))[0]
    reason = _clean(sub.get("reason"))
    text = None
    for pattern, short in _SUB_CHECK_SHORT.get(name, []):
        match = pattern.search(str(sub.get("reason") or ""))
        if match:
            text = _clean(short(match))
            break
    return _item("medium", title, text or first_sentence(reason, 170), detail=reason,
                 findings=[{"finding": name}], page=None)


def _issuer(result: str, details: dict) -> tuple[str | None, list[Item], list[str]]:
    name = details.get("issuer_name")
    matched, score = details.get("matched_registry_name"), details.get("match_score")
    if result == "not_checked":
        return "Not checked — " + first_sentence(str(details.get("reason") or "").removeprefix("Not checked: "), 120), [], []
    if result == "flag":
        text = f"{_quote(name, 60)} is not in the registry"
        if matched:
            text += f"; closest {_quote(matched, 50)} ({score:.0f}/100)" if isinstance(score, (int, float)) else f"; closest {_quote(matched, 50)}"
        return None, [_item("medium", "Issuer not in registry", text, findings=[{"finding": "issuer_not_in_registry"}],
                            detail=str(details.get("reason") or ""))], []
    if matched:
        score_text = f" ({score:.0f}/100)" if isinstance(score, (int, float)) else ""
        return f"Matched registry entry {_quote(matched, 60)}{score_text}", [], []
    return "Issuer matched the registry", [], []


def _signature(result: str, details: dict, stamp: dict | None) -> tuple[str | None, list[Item], list[str]]:
    detected = [d for d in details.get("detected") or [] if isinstance(d, dict)]
    items: list[Item] = []
    notes: list[str] = []
    for name, sub in (stamp or {}).items():
        if sub.get("status") == "flag":
            items.append(sub_check_item(name, sub))
        elif sub.get("status") == "pass" and name == STAMP_SUB_CHECK:
            notes.append("Stamp names the issuer.")
    if result == "flag":
        items.append(_item("low", "No signature or stamp", "expected on this kind of document, none found",
                           findings=[{"finding": "signature_expected_missing"}], detail=""))
        return None, items, notes
    kinds = Counter(d.get("kind") for d in detected)
    found = [f"{n} {k}{'s' if n > 1 else ''}" for k, n in kinds.items() if k]
    pages = sorted({(d.get("bounding_box") or {}).get("page") for d in detected if d.get("bounding_box")} - {None})
    headline = ("Found " + " and ".join(found) + (f", {_pages(pages)}" if pages else "")) if found else "No signature or stamp found"
    for d in detected:
        if d.get("kind") == "stamp" and d.get("text"):
            notes.append(f"Stamp reads: {_quote(d['text'].replace('★', '*'), 90)}")
    return (None if items else headline), items, notes


def _humanize(name: Any) -> str:
    text = str(name or "").replace("_", " ").strip()
    return text[:1].upper() + text[1:]


# --- assembly ------------------------------------------------------------------------


def summarize_check(
    check_type: str, status: str, result: dict | None, error_message: str | None = None, *,
    stamp_sub_check: dict | None = None, exclude_sub_checks: set[str] | None = None,
) -> dict[str, Any]:
    """{"headline", "items", "notes"} for one stored check (module docstring)."""
    if status == "failed":
        return {"headline": "Failed to run: " + first_sentence(error_message or "unknown error", 120), "items": [], "notes": []}
    if status != "completed" or not isinstance(result, dict):
        return {"headline": "Running…", "items": [], "notes": []}
    outcome = result.get("result")
    details = result.get("details")
    headline: str | None = None
    items: list[Item] = []
    notes: list[str] = []

    if isinstance(details, list):
        findings = [f for f in details if isinstance(f, dict)]
        if check_type == "font_consistency":
            items, notes = _font(findings)
        elif check_type == "metadata_forensics":
            items, notes = _metadata(findings)
        elif check_type == "ghost_content":
            items, notes = _ghost(findings)
            scope = next((f for f in findings if f.get("finding") == "ghost_content_scope"), {})
            if outcome == "not_applicable":
                why = (scope.get("data") or {}).get("reason") or "no page is a scan converted to editable text"
                headline = f"Not applicable — {why}"
            elif not items:
                headline = f"Converted {_pages((scope.get('data') or {}).get('pages'))} searched — nothing deleted or shortened"
        elif check_type in ("error_level_analysis", "copy_move_detection"):
            items, notes = _ela(check_type, findings)
            limited = next((f for f in findings if f.get("finding") == "pixel_analysis_limited"), None)
            if limited is not None and not items:
                pages = _pages((limited.get("data") or {}).get("pages"))
                headline = f"Limited — {pages} is editable text over a scan; this pixel check sees only the image"
        elif check_type == "duplicate_detection":
            items, notes = _duplicate(findings)
        elif check_type == "visual_inconsistency_review":
            items, notes = _visual(findings)
            if outcome == "not_applicable":
                headline = "Not applicable"
        else:
            items = [
                _item(f.get("severity") or "info", _humanize(f.get("finding")), first_sentence(f.get("description"), 150), [f])
                for f in _material(findings)
            ]
    elif isinstance(details, dict):
        if check_type == "field_validation":
            items, notes = _field_validation(details, exclude_sub_checks or set())
        elif check_type == "issuer_verification":
            headline, items, notes = _issuer(outcome or "", details)
        elif check_type == "signature_stamp_detection":
            if outcome == "not_applicable":
                headline = "Not applicable — no pages to check"
            else:
                headline, items, notes = _signature(outcome or "", details, stamp_sub_check)

    items.sort(key=lambda i: _SEVERITY_RANK.get(i["severity"], 4))
    if headline is None:
        scored = [i for i in items if i["severity"] in ("medium", "high")]
        if outcome == "not_applicable":
            why = next(
                (f["data"]["reason"] for f in (details if isinstance(details, list) else [])
                 if isinstance(f, dict) and isinstance(f.get("data"), dict) and f["data"].get("reason")),
                None,
            )
            headline = f"Not applicable — {why}" if why else "Not applicable to this document"
        elif scored:
            titles = list(dict.fromkeys(i["title"] for i in scored))
            headline = f"{len(scored)} issue{'s' if len(scored) > 1 else ''}: " + ", ".join(titles[:3]) + (
                f" +{len(titles) - 3} more" if len(titles) > 3 else "")
        elif items:
            headline = "Shown for review, not scored: " + ", ".join(dict.fromkeys(i["title"] for i in items))
        else:
            headline = "No issues found"
    return {"headline": headline, "items": items, "notes": list(dict.fromkeys(n for n in notes if n))}


def summarize_document_checks(checks: list[Any], *, members: bool = False) -> dict[str, dict[str, Any]]:
    """{check_type: summary} for one document's check rows (objects with
    check_type, status, result, error_message — ORM rows or the API's
    summaries). The stamp-vs-issuer sub-check moves from field validation
    to signature / stamp detection."""
    def value(x: Any) -> str:
        return getattr(x, "value", x)

    by_type = {value(c.check_type): c for c in checks}
    validation = by_type.get("field_validation")
    stamp = None
    if validation is not None and isinstance(getattr(validation, "result", None), dict):
        details = validation.result.get("details")
        if isinstance(details, dict):
            stamp = {n: details[n] for n in STAMP_SUB_CHECKS if isinstance(details.get(n), dict)} or None
    out = {}
    for check_type, c in by_type.items():
        out[check_type] = summarize_check(
            check_type, value(c.status), c.result, getattr(c, "error_message", None),
            stamp_sub_check=stamp if check_type == "signature_stamp_detection" else None,
            exclude_sub_checks=set(STAMP_SUB_CHECKS) if check_type == "field_validation" else None,
        )
        if not members:
            for item in out[check_type]["items"]:
                item.pop("members", None)
    return out


# --- fired rules ---------------------------------------------------------------------------

RULE_TITLES = {
    "font.inconsistency": "Fonts: text retyped",
    "font.inconsistency_scanned": "Fonts: odd font on the scan",
    "content.deleted_ghost_block": "Text deleted after conversion",
    "content.replaced_ghost_line": "Line shortened after conversion",
    "metadata.pdf_unreadable": "Unreadable PDF",
    "metadata.stripped": "Metadata stripped",
    "metadata.editing_software_detected": "Image editor used",
    "metadata.pdf_editor_producer": "PDF editor used",
    "metadata.creation_date_mismatch": "Creation dates disagree",
    "metadata.mod_date_mismatch": "Modification dates disagree",
    "metadata.producer_mismatch": "Producer names disagree",
    "metadata.modified_after_creation": "Edited after creation",
    "metadata.incremental_update_single": "Saved again once",
    "metadata.incremental_update_multiple": "Saved again several times",
    "metadata.history_tool_anomaly": "Editor in the edit history",
    "metadata.history_scanned_doc_edited": "Scanned page edited",
    "metadata.editable_text_over_scan": "Scan converted to editable text",
    "metadata.producer_scrubbed": "Tool names removed",
    "metadata.history_edit_after_final": "History after the final save",
    "metadata.history_shorter_than_revisions": "History missing saves",
    "metadata.history_absent_despite_revisions": "History missing saves",
    "metadata.orphaned_objects": "Leftover objects",
    "metadata.javascript_or_openaction": "Script or auto-action",
    "metadata.rescan_conflict": "Printed and re-scanned",
    "metadata.document_date_after_file_creation": "Dated after its own file",
    "ela.tamper_region_detected": "Possible edited area",
    "ela.anti_forensic_signal": "Smoothing detected",
    "copy_move.cluster_detected": "Copied region",
    "visual.font_inconsistency": "Fonts look inconsistent",
    "visual.alignment_inconsistency": "Alignment looks off",
    "visual.color_contrast_inconsistency": "Colour / contrast differs",
    "visual.sharpness_inconsistency": "Sharpness differs",
    "visual.lighting_inconsistency": "Lighting differs",
    "ai.generated_content_suspected": "Possibly AI-generated",
    "duplicate.cross_case_match": "Submitted before",
    "field.date_in_future": "Future-dated",
    "field.subtotal_line_item_mismatch": "Line items don't add up",
    "field.total_tax_mismatch": "Total ≠ subtotal + tax",
    "field.line_item_arithmetic_mismatch": "Line item miscalculated",
    "field.tax_rate_mismatch": "Wrong tax amount",
    "field.amount_in_words_mismatch": "Amount in words differs",
    "field.iban_trn_validation": "Invalid IBAN / TRN",
    "field.period_quantity_mismatch": "Months charged ≠ months due",
    "field.reference_number_malformed": "Odd reference number",
    "field.installment_sum_mismatch": "Installments don't add up",
    "field.fake_scan_watermark": "Fake scanner line",
    "signature.synthetic_stamp": "Stamp typed into the file",
    "signature.synthetic_stamp_unsigned": "Typed stamp, no signature",
    "field.tax_invoice_without_trn": "Tax invoice without TRN",
    "field.multiple_invoices_same_period": "Two invoices for one month",
    "field.per_invoice_mismatch": "An invoice in the file doesn't add up",
    "metadata.web_page_origin": "Built as a web page",
    "field.date_sequence_inconsistent": "Dates out of order",
    "cross_doc.amount_mismatch": "Amounts differ across documents",
    "cross_doc.date_mismatch": "Dates differ across documents",
    "cross_doc.issuer_mismatch": "Issuers differ across documents",
    "issuer.not_in_registry": "Issuer not in registry",
    "signature.stamp_issuer_mismatch": "Stamp names another organisation",
    "signature.expected_missing": "No signature or stamp",
    "signature.inconsistent_with_reference": "Signature differs from reference",
    "signature.possibly_consistent_only": "Signature only possibly matches",
    "signature.identical_reuse": "Signature image reused",
    "signature.reused_different_signer": "Signature reused for another signer",
}


def _rule_targets(rule_id: str) -> set[str]:
    """Finding / sub-check names a built-in rule fires on."""
    spec = next((s for s in SEED_RULES if s["rule_id"] == rule_id), None)
    condition = (spec or {}).get("condition") or {}
    names = set(condition.get("finding_in") or [])
    for key in ("finding", "sub_check"):
        if condition.get(key):
            names.add(condition[key])
    return names


def risk_reason_short(reason: dict[str, Any], summaries: dict[str, dict[str, Any]] | None) -> tuple[str, str]:
    """(title, short line) for one fired rule. The line is the summary items
    of the check that fired it (its document's), else the rule's own
    reason, shortened."""
    rule_id = str(reason.get("rule_id") or "")
    title = RULE_TITLES.get(rule_id) or _humanize(rule_id.split(".", 1)[-1])
    targets = _rule_targets(rule_id)
    summary = (summaries or {}).get(str(reason.get("check_type") or ""))
    if summary is None and rule_id.startswith("signature.stamp"):
        summary = (summaries or {}).get("signature_stamp_detection")
    if summary and targets:
        matched = [i for i in summary["items"] if targets & set(i["findings"])]
        if len(matched) == 1:
            return title, _shorten(matched[0]["text"], 240)
        if matched:
            # Several groups: each one's values (the part before " — ").
            return title, _shorten("; ".join(i["text"].split(" — ")[0] for i in matched), 240)
    text = re.sub(r"^'[^']*'\s*:?\s*", "", _clean(reason.get("reason")))
    return title, first_sentence(text, 180)
