# Entity-relationship diagram

<!-- Generated from the live database's foreign keys by `docs/tools/gen_db_docs.py` (Alembic head f3b7d1e8a4c5).
     Only key columns are drawn to stay readable; the full column list is in schema.md. -->

Every tenant-owned table also references `companies` through `company_id`. Those edges are left out of
the first diagram so it stays readable; the second diagram shows them.

## Domain relationships

```mermaid
erDiagram
    users |o--o{ audit_log : "actor_user_id"
    cases |o--o{ audit_log : "case_id"
    documents |o--o{ audit_log : "document_id"
    bulk_uploads ||--o{ bulk_upload_cases : "bulk_upload_id"
    cases |o--o{ bulk_upload_cases : "case_id"
    users ||--o{ bulk_uploads : "uploaded_by_user_id"
    users |o--o{ case_actions : "actor_user_id"
    cases ||--o{ case_actions : "case_id"
    cases ||--o{ case_reports : "case_id"
    users ||--o{ case_reports : "generated_by_user_id"
    case_risk_assessments |o--o{ case_reports : "risk_assessment_id"
    cases ||--o{ case_risk_assessments : "case_id"
    bulk_uploads |o--o{ cases : "bulk_upload_id"
    users ||--o{ cases : "submitted_by_user_id"
    cases ||--o{ cross_document_findings : "case_id"
    documents ||--o{ document_checks : "document_id"
    documents ||--o{ document_page_hashes : "document_id"
    cases ||--o{ documents : "case_id"
    users ||--o{ documents : "uploaded_by_user_id"
    users |o--o{ risk_rule_templates : "updated_by"
    users |o--o{ risk_rules : "updated_by"
    cases ||--o{ risk_scores : "case_id"
    users |o--o{ risk_settings : "updated_by"
    cases ||--o{ signature_matches : "case_id"
    documents ||--o{ signature_matches : "document_id"
    signature_references ||--o{ signature_matches : "signature_reference_id"
    users ||--o{ signature_references : "created_by"
    cases ||--o{ signature_references : "source_case_id"
    documents ||--o{ signature_references : "source_document_id"
    users {
        uuid id PK
    }
    cases {
        uuid id PK
        uuid submitted_by_user_id FK
        uuid bulk_upload_id FK
    }
    documents {
        uuid id PK
        uuid case_id FK
        uuid uploaded_by_user_id FK
    }
    document_checks {
        uuid id PK
        uuid document_id FK
    }
    document_page_hashes {
        uuid id PK
        uuid document_id FK
    }
    cross_document_findings {
        uuid id PK
        uuid case_id FK
    }
    issuer_registry {
        uuid id PK
    }
    risk_rules {
        uuid id PK
        uuid updated_by FK
    }
    risk_rule_templates {
        uuid id PK
        uuid updated_by FK
    }
    risk_settings {
        uuid id PK
        uuid updated_by FK
    }
    case_risk_assessments {
        uuid id PK
        uuid case_id FK
    }
    risk_scores {
        uuid id PK
        uuid case_id FK
    }
    case_actions {
        uuid id PK
        uuid case_id FK
        uuid actor_user_id FK
    }
    case_reports {
        uuid id PK
        uuid case_id FK
        uuid generated_by_user_id FK
        uuid risk_assessment_id FK
    }
    audit_log {
        uuid id PK
        uuid case_id FK
        uuid document_id FK
        uuid actor_user_id FK
    }
    signature_references {
        uuid source_document_id FK
        uuid source_case_id FK
        uuid created_by FK
        uuid id PK
    }
    signature_matches {
        uuid id PK
        uuid document_id FK
        uuid case_id FK
        uuid signature_reference_id FK
    }
    bulk_upload_cases {
        uuid id PK
        uuid bulk_upload_id FK
        uuid case_id FK
    }
    bulk_uploads {
        uuid id PK
        uuid uploaded_by_user_id FK
    }
```

## Tenancy — what belongs to a company

Every table below references `companies.id` through the listed column (a table, not a diagram:
drawn, the fan-out from `companies` is too wide to read).

| Table | Column | Nullable | Row-Level Security |
|---|---|---|---|
| `company_usage_stats` | `company_id` | no | on |
| `users` | `company_id` | yes | on |
| `cases` | `company_id` | no | on |
| `documents` | `company_id` | no | on |
| `document_checks` | `company_id` | no | on |
| `document_page_hashes` | `company_id` | no | on |
| `cross_document_findings` | `company_id` | no | on |
| `issuer_registry` | `company_id` | no | on |
| `risk_rules` | `company_id` | no | on |
| `risk_settings` | `company_id` | no | on |
| `case_risk_assessments` | `company_id` | no | on |
| `risk_scores` | `company_id` | no | on |
| `case_actions` | `company_id` | no | on |
| `case_reports` | `company_id` | no | on |
| `audit_log` | `company_id` | yes | on |
| `signature_references` | `company_id` | no | on |
| `signature_matches` | `company_id` | no | on |
| `bulk_upload_cases` | `company_id` | no | on |
| `bulk_uploads` | `company_id` | no | on |

`users.company_id` and `audit_log.company_id` are nullable: NULL marks a platform admin and a platform-level event respectively. `risk_rule_templates` has no `company_id` — it is platform-level and only reachable by the platform database role.
