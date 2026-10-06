"""
Loads reference issuers and their published fees
(app/db/seed_data/reference_issuers.json) into ONE company's issuer registry
and fee benchmarks. The registry is each company's own data, so this is run
per company, never by a migration.

Usage (from backend/, migrations applied):
    python -m scripts.seed_reference_issuers --company testcompany
    python -m scripts.seed_reference_issuers --company-id <uuid>

Idempotent: an issuer already in the registry (same name) is updated with
any missing Arabic name / tax ID / country and reactivated; a benchmark row
with the same (issuer, year, grade, item) is updated in place.
"""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from sqlalchemy import select

from app.db.session import system_session
from app.models.company import Company
from app.models.issuer_fee_benchmark import IssuerFeeBenchmark
from app.models.issuer_registry import IssuerRegistry, IssuerType

DATA = Path(__file__).resolve().parents[1] / "app" / "db" / "seed_data" / "reference_issuers.json"


def seed(db, company_id: uuid.UUID, data: dict) -> tuple[int, int]:
    issuers = benchmarks = 0
    for spec in data["issuers"]:
        row = db.execute(
            select(IssuerRegistry).where(IssuerRegistry.company_id == company_id, IssuerRegistry.name == spec["name"])
        ).scalars().first()
        if row is None:
            row = IssuerRegistry(company_id=company_id, name=spec["name"], type=IssuerType(spec["type"]))
            db.add(row)
            issuers += 1
        row.name_arabic = row.name_arabic or spec.get("name_arabic")
        row.tax_id = row.tax_id or spec.get("tax_id")
        row.country = row.country or spec.get("country")
        row.is_active = True
        db.flush()
        for b in spec.get("benchmarks", []):
            existing = db.execute(
                select(IssuerFeeBenchmark).where(
                    IssuerFeeBenchmark.company_id == company_id,
                    IssuerFeeBenchmark.issuer_id == row.id,
                    IssuerFeeBenchmark.academic_year == b["academic_year"],
                    IssuerFeeBenchmark.grade == b["grade"],
                    IssuerFeeBenchmark.item == b["item"],
                )
            ).scalars().first()
            if existing is None:
                existing = IssuerFeeBenchmark(company_id=company_id, issuer_id=row.id, **{
                    k: b[k] for k in ("academic_year", "grade", "item")
                })
                db.add(existing)
                benchmarks += 1
            for key in ("amount", "currency", "source_url", "source_note", "verified"):
                setattr(existing, key, b[key])
    db.flush()
    return issuers, benchmarks


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--company")
    group.add_argument("--company-id", type=uuid.UUID)
    args = parser.parse_args()
    data = json.loads(DATA.read_text(encoding="utf-8"))
    with system_session() as db:
        query = select(Company).where(
            Company.id == args.company_id if args.company_id else Company.name == args.company
        )
        company = db.execute(query).scalar_one()
        name = company.name
        issuers, benchmarks = seed(db, company.id, data)
        db.commit()
    print(f"{name}: {issuers} issuer(s) and {benchmarks} benchmark row(s) added; existing rows updated.")


if __name__ == "__main__":
    main()
