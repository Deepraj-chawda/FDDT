"""
Real-Azure check of the token-weighted rate limiter: fire more vision calls
than the deployment's TPM allows in a minute, from several threads, through
the app's own LLM service (and therefore its limiter), and count 429s.

    python -m scripts.azure_limiter_burst [--calls 40] [--threads 8]

Expected: the limiter spaces the calls to AZURE_OPENAI_MAX_TOKENS_PER_MINUTE
and Azure answers 0 × 429. Cost: ~3k prompt tokens per call on gpt-4.1-mini.
"""
import argparse
import threading
import time
from pathlib import Path

from app.core.config import settings
from app.services import rate_limiter
from app.services.forensics.pdf_render import render_pdf_pages
from app.services.llm_service import LLMOperationError, get_llm_service
from app.services.visual_inconsistency_service import _encode_page_image

SAMPLE = sorted((Path(__file__).resolve().parents[2] / "sample-documents").glob("*.pdf"))[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--calls", type=int, default=40)
    parser.add_argument("--threads", type=int, default=8)
    args = parser.parse_args()

    for key in rate_limiter._redis().scan_iter("fddt:ratelimit:azure_openai*"):
        rate_limiter._redis().delete(key)
    uri = _encode_page_image(render_pdf_pages(SAMPLE.read_bytes())[0])
    llm = get_llm_service()
    done, errors, starts = [], [], []
    lock = threading.Lock()
    remaining = list(range(args.calls))

    def worker():
        while True:
            with lock:
                if not remaining:
                    return
                remaining.pop()
            t = time.time()
            try:
                llm.analyze_page_visual_consistency(uri)
                with lock:
                    done.append(time.time() - t)
                    starts.append(t)
            except LLMOperationError as exc:
                with lock:
                    errors.append(str(exc)[:200])

    t0 = time.time()
    threads = [threading.Thread(target=worker) for _ in range(args.threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    elapsed = time.time() - t0

    stats = rate_limiter.stats(rate_limiter.AZURE_OPENAI_TOKENS, 60.0)
    req = rate_limiter.stats(rate_limiter.AZURE_OPENAI, 60.0)
    rate_limited = [e for e in errors if "429" in e or "rate limit" in e.lower()]
    print(f"caps: {settings.azure_openai_max_requests_per_minute:g} RPM, {settings.azure_openai_max_tokens_per_minute:g} TPM")
    print(f"calls ok: {len(done)}  errors: {len(errors)}  of which 429: {len(rate_limited)}")
    print(f"elapsed {elapsed:.0f} s  => {len(done) / elapsed * 60:.1f} calls/min")
    print(f"token limiter: throttled {stats.get('throttled_calls_total')} calls, waited "
          f"{stats.get('throttled_seconds_total')} s, http_429 {stats.get('http_429_total')}")
    print(f"request limiter: throttled {req.get('throttled_calls_total')}")
    for e in errors[:5]:
        print("  error:", e)


if __name__ == "__main__":
    main()
