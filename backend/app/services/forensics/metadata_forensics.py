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
        return str(value)
    if isinstance(value, pikepdf.String):
        return str(value)
    if isinstance(value, pikepdf.Array):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)


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


def _extract_xmp_history(raw_xml: str | None) -> list[HistoryEntry]:
    if not raw_xml:
        return []
    try:
        root = etree.fromstring(raw_xml.encode("utf-8"))
    except etree.XMLSyntaxError:
        return []
    entries: list[HistoryEntry] = []
    for li in root.findall(".//xmpMM:History//rdf:li", namespaces=_XML_NS):
        entries.append(
            HistoryEntry(
                action=li.findtext("stEvt:action", namespaces=_XML_NS),
                when=li.findtext("stEvt:when", namespaces=_XML_NS),
                software_agent=li.findtext("stEvt:softwareAgent", namespaces=_XML_NS),
            )
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


def _detect_javascript_openaction(pdf: pikepdf.Pdf) -> dict[str, Any]:
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
    return {"open_action": open_action, "embedded_javascript": has_js, "javascript_count": js_count}


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
        candidate_objgens = {
            obj.objgen
            for obj in pdf.objects
            if isinstance(obj, pikepdf.Object) and not _is_structural_object(obj)
        }
        orphaned = sorted(candidate_objgens - reachable)
        findings.append(
            Finding(
                finding="orphaned_objects",
                severity="medium" if orphaned else "info",
                description=(
                    f"Found {len(orphaned)} object(s) present in the file but not reachable "
                    "from /Root or /Info — likely leftover remnants from editing software."
                    if orphaned
                    else "No orphaned/unreferenced objects found."
                ),
                data={"count": len(orphaned), "object_numbers": [objgen[0] for objgen in orphaned[:50]]},
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
            agent = (entry.software_agent or "").lower()
            matched_name = next((name for name in editing_names if name in agent), None)
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
        js_flagged = js["open_action"] or js["embedded_javascript"]
        findings.append(
            Finding(
                finding="javascript_or_openaction",
                severity="medium" if js_flagged else "info",
                description=(
                    "This PDF contains an OpenAction and/or embedded JavaScript — unusual "
                    "for a business document."
                    if js_flagged
                    else "No OpenAction or embedded JavaScript found."
                ),
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
