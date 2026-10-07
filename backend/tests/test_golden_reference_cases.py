"""
Golden integration tests: the manually reviewed reference cases.

Each runs the check pipeline end to end over the case's own file and its
recorded external results (tests/fixtures/golden/<case>/: the stored LLM
extraction, vision review, signature/stamp detection and duplicate result,
plus one Azure Layout OCR result of the same file), then the built-in risk
rules, and asserts the expected-results table:

| Rule                                  | CASE-DE627FA2 (GIPA)  | CASE-DB43653A (Headstart) |
|---------------------------------------|-----------------------|---------------------------|
| duplicate.cross_case_match            | either                | either                    |
| metadata.javascript_or_openaction v2  | pass                  | pass                      |
| metadata.rescan_conflict              | pass                  | flag 10                   |
| issuer.not_in_registry                | not checked           | not checked               |
| field.tax_rate_mismatch v2            | pass (5% of 850)      | not applicable            |
| field.line_item_arithmetic_mismatch   | no qty x rate lines   | flag 25 (term 3)          |
| field.subtotal_line_item_mismatch v2  | pass                  | flag 15 (diff 400)        |
| field.amount_in_words_mismatch        | not applicable        | flag 25                   |
| field.period_quantity_mismatch        | not applicable        | flag 10 (term 1)          |
| field.iban_trn_validation             | pass                  | pass                      |
| font.inconsistency_scanned            | pass                  | flag 25 (incl. 6,000)     |
| visual.alignment / sharpness v2       | pass                  | pass                      |
| ELA / copy-move / AI-generation       | pass                  | pass                      |
| score                                 | 40 Medium (0 w/o dup) | 100 High (raw 150 / 110)  |

CASE-F6F5FE76 (British Orchard Nursery): a scan converted to editable text
(Acrobat "Edit scanned document"), its total retyped, saved by iLovePDF.

| Rule                                         | CASE-F6F5FE76               |
|----------------------------------------------|-----------------------------|
| font.inconsistency v2                        | flag 40 ("6", "000"; not "03") |
| metadata.modified_after_creation             | flag 15 (315 days)          |
| metadata.document_date_after_file_creation   | flag 25 (dated 314 days after the file) |
| metadata.history_scanned_doc_edited          | flag 15 (editedScannedDoc)  |
| metadata.pdf_editor_producer                 | flag 10 (iLovePDF)          |
| metadata.editable_text_over_scan             | flag 10 (converter fonts over a scan) |
| metadata group (all metadata.* above)        | 75 points, capped at 40     |
| signature.stamp_issuer_mismatch              | pass (stamp: British Orchard Nursery Br.1) |
| field.subtotal_line_item_mismatch            | pass (4 lines from the table = 36,000) |
| ELA / copy-move                              | limited (vector text over a scan) |
| content.deleted_ghost_block / replaced_line  | pass (every ghost has its text on top) |
| issuer.not_in_registry                       | not checked                 |
| score                                        | 80 High (font 40 + metadata 40) |

Extraction clean-up: the footer "DATE OF ISSUE 14.03.2022" is labelled
form_template_date; "prepared_by" drops "Dalab", the signature read as a word.

CASE-39CB18BF (Ajyal International School): a scan converted to editable
text in Acrobat; the bank-transfer details under the fee table deleted, the
amounts, totals and the student's name retyped (each in an extra copy of
its font), the producer metadata scrubbed.

| Rule                                         | CASE-39CB18BF                |
|----------------------------------------------|------------------------------|
| font.inconsistency v3                        | flag 40 (17 spans in extra subsets) |
| content.deleted_ghost_block                  | flag 25 (6-line block, x 30–270, y 388–480 pt) |
| content.replaced_ghost_line                  | flag 10 ("Registration Fees")  |
| metadata.modified_after_creation             | flag 15 (8 days)             |
| metadata.editable_text_over_scan v2          | flag 10 (ordinary subset fonts) |
| metadata.producer_scrubbed                   | flag 10 (Adobe XMP Core, no producer) |
| metadata group                               | 35 points (under the 40 cap) |
| score                                        | 100 High (raw 110)           |
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from app.models.risk_rule import RiskRule
from app.services.field_validation_service import validate_fields
from app.services.forensics.copy_move import run_copy_move_check
from app.services.forensics.ela import run_ela_check
from app.services.forensics.font_consistency import analyze_font_consistency
from app.services.forensics.ghost_content import analyze_ghost_content, exclude_signature_regions
from app.services.forensics.metadata_forensics import analyze_pdf_metadata
from app.services.forensics.page_structure import analyze_page_structure, limit_pixel_check
from app.services.forensics.pdf_render import render_pdf_pages
from app.services.issuer_service import verify_issuer
from app.services.line_item_parsing import enrich_extracted_fields
from app.services.risk_rule_seed import SEED_RULE_VERSIONS, SEED_RULES
from app.services.risk_scoring_service import _Evidence, capped_raw_score, evaluate_rules
from app.services.visual_inconsistency_service import apply_region_filters
from tests.regression.harness import ocr_from_json

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "golden"
METADATA_CAP = 40  # risk_settings.metadata_score_cap default


def _rules() -> list[RiskRule]:
    rules = []
    for spec in SEED_RULES:
        rule = RiskRule(**spec, is_active=True, version=SEED_RULE_VERSIONS.get(spec["rule_id"], 1))
        rule.id = uuid.uuid5(uuid.NAMESPACE_URL, spec["rule_id"])
        rules.append(rule)
    return rules


def _run(case: str, db) -> dict:
    stored = json.loads((FIXTURES / case / "stored.json").read_text(encoding="utf-8"))
    ocr = ocr_from_json(json.loads((FIXTURES / case / "ocr.json").read_text(encoding="utf-8")))
    pdf = (ROOT / stored["pdf"]).read_bytes()
    pages = render_pdf_pages(pdf)
    company = db.info["test_company_id"]
    structure = analyze_page_structure(pdf)

    detected = stored["signature_stamp_detection"]["details"].get("detected") or []
    fields = enrich_extracted_fields(
        stored["extracted_fields"], ocr_text=ocr.text, ocr_tables=ocr.tables, pdf_bytes=pdf,
        ocr_pages=ocr.pages, signature_regions=detected,
    )
    checks = {
        "metadata_forensics": analyze_pdf_metadata(pdf),
        "error_level_analysis": limit_pixel_check(run_ela_check(pages), structure, "error level analysis"),
        "copy_move_detection": limit_pixel_check(run_copy_move_check(pages), structure, "copy-move detection"),
        "font_consistency": analyze_font_consistency(pdf, ocr.pages),
        "ghost_content": exclude_signature_regions(analyze_ghost_content(pdf), detected),
        "field_validation": validate_fields(
            fields, ocr_text=stored["ocr_text"], stamps=[d for d in detected if d["kind"] == "stamp"]
        ),
        "issuer_verification": verify_issuer(
            db, company, fields, document_type=stored["document_type"]
        ),
        "visual_inconsistency_review": apply_region_filters(
            stored["visual_inconsistency_review"], stored["signature_stamp_detection"], pages
        ),
        "signature_stamp_detection": stored["signature_stamp_detection"],
        "duplicate_detection": stored["duplicate_detection"],
    }
    doc = SimpleNamespace(id=uuid.uuid4(), original_filename=Path(stored["pdf"]).name)
    evidence = _Evidence(
        [doc], {(doc.id, ct): SimpleNamespace(id=uuid.uuid4(), result=r) for ct, r in checks.items()}, [], []
    )
    fired, _ = evaluate_rules(_rules(), evidence)
    raw, caps = capped_raw_score(fired, METADATA_CAP)
    return {
        "checks": checks,
        "fields": fields,
        "rules": {f.rule.rule_id: (f.rule.weight, f.rule.version, f.reason) for f in fired},
        "raw": raw,
        "caps": caps,
    }


def _sub(run: dict, name: str) -> dict:
    return run["checks"]["field_validation"]["details"][name]


# --- CASE-DE627FA2: The Gulf International Private Academy ------------------------------


def test_gipa_rule_table(db_session):
    run = _run("CASE-DE627FA2", db_session)
    rules = run["rules"]
    # False positives gone.
    for rule in ("metadata.javascript_or_openaction", "issuer.not_in_registry", "field.tax_rate_mismatch",
                 "visual.alignment_inconsistency", "visual.sharpness_inconsistency"):
        assert rule not in rules, rule
    # The document is internally consistent: nothing else fires either.
    for rule in ("metadata.rescan_conflict", "field.line_item_arithmetic_mismatch",
                 "field.subtotal_line_item_mismatch", "field.amount_in_words_mismatch",
                 "field.period_quantity_mismatch", "field.iban_trn_validation", "font.inconsistency_scanned",
                 "ela.tamper_region_detected", "copy_move.cluster_detected", "ai.generated_content_suspected"):
        assert rule not in rules, rule
    assert set(rules) <= {"duplicate.cross_case_match"}
    score = sum(w for w, _, _ in rules.values())
    assert score == (40 if "duplicate.cross_case_match" in rules else 0)


def test_gipa_check_details(db_session):
    run = _run("CASE-DE627FA2", db_session)
    assert _sub(run, "tax_rate_consistency")["status"] == "pass"
    assert _sub(run, "tax_rate_consistency")["reason"].startswith("Tax applies to {Uniform, Technology Fee} = 850.00")
    subtotal = _sub(run, "subtotal_line_item_consistency")
    assert subtotal["status"] == "pass" and len(subtotal["summed_lines"]) == 5
    iban = _sub(run, "iban_trn_validation")
    assert iban["status"] == "pass"
    assert {i["value"] for i in iban["identifiers"]} == {"AE250030000260337020001", "100216382000003"}
    assert [i["source"] for i in run["fields"]["line_items"]] == ["table"] * 5
    # Empty registry: nothing to check the issuer against.
    assert run["checks"]["issuer_verification"]["result"] == "not_checked"
    assert run["checks"]["metadata_forensics"]["result"] == "pass"
    assert run["checks"]["visual_inconsistency_review"]["result"] == "pass"


# --- CASE-DB43653A: Headstart Nursery --------------------------------------------------------


def test_headstart_rule_table(db_session):
    run = _run("CASE-DB43653A", db_session)
    rules = run["rules"]
    for rule in ("metadata.javascript_or_openaction", "issuer.not_in_registry", "visual.alignment_inconsistency",
                 "visual.sharpness_inconsistency", "field.iban_trn_validation", "field.tax_rate_mismatch",
                 "ela.tamper_region_detected", "copy_move.cluster_detected"):
        assert rule not in rules, rule
    expected = {
        "metadata.rescan_conflict": 10,
        "field.line_item_arithmetic_mismatch": 25,
        "field.subtotal_line_item_mismatch": 15,
        "field.amount_in_words_mismatch": 25,
        "field.period_quantity_mismatch": 10,
        "font.inconsistency_scanned": 25,
    }
    for rule, weight in expected.items():
        assert rules[rule][0] == weight, rule
    assert set(rules) - set(expected) <= {"duplicate.cross_case_match"}
    raw = sum(w for w, _, _ in rules.values())
    assert raw == (150 if "duplicate.cross_case_match" in rules else 110)
    assert min(100, raw) == 100


def test_headstart_check_details(db_session):
    run = _run("CASE-DB43653A", db_session)
    subtotal = _sub(run, "subtotal_line_item_consistency")
    assert subtotal["reason"].startswith("Line items = 55,100.00, stated total 55,500.00, difference 400.00")
    assert subtotal["excluded_lines"] == ["line 1 (REGISTRATION FEES): 500.00"]
    assert "3 × 6,000.00 = 18,000.00" in _sub(run, "line_item_arithmetic")["reason"]
    assert "3 months listed as due" in _sub(run, "period_quantity_consistency")["reason"]
    assert "× 4 charged" in _sub(run, "period_quantity_consistency")["reason"]
    assert _sub(run, "rescan_conflict")["reason"].startswith("Printed and re-scanned")
    assert _sub(run, "iban_trn_validation")["status"] == "pass"
    assert run["checks"]["issuer_verification"]["result"] == "not_checked"
    amounts = {
        f["data"]["text"] for f in run["checks"]["font_consistency"]["details"]
        if f["finding"] == "font_inconsistency" and f["severity"] == "medium"
    }
    assert {"5,500", "22,000", "16,500", "6,000", "16,600", "55,500", "4", "MONTH"} <= amounts
    assert run["checks"]["visual_inconsistency_review"]["result"] == "pass"
    assert run["checks"]["metadata_forensics"]["result"] == "pass"


# --- CASE-F6F5FE76: British Orchard Nursery (scan converted to editable text) ---------------


def test_british_orchard_rule_table(db_session):
    run = _run("CASE-F6F5FE76", db_session)
    rules = run["rules"]
    expected = {
        "font.inconsistency": 40,
        "metadata.modified_after_creation": 15,
        "metadata.document_date_after_file_creation": 25,
        "metadata.history_scanned_doc_edited": 15,
        "metadata.pdf_editor_producer": 10,
        "metadata.editable_text_over_scan": 10,
    }
    for rule, weight in expected.items():
        assert rules[rule][0] == weight, rule
    assert rules["font.inconsistency"][1] == 3
    assert set(rules) - set(expected) <= {"duplicate.cross_case_match"}
    # Ghosts under the converted text, none without it.
    assert run["checks"]["ghost_content"]["result"] == "pass"
    # One edit, five metadata traces: 75 points, counted as 40.
    assert run["caps"] == {"metadata": {"cap": 40, "points": 75}}
    assert run["raw"] == (120 if "duplicate.cross_case_match" in rules else 80)
    assert run["raw"] >= 60  # High


def test_british_orchard_font_findings(db_session):
    run = _run("CASE-F6F5FE76", db_session)
    font = run["checks"]["font_consistency"]["details"]
    flagged = [f for f in font if f["severity"] in ("medium", "high")]
    # The retyped digits, in the full font; not "03" (two converter subsets side by side).
    assert sorted(f["data"]["text"] for f in flagged) == ["000", "6"]
    for f in flagged:
        assert f["data"]["font"] == "Comic Sans MS-Bold"
        assert f["data"]["expected_font"] == "Comic Sans MS-Bold-6003"
        assert f["data"]["converted_scan"] is True
        assert f["data"]["original_subset_digits"] == ["1", "3", "5", "7", "9"]
        assert f["data"]["size_change"] == {"number": "36,000", "sizes": [11.7, 12.0]}
    assert font[0]["data"]["pages"] == [{"page": 1, "source": "text_layer", "converted_scan": True}]


def test_british_orchard_check_details(db_session):
    run = _run("CASE-F6F5FE76", db_session)
    findings = {f["finding"]: f for f in run["checks"]["metadata_forensics"]["details"]}
    assert findings["pdf_editor_detected"]["data"]["matched_name"] == "ilovepdf"
    assert findings["history_scanned_document_edited"]["data"]["action"] == "editedScannedDoc"
    assert findings["editable_text_over_scan"]["severity"] == "medium"
    assert findings["editable_text_over_scan"]["data"]["converter_font_pages"] == [1]
    date_check = _sub(run, "document_date_vs_file_creation")
    assert date_check["status"] == "flag"
    assert date_check["reason"].startswith(
        "The document is dated 2024-09-02, but its PDF file was created on 2023-10-24 — 314 days earlier"
    )
    subtotal = _sub(run, "subtotal_line_item_consistency")
    assert subtotal["status"] == "pass" and len(subtotal["summed_lines"]) == 4
    assert [i["source"] for i in run["fields"]["line_items"]] == ["table"] * 4
    assert [i["line_total"] for i in run["fields"]["line_items"]] == [12000, 9000, 9000, 6000]
    for check in ("error_level_analysis", "copy_move_detection"):
        assert run["checks"][check]["result"] == "limited"
        assert run["checks"][check]["details"][0]["finding"] == "pixel_analysis_limited"
    assert run["checks"]["issuer_verification"]["result"] == "not_checked"
    stamp = _sub(run, "stamp_issuer_consistency")
    assert stamp["status"] == "pass" and "BRITISH ORCHARD NURSERY Br.1" in stamp["reason"]
    extra = {f["field_name"]: f for f in run["fields"]["additional_fields"]}
    assert extra["form_template_date"]["value"] == "2022-03-14"
    assert extra["form_template_date"]["field_name_as_extracted"] == "date_of_issue"
    assert "date_of_issue" not in extra
    assert extra["prepared_by"]["value"] == "Princess"
    assert extra["prepared_by"]["value_as_read"] == "Princess Dalab"


# --- CASE-39CB18BF: Ajyal International School (converted scan, content deleted) ------------


def test_ajyal_rule_table(db_session):
    run = _run("CASE-39CB18BF", db_session)
    rules = run["rules"]
    expected = {
        "font.inconsistency": 40,
        "content.deleted_ghost_block": 25,
        "content.replaced_ghost_line": 10,
        "metadata.modified_after_creation": 15,
        "metadata.editable_text_over_scan": 10,
        "metadata.producer_scrubbed": 10,
    }
    for rule, weight in expected.items():
        assert rules[rule][0] == weight, rule
    assert rules["font.inconsistency"][1] == 3
    assert rules["metadata.editable_text_over_scan"][1] == 2
    assert set(rules) - set(expected) <= {"duplicate.cross_case_match"}
    assert run["caps"] == {}  # metadata 35 points: under the cap
    assert run["raw"] == (150 if "duplicate.cross_case_match" in rules else 110)


def test_ajyal_font_subset_findings(db_session):
    run = _run("CASE-39CB18BF", db_session)
    splits = [f for f in run["checks"]["font_consistency"]["details"] if f["finding"] == "font_subset_split"]
    by_subset: dict[str, list[str]] = {}
    for f in splits:
        assert f["severity"] == "high" and f["bounding_box"]["page"] == 1
        by_subset.setdefault(f["data"]["subset"], []).append(f["data"]["text"])
    # The amounts (both columns) in a digits-only copy of Times New Roman;
    # 4,575.00 stays in the main copy.
    assert sorted(by_subset["JIXQHL"]) == sorted(["1,450.00", "16,450.00", "2,300.00", "13,350.00", "13,350.00"] * 2)
    assert {f["data"]["subset_glyphs"] for f in splits if f["data"]["subset"] == "JIXQHL"} == {",.0123456"}
    # The totals and the amount in words in a third bold copy.
    assert sorted(by_subset["AKJOLD"]) == sorted(["51,475.00"] * 3 + ["Fifty-One Thousand Four Hundred Seventy-Five Only"])
    # The student's first name, ID and class in a second bold copy.
    assert sorted(by_subset["MONKPZ"]) == ["2820", "AL REEM", "PRE-KG-NA"]


def test_ajyal_ghost_and_metadata_details(db_session):
    run = _run("CASE-39CB18BF", db_session)
    ghost = run["checks"]["ghost_content"]
    [block] = [f for f in ghost["details"] if f["finding"] == "ghost_deleted_block"]
    x0, y0, x1, y1 = block["data"]["box_pt"]
    assert 25 <= x0 <= 45 and 380 <= y0 <= 400 and 260 <= x1 <= 290 and 470 <= y1 <= 495
    [line] = [f for f in ghost["details"] if f["finding"] == "ghost_replaced_line"]
    assert line["data"]["text"] == "Registration Fees"
    findings = {f["finding"]: f for f in run["checks"]["metadata_forensics"]["details"]}
    assert findings["producer_scrubbed"]["data"]["xmp_toolkit"].startswith("Adobe XMP Core 9.1")
    assert findings["editable_text_over_scan"]["severity"] == "medium"
    assert findings["editable_text_over_scan"]["data"]["converter_font_pages"] == []
    for check in ("error_level_analysis", "copy_move_detection"):
        assert run["checks"][check]["result"] == "limited"
