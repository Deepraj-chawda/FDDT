"""
Rendering half of the per-case PDF report (data comes from app/services/
case_report_data.py; storage/orchestration is app/services/
case_report_service.py). Pure functions: `ReportData` in, PDF bytes out —
no database, no network.

Report structure (in this order):
  1. Case Details            2. Executive Summary      3. Explainable Findings
  4. All Checks              5. Exceptions             6. Limitations and Assumptions
  7. Audit Trail             8. Appendix               9. PDF Highlighted Regions
Every highlight is numbered R1, R2, ... report-wide; Section 5 (and 3) cite
those IDs and link to the page in Section 9 that shows them.

TWO DIFFERENT OVERLAY MECHANISMS — DO NOT CONFUSE THEM
------------------------------------------------------
The live Case Detail screen (frontend PdfOverlayViewer.tsx) draws bounding
boxes dynamically ON TOP of a rendered page in the browser and never saves
anything: no annotated image is ever produced or stored there. That is
unchanged and must stay that way.

THIS module is different on purpose: it builds a NEW, separate PDF at
export time in which findings ARE burned into rendered page images. That
does not violate the immutable-original rule — the original file in Blob
Storage is only ever READ (opened from bytes in memory, rendered, closed) to
produce the report; it is never modified or written back, and the annotated
renders exist only inside the generated report.

The visual convention is the live UI's, reused exactly, plus one category:
  ELA                        -> solid red box
  Copy-move                  -> solid orange box
  Visual review / signature  -> dashed blue box, captioned "approximate"
  Field exception (rule)     -> solid PURPLE box (deterministic comparisons of
                                extracted values: neither pixel forensics nor
                                model judgment)
Each box gets a caption naming the check that produced it.
"""
from __future__ import annotations

import html
import io
import re
from datetime import datetime

import pymupdf

from app.services.case_report_data import (
    AuditRow,
    CoverageRow,
    CrossRow,
    DocumentSection,
    ExceptionItem,
    PageAnnotation,
    ReportData,
)

A4 = pymupdf.paper_rect("a4")
MARGIN_X = 40
CONTENT = A4 + (MARGIN_X, 48, -MARGIN_X, -56)  # room for the header and footer
RENDER_DPI = 144  # crisp enough to read, small enough to keep the report light

# Live-UI colours (frontend/src/index.css: --destructive #DC2626, --warning
# #D97706, --info #2563EB) plus violet for field exceptions, fuchsia for
# text-layer font mismatches and teal for ghost content (deleted text).
_HEX = {
    "ela": "#DC2626", "copy_move": "#D97706", "model": "#2563EB",
    "field": "#7C3AED", "font": "#C026D3", "ghost": "#0D9488", "page_level": "#64748B",
}
_KIND_ORDER = ("ela", "copy_move", "font", "ghost", "field", "model", "page_level")


def _rgb(hex_colour: str) -> tuple[float, float, float]:
    h = hex_colour.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


# Overlay style per annotation kind: (stroke colour, dashed?)
_STYLE = {
    "ela": (_rgb(_HEX["ela"]), False),
    "copy_move": (_rgb(_HEX["copy_move"]), False),
    "model": (_rgb(_HEX["model"]), True),
    "field": (_rgb(_HEX["field"]), False),
    "font": (_rgb(_HEX["font"]), False),
    "ghost": (_rgb(_HEX["ghost"]), False),
    "page_level": (_rgb(_HEX["page_level"]), False),
}
_KIND_LEGEND = {
    "ela": "ELA (solid red)",
    "copy_move": "Copy-move (solid orange)",
    "model": "Model judgment (dashed blue, approximate)",
    "field": "Field exception (solid purple)",
    "font": "Font mismatch in the text layer (solid fuchsia)",
    "ghost": "Ghost text: deleted or shortened content (solid teal)",
    "page_level": "Page-level finding (not localized)",
}

# Risk-tier colours = the live Case Detail card colours (emerald / amber / red).
_TIER = {
    "low": ("#065F46", "#ECFDF5", "#6EE7B7", "LOW RISK"),
    "medium": ("#92400E", "#FFFBEB", "#FCD34D", "MEDIUM RISK"),
    "high": ("#991B1B", "#FEF2F2", "#FCA5A5", "HIGH RISK"),
}
_RESULT = {
    "pass": ("Pass", "#065F46", "#D1FAE5"),
    "flag": ("Flag", "#991B1B", "#FEE2E2"),
    "review": ("Review", "#92400E", "#FEF3C7"),
    "failed": ("Failed", "#991B1B", "#FEE2E2"),
    "not_applicable": ("N/A", "#475569", "#E2E8F0"),
    "not_checked": ("Not checked", "#475569", "#E2E8F0"),
    "limited": ("Limited", "#92400E", "#FEF3C7"),
    "in_progress": ("In progress", "#475569", "#E2E8F0"),
}
_CROSS_RESULT = {
    "consistent": ("Consistent", "#065F46", "#D1FAE5"),
    "mismatch": ("Mismatch", "#991B1B", "#FEE2E2"),
    "not_compared": ("Not compared", "#475569", "#E2E8F0"),
}
_SEVERITY_CHIP = {
    "high": ("#991B1B", "#FEE2E2"),
    "medium": ("#92400E", "#FEF3C7"),
    "low": ("#475569", "#E2E8F0"),
}

_CSS = """
body { font-family: sans-serif; font-size: 9pt; color: #0F172A; }
h1 { font-size: 16pt; margin: 0 0 5pt 0; color: #0F172A; }
h2 { font-size: 11.5pt; margin: 12pt 0 4pt 0; color: #1E293B; }
h3 { font-size: 10pt; margin: 8pt 0 3pt 0; color: #334155; }
p { margin: 0 0 5pt 0; line-height: 1.25; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; background-color: #F1F5F9; color: #334155; font-size: 8pt; padding: 3pt 4pt; border: 0.5pt solid #CBD5E1; }
td { padding: 3pt 4pt; border: 0.5pt solid #E2E8F0; vertical-align: top; }
.muted { color: #64748B; }
.small { font-size: 7.5pt; }
.mono { font-family: monospace; font-size: 8pt; }
.box { padding: 6pt 8pt; border: 1pt solid #CBD5E1; margin: 0 0 6pt 0; }
.banner { padding: 5pt 8pt; background-color: #FEF3C7; border: 1pt solid #FCD34D; color: #92400E; margin: 0 0 6pt 0; }
"""

_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")


def _e(value: object) -> str:
    """HTML-escape `value`. Text whose first letter is Arabic-script is
    wrapped in an RTL span so extracted Arabic values render right-to-left
    (numbers inside stay left-to-right by the Unicode bidi rules) — the same
    value-level treatment the live UI gives them; the report layout itself
    stays LTR (SPECIFICATION.md section 3.7)."""
    text = html.escape(str(value))
    first_letter = next((c for c in text if c.isalpha()), "")
    if first_letter and _ARABIC.match(first_letter):
        return f'<span dir="rtl">{text}</span>'
    return text


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _chip(label: str, fg: str, bg: str) -> str:
    return f'<span style="color:{fg}; background-color:{bg}; font-weight:bold">&nbsp;{html.escape(label)}&nbsp;</span>'


def _ids(annotations: list[PageAnnotation]) -> str:
    """Region IDs as bold text; the link overlay is added after assembly."""
    ids = [a.region_id for a in annotations if a.region_id]
    return ", ".join(f"<b>{i}</b>" for i in ids) if ids else '<span class="muted">—</span>'


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _where(item: ExceptionItem, d: ReportData) -> str:
    if item.document_index is not None:
        page = f" · page {item.page}" if item.page else ""
        return f"Document {item.document_index} ({_e(item.document)}){page}"
    by_id = {str(s.document_id): s for s in d.documents}
    lines = []
    for doc_id in item.involved:
        section = by_id.get(doc_id)
        if section is None:
            continue
        pages = sorted({a.page for a in item.annotations if a in section.annotations})
        page = f" · page {', '.join(map(str, pages))}" if pages else ""
        lines.append(f"Document {section.index} ({_e(section.filename)}){page}")
    return "<br/>".join(lines) or "Case-level"


# ---------------------------------------------------------------------------
# HTML segments (Story -> PDF pages)
# ---------------------------------------------------------------------------

def _decision_text(d: ReportData) -> str:
    if d.decision is None:
        base = "Pending review"
    else:
        who = f" by {d.decision.by}" if d.decision.by else ""
        when = f" on {_ts(d.decision.at)}" if d.decision.at else ""
        base = f"{d.decision.label}{who}{when}"
    if d.escalation is not None:
        base += " · escalated" + (f" by {d.escalation.by}" if d.escalation.by else "")
    return base


def _details_and_summary_html(d: ReportData) -> str:
    parts = [
        f"<h1>{html.escape(d.app_name)} Case Report</h1>",
        f'<p class="muted">{html.escape(d.app_full_name)} — system-assisted analysis for human review. See '
        "Section 6 for what this report is and is not.</p>",
        "<h1>1. Case Details</h1>",
        '<table cellspacing="0">',
        f'<tr><th width="24%" align="left">Case ID</th><td><b>{_e(d.case_number)}</b> &nbsp;<span class="muted small">{_e(d.case_id)}</span></td></tr>',
        f"<tr><th>Submitted by</th><td>{_e(d.submitter)}</td></tr>",
        f"<tr><th>Submission date</th><td>{_ts(d.submitted_at)}</td></tr>",
        f"<tr><th>Documents</th><td>{len(d.documents)} &nbsp;&nbsp;<span class=\"muted\">Types:</span> "
        f"{_e(', '.join(d.document_types)) if d.document_types else 'not yet classified'}</td></tr>",
        f"<tr><th>Case type</th><td>{_e(d.case_type)}</td></tr>",
        f"<tr><th>Status / decision</th><td>{_e(d.case_status)} &nbsp;/&nbsp; <b>{_e(_decision_text(d))}</b></td></tr>",
        f"<tr><th>Report generated</th><td>{_ts(d.generated_at)} by <b>{_e(d.generated_by_name)}</b> ({_e(d.generated_by_role)})</td></tr>",
        f'<tr><th>Report ID</th><td class="mono">{_e(d.report_id)}</td></tr>',
        "</table>",
    ]
    if d.pipeline_pending:
        pending = "; ".join(html.escape(p) for p in d.pipeline_pending[:6])
        parts.append(
            '<p></p><div class="banner"><b>Preliminary report.</b> Automated analysis had not finished '
            f"when this report was generated. Still outstanding: {pending}.</div>"
        )

    # ---- 2. Executive summary
    parts.append("<h1 style=\"margin-top:14pt\">2. Executive Summary</h1>")
    exceptions = d.exceptions
    region_count = sum(len(s.annotations) for s in d.documents)
    if d.risk is None:
        parts.append('<div class="box"><b>Not yet scored.</b> The risk engine has not produced an assessment for this case, '
                     "so there is no overall tier.</div>")
        headline = "No risk tier yet — automated analysis is still in progress."
    else:
        fg, bg, border, label = _TIER[d.risk.tier]
        parts.append(
            f'<div class="box" style="background-color:{bg}; border-color:{border}; color:{fg}">'
            f'<span style="font-size:14pt"><b>{label}</b></span> &nbsp;&nbsp; <b>Score {d.risk.score} / 100</b> '
            f'<span class="small">(computed {_ts(d.risk.computed_at)})</span></div>'
        )
        top = max(d.risk.reasons, key=lambda r: r.weight) if d.risk.reasons else None
        headline = (
            f"Driven mainly by ({top.weight:g} points): {_shorten(top.text, 200)}"
            if top else "No risk rule fired for this case."
        )
    parts.append(f"<p><b>Why:</b> {_e(headline)}</p>")
    parts.append(f"<p><b>Decision status:</b> {_e(_decision_text(d))}.</p>")
    if exceptions:
        parts.append(
            f"<p><b>Exceptions:</b> {len(exceptions)} item(s) did not pass"
            + (f", {region_count} of them shown as highlighted regions on the source pages" if region_count else "")
            + ". Section 5 lists each one; Section 9 shows them on the documents.</p>"
        )
    else:
        parts.append("<p><b>Exceptions:</b> none — every check that ran passed.</p>")
    parts.append(
        '<p class="muted small">This summary is deliberately short. The reasons behind the tier are in Section 3, '
        "the complete checklist is in Section 4.</p>"
    )
    return "".join(parts)


def _findings_html(d: ReportData) -> str:
    parts = ["<h1>3. Explainable Findings</h1>"]
    if d.risk is None:
        parts.append('<p class="muted">The case has not been scored, so there are no triggered risk reasons yet.</p>')
        return "".join(parts)
    t = d.risk.thresholds
    parts.append(
        f"<p>The risk engine is a transparent, weighted rule set. Each row is one rule that fired, with the reason text "
        f"stored at scoring time. Points are summed (raw {d.risk.raw_score:g}"
        + (
            f"; the metadata rules' {d.risk.group_caps['metadata']['points']:g} points count as "
            f"{d.risk.group_caps['metadata']['cap']:g}, as one edit leaves several metadata traces"
            if d.risk.group_caps.get("metadata") else ""
        )
        + "), capped at 100, and mapped to a tier"
        + (f" (medium from {t.get('medium')}, high from {t.get('high')})" if t else "")
        + f" — here <b>{d.risk.score}/100, {d.risk.tier}</b>.</p>"
    )
    if not d.findings:
        parts.append('<p class="muted">No risk rule fired for this case.</p>')
        return "".join(parts)
    parts.append(
        '<table cellspacing="0"><tr><th width="9%" align="left">Severity</th><th width="7%" align="left">Points</th>'
        '<th width="17%" align="left">Check</th><th width="20%" align="left">Document / page</th>'
        '<th align="left">What was found</th></tr>'
    )
    for f in d.findings:
        fg, bg = _SEVERITY_CHIP.get(f.severity, _SEVERITY_CHIP["low"])
        page = f" · page {f.page}" if f.page else ""
        regions = f'<br/><span class="small muted">Highlights:</span> {_ids(f.annotations)}' if f.annotations else ""
        parts.append(
            f"<tr><td>{_chip(f.severity.upper(), fg, bg)}</td><td>{f.weight:g}</td><td>{html.escape(f.check)}</td>"
            f"<td class=\"small\">{_e(f.document or 'Case-level')}{page}</td><td>{_short_first(f.title, f.short, f.text)}{regions}</td></tr>"
        )
    parts.append("</table>")
    return "".join(parts)


def _short_first(title: str, short: str, full: str) -> str:
    """The short line in bold, the full explanation under it in grey."""
    if not (title or short):
        return _e(full)
    head = f"<b>{_e(title)}</b>" + (f" — {_e(short)}" if short else "")
    return f'{head}<br/><span class="small muted">{_e(full)}</span>' if full else head


def _all_checks_html(d: ReportData) -> str:
    parts = [
        "<h1>4. All Checks</h1>",
        "<p>The completeness record: every check that ran on every document, including those that passed. "
        "Section 3 is the narrative; this is the checklist.</p>",
    ]
    for doc in d.documents:
        flagged = sum(1 for r in doc.check_rows if r.result in ("flag", "failed", "review"))
        passed = sum(1 for r in doc.check_rows if r.result == "pass")
        parts.append(
            f"<h3>Document {doc.index} — {_e(doc.filename)} "
            f'<span class="muted small">({_e(doc.document_type)} · {passed} passed, {flagged} flagged)</span></h3>'
        )
        if doc.processing_note:
            parts.append(f'<div class="banner">{html.escape(doc.processing_note)}</div>')
        if not doc.check_rows:
            parts.append('<p class="muted">No automated checks have completed on this document.</p>')
            continue
        parts.append('<table cellspacing="0"><tr><th width="27%" align="left">Check</th><th width="11%" align="left">Result</th><th align="left">Details</th></tr>')
        for row in doc.check_rows:
            label, fg, bg = _RESULT.get(row.result, _RESULT["in_progress"])
            parts.append(
                f'<tr><td><b>{html.escape(row.name)}</b><br/><span class="muted small">{html.escape(row.description)}</span></td>'
                f"<td>{_chip(label, fg, bg)}</td><td><b>{_e(row.summary)}</b>"
                + "".join(f'<br/><span class="small">• {_e(line)}</span>' for line in row.lines)
                + "</td></tr>"
            )
        parts.append("</table>")
    if d.cross_document is not None:
        parts.append("<h3>Cross-document comparisons</h3>")
        parts.append(
            "<p>Issuer, date and amount compared for every document pair. Mismatches are the findings stored by the "
            "cross-document check when it ran; nothing is re-computed for this report.</p>"
        )
        parts.append(_cross_table(d.cross_document))
    if d.linked_cases:
        parts.append("<h3>Linked cases (reviewer note, not scored)</h3>")
        parts.append(
            "<p>Other cases of this company that share a scanning device, a parent/guardian or a family/student "
            "ID with this one. Shared office scanners are common, so this is context for the reviewer only; it "
            "does not affect the risk score.</p>"
        )
        parts.append('<table cellspacing="0"><tr><th width="18%" align="left">Case</th><th align="left">Shared</th></tr>')
        for link in d.linked_cases:
            parts.append(f"<tr><td>{_e(link.case_number)}</td><td>{_e('; '.join(link.reasons))}</td></tr>")
        parts.append("</table>")
    return "".join(parts)


def _cross_table(rows: list[CrossRow]) -> str:
    parts = ['<table cellspacing="0"><tr><th width="10%" align="left">Field</th><th width="28%" align="left">Documents</th>'
             '<th width="13%" align="left">Result</th><th align="left">In plain language</th></tr>']
    for r in rows:
        label, fg, bg = _CROSS_RESULT[r.result]
        parts.append(
            f"<tr><td>{html.escape(r.field)}</td><td class=\"small\">{_e(r.documents)}</td>"
            f"<td>{_chip(label, fg, bg)}</td><td>{_e(r.detail)}</td></tr>"
        )
    parts.append("</table>")
    return "".join(parts)


def _exceptions_html(d: ReportData) -> str:
    parts = ["<h1>5. Exceptions</h1>"]
    if not d.exceptions:
        parts.append('<p class="muted">No exceptions — every check that ran passed.</p>')
        return "".join(parts)
    parts.append(
        f"<p>Everything that did <b>not</b> pass, drawn from Section 4 and written as concrete items "
        f"({len(d.exceptions)} in total). A region ID (R1, R2, …) points to the highlight that shows the exception "
        "on the source page in Section 9 — click it to jump there.</p>"
    )
    parts.append(
        '<table cellspacing="0"><tr><th width="5%" align="left">No.</th><th align="left">Exception</th>'
        '<th width="27%" align="left">Where</th><th width="11%" align="left">Highlight</th></tr>'
    )
    for n, e in enumerate(d.exceptions, 1):
        detail = f'<br/><span class="small muted">{_e(e.detail)}</span>' if e.detail else ""
        parts.append(
            f"<tr><td>{n}</td><td><b>{_e(e.text)}</b>{detail}</td>"
            f'<td class="small">{_where(e, d)}</td><td>{_ids(e.annotations)}</td></tr>'
        )
    parts.append("</table>")
    missing = [e for e in d.exceptions if not e.annotations]
    if missing:
        parts.append(
            f'<p class="muted small">{len(missing)} exception(s) show “—” because they have no place on a page '
            "(for example a missing field, or a check that did not localize its finding).</p>"
        )
    return "".join(parts)


def _coverage_table(rows: list[CoverageRow]) -> str:
    style = {
        "ran": ("Ran", "#065F46", "#D1FAE5"),
        "not_run": ("Did not run", "#92400E", "#FEF3C7"),
        "not_applicable": ("Not applicable", "#475569", "#E2E8F0"),
    }
    parts = ['<table cellspacing="0" style="page-break-inside: avoid"><tr><th width="32%" align="left">Check</th><th width="15%" align="left">Status</th><th align="left">Coverage on this case</th></tr>']
    for r in rows:
        label, fg, bg = style[r.status]
        parts.append(f"<tr><td>{html.escape(r.name)}</td><td>{_chip(label, fg, bg)}</td><td>{html.escape(r.detail)}</td></tr>")
    parts.append("</table>")
    return "".join(parts)


def _limitations_html(d: ReportData) -> str:
    parts = [
        "<h1>6. Limitations and Assumptions</h1>",
        "<p>Please read this section before relying on anything in this report.</p>",
        "<h2>Limitations of This Analysis</h2>",
        "<h3>What this report is</h3>",
        "<p>This is <b>system-assisted analysis to support human review</b>. It is not a certified forensic or "
        "legal determination that any document is genuine, forged or fraudulent, and it must not be presented as one.</p>",
        "<h3>Three kinds of finding</h3>",
        "<p><b>Deterministic pixel and structure checks</b> — error level analysis, copy-move detection and PDF metadata "
        "forensics — are computed by fixed algorithms over the file’s pixels and structure. Localized findings are "
        "drawn with <b>solid</b> boxes (ELA in red, copy-move in orange). They are signals, not proof: ELA in particular can "
        "false-positive on legitimately recompressed scans and can miss print-and-rescan tampering.</p>",
        "<p><b>Deterministic rule-based field checks</b> — field validation and cross-document consistency — compare "
        "extracted values by fixed rules and are drawn with <b>solid purple</b> boxes. They are exact about the values they "
        "compare, but only as reliable as the OCR and extraction that produced those values.</p>",
        "<p><b>Model-judgment checks</b> — the visual inconsistency review, the AI-generation assessment and the "
        "signature/stamp comparison — are <b>probabilistic assessments</b> made by a vision-language model, not "
        "deterministic proof. They can vary from run to run, and their boxes are only approximate; they are the "
        "<b>dashed</b> boxes captioned “approximate” throughout this report. Signature and stamp detection shows "
        "presence and placement only and is not an identity match; the AI-generation assessment is experimental, and its "
        "accuracy on scanned business documents has not been validated.</p>",
        "<h3>What the system can and cannot see</h3>",
        "<p>The system evaluates the <b>submitted document file itself</b>. It cannot verify facts external to the document "
        "— for example whether a real-world transaction actually occurred, whether the named issuer really produced "
        "the document, or (without a pre-existing reference signature) who a signatory is. Printed Arabic and English text are "
        "read; handwritten Arabic is not reliably read.</p>",
        "<h3>Absence of a finding</h3>",
        "<p><b>The absence of a flagged finding does not guarantee that a document is genuine.</b> It means only that no "
        "supported check detected an anomaly.</p>",
        "<h3>Check coverage on this case</h3>",
        "<p>Not every check applies to every document or case. This is what actually ran here:</p>",
        _coverage_table(d.coverage),
        "<h3>Risk score and rule versions</h3>",
    ]
    if d.risk is None:
        parts.append("<p>This case had not been scored when the report was generated, so no risk-rule versions apply.</p>")
    else:
        t = d.risk.thresholds
        thresholds = (
            f" Tier thresholds at that time: medium from {t.get('medium')}, high from {t.get('high')}." if t else ""
        )
        versions = (
            "; ".join(f'<span class="mono">{html.escape(rid)}</span> v{ver} (weight {w:g})' for rid, ver, w in d.risk.rule_versions)
            if d.risk.rule_versions else "no rules fired"
        )
        parts.append(
            f"<p>The risk score in this report (<b>{d.risk.score}/100, {d.risk.tier}</b>) was computed at "
            f'{_ts(d.risk.computed_at)} (assessment <span class="mono">{html.escape(str(d.risk.assessment_id))}</span>) '
            f"from the risk-rule versions that fired: {versions}.{thresholds}</p>"
            "<p>Rule weights are versioned and are never re-applied retroactively: if an administrator later edits a rule, "
            "this case is not re-scored, and <b>this report remains an accurate historical record</b> of what was known and "
            "weighted when it was generated. A report generated later may legitimately differ.</p>"
        )
    parts.append("<h2>Assumptions</h2>")
    parts.append(
        "<p>What the analysis took to be true without independently verifying it. These are specific to what ran on this case.</p>"
    )
    parts.append("<ul>" + "".join(f"<li>{_e(a)}</li>" for a in d.assumptions) + "</ul>")
    return "".join(parts)


def _audit_html(rows: list[AuditRow], truncated: int) -> str:
    parts = [
        "<h1>7. Audit Trail</h1>",
        "<p>Chronological case-level events, read from the append-only audit log (times in UTC). "
        "This report does not write to or duplicate the log.</p>",
    ]
    if not rows:
        parts.append('<p class="muted">No audit events are recorded for this case.</p>')
        return "".join(parts)
    parts.append('<table cellspacing="0"><tr><th width="21%" align="left">Time</th><th width="32%" align="left">Event</th><th width="14%" align="left">Actor</th><th align="left">Detail</th></tr>')
    for r in rows:
        parts.append(
            f'<tr><td class="small">{_ts(r.at)}</td><td>{html.escape(r.event)}</td>'
            f'<td>{_e(r.actor)}</td><td class="small">{_e(r.detail)}</td></tr>'
        )
    parts.append("</table>")
    if truncated:
        parts.append(f'<p class="muted small">{truncated} later event(s) omitted from this excerpt; the full history is in the audit log.</p>')
    return "".join(parts)


def _appendix_html(d: ReportData) -> str:
    parts = [
        "<h1>8. Appendix</h1>",
        '<p class="muted">The detailed technical record, for someone auditing the analysis itself rather than a first-pass reviewer.</p>',
        "<h2>8.1 Extracted fields</h2>",
        "<p>Normalized values as stored at extraction time. “Located” is the page the value was matched to (used for the "
        "purple highlights); a blank means no position was recorded. Arabic values are shown right-to-left, numbers left-to-right.</p>",
    ]
    for doc in d.documents:
        parts.append(f"<h3>Document {doc.index} — {_e(doc.filename)}</h3>")
        if not doc.fields:
            parts.append('<p class="muted">No fields were extracted from this document.</p>')
            continue
        parts.append('<table cellspacing="0"><tr><th width="26%" align="left">Field</th><th align="left">Value</th><th width="15%" align="left">Confidence</th><th width="9%" align="left">Located</th></tr>')
        for f in doc.fields:
            # A field that wasn't found has a meaningless 0% confidence — leave it blank.
            conf = "" if f.confidence is None or f.value == "Not found" else f"{f.confidence * 100:.0f}%"
            if f.uncertain:
                conf += " &nbsp;" + _chip("uncertain", "#92400E", "#FEF3C7")
            muted = ' class="muted"' if f.value == "Not found" else ""
            located = f"p.{f.page}" if f.page else ""
            parts.append(f"<tr><td>{html.escape(f.name)}</td><td{muted}>{_e(f.value)}</td><td>{conf}</td><td class=\"small\">{located}</td></tr>")
        parts.append("</table>")

    parts.append("<h2>8.2 Document hashes (chain of custody)</h2>")
    parts.append(
        "<p>SHA-256 of each original as recorded when it was uploaded to immutable storage, re-checked against the stored "
        "file when this report was generated.</p>"
    )
    parts.append('<table cellspacing="0"><tr><th width="25%" align="left">Document</th><th align="left">SHA-256</th><th width="17%" align="left">Verification</th></tr>')
    for doc in d.documents:
        v = doc.hash
        chip = {
            "match": _chip("VERIFIED", "#065F46", "#D1FAE5"),
            "mismatch": _chip("MISMATCH", "#991B1B", "#FEE2E2"),
        }.get(v.status, _chip("NOT VERIFIED", "#92400E", "#FEF3C7"))
        extra = ""
        if v.status == "mismatch":
            extra = f'<br/><span class="small">computed now: <span class="mono">{html.escape(v.computed_sha256 or "")}</span> — the stored original no longer matches its upload hash.</span>'
        elif v.status == "unverified":
            extra = f'<br/><span class="small">{html.escape(v.note or "")}</span>'
        size = f'<br/><span class="small muted">{doc.file_size_bytes:,} bytes</span>' if doc.file_size_bytes else ""
        parts.append(
            f'<tr><td>{_e(doc.filename)}{size}</td><td><span class="mono">{html.escape(v.recorded_sha256)}</span>{extra}</td><td>{chip}</td></tr>'
        )
    parts.append("</table>")

    parts.append("<h2>8.3 Technical parameters used by each check</h2>")
    for block in d.technical:
        parts.append(f"<h3>{html.escape(block.title)}</h3>")
        parts.append('<table cellspacing="0">' + "".join(
            f'<tr><td width="34%"><b>{html.escape(k)}</b></td><td>{_e(v)}</td></tr>' for k, v in block.rows
        ) + "</table>")
    return "".join(parts)


def _regions_intro_html(d: ReportData, regions: list[tuple[DocumentSection, PageAnnotation]]) -> str:
    parts = [
        "<h1>9. PDF Highlighted Regions</h1>",
        "<p>Every flagged page, rendered from the original with its findings drawn in. These are copies made for this report; "
        "the original files are untouched. Only pages with at least one highlight appear. Each highlight has a region ID "
        "that Section 5 refers back to.</p>",
    ]
    if not regions:
        parts.append('<p class="muted">No check produced a highlight for this case, so no pages are rendered.</p>')
        return "".join(parts)
    parts.append("<h3>Colour key</h3><table cellspacing=\"0\">" + "".join(
        f'<tr><td width="7%"><span style="color:{_HEX[k]}"><b>■</b></span></td><td>{html.escape(_KIND_LEGEND[k])}</td></tr>'
        for k in _KIND_ORDER
    ) + "</table>")
    parts.append("<h3>Region index</h3>")
    parts.append('<table cellspacing="0"><tr><th width="8%" align="left">Region</th><th width="30%" align="left">Document</th><th width="8%" align="left">Page</th><th align="left">What it shows</th></tr>')
    for section, a in regions:
        parts.append(
            f"<tr><td><b>{a.region_id}</b></td><td class=\"small\">Document {section.index} ({_e(section.filename)})</td>"
            f"<td>{a.page}</td><td class=\"small\">{_e(a.caption)}</td></tr>"
        )
    parts.append("</table>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _story_pdf(body_html: str) -> pymupdf.Document:
    story = pymupdf.Story(html=f"<body>{body_html}</body>", user_css=_CSS)
    buffer = io.BytesIO()
    writer = pymupdf.DocumentWriter(buffer)
    more = 1
    while more:
        device = writer.begin_page(A4)
        more, _ = story.place(CONTENT)
        story.draw(device)
        writer.end_page()
    writer.close()
    return pymupdf.open("pdf", buffer.getvalue())


# `Page.insert_text(fontname="helv")` maps text through a Latin-1 table, which
# turns the em dash in captions into a stray dot and makes measured widths
# lie (captions then clip). A real Font object measures and draws the same
# Unicode glyphs.
_FONT = pymupdf.Font("helv")
_BOLD = pymupdf.Font("hebo")


def _text_width(text: str, size: float, font: pymupdf.Font = _FONT) -> float:
    return font.text_length(text, fontsize=size)


def _draw_text(page: pymupdf.Page, pos: tuple[float, float], text: str, size: float, color, font: pymupdf.Font = _FONT) -> None:
    writer = pymupdf.TextWriter(page.rect)
    writer.append(pos, text, font=font, fontsize=size)
    writer.write_text(page, color=color)


def _draw_dashes(dashed: bool) -> str | None:
    return "[4 3] 0" if dashed else None


def _legend(page: pymupdf.Page, kinds: set[str], y: float) -> None:
    x = MARGIN_X
    for kind in _KIND_ORDER:
        if kind not in kinds:
            continue
        colour, dashed = _STYLE[kind]
        swatch = pymupdf.Rect(x, y, x + 16, y + 9)
        shape = page.new_shape()
        shape.draw_rect(swatch)
        shape.finish(color=colour, fill=colour, fill_opacity=0.2, width=1.2, dashes=_draw_dashes(dashed))
        shape.commit()
        _draw_text(page, (x + 20, y + 8), _KIND_LEGEND[kind], 7.2, (0.2, 0.25, 0.33))
        x += 26 + _text_width(_KIND_LEGEND[kind], 7.2) + 8


def _findings_list_html(annotations: list[PageAnnotation]) -> str:
    items = []
    for a in annotations:
        located = "" if a.box else ' <span class="muted">(page-level — not localized to a region)</span>'
        text = " ".join(a.description.split())
        items.append(
            f'<p><b>{a.region_id}. {html.escape(a.check_label)}</b>{located}<br/>{_e(text if len(text) < 420 else text[:419] + "…")}</p>'
        )
    return f"<body>{''.join(items)}</body>"


def _add_annotated_page(
    out: pymupdf.Document,
    d: ReportData,
    doc: DocumentSection,
    source: pymupdf.Document,
    page_number: int,
    annotations: list[PageAnnotation],
) -> None:
    page = out.new_page(width=A4.width, height=A4.height)

    ids = ", ".join(a.region_id for a in annotations)
    header = (
        f'<body><p style="font-size:11pt"><b>{_e(doc.filename)}</b> — page {page_number}</p>'
        f'<p class="muted" style="font-size:8pt">Section 9 · Document {doc.index} of {len(d.documents)} · '
        f"regions {html.escape(ids)}</p></body>"
    )
    page.insert_htmlbox(pymupdf.Rect(MARGIN_X, 42, A4.width - MARGIN_X, 82), header, css=_CSS)
    _legend(page, {a.kind for a in annotations}, 86)

    # Room for the findings list under the image.
    list_height = min(300, 24 + 44 * len(annotations))
    top, bottom = 102, A4.height - 56 - list_height
    area = pymupdf.Rect(MARGIN_X, top, A4.width - MARGIN_X, bottom)

    src = source[page_number - 1]
    aspect = src.rect.width / src.rect.height
    width = min(area.width, area.height * aspect)
    height = width / aspect
    img_rect = pymupdf.Rect(area.x0 + (area.width - width) / 2, top, area.x0 + (area.width + width) / 2, top + height)

    # The page image: a render of the ORIGINAL (read-only) — the findings are
    # drawn on top of it as vector shapes below, so they are burned into this
    # report page, never into the original file.
    pix = src.get_pixmap(dpi=RENDER_DPI, colorspace=pymupdf.csRGB, alpha=False)
    page.insert_image(img_rect, stream=pix.tobytes("jpeg", jpg_quality=82))
    frame = page.new_shape()
    frame.draw_rect(img_rect)
    frame.finish(color=(0.8, 0.84, 0.88), width=0.6)
    frame.commit()

    fontsize = 7
    used_labels: list[pymupdf.Rect] = []
    for a in annotations:
        if a.box is None:
            continue
        x, y, w, h = a.box
        box = pymupdf.Rect(
            img_rect.x0 + x * img_rect.width,
            img_rect.y0 + y * img_rect.height,
            img_rect.x0 + (x + w) * img_rect.width,
            img_rect.y0 + (y + h) * img_rect.height,
        )
        colour, dashed = _STYLE[a.kind]
        shape = page.new_shape()
        shape.draw_rect(box)
        shape.finish(color=colour, fill=colour, fill_opacity=0.14, width=1.6, dashes=_draw_dashes(dashed))
        shape.commit()

        text = f"{a.region_id} · {a.caption}"
        label_w = _text_width(text, fontsize) + 8
        label_h = fontsize + 5
        lx = min(max(box.x0, img_rect.x0), img_rect.x1 - label_w)
        ly = box.y0 - label_h if box.y0 - label_h >= img_rect.y0 else box.y1  # above, else below
        label = pymupdf.Rect(lx, ly, lx + label_w, ly + label_h)
        for _ in range(12):  # nudge clear of earlier captions
            clash = next((u for u in used_labels if u.intersects(label)), None)
            if clash is None:
                break
            label = pymupdf.Rect(label.x0, clash.y0 - label_h, label.x1, clash.y0)
        if label.y0 < img_rect.y0:  # never let a caption ride up out of the image
            shift = img_rect.y0 - label.y0
            label = pymupdf.Rect(label.x0, label.y0 + shift, label.x1, label.y1 + shift)
        used_labels.append(label)
        chip = page.new_shape()
        chip.draw_rect(label)
        chip.finish(color=None, fill=colour)
        chip.commit()
        _draw_text(page, (label.x0 + 4, label.y1 - 3.5), text, fontsize, (1, 1, 1))

    list_rect = pymupdf.Rect(MARGIN_X, img_rect.y1 + 8, A4.width - MARGIN_X, A4.height - 50)
    page.insert_htmlbox(list_rect, _findings_list_html(annotations), css=_CSS, scale_low=0.55)


_CROP_MAX_HEIGHT = 230.0


def _add_crop_pages(out: pymupdf.Document, doc: DocumentSection, page_number: int, annotations: list[PageAnnotation]) -> None:
    """The enhanced crops some highlights carry (the ghost-content check's
    faint traces of erased text), each under its region ID, on pages after
    the annotated page — the trace is too faint to see on the page render."""
    with_images = [a for a in annotations if a.image_png]
    if not with_images:
        return
    page, y = None, 0.0
    width = A4.width - 2 * MARGIN_X
    for a in with_images:
        try:
            pix = pymupdf.Pixmap(a.image_png)
        except Exception:  # noqa: BLE001 - an unreadable crop is just not shown
            continue
        scale = min(width / pix.width, _CROP_MAX_HEIGHT / pix.height, 2.5)
        w, h = pix.width * scale, pix.height * scale
        if page is None or y + h + 40 > A4.height - 60:
            page = out.new_page(width=A4.width, height=A4.height)
            header = (
                f'<body><p style="font-size:11pt"><b>{_e(doc.filename)}</b> — page {page_number}: enhanced traces</p>'
                '<p class="muted" style="font-size:8pt">The scanned background, contrast-stretched so the faint '
                "trace of erased text shows. These traces are what the ghost-content highlights on the previous page "
                "mark.</p></body>"
            )
            page.insert_htmlbox(pymupdf.Rect(MARGIN_X, 42, A4.width - MARGIN_X, 86), header, css=_CSS)
            y = 92.0
        colour, _ = _STYLE[a.kind]
        _draw_text(page, (MARGIN_X, y + 9), f"{a.region_id} · {a.caption}", 8.5, colour, _BOLD)
        rect = pymupdf.Rect(MARGIN_X, y + 14, MARGIN_X + w, y + 14 + h)
        page.insert_image(rect, stream=a.image_png)
        frame = page.new_shape()
        frame.draw_rect(rect)
        frame.finish(color=colour, width=1.0)
        frame.commit()
        y = rect.y1 + 18


def _decorate(out: pymupdf.Document, d: ReportData) -> None:
    """Header (product name) and footer (case, report, page numbers) on every page."""
    total = len(out)
    grey = (0.4, 0.45, 0.52)
    left = f"{d.app_name} case report  |  {d.case_number}  |  report {str(d.report_id)[:8]}  |  generated {_ts(d.generated_at)}"
    note = "System-assisted analysis — not a certified forensic determination"
    for i, page in enumerate(out, 1):
        # Story-generated pages leave their content stream's transform
        # un-restored, which mirrors anything appended afterwards; wrapping
        # the existing content in q/Q isolates it.
        page.wrap_contents()
        _remove_stray_header_fills(page)
        _draw_text(page, (MARGIN_X, 26), d.app_name, 10, (0.08, 0.15, 0.29), _BOLD)
        _draw_text(page, (MARGIN_X + _text_width(d.app_name, 10, _BOLD) + 6, 26), d.app_full_name, 7.5, grey)
        right = f"Case {d.case_number}"
        _draw_text(page, (A4.width - MARGIN_X - _text_width(right, 8), 26), right, 8, grey)
        page.draw_line((MARGIN_X, 33), (A4.width - MARGIN_X, 33), color=(0.8, 0.84, 0.88), width=0.5)
        y = A4.height - 30
        page.draw_line((MARGIN_X, y - 8), (A4.width - MARGIN_X, y - 8), color=(0.8, 0.84, 0.88), width=0.5)
        _draw_text(page, (MARGIN_X, y + 2), left, 6.8, grey)
        _draw_text(page, (MARGIN_X, y + 11), note, 6.8, grey)
        label = f"Page {i} of {total}"
        _draw_text(page, (A4.width - MARGIN_X - _text_width(label, 7.5), y + 2), label, 7.5, (0.2, 0.25, 0.33))


_TH_FILL = (0.9450980424880981, 0.9607843160629272, 0.9764705896377563)  # #F1F5F9, the table-header fill


def _remove_stray_header_fills(page: pymupdf.Page) -> None:
    """MuPDF's Story repaints a table's header-row fill as a thin (< 10pt)
    orphan band on the page a table continues onto (or follows). A real
    header row is ~16pt tall; the orphans are removed so no stray grey band
    shows on the page."""
    stray = [
        d["rect"] for d in page.get_drawings()
        if d.get("fill") == _TH_FILL and d["rect"].height < 10 and d["rect"].width > 100
    ]
    if not stray:
        return
    for rect in stray:
        page.add_redact_annot(rect)
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
        text=pymupdf.PDF_REDACT_TEXT_NONE,
    )


def _link_region_ids(out: pymupdf.Document, page_range: range, targets: dict[str, int]) -> None:
    """Make every bare region ID (R1, R2, ...) on `page_range` a link to the
    Section 9 page that shows it."""
    for index in page_range:
        page = out[index]
        for x0, y0, x1, y1, word, *_ in page.get_text("words"):
            region = word.strip(",;:()[].")
            target = targets.get(region)
            if target is None or target == index:
                continue
            page.insert_link(
                {"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(x0, y0, x1, y1), "page": target, "to": pymupdf.Point(0, 40)}
            )


def _open_original(doc: DocumentSection) -> tuple[pymupdf.Document | None, str | None]:
    if not doc.annotations:
        return None, None
    if doc.original_bytes is None:
        return None, "The original file could not be read from storage, so its pages could not be rendered."
    try:
        source = pymupdf.open(stream=doc.original_bytes, filetype=doc.original_kind)
        if source.page_count == 0:
            return None, "The original file has no renderable pages."
        return source, None
    except Exception:  # noqa: BLE001 - unreadable original => list findings instead of failing the report
        return None, "The original file could not be opened for rendering."


def build_report_pdf(d: ReportData) -> bytes:
    out = pymupdf.open()
    toc: list[list] = []
    ranges: dict[str, range] = {}

    def add(body_html: str, title: str, key: str | None = None, level: int = 1) -> None:
        segment = _story_pdf(body_html)
        start = len(out)
        toc.append([level, title, start + 1])
        out.insert_pdf(segment)
        if key:
            ranges[key] = range(start, len(out))
        segment.close()

    add(_details_and_summary_html(d), "1. Case Details / 2. Executive Summary")
    add(_findings_html(d), "3. Explainable Findings", "findings")
    add(_all_checks_html(d), "4. All Checks")
    add(_exceptions_html(d), "5. Exceptions", "exceptions")
    add(_limitations_html(d), "6. Limitations and Assumptions")
    add(_audit_html(d.audit, d.audit_truncated), "7. Audit Trail")
    add(_appendix_html(d), "8. Appendix")

    # ---- 9. Highlighted regions: every flagged page, consolidated ----------
    regions = [(s, a) for s in d.documents for a in s.annotations]
    add(_regions_intro_html(d, regions), "9. PDF Highlighted Regions", "regions")
    targets: dict[str, int] = {}
    for doc in d.documents:
        source, problem = _open_original(doc)
        try:
            by_page: dict[int, list[PageAnnotation]] = {}
            if source is not None:
                for a in doc.annotations:
                    if 1 <= a.page <= source.page_count:
                        by_page.setdefault(a.page, []).append(a)
            elif doc.annotations and problem:
                # No render possible: say so and list what would have been shown.
                rows = "".join(
                    f"<tr><td><b>{a.region_id}</b></td><td>{a.page}</td><td>{_e(a.caption)}</td><td>{_e(a.description)}</td></tr>"
                    for a in doc.annotations
                )
                for a in doc.annotations:
                    targets[a.region_id] = len(out)
                add(
                    f"<h2>Document {doc.index} — {_e(doc.filename)}</h2><div class=\"banner\">{html.escape(problem)} "
                    "The highlights are listed instead.</div>"
                    '<table cellspacing="0"><tr><th width="8%" align="left">Region</th><th width="8%" align="left">Page</th>'
                    f'<th width="30%" align="left">Caption</th><th align="left">Detail</th></tr>{rows}</table>',
                    f"Document {doc.index}: highlights (page not rendered)", level=2,
                )
            for page_number in sorted(by_page):
                for a in by_page[page_number]:
                    targets[a.region_id] = len(out)
                toc.append([2, f"Document {doc.index}: {doc.filename} — page {page_number}", len(out) + 1])
                _add_annotated_page(out, d, doc, source, page_number, by_page[page_number])
                _add_crop_pages(out, doc, page_number, by_page[page_number])
        finally:
            if source is not None:
                source.close()

    # Region IDs cited in Sections 3 and 5 (and the index) jump to their page.
    for key in ("findings", "exceptions", "regions"):
        if key in ranges:
            _link_region_ids(out, ranges[key], targets)

    _decorate(out, d)
    out.set_toc(toc)
    out.set_metadata(
        {
            "title": f"{d.app_name} case report {d.case_number}",
            "author": d.generated_by_name,
            "subject": f"{d.app_full_name} — system-assisted document authenticity analysis",
            "creator": f"{d.app_name} ({d.app_full_name})",
            "producer": "PyMuPDF",
        }
    )
    return out.tobytes(garbage=3, deflate=True)
