"""
PDF metadata forensics (SPECIFICATION.md section 3.1 "Metadata/digital
forensics" / 3.2). An exhaustive pass over a PDF's own embedded
metadata — the Info dictionary, the separate XMP metadata stream
(including custom/private namespaces and the xmpMM:History revision
chain), the document ID chain, and the file's actual incremental-
update/xref-revision structure — using pikepdf to read the PDF's own
object model directly, plus PyMuPDF where noted. Deliberately NOT
ExifTool and NOT a page-to-image render: both of those would either
miss the PDF's own embedded metadata entirely or read a re-encoded
copy of it, not the real thing.

PDF-only for now (SPECIFICATION.md's "starting with PDF documents only" scope
note) — see app/tasks/metadata_forensics_task.py for the content-type
gate that enforces this.

Findings are a flat list, not the nested-dict shape app/services/
field_validation_service.py uses (that check has a small, fixed set of
named sub-checks; this one has an open-ended, growing list of
individual signals). Every extracted piece of data becomes its own
entry: `severity: "info"` when it's just data being recorded for later
use (the document ID chain, revision counts, XMP history — SPECIFICATION.md
sections 3.3/6.3 want these stored even with no baseline to compare
against yet), escalated to "low"/"medium"/"high" when something is
actually anomalous. `analyze_pdf_metadata()`'s overall pass/flag verdict
looks only at medium/high findings.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import pikepdf
from lxml import etree

from app.core.config import settings
from app.services.forensics.page_structure import analyze_page_structure

Severity = Literal["info", "low", "medium", "high"]

# --- XMP namespace URIs used below (pikepdf's flat metadata mapping keys
# every property as "{namespace-uri}local-name") ---
_NS_XAP = "http://ns.adobe.com/xap/1.0/"
_NS_MM = "http://ns.adobe.com/xap/1.0/mm/"
_NS_PDF = "http://ns.adobe.com/pdf/1.3/"

_XML_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "xmpMM": _NS_MM,
    "stEvt": "http://ns.adobe.com/xap/1.0/sType/ResourceEvent#",
}

# xmpMM:History actions that record a change to the document's content
# itself, whatever tool wrote them: Acrobat's "Edit scanned document" (the
# scan was converted to editable text and edited).
_CONTENT_EDIT_ACTIONS = {"editedscanneddoc"}

# Two Info/XMP (or XMP-history) dates within this tolerance of each
# other count as "the same", not a mismatch — allows for whole-second
# rounding differences between the two representations.
_DATE_MISMATCH_TOLERANCE = timedelta(minutes=1)


def _xmp_key(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


@dataclass
class Finding:
    finding: str
    severity: Severity
    description: str
    data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "finding": self.finding,
            "severity": self.severity,
            "description": self.description,
        }
        if self.data is not None:
            result["data"] = self.data
        return result


@dataclass
class HistoryEntry:
    action: str | None
    when: str | None
    software_agent: str | None


_PDF_DATE_RE = re.compile(
    r"^D:(?P<year>\d{4})(?P<month>\d{2})?(?P<day>\d{2})?"
    r"(?P<hour>\d{2})?(?P<minute>\d{2})?(?P<second>\d{2})?"
    r"(?P<tz>[Zz]|[+-]\d{2}'?\d{2}'?)?"
)


def _parse_pdf_date(value: Any) -> datetime | None:
    """Parses the PDF spec's native date format (ISO 32000 7.9.4), e.g.
    "D:20251103185823+08'00'" — distinct from XMP's own dates, which are
    already plain ISO 8601 (see _parse_xmp_date)."""
    if not value:
        return None
    match = _PDF_DATE_RE.match(str(value).strip())
    if not match:
        return None
    g = match.groupdict()
    tzinfo: timezone = timezone.utc
    tz = g["tz"]
    if tz and tz not in ("Z", "z"):
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace("'", "")
        tz_hour = int(digits[0:2]) if len(digits) >= 2 else 0
        tz_minute = int(digits[2:4]) if len(digits) >= 4 else 0
        tzinfo = timezone(sign * timedelta(hours=tz_hour, minutes=tz_minute))
    try:
        return datetime(
            int(g["year"]),
            int(g["month"] or 1),
            int(g["day"] or 1),
            int(g["hour"] or 0),
            int(g["minute"] or 0),
            int(g["second"] or 0),
            tzinfo=tzinfo,
        )
    except ValueError:
        return None


def _parse_xmp_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, pikepdf.Name):
        return str(value).replace("\x00", "")
    if isinstance(value, pikepdf.String):
        return str(value).replace("\x00", "")
    if isinstance(value, pikepdf.Array):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value).replace("\x00", "")


def _extract_info_dict(pdf: pikepdf.Pdf) -> dict[str, Any]:
    return {str(k): _to_jsonable(v) for k, v in pdf.docinfo.items()}


def _extract_xmp(pdf: pikepdf.Pdf) -> tuple[dict[str, Any], str | None]:
    """Returns (flattened key/value map covering every namespace present
    — not just Dublin Core, e.g. pdfx:/photoshop:/custom namespaces all
    come through — and the raw XMP packet XML, or (empty, None) if the
    document has no XMP metadata stream at all)."""
    try:
        # `set_pikepdf_as_editor`/`update_docinfo` default to True, which
        # rewrites the XMP packet (and syncs docinfo) as a SIDE EFFECT of
        # this `with` block exiting — even though nothing here modifies
        # any field. That's the opposite of what a forensic reader wants
        # (never mutate the evidence, and it was creating a phantom
        # orphaned object — the superseded old Metadata stream — on every
        # real-world file this ran against). Both off: purely read-only.
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
            keys = list(meta.keys())
            if not keys:
                return {}, None
            raw_xml = str(meta)
            flat: dict[str, Any] = {}
            for key in keys:
                # xmpMM:History is a sequence of structured records
                # (action/when/softwareAgent each), not a plain scalar or
                # list of literals — pikepdf's flat mapping can't
                # represent that (it comes back as a list of whitespace
                # text nodes). Extracted properly from the raw XML by
                # _extract_xmp_history instead.
                if key == _xmp_key(_NS_MM, "History"):
                    continue
                try:
                    flat[key] = meta[key]
                except Exception:  # noqa: BLE001 - one malformed property
                    # shouldn't drop the rest of the packet.
                    continue
            return flat, raw_xml
    except Exception:  # noqa: BLE001 - a malformed XMP packet shouldn't
        # crash the whole check; treat as "no XMP" and let
        # metadata_entirely_stripped react to that if Info is also empty.
        return {}, None


_XMP_TOOLKIT_RE = re.compile(rb'x:xmptk\s*=\s*["\']([^"\']*)["\']')


def _xmp_toolkit(pdf: pikepdf.Pdf) -> str:
    """The x:xmptk attribute of the raw XMP packet (the library that wrote
    it, e.g. "Adobe XMP Core 9.1-c001 ..."); "" if none."""
    try:
        raw = pdf.Root.Metadata.read_bytes()
    except Exception:  # noqa: BLE001 - no or unreadable metadata stream
        return ""
    match = _XMP_TOOLKIT_RE.search(raw[:4096])
    return match.group(1).decode("utf-8", "replace").strip() if match else ""


def _extract_xmp_history(raw_xml: str | None) -> list[HistoryEntry]:
    if not raw_xml:
        return []
    try:
        root = etree.fromstring(raw_xml.encode("utf-8"))
    except etree.XMLSyntaxError:
        return []
    entries: list[HistoryEntry] = []
    for li in root.findall(".//xmpMM:History//rdf:li", namespaces=_XML_NS):
        # Written either as child elements or as attributes of the rdf:li.
        def value(name: str) -> str | None:
            return li.findtext(f"stEvt:{name}", namespaces=_XML_NS) or li.get(f"{{{_XML_NS['stEvt']}}}{name}")

        entries.append(
            HistoryEntry(action=value("action"), when=value("when"), software_agent=value("softwareAgent"))
        )
    return entries


def _walk_xref_chain(data: bytes) -> list[int]:
    """Follows the real xref/trailer /Prev chain starting from the
    file's final `startxref`, returning every xref-section offset
    visited (in order). len(result) is the true xref-section count —
    NOT a raw "%%EOF" byte count, which is false-positive prone (that
    string can legitimately appear inside a content/object stream) and
    would also over-count linearized files, which always split their
    xref into 2 sections as part of ONE save (see is_linearized handling
    in analyze_pdf_metadata) — that's normal structure, not an edit."""
    visited: list[int] = []
    idx = data.rfind(b"startxref")
    if idx == -1:
        return visited
    match = re.match(rb"\s*(\d+)", data[idx + len(b"startxref") :])
    if not match:
        return visited
    offset: int | None = int(match.group(1))
    seen: set[int] = set()
    while offset is not None and 0 <= offset < len(data) and offset not in seen:
        seen.add(offset)
        visited.append(offset)
        window = data[offset : offset + 8192]
        if window.lstrip().startswith(b"xref"):
            # Classic xref table — its trailer dict (where /Prev lives)
            # comes after every entry, which can be far past this
            # window for a large document, so locate "trailer" first.
            trailer_idx = data.find(b"trailer", offset)
            if trailer_idx == -1:
                break
            search_window = data[trailer_idx : trailer_idx + 8192]
        else:
            # Cross-reference stream — /Prev lives directly in the
            # object's own dict, right at this offset.
            search_window = window
        prev_match = re.search(rb"/Prev\s+(\d+)", search_window)
        offset = int(prev_match.group(1)) if prev_match else None
    return visited


# Object types that are structural/administrative parts of the PDF
# FILE FORMAT itself (how it's stored/located on disk), not part of the
# document's own logical content graph — nothing in a well-formed
# document ever points to them with a normal indirect reference, because
# readers find them by file position (from startxref/Prev, or from the
# linearization dict's own byte offsets), not by reference. Without this
# exclusion, orphan detection would flag EVERY linearized and/or
# object-stream-compressed PDF (i.e. most real-world Acrobat/Word output)
# as having dozens of "orphaned objects", which is a false positive, not
# a tampering signal — confirmed against a real Acrobat PDFMaker export
# during development.
_STRUCTURAL_OBJECT_TYPES = {"/XRef", "/ObjStm"}
# A linearization hint stream has no /Type of its own — recognized
# instead by having (almost) nothing else: just the handful of keys the
# hint-stream/administrative dictionaries use.
_ADMINISTRATIVE_STREAM_KEYS = {
    "/Filter", "/Length", "/DecodeParms", "/N", "/First", "/O", "/S", "/I", "/C", "/E", "/H", "/T", "/L", "/V",
}


def _is_structural_object(obj: Any) -> bool:
    if not isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
        return False
    obj_type = obj.get("/Type")
    if obj_type is not None and str(obj_type) in _STRUCTURAL_OBJECT_TYPES:
        return True
    if "/Linearized" in obj:
        return True
    if obj_type is None and isinstance(obj, pikepdf.Stream):
        keys = {str(k) for k in obj.keys()}
        if keys and keys.issubset(_ADMINISTRATIVE_STREAM_KEYS):
            return True
    return False


# PDF producers that render an HTML page: a browser's "Save as PDF" or a
# headless HTML-to-PDF engine.
_WEB_ENGINES = (
    "pdfium", "skia/pdf", "chromium", "headlesschrome", "google chrome", "wkhtmltopdf", "puppeteer",
    "playwright", "weasyprint", "prince", "phantomjs", "electron", "dompdf", "mpdf",
)


def _web_page_origin(
    pdf_bytes: bytes, producer_creator: dict[str, str], info: dict[str, Any]
) -> Finding | None:
    """`web_page_origin`: the PDF was made from a web page — its producer is
    a browser's or an HTML engine's — and how strongly it looks built as one:
    no CreationDate (what a browser print leaves) and a CSS framework's
    stock colours. The engine alone is low (shown only: schools' own portals
    print from browsers too); with either sign it is medium."""
    from app.services.forensics.pdf_facts import css_palette

    engine = next(
        (value for value in producer_creator.values() if any(e in value.lower() for e in _WEB_ENGINES)), None
    )
    if engine is None:
        return None
    no_date = not str(info.get("/CreationDate") or "").strip()
    framework, colours = css_palette(pdf_bytes)
    signs = []
    if no_date:
        signs.append("no creation date")
    if framework:
        signs.append(f"{framework} stock colours ({', '.join(colours[:6])})")
    description = (
        f"The PDF was made by {engine!r}, a web browser's or HTML engine's PDF output: the document was "
        "built as a web page and printed, not produced by a billing or school-management system"
        + (f" — {' and '.join(signs)}." if signs else ".")
        + ("" if signs else " Shown for review only: school portals print from browsers too.")
    )
    return Finding(
        finding="web_page_origin",
        severity="medium" if signs else "low",
        description=description,
        data={"engine": engine, "no_creation_date": no_date, "css_framework": framework, "css_colours": colours},
    )


_CONTENT_TYPES = {"/Font", "/Page", "/Annot", "/XObject", "/FontDescriptor"}


def _object_kind(obj: Any) -> str:
    if isinstance(obj, pikepdf.Stream):
        subtype = obj.get("/Subtype")
        return {"/Image": "image", "/Form": "form"}.get(str(subtype), "stream") if subtype is not None else "stream"
    if isinstance(obj, pikepdf.Dictionary):
        kind = str(obj.get("/Type") or obj.get("/Subtype") or "")
        return kind.lstrip("/").lower() or "dictionary"
    return "other"


# A stream this short (or empty once decoded) holds nothing — PDFium leaves
# an empty Form XObject per page.
_EMPTY_STREAM_BYTES = 16


def _is_empty_stream(obj: pikepdf.Stream) -> bool:
    try:
        length = int(obj.get("/Length", 0))
    except (TypeError, ValueError):
        length = 0
    if length <= _EMPTY_STREAM_BYTES:
        return True
    try:
        return len(obj.read_bytes()) == 0
    except Exception:  # noqa: BLE001 - undecodable: count it
        return False


def _is_content_object(obj: Any) -> bool:
    """An orphan that can be deleted content: a non-empty stream, or a font,
    page, annotation or image/form XObject dictionary."""
    if isinstance(obj, pikepdf.Stream):
        return not _is_empty_stream(obj)
    if isinstance(obj, pikepdf.Dictionary):
        return str(obj.get("/Type")) in _CONTENT_TYPES or str(obj.get("/Subtype")) in ("/Image", "/Form")
    return False


def _find_reachable_objgens(pdf: pikepdf.Pdf) -> set[tuple[int, int]]:
    """BFS from the trailer's /Root and /Info — everything a well-formed
    PDF's own object graph should reach from there. Anything in
    `pdf.objects` (every object the current xref table considers live)
    that ISN'T in this set is an orphan: a physically-present,
    addressable object nothing in the document actually points to — a
    common leftover from editing software that replaced content without
    cleaning up the objects it replaced."""
    visited: set[tuple[int, int]] = set()
    stack: list[Any] = []
    trailer = pdf.trailer
    if "/Root" in trailer:
        stack.append(trailer["/Root"])
    if "/Info" in trailer:
        stack.append(trailer["/Info"])
    while stack:
        obj = stack.pop()
        if not isinstance(obj, pikepdf.Object):
            continue
        objgen = obj.objgen
        if objgen != (0, 0):
            if objgen in visited:
                continue
            visited.add(objgen)
        try:
            if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
                stack.extend(obj.values())
            elif isinstance(obj, pikepdf.Array):
                stack.extend(obj)
        except Exception:  # noqa: BLE001 - a malformed sub-object
            # shouldn't abort orphan detection for the rest of the file.
            continue
    return visited


# Action types that run code, open something outside the document, or send
# data out. Anything else an /OpenAction or /AA can hold — a destination
# array ([page /Fit], [page /XYZ ...]) or a /GoTo to one of the document's own
# pages, which scanners and office tools write routinely — only decides what
# the viewer shows first.
_RISKY_ACTION_TYPES = {"/JavaScript", "/Launch", "/URI", "/SubmitForm", "/ImportData", "/GoToR", "/GoToE"}
# How deep to follow /Next action chains and nested /AA dictionaries.
_MAX_ACTION_DEPTH = 8


def _risky_actions(action: Any, where: str, depth: int = 0) -> list[str]:
    """Risky action types (with where they sit) in one /OpenAction or /AA
    entry, following its /Next chain."""
    if depth > _MAX_ACTION_DEPTH or not isinstance(action, pikepdf.Dictionary):
        return []  # a destination array (or anything not an action) is harmless
    found: list[str] = []
    kind = action.get("/S")
    if kind is not None and str(kind) in _RISKY_ACTION_TYPES:
        found.append(f"{str(kind)[1:]} action in {where}")
    elif "/JS" in action:
        found.append(f"JavaScript (/JS) in {where}")
    following = action.get("/Next")
    for item in following if isinstance(following, pikepdf.Array) else [following]:
        found.extend(_risky_actions(item, where, depth + 1))
    return found


def _additional_actions(obj: Any, where: str) -> list[str]:
    aa = obj.get("/AA") if isinstance(obj, pikepdf.Dictionary) else None
    if not isinstance(aa, pikepdf.Dictionary):
        return []
    found: list[str] = []
    for trigger, action in aa.items():
        found.extend(_risky_actions(action, f"{where} {trigger}"))
    return found


def _detect_javascript_openaction(pdf: pikepdf.Pdf) -> dict[str, Any]:
    """Executable or outward-reaching content: a /JavaScript name tree, any
    /JS key, or an /OpenAction or /AA (document, page, form field or
    annotation) whose action type is in _RISKY_ACTION_TYPES. `risky` is the
    verdict; `open_action` only records that an /OpenAction exists."""
    root = pdf.Root
    open_action = "/OpenAction" in root
    has_js = False
    js_count = 0
    names = root.get("/Names")
    if names is not None and "/JavaScript" in names:
        has_js = True
        js_tree = names["/JavaScript"]
        names_array = js_tree.get("/Names") if isinstance(js_tree, pikepdf.Dictionary) else None
        if names_array is not None:
            js_count = len(names_array) // 2

    risky: list[str] = []
    if open_action:
        risky.extend(_risky_actions(root["/OpenAction"], "/OpenAction"))
    risky.extend(_additional_actions(root, "document /AA"))
    for number, page in enumerate(pdf.pages, start=1):
        risky.extend(_additional_actions(page.obj, f"page {number} /AA"))
        for annot in page.obj.get("/Annots") or []:
            if isinstance(annot, pikepdf.Dictionary):
                risky.extend(_risky_actions(annot.get("/A"), f"page {number} annotation /A"))
                risky.extend(_additional_actions(annot, f"page {number} annotation /AA"))
    acroform = root.get("/AcroForm")
    fields = acroform.get("/Fields") if isinstance(acroform, pikepdf.Dictionary) else None
    for field in fields or []:
        risky.extend(_additional_actions(field, "form field /AA"))
    # Any other /JS key anywhere (e.g. an action reached some other way).
    if not risky and not has_js:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Dictionary) and "/JS" in obj:
                risky.append("JavaScript (/JS) in an action object")
                break

    open_action_kind = None
    if open_action:
        target = root["/OpenAction"]
        open_action_kind = (
            "destination" if isinstance(target, pikepdf.Array)
            else str(target.get("/S", "/?"))[1:] if isinstance(target, pikepdf.Dictionary)
            else "other"
        )
    return {
        "open_action": open_action,
        "open_action_kind": open_action_kind,
        "embedded_javascript": has_js,
        "javascript_count": js_count,
        "risky_actions": sorted(set(risky)),
        "risky": has_js or bool(risky),
    }


def _detect_optional_content(pdf: pikepdf.Pdf) -> dict[str, Any]:
    ocp = pdf.Root.get("/OCProperties")
    if ocp is None:
        return {"has_optional_content": False, "layer_count": 0}
    ocgs = ocp.get("/OCGs")
    layer_count = len(ocgs) if ocgs is not None else 0
    return {"has_optional_content": layer_count > 0, "layer_count": layer_count}


def _detect_signature(pdf: pikepdf.Pdf) -> dict[str, Any]:
    try:
        acroform = pdf.acroform
        if not acroform.exists:
            return {"has_signature_field": False, "signed": False, "field_count": 0}
        sig_fields = [f for f in acroform.fields if str(f.get("/FT", "")) == "/Sig"]
    except Exception:  # noqa: BLE001 - a malformed AcroForm shouldn't
        # crash the whole check.
        return {"has_signature_field": False, "signed": False, "field_count": 0}
    signed = any("/V" in f for f in sig_fields)
    return {
        "has_signature_field": len(sig_fields) > 0,
        "signed": signed,
        "field_count": len(sig_fields),
    }


def _document_id_chain(pdf: pikepdf.Pdf, xmp: dict[str, Any]) -> dict[str, Any]:
    """Stored regardless of whether there's a baseline to compare
    against yet — SPECIFICATION.md sections 3.3 (duplicate/lineage analysis)
    and 6.3 (investigation evidence) want these on record."""
    trailer_id = pdf.trailer.get("/ID")
    original_id = current_id = None
    if trailer_id is not None:
        if len(trailer_id) >= 1:
            original_id = bytes(trailer_id[0]).hex()
        if len(trailer_id) >= 2:
            current_id = bytes(trailer_id[1]).hex()
    return {
        "trailer_id_original": original_id,
        "trailer_id_current": current_id,
        "xmp_document_id": xmp.get(_xmp_key(_NS_MM, "DocumentID")),
        "xmp_instance_id": xmp.get(_xmp_key(_NS_MM, "InstanceID")),
        "xmp_original_document_id": xmp.get(_xmp_key(_NS_MM, "OriginalDocumentID")),
    }


def analyze_pdf_metadata(pdf_bytes: bytes) -> dict[str, Any]:
    """Runs the full forensic pass over one PDF's bytes. Returns
    {"result": "pass"|"flag", "details": [finding, ...]} ready to store
    directly in a `document_checks.result` jsonb column."""
    try:
        pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    except Exception as exc:  # noqa: BLE001 - any failure to even open
        # the file as a PDF is itself the finding.
        finding = Finding(
            finding="pdf_unreadable",
            severity="high",
            description=f"Could not open this file as a PDF for forensic analysis: {exc}",
        )
        return {"result": "flag", "details": [finding.to_dict()]}

    findings: list[Finding] = []
    try:
        editing_names = settings.metadata_forensics_editing_software_name_list
        pdf_editor_names = settings.metadata_forensics_pdf_editor_name_list

        # --- #9 orphaned/unreferenced objects — MUST run before anything
        # touches pdf.open_metadata() (used by _extract_xmp below).
        # open_metadata() re-serializes the XMP packet into a brand-new
        # stream object as soon as its `with` block exits, EVEN in a
        # pure-read context (confirmed empirically: neither
        # set_pikepdf_as_editor=False nor update_docinfo=False prevents
        # it) — which orphans the original Metadata object and would
        # make every document with an XMP stream falsely report an
        # orphaned object of its own metadata.
        reachable = _find_reachable_objgens(pdf)
        # Indirect scalars (e.g. a stream's /Length written as its own object,
        # common in scanner output) come back from pikepdf as plain Python
        # ints/floats with no objgen — and an orphaned number cannot hide
        # deleted content, so they are skipped.
        candidates = {
            obj.objgen: obj
            for obj in pdf.objects
            if isinstance(obj, pikepdf.Object) and not _is_structural_object(obj)
        }
        unreachable = sorted(set(candidates) - reachable)
        # Only content can be deleted content: an unreachable stream (page
        # content, an image, an embedded file), font, image, page or
        # annotation. Trivial leftovers — a bare array or number, a plain
        # dictionary (macOS Quartz leaves its page box [0 0 612 792]) — are
        # not counted.
        orphaned = [o for o in unreachable if _is_content_object(candidates[o])]
        trivial = len(unreachable) - len(orphaned)
        findings.append(
            Finding(
                finding="orphaned_objects",
                severity="medium" if orphaned else "info",
                description=(
                    f"Found {len(orphaned)} content object(s) (streams, fonts, images, pages or annotations) present "
                    "in the file but not reachable from /Root or /Info — likely leftover remnants from editing software."
                    if orphaned
                    else "No orphaned content objects found"
                    + (f" ({trivial} trivial unreachable object(s), such as a bare array, ignored)." if trivial else ".")
                ),
                data={
                    "count": len(orphaned),
                    "object_numbers": [objgen[0] for objgen in orphaned[:50]],
                    "kinds": sorted({_object_kind(candidates[o]) for o in orphaned}),
                    "trivial_ignored": trivial,
                },
            )
        )

        # --- #1 Info dictionary, #2 XMP metadata stream ---
        info = _extract_info_dict(pdf)
        xmp, xmp_raw = _extract_xmp(pdf)
        history = _extract_xmp_history(xmp_raw)

        findings.append(
            Finding(
                finding="info_dictionary",
                severity="info",
                description=(
                    f"Info dictionary has {len(info)} field(s)."
                    if info
                    else "Info dictionary is empty."
                ),
                data=info,
            )
        )
        findings.append(
            Finding(
                finding="xmp_metadata",
                severity="info",
                description=(
                    f"XMP metadata stream present with {len(xmp)} field(s)."
                    if xmp
                    else "No XMP metadata stream present."
                ),
                data=xmp,
            )
        )

        # --- #11 metadata entirely stripped ---
        if not info and not xmp:
            findings.append(
                Finding(
                    finding="metadata_entirely_stripped",
                    severity="high",
                    description=(
                        "Both the Info dictionary and the XMP metadata stream are "
                        "completely empty/absent — a fully-scrubbed file is itself a "
                        "red flag, not just an absence of data."
                    ),
                )
            )

        # --- #11 known editing-software names in Producer/Creator ---
        producer_creator_values = {
            "info./Producer": str(info.get("/Producer") or ""),
            "info./Creator": str(info.get("/Creator") or ""),
            "xmp.pdf:Producer": str(xmp.get(_xmp_key(_NS_PDF, "Producer")) or ""),
            "xmp.xmp:CreatorTool": str(xmp.get(_xmp_key(_NS_XAP, "CreatorTool")) or ""),
        }
        for field_label, value in producer_creator_values.items():
            lowered = value.lower()
            matched_name = next((name for name in editing_names if name in lowered), None)
            if matched_name:
                findings.append(
                    Finding(
                        finding="editing_software_detected",
                        severity="high",
                        description=(
                            f"{field_label} mentions '{matched_name}' ({value!r}) — unusual "
                            "authorship for a business document."
                        ),
                        data={"field": field_label, "value": value, "matched_name": matched_name},
                    )
                )
        # PDF editors: one finding however many fields name the same tool.
        pdf_editor_fields = {
            field_label: (value, name)
            for field_label, value in producer_creator_values.items()
            if (name := next((n for n in pdf_editor_names if n in value.lower()), None))
        }
        for name in sorted({name for _, name in pdf_editor_fields.values()}):
            fields = [label for label, (_, n) in pdf_editor_fields.items() if n == name]
            value = pdf_editor_fields[fields[0]][0]
            findings.append(
                Finding(
                    finding="pdf_editor_detected",
                    severity="medium",
                    description=(
                        f"The file was last written by a PDF editor ({value!r} in {', '.join(fields)}), not by a "
                        "scanner or the issuer's billing system — the tool can change the text and numbers of an "
                        "existing PDF."
                    ),
                    data={"fields": fields, "value": value, "matched_name": name},
                )
            )

        # --- producer fields removed: Adobe's XMP library wrote the packet
        # (Acrobat, InDesign, Photoshop ...), and every one of those writes
        # its name into Producer/CreatorTool and its saves into the history.
        # With all of them gone, they were deleted afterwards. A fully empty
        # file is metadata_entirely_stripped above instead. ---
        toolkit = _xmp_toolkit(pdf)
        if (
            xmp
            and toolkit.lower().startswith("adobe xmp core")
            and not any(v.strip() for v in producer_creator_values.values())
            and not history
        ):
            findings.append(
                Finding(
                    finding="producer_scrubbed",
                    severity="medium",
                    description=(
                        f"The XMP metadata was written by Adobe's own library ({toolkit.split(',')[0].strip()}), "
                        "yet Producer, Creator, CreatorTool and the edit history are all missing. Adobe software "
                        "always records its name and saves there, so they were removed afterwards — hiding which "
                        "tool last changed the file."
                    ),
                    data={"xmp_toolkit": toolkit, "empty_fields": list(producer_creator_values), "history": 0},
                )
            )

        # --- #3 Info-vs-XMP consistency ---
        info_created = _parse_pdf_date(info.get("/CreationDate"))
        info_modified = _parse_pdf_date(info.get("/ModDate"))
        xmp_created = _parse_xmp_date(xmp.get(_xmp_key(_NS_XAP, "CreateDate")))
        xmp_modified = _parse_xmp_date(xmp.get(_xmp_key(_NS_XAP, "ModifyDate")))

        if info_created and xmp_created and abs(info_created - xmp_created) > _DATE_MISMATCH_TOLERANCE:
            findings.append(
                Finding(
                    finding="info_xmp_creation_date_mismatch",
                    severity="high",
                    description=(
                        f"Info /CreationDate ({info_created.isoformat()}) disagrees with XMP "
                        f"xmp:CreateDate ({xmp_created.isoformat()}) — tools that scrub or edit "
                        "one often forget to update the other."
                    ),
                )
            )
        if info_modified and xmp_modified and abs(info_modified - xmp_modified) > _DATE_MISMATCH_TOLERANCE:
            findings.append(
                Finding(
                    finding="info_xmp_mod_date_mismatch",
                    severity="high",
                    description=(
                        f"Info /ModDate ({info_modified.isoformat()}) disagrees with XMP "
                        f"xmp:ModifyDate ({xmp_modified.isoformat()})."
                    ),
                )
            )

        info_producer = str(info.get("/Producer") or "").strip()
        xmp_producer = str(xmp.get(_xmp_key(_NS_PDF, "Producer")) or "").strip()
        if info_producer and xmp_producer and info_producer != xmp_producer:
            findings.append(
                Finding(
                    finding="info_xmp_producer_mismatch",
                    severity="medium",
                    description=(
                        f"Info /Producer ({info_producer!r}) disagrees with XMP pdf:Producer "
                        f"({xmp_producer!r})."
                    ),
                )
            )

        # --- #11 ModDate significantly after CreationDate ---
        creation = info_created or xmp_created
        modified = info_modified or xmp_modified
        if creation and modified:
            tolerance = settings.metadata_forensics_mod_date_threshold_seconds
            gap = modified - creation
            if gap > timedelta(seconds=tolerance):
                findings.append(
                    Finding(
                        finding="mod_date_after_creation_date",
                        severity="high",
                        description=(
                            f"The file was modified after it was created: ModDate is {gap} after "
                            f"CreationDate (more than the {tolerance:g}-second timestamp tolerance)."
                        ),
                        data={
                            "creation_date": creation.isoformat(),
                            "modified_date": modified.isoformat(),
                            "gap_seconds": round(gap.total_seconds(), 2),
                            "gap_hours": round(gap.total_seconds() / 3600, 2),
                        },
                    )
                )

        # --- #6 incremental update / revision count (computed before #4
        # so the history findings below can react to it) ---
        xref_offsets = _walk_xref_chain(pdf_bytes)
        revision_count = max(len(xref_offsets), 1)
        is_linearized = bool(pdf.is_linearized)
        # A linearized file legitimately splits its xref into 2 sections
        # (a small first-page one plus the main one) as part of ONE
        # save — expected structure, not an edit — so that's the
        # baseline for linearized files; a non-linearized file's
        # baseline is 1 (a single original write).
        baseline_sections = 2 if is_linearized else 1
        incremental_updates = max(revision_count - baseline_sections, 0)
        findings.append(
            Finding(
                finding="revision_count",
                severity="info",
                description=(
                    f"{revision_count} xref section(s) found via the /Prev chain "
                    f"({'linearized' if is_linearized else 'not linearized'}); "
                    f"{incremental_updates} incremental update(s) beyond the expected baseline."
                ),
                data={
                    "xref_section_count": revision_count,
                    "xref_offsets": xref_offsets,
                    "is_linearized": is_linearized,
                    "incremental_update_count": incremental_updates,
                },
            )
        )
        if incremental_updates > 0:
            findings.append(
                Finding(
                    finding="incremental_updates_present",
                    severity="high" if incremental_updates > 1 else "medium",
                    description=(
                        f"Found {incremental_updates} incremental update(s) beyond the "
                        "baseline expected for this file's structure."
                    ),
                )
            )

        # --- #4 xmpMM:History ---
        history_data = [
            {"action": h.action, "when": h.when, "software_agent": h.software_agent} for h in history
        ]
        findings.append(
            Finding(
                finding="xmp_history",
                severity="info",
                description=(
                    f"xmpMM:History has {len(history)} entr{'y' if len(history) == 1 else 'ies'}."
                    if history
                    else "No xmpMM:History revision chain present."
                ),
                data={"entries": history_data},
            )
        )
        for entry in history:
            if (entry.action or "").lower() in _CONTENT_EDIT_ACTIONS:
                findings.append(
                    Finding(
                        finding="history_scanned_document_edited",
                        severity="high",
                        description=(
                            f"xmpMM:History records '{entry.action}' at {entry.when}"
                            + (f" by {entry.software_agent!r}" if entry.software_agent else "")
                            + " — the scanned page was converted to editable text and edited (Acrobat's "
                            "\"Edit scanned document\")."
                        ),
                        data={"action": entry.action, "when": entry.when, "software_agent": entry.software_agent},
                    )
                )
            agent = (entry.software_agent or "").lower()
            matched_name = next((name for name in editing_names + pdf_editor_names if name in agent), None)
            if matched_name:
                findings.append(
                    Finding(
                        finding="history_editing_tool_anomaly",
                        severity="medium",
                        description=(
                            f"xmpMM:History records an edit by {entry.software_agent!r} "
                            f"({entry.action} at {entry.when}) — an editing tool inconsistent "
                            "with how this document type should have been produced."
                        ),
                        data={"action": entry.action, "when": entry.when, "software_agent": entry.software_agent},
                    )
                )
        if modified:
            history_dates = [d for d in (_parse_xmp_date(h.when) for h in history) if d]
            if history_dates and max(history_dates) > modified + _DATE_MISMATCH_TOLERANCE:
                latest = max(history_dates)
                findings.append(
                    Finding(
                        finding="history_edit_after_final_date",
                        severity="high",
                        description=(
                            f"xmpMM:History's latest recorded edit ({latest.isoformat()}) is "
                            f"after the document's own claimed final ModDate "
                            f"({modified.isoformat()})."
                        ),
                    )
                )
        if incremental_updates > 0:
            if history and len(history) < incremental_updates:
                findings.append(
                    Finding(
                        finding="history_shorter_than_revisions",
                        severity="medium",
                        description=(
                            f"xmpMM:History records only {len(history)} "
                            f"entr{'y' if len(history) == 1 else 'ies'} but the file structure "
                            f"shows {incremental_updates} incremental update(s) — the metadata's "
                            "own history is suspiciously short given what actually happened to "
                            "the file."
                        ),
                    )
                )
            elif not history:
                findings.append(
                    Finding(
                        finding="history_absent_despite_revisions",
                        severity="medium",
                        description=(
                            f"The file shows {incremental_updates} incremental update(s) but has "
                            "no xmpMM:History revision chain at all."
                        ),
                    )
                )

        # --- #5 document ID chain (always stored) ---
        findings.append(
            Finding(
                finding="document_id_chain",
                severity="info",
                description="Document/instance ID values recorded for future duplicate/lineage analysis.",
                data=_document_id_chain(pdf, xmp),
            )
        )

        # --- #7 embedded JavaScript / OpenAction ---
        js = _detect_javascript_openaction(pdf)
        if js["risky"]:
            what = list(js["risky_actions"])
            if js["embedded_javascript"]:
                what.insert(0, f"a document-level JavaScript name tree ({js['javascript_count']} script(s))")
            description = (
                "This PDF contains executable or outward-reaching content — "
                + "; ".join(what)
                + " — unusual for a business document."
            )
        elif js["open_action"]:
            description = (
                f"The PDF's OpenAction only sets the initial view ({js['open_action_kind']}); "
                "no JavaScript, launch, link-out or form-submit action found."
            )
        else:
            description = "No OpenAction or embedded JavaScript found."
        findings.append(
            Finding(
                finding="javascript_or_openaction",
                severity="medium" if js["risky"] else "info",
                description=description,
                data=js,
            )
        )

        # --- #8 Optional Content Groups (hidden layers) ---
        ocg = _detect_optional_content(pdf)
        findings.append(
            Finding(
                finding="optional_content_groups",
                severity="low" if ocg["has_optional_content"] else "info",
                description=(
                    f"This PDF has {ocg['layer_count']} optional content group(s) (layers) "
                    "that could hide or reveal content conditionally."
                    if ocg["has_optional_content"]
                    else "No optional content groups (layers) found."
                ),
                data=ocg,
            )
        )

        # --- #12 editable text over a scan: page structure, independent of
        # the XMP history (still holds when the metadata is stripped) ---
        layered = [p for p in analyze_page_structure(pdf_bytes) if p.vector_text_over_image]
        if layered:
            listed = ", ".join(str(p.page) for p in layered)
            converted = [p.page for p in layered if p.converter_fonts]
            fonts = (
                " in converter-generated fonts (\"-NNNN\" subsets)" if converted
                else " (in ordinary subset fonts, as Acrobat's own editor or a re-saved converted file leaves them)"
            )
            description = (
                f"Page(s) {listed}: a page-sized scan image with visible, editable text drawn on top of it{fonts} — "
                "a scan converted to editable text. A normal searchable scan keeps its OCR text invisible; this "
                "structure is what lets a scan's words and numbers be retyped or deleted."
            )
            findings.append(
                Finding(
                    finding="editable_text_over_scan",
                    severity="medium",
                    description=description,
                    data={"pages": [p.page for p in layered], "converter_font_pages": converted},
                )
            )

        # --- a web page printed to PDF: built as HTML, not by a billing system ---
        web = _web_page_origin(pdf_bytes, producer_creator_values, info)
        if web is not None:
            findings.append(web)

        # --- #10 digital signature presence (data point only) ---
        signature = _detect_signature(pdf)
        findings.append(
            Finding(
                finding="digital_signature",
                severity="info",
                description=(
                    "Signed digital signature field present."
                    if signature["signed"]
                    else "Unsigned signature field present."
                    if signature["has_signature_field"]
                    else "No digital signature field present."
                ),
                data=signature,
            )
        )
    finally:
        pdf.close()

    overall = "flag" if any(f.severity in ("high", "medium") for f in findings) else "pass"
    return {"result": overall, "details": [f.to_dict() for f in findings]}
