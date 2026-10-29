"""
FastAPI application entrypoint.

Auth, cases, document upload + the analysis pipeline, risk scoring,
reviewer actions, per-case PDF report export, the admin Settings API and
the audit history, and the platform-admin API (companies, billing/usage, queue
monitor) are wired up. Multi-tenant: see docs/multi-tenancy.md. MIS/compliance reporting (app/api/reports.py) is still a stub.
"""
from fastapi import FastAPI, Request

from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.bulk_uploads import router as bulk_uploads_router
from app.api.case_actions import router as case_actions_router
from app.api.case_reports import router as case_reports_router
from app.api.cases import router as cases_router
from app.api.documents import router as documents_router
from app.api.platform import router as platform_router
from app.api.settings import router as settings_router
from app.api.signatures import router as signatures_router
from app.core.config import APP_FULL_NAME, APP_NAME, settings
from app.core.security import check_secret_strength

check_secret_strength()  # refuse to start with a guessable JWT signing key

_docs_enabled = (
    settings.api_docs_enabled
    if settings.api_docs_enabled is not None
    else settings.environment.lower() != "production"
)

app = FastAPI(
    title=f"{APP_NAME} — {APP_FULL_NAME} API",
    version="0.1.0",
    description=(
        f"{APP_NAME} ({APP_FULL_NAME}) backend. "
        f"Environment: {settings.environment}."
    ),
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

# Sent on every API response. The API returns JSON only: nothing of it may be
# framed, sniffed into another type, or leak the URL onward. Auth responses
# are also never cached (set per route in app/api/auth.py).
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if request.url.path.startswith("/auth"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response

app.include_router(auth_router)
app.include_router(cases_router)
app.include_router(case_actions_router)
app.include_router(case_reports_router)
app.include_router(documents_router)
app.include_router(bulk_uploads_router)
app.include_router(signatures_router)
app.include_router(settings_router)
app.include_router(audit_router)
app.include_router(platform_router)


@app.get(
    "/health",
    tags=["health"],
    summary="Liveness check",
    description="Unauthenticated. Returns `{status: ok, environment}`; does not touch the database, Redis or Azure.",
)
def health_check() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}
