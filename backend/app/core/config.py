"""
Application settings.

Every environment-specific value (DB/Redis connections, API keys, secrets)
is loaded from environment variables — nothing is hardcoded here — so the
same code moves from local development to Azure later without changes.
See `.env.example` at the backend root for the full list of variables a
local `.env` file must define.
"""
from functools import lru_cache

from pydantic import Field

# The product's display name — the ONE place backend code gets it from
# (API docs title, PDF report header/footer/metadata, any future email
# templates). Display-level only: internal identifiers (the `docauth`
# database/user, docker container names, the localStorage token key, Python
# package and folder names) deliberately keep their original names.
APP_NAME = "FDDT"
APP_FULL_NAME = "Fraud Document Detection Tool"
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- General ---
    environment: str = Field(default="local", alias="ENVIRONMENT")
    api_v1_prefix: str = Field(default="/api/v1", alias="API_V1_PREFIX")

    # --- Database (PostgreSQL) ---
    # The OWNER connection: used by Alembic migrations and seed.py only. The
    # running app never queries as this role (it owns the tables and, locally,
    # is a superuser — both of which skip Row-Level Security).
    database_url: str = Field(
        default="postgresql+psycopg2://docauth:docauth@localhost:5432/docauth",
        alias="DATABASE_URL",
    )
    # The two roles the app actually connects as (same host/database as
    # DATABASE_URL, different credentials). Created by the multi-tenancy
    # migration (app/db/migrations/versions/a7c1e9d3f5b2_multi_tenancy.py).
    #  - app role: NOSUPERUSER NOBYPASSRLS — every company-scoped request and
    #    pipeline task. RLS policies confine it to the company set on the
    #    transaction.
    #  - platform role: BYPASSRLS — only the explicit platform code path
    #    (sign-in lookup, platform-admin screens, usage reconciliation).
    database_app_user: str = Field(default="fddt_app", alias="DATABASE_APP_USER")
    database_app_password: str = Field(default="fddt_app_local", alias="DATABASE_APP_PASSWORD")
    database_platform_user: str = Field(default="fddt_platform", alias="DATABASE_PLATFORM_USER")
    database_platform_password: str = Field(
        default="fddt_platform_local", alias="DATABASE_PLATFORM_PASSWORD"
    )

    # Connection pooling. When DATABASE_POOLER_HOST is set the two app roles
    # connect through PgBouncer (transaction pooling) and each process keeps
    # NO pool of its own (SQLAlchemy NullPool) — PgBouncer does the pooling,
    # and a second layer of idle connections per process would only multiply
    # server connections. Without a pooler, each process keeps a small
    # SQLAlchemy pool per role (DB_POOL_SIZE + DB_MAX_OVERFLOW) — which must
    # be at least the thread count of a threads-pool worker, or threads wait
    # for a connection. See docs/processing-queues.md for the arithmetic.
    # Migrations/seed (DATABASE_URL) always connect directly.
    database_pooler_host: str | None = Field(default=None, alias="DATABASE_POOLER_HOST")
    database_pooler_port: int = Field(default=6432, alias="DATABASE_POOLER_PORT")
    db_pool_size: int = Field(default=5, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=10, alias="DB_MAX_OVERFLOW")

    # --- Multi-tenancy ---
    # Name of the company the pre-multi-tenancy data is assigned to by the
    # migration.
    default_company_name: str = Field(default="Default Company", alias="DEFAULT_COMPANY_NAME")
    # Every platform-admin read of company data writes a platform-only
    # audit_log row (never visible to the company). 0 = record every single
    # request (default, full fidelity); > 0 collapses identical reads by the
    # same admin within that many seconds.
    platform_access_audit_dedup_seconds: int = Field(
        default=0, alias="PLATFORM_ACCESS_AUDIT_DEDUP_SECONDS"
    )

    # Upload limits are PER COMPANY (companies.max_file_size_mb /
    # max_zip_size_mb, set by a platform admin; app/services/upload_limits.py
    # is the one place they are read). These two values are only the defaults
    # a NEW company is created with. Changing them never changes an existing
    # company.
    default_max_file_size_mb: int = Field(default=10, ge=1, alias="DEFAULT_MAX_FILE_SIZE_MB")
    default_max_zip_size_mb: int = Field(default=300, ge=1, alias="DEFAULT_MAX_ZIP_SIZE_MB")

    # Bulk upload (app/api/bulk_uploads.py, app/services/bulk_upload_service.py):
    # one zip, one top-level folder per case. Every document inside still gets
    # the single-file checks and the company's per-file limit.
    # Safety cap on zip ENTRIES (not cases): stops a zip of millions of tiny
    # entries from tying up the ingestion worker. Far above any real upload.
    bulk_upload_max_entries: int = Field(default=25_000, alias="BULK_UPLOAD_MAX_ENTRIES")
    # Soft warning only — a zip with more cases is still accepted.
    bulk_upload_case_warning_threshold: int = Field(
        default=100, alias="BULK_UPLOAD_CASE_WARNING_THRESHOLD"
    )

    # --- Processing queues (app/tasks/celery_app.py) ---
    # Global (cross-worker, Redis-backed) call-rate ceilings, applied at each
    # external API call — see app/services/rate_limiter.py.
    # Azure Document Intelligence allows 15 analyze TPS by default; stay under
    # it with headroom.
    azure_document_intelligence_max_calls_per_second: float = Field(
        default=10.0, alias="AZURE_DOCUMENT_INTELLIGENCE_MAX_CALLS_PER_SECOND"
    )
    # The Azure OpenAI deployment's requests-per-minute quota (Azure portal >
    # deployment > Rate limit, or the x-ratelimit-limit-requests response
    # header). Set this to ~80% of the deployment's RPM (85% saw a
    # rare 429 at full saturation — two independent sliding windows race).
    azure_openai_max_requests_per_minute: float = Field(
        default=60.0, alias="AZURE_OPENAI_MAX_REQUESTS_PER_MINUTE"
    )
    # The deployment's tokens-per-minute quota (x-ratelimit-limit-tokens).
    # Usually the binding limit for vision calls: Azure counts prompt + the
    # max_tokens reservation per request. ~80% of the quota; 0 disables.
    azure_openai_max_tokens_per_minute: float = Field(
        default=0.0, alias="AZURE_OPENAI_MAX_TOKENS_PER_MINUTE"
    )
    # Worker-pool sizes — read by start-worker.sh; listed here so the queue
    # monitor can report the configured value next to each queue.
    extraction_worker_concurrency: int = Field(default=4, alias="EXTRACTION_WORKER_CONCURRENCY")
    vision_worker_concurrency: int = Field(default=4, alias="VISION_WORKER_CONCURRENCY")
    forensics_worker_concurrency: int = Field(default=0, alias="FORENSICS_WORKER_CONCURRENCY")
    # Fair-share scheduling (app/tasks/fairshare.py): a company's tasks drop
    # one priority level per this many outstanding tasks on a queue. Smaller
    # = a newly arriving company overtakes a big backlog sooner.
    fair_share_bucket_size: int = Field(default=5, alias="FAIR_SHARE_BUCKET_SIZE")
    # The every-minute queue_metrics beat task logs a WARNING `queue_alert`
    # line when a queue's oldest waiting task has waited longer than this
    # (seconds). 0 disables the alert line.
    queue_alert_oldest_waiting_seconds: float = Field(
        default=300.0, alias="QUEUE_ALERT_OLDEST_WAITING_SECONDS"
    )
    # Nightly usage reconciliation (Celery beat), UTC.
    usage_reconciliation_hour_utc: int = Field(default=2, alias="USAGE_RECONCILIATION_HOUR_UTC")
    # Stuck-document recovery (app/tasks/stuck_documents_task.py, every 5
    # minutes): a document still `pending` after PENDING minutes, or still
    # `processing` after PROCESSING minutes, while the extraction queue is
    # empty and idle, lost its tasks (e.g. Redis restarted, or was down at
    # upload) and its pipeline is queued again — at most MAX_REQUEUES times,
    # then it is marked failed. 0 minutes disables that half of the check.
    stuck_document_pending_minutes: int = Field(default=10, alias="STUCK_DOCUMENT_PENDING_MINUTES")
    stuck_document_processing_minutes: int = Field(default=30, alias="STUCK_DOCUMENT_PROCESSING_MINUTES")
    stuck_document_max_requeues: int = Field(default=3, alias="STUCK_DOCUMENT_MAX_REQUEUES")
    stuck_document_batch_size: int = Field(default=200, alias="STUCK_DOCUMENT_BATCH_SIZE")

    # --- Sign-in throttling (app/services/login_throttle.py, Redis) ---
    # After this many failed sign-ins for one email address, that address is
    # refused for LOGIN_LOCKOUT_MINUTES (counted per address, so it works
    # whatever IP the attempts come from). 0 disables.
    login_max_failed_attempts: int = Field(default=5, alias="LOGIN_MAX_FAILED_ATTEMPTS")
    login_lockout_minutes: int = Field(default=15, alias="LOGIN_LOCKOUT_MINUTES")
    # Sign-in attempts (any outcome) per client IP per minute. 0 disables.
    login_max_attempts_per_ip_per_minute: int = Field(
        default=20, alias="LOGIN_MAX_ATTEMPTS_PER_IP_PER_MINUTE"
    )

    # --- Redis / Celery ---
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    celery_broker_url: str = Field(
        default="redis://localhost:6379/0", alias="CELERY_BROKER_URL"
    )
    celery_result_backend: str = Field(
        default="redis://localhost:6379/1", alias="CELERY_RESULT_BACKEND"
    )

    # --- JWT Auth ---
    jwt_secret_key: str = Field(default="changeme-in-.env", alias="JWT_SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    jwt_access_token_expire_minutes: int = Field(
        default=60 * 8, alias="JWT_ACCESS_TOKEN_EXPIRE_MINUTES"
    )

    # --- Azure Blob Storage ---
    azure_storage_connection_string: str | None = Field(
        default=None, alias="AZURE_STORAGE_CONNECTION_STRING"
    )
    azure_storage_container_name: str = Field(
        default="documents", alias="AZURE_STORAGE_CONTAINER_NAME"
    )

    # --- Azure Document Intelligence (OCR / layout) ---
    azure_document_intelligence_endpoint: str | None = Field(
        default=None, alias="AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
    )
    azure_document_intelligence_key: str | None = Field(
        default=None, alias="AZURE_DOCUMENT_INTELLIGENCE_KEY"
    )
    # Ask Layout for its font-style add-on (`styleFont`: an estimated font
    # family per piece of recognized text). The font consistency check
    # (app/services/forensics/font_consistency.py) needs it for scanned pages,
    # which have no text layer. Billed by Azure as an add-on per page; off =
    # fonts on scans are left to the visual review only.
    azure_document_intelligence_style_font: bool = Field(
        default=True, alias="AZURE_DOCUMENT_INTELLIGENCE_STYLE_FONT"
    )

    # --- Azure AI Vision (signature/stamp detection, logo matching) ---
    azure_ai_vision_endpoint: str | None = Field(
        default=None, alias="AZURE_AI_VISION_ENDPOINT"
    )
    azure_ai_vision_key: str | None = Field(default=None, alias="AZURE_AI_VISION_KEY")

    # --- Azure OpenAI ---
    # The classification/extraction and vision model. Called only through
    # app/services/llm_service.py's `LLMService` abstraction, so another
    # provider would be a new implementation class, not a rewrite of the
    # classification/extraction call sites.
    azure_openai_key: str | None = Field(default=None, alias="AZURE_OPENAI_KEY")
    # Accepts either the classic resource endpoint
    # (https://<resource>.openai.azure.com/) or an Azure AI Foundry project
    # endpoint (https://<resource>.services.ai.azure.com/api/projects/<name>)
    # copied from the Foundry portal — AzureOpenAILLMService normalizes
    # either to the bare https://<resource>/ origin the chat-completions API
    # actually needs.
    azure_openai_endpoint: str | None = Field(default=None, alias="AZURE_OPENAI_ENDPOINT")
    azure_openai_deployment_name: str | None = Field(
        default=None, alias="AZURE_OPENAI_DEPLOYMENT_NAME"
    )
    azure_openai_api_version: str = Field(
        default="2024-10-21", alias="AZURE_OPENAI_API_VERSION"
    )
    # The openai SDK's own default request timeout is ~10 minutes with 2
    # retries — on a Celery worker running with a single-task pool (this
    # project's Windows dev setup uses --pool=solo; see README.md), one
    # slow/hung LLM call blocks every OTHER queued task (forensics,
    # duplicate-check, the next document's OCR, ...) for up to that whole
    # window with no visible progress. A much shorter, explicit timeout
    # makes a stuck call fail fast (process_document's existing except
    # Exception handler already marks the document "failed" cleanly on
    # any LLMOperationError) instead of silently starving the queue.
    azure_openai_request_timeout_seconds: float = Field(
        default=60.0, alias="AZURE_OPENAI_REQUEST_TIMEOUT_SECONDS"
    )

    # --- Issuer registry fuzzy matching (rapidfuzz) ---
    # Below this score (0-100, rapidfuzz's WRatio scale), an extracted
    # issuer name is treated as not matching anything in `issuer_registry`
    # (SPECIFICATION.md section 3.1) — configurable so it can be tuned without a
    # redeploy once real registry data shows how strict this needs to be.
    issuer_fuzzy_match_threshold: float = Field(
        default=85.0, alias="ISSUER_FUZZY_MATCH_THRESHOLD"
    )

    # --- Metadata forensics (app/services/forensics/metadata_forensics.py) ---
    # A file modified AFTER it was created is itself the anomaly
    # (SPECIFICATION.md section 3.1/3.2), so this is only a timestamp-jitter
    # tolerance, not a "how long a gap is acceptable" allowance: PDF dates
    # have whole-second resolution and a generator that writes CreationDate
    # and ModDate a moment apart is not an edit. A ModDate more than this
    # many seconds past CreationDate is flagged.
    metadata_forensics_mod_date_threshold_seconds: float = Field(
        default=1.0, alias="METADATA_FORENSICS_MOD_DATE_THRESHOLD_SECONDS"
    )
    # Comma-separated, case-insensitive substrings of Producer/Creator/
    # softwareAgent values that read as image/graphic-editing tools —
    # unusual authorship for a business document (invoice, receipt,
    # school record, ...) regardless of which specific tool it is. A
    # configurable list (not one hardcoded "contains Photoshop" check)
    # so it can grow without a code change.
    metadata_forensics_editing_software_names: str = Field(
        default="photoshop,gimp,illustrator,affinity,coreldraw,paint.net,pixlr,canva,inkscape",
        alias="METADATA_FORENSICS_EDITING_SOFTWARE_NAMES",
    )
    # Same, for PDF editors (online and desktop) — tools that change the
    # text and numbers of an existing PDF. Kept apart from the image editors
    # above and weighted lower: they are also used innocently, to compress,
    # merge or sign a genuine document. Print drivers and plain PDF creators
    # of the same vendors are deliberately not listed.
    metadata_forensics_pdf_editor_names: str = Field(
        default=(
            "ilovepdf,smallpdf,sejda,pdfescape,pdf-xchange editor,phantompdf,foxit pdf editor,nitro pro,"
            "nitro pdf pro,pdfelement,soda pdf,sodapdf,pdffiller,dochub,pdfcandy,pdf candy"
        ),
        alias="METADATA_FORENSICS_PDF_EDITOR_NAMES",
    )

    # --- Duplicate/near-duplicate detection (app/services/forensics/
    # duplicate_check.py) ---
    # Max Hamming distance (0-64 — imagehash's default 64-bit pHash)
    # between a new page's hash and a prior page's hash before it counts
    # as a near-duplicate match (SPECIFICATION.md section 3.2). Deliberately
    # conservative by default — this should only catch near-identical
    # resubmissions/copies, not merely similar-looking documents (e.g.
    # two different invoices sharing the same template) — configurable so
    # it can be loosened/tightened without a redeploy once real
    # duplicate-fraud patterns are seen.
    duplicate_hash_hamming_threshold: int = Field(
        default=5, alias="DUPLICATE_HASH_HAMMING_THRESHOLD"
    )

    @property
    def metadata_forensics_editing_software_name_list(self) -> list[str]:
        return [
            name.strip().lower()
            for name in self.metadata_forensics_editing_software_names.split(",")
            if name.strip()
        ]

    @property
    def metadata_forensics_pdf_editor_name_list(self) -> list[str]:
        return [name.strip().lower() for name in self.metadata_forensics_pdf_editor_names.split(",") if name.strip()]

    # --- Cloud AI-content-detection API (e.g. Azure AI Content Safety / Hive) ---
    ai_content_detection_endpoint: str | None = Field(
        default=None, alias="AI_CONTENT_DETECTION_ENDPOINT"
    )
    ai_content_detection_key: str | None = Field(
        default=None, alias="AI_CONTENT_DETECTION_KEY"
    )

    # --- Transactional email (Azure Communication Services or similar) ---
    email_service_connection_string: str | None = Field(
        default=None, alias="EMAIL_SERVICE_CONNECTION_STRING"
    )
    email_from_address: str | None = Field(default=None, alias="EMAIL_FROM_ADDRESS")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
