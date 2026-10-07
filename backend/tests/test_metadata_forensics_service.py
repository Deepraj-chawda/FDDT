"""
Unit tests for app/services/forensics/metadata_forensics.py — pure
logic over raw PDF bytes, no DB/Celery/network involved. PDFs are built
in-memory with pikepdf (plus hand-appended incremental updates, built
the same way a real PDF editor would append one) so this suite is fully
self-contained and deterministic — no external files required.
"""
from __future__ import annotations

import io
import re

import pikepdf
import pytest

from app.core.config import settings
from app.services.forensics.metadata_forensics import (
    _parse_pdf_date,
    _parse_xmp_date,
    _walk_xref_chain,
    analyze_pdf_metadata,
)

# --- fixture builders -------------------------------------------------


def _build_pdf(info: dict[str, str] | None = None, xmp_xml: str | None = None, *, linearize: bool = False) -> bytes:
    pdf = pikepdf.new()
    pdf.add_blank_page()
    if info:
        for key, value in info.items():
            pdf.docinfo[key] = value
    if xmp_xml:
        stream = pikepdf.Stream(pdf, xmp_xml.encode("utf-8"))
        stream.Type = pikepdf.Name("/Metadata")
        stream.Subtype = pikepdf.Name("/XML")
        pdf.Root.Metadata = stream
    buf = io.BytesIO()
    pdf.save(buf, linearize=linearize)
    return buf.getvalue()


def _xmp_packet(*, create_date=None, modify_date=None, producer=None, creator_tool=None, history=None) -> str:
    props = []
    if create_date:
        props.append(f"<xmp:CreateDate>{create_date}</xmp:CreateDate>")
    if modify_date:
        props.append(f"<xmp:ModifyDate>{modify_date}</xmp:ModifyDate>")
    if producer:
        props.append(f"<pdf:Producer>{producer}</pdf:Producer>")
    if creator_tool:
        props.append(f"<xmp:CreatorTool>{creator_tool}</xmp:CreatorTool>")
    history_xml = ""
    if history:
        entries = "".join(
            f'<rdf:li rdf:parseType="Resource">'
            f"<stEvt:action>{action}</stEvt:action>"
            f"<stEvt:when>{when}</stEvt:when>"
            f"<stEvt:softwareAgent>{agent}</stEvt:softwareAgent>"
            f"</rdf:li>"
            for action, when, agent in history
        )
        history_xml = f"<xmpMM:History><rdf:Seq>{entries}</rdf:Seq></xmpMM:History>"
    return f"""<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:pdf="http://ns.adobe.com/pdf/1.3/" xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/"
    xmlns:stEvt="http://ns.adobe.com/xap/1.0/sType/ResourceEvent#">
   {"".join(props)}
   {history_xml}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""


def _append_incremental_update(base_bytes: bytes, info_overrides: dict[str, str]) -> bytes:
    """Appends a real incremental update replacing the Info dict object
    — the same physical structure a PDF editor produces when it re-saves
    a file without a full rewrite. Discovers the existing Info/Root
    object numbers and the base file's own final xref offset (for
    /Prev) from the base file itself, so this works on any base built by
    _build_pdf()."""
    pdf = pikepdf.open(io.BytesIO(base_bytes))
    info_objnum, _ = pdf.docinfo.objgen
    root_objnum, _ = pdf.Root.objgen
    size = int(pdf.trailer["/Size"])
    pdf.close()

    idx = base_bytes.rfind(b"startxref")
    prev_offset = int(re.match(rb"\s*(\d+)", base_bytes[idx + len(b"startxref") :]).group(1))

    body = "".join(f"/{k} ({v})" for k, v in info_overrides.items())
    new_obj = f"{info_objnum} 0 obj\n<<{body}>>\nendobj\n".encode("ascii")
    obj_offset = len(base_bytes)
    xref_offset = obj_offset + len(new_obj)
    xref_entry = f"{obj_offset:010d} 00000 n \r\n".encode("ascii")
    xref_table = (
        f"xref\n{info_objnum} 1\n".encode("ascii")
        + xref_entry
        + f"trailer\n<</Size {size}/Root {root_objnum} 0 R/Info {info_objnum} 0 R/Prev {prev_offset}>>\n".encode(
            "ascii"
        )
        + f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return base_bytes + new_obj + xref_table


def _append_orphan_object(base_bytes: bytes) -> bytes:
    """Appends one floating object nothing references — same technique
    as _append_incremental_update, but adding a brand-new object number
    instead of replacing an existing one, and leaving Root/Info
    unchanged so the new object is genuinely unreferenced."""
    pdf = pikepdf.open(io.BytesIO(base_bytes))
    info_objnum, _ = pdf.docinfo.objgen
    root_objnum, _ = pdf.Root.objgen
    size = int(pdf.trailer["/Size"])
    orphan_objnum = size
    pdf.close()

    idx = base_bytes.rfind(b"startxref")
    prev_offset = int(re.match(rb"\s*(\d+)", base_bytes[idx + len(b"startxref") :]).group(1))

    # A font: content an edit can leave behind (a bare dictionary or array
    # would be ignored as trivial).
    new_obj = f"{orphan_objnum} 0 obj\n<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>\nendobj\n".encode("ascii")
    obj_offset = len(base_bytes)
    xref_offset = obj_offset + len(new_obj)
    xref_entry = f"{obj_offset:010d} 00000 n \r\n".encode("ascii")
    xref_table = (
        f"xref\n{orphan_objnum} 1\n".encode("ascii")
        + xref_entry
        + (
            f"trailer\n<</Size {orphan_objnum + 1}/Root {root_objnum} 0 R"
            f"/Info {info_objnum} 0 R/Prev {prev_offset}>>\n"
        ).encode("ascii")
        + f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return base_bytes + new_obj + xref_table


@pytest.fixture(autouse=True)
def _default_forensics_settings(monkeypatch):
    """Pin the configurable thresholds to known values so tests don't
    depend on whatever's in the environment's .env."""
    monkeypatch.setattr(settings, "metadata_forensics_mod_date_threshold_seconds", 1.0)
    monkeypatch.setattr(
        settings,
        "metadata_forensics_editing_software_names",
        "photoshop,gimp,illustrator",
    )


# --- date parsing -------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_iso",
    [
        ("D:20200519103011-07'00'", "2020-05-19T10:30:11-07:00"),
        ("D:20251103185823+08'00'", "2025-11-03T18:58:23+08:00"),
        ("D:20260814182244Z", "2026-08-14T18:22:44+00:00"),
        ("D:20260816173455+05'30'", "2026-08-16T17:34:55+05:30"),
    ],
)
def test_parse_pdf_date(raw, expected_iso):
    assert _parse_pdf_date(raw).isoformat() == expected_iso


def test_parse_pdf_date_invalid_returns_none():
    assert _parse_pdf_date("not a date") is None
    assert _parse_pdf_date(None) is None


def test_parse_xmp_date_iso8601():
    assert _parse_xmp_date("2025-11-03T18:58:23+08:00") is not None
    assert _parse_xmp_date("garbage") is None


# --- clean documents pass with no flags -------------------------------


def test_minimal_pdf_with_some_metadata_passes():
    # Just a Producer, nothing else — WeasyPrint-style minimal output
    # (see one of our own real sample documents) isn't "stripped", just
    # minimal; see test_metadata_entirely_stripped_is_flagged below for
    # the genuinely-empty case, which IS flagged.
    data = _build_pdf(info={"/Producer": "Test Producer"})
    result = analyze_pdf_metadata(data)
    assert result["result"] == "pass"
    assert all(f["severity"] not in ("high", "medium") for f in result["details"])


def test_clean_pdf_with_consistent_info_and_xmp_passes():
    data = _build_pdf(
        info={
            "/Producer": "ReportLab PDF Library",
            "/Creator": "ReportLab",
            "/CreationDate": "D:20260101120000Z",
            "/ModDate": "D:20260101120000Z",
        },
        xmp_xml=_xmp_packet(
            create_date="2026-01-01T12:00:00+00:00",
            modify_date="2026-01-01T12:00:00+00:00",
            producer="ReportLab PDF Library",
        ),
    )
    result = analyze_pdf_metadata(data)
    assert result["result"] == "pass"


def test_every_finding_always_present_even_when_clean():
    # Every numbered check (SPECIFICATION.md's ask) produces a finding entry
    # even when nothing is wrong — "info" severity, not absence.
    data = _build_pdf()
    result = analyze_pdf_metadata(data)
    names = {f["finding"] for f in result["details"]}
    assert {
        "orphaned_objects",
        "info_dictionary",
        "xmp_metadata",
        "revision_count",
        "xmp_history",
        "document_id_chain",
        "javascript_or_openaction",
        "optional_content_groups",
        "digital_signature",
    }.issubset(names)


# --- metadata entirely stripped -------------------------------


def test_metadata_entirely_stripped_is_flagged():
    data = _build_pdf()  # no info, no xmp
    result = analyze_pdf_metadata(data)
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["metadata_entirely_stripped"]["severity"] == "high"
    assert result["result"] == "flag"


def test_minimal_info_present_is_not_stripped():
    data = _build_pdf(info={"/Producer": "WeasyPrint"})
    result = analyze_pdf_metadata(data)
    names = {f["finding"] for f in result["details"]}
    assert "metadata_entirely_stripped" not in names


# --- editing software detection -------------------------------


def test_editing_software_in_info_producer_is_flagged():
    data = _build_pdf(info={"/Producer": "Adobe Photoshop 2024"})
    result = analyze_pdf_metadata(data)
    assert result["result"] == "flag"
    matches = [f for f in result["details"] if f["finding"] == "editing_software_detected"]
    assert len(matches) == 1
    assert matches[0]["severity"] == "high"
    assert matches[0]["data"]["matched_name"] == "photoshop"


def test_editing_software_in_creator_is_flagged():
    data = _build_pdf(info={"/Creator": "GIMP 2.10"})
    result = analyze_pdf_metadata(data)
    assert any(f["finding"] == "editing_software_detected" for f in result["details"])


def test_editing_software_in_xmp_producer_is_flagged():
    data = _build_pdf(xmp_xml=_xmp_packet(producer="Illustrator CC 2024"))
    result = analyze_pdf_metadata(data)
    assert any(f["finding"] == "editing_software_detected" for f in result["details"])


def test_unrelated_producer_is_not_flagged():
    data = _build_pdf(info={"/Producer": "ReportLab PDF Library"})
    result = analyze_pdf_metadata(data)
    assert not any(f["finding"] == "editing_software_detected" for f in result["details"])


# --- ModDate after CreationDate -------------------------------


def test_mod_date_19_days_after_creation_is_flagged():
    data = _build_pdf(
        info={"/CreationDate": "D:20260101120000Z", "/ModDate": "D:20260120120000Z"}
    )
    result = analyze_pdf_metadata(data)
    findings = {f["finding"]: f for f in result["details"]}
    assert "mod_date_after_creation_date" in findings
    assert findings["mod_date_after_creation_date"]["severity"] == "high"
    assert findings["mod_date_after_creation_date"]["data"]["gap_hours"] == pytest.approx(19 * 24, abs=0.01)


def _mod_date_flagged(creation: str, modified: str) -> bool:
    data = _build_pdf(info={"/CreationDate": creation, "/ModDate": modified})
    return any(f["finding"] == "mod_date_after_creation_date" for f in analyze_pdf_metadata(data)["details"])


def test_mod_date_equal_to_or_within_a_second_of_creation_is_not_flagged():
    assert not _mod_date_flagged("D:20260101120000Z", "D:20260101120000Z")  # never modified
    assert not _mod_date_flagged("D:20260101120000Z", "D:20260101120001Z")  # 1s: timestamp jitter


def test_any_modification_after_creation_beyond_a_second_is_flagged_not_just_a_day():
    """Used to need a 24-hour gap; a file modified after it was created is the flag."""
    assert _mod_date_flagged("D:20260101120000Z", "D:20260101120002Z")  # 2 seconds
    assert _mod_date_flagged("D:20260101120000Z", "D:20260101120500Z")  # 5 minutes
    assert _mod_date_flagged("D:20260101120000Z", "D:20260101130000Z")  # 1 hour (was "within threshold")


def test_mod_date_before_creation_is_not_flagged_by_this_rule():
    assert not _mod_date_flagged("D:20260101120000Z", "D:20260101110000Z")


def test_mod_date_tolerance_is_configurable(monkeypatch):
    monkeypatch.setattr(settings, "metadata_forensics_mod_date_threshold_seconds", 60.0)
    assert not _mod_date_flagged("D:20260101120000Z", "D:20260101120030Z")  # 30s < 60s tolerance
    assert _mod_date_flagged("D:20260101120000Z", "D:20260101120200Z")  # 2 min > 60s


# --- Info-vs-XMP consistency -------------------------------


def test_info_xmp_creation_date_mismatch_is_flagged():
    data = _build_pdf(
        info={"/CreationDate": "D:20260101120000Z"},
        xmp_xml=_xmp_packet(create_date="2026-06-01T12:00:00+00:00"),
    )
    result = analyze_pdf_metadata(data)
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["info_xmp_creation_date_mismatch"]["severity"] == "high"


def test_info_xmp_producer_mismatch_is_flagged():
    data = _build_pdf(
        info={"/Producer": "ReportLab"},
        xmp_xml=_xmp_packet(producer="Some Other Tool"),
    )
    result = analyze_pdf_metadata(data)
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["info_xmp_producer_mismatch"]["severity"] == "medium"


def test_info_xmp_matching_dates_are_not_flagged():
    data = _build_pdf(
        info={"/CreationDate": "D:20260101120000Z"},
        xmp_xml=_xmp_packet(create_date="2026-01-01T12:00:00+00:00"),
    )
    result = analyze_pdf_metadata(data)
    assert not any(f["finding"] == "info_xmp_creation_date_mismatch" for f in result["details"])


# --- xmpMM:History -------------------------------


def test_xmp_history_entries_are_extracted():
    data = _build_pdf(
        info={"/ModDate": "D:20260201120000Z"},
        xmp_xml=_xmp_packet(
            history=[
                ("created", "2026-01-01T10:00:00Z", "Acrobat Pro DC 21.1"),
                ("saved", "2026-01-05T14:22:00Z", "Acrobat Pro DC 21.1"),
            ]
        ),
    )
    result = analyze_pdf_metadata(data)
    history_finding = next(f for f in result["details"] if f["finding"] == "xmp_history")
    assert len(history_finding["data"]["entries"]) == 2
    assert history_finding["data"]["entries"][0]["action"] == "created"
    assert history_finding["data"]["entries"][1]["software_agent"] == "Acrobat Pro DC 21.1"


def test_history_editing_tool_anomaly_is_flagged():
    data = _build_pdf(
        xmp_xml=_xmp_packet(
            history=[("edited", "2026-01-05T14:22:00Z", "Adobe Photoshop 24.0 (Windows)")]
        )
    )
    result = analyze_pdf_metadata(data)
    findings = {f["finding"]: f for f in result["details"]}
    assert "history_editing_tool_anomaly" in findings
    assert findings["history_editing_tool_anomaly"]["severity"] == "medium"


def test_history_edit_after_final_date_is_flagged():
    data = _build_pdf(
        info={"/ModDate": "D:20260101120000Z"},
        xmp_xml=_xmp_packet(
            modify_date="2026-01-01T12:00:00+00:00",
            history=[("saved", "2026-06-01T00:00:00Z", "Acrobat Pro DC")],
        ),
    )
    result = analyze_pdf_metadata(data)
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["history_edit_after_final_date"]["severity"] == "high"


def test_history_edit_before_final_date_is_not_flagged():
    data = _build_pdf(
        info={"/ModDate": "D:20260601120000Z"},
        xmp_xml=_xmp_packet(
            modify_date="2026-06-01T12:00:00+00:00",
            history=[("saved", "2026-01-01T00:00:00Z", "Acrobat Pro DC")],
        ),
    )
    result = analyze_pdf_metadata(data)
    assert not any(f["finding"] == "history_edit_after_final_date" for f in result["details"])


# --- revision count / incremental updates -------------------------------


def test_single_revision_pdf_is_not_flagged():
    data = _build_pdf(info={"/Producer": "ReportLab"})
    result = analyze_pdf_metadata(data)
    revision_finding = next(f for f in result["details"] if f["finding"] == "revision_count")
    assert revision_finding["data"]["incremental_update_count"] == 0
    assert not any(f["finding"] == "incremental_updates_present" for f in result["details"])


def test_linearized_pdf_two_sections_is_not_flagged():
    # Linearization legitimately produces a 2-section /Prev chain as
    # part of ONE save — not an incremental update.
    data = _build_pdf(linearize=True)
    assert len(_walk_xref_chain(data)) == 2
    result = analyze_pdf_metadata(data)
    revision_finding = next(f for f in result["details"] if f["finding"] == "revision_count")
    assert revision_finding["data"]["is_linearized"] is True
    assert revision_finding["data"]["incremental_update_count"] == 0
    assert not any(f["finding"] == "incremental_updates_present" for f in result["details"])


def test_genuine_incremental_update_is_flagged():
    base = _build_pdf(info={"/Producer": "ReportLab", "/ModDate": "D:20260101120000Z"})
    updated = _append_incremental_update(base, {"Producer": "Adobe Photoshop 2024", "ModDate": "D:20260120120000Z"})

    # Sanity check: pikepdf reads the NEW value back (the update took).
    pdf = pikepdf.open(io.BytesIO(updated))
    assert str(pdf.docinfo["/Producer"]) == "Adobe Photoshop 2024"
    pdf.close()

    result = analyze_pdf_metadata(updated)
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["revision_count"]["data"]["incremental_update_count"] == 1
    assert findings["incremental_updates_present"]["severity"] == "medium"
    assert result["result"] == "flag"


def test_two_incremental_updates_is_high_severity():
    base = _build_pdf(info={"/Producer": "ReportLab"})
    once = _append_incremental_update(base, {"Producer": "ReportLab"})
    twice = _append_incremental_update(once, {"Producer": "ReportLab"})
    result = analyze_pdf_metadata(twice)
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["revision_count"]["data"]["incremental_update_count"] == 2
    assert findings["incremental_updates_present"]["severity"] == "high"


def test_history_absent_despite_incremental_update_is_flagged():
    base = _build_pdf(info={"/Producer": "ReportLab"})  # no XMP at all
    updated = _append_incremental_update(base, {"Producer": "ReportLab"})
    result = analyze_pdf_metadata(updated)
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["history_absent_despite_revisions"]["severity"] == "medium"


def test_history_shorter_than_revisions_is_flagged():
    base = _build_pdf(xmp_xml=_xmp_packet(history=[("created", "2026-01-01T00:00:00Z", "ReportLab")]))
    once = _append_incremental_update(base, {"Producer": "x"})
    twice = _append_incremental_update(once, {"Producer": "y"})
    result = analyze_pdf_metadata(twice)
    findings = {f["finding"]: f for f in result["details"]}
    # 1 history entry but 2 incremental updates.
    assert findings["history_shorter_than_revisions"]["severity"] == "medium"


# --- orphaned objects -------------------------------


def test_orphaned_object_is_detected():
    # A real Info object must already exist in the base file before
    # appending — pikepdf lazily materializes `docinfo` on first access
    # when a fresh pikepdf.new() has none yet, which makes its object
    # number unstable for this helper's purposes.
    base = _build_pdf(info={"/Producer": "ReportLab"})
    with_orphan = _append_orphan_object(base)
    result = analyze_pdf_metadata(with_orphan)
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["orphaned_objects"]["severity"] == "medium"
    assert findings["orphaned_objects"]["data"]["count"] == 1


def test_no_orphans_in_clean_pdf():
    data = _build_pdf(info={"/Producer": "ReportLab"})
    result = analyze_pdf_metadata(data)
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["orphaned_objects"]["severity"] == "info"
    assert findings["orphaned_objects"]["data"]["count"] == 0


def test_linearized_pdf_structural_objects_are_not_false_positive_orphans():
    # Regression test: xref streams, the linearization dict, and any
    # hint stream must never be counted as "orphaned" — they're
    # structural, discovered by file position rather than by reference,
    # in every linearized PDF (i.e. most real-world Acrobat/Word output).
    data = _build_pdf(info={"/Producer": "ReportLab"}, linearize=True)
    result = analyze_pdf_metadata(data)
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["orphaned_objects"]["data"]["count"] == 0


def test_reading_xmp_does_not_corrupt_orphan_detection():
    # Regression test: pikepdf's open_metadata() re-serializes the XMP
    # packet into a new stream object as soon as its `with` block exits
    # — even in a pure-read context — which would otherwise orphan the
    # original Metadata object on every document that has one.
    data = _build_pdf(info={"/Producer": "ReportLab"}, xmp_xml=_xmp_packet(producer="ReportLab"))
    result = analyze_pdf_metadata(data)
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["orphaned_objects"]["data"]["count"] == 0


# --- JavaScript / OpenAction -------------------------------


def test_openaction_is_flagged():
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name("/JavaScript"), JS="app.alert('hi')")
    buf = io.BytesIO()
    pdf.save(buf)
    result = analyze_pdf_metadata(buf.getvalue())
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["javascript_or_openaction"]["severity"] == "medium"
    assert findings["javascript_or_openaction"]["data"]["open_action"] is True


def test_no_openaction_is_not_flagged():
    data = _build_pdf()
    result = analyze_pdf_metadata(data)
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["javascript_or_openaction"]["severity"] == "info"


def _js_finding(build) -> dict:
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.add_blank_page()
    build(pdf)
    buf = io.BytesIO()
    pdf.save(buf)
    findings = {f["finding"]: f for f in analyze_pdf_metadata(buf.getvalue())["details"]}
    return findings["javascript_or_openaction"]


def test_openaction_destination_array_is_not_flagged():
    # What office scanners write: open on page 1, fit to window.
    finding = _js_finding(lambda pdf: setattr(pdf.Root, "OpenAction", pikepdf.Array([pdf.pages[0].obj, pikepdf.Name.Fit])))
    assert finding["severity"] == "info"
    assert finding["data"]["open_action"] is True
    assert finding["data"]["open_action_kind"] == "destination"
    assert "initial view" in finding["description"]


def test_openaction_xyz_destination_and_internal_goto_are_not_flagged():
    def build(pdf):
        pdf.Root.OpenAction = pikepdf.Dictionary(
            S=pikepdf.Name.GoTo, D=pikepdf.Array([pdf.pages[1].obj, pikepdf.Name.XYZ, 0, 792, 0])
        )

    finding = _js_finding(build)
    assert finding["severity"] == "info"
    assert finding["data"]["open_action_kind"] == "GoTo"


@pytest.mark.parametrize("kind", ["/Launch", "/URI", "/SubmitForm", "/ImportData", "/GoToR", "/GoToE"])
def test_openaction_with_external_action_is_flagged(kind):
    finding = _js_finding(lambda pdf: setattr(pdf.Root, "OpenAction", pikepdf.Dictionary(S=pikepdf.Name(kind))))
    assert finding["severity"] == "medium"
    assert finding["data"]["risky_actions"] == [f"{kind[1:]} action in /OpenAction"]


def test_page_additional_action_launch_is_flagged():
    def build(pdf):
        pdf.pages[1].obj.AA = pikepdf.Dictionary(O=pikepdf.Dictionary(S=pikepdf.Name.Launch, F="cmd.exe"))

    finding = _js_finding(build)
    assert finding["severity"] == "medium"
    assert "Launch action in page 2 /AA /O" in finding["description"]


def test_javascript_chained_after_harmless_goto_is_flagged():
    def build(pdf):
        pdf.Root.OpenAction = pikepdf.Dictionary(
            S=pikepdf.Name.GoTo,
            D=pikepdf.Array([pdf.pages[0].obj, pikepdf.Name.Fit]),
            Next=pikepdf.Dictionary(S=pikepdf.Name.URI, URI="https://example.com"),
        )

    assert _js_finding(build)["severity"] == "medium"


def test_javascript_name_tree_is_flagged_without_openaction():
    def build(pdf):
        script = pdf.make_indirect(pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS="app.alert(1)"))
        pdf.Root.Names = pikepdf.Dictionary(JavaScript=pikepdf.Dictionary(Names=pikepdf.Array(["init", script])))

    finding = _js_finding(build)
    assert finding["severity"] == "medium"
    assert finding["data"]["embedded_javascript"] is True
    assert finding["data"]["open_action"] is False


def test_reference_mfp_scans_openaction_is_not_flagged():
    """CASE-DB43653A / CASE-DE627FA2: Develop ineo+ scans with
    /OpenAction [page 1 /Fit] and nothing executable."""
    from pathlib import Path

    samples = Path(__file__).resolve().parents[2] / "sample-documents" / "tampered_test_samples"
    for name in ("case1.pdf", "case2.pdf"):
        result = analyze_pdf_metadata((samples / name).read_bytes())
        findings = {f["finding"]: f for f in result["details"]}
        assert findings["javascript_or_openaction"]["severity"] == "info", name
        assert result["result"] == "pass", name


# --- Optional Content Groups -------------------------------


def test_optional_content_groups_are_flagged_low():
    pdf = pikepdf.new()
    pdf.add_blank_page()
    ocg = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name("/OCG"), Name="Layer 1"))
    pdf.Root.OCProperties = pikepdf.Dictionary(OCGs=[ocg], D=pikepdf.Dictionary())
    buf = io.BytesIO()
    pdf.save(buf)
    result = analyze_pdf_metadata(buf.getvalue())
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["optional_content_groups"]["severity"] == "low"
    assert findings["optional_content_groups"]["data"]["layer_count"] == 1


def test_no_optional_content_groups_is_not_flagged():
    data = _build_pdf()
    result = analyze_pdf_metadata(data)
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["optional_content_groups"]["severity"] == "info"


# --- document ID chain -------------------------------


def test_document_id_chain_is_always_stored():
    data = _build_pdf(xmp_xml=_xmp_packet())
    result = analyze_pdf_metadata(data)
    findings = {f["finding"]: f for f in result["details"]}
    chain_finding = findings["document_id_chain"]
    assert chain_finding["severity"] == "info"
    assert "trailer_id_original" in chain_finding["data"]
    assert "xmp_document_id" in chain_finding["data"]


# --- digital signature -------------------------------


def test_no_signature_is_info_not_flagged():
    data = _build_pdf()
    result = analyze_pdf_metadata(data)
    findings = {f["finding"]: f for f in result["details"]}
    assert findings["digital_signature"]["severity"] == "info"
    assert findings["digital_signature"]["data"]["has_signature_field"] is False


# --- unreadable file -------------------------------


def test_garbage_bytes_are_reported_as_unreadable():
    result = analyze_pdf_metadata(b"this is not a pdf file at all")
    assert result["result"] == "flag"
    assert result["details"][0]["finding"] == "pdf_unreadable"
    assert result["details"][0]["severity"] == "high"


# --- overall pass/flag aggregation -------------------------------


def test_multiple_flags_still_aggregate_to_single_flag_result():
    data = _build_pdf(
        info={
            "/Producer": "Adobe Photoshop 2024",
            "/CreationDate": "D:20260101120000Z",
            "/ModDate": "D:20260120120000Z",
        }
    )
    result = analyze_pdf_metadata(data)
    assert result["result"] == "flag"
    flagged = [f for f in result["details"] if f["severity"] in ("high", "medium")]
    assert len(flagged) >= 2


def test_indirect_scalar_objects_do_not_crash_orphan_detection():
    # Scanner output often writes a stream's /Length as its own indirect
    # object; pikepdf returns those as plain ints, which have no objgen.
    pdf = pikepdf.new()
    pdf.add_blank_page()
    length = pdf.make_indirect(pikepdf.Object.parse(b"12"))
    pdf.Root.Extra = pikepdf.Dictionary(Length=length)
    buf = io.BytesIO()
    pdf.save(buf)
    result = analyze_pdf_metadata(buf.getvalue())
    assert "pdf_unreadable" not in [d["finding"] for d in result["details"]]
    assert "orphaned_objects" in [d["finding"] for d in result["details"]]
