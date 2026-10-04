"""
Load-test stand-ins for the three EXTERNAL services — and nothing else.

Everything real stays real: the FastAPI app, Postgres (with RLS), the Celery
workers on the three queues, Redis, the global rate limiter, and the CPU
forensics (ELA, copy-move, duplicate hashing, metadata) on real sample PDFs.
Only these are replaced, so a load test costs nothing and uploads nothing:

* Azure Blob Storage  -> files on local disk (LOADTEST_STORAGE_DIR)
* Azure Document Intelligence SDK client -> sleeps LOADTEST_DI_LATENCY
* Azure OpenAI SDK client -> sleeps LOADTEST_LLM_LATENCY, returns
  schema-valid canned JSON

The fakes are installed at the SDK-client level, so the app's own OCR/LLM
service code — including its calls into app/services/rate_limiter.py and its
429 handling — runs unchanged. Each fake also enforces a simulated Azure
server-side quota (LOADTEST_AZURE_* below) and answers 429 above it, so the
run shows whether the app's limiter keeps it from ever being hit.

Never import this from the app — only loadtest/run_api.py and
loadtest/run_worker.py install it, and they refuse to run unless
ENVIRONMENT=local.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import redis

STORAGE_DIR = Path(os.environ.get("LOADTEST_STORAGE_DIR", Path(tempfile.gettempdir()) / "fddt-loadtest-blobs"))
DI_LATENCY = float(os.environ.get("LOADTEST_DI_LATENCY", "1.5"))
LLM_LATENCY = float(os.environ.get("LOADTEST_LLM_LATENCY", "1.0"))
# The simulated Azure quotas (what real Azure would 429 above).
AZURE_DI_TPS = float(os.environ.get("LOADTEST_AZURE_DI_TPS", "15"))
AZURE_OPENAI_RPM = float(os.environ.get("LOADTEST_AZURE_OPENAI_RPM", "1000"))
AZURE_OPENAI_TPM = float(os.environ.get("LOADTEST_AZURE_OPENAI_TPM", "1000000"))

_BASE_URL = "https://loadtest.invalid/documents/"


def _redis():
    from app.core.config import settings

    return redis.Redis.from_url(settings.celery_broker_url)


def _azure_quota_hit(service: str, limit: float, window: float, cost: int = 1) -> bool:
    """Simulated server-side quota: True (=> 429) if this call would exceed it."""
    r = _redis()
    key = f"loadtest:azure:{service}"
    now = time.time()
    r.zremrangebyscore(key, "-inf", now - window)
    used = sum(int(m.decode().split(":", 1)[0]) for m in r.zrange(key, 0, -1))
    r.rpush(f"loadtest:calls:{service}", f"{now:.3f}")
    if used + cost > limit:
        r.incr(f"loadtest:azure:{service}:429")
        return True
    r.zadd(key, {f"{cost}:{uuid.uuid4().hex}": now})
    r.expire(key, int(window) + 5)
    return False


# ---------------------------------------------------------------------------
# Blob storage -> local disk
# ---------------------------------------------------------------------------


def _local_storage_class():
    from app.services.storage_service import StorageService

    import hashlib

    class LocalDiskStorage(StorageService):
        # Flat, hashed file names: the company/case/document blob path nests
        # deeply enough to exceed Windows' 260-character path limit.
        @staticmethod
        def _file(blob_path: str) -> Path:
            return STORAGE_DIR / hashlib.sha1(blob_path.encode()).hexdigest()

        def _path(self, file_url: str) -> Path:
            return self._file(file_url.split("?", 1)[0].split(_BASE_URL, 1)[-1])

        def upload(self, blob_path, content, content_type=None):
            path = self._file(blob_path)
            if path.exists():
                raise FileExistsError(blob_path)
            path.write_bytes(content)
            return _BASE_URL + blob_path

        def get_download_url(self, file_url, expires_in_minutes=15):
            return f"{file_url}?loadtest-signature"

        def download_bytes(self, file_url):
            return self._path(file_url).read_bytes()

    return LocalDiskStorage


# ---------------------------------------------------------------------------
# Azure Document Intelligence SDK client
# ---------------------------------------------------------------------------


class _FakePoller:
    def result(self):
        time.sleep(DI_LATENCY)
        word = SimpleNamespace(content="INVOICE", polygon=[1.0, 1.0, 2.5, 1.0, 2.5, 1.3, 1.0, 1.3])
        page = SimpleNamespace(page_number=1, width=8.5, height=11.0, words=[word], lines=[])
        return SimpleNamespace(
            content="INVOICE\nLoadtest Supplies LLC\nInvoice No: LT-001\nDate: 2026-09-01\nTotal: 1,250.00 USD",
            tables=[],
            key_value_pairs=[],
            pages=[page],
        )


class FakeDocumentIntelligenceClient:
    def __init__(self, *args, **kwargs):
        pass

    def begin_analyze_document(self, *args, **kwargs):
        if _azure_quota_hit("document_intelligence", AZURE_DI_TPS, 1.0):
            from azure.core.exceptions import HttpResponseError

            error = HttpResponseError(message="(429) Too Many Requests (simulated Azure quota)")
            error.status_code = 429
            raise error
        return _FakePoller()


# ---------------------------------------------------------------------------
# Azure OpenAI SDK client
# ---------------------------------------------------------------------------


def _canned(schema_name: str) -> dict:
    field = {"value": None, "confidence": 0.9, "uncertain": False}
    amount = {"value": None, "raw_text": None, "confidence": 0.9, "uncertain": False, "currency": None}
    consistent = {"consistent": True, "description": None, "confidence": None, "bounding_box": None}
    if schema_name == "document_analysis":
        return {
            "document_type": "vendor_invoice",
            "document_type_confidence": 0.95,
            "issuer": {"value": "Loadtest Supplies LLC", "confidence": 0.95, "uncertain": False},
            "reference_number": {"value": "LT-001", "confidence": 0.95, "uncertain": False},
            "date": {"value": "2026-09-01", "raw_text": "2026-09-01", "confidence": 0.95, "uncertain": False},
            "amount": {**amount, "value": 1250.0, "raw_text": "1,250.00", "currency": "USD"},
            "subtotal": amount,
            "tax_amount": amount,
            "tax_rate": {"value": None, "raw_text": None, "confidence": 0.9, "uncertain": False},
            "additional_fields": [],
        }
    if schema_name == "page_visual_analysis":
        return {
            "font_consistency": consistent,
            "text_alignment": consistent,
            "color_contrast_consistency": consistent,
            "resolution_sharpness_consistency": consistent,
            "shadow_lighting_consistency": consistent,
            "ai_generation_assessment": {
                "likely_ai_generated": False, "description": "Load-test stub.", "confidence": "low",
            },
        }
    if schema_name == "signature_detection":
        return {"signature_expected": True, "regions": []}
    if schema_name == "entity_match":
        return {"matched_candidate": None, "reasoning": "Load-test stub."}
    if schema_name == "signature_comparison":
        return {"reasoning": "Load-test stub.", "result": "cannot_determine"}
    return field


class _FakeCompletions:
    def create(self, *, model, messages, response_format, max_tokens, temperature=0, **kwargs):
        from app.services.llm_service import _estimate_request_tokens

        cost = _estimate_request_tokens(messages[0]["content"], messages[1]["content"], max_tokens)
        if _azure_quota_hit("openai_requests", AZURE_OPENAI_RPM, 60.0) or _azure_quota_hit(
            "openai_tokens", AZURE_OPENAI_TPM, 60.0, cost
        ):
            import httpx
            import openai

            response = httpx.Response(
                429, headers={"retry-after": "2"}, request=httpx.Request("POST", "https://loadtest.invalid")
            )
            raise openai.RateLimitError("Rate limit exceeded (simulated Azure quota)", response=response, body=None)
        time.sleep(LLM_LATENCY)
        content = json.dumps(_canned(response_format["json_schema"]["name"]))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class FakeAzureOpenAI:
    def __init__(self, *args, **kwargs):
        self.chat = SimpleNamespace(completions=_FakeCompletions())


# ---------------------------------------------------------------------------


def install() -> None:
    from app.core.config import settings

    if settings.environment != "local":
        raise SystemExit("Refusing to install load-test stubs outside ENVIRONMENT=local.")

    import azure.ai.documentintelligence as di_module
    import openai

    import app.services.storage_service as storage_service

    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    storage = _local_storage_class()()
    storage_service._build_storage_service = lambda: storage
    di_module.DocumentIntelligenceClient = FakeDocumentIntelligenceClient
    openai.AzureOpenAI = FakeAzureOpenAI
