"""
One-off / repeatable backfill: score every case whose automated pipeline
has finished but that has no risk assessment yet (e.g. cases created before
the risk-scoring engine existed).

Usage (from backend/, venv active):
    python backfill_risk_scores.py

Safe to re-run: a case whose evidence is unchanged since its latest
assessment is skipped, and a case whose pipeline hasn't finished is left
alone. Never re-scores a scored case under changed weights.
"""
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.case import Case
from app.services.risk_scoring_service import latest_assessment, score_case


def main() -> None:
    with SessionLocal() as db:
        scored = skipped = 0
        for case_id in db.execute(select(Case.id).order_by(Case.created_at)).scalars().all():
            if latest_assessment(db, case_id) is not None:
                skipped += 1
                continue
            if score_case(db, case_id) is not None:
                scored += 1
            else:
                skipped += 1
            db.commit()
        print(f"Scored {scored} case(s); left {skipped} untouched (already scored or pipeline incomplete).")


if __name__ == "__main__":
    main()
