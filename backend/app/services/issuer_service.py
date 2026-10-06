"""
Issuer validation (SPECIFICATION.md section 3.1, "Issuer validation") —
matches an extracted issuer name against the `issuer_registry` table
(app/models/issuer_registry.py), first via rapidfuzz fuzzy string
matching, then (if that doesn't clear the threshold) via an LLM semantic
judgment call.

The LLM fallback exists because fuzzy string matching alone can't cross
scripts: an Arabic-script extracted issuer name compared against an
English-only registry entry scores low no matter how good a match it
actually is — that's a different-script problem, not a spelling-variance
problem rapidfuzz is built for. `issuer_registry.name_arabic` covers the
case where a registry row's own Arabic name is on file (checked
alongside `name` in the fuzzy pass); the LLM fallback additionally
catches transliteration variance the registry doesn't have an exact
Arabic entry for (e.g. a registry entry that's only ever been recorded
in English).

NOTE: `issuer_registry` is seeded (see the Alembic migrations that
create/extend it) with a small set of FAKE test entries for local
development/pipeline testing only. This registry must be populated with
the client's real vendor/school/tax-ID data (in both scripts, where
relevant) before this check is meaningful in production.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz, process, utils
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.issuer_registry import IssuerRegistry
from app.services.llm_service import LLMConfigurationError, LLMOperationError, LLMService


@dataclass
class IssuerMatchResult:
    matched: bool
    best_match_name: str | None
    best_match_score: float | None
    threshold: float
    # How the match was decided: "fuzzy" (rapidfuzz cleared the
    # threshold), "llm" (fuzzy match failed but the LLM judged it the
    # same entity), or None (no match by either method, or nothing to
    # compare).
    matched_via: Literal["fuzzy", "llm"] | None = None


def _no_match(threshold: float, *, best_match_name: str | None = None, best_match_score: float | None = None) -> IssuerMatchResult:
    return IssuerMatchResult(
        matched=False,
        best_match_name=best_match_name,
        best_match_score=best_match_score,
        threshold=threshold,
        matched_via=None,
    )


def match_issuer(
    db: Session,
    company_id,
    issuer_name: str | None,
    threshold: float | None = None,
    llm_service: LLMService | None = None,
    before_slow_call=None,
) -> IssuerMatchResult:
    """Match `issuer_name` against every ACTIVE registry row of `company_id`
    (the registry is per company — another company's vendors never count as
    known) on both `name` and `name_arabic`. `matched` is True if either a fuzzy match clears
    `threshold` (default: settings.issuer_fuzzy_match_threshold, an
    admin-tunable config value) or, failing that, `llm_service` (when
    given) judges the name a semantic match for one of the registry
    candidates. `llm_service` is optional — omit it (as tests do) to
    keep this fuzzy-only and offline; app/tasks/document_checks.py
    passes a real one in production."""
    effective_threshold = threshold if threshold is not None else settings.issuer_fuzzy_match_threshold

    if not issuer_name or not issuer_name.strip():
        return _no_match(effective_threshold)

    # Deactivated issuers (soft-deleted by an admin) no longer count as
    # "known" — an invoice from one should not pass issuer verification.
    rows = db.execute(
        select(IssuerRegistry).where(
            IssuerRegistry.company_id == company_id, IssuerRegistry.is_active.is_(True)
        )
    ).scalars().all()
    if not rows:
        return _no_match(effective_threshold)

    # One (candidate_string, row) pair per non-null name field — a row
    # with both `name` and `name_arabic` contributes two candidates, so
    # an Arabic-script issuer_name can match via name_arabic even though
    # the row's primary `name` is English.
    candidates: list[tuple[str, IssuerRegistry]] = []
    for row in rows:
        if row.name:
            candidates.append((row.name, row))
        if row.name_arabic:
            candidates.append((row.name_arabic, row))
    candidate_strings = [c for c, _ in candidates]

    # `processor=utils.default_process` lowercases, strips punctuation, and
    # collapses whitespace before scoring — without it, case/punctuation
    # differences alone (e.g. "TECHSOURCE SOLUTIONS INC." vs. "TechSource
    # Solutions Inc.") tank rapidfuzz's score far more than a human would
    # judge them as actually different issuers.
    best = process.extractOne(
        issuer_name, candidate_strings, scorer=fuzz.WRatio, processor=utils.default_process
    )
    best_name = best[0] if best else None
    # rapidfuzz's score is a C-computed float (e.g. 28.000000000000004) —
    # round for a value that's meant to be read by a reviewer, not just
    # compared against the threshold.
    best_score = round(best[1], 2) if best else None

    if best is not None and best[1] >= effective_threshold:
        return IssuerMatchResult(
            matched=True,
            best_match_name=best_name,
            best_match_score=best_score,
            threshold=effective_threshold,
            matched_via="fuzzy",
        )

    if llm_service is not None:
        if before_slow_call is not None:
            # Lets the caller end its transaction so no DB connection is held
            # during the LLM call (it has nothing pending at this point).
            before_slow_call()
        try:
            judgment = llm_service.judge_entity_match(issuer_name, candidate_strings)
        except (LLMConfigurationError, LLMOperationError):
            # No LLM judgment available (not configured, or the call
            # failed) — fall through to reporting the fuzzy-only result
            # rather than failing the whole check.
            judgment = None
        if judgment is not None and judgment.matched_candidate is not None:
            return IssuerMatchResult(
                matched=True,
                best_match_name=judgment.matched_candidate,
                best_match_score=best_score,
                threshold=effective_threshold,
                matched_via="llm",
            )

    return _no_match(effective_threshold, best_match_name=best_name, best_match_score=best_score)
