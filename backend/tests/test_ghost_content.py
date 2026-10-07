"""
Deleted / replaced content ("ghost text") on scans converted to editable text
(app/services/forensics/ghost_content.py).

Synthetic pages: a "scan" image whose text was erased leaving faint ghosts,
with live text drawn on top the way a converter does — then the same page
with a block of ghosts left uncovered (deleted), a ghost running past its
text (shortened), a block of mirrored ghosts (the back of the page showing
through), and the shapes the check must leave alone (a plain scan, a scan
with invisible OCR text, a born-digital page).

Then the acceptance cases, on the real files when present.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pymupdf
import pytest

from app.services.forensics.ghost_content import analyze_ghost_content, exclude_signature_regions

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "sample-documents" / "tampered_test_samples"

DPI = 150
PX = DPI / 72  # image pixels per point
PAPER = 250
GHOST = 236  # erased text leaves a residue about this much darker than the paper
FONT_SIZE = 11
# A phrase dense on the left and sparse on the right, so its orientation can
# be told (see _fake_reader).
PHRASE = "WWWMMWWW account iii"


def _ghost(img: np.ndarray, text: str, x_pt: float, baseline_pt: float, mirrored: bool = False) -> None:
    """Draw `text` faintly into the scan at the place a FONT_SIZE pt line
    with that baseline would be printed."""
    layer = np.zeros_like(img)
    org = (int(x_pt * PX), int(baseline_pt * PX))
    cv2.putText(layer, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.62, 255, 2, cv2.LINE_AA)
    if mirrored:
        (w, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.62, 2)
        x0, x1 = org[0], org[0] + w
        layer[:, x0:x1] = layer[:, x0:x1][:, ::-1]
    img[layer > 0] = np.minimum(img[layer > 0], GHOST + (255 - layer[layer > 0]) // 16)


def _page(
    *,
    covered_rows: int = 18,
    orphan_rows: int = 0,
    orphan_covered: bool = False,
    mirrored_rows: int = 0,
    short_line: bool = False,
    with_image: bool = True,
    text_mode: int = 0,
    with_text: bool = True,
) -> bytes:
    """One A4 page. Rows of ghost text with the same text live on top
    (`covered_rows`), plus optionally a block of `orphan_rows` ghost lines
    (with live text on top too if `orphan_covered`), a block of mirrored
    ghost lines, and a line whose ghost is much longer than its live text."""
    width, height = 595, 842
    img = np.full((int(height * PX), int(width * PX)), PAPER, np.uint8)
    rng = np.random.default_rng(7)
    lines: list[tuple[str, float, float]] = []  # (live text, x, baseline)
    for i in range(covered_rows):
        y = 80 + i * 26
        text = f"Line {i + 1} Tuition fee item {i * 7 + 3}"
        _ghost(img, text, 60, y)
        lines.append((text, 60, y))
    if short_line:
        y = 80 + covered_rows * 26
        _ghost(img, "Registration Fees WWWMMWWW account details", 60, y)
        lines.append(("Registration Fees", 60, y))
    for i in range(orphan_rows):
        y = 620 + i * 18
        _ghost(img, PHRASE, 60, y)
        if orphan_covered:
            lines.append((PHRASE, 60, y))
    for i in range(mirrored_rows):
        _ghost(img, PHRASE, 330, 620 + i * 18, mirrored=True)
    img = np.clip(img.astype(np.int16) + rng.normal(0, 1.2, img.shape).round().astype(np.int16), 0, 255).astype(np.uint8)

    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    if with_image:
        ok, png = cv2.imencode(".png", img)
        page.insert_image(page.rect, stream=png.tobytes())
    if with_text:
        for text, x, y in lines:
            # Hershey glyphs run wider than Helvetica; stretch the live text to the ghost's width.
            (w_px, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.62, 2)
            natural = pymupdf.get_text_length(text, fontname="helv", fontsize=FONT_SIZE)
            page.insert_text(
                (x, y), text, fontsize=FONT_SIZE, fontname="helv", render_mode=text_mode,
                morph=(pymupdf.Point(x, y), pymupdf.Matrix(w_px / PX / natural, 1)),
            )
    data = doc.tobytes()
    doc.close()
    return data


def _fake_reader(image: bytes) -> tuple[str, float]:
    """Stands in for OCR: "reads" PHRASE only when its dense end is on the
    left, as printed — the mirrored crop of correctly printed text reads as
    nothing, and vice versa."""
    gray = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_GRAYSCALE)
    ink = 255 - gray.astype(np.int32)
    cols = np.nonzero(ink.sum(axis=0) > ink.sum(axis=0).max() * 0.05)[0]
    if cols.size == 0:
        return "", 0.0
    left, right = cols.min(), cols.max()
    mid = (left + right) // 2
    return (PHRASE, 0.9) if ink[:, left:mid].sum() > 1.4 * ink[:, mid:right].sum() else ("", 0.0)


def _findings(result: dict, name: str) -> list[dict]:
    return [d for d in result["details"] if d["finding"] == name]


# --- synthetic ------------------------------------------------------------------


def test_converted_page_with_every_ghost_covered_passes():
    result = analyze_ghost_content(_page())
    assert result["result"] == "pass"
    assert [d["finding"] for d in result["details"]] == ["ghost_content_scope"]


def test_ghost_block_with_no_live_text_is_deleted_content():
    result = analyze_ghost_content(_page(orphan_rows=4))
    assert result["result"] == "flag"
    [block] = _findings(result, "ghost_deleted_block")
    assert block["severity"] == "high"
    assert block["data"]["lines"] == 4
    x0, y0, x1, y1 = block["data"]["box_pt"]
    assert 50 <= x0 <= 70 and 600 <= y0 <= 620 and 150 <= x1 <= 260 and 660 <= y1 <= 685
    assert block["data"]["crop_png_base64"]
    assert block["bounding_box"]["page"] == 1


def test_same_block_with_matching_live_text_on_top_is_not_reported():
    result = analyze_ghost_content(_page(orphan_rows=4, orphan_covered=True))
    assert result["result"] == "pass"
    assert not _findings(result, "ghost_deleted_block")


def test_ghost_running_past_its_line_is_a_replaced_line():
    result = analyze_ghost_content(_page(short_line=True))
    [line] = _findings(result, "ghost_replaced_line")
    assert line["severity"] == "medium"
    assert line["data"]["text"] == "Registration Fees"
    assert line["data"]["overhang_pt"] > 60
    assert not _findings(result, "ghost_deleted_block")


def test_mirrored_ghost_block_is_show_through_and_not_scored():
    result = analyze_ghost_content(_page(mirrored_rows=4), read_image=_fake_reader)
    assert result["result"] == "pass"
    [through] = _findings(result, "ghost_show_through")
    assert through["severity"] == "info"
    assert through["data"]["hint"] == PHRASE


def test_deleted_block_reads_as_is_and_keeps_its_hint():
    result = analyze_ghost_content(_page(orphan_rows=4), read_image=_fake_reader)
    [block] = _findings(result, "ghost_deleted_block")
    assert block["data"]["hint"] == PHRASE and block["data"]["hint_confidence"] == 0.9
    assert PHRASE in block["description"]


@pytest.mark.parametrize(
    "kwargs, why",
    [
        ({"with_text": False}, "plain scan: no text layer"),
        ({"text_mode": 3}, "searchable scan: OCR text invisible"),
        ({"with_image": False}, "born-digital page: no page-sized image"),
    ],
)
def test_not_applicable(kwargs, why):
    result = analyze_ghost_content(_page(orphan_rows=4, **kwargs))
    assert result["result"] == "not_applicable", why
    assert [d["finding"] for d in result["details"]] == ["ghost_content_scope"]


def test_ghost_inside_a_detected_signature_or_stamp_is_set_aside():
    result = analyze_ghost_content(_page(orphan_rows=4))
    [block] = _findings(result, "ghost_deleted_block")
    box = block["bounding_box"]
    stamp = {"kind": "stamp", "bounding_box": {**box, "width": box["width"] * 1.2, "height": box["height"] * 1.2}}
    filtered = exclude_signature_regions(result, [stamp])
    assert filtered["result"] == "pass"
    [aside] = _findings(filtered, "ghost_near_signature")
    assert aside["severity"] == "info" and aside["data"]["set_aside_finding"] == "ghost_deleted_block"
    # Elsewhere on the page: unchanged.
    away = {"kind": "signature", "bounding_box": {"page": 1, "x": 0.7, "y": 0.05, "width": 0.1, "height": 0.05}}
    assert exclude_signature_regions(result, [away]) is result


# --- acceptance: the reference cases ---------------------------------------------


def _sample(name: str) -> bytes:
    path = SAMPLES / name
    if not path.exists():
        pytest.skip(f"{path} not present")
    return path.read_bytes()


def test_ajyal_deleted_bank_details_and_shortened_registration_row():
    """CASE-39CB18BF (Ajyal, 6f277532...): the bank-details block under the
    fee table was deleted after conversion; the "Registration Fees" row's
    trace runs on well past its text."""
    result = analyze_ghost_content(_sample("case10.pdf"))
    assert result["result"] == "flag"
    [block] = _findings(result, "ghost_deleted_block")
    x0, y0, x1, y1 = block["data"]["box_pt"]
    assert 25 <= x0 <= 45 and 380 <= y0 <= 400 and 260 <= x1 <= 290 and 470 <= y1 <= 495
    assert 6 <= block["data"]["lines"] <= 7
    [line] = _findings(result, "ghost_replaced_line")
    assert line["data"]["text"] == "Registration Fees"


def test_british_orchard_has_ghosts_but_no_deleted_blocks():
    """CASE-1CEDFDBF / CASE-F6F5FE76 (895a259e...): converted text over its
    ghosts; the shaded cells, the footer the converter garbled and the stamp
    produce nothing."""
    result = analyze_ghost_content(_sample("case5.pdf"))
    assert result["result"] == "pass"
    assert not _findings(result, "ghost_deleted_block")
    assert not _findings(result, "ghost_replaced_line")


def test_ilm_shaded_rows_are_not_deleted_content():
    """CASE-B70765EC (ILM Academy, 14926b7f...)."""
    result = analyze_ghost_content(_sample("case8.pdf"))
    assert result["result"] == "pass"
    assert not _findings(result, "ghost_deleted_block")


@pytest.mark.parametrize(
    "name",
    [
        "case2.pdf",  # CASE-DE627FA2: MFP scan, no visible text layer
        "case1.pdf",  # CASE-DB43653A: MFP scan, no visible text layer
        "case6.pdf",  # CASE-4B297080: born-digital Word export
    ],
)
def test_scans_and_born_digital_files_are_not_applicable(name):
    assert analyze_ghost_content(_sample(name))["result"] == "not_applicable"
