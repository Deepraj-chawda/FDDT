"""Regenerate docs/database/schema.md and docs/database/erd.md from the LIVE
PostgreSQL database (information_schema / pg_catalog), not from the models.

    cd backend
    .venv/Scripts/python ../docs/tools/gen_db_docs.py            # Windows
    .venv/bin/python ../docs/tools/gen_db_docs.py                # Linux/macOS

Connects with DOCS_DATABASE_URL, else DATABASE_URL from the environment, else
the local docker default (owner role). Re-run after every migration.
"""
from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "database"
URL = (
    os.environ.get("DOCS_DATABASE_URL")
    or os.environ.get("DATABASE_URL")
    or "postgresql+psycopg2://docauth:docauth@localhost:5432/docauth"
)

# Hand-written purpose of each table (kept here so regeneration never loses it).
PURPOSE = {
    "companies": "One row per client company (tenant). `is_active = false` suspends it: its users can't sign in and existing tokens stop working.",
    "company_usage_stats": "Per-company, per-UTC-day usage counters (cases, documents, files, storage bytes) for the platform billing dashboard; bumped in the same transaction as each upload/case/report and reconciled nightly.",
    "users": "Accounts (email + bcrypt-hashed password). Company users have a `company_id` and a ranked role (`user` / `reviewer_l1` / `reviewer_l2`); platform admins have `role = platform_admin` and no company (CHECK constraint).",
    "cases": "The unit of work: one submission of one or more documents. Carries workflow `status`, the owning reviewer tier `assigned_tier` and the latest `risk_tier`.",
    "documents": "One row per uploaded PDF. The original bytes live in Blob Storage (immutable); this row holds the SHA-256, the extraction results (`extracted_fields`, `ocr_text`) and `processing_status`.",
    "document_checks": "One row per (document, check type) — the latest run of each automated check (extraction stages, every forensic check, signature detection…), upserted so a redelivered task never duplicates it. `result` is the check's raw JSON output; the run history is in `audit_log`.",
    "document_page_hashes": "Perceptual hash (pHash) of every rendered PDF page, one per (document, page), used to find duplicate/near-duplicate pages within the same company.",
    "cross_document_findings": "Case-level reconciliation results: a shared field (amount, date, issuer) that disagrees between documents in the same case.",
    "issuer_registry": "A company's known vendors/schools/etc. that extracted issuer names are fuzzy-matched against. Rows are deactivated, never deleted. A new company starts empty.",
    "risk_rules": "A company's configurable scoring rules. Rows are immutable and versioned: an edit inserts `version + 1`; the current rule is the highest version. Seeded from `risk_rule_templates` when the company is created.",
    "risk_rule_templates": "PLATFORM-LEVEL (no `company_id`): the default rule set copied into every NEW company's `risk_rules`. Edited by platform admins; edits never change existing companies. Only `fddt_platform` can access it.",
    "risk_settings": "One row per company holding its `medium` / `high` score thresholds and the metadata score cap (`metadata_score_cap`, default 40: the most points all `metadata.*` rules add together).",
    "case_risk_assessments": "One row per scoring run of a case (insert-only history; unique per (case, evidence fingerprint)): score, tier, frozen reason text and a snapshot of the exact rule versions and thresholds used.",
    "risk_scores": "LEGACY. Created by the initial migration before the versioned engine existed; nothing in the application reads or writes it. `case_risk_assessments` replaced it.",
    "case_actions": "Reviewer-facing history of human actions on a case (approve / reject / escalate …) with the actor's role at the time. Complements, and is also mirrored into, `audit_log`.",
    "case_reports": "History of generated per-case forensic PDF reports (insert-only). The PDF lives in Blob Storage under `companies/{company}/cases/{case}/reports/`; this row stores its SHA-256, size and the assessment it reflects.",
    "audit_log": "Append-only system of record: every automated check result, human action and settings change is inserted here and never updated or deleted. `company_id` NULL = a platform-level event (company created, template edits, usage reconciliation, platform-admin access), visible only to platform admins; a company user's *user created/updated* goes into that company's log.",
    "signature_references": "A signature/stamp region drawn on a document, cropped and stored as a PNG, used as the reference for comparisons.",
    "signature_matches": "Result of comparing one reference against the signature region of another document in the same case (vision-model verdict or pixel-identical reuse verdict); one row per (reference, document, scope).",
}
ORDER = [
    "companies", "company_usage_stats", "users", "cases", "documents", "document_checks",
    "document_page_hashes", "cross_document_findings", "issuer_registry", "risk_rules",
    "risk_rule_templates", "risk_settings", "case_risk_assessments", "risk_scores", "case_actions",
    "case_reports", "audit_log", "signature_references", "signature_matches",
]
INSERT_ONLY = ["audit_log", "case_risk_assessments", "case_reports", "risk_rules"]


def q(conn, sql, **kw):
    return conn.execute(text(sql), kw).mappings().all()


def main() -> None:
    engine = create_engine(URL)
    with engine.connect() as conn:
        head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        tables = [r["table_name"] for r in q(conn, """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE' AND table_name <> 'alembic_version'""")]
        unknown = sorted(set(tables) - set(ORDER))
        order = [t for t in ORDER if t in tables] + unknown
        missing = [t for t in ORDER if t not in tables]

        cols = defaultdict(list)
        for r in q(conn, """
            SELECT c.table_name, c.column_name, c.is_nullable, c.column_default, c.data_type,
                   c.character_maximum_length, c.udt_name, c.ordinal_position
            FROM information_schema.columns c WHERE c.table_schema = 'public'
            ORDER BY c.table_name, c.ordinal_position"""):
            cols[r["table_name"]].append(r)

        pks = defaultdict(set)
        for r in q(conn, """
            SELECT tc.table_name, kcu.column_name FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'"""):
            pks[r["table_name"]].add(r["column_name"])

        fks = defaultdict(dict)
        fk_list = []
        for r in q(conn, """
            SELECT cl.relname AS table_name, att.attname AS column_name, rcl.relname AS ref_table,
                   ratt.attname AS ref_column, con.confdeltype AS on_delete
            FROM pg_constraint con
            JOIN pg_class cl ON cl.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = cl.relnamespace AND ns.nspname = 'public'
            JOIN pg_class rcl ON rcl.oid = con.confrelid
            JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, n) ON true
            JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS rk(attnum, n) ON rk.n = k.n
            JOIN pg_attribute att ON att.attrelid = cl.oid AND att.attnum = k.attnum
            JOIN pg_attribute ratt ON ratt.attrelid = rcl.oid AND ratt.attnum = rk.attnum
            WHERE con.contype = 'f' ORDER BY cl.relname, att.attname"""):
            fks[r["table_name"]][r["column_name"]] = (r["ref_table"], r["ref_column"], r["on_delete"])
            fk_list.append(r)

        indexes = defaultdict(list)
        for r in q(conn, """
            SELECT t.relname AS table_name, i.relname AS index_name, ix.indisunique AS is_unique,
                   ix.indisprimary AS is_primary,
                   array_to_string(ARRAY(
                       SELECT pg_get_indexdef(ix.indexrelid, k + 1, true)
                       FROM generate_subscripts(ix.indkey, 1) AS k ORDER BY k), ', ') AS columns,
                   pg_get_expr(ix.indpred, ix.indrelid) AS predicate
            FROM pg_index ix
            JOIN pg_class t ON t.oid = ix.indrelid
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = 'public'
            ORDER BY t.relname, i.relname"""):
            if not r["is_primary"]:
                indexes[r["table_name"]].append(r)

        checks = defaultdict(list)
        for r in q(conn, """
            SELECT cl.relname AS table_name, con.conname, pg_get_constraintdef(con.oid) AS def
            FROM pg_constraint con JOIN pg_class cl ON cl.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = cl.relnamespace AND ns.nspname = 'public'
            WHERE con.contype = 'c' ORDER BY cl.relname, con.conname"""):
            checks[r["table_name"]].append(r)

        enums = defaultdict(list)
        for r in q(conn, """
            SELECT t.typname, e.enumlabel FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
            JOIN pg_namespace n ON n.oid = t.typnamespace AND n.nspname = 'public'
            ORDER BY t.typname, e.enumsortorder"""):
            enums[r["typname"]].append(r["enumlabel"])

        rls = {r["relname"]: (r["relrowsecurity"], r["relforcerowsecurity"]) for r in q(conn, """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public' WHERE c.relkind = 'r'""")}
        policies = defaultdict(list)
        for r in q(conn, """
            SELECT tablename, policyname, roles::text AS roles, cmd FROM pg_policies
            WHERE schemaname = 'public' ORDER BY tablename, policyname"""):
            policies[r["tablename"]].append(r)
        grants = defaultdict(lambda: defaultdict(set))
        for r in q(conn, """
            SELECT table_name, grantee, privilege_type FROM information_schema.role_table_grants
            WHERE table_schema = 'public' AND grantee IN ('fddt_app', 'fddt_platform')"""):
            grants[r["table_name"]][r["grantee"]].add(r["privilege_type"])

    def coltype(c):
        if c["data_type"] == "USER-DEFINED":
            return f"enum `{c['udt_name']}`"
        if c["data_type"] == "character varying":
            return f"varchar({c['character_maximum_length']})" if c["character_maximum_length"] else "varchar"
        return {"timestamp with time zone": "timestamptz", "timestamp without time zone": "timestamp",
                "double precision": "double precision"}.get(c["data_type"], c["data_type"])

    def privs(table, role):
        p = grants[table].get(role, set())
        if not p:
            return "—"
        short = [x for x in ("SELECT", "INSERT", "UPDATE", "DELETE") if x in p]
        return ", ".join(short)

    def tenancy_cell(t):
        has_company = any(c["column_name"] == "company_id" for c in cols[t])
        rls_on = rls.get(t, (False, False))[0]
        if t == "risk_rule_templates":
            return "platform-only"
        if t == "companies":
            return "RLS (own row)" if rls_on else "—"
        if has_company:
            return "tenant · RLS" if rls_on else "tenant · **no RLS**"
        return "RLS" if rls_on else "—"

    L = []
    L.append("# Database schema\n")
    L.append("<!-- Generated from the LIVE local Postgres (not from the models) by `docs/tools/gen_db_docs.py`, which")
    L.append(f"     inspects `information_schema`/`pg_catalog`. Alembic head at generation time: {head}. Do not edit by hand. -->\n")
    L.append(f"PostgreSQL, {len(order)} application tables plus Alembic's `alembic_version` (current head `{head}`). "
             "All primary keys are UUIDs generated by the application. The entity-relationship diagram is in "
             "[erd.md](erd.md); tenant isolation is described in [multi-tenancy.md](../multi-tenancy.md).\n")
    L.append("**Conventions**\n")
    L.append("- **Multi-tenant.** Every tenant-owned table has `company_id` (FK → `companies.id`). Row-Level Security "
             "is enabled on them with a `tenant_isolation` policy for `fddt_app` (`company_id = "
             "fddt_current_company_id()`, the transaction-local `app.current_company_id`) and a `platform_all` "
             "policy for `fddt_platform`. No role has BYPASSRLS.")
    L.append(f"- **Insert-only tables** (the app roles are granted only SELECT + INSERT): "
             + ", ".join(f"`{t}`" for t in INSERT_ONLY) + " (rule edits insert a new version).")
    L.append("- **JSON columns** (`jsonb`) hold check output and snapshots; their shapes are documented in the "
             "[pipeline docs](../pipeline/).")
    L.append("- **Bounding boxes** anywhere in JSON use one convention: `{page (1-based), x, y, width, height}` as "
             "0-1 fractions of the page, origin top-left.")
    L.append("- **Enums** are native Postgres enums; their values are listed under each table and again in the "
             "index at the bottom.\n")
    L.append("| Table | Tenancy | `fddt_app` | Purpose |")
    L.append("|---|---|---|---|")
    for t in order:
        L.append(f"| [`{t}`](#{t}) | {tenancy_cell(t)} | {privs(t, 'fddt_app')} | {PURPOSE.get(t, '')} |")
    L.append("")

    used_enums: set[str] = set()
    for t in order:
        L.append(f"## {t}\n")
        if PURPOSE.get(t):
            L.append(PURPOSE[t] + "\n")
        L.append("| Column | Type | Null | Default | Notes |")
        L.append("|---|---|---|---|---|")
        uniq_single = {ix["columns"] for ix in indexes[t] if ix["is_unique"] and "," not in ix["columns"]
                       and not ix["predicate"]}
        for c in cols[t]:
            notes = []
            if c["column_name"] in pks[t]:
                notes.append("PK")
            if c["column_name"] in fks[t]:
                rt, rc, od = fks[t][c["column_name"]]
                notes.append(f"FK → `{rt}.{rc}`" + (" (on delete cascade)" if od == "c" else
                                                     " (on delete set null)" if od == "n" else ""))
            if c["column_name"] in uniq_single and c["column_name"] not in pks[t]:
                notes.append("unique")
            default = c["column_default"] or ""
            if len(default) > 40:
                default = default[:37] + "…"
            if c["data_type"] == "USER-DEFINED":
                used_enums.add(c["udt_name"])
            L.append(f"| `{c['column_name']}` | {coltype(c)} | {'yes' if c['is_nullable'] == 'YES' else 'no'} | "
                     f"{('`' + default + '`') if default else ''} | {', '.join(notes)} |")
        L.append("")
        for ix in indexes[t]:
            extra = " — unique" if ix["is_unique"] else ""
            if ix["predicate"]:
                extra += f" where `{ix['predicate']}`"
            L.append(f"- **Index** `{ix['index_name']}` on ({ix['columns']}){extra}")
        for ck in checks[t]:
            L.append(f"- **Check** `{ck['conname']}`: `{ck['def']}`")
        for c in cols[t]:
            if c["data_type"] == "USER-DEFINED":
                L.append(f"- **Enum** `{c['udt_name']}`: " + ", ".join(f"`{v}`" for v in enums[c["udt_name"]]))
        rls_on, forced = rls.get(t, (False, False))
        if rls_on:
            pol = "; ".join(f"`{p['policyname']}` ({p['cmd']} to {p['roles'].strip('{}')})" for p in policies[t])
            L.append(f"- **Row-Level Security** on{' (forced)' if forced else ''}: {pol}")
        L.append(f"- **Grants** `fddt_app`: {privs(t, 'fddt_app')} · `fddt_platform`: {privs(t, 'fddt_platform')}")
        L.append("")

    L.append("## Enum index\n")
    L.append("| Enum | Values |")
    L.append("|---|---|")
    for name in sorted(enums):
        L.append(f"| `{name}` | " + ", ".join(f"`{v}`" for v in enums[name]) + " |")
    L.append("")
    L.append("## Schema notes\n")
    L.append("- **`user_role` still contains `admin`.** The single-tenant `admin` role was migrated to "
             "`platform_admin`; Postgres cannot drop an enum value, so `admin` remains in the type but no row "
             "uses it and the application never assigns it.")
    L.append("- **`cases.case_number` and `users.email`** carry a `UNIQUE` constraint *plus* a separate "
             "non-unique index from the original migrations; uniqueness is enforced either way.")
    L.append("- **`risk_scores`** exists but nothing in the application uses it. It is safe to leave; dropping "
             "it needs a migration.")
    L.append("- **No `notifications` table.** Notifications are not part of the current build.")
    L.append("- **Enum values added by later migrations** use `ALTER TYPE … ADD VALUE`; a downgrade leaves "
             "them in place.")
    L.append("- **New tenant-owned tables** need `TenantScopedMixin`, RLS with a `tenant_isolation` and a "
             "`platform_all` policy, and explicit grants in their migration — there are no default "
             "privileges, so a table without them is unreachable by the app.")
    if missing:
        L.append(f"- Tables documented in the generator but missing from this database: {', '.join(missing)}.")
    (OUT / "schema.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    E = ["# Entity-relationship diagram\n",
         f"<!-- Generated from the live database's foreign keys by `docs/tools/gen_db_docs.py` (Alembic head {head}).",
         "     Only key columns are drawn to stay readable; the full column list is in schema.md. -->\n",
         "Every tenant-owned table also references `companies` through `company_id`. Those edges are left out of",
         "the first diagram so it stays readable; the second diagram shows them.\n",
         "## Domain relationships\n",
         "```mermaid", "erDiagram"]
    nullable = {(t, c["column_name"]): c["is_nullable"] == "YES" for t in cols for c in cols[t]}
    for r in fk_list:
        if r["ref_table"] == "companies":
            continue
        left = "|o" if nullable.get((r["table_name"], r["column_name"])) else "||"
        E.append(f'    {r["ref_table"]} {left}--o{{ {r["table_name"]} : "{r["column_name"]}"')
    for t in order:
        if t in ("companies", "company_usage_stats"):
            continue
        E.append(f"    {t} {{")
        for c in cols[t]:
            if c["column_name"] in pks[t]:
                E.append(f"        uuid {c['column_name']} PK")
            elif c["column_name"] in fks[t] and fks[t][c["column_name"]][0] != "companies":
                E.append(f"        uuid {c['column_name']} FK")
        E.append("    }")
    E.append("```\n")
    E.append("## Tenancy — what belongs to a company\n")
    E.append("Every table below references `companies.id` through the listed column (a table, not a diagram:")
    E.append("drawn, the fan-out from `companies` is too wide to read).\n")
    E.append("| Table | Column | Nullable | Row-Level Security |")
    E.append("|---|---|---|---|")
    for r in sorted((r for r in fk_list if r["ref_table"] == "companies"), key=lambda r: order.index(r["table_name"])
                    if r["table_name"] in order else 99):
        null = "yes" if nullable.get((r["table_name"], r["column_name"])) else "no"
        rls_on = "on" if rls.get(r["table_name"], (False, False))[0] else "**off**"
        E.append(f"| `{r['table_name']}` | `{r['column_name']}` | {null} | {rls_on} |")
    E.append("")
    E.append("`users.company_id` and `audit_log.company_id` are nullable: NULL marks a platform admin and a "
             "platform-level event respectively. `risk_rule_templates` has no `company_id` — it is "
             "platform-level and only reachable by the platform database role.")
    (OUT / "erd.md").write_text("\n".join(E) + "\n", encoding="utf-8")
    print(f"schema.md + erd.md written ({len(order)} tables, head {head})")


if __name__ == "__main__":
    main()
