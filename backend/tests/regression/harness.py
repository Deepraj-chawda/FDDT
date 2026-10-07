"""
Regression harness for the check pipeline: replays every check on a fixed
set of inputs and records per-document, per-check results and the points
each risk rule awards, so two code versions can be compared line by line.

Why a replay rather than re-running the live pipeline: OCR, field extraction
and the vision-model review are external, non-deterministic calls. Running
them again for a baseline and again after a change would mix model noise into
the diff. So each input is captured ONCE (`inputs`) and both runs (`run`)
replay the checks over exactly the same captured data:

  per distinct file (by SHA-256) among the stored documents:
    - the PDF bytes (from blob storage)
    - one Azure Document Intelligence Layout result (text, tables, words with
      font estimates), taken with the same settings as the pipeline
    - the stored LLM extraction (documents.extracted_fields), the stored
      vision-model review, signature/stamp detection, duplicate detection
      and the company the representative document belongs to

  replayed by `run` with whatever code is checked out:
    - metadata forensics, ELA, copy-move, font consistency  (from the PDF/OCR)
    - extraction post-processing (if the code has any) + field validation
    - issuer verification against the representative company's registry
      (fuzzy match only: the LLM fallback is not called, in either run)
    - the vision-model review, through any post-filter the code applies
    - signature/stamp and duplicate detection: stored results, passed through
    - the risk rules (the built-in rule set) evaluated on those results

Usage (from backend/):
    python -m tests.regression.harness inputs
    python -m tests.regression.harness run --out tests/baseline
    python -m tests.regression.harness run --out tests/regression_after
    python -m tests.regression.harness diff tests/baseline tests/regression_after \
        --report tests/regression_diff.md

The input cache (tests/regression/_cache/) holds client PDFs and is not
committed.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import inspect
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

HERE = Path(__file__).resolve().parent
CACHE = HERE / "_cache"

# Companies created by the load-test scripts carry the same sample files many
# times over; a real company's copy of a file is preferred as its
# representative.
_LOADTEST_PREFIX = "Loadtest"


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------

def _ocr_to_json(result) -> dict[str, Any]:
    return {
        "text": result.text,
        "tables": [dataclasses.asdict(t) for t in result.tables],
        "key_value_pairs": result.key_value_pairs,
        "pages": [dataclasses.asdict(p) for p in result.pages],
    }


def ocr_from_json(data: dict[str, Any]):
    from app.services.ocr_service import OCRPage, OCRResult, OCRTable, OCRWord

    return OCRResult(
        text=data["text"],
        tables=[OCRTable(**t) for t in data["tables"]],
        key_value_pairs=data["key_value_pairs"],
        pages=[
            OCRPage(
                page_number=p["page_number"],
                words=[OCRWord(**w) for w in p["words"]],
                lines=[OCRWord(**w) for w in p["lines"]],
            )
            for p in data["pages"]
        ],
    )


def _analyze_bytes(pdf_bytes: bytes):
    """Layout analysis of raw bytes, with the pipeline's own settings and
    result mapping (app/services/ocr_service.py)."""
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest, DocumentAnalysisFeature

    from app.core.config import settings
    from app.services.ocr_service import OCRResult, OCRTable, get_ocr_service, pages_from_result

    service = get_ocr_service()
    features = [DocumentAnalysisFeature.STYLE_FONT] if settings.azure_document_intelligence_style_font else None
    result = service._client.begin_analyze_document(
        "prebuilt-layout", body=AnalyzeDocumentRequest(bytes_source=pdf_bytes), features=features
    ).result()
    tables = [
        OCRTable(
            row_count=t.row_count,
            column_count=t.column_count,
            cells=[{"row_index": c.row_index, "column_index": c.column_index, "content": c.content} for c in t.cells or []],
        )
        for t in result.tables or []
    ]
    kv = {
        kv.key.content: kv.value.content
        for kv in (result.key_value_pairs or [])
        if kv.key and kv.value and kv.key.content
    }
    return OCRResult(text=result.content or "", tables=tables, key_value_pairs=kv, pages=pages_from_result(result))


def build_inputs(limit: int | None = None) -> None:
    from sqlalchemy import select

    from app.db.session import system_session
    from app.models.case import Case
    from app.models.company import Company
    from app.models.document import Document, DocumentProcessingStatus
    from app.models.document_check import DocumentCheck, DocumentCheckStatus
    from app.services.storage_service import get_storage_service_for_task

    storage = get_storage_service_for_task()
    CACHE.mkdir(exist_ok=True)
    with system_session() as db:
        rows = db.execute(
            select(Document, Case.case_number, Company.name)
            .join(Case, Case.id == Document.case_id)
            .join(Company, Company.id == Document.company_id)
            .where(Document.processing_status == DocumentProcessingStatus.complete)
            .order_by(Document.created_at)
        ).all()
        by_hash: dict[str, list] = {}
        for doc, case_number, company_name in rows:
            by_hash.setdefault(doc.file_hash, []).append((doc, case_number, company_name))

        hashes = sorted(by_hash)[:limit] if limit else sorted(by_hash)
        for n, file_hash in enumerate(hashes, 1):
            target = CACHE / file_hash
            if (target / "stored.json").exists():
                continue
            entries = by_hash[file_hash]
            real = [e for e in entries if not e[2].startswith(_LOADTEST_PREFIX)]
            doc, case_number, company_name = (real or entries)[-1]  # most recent
            print(f"[{n}/{len(hashes)}] {doc.original_filename} ({case_number}, {company_name})", flush=True)
            pdf = storage.download_bytes(doc.blob_storage_path)
            if hashlib.sha256(pdf).hexdigest() != file_hash:
                print("   ! downloaded bytes do not match the stored hash; skipped")
                continue
            checks = {
                c.check_type.value: c.result
                for c in db.execute(
                    select(DocumentCheck).where(
                        DocumentCheck.document_id == doc.id, DocumentCheck.status == DocumentCheckStatus.completed
                    )
                ).scalars()
            }
            target.mkdir(exist_ok=True)
            (target / "document.pdf").write_bytes(pdf)
            ocr = _analyze_bytes(pdf)
            (target / "ocr.json").write_text(json.dumps(_ocr_to_json(ocr)), encoding="utf-8")
            stored = {
                "sha256": file_hash,
                "filename": doc.original_filename,
                "document_id": str(doc.id),
                "document_type": doc.document_type,
                "company_id": str(doc.company_id),
                "company_name": company_name,
                "case_number": case_number,
                "all_cases": sorted({c for _, c, _ in entries}),
                "extracted_fields": doc.extracted_fields,
                "ocr_text": doc.ocr_text,
                "checks": checks,
            }
            (target / "stored.json").write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def _optional(module: str, name: str):
    try:
        mod = __import__(module, fromlist=[name])
    except ImportError:
        return None
    return getattr(mod, name, None)


def _call(fn, *args, **kwargs):
    """Call `fn` with only the keyword arguments its signature accepts, so the
    same harness drives code from before and after a change."""
    params = inspect.signature(fn).parameters
    accepted = {k: v for k, v in kwargs.items() if k in params}
    return fn(*args, **accepted)


def _rules():
    from app.models.risk_rule import RiskRule
    from app.services.risk_rule_seed import SEED_RULES

    versions = _optional("app.services.risk_rule_seed", "SEED_RULE_VERSIONS") or {}
    rules = []
    for spec in SEED_RULES:
        rule = RiskRule(**spec, is_active=True, version=versions.get(spec["rule_id"], 1))
        rule.id = uuid.uuid5(uuid.NAMESPACE_URL, spec["rule_id"])
        rules.append(rule)
    return rules


def _summarize(check_type: str, result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"result": None}
    out: dict[str, Any] = {"result": result.get("result")}
    details = result.get("details")
    if isinstance(details, list):
        out["findings"] = [
            {
                "finding": d.get("finding"),
                "severity": d.get("severity"),
                "page": d.get("page"),
                "text": d.get("description"),
                "bbox": d.get("bounding_box"),
            }
            for d in details
            if isinstance(d, dict) and d.get("severity") != "info"
        ]
    elif isinstance(details, dict):
        subs = {k: v for k, v in details.items() if isinstance(v, dict) and "status" in v}
        if subs:
            out["sub_checks"] = {k: {"status": v["status"], "text": v.get("reason")} for k, v in subs.items()}
        rest = {k: v for k, v in details.items() if k not in subs and k not in ("detected",)}
        if rest:
            out["details"] = rest
        if "detected" in details:
            out["detected"] = [
                {"kind": d.get("kind"), "bbox": d.get("bounding_box")} for d in details.get("detected") or []
            ]
    return out


def replay(entry: Path, db) -> dict[str, Any]:
    from app.services.field_validation_service import validate_fields
    from app.services.forensics.copy_move import run_copy_move_check
    from app.services.forensics.ela import run_ela_check
    from app.services.forensics.font_consistency import analyze_font_consistency
    from app.services.forensics.metadata_forensics import analyze_pdf_metadata
    from app.services.forensics.pdf_render import render_pdf_pages
    from app.services.issuer_service import match_issuer
    from app.services.risk_scoring_service import _Evidence, evaluate_rules

    stored = json.loads((entry / "stored.json").read_text(encoding="utf-8"))
    pdf = (entry / "document.pdf").read_bytes()
    ocr = ocr_from_json(json.loads((entry / "ocr.json").read_text(encoding="utf-8")))
    pages = render_pdf_pages(pdf)
    company_id = uuid.UUID(stored["company_id"])

    results: dict[str, Any] = {}
    results["metadata_forensics"] = analyze_pdf_metadata(pdf)
    results["error_level_analysis"] = run_ela_check(pages)
    results["copy_move_detection"] = run_copy_move_check(pages)
    structure_fn = _optional("app.services.forensics.page_structure", "analyze_page_structure")
    if structure_fn is not None:
        from app.services.forensics.page_structure import limit_pixel_check

        structure = structure_fn(pdf)
        results["error_level_analysis"] = limit_pixel_check(results["error_level_analysis"], structure, "error level analysis")
        results["copy_move_detection"] = limit_pixel_check(results["copy_move_detection"], structure, "copy-move detection")
    results["font_consistency"] = analyze_font_consistency(pdf, ocr.pages)
    ghost_fn = _optional("app.services.forensics.ghost_content", "analyze_ghost_content")
    if ghost_fn is not None:
        from app.services.forensics.ghost_content import exclude_signature_regions

        stored_detection = ((stored["checks"].get("signature_stamp_detection") or {}).get("details") or {})
        results["ghost_content"] = exclude_signature_regions(ghost_fn(pdf), stored_detection.get("detected"))

    fields = copy.deepcopy(stored["extracted_fields"] or {})
    enrich = _optional("app.services.line_item_parsing", "enrich_extracted_fields")
    if enrich is not None:
        fields = _call(
            enrich, fields, ocr_text=ocr.text, ocr_tables=ocr.tables, pdf_bytes=pdf, ocr_pages=ocr.pages,
            signature_regions=((stored["checks"].get("signature_stamp_detection") or {}).get("details") or {}).get("detected"),
        )
    detected = ((stored["checks"].get("signature_stamp_detection") or {}).get("details") or {}).get("detected")
    results["field_validation"] = _call(
        validate_fields, fields, ocr_text=stored["ocr_text"] or ocr.text,
        stamps=None if detected is None else [d for d in detected if d.get("kind") == "stamp"],
    )

    verify = _optional("app.services.issuer_service", "verify_issuer")
    if verify is not None:
        results["issuer_verification"] = _call(
            verify, db, company_id, fields, document_type=stored["document_type"], llm_service=None
        )
    else:
        issuer_name = ((fields.get("core_fields") or {}).get("issuer") or {}).get("value")
        match = match_issuer(db, company_id, issuer_name)
        results["issuer_verification"] = {
            "result": "pass" if match.matched else "flag",
            "details": {
                "issuer_name": issuer_name,
                "matched_registry_name": match.best_match_name,
                "match_score": match.best_match_score,
                "matched_via": match.matched_via,
                "threshold": match.threshold,
            },
        }

    visual = stored["checks"].get("visual_inconsistency_review")
    signature = stored["checks"].get("signature_stamp_detection")
    post_filter = _optional("app.services.visual_inconsistency_service", "apply_region_filters")
    if visual is not None and post_filter is not None:
        visual = post_filter(copy.deepcopy(visual), signature, pages)
    if visual is not None:
        results["visual_inconsistency_review"] = visual
    if signature is not None:
        results["signature_stamp_detection"] = signature
    if stored["checks"].get("duplicate_detection") is not None:
        results["duplicate_detection"] = stored["checks"]["duplicate_detection"]

    doc = SimpleNamespace(id=uuid.UUID(stored["document_id"]), original_filename=stored["filename"])
    checks = {
        (doc.id, ct): SimpleNamespace(id=uuid.uuid5(doc.id, ct), result=res) for ct, res in results.items()
    }
    fired, _warnings = evaluate_rules(_rules(), _Evidence([doc], checks, [], []))
    rules = {}
    for f in sorted(fired, key=lambda f: f.rule.rule_id):
        rules[f.rule.rule_id] = {"version": f.rule.version, "points": f.rule.weight, "reason": f.reason}
    raw = sum(r["points"] for r in rules.values())
    capped = _optional("app.services.risk_scoring_service", "capped_raw_score")
    if capped is not None:
        raw, _caps = capped(fired, 40)  # risk_settings.metadata_score_cap default
    return {
        "document": {
            "file": stored["filename"],
            "sha256": stored["sha256"],
            "representative_case": stored["case_number"],
            "company": stored["company_name"],
            "cases": stored["all_cases"],
        },
        "checks": {ct: _summarize(ct, res) for ct, res in sorted(results.items())},
        "rules": rules,
        "score": {"raw": raw, "score": max(0, min(100, round(raw)))},
    }


def run(out_dir: Path, only: str | None = None) -> None:
    from app.db.session import system_session

    out_dir.mkdir(parents=True, exist_ok=True)
    entries = sorted(p for p in CACHE.iterdir() if (p / "stored.json").exists())
    with system_session() as db:
        for n, entry in enumerate(entries, 1):
            if only and not entry.name.startswith(only):
                continue
            result = replay(entry, db)
            name = f"{Path(result['document']['file']).stem}_{entry.name[:12]}.json"
            (out_dir / name).write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            print(f"[{n}/{len(entries)}] {name}: score {result['score']['score']} ({len(result['rules'])} rules)", flush=True)
            db.rollback()


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def _check_lines(check: dict[str, Any]) -> dict[str, str]:
    """A check flattened to {key: comparable text}."""
    lines = {"result": str(check.get("result"))}
    for i, f in enumerate(check.get("findings") or []):
        lines[f"finding:{f['finding']}#{i}"] = f"{f['severity']} | {f['text']}"
    for name, sub in (check.get("sub_checks") or {}).items():
        lines[f"sub:{name}"] = f"{sub['status']} | {sub['text']}"
    return lines


def diff(before_dir: Path, after_dir: Path) -> str:
    out = ["# Regression diff", ""]
    out.append(f"Before: `{before_dir.as_posix()}` — after: `{after_dir.as_posix()}`.")
    out.append("")
    summary = ["| Document | Cases | Score before | Score after | Rules removed | Rules added |", "|---|---|---|---|---|---|"]
    body: list[str] = []
    for before_file in sorted(before_dir.glob("*.json")):
        after_file = after_dir / before_file.name
        if not after_file.exists():
            body.append(f"## {before_file.stem}\n\nMissing from the after run.\n")
            continue
        b = json.loads(before_file.read_text(encoding="utf-8"))
        a = json.loads(after_file.read_text(encoding="utf-8"))
        removed = sorted(set(b["rules"]) - set(a["rules"]))
        added = sorted(set(a["rules"]) - set(b["rules"]))
        changed_rules = sorted(
            r for r in set(a["rules"]) & set(b["rules"]) if a["rules"][r]["points"] != b["rules"][r]["points"]
        )
        check_changes: list[str] = []
        for ct in sorted(set(b["checks"]) | set(a["checks"])):
            bl, al = _check_lines(b["checks"].get(ct, {})), _check_lines(a["checks"].get(ct, {}))
            for key in sorted(set(bl) | set(al)):
                if bl.get(key) != al.get(key):
                    check_changes.append(f"- `{ct}` `{key}`: **{bl.get(key, '—')}** → **{al.get(key, '—')}**")
        sb, sa = b["score"]["score"], a["score"]["score"]
        if not (removed or added or changed_rules or check_changes or sb != sa):
            continue
        doc = a["document"]
        summary.append(
            f"| {doc['file']} (`{doc['sha256'][:12]}`) | {len(doc['cases'])} | {sb} | {sa} | "
            f"{', '.join(removed) or '—'} | {', '.join(added) or '—'} |"
        )
        body.append(f"## {doc['file']} (`{doc['sha256'][:12]}`)\n")
        body.append(f"Representative case {doc['representative_case']} ({doc['company']}); cases: {', '.join(doc['cases'])}.\n")
        body.append(f"Score {sb} → {sa} (raw {b['score']['raw']} → {a['score']['raw']}).\n")
        for r in removed:
            body.append(f"- rule **removed** `{r}` (−{b['rules'][r]['points']:g}): {b['rules'][r]['reason']}")
        for r in added:
            body.append(f"- rule **added** `{r}` (+{a['rules'][r]['points']:g}): {a['rules'][r]['reason']}")
        for r in changed_rules:
            body.append(f"- rule `{r}` points {b['rules'][r]['points']:g} → {a['rules'][r]['points']:g}")
        if check_changes:
            body.append("")
            body.append("Check-level changes:")
            body.extend(check_changes)
        body.append("")
    unchanged = len(list(before_dir.glob("*.json"))) - (len(summary) - 2)
    out.append(f"{len(summary) - 2} document(s) changed, {unchanged} unchanged.")
    out.append("")
    out.extend(summary)
    out.append("")
    out.extend(body)
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_in = sub.add_parser("inputs")
    p_in.add_argument("--limit", type=int)
    p_run = sub.add_parser("run")
    p_run.add_argument("--out", type=Path, required=True)
    p_run.add_argument("--only")
    p_diff = sub.add_parser("diff")
    p_diff.add_argument("before", type=Path)
    p_diff.add_argument("after", type=Path)
    p_diff.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "inputs":
        build_inputs(args.limit)
    elif args.cmd == "run":
        run(args.out, args.only)
    else:
        report = diff(args.before, args.after)
        if args.report:
            args.report.write_text(report, encoding="utf-8")
        else:
            sys.stdout.write(report)


if __name__ == "__main__":
    main()
