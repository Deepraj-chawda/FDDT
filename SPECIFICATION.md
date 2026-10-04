# System Specification — Fraud Document Detection Tool (FDDT)

## 1. Project Overview & Core Objectives

**What we are building:** A full-stack, web-based AI Document Authenticator
Tool — a system that authenticates, verifies, and detects fraud in
third-party documents submitted in support of employee claims, vendor
invoices, procurement, and other business transactions.

**Primary goal:** Replace a manual document-review process with a
technology-enabled pipeline that validates the authenticity and integrity
of submitted documents *before* approvals and payments are processed —
reducing excess payments, financial losses, administrative effort, and
compliance risk caused by incorrect, inaccurate, or falsified supporting
documents.

**Origin:** Requirements were provided via a client requirements email
covering five capability areas: Document Authentication & Verification,
Fraud Detection & Forensic Analysis, Risk Assessment & Decision Support,
Workflow & Case Management, and Audit & Governance.

---

## 2. Architecture & Tech Stack

### Backend
- **Framework:** FastAPI (Python)
- **Database:** PostgreSQL
- **File storage:** Azure Blob Storage (original documents stored
  immutably, hash-verified)
- **Async task queue:** Celery + Redis (for OCR, forensics, and scoring
  jobs — these are slow and must not block the request/response cycle).
  One task per document per step on three shared queues —
  `extraction_queue` (Azure Document Intelligence), `vision_queue` (Azure
  OpenAI), `forensics_queue` (local CPU, unthrottled, Linux prefork) — each
  with its own worker pool (`start-worker.sh <queue>`). Dispatch is
  FAIR-SHARE PER COMPANY (`app/tasks/fairshare.py`: priority from the
  company's outstanding work; no fixed tiers). Azure calls are capped
  globally across workers by a Redis rate limiter
  (`app/services/rate_limiter.py`) whose token reservations are calibrated
  against real usage (`scripts/calibrate_llm_tokens.py`; re-run when the
  model, prompts or page render size change). Tasks must NOT hold a DB
  transaction across slow steps (commit before downloads/Azure calls/CPU
  work) and must write checks idempotently (`app/services/check_store.py`,
  one row per document+check). See `docs/processing-queues.md`.
- **Multi-tenant:** several client companies share one deployment; each
  company's users see ONLY their company's data. Every tenant-owned table
  has `company_id`; isolation is enforced twice — application layer
  (company-bound sessions auto-filter every ORM query, services take
  `company_id` explicitly; `app/db/tenancy.py`) AND PostgreSQL Row-Level
  Security (the app connects as `fddt_app`; only the explicit platform path
  uses `fddt_platform`, which sees all rows through an explicit
  `platform_all` policy — NO role has BYPASSRLS and no superuser is needed).
  A cross-company id is a 404. Full design: `docs/multi-tenancy.md`. Any new
  tenant-owned table needs `TenantScopedMixin` + a `tenant_isolation` and a
  `platform_all` policy + explicit grants in its migration. The app's roles
  connect through PgBouncer (transaction pooling); tenant context must only
  ever be set transaction-locally (`set_config(..., true)`).
- **Auth:** Simple JWT (email/password); the token carries `role`,
  `company_id` and `is_platform_admin`, and both the role check and the
  tenant check are mandatory on every endpoint. Company roles, ranked:
  `user` (0: submits documents/claims, tracks own case status),
  `reviewer_l1` (1: reviews the company's queue, approves/rejects/escalates
  in the L1 tier), `reviewer_l2` (2: everything L1 can, plus cases escalated
  to L2, plus the company's own Settings › Issuer Registry and Risk Rules).
  Permission checks gate on a minimum rank (`require_company_role` /
  `has_rank`, `hasRank` in the frontend), never on lists of role names.
  `platform_admin` belongs to NO company and is not on the rank ladder:
  creates companies and all users (companies cannot manage users), sees the
  per-company billing/usage dashboard and the queue monitor, and has
  read-only support access to any company's data — every such access writes
  a PLATFORM-ONLY `platform_admin_access` audit row (company_id NULL; never
  visible to any company user). Platform admins also edit the platform
  risk-rule template copied into each NEW company. UI labels:
  "User", "Reviewer L1", "Reviewer L2", "Platform Admin". Azure AD/Entra ID
  SSO is a possible later upgrade, not part of the initial build.

### Frontend
- **Framework:** React + TypeScript (Vite)
- **UI components:** shadcn/ui (Radix + Tailwind) — chosen for a polished,
  customer-friendly look with less boilerplate than a full custom build
- **Styling:** Tailwind CSS
- **Data fetching/caching:** TanStack Query
- **Tables:** hand-built Tailwind tables per page (TanStack Table is installed but not used)
- **Forms:** React Hook Form + Zod (schema validation mirrored from
  backend Pydantic models)
- **Charts:** none in the current build (organisation-wide MIS dashboard is out of scope for v1)
- **Motion:** Framer Motion is installed but not used
- **UI language:** English only. Documents processed by the system may be
  English or Arabic — the *application interface* itself is not
  internationalized and does not need an RTL layout.

### Hosting / Deployment
- **Current stage:** Local development only.
- **Future (not yet built):** Likely Azure (App Service or Container
  Apps — undecided) with CI/CD. Do not build cloud deployment
  configuration yet; keep this in mind only so local setup doesn't
  paint us into a corner (e.g. prefer environment-variable-driven config
  over hardcoded local paths).

### Cloud AI / Processing APIs
No custom models are trained or hosted at any stage. All intelligence is
delivered via managed cloud APIs:
- **Azure Document Intelligence** — OCR, layout extraction, tables,
  key-value pairs. Supports Arabic script for printed/typed text (not
  handwritten Arabic — flag this limitation if it becomes relevant).
- **Azure OpenAI (vision-capable deployment)** - signature/stamp region
  detection (presence/placement only) and reviewer-reference signature comparison.
- **Azure OpenAI** (behind the `LLMService` abstraction, strict JSON-schema
  output) - document-type classification and template-free semantic field
  extraction in one call (works on any document type without a per-type
  template), issuer semantic match, secondary visual-inconsistency review.
  Handles Arabic text natively.
- **AI-generated document assessment** - one question inside the visual-review
  vision call, treated as an experimental signal among several, never a
  standalone verdict. No separate content-detection API is integrated.

### Forensics / Utility Libraries (no external API cost)
- **PyMuPDF** - PDF page rendering for forensics and the report PDF
- **pikepdf** - PDF internal structure and metadata analysis (incremental
  save markers, XMP history, object anomalies). Forensics are PDF-only.
- **Pillow / NumPy** - Error Level Analysis (recompression-difference
  tampering detection)
- **OpenCV** — copy-move forgery detection (feature matching)
- **imagehash** — perceptual hashing for duplicate/near-duplicate
  detection
- **rapidfuzz** — fuzzy string matching for issuer-registry lookups
- **transitions** — Python state-machine library for the case workflow
  engine
- **Report PDF** - per-case forensic report generated on demand

### High-Level Data Flow
```
Upload → Blob Storage (original, hashed) + Postgres record
   → Celery job: OCR/Layout extraction (Azure Doc Intelligence)
   → Celery job: Classification + field extraction (Azure OpenAI)
   → Celery job: Forensic checks (metadata, ELA, duplicate hash, PDF structure)
   → Celery job: Visual review incl. AI-generated content assessment
   → Cross-document consistency check (case-level, once all case docs processed)
   → Risk scoring engine (weighted rules → score + tier + reasons)
   → Every scored case goes to the L1 reviewer queue (risk tier drives the
     flag/badge; escalation hands the case to the L2 tier)
   → Reviewer action (approve/reject/escalate) → state machine transition
   → Every step logged to append-only audit_log
```

---

## 3. Core Requirements & Features

### 3.1 Document Authentication & Verification
- **OCR-based data extraction & classification:** Every upload goes
  through a *generic* layout-extraction pass (Azure Document Intelligence
  Layout model) — no fixed template per document type. Extracted text is
  classified against a configurable label list (invoice, school document,
  travel invoice, quotation, etc.) via Azure OpenAI.
- **Issuer validation:** Extracted issuer name fuzzy-matched (rapidfuzz)
  against a maintained `issuer_registry` table (known vendors, schools,
  tax IDs). Letterhead/logo matching is not built; tax IDs are stored but
  matching is by name only.
- **Signature/stamp verification:** Detect presence/location of a
  signature or stamp region (Azure OpenAI vision model). Treat as
  presence/placement confirmation for a human reviewer, *not* a reliable
  automated identity-match — this is a known weak point, do not oversell
  its accuracy in UI copy or docs.
- **Date/amount/consistency validation:** Rule-based validation on
  extracted structured fields (e.g. date not in future, total = sum of
  line items).
- **Cross-document validation & reconciliation:** Documents grouped by
  `case_id`; shared fields (amount, date, vendor) compared pairwise
  across all documents in a case; mismatches flagged.

### 3.2 Fraud Detection & Forensic Analysis
- **Metadata/digital forensics:** pikepdf + PyMuPDF (PDF only). Flag:
  modify-date long after create-date, editing-software fingerprints
  (e.g. Photoshop) in tool history, stripped metadata, multiple PDF
  incremental-save markers.
- **Tampering/alteration detection:** Error Level Analysis (Pillow) +
  copy-move detection (OpenCV). Known limitation: ELA is a signal, not a
  standalone verdict — weak against print-and-rescan tampering and prone
  to false positives on legitimately recompressed scans. Weight
  accordingly in scoring, don't treat as definitive.
- **Duplicate/near-duplicate detection:** Perceptual hash (imagehash) on
  every upload, compared against all historical hashes; small Hamming
  distance = flag.
- **AI-generated document detection:** Vision-model assessment inside the
  visual review,
  treated as experimental/one-signal-among-several — real-world accuracy
  on scanned business documents is unproven and should be validated
  post-launch, not promised upfront.
- **Image/PDF manipulation analysis:** Combines ELA + PDF structural
  checks; Azure OpenAI vision as a secondary "describe visual
  inconsistencies" pass for edge cases.

### 3.3 Risk Assessment & Decision Support
- **Risk scoring engine:** Transparent, weighted **rules engine** — not a
  black-box ML model. Each rule = `{rule_id, category, condition, weight,
  severity, reason_template}`, stored as configuration in a `risk_rules`
  table (not hardcoded), so weights/thresholds can be tuned by an admin
  without a redeploy.
- **Scoring logic:** Sum weights of every rule that fired for a case, cap
  at 100, map to tier via configurable thresholds (e.g. 0–29 low, 30–59
  medium, 60–100 high).
- **Explainability:** Every triggered rule's filled-in `reason_template`
  (e.g. "Issuer name 'X' did not match any known vendor in the registry")
  is stored alongside the score in `risk_scores.triggered_reasons`
  (jsonb) — this is what reviewers see, not just a number.
- **Rule versioning:** When a rule's weight changes, do not retroactively
  rescore historical cases — keep the weight that was active at scoring
  time attached to that case's historical record.
- **Risk-based routing:** Every scored case moves to the reviewer queue
  (`pending_manual_review`); nothing auto-approves. The tier drives the
  case flag/badge and whether an approval needs a written justification.
  Escalation is a manual reviewer action that moves the case to the L2
  tier (`assigned_tier`, not a status).

### 3.4 Workflow & Case Management (self-contained — no external platform)
- Case management is built **natively** into this platform's own DB +
  React UI. There is no integration with an external ticketing platform
  (ServiceNow, Jira, etc.) in the initial scope.
- **State machine** (`transitions` library): statuses in use are
  submitted → pending_manual_review → approved / rejected. The enum also
  defines under_automated_review, auto_approved, escalated,
  under_investigation and closed for future use; nothing enters them today.
- **Escalation:** sets `cases.assigned_tier` from `l1` to `l2` (reason
  required; within the case's company). One-directional — there is no de-escalation; an L2 reviewer
  resolves it with the ordinary approve/reject. On an L2 case a
  `reviewer_l1` keeps read access (every GET) but every action endpoint
  returns 403; it leaves their actionable queue and stays reachable from a
  "view only" tab. `case_actions.actor_role` and the audit event data
  record the actor's role at the time, so history and the report read
  "Reviewer L1" / "Reviewer L2". SLA deadlines and scheduled overdue checks
  are not built.
- **Case detail view:** aggregates all documents, all check results, risk
  score + reasons, and all reviewer actions in one place for
  investigation support.
- **Notifications:** in-app and email notifications are not built in the
  current version; the event-based workflow engine is ready for them as
  future subscribers.

### 3.5 Audit & Governance
- **Append-only `audit_log` table:** every automated check result and
  every human action is inserted, never updated or deleted.
- **Review history/timeline:** per-case chronological view built from
  the same audit log.
- **Evidence repository:** original files remain untouched in Blob
  Storage, hash-verified; a per-case forensic report PDF (with file hashes
  and annotated renders) is exportable. A bundle of the original files is
  not built.
- **MIS/compliance reporting:** organisation-wide reporting (fraud rate,
  average resolution time, volumes by type/status, PDF/Excel export) is not
  built in the current version; the per-case forensic report is.

### 3.6 Document Type Coverage (template-free approach)
Must handle: school/educational documents, vendor invoices, commercial
invoices, vendor/procurement documentation, quotations, travel/
accommodation/reimbursement documents, and other externally issued
documents — **without building a separate pipeline per type.**
- Layer 1: Azure Document Intelligence Layout model — generic
  text/table/key-value extraction, no type assumption.
- Layer 2: Azure OpenAI — semantic field extraction informed by the
  Layer-1 output and the Section 3.1 classification label, returns
  issuer/date/amount/reference number + any other identifiable fields as
  JSON, regardless of layout/issuer/country variation.
- Include a `confidence`/`fields_uncertain` flag per extracted field;
  low-confidence key fields auto-route the document to manual review
  instead of silently proceeding.
- New/unlisted document types are handled automatically by this same
  pipeline — no additional engineering required per new type.

### 3.7 Arabic Language Support
- Documents may be in English or formal/printed Arabic script (MSA —
  consistent across UAE, Saudi, Egypt, etc.; dialect variation is not a
  script-level concern for OCR).
- Azure Document Intelligence OCR supports printed Arabic natively.
  **Handwritten Arabic is not reliably supported** — flag if relevant.
- The Azure OpenAI model handles Arabic text (including mixed Arabic/English
  documents) natively for classification and extraction.
- **UI stays English-only.** The only Arabic-specific frontend work is
  correct RTL rendering of individual extracted field *values* when they
  happen to be Arabic text (e.g. `dir="rtl"` on that value's container),
  not a layout-wide i18n system. Numbers inside Arabic strings must
  render left-to-right per standard Unicode bidi behavior — verify this
  against real sample data, don't assume.

---

## 4. Constraints & Development Rules

### Explicitly DO
- Use cloud APIs (Azure AI services, Azure OpenAI) for every
  intelligence/extraction step.
- Keep the risk-scoring engine as transparent, configurable rules logic.
- Design the document-extraction pipeline to be template-free / type-
  agnostic from the start (Layer 1 + Layer 2 approach above) — do not
  hardcode per-document-type extractors.
- Make the audit log append-only at the application layer.
- Use environment variables for all API keys, connection strings, and
  environment-specific config (no hardcoded local paths/secrets),
  since local → Azure migration is expected later.
- Store original uploaded files immutably; never overwrite or mutate
  them during processing.
- Design the workflow/state-machine engine to **emit internal events**
  on every case state change (e.g. `case_created`, `case_status_changed`,
  `case_escalated`, `case_approved`, `case_rejected`) rather than coupling
  transition logic directly to side effects. Current subscribers to these
  events are just the audit log and in-app notifications, but this
  event-based design means a future external case-management integration
  (see Section 4 note below) can be added as a new event subscriber
  without touching the state machine itself.

### Explicitly DO NOT
- **Do not train, fine-tune, or self-host any custom AI/ML model at any
  stage** (this includes signature verification, tamper detection, and
  AI-generated-content detection — use cloud APIs or classical
  CV/forensics libraries instead, even where accuracy is imperfect).
- **Do not build a separate extraction template/pipeline per document
  type.** One generic pipeline must cover all listed and future/unlisted
  types.
- **Do not integrate with an external case-management platform**
  (ServiceNow, Jira, etc.) in the initial build — case management is
  native to this app's own DB/UI. *(Note: if this is requested later, it
  should be addable as a new subscriber to the event system described
  above, plus an optional nullable `external_ref_id` column on `cases` —
  not a redesign of the workflow engine. Build with that event-based
  extensibility in mind now, without building the integration itself.)*
- **Do not build a black-box ML risk-scoring model** for v1 — must stay
  explainable, weighted rules logic.
- **Do not build application-level i18n/RTL layout for the UI** — the
  interface is English-only; only extracted Arabic field *values* need
  RTL-aware rendering.
- **Do not overstate signature/stamp verification or AI-generated-
  document-detection accuracy** anywhere in UI copy, docs, or code
  comments — these are known-weak, best-effort/assistive checks, not
  reliable automated verdicts.
- **Do not retroactively rescore historical cases** when risk-rule
  weights are tuned — preserve the weight that was active at the time
  each case was scored.
- Do not build Azure deployment/CI-CD configuration yet — local dev only
  for now (see Section 2, Hosting).

---

## 5. Proposed File Structure

```
document-authenticator/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── api/
│   │   │   ├── auth.py
│   │   │   ├── cases.py
│   │   │   ├── documents.py
│   │   │   └── reports.py
│   │   ├── models/              # SQLAlchemy models
│   │   │   ├── user.py
│   │   │   ├── case.py
│   │   │   ├── document.py
│   │   │   ├── document_check.py
│   │   │   ├── cross_document_finding.py
│   │   │   ├── risk_score.py
│   │   │   ├── risk_rule.py
│   │   │   ├── case_action.py
│   │   │   └── audit_log.py
│   │   ├── schemas/              # Pydantic schemas
│   │   ├── services/
│   │   │   ├── ocr_service.py            # Azure Document Intelligence
│   │   │   ├── classification_service.py # Azure OpenAI (combined with extraction)
│   │   │   ├── extraction_service.py     # Azure OpenAI, template-free
│   │   │   ├── issuer_service.py         # rapidfuzz matching
│   │   │   ├── forensics/
│   │   │   │   ├── metadata_forensics.py # pikepdf, PyMuPDF
│   │   │   │   ├── ela.py                # Pillow
│   │   │   │   ├── copy_move.py          # OpenCV
│   │   │   │   └── duplicate_check.py    # imagehash
│   │   │   ├── ai_content_detection.py
│   │   │   ├── cross_document_service.py
│   │   │   ├── risk_scoring_service.py
│   │   │   └── workflow_service.py       # transitions state machine
│   │   ├── tasks/                 # Celery task definitions
│   │   ├── core/
│   │   │   ├── config.py          # env-var driven settings
│   │   │   └── security.py        # JWT auth
│   │   └── db/
│   │       ├── session.py
│   │       └── migrations/        # Alembic
│   ├── tests/
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── ui/                # shadcn components
│   │   │   ├── case/
│   │   │   ├── upload/
│   │   │   ├── admin/
│   │   │   └── reports/
│   │   ├── pages/
│   │   ├── api/
│   │   ├── hooks/
│   │   ├── types/
│   │   └── App.tsx
│   ├── package.json
│   └── vite.config.ts
├── sample-documents/              # test PDFs (already generated)
├── docker-compose.yml             # local: postgres + redis + backend + frontend
├── SPECIFICATION.md
└── README.md
```

---

## 6. Current Status

The build is considered final for this delivery. Implemented: multi-tenancy
(companies, `company_id` everywhere, application-layer + PostgreSQL RLS
isolation without BYPASSRLS, platform admin with platform-only audited
support access, platform risk-rule templates seeding per-company issuer
registry/risk rules, company-prefixed blob paths, per-company billing/usage
counters with nightly reconciliation, PgBouncer transaction pooling), three
fair-share, calibrated-rate-limited processing queues with a queue monitor, JWT auth with ranked `user` / `reviewer_l1` /
`reviewer_l2` company roles plus `platform_admin`, case and document upload with immutable
hash-verified storage (uploads are PDF only, within the company's own size limit — per-company `max_file_size_mb` / `max_zip_size_mb` set by a platform admin, 10 MB / 300 MB by default, read through `app/services/upload_limits.py` by every upload path — and validated synchronously before storage —
corrupted and password-protected PDFs are rejected; `app/services/upload_validation.py`), bulk upload
(one zip of at most 300 MB, one folder per case, ingested in the background case by case with partial
success and a live per-case summary; each document validated and queued exactly like a single upload;
`app/services/bulk_upload_service.py`, `docs/pipeline/01a-bulk-upload.md`), the Celery pipeline (OCR, classification and
extraction, field validation, issuer verification, cross-document checks,
PDF forensics, visual review, signature/stamp detection), the transparent
versioned rules-engine risk score, the two-tier reviewer workflow (L1, with
escalation routing to L2) with an append-only audit log, settings (per-company issuer registry and risk rules; platform-level users), the
per-case forensic report PDF, and the React frontend.

Out of scope for this version: notifications and email, SLA tracking,
organisation-wide MIS reporting, letterhead/logo matching, cross-case
signature library matching, and cloud deployment (the production plan, tiers and costs are documented in
`docs/deployment/production-deployment.md`; no infrastructure-as-code is built). Full documentation is in
`docs/README.md`; the PDFs in `docs-pdf/` are built from it (`docs/tools`).
