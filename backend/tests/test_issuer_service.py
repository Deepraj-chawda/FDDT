"""
Unit tests for app/services/issuer_service.py's matching against
`issuer_registry` — fuzzy string matching (including the bilingual
name/name_arabic fields), plus the LLM semantic-judgment fallback used
when fuzzy matching can't help (different scripts, not just spelling
variance). Uses the shared `db_session` fixture (in-memory SQLite, see
tests/conftest.py).
"""
from unittest.mock import MagicMock

from app.models.issuer_registry import IssuerRegistry, IssuerType
from app.services.issuer_service import match_issuer
from app.services.llm_service import EntityMatchJudgment, LLMConfigurationError


def test_no_match_when_registry_is_empty(db_session):
    result = match_issuer(db_session, db_session.info["test_company_id"], "Acme LLC")
    assert result.matched is False
    assert result.best_match_name is None
    assert result.matched_via is None


def test_exact_name_matches(db_session):
    db_session.add(IssuerRegistry(name="Acme LLC", tax_id="123", type=IssuerType.vendor))
    db_session.commit()

    result = match_issuer(db_session, db_session.info["test_company_id"], "Acme LLC")
    assert result.matched is True
    assert result.best_match_name == "Acme LLC"
    assert result.best_match_score == 100.0
    assert result.matched_via == "fuzzy"


def test_close_variant_matches_above_threshold(db_session):
    db_session.add(IssuerRegistry(name="Al Nukhba Technical Systems Est.", type=IssuerType.vendor))
    db_session.commit()

    # Minor OCR/formatting noise shouldn't defeat the match.
    result = match_issuer(db_session, db_session.info["test_company_id"], "Al Nukhba Technical Systems Est")
    assert result.matched is True
    assert result.best_match_name == "Al Nukhba Technical Systems Est."


def test_unrelated_name_does_not_match(db_session):
    db_session.add(IssuerRegistry(name="Acme LLC", type=IssuerType.vendor))
    db_session.commit()

    result = match_issuer(db_session, db_session.info["test_company_id"], "Totally Different Trading Co")
    assert result.matched is False
    # Still reports the best (non-matching) candidate for reviewer context.
    assert result.best_match_name == "Acme LLC"
    assert result.matched_via is None


def test_custom_threshold_overrides_default(db_session):
    db_session.add(IssuerRegistry(name="Acme LLC", type=IssuerType.vendor))
    db_session.commit()

    # A score that passes the default threshold can still fail a much
    # stricter one supplied explicitly.
    result = match_issuer(db_session, db_session.info["test_company_id"], "Acme LLC", threshold=100.1)
    assert result.matched is False


def test_missing_issuer_name_does_not_match(db_session):
    db_session.add(IssuerRegistry(name="Acme LLC", type=IssuerType.vendor))
    db_session.commit()

    result = match_issuer(db_session, db_session.info["test_company_id"], None)
    assert result.matched is False
    assert result.best_match_name is None

    result = match_issuer(db_session, db_session.info["test_company_id"], "   ")
    assert result.matched is False


def test_arabic_extracted_name_matches_via_name_arabic_field(db_session):
    db_session.add(
        IssuerRegistry(
            name="Al Nukhba Technical Systems Est.",
            name_arabic="مؤسسة النخبة للأنظمة التقنية",
            type=IssuerType.vendor,
        )
    )
    db_session.commit()

    result = match_issuer(db_session, db_session.info["test_company_id"], "مؤسسة النخبة للأنظمة التقنية")
    assert result.matched is True
    assert result.best_match_name == "مؤسسة النخبة للأنظمة التقنية"
    assert result.matched_via == "fuzzy"


def test_arabic_extracted_name_against_english_only_registry_does_not_fuzzy_match(db_session):
    # No name_arabic on file for this row — cross-script fuzzy matching
    # should not clear the threshold (this is the gap the LLM fallback
    # exists for, exercised separately below).
    db_session.add(IssuerRegistry(name="TechSource Solutions Inc.", type=IssuerType.vendor))
    db_session.commit()

    result = match_issuer(db_session, db_session.info["test_company_id"], "شركة تك سورس للحلول")
    assert result.matched is False
    assert result.matched_via is None


def test_llm_fallback_matches_cross_script_name_fuzzy_matching_missed(db_session):
    db_session.add(IssuerRegistry(name="Al Wadi Al Akhdar General Trading LLC", type=IssuerType.vendor))
    db_session.commit()

    fake_llm = MagicMock()
    fake_llm.judge_entity_match.return_value = EntityMatchJudgment(
        matched_candidate="Al Wadi Al Akhdar General Trading LLC",
        reasoning="Transliteration of the same company name.",
    )

    result = match_issuer(
        db_session,
        db_session.info["test_company_id"], "شركة الوادي الأخضر للتجارة العامة ذ.م.م", llm_service=fake_llm
    )
    assert result.matched is True
    assert result.matched_via == "llm"
    assert result.best_match_name == "Al Wadi Al Akhdar General Trading LLC"
    fake_llm.judge_entity_match.assert_called_once()


def test_llm_fallback_not_used_when_no_llm_service_given(db_session):
    db_session.add(IssuerRegistry(name="Acme LLC", type=IssuerType.vendor))
    db_session.commit()

    # No llm_service passed — stays fuzzy-only, deterministic/offline
    # (this is what every other test in this file relies on).
    result = match_issuer(db_session, db_session.info["test_company_id"], "Completely Unrelated Name")
    assert result.matched is False
    assert result.matched_via is None


def test_llm_fallback_reports_no_match_when_llm_finds_none(db_session):
    db_session.add(IssuerRegistry(name="Acme LLC", type=IssuerType.vendor))
    db_session.commit()

    fake_llm = MagicMock()
    fake_llm.judge_entity_match.return_value = EntityMatchJudgment(
        matched_candidate=None, reasoning="Not the same entity."
    )

    result = match_issuer(db_session, db_session.info["test_company_id"], "Totally Unrelated Co", llm_service=fake_llm)
    assert result.matched is False
    assert result.matched_via is None


def test_llm_fallback_configuration_error_is_swallowed(db_session):
    db_session.add(IssuerRegistry(name="Acme LLC", type=IssuerType.vendor))
    db_session.commit()

    fake_llm = MagicMock()
    fake_llm.judge_entity_match.side_effect = LLMConfigurationError("not configured")

    # Doesn't raise — falls back to the fuzzy-only (non-matching) result.
    result = match_issuer(db_session, db_session.info["test_company_id"], "Totally Unrelated Co", llm_service=fake_llm)
    assert result.matched is False
    assert result.matched_via is None


def test_llm_fallback_not_attempted_when_fuzzy_already_matched(db_session):
    db_session.add(IssuerRegistry(name="Acme LLC", type=IssuerType.vendor))
    db_session.commit()

    fake_llm = MagicMock()
    result = match_issuer(db_session, db_session.info["test_company_id"], "Acme LLC", llm_service=fake_llm)
    assert result.matched is True
    assert result.matched_via == "fuzzy"
    fake_llm.judge_entity_match.assert_not_called()
