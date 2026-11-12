"""
Pairs each amount written in words with the figure the document prints for
it, read from the OCR layout — applied on top of the LLM extraction in
app/services/line_item_parsing.py's `enrich_extracted_fields`, before the
amount-in-words check in app/services/field_validation_service.py runs.

A document can spell out several amounts (a quotation: the total, the VAT and
the grand total each in words, each beside its own figure). The extraction
model is given one list to fill, but which words belong to which figure is a
question about where things sit on the page, so it is answered here from the
page itself, three ways, in order of how certain each is:

  - Table rows: a row holding one cell with words and one cell with a figure
    ("Total Amount in Words (AED): Twenty-Nine Thousand Six Hundred Dirhams" |
    "Total in AED" | "29,800") pairs them, the other text cells being the
    figure's label.
  - Inline: a line or cell that prints both ("Total: AED 20,500 (Twenty
    Thousand Five Hundred Only)").
  - Page geometry: a words line (with its label line above it, and a wrapped
    continuation below it) and the figure printed in the same row band to
    its right (or left), with the label text between them. When several
    figures share the band — a stacked Subtotal / VAT / Total block — the one
    whose label names the same amount as the words' label is taken, and none
    when that is not unique: a wrong pairing is worse than no pairing, which
    the check falls back from by label.

Only text that reads as ONE English amount (app/services/amount_words.py)
counts as words; nothing is recomputed, every figure is copied as printed.
Each pair records its `source` ("table", "layout" or "llm"), the words'
`bounding_box` and the figure's `figure_bounding_box` where known, and is
stored as `amounts_in_words` in `documents.extracted_fields`.
"""
from __future__ import annotations

import re
from typing import Any

from app.services.amount_words import (
    amount_kind,
    find_amount_words,
    is_words_label,
    parse_english_amount,
    parse_figure,
    reads_as_amount_words,
)

# How far a label line may sit above its words line, in line heights: any
# "Something:" line directly above; a line that says "in words" up to a
# blank line's worth further up ("Grand Total Amount in Words:" / blank /
# "Thirty-One Thousand Eighty Dirhams").
_LABEL_GAP = 1.5
_NAMED_LABEL_GAP = 3.5
# How far below a words line its wrapped continuation may start, in line heights.
_WRAP_GAP = 0.6
# Half a line height above and below the words block still counts as its row.
_BAND_MARGIN = 0.5


def parse_amounts_in_words(
    ocr_text: str | None, ocr_tables: list[Any] | None, ocr_pages: list[Any] | None
) -> list[dict[str, Any]]:
    """Every amount in words the layout shows, each with the figure printed
    for it where one could be paired (module docstring)."""
    pairs: dict[str, dict[str, Any]] = {}
    for pair in _from_tables(ocr_tables):
        _add(pairs, pair)
    for pair in _from_pages(ocr_pages):
        _add(pairs, pair)
    if not pairs and not ocr_pages:
        for pair in _from_text(ocr_text):
            _add(pairs, pair)
    return list(pairs.values())


def merge_amounts_in_words(
    llm_pairs: list[dict[str, Any]], layout_pairs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Layout pairs first (they carry the page's own pairing), then any words
    the model read that the layout did not find. A model pair for the same
    words lends its figure to a layout pair that has none."""
    merged: list[dict[str, Any]] = [dict(p) for p in layout_pairs]
    for llm in llm_pairs:
        if not isinstance(llm, dict) or not (llm.get("text") or "").strip():
            continue
        match = next((p for p in merged if _same_words(p["text"], llm["text"])), None)
        if match is None:
            merged.append(dict(llm, source=llm.get("source") or "llm"))
            continue
        if match.get("figure") is None and _num(llm.get("figure")) is not None:
            match["figure"] = _num(llm.get("figure"))
            match["figure_raw_text"] = llm.get("figure_raw_text")
            match["figure_source"] = "llm"
        if not match.get("label") and llm.get("label"):
            match["label"] = llm["label"]
    return merged


# --- tables -----------------------------------------------------------------------


def _grid(table: Any) -> dict[int, dict[int, str]]:
    cells = table.cells if hasattr(table, "cells") else table.get("cells", [])
    grid: dict[int, dict[int, str]] = {}
    for cell in cells:
        grid.setdefault(cell["row_index"], {})[cell["column_index"]] = (cell.get("content") or "").strip()
    return grid


def _from_tables(tables: list[Any] | None):
    for table in tables or []:
        grid = _grid(table)
        for row in sorted(grid):
            words: list[dict[str, Any]] = []
            figures: list[tuple[float, str]] = []
            labels: list[str] = []
            for col in sorted(grid[row]):
                content = grid[row][col]
                if not content:
                    continue
                found = find_amount_words(content)
                if found is not None:
                    words.append(found)
                    continue
                figure = parse_figure(content)
                if figure is not None:
                    figures.append((figure, content))
                    continue
                labels.append(content)
            if len(words) != 1:
                continue  # a row with several amounts in words: left to the page geometry
            pair = _pair(words[0], "table")
            if pair["figure"] is None and len(figures) == 1:
                pair["figure"], pair["figure_raw_text"] = figures[0]
            if labels:
                pair["figure_label"] = " ".join(labels)
            yield pair


# --- page geometry ---------------------------------------------------------------


def _from_pages(pages: list[Any] | None):
    for page in pages or []:
        lines = [line for line in getattr(page, "lines", None) or [] if (line.text or "").strip()]
        lines.sort(key=lambda line: (round(line.y, 3), line.x))
        page_number = getattr(page, "page_number", 1)
        used: set[int] = set()
        for index, line in enumerate(lines):
            if index in used:
                continue
            found = find_amount_words(line.text)
            if found is None:
                continue
            height = line.height or 0.01
            top, bottom, left, right = line.y, line.y + line.height, line.x, line.x + line.width
            words_box = [line]
            # A words phrase wrapped onto the line below ("... Six Hundred" / "Dirhams Only").
            wrapped = _wrapped_below(lines, index, bottom, left, height) if found["figure"] is None else None
            if wrapped is not None:
                merged = find_amount_words(f"{line.text} {lines[wrapped].text}")
                if merged is not None:
                    found = merged
                    used.add(wrapped)
                    after = lines[wrapped]
                    bottom, right = after.y + after.height, max(right, after.x + after.width)
                    words_box.append(after)
            if found["label"] is None:
                label_line = _label_above(lines, index, left, right, top, height)
                if label_line is not None:
                    found["label"] = label_line.text.rstrip(":").strip()
                    top, left = label_line.y, min(left, label_line.x)
            pair = _pair(found, "layout")
            pair["bounding_box"] = _union_box(page_number, words_box)
            if pair["figure"] is None:
                figure_line, label_text = _figure_beside(lines, index, used, top, bottom, left, right, height, found["label"])
                if figure_line is not None:
                    pair["figure"] = parse_figure(figure_line.text)
                    pair["figure_raw_text"] = figure_line.text.strip()
                    pair["figure_bounding_box"] = _union_box(page_number, [figure_line])
                    if label_text:
                        pair["figure_label"] = label_text
            yield pair


def _wrapped_below(lines: list[Any], index: int, bottom: float, left: float, height: float) -> int | None:
    """The index of the line starting just under the words line, left-aligned
    with it, made only of number/currency/filler words."""
    for j in range(index + 1, len(lines)):
        candidate = lines[j]
        gap = candidate.y - bottom
        if gap > _WRAP_GAP * height:
            break  # sorted by y: nothing closer follows
        if gap >= -0.2 * height and abs(candidate.x - left) <= 0.1 and reads_as_amount_words(candidate.text):
            return j
    return None


def _label_above(lines: list[Any], index: int, left: float, right: float, top: float, height: float):
    best = None
    for candidate in lines[:index]:
        gap = top - (candidate.y + candidate.height)
        if not (-0.2 * height <= gap <= _NAMED_LABEL_GAP * height):
            continue
        if candidate.x >= right or candidate.x + candidate.width <= left:
            continue
        if not is_words_label(candidate.text, strict=gap > _LABEL_GAP * height):
            continue
        if best is None or candidate.y > best.y:
            best = candidate
    return best


def _figure_beside(
    lines: list[Any], index: int, used: set[int], top: float, bottom: float, left: float, right: float,
    height: float, words_label: str | None,
) -> tuple[Any, str | None]:
    """The figure printed in the words' row band, to the right (else left) of
    the block, and the label text sitting between them."""
    band_top, band_bottom = top - _BAND_MARGIN * height, bottom + _BAND_MARGIN * height
    in_band = [
        (i, line) for i, line in enumerate(lines)
        if i != index and i not in used and band_top <= line.y + line.height / 2 <= band_bottom
    ]
    for side in ("right", "left"):
        if side == "right":
            figures = [l for _, l in in_band if l.x >= right - 0.01 and parse_figure(l.text) is not None]
            figures.sort(key=lambda l: l.x)
        else:
            figures = [l for _, l in in_band if l.x + l.width <= left + 0.01 and parse_figure(l.text) is not None]
            figures.sort(key=lambda l: -(l.x + l.width))
        if not figures:
            continue
        labelled = [(figure, _label_between(in_band, figure, left, right, side)) for figure in figures]
        if len(figures) == 1:
            return labelled[0]
        wanted = amount_kind(words_label)
        same = [(figure, label) for figure, label in labelled if wanted and amount_kind(label) == wanted]
        if len(same) == 1:
            return same[0]
        return None, None  # several figures share the row: not ours to guess
    return None, None


def _label_between(in_band: list[tuple[int, Any]], figure: Any, left: float, right: float, side: str) -> str | None:
    """The label text on the figure's own line, between the words block and
    the figure ("Total in AED") — not the labels of other figures stacked in
    the same band."""
    texts = []
    figure_centre = figure.y + figure.height / 2
    for _, line in in_band:
        if line is figure or parse_figure(line.text) is not None or parse_english_amount(line.text) is not None:
            continue
        if abs(line.y + line.height / 2 - figure_centre) > 0.7 * max(line.height, figure.height, 0.004):
            continue
        if side == "right" and right - 0.01 <= line.x and line.x + line.width <= figure.x + 0.01:
            texts.append(line)
        elif side == "left" and figure.x + figure.width - 0.01 <= line.x and line.x + line.width <= left + 0.01:
            texts.append(line)
    texts.sort(key=lambda l: (round(l.y, 3), l.x))
    return " ".join(l.text.strip() for l in texts) or None


def _union_box(page_number: int, lines: list[Any]) -> dict[str, float | int]:
    x0 = min(l.x for l in lines)
    y0 = min(l.y for l in lines)
    x1 = max(l.x + l.width for l in lines)
    y1 = max(l.y + l.height for l in lines)
    return {"page": page_number, "x": round(x0, 4), "y": round(y0, 4), "width": round(x1 - x0, 4), "height": round(y1 - y0, 4)}


# --- plain text (no geometry at all) ---------------------------------------------


def _from_text(text: str | None):
    lines = [line.strip() for line in (text or "").splitlines()]
    for index, line in enumerate(lines):
        if not line:
            continue
        found = find_amount_words(line)
        if found is None:
            continue
        if found["label"] is None and index > 0 and is_words_label(lines[index - 1]):
            found["label"] = lines[index - 1].rstrip(":").strip()
        yield _pair(found, "text")


# --- shared ------------------------------------------------------------------------


def _pair(found: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "label": found["label"],
        "text": found["text"],
        "value": found["value"],
        "figure": found["figure"],
        "figure_raw_text": found["figure_raw_text"],
        "figure_label": None,
        "confidence": 1.0,
        "uncertain": False,
        "source": source,
    }


def _add(pairs: dict[str, dict[str, Any]], pair: dict[str, Any]) -> None:
    key = _norm(pair["text"])
    existing = pairs.get(key)
    if existing is None:
        pairs[key] = pair
        return
    for field in ("figure", "figure_raw_text", "figure_label", "label", "bounding_box", "figure_bounding_box"):
        if existing.get(field) in (None, "") and pair.get(field) not in (None, ""):
            existing[field] = pair[field]


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()


def _same_words(a: str | None, b: str | None) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    va, vb = parse_english_amount(a), parse_english_amount(b)
    return va is not None and va == vb


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
