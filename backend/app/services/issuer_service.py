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

OCR noise is normalized before matching (`issuer_name_variants`): trademark
signs and their misreads glued to the last word ("ACADEMYO" for "ACADEMY®")
are stripped, and a bilingual name ("أكاديمية الخليج ... THE GULF ...
ACADEMY") is also matched part by part — its Arabic and Latin parts
separately — keeping the best score.

`verify_issuer` builds the stored issuer_verification result. When the
registry has NO active entries that could apply to the document (none at all,
or none for its country / kind of issuer), an unmatched issuer is reported as
"not_checked" — there was nothing to check it against — instead of "flag".

NOTE: `issuer_registry` is seeded (see the Alembic migrations that
create/extend it) with a small set of FAKE test entries for local
development/pipeline testing only. This registry must be populated with
the client's real vendor/school/tax-ID data (in both scripts, where
relevant) before this check is meaningful in production.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

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


# Organisation words a trademark sign (® ™) is often printed after; OCR reads
# the sign as a trailing "O"/"0" glued to the word ("ACADEMYO").
_ORG_WORDS = {
    "ACADEMY", "SCHOOL", "SCHOOLS", "NURSERY", "COLLEGE", "UNIVERSITY", "INSTITUTE", "CENTER", "CENTRE",
    "COMPANY", "LLC", "INC", "LTD", "LIMITED", "GROUP", "TRADING", "HOSPITAL", "CLINIC", "EST", "CORP",
    "CORPORATION", "SERVICES", "SOLUTIONS", "INTERNATIONAL", "BANK",
}
_TRADEMARK_RE = re.compile(r"[®™©℗]|\((?:R|TM|C)\)", re.IGNORECASE)
_ARABIC = r"؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿"
_ARABIC_RUN_RE = re.compile(rf"[{_ARABIC}][{_ARABIC}\s\-]*")


def _strip_trademark_misread(word: str) -> str:
    bare = word.rstrip(".,;:")
    if len(bare) > 3 and bare[-1] in "Oo0" and bare[:-1].upper() in _ORG_WORDS:
        return bare[:-1]
    return word


def issuer_name_variants(name: str) -> list[str]:
    """The extracted name, an OCR-cleaned copy, and its Arabic and Latin
    parts on their own (for a bilingual letterhead), without duplicates."""
    cleaned = _TRADEMARK_RE.sub(" ", name)
    cleaned = " ".join(_strip_trademark_misread(w) for w in cleaned.split())
    variants = [name.strip(), cleaned]
    arabic = " ".join(m.group(0).strip() for m in _ARABIC_RUN_RE.finditer(cleaned)).strip()
    latin = " ".join(_ARABIC_RUN_RE.sub(" ", cleaned).split())
    if arabic and latin and re.search(r"[A-Za-z]{3}", latin):
        variants += [arabic, latin]
    seen: set[str] = set()
    out = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


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
    best = None
    for variant in issuer_name_variants(issuer_name):
        found = process.extractOne(variant, candidate_strings, scorer=fuzz.WRatio, processor=utils.default_process)
        if found is not None and (best is None or found[1] > best[1]):
            best = found
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


# ---------------------------------------------------------------------------
# The stored issuer_verification result
# ---------------------------------------------------------------------------

# document_type (app/services/classification_service.py) -> the kind of
# registry entry that can be its issuer; None = any kind.
_ISSUER_TYPE_FOR_DOCUMENT = {
    "school_document": "school",
    "vendor_invoice": "vendor",
    "commercial_invoice": "vendor",
    "procurement_documentation": "vendor",
    "travel_invoice": "vendor",
}
_COUNTRY_FOR_CURRENCY = {
    "AED": "AE", "SAR": "SA", "QAR": "QA", "KWD": "KW", "BHD": "BH", "OMR": "OM", "EGP": "EG",
    "JOD": "JO", "INR": "IN", "PKR": "PK", "GBP": "GB",
}


def document_country(extracted_fields: dict[str, Any]) -> str | None:
    """The document's country, from a printed IBAN or else the currency."""
    for field in extracted_fields.get("additional_fields") or []:
        if isinstance(field, dict) and "iban" in (field.get("field_name") or "").lower():
            value = re.sub(r"\s", "", str(field.get("value") or "")).upper()
            if re.match(r"^[A-Z]{2}\d{2}", value):
                return value[:2]
    currency = (((extracted_fields.get("core_fields") or {}).get("amount") or {}).get("currency") or "").upper()
    return _COUNTRY_FOR_CURRENCY.get(currency)


def relevant_registry_rows(
    db: Session, company_id, *, document_type: str | None, country: str | None
) -> list[IssuerRegistry]:
    """Active registry entries that could be this document's issuer: of a
    matching kind (or "other") and country (or no country on file)."""
    wanted = _ISSUER_TYPE_FOR_DOCUMENT.get(document_type or "")
    rows = db.execute(
        select(IssuerRegistry).where(IssuerRegistry.company_id == company_id, IssuerRegistry.is_active.is_(True))
    ).scalars().all()
    return [
        r for r in rows
        if (wanted is None or r.type.value in (wanted, "other"))
        and (country is None or r.country is None or r.country.upper() == country)
    ]


def verify_issuer(
    db: Session,
    company_id,
    extracted_fields: dict[str, Any],
    *,
    document_type: str | None = None,
    llm_service: LLMService | None = None,
    before_slow_call=None,
) -> dict[str, Any]:
    """{"result": "pass" | "flag" | "not_checked", "details": {...}} for the
    issuer_verification check."""
    issuer_name = ((extracted_fields.get("core_fields") or {}).get("issuer") or {}).get("value")
    match = match_issuer(db, company_id, issuer_name, llm_service=llm_service, before_slow_call=before_slow_call)
    details: dict[str, Any] = {
        "issuer_name": issuer_name,
        "matched_registry_name": match.best_match_name,
        "match_score": match.best_match_score,
        "matched_via": match.matched_via,
        "threshold": match.threshold,
    }
    if match.matched:
        result = "pass"
    else:
        country = document_country(extracted_fields)
        if relevant_registry_rows(db, company_id, document_type=document_type, country=country):
            result = "flag"
        else:
            result = "not_checked"
            kind = _ISSUER_TYPE_FOR_DOCUMENT.get(document_type or "")
            scope = " ".join(p for p in (f"{kind} issuers" if kind else None, f"in {country}" if country else None) if p)
            details["reason"] = (
                "Not checked: the issuer registry has no active entries"
                + (f" for {scope}" if scope else "")
                + ", so there was nothing to compare this issuer against."
            )
    return {"result": result, "details": details}

