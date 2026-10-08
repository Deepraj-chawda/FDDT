# FDDT documentation

**FDDT — Fraud Document Detection Tool** authenticates third-party documents (vendor invoices, school
documents, quotations, travel claims and the payment evidence behind them) *before* an approval or payment
is made. A user submits a **case** of PDF documents; the system extracts their data, runs a battery of
automated checks, turns the results into an **explainable** risk score, and hands the case to a human
reviewer (Reviewer L1, with escalation routing a case to Reviewer L2). Every step is written to an append-only audit log.
Several client companies share one deployment, each isolated from the others by the application and by
PostgreSQL Row-Level Security.

These docs describe the system **as implemented**, checked against the code. The capabilities included in the
current build, and those outside its scope, are collected in the table in
[architecture/overview.md](architecture/overview.md#scope-of-the-current-build).

## Start here

| If you want to… | Read |
|---|---|
| Learn to use the app as a Company User (Submitter, Reviewer L1, Reviewer L2) | [COMPANY_USER_MANUAL.md](COMPANY_USER_MANUAL.md) |
| Manage the multi-tenant system as a Platform Administrator | [PLATFORM_ADMIN_MANUAL.md](PLATFORM_ADMIN_MANUAL.md) |
| Understand the whole system in ten minutes | [architecture/overview.md](architecture/overview.md) |
| Understand companies, roles and tenant isolation (RLS) | [multi-tenancy.md](multi-tenancy.md) |
| Tune processing queues, worker pools and Azure rate limits | [processing-queues.md](processing-queues.md) |
| Follow one case from upload to report | [architecture/data-flow.md](architecture/data-flow.md) |
| Run it on your machine | [deployment/local-setup.md](deployment/local-setup.md) |
| Know what to configure and where to get keys | [deployment/environment-variables.md](deployment/environment-variables.md) |
| Understand what each check detects and its limits | the [pipeline docs](#pipeline-what-each-stage-does) below |
| Call or change the API | [api/README.md](api/README.md) → Swagger at `http://localhost:8000/docs` |
| Change the database | [database/schema.md](database/schema.md), [database/erd.md](database/erd.md) |
| Work on the UI | [frontend/screens.md](frontend/screens.md), [frontend/components.md](frontend/components.md) |
| **Deploy to production on Azure, step by step** (network/IP/port plan, every command, scaling, alerts, release) | [deployment/azure-production-runbook.md](deployment/azure-production-runbook.md) |
| Understand the production design on Azure (architecture, service tiers, costs) | [deployment/production-deployment.md](deployment/production-deployment.md) |
| See the remaining pre-production gaps | [deployment/azure-migration-notes.md](deployment/azure-migration-notes.md) |
| See how the Azure demo is deployed | [deployment/azure-demo-deployment.md](deployment/azure-demo-deployment.md) |
| Read what changed in the multi-tenancy / queues work, and why | [MULTI_TENANCY_CHANGE_REPORT.md](MULTI_TENANCY_CHANGE_REPORT.md) |

## Contents

### User Manuals
- [COMPANY_USER_MANUAL.md](COMPANY_USER_MANUAL.md) — Comprehensive guide for client organizations covering all 3 company roles (User/Submitter, Reviewer L1, Reviewer L2), case submission, bulk intake, review workflows, automated forensic checks, overlays, and company settings. Includes high-resolution interface screenshots.
- [PLATFORM_ADMIN_MANUAL.md](PLATFORM_ADMIN_MANUAL.md) — Complete operational manual for Platform Administrators covering tenant provisioning, company limits, user administration across all 4 roles, password resets, rule templates, queue monitoring, usage analytics, and immutable audit history. Includes high-resolution interface screenshots.
- [user-manual.md](user-manual.md) — Consolidated role-by-role overview guide.

### Architecture
- [overview.md](architecture/overview.md) — system diagram, tech stack, the key design decisions and their
  rationale, the scope-of-the-current-build table, security posture.
- [data-flow.md](architecture/data-flow.md) — sequence diagram and step-by-step walkthrough, Celery task names and queues.
- [multi-tenancy.md](multi-tenancy.md) — companies, roles, two-layer tenant isolation (application + RLS),
  platform-admin support access, blob layout, billing & usage.
- [processing-queues.md](processing-queues.md) — the three queues, fair-share scheduling, idempotent writes,
  calibrated Azure rate limits, database connections and PgBouncer, load tests.

### Pipeline — what each stage does
Each page covers what it detects, the algorithm and libraries, the real input/output shape, known
limitations and the risk rules it feeds.

1. [Upload & intake](pipeline/01-upload-intake.md) · [1a. Bulk upload (a zip of cases)](pipeline/01a-bulk-upload.md)
2. [Extraction & normalization](pipeline/02-extraction-normalization.md)
3. [Field validation](pipeline/03-field-validation.md)
4. [Cross-document consistency](pipeline/04-cross-document-consistency.md)
5. [Issuer verification](pipeline/05-issuer-verification.md)
6. [Metadata forensics](pipeline/06-metadata-forensics.md) · [6a. Font consistency](pipeline/06a-font-consistency.md)
7. [ELA & copy-move](pipeline/07-ela-copy-move.md) · [7a. Deleted / replaced content (ghost text)](pipeline/07a-ghost-content.md)
8. [Visual review & AI-generation](pipeline/08-visual-review-ai-generation.md)
9. [Signature verification](pipeline/09-signature-verification.md)
10. [Duplicate detection](pipeline/10-duplicate-detection.md)
11. [Risk scoring engine](pipeline/11-risk-scoring-engine.md)
12. [Short check summaries (what the case page and report show first)](pipeline/12-check-summaries.md)

### Reference
- [API](api/README.md) + [`openapi.json`](api/openapi.json) (generated from the code)
- [Database schema](database/schema.md) and [ERD](database/erd.md) (generated from the live database by
  `tools/gen_db_docs.py`)
- [Frontend screens](frontend/screens.md) and [components](frontend/components.md)
- [Per-case forensic report](reports/forensic-report.md)

### Deployment
- [Local setup](deployment/local-setup.md) · [Environment variables](deployment/environment-variables.md)
- [Azure production runbook](deployment/azure-production-runbook.md) — the step-by-step production deployment:
  address plan, ports and NSGs, static IPs, DNS, Key Vault, YAML for every container app, autoscaling,
  alerts, release/rollback, troubleshooting and go-live checklist
- [Production deployment](deployment/production-deployment.md) — Azure Container Apps architecture, S/M/L
  service tiers with monthly costs (UAE North list prices), deployment runbook, operations and go-live
  checklist
- [Azure migration notes](deployment/azure-migration-notes.md) — what is Azure-ready and the remaining gaps
- [Azure demo deployment](deployment/azure-demo-deployment.md) — the low-cost demo topology and operating notes

### Change records
- [MULTI_TENANCY_CHANGE_REPORT.md](MULTI_TENANCY_CHANGE_REPORT.md) — every change, decision and problem in
  the multi-tenancy, processing-queue and follow-up work

## Ground rules the design follows

These principles explain many choices you will meet in the code:

- **No model is trained or hosted.** Intelligence is a managed cloud API or a classical library.
- **A transparent rules engine, not a model, produces the risk score**, with versioned, admin-tunable rules.
- **One generic, template-free extraction pipeline** for every document type.
- **Original files are immutable** and hash-verified; everything else derives from them.
- **The audit log is append-only.**
- **Weak signals are labelled weak** — signature comparison, AI-generation detection and ELA are advisory,
  never verdicts, and never described as "verified".
- **The UI is English-only**; only extracted Arabic values need right-to-left handling.

## Keeping these docs true

- The **OpenAPI snapshot** and the **database docs** are generated — regenerate them rather than editing:
  `api/openapi.json` (command in [api/README.md](api/README.md#regenerating-the-snapshot)); `database/*.md`
  with `cd backend && .venv/Scripts/python ../docs/tools/gen_db_docs.py` against the migrated local Postgres
  — re-run after every migration.
- The **pipeline docs** quote real constants, thresholds and result shapes. When you change an algorithm or a
  default rule, update its page and the rule tables (the default rules live in
  `backend/app/services/risk_rule_seed.py` and the platform's `risk_rule_templates`).
- When you add or change a capability listed in the overview's scope table, update that table.
- The **PDFs in `docs-pdf/`** are built from these Markdown files (and `api/openapi.json`) — never edit them
  by hand. After changing any doc, rebuild: `cd docs/tools && npm install && npm run build`
  (see [tools/README.md](tools/README.md)).
