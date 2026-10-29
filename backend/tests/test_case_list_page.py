"""The paginated case list (GET /cases/page) and its counts (GET /cases/summary):
the same visibility as GET /cases, filtered, searched and paged on the server."""
import uuid
from datetime import datetime, timedelta, timezone

from app.models.case import Case, CaseStatus, CaseTier, RiskTier
from app.models.case_risk_assessment import CaseRiskAssessment
from tests.sample_files import PDF


def _create_case(client, headers, case_type="vendor_invoice"):
    return client.post("/cases", json={"case_type": case_type}, headers=headers).json()


def _set(db_session, case_id, **fields):
    case = db_session.get(Case, uuid.UUID(case_id))
    for name, value in fields.items():
        setattr(case, name, value)
    db_session.commit()


def _score(db_session, case_id, score, tier):
    db_session.add(
        CaseRiskAssessment(
            case_id=uuid.UUID(case_id), raw_score=score, score=score, tier=tier, triggered_reasons=[],
            risk_rules_version_snapshot={}, evidence_fingerprint=f"fp-{score}",
            computed_at=datetime.now(timezone.utc),
        )
    )
    _set(db_session, case_id, risk_tier=tier)


def _page(client, headers, **params):
    response = client.get("/cases/page", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _ids(page):
    return [c["id"] for c in page["items"]]


def test_pages_cover_every_case_once_newest_first(client, auth_headers, db_session):
    created = [_create_case(client, auth_headers)["id"] for _ in range(5)]
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i, case_id in enumerate(created):
        _set(db_session, case_id, created_at=base + timedelta(minutes=i))

    first = _page(client, auth_headers, limit=2)
    second = _page(client, auth_headers, limit=2, offset=2)
    third = _page(client, auth_headers, limit=2, offset=4)
    assert first["total"] == second["total"] == 5
    assert _ids(first) + _ids(second) + _ids(third) == list(reversed(created))
    assert first["items"][0]["document_count"] == 0  # same item shape as GET /cases


def test_limit_is_bounded(client, auth_headers):
    assert client.get("/cases/page?limit=0", headers=auth_headers).status_code == 422
    assert client.get("/cases/page?limit=201", headers=auth_headers).status_code == 422


def test_status_groups(client, auth_headers, db_session):
    analyzing = _create_case(client, auth_headers)["id"]
    awaiting = _create_case(client, auth_headers)["id"]
    approved = _create_case(client, auth_headers)["id"]
    rejected = _create_case(client, auth_headers)["id"]
    escalated = _create_case(client, auth_headers)["id"]
    _set(db_session, awaiting, status=CaseStatus.pending_manual_review)
    _set(db_session, approved, status=CaseStatus.auto_approved)
    _set(db_session, rejected, status=CaseStatus.rejected)
    _set(db_session, escalated, status=CaseStatus.escalated, assigned_tier=CaseTier.l2)

    def group(name):
        return set(_ids(_page(client, auth_headers, status_group=name)))

    assert group("analyzing") == {analyzing}
    assert group("awaiting_review") == {awaiting, escalated}
    assert group("approved") == {approved}
    assert group("rejected") == {rejected}
    assert group("escalated") == {escalated}
    assert group("open") == {analyzing, awaiting, escalated}


def test_risk_filter_search_and_case_type(client, auth_headers, db_session, seeded_user):
    high = _create_case(client, auth_headers, "quotation")["id"]
    unscored = _create_case(client, auth_headers)["id"]
    _score(db_session, high, 80, RiskTier.high)

    assert _ids(_page(client, auth_headers, risk="high")) == [high]
    assert _ids(_page(client, auth_headers, risk="pending")) == [unscored]
    assert _ids(_page(client, auth_headers, case_type="quotation")) == [high]

    number = client.get(f"/cases/{high}", headers=auth_headers).json()["case_number"]
    assert _ids(_page(client, auth_headers, q=number.lower())) == [high]
    assert _page(client, auth_headers, q=seeded_user.email.upper())["total"] == 2  # submitter match
    assert _page(client, auth_headers, q="%")["total"] == 0  # LIKE wildcards are literal


def test_a_submitter_sees_only_their_cases_and_never_filters_on_risk(
    client, auth_headers, plain_headers, db_session
):
    mine = _create_case(client, plain_headers)["id"]
    _create_case(client, auth_headers)
    _score(db_session, mine, 90, RiskTier.high)

    assert _ids(_page(client, plain_headers)) == [mine]
    assert _ids(_page(client, plain_headers, risk="high")) == []  # would reveal the tier
    assert _ids(_page(client, plain_headers, risk="pending")) == [mine]
    assert _page(client, plain_headers)["items"][0]["flag"]["flag"] == "pending"

    summary = client.get("/cases/summary", headers=plain_headers).json()
    assert summary["total"] == 1
    assert summary["risk"] == {"high": 0, "medium": 0, "low": 0, "pending": 1}
    assert summary["high_risk_open"] == 0 and summary["action_required"] == 0


def test_mine_limits_a_reviewer_to_their_own_submissions(client, auth_headers, plain_headers):
    own = _create_case(client, auth_headers)["id"]
    _create_case(client, plain_headers)
    assert _ids(_page(client, auth_headers, mine="true")) == [own]
    assert client.get("/cases/summary?mine=true", headers=auth_headers).json()["total"] == 1


def test_priority_sort_puts_open_l2_then_highest_score_first(client, auth_headers, db_session):
    low = _create_case(client, auth_headers)["id"]
    high = _create_case(client, auth_headers)["id"]
    l2 = _create_case(client, auth_headers)["id"]
    _score(db_session, low, 10, RiskTier.low)
    _score(db_session, high, 75, RiskTier.high)
    _set(db_session, l2, assigned_tier=CaseTier.l2)
    assert _ids(_page(client, auth_headers, sort="priority")) == [l2, high, low]


def test_summary_counts(client, auth_headers, reviewer_headers, db_session):
    a = _create_case(client, auth_headers)["id"]
    b = _create_case(client, auth_headers)["id"]
    c = _create_case(client, auth_headers)["id"]
    client.post(f"/cases/{a}/documents", headers=auth_headers, files={"file": ("a.pdf", PDF, "application/pdf")})
    _score(db_session, a, 80, RiskTier.high)
    _set(db_session, a, status=CaseStatus.pending_manual_review)
    _set(db_session, b, status=CaseStatus.approved)
    _set(db_session, c, assigned_tier=CaseTier.l2)

    summary = client.get("/cases/summary", headers=auth_headers).json()
    assert summary["total"] == 3 and summary["documents"] == 1
    assert (summary["analyzing"], summary["awaiting_review"], summary["approved"], summary["rejected"]) == (1, 1, 1, 0)
    assert summary["open"] == 2 and summary["escalated"] == 1 and summary["high_risk_open"] == 1
    assert summary["risk"] == {"high": 1, "medium": 0, "low": 0, "pending": 2}
    assert summary["l2"] == 1 and summary["actionable"] == 3  # a Reviewer L2 may act on all
    assert client.get("/cases/summary", headers=reviewer_headers).json()["actionable"] == 2  # L1: not the L2 case
