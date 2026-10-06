"""
Golden integration tests: the two manually reviewed reference cases.

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
| issuer.not_in_registry                | pass (seeded)         | pass (seeded)             |
| issuer.fee_benchmark_deviation        | flag 20               | flag 20                   |
| field.tax_rate_mismatch v2            | pass (5% of 850)      | not applicable            |
| field.line_item_arithmetic_mismatch   | no qty x rate lines   | flag 25 (term 3)          |
| field.subtotal_line_item_mismatch v2  | pass                  | flag 15 (diff 400)        |
| field.amount_in_words_mismatch        | not applicable        | flag 25                   |
| field.period_quantity_mismatch        | not applicable        | flag 10 (term 1)          |
| field.iban_trn_validation             | pass                  | pass                      |
| font.inconsistency_scanned            | pass                  | flag 25 (incl. 6,000)     |
| visual.alignment / sharpness v2       | pass                  | pass                      |
| ELA / copy-move / AI-generation       | pass                  | pass                      |
| score                                 | 60 (40 without dup)   | 100 (raw 170 / 130)       |
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
from app.services.forensics.metadata_forensics import analyze_pdf_metadata
from app.services.forensics.pdf_render import render_pdf_pages
from app.services.issuer_service import verify_issuer
from app.services.line_item_parsing import enrich_extracted_fields
from app.services.risk_rule_seed import SEED_RULE_VERSIONS, SEED_RULES
from app.services.risk_scoring_service import _Evidence, evaluate_rules
from app.services.visual_inconsistency_service import apply_region_filters
from scripts.seed_reference_issuers import DATA, seed
from tests.regression.harness import ocr_from_json

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "golden"


def _rules() -> list[RiskRule]:
    rules = []
    for spec in SEED_RULES:
        rule = RiskRule(**spec, is_active=True, version=SEED_RULE_VERSIONS.get(spec["rule_id"], 1))
        rule.id = uuid.uuid5(uuid.NAMESPACE_URL, spec["rule_id"])
        rules.append(rule)
    return rules


def _run(case: str, db, *, seeded: bool = True) -> dict:
    stored = json.loads((FIXTURES / case / "stored.json").read_text(encoding="utf-8"))
    ocr = ocr_from_json(json.loads((FIXTURES / case / "ocr.json").read_text(encoding="utf-8")))
    pdf = (ROOT / stored["pdf"]).read_bytes()
    pages = render_pdf_pages(pdf)
    company = db.info["test_company_id"]
    if seeded:
        seed(db, company, json.loads(Path(DATA).read_text(encoding="utf-8")))
        db.commit()

    fields = enrich_extracted_fields(stored["extracted_fields"], ocr_text=ocr.text, ocr_tables=ocr.tables, pdf_bytes=pdf)
    checks = {
        "metadata_forensics": analyze_pdf_metadata(pdf),
        "error_level_analysis": run_ela_check(pages),
        "copy_move_detection": run_copy_move_check(pages),
        "font_consistency": analyze_font_consistency(pdf, ocr.pages),
        "field_validation": validate_fields(fields, ocr_text=stored["ocr_text"]),
        "issuer_verification": verify_issuer(
            db, company, fields, document_type=stored["document_type"], ocr_text=stored["ocr_text"]
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
    return {
        "checks": checks,
        "fields": fields,
        "rules": {f.rule.rule_id: (f.rule.weight, f.rule.version, f.reason) for f in fired},
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
    # Arithmetic is right; nothing else should fire either.
    for rule in ("metadata.rescan_conflict", "field.line_item_arithmetic_mismatch",
                 "field.subtotal_line_item_mismatch", "field.amount_in_words_mismatch",
                 "field.period_quantity_mismatch", "field.iban_trn_validation", "font.inconsistency_scanned",
                 "ela.tamper_region_detected", "copy_move.cluster_detected", "ai.generated_content_suspected"):
        assert rule not in rules, rule
    # The real concern: tuition ~4x the published fee.
    weight, version, reason = rules["issuer.fee_benchmark_deviation"]
    assert weight == 20
    assert "Tuition Fees 52,130.00 is 306% above the published 12,840.00" in reason
    assert set(rules) <= {"issuer.fee_benchmark_deviation", "duplicate.cross_case_match"}
    score = sum(w for w, _, _ in rules.values())
    assert score == (60 if "duplicate.cross_case_match" in rules else 20)


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
    issuer = run["checks"]["issuer_verification"]
    assert issuer["result"] == "pass" and issuer["details"]["matched_registry_name"]
    assert run["checks"]["metadata_forensics"]["result"] == "pass"
    assert run["checks"]["visual_inconsistency_review"]["result"] == "pass"


def test_gipa_issuer_not_checked_without_registry(db_session):
    run = _run("CASE-DE627FA2", db_session, seeded=False)
    assert run["checks"]["issuer_verification"]["result"] == "not_checked"
    assert "issuer.not_in_registry" not in run["rules"]
    assert "issuer.fee_benchmark_deviation" not in run["rules"]


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
        "issuer.fee_benchmark_deviation": 20,
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
    assert raw == (170 if "duplicate.cross_case_match" in rules else 130)
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
    bench = run["checks"]["issuer_verification"]["details"]["fee_benchmark"]
    assert "Total 55,500.00 is 178% above the published 20,000.00" in bench["reason"]
    assert "words fit the published fee" in bench["reason"]
    amounts = {
        f["data"]["text"] for f in run["checks"]["font_consistency"]["details"]
        if f["finding"] == "font_inconsistency" and f["severity"] == "medium"
    }
    assert {"5,500", "22,000", "16,500", "6,000", "16,600", "55,500", "4", "MONTH"} <= amounts
    assert run["checks"]["visual_inconsistency_review"]["result"] == "pass"
    assert run["checks"]["metadata_forensics"]["result"] == "pass"
