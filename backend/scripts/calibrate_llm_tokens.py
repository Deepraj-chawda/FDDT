"""
Calibrate the TPM reservation estimate against REAL Azure OpenAI usage.

The token-weighted rate limiter (app/services/rate_limiter.py) reserves, per
call, an ESTIMATE of the prompt tokens plus max_tokens. If the estimate is too
low the limiter lets Azure 429 us; too high wastes throughput. This script
measures it:

1. Runs the app's real LLM calls on the sample PDFs (sample-documents/):
   classification+extraction, visual review (2 calls/page), signature/stamp
   detection, signature comparison and issuer entity match — through the
   real AzureOpenAILLMService, so every response's `usage` is recorded by
   `_record_usage` (and the calls go through the real rate limiter).
2. Prints, per call type: real prompt tokens vs. the current estimate, real
   completion tokens vs. the max_tokens reserved.
3. Fits CHARS_PER_TOKEN (from text-only calls; the most token-dense document
   wins, so Arabic isn't under-estimated) and IMAGE_TOKENS_PER_PATCH (from
   vision calls; tokens per 32-px image patch, the largest observed ratio
   wins), and prints the constants to put in app/services/llm_service.py.

Classification normally receives Azure Document Intelligence text; here the
PDF's embedded text (PyMuPDF) stands in for it — same documents, same
language, comparable length — so this needs no Document Intelligence calls
or Blob Storage uploads. Cost: a few cents on gpt-4.1-mini.

    python -m scripts.calibrate_llm_tokens [--docs 6] [--out results.json]
"""
from __future__ import annotations

import argparse
import base64
import json
import statistics
from pathlib import Path

import cv2
import pymupdf

from app.services import llm_service as L
from app.services.extraction_service import classify_and_extract
from app.services.forensics.pdf_render import render_pdf_pages
from app.services.rate_limiter import _redis
from app.services.signature_detection_service import detect_signatures
from app.services.visual_inconsistency_service import run_visual_inconsistency_review

SAMPLES = sorted((Path(__file__).resolve().parents[2] / "sample-documents").glob("*.pdf"))
SCHEMAS = ("document_analysis", "page_visual_analysis", "signature_detection", "signature_comparison", "entity_match")


def _crop_uri(page, box) -> str:
    h, w = page.image.shape[:2]
    x, y, bw, bh = box
    crop = page.image[int(y * h): int((y + bh) * h), int(x * w): int((x + bw) * w)]
    ok, buf = cv2.imencode(".png", crop)
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode()


def run_calls(docs: int) -> None:
    llm = L.get_llm_service()
    for path in SAMPLES[:docs]:
        pdf = path.read_bytes()
        with pymupdf.open("pdf", pdf) as d:
            text = "\n".join(page.get_text() for page in d)
        pages = render_pdf_pages(pdf)
        print(f"  {path.name}: {len(pages)} page(s), {len(text)} text chars")
        analysis = classify_and_extract(llm, text)
        run_visual_inconsistency_review(llm, pages)
        detect_signatures(llm, pages, pdf)
        issuer = (analysis.issuer.value or "Unknown Supplier LLC")
        llm.judge_entity_match(issuer, ["Gulf Office Supplies LLC", "مؤسسة النخبة للأنظمة التقنية", "Acme Trading Co"])
        llm.compare_signatures(_crop_uri(pages[-1], (0.55, 0.75, 0.35, 0.12)), _crop_uri(pages[0], (0.1, 0.75, 0.35, 0.12)))


def samples(schema: str) -> list[dict]:
    return [json.loads(x) for x in _redis().lrange(f"fddt:llm_usage:{schema}:samples", 0, -1)]


def report() -> dict:
    out = {}
    print(f"\n{'call type':24s} {'calls':>5s} {'prompt avg':>10s} {'prompt max':>10s} {'est. avg':>9s} "
          f"{'est/real':>8s} {'compl avg':>9s} {'compl max':>9s} {'max_tokens':>10s}")
    for schema in SCHEMAS:
        rows = samples(schema)
        if not rows:
            continue
        p = [r["p"] for r in rows]
        e = [r["e"] for r in rows]
        c = [r["c"] for r in rows]
        stats = {
            "calls": len(rows),
            "prompt_avg": round(statistics.fmean(p)), "prompt_max": max(p), "prompt_min": min(p),
            "estimated_avg": round(statistics.fmean(e)),
            "estimate_to_real": round(statistics.fmean(e) / statistics.fmean(p), 2),
            "completion_avg": round(statistics.fmean(c)), "completion_max": max(c),
            "max_tokens": rows[0]["m"], "images_per_call": rows[0]["i"],
            "text_chars_avg": round(statistics.fmean(r["t"] for r in rows)),
        }
        out[schema] = stats
        print(f"{schema:24s} {stats['calls']:5d} {stats['prompt_avg']:10d} {stats['prompt_max']:10d} "
              f"{stats['estimated_avg']:9d} {stats['estimate_to_real']:8.2f} {stats['completion_avg']:9d} "
              f"{stats['completion_max']:9d} {stats['max_tokens']:10d}")
    return out


def fit() -> dict:
    text_rows = [r for s in ("document_analysis", "entity_match") for r in samples(s) if r["i"] == 0 and r["p"]]
    # Most token-dense observed (fewest chars per token) => never under-reserve text.
    cpt = min(r["t"] / r["p"] for r in text_rows)
    image_rows = [r for s in ("page_visual_analysis", "signature_detection", "signature_comparison")
                  for r in samples(s) if r["i"] > 0 and r.get("x")]
    per_patch = max((r["p"] - r["t"] / cpt) / r["x"] for r in image_rows)
    fitted = {"CHARS_PER_TOKEN": round(cpt, 2), "IMAGE_TOKENS_PER_PATCH": round(per_patch, 3)}
    print(f"\nFitted: CHARS_PER_TOKEN = {fitted['CHARS_PER_TOKEN']}  "
          f"IMAGE_TOKENS_PER_PATCH = {fitted['IMAGE_TOKENS_PER_PATCH']}")
    return fitted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", type=int, default=6)
    parser.add_argument("--out", default="llm_token_calibration.json")
    parser.add_argument("--keep", action="store_true", help="don't clear earlier samples first")
    args = parser.parse_args()
    if not args.keep:
        for schema in SCHEMAS:
            _redis().delete(f"fddt:llm_usage:{schema}", f"fddt:llm_usage:{schema}:samples")
    print(f"Estimator now: CHARS_PER_TOKEN={L.CHARS_PER_TOKEN} IMAGE_TOKENS_PER_PATCH={L.IMAGE_TOKENS_PER_PATCH} "
          f"IMAGE_BASE={L.IMAGE_BASE_TOKENS} OVERHEAD={L.PROMPT_OVERHEAD_TOKENS} MARGIN={L.ESTIMATE_SAFETY_MARGIN}")
    run_calls(args.docs)
    result = {"report": report(), "fitted": fit()}
    Path(args.out).write_text(json.dumps(result, indent=2))
    print(f"Saved {args.out}")


if __name__ == "__main__":
    main()
