"""Platform-admin case delete (soft) and restore: the case vanishes for its
company everywhere, the platform admin can still see and restore it, and only
the platform audit log records it."""
from sqlalchemy import select

from app.db import tenancy
from app.models.audit_log import AuditLog
from app.models.case import Case
from tests.helpers_risk import make_case


def _company_id(db_session):
    return db_session.info["test_company_id"]


def test_only_platform_admins_can_delete_or_restore(client, db_session, seeded_user, auth_headers, reviewer_headers, plain_headers):
    case = make_case(db_session, seeded_user)
    for headers in (auth_headers, reviewer_headers, plain_headers):
        assert client.delete(f"/cases/{case.id}", headers=headers).status_code == 403
        assert client.post(f"/cases/{case.id}/restore", headers=headers).status_code == 403
    assert client.get(f"/cases/{case.id}", headers=auth_headers).status_code == 200


def test_deleted_case_vanishes_for_the_company(client, db_session, seeded_user, auth_headers, platform_admin_headers):
    case = make_case(db_session, seeded_user)
    kept = make_case(db_session, seeded_user)
    company = _company_id(db_session)

    res = client.request(
        "DELETE", f"/cases/{case.id}", headers=platform_admin_headers, json={"reason": "uploaded by mistake"}
    )
    assert res.status_code == 204, res.text
    assert client.delete(f"/cases/{case.id}", headers=platform_admin_headers).status_code == 409

    # Company: gone from the list, the case page and its direct timeline.
    listed = [c["id"] for c in client.get("/cases", headers=auth_headers).json()]
    assert str(case.id) not in listed and str(kept.id) in listed
    assert client.get(f"/cases/{case.id}", headers=auth_headers).status_code == 404
    assert client.get(f"/cases/{case.id}/audit-log", headers=auth_headers).status_code == 404

    # Company audit history: shows the deleted case's history and the case_deleted event!
    log = client.get("/audit-log", headers=auth_headers).json()["items"]
    case_events = [item for item in log if item["case_id"] == str(case.id)]
    assert len(case_events) > 0
    del_event = next(item for item in case_events if item["event_type"] == "case_deleted")
    assert del_event["actor_name"] == "Platform Administrator"
    assert del_event["actor_email"] is None
    assert del_event["case_deleted"] is True
    assert del_event["event_data"]["reason"] == "uploaded by mistake"
    assert "admin_email" not in del_event["event_data"]

    # Platform admin: still visible, marked deleted.
    admin_list = client.get(f"/cases?company_id={company}", headers=platform_admin_headers).json()
    (row,) = [c for c in admin_list if c["id"] == str(case.id)]
    assert row["deleted_at"] is not None
    assert client.get(f"/cases/{case.id}", headers=platform_admin_headers).json()["deleted_at"] is not None

    # Audit row in company log.
    tenancy.bind_platform(db_session)
    (event,) = db_session.execute(select(AuditLog).where(AuditLog.event_type == "case_deleted")).scalars().all()
    assert event.company_id == company and event.case_id == case.id
    assert event.event_data["reason"] == "uploaded by mistake" and event.event_data["company_id"] == str(company)
    # Data is kept.
    assert db_session.get(Case, case.id) is not None
    tenancy.bind_company(db_session, company)


def test_restore_brings_the_case_back(client, db_session, seeded_user, auth_headers, platform_admin_headers):
    case = make_case(db_session, seeded_user)
    assert client.post(f"/cases/{case.id}/restore", headers=platform_admin_headers).status_code == 409
    assert client.delete(f"/cases/{case.id}", headers=platform_admin_headers).status_code == 204
    assert client.post(f"/cases/{case.id}/restore", headers=platform_admin_headers).status_code == 204

    assert client.get(f"/cases/{case.id}", headers=auth_headers).status_code == 200
    assert str(case.id) in [c["id"] for c in client.get("/cases", headers=auth_headers).json()]
    log = client.get("/audit-log", headers=auth_headers).json()["items"]
    assert any(
        item["event_type"] == "case_restored" and item["case_id"] == str(case.id) for item in log
    )


def test_admin_timeline_shows_every_platform_event_company_timeline_none(
    client, db_session, seeded_user, auth_headers, platform_admin_headers
):
    case = make_case(db_session, seeded_user)
    client.request("DELETE", f"/cases/{case.id}", headers=platform_admin_headers, json={"reason": "duplicate upload"})
    client.post(f"/cases/{case.id}/restore", headers=platform_admin_headers)

    admin = client.get(f"/cases/{case.id}/audit-log", headers=platform_admin_headers).json()
    by_type = {e["event_type"]: e for e in admin}
    assert {"case_deleted", "case_restored", "platform_admin_access"} <= set(by_type)
    deleted = by_type["case_deleted"]
    assert deleted["event_data"]["reason"] == "duplicate upload"
    assert deleted["event_data"]["admin_email"] and deleted["actor_name"]

    company = client.get(f"/cases/{case.id}/audit-log", headers=auth_headers).json()
    by_type_co = {e["event_type"]: e for e in company}
    assert "case_deleted" in by_type_co
    assert "case_restored" in by_type_co
    assert "platform_admin_access" not in by_type_co
    assert by_type_co["case_deleted"]["actor_name"] == "Platform Administrator"
    assert "admin_email" not in (by_type_co["case_deleted"]["event_data"] or {})


def test_paginated_list_shows_deleted_cases_only_to_platform_admins_and_only_on_request(
    client, db_session, seeded_user, auth_headers, platform_admin_headers
):
    case = make_case(db_session, seeded_user)
    kept = make_case(db_session, seeded_user)
    company = _company_id(db_session)
    assert client.delete(f"/cases/{case.id}", headers=platform_admin_headers).status_code == 204

    def ids(headers, **params):
        page = client.get("/cases/page", params={"company_id": company, **params}, headers=headers).json()
        return {c["id"] for c in page["items"]}

    assert ids(auth_headers) == {str(kept.id)}
    assert ids(auth_headers, status_group="deleted") == set()
    assert ids(platform_admin_headers, status_group="deleted") == {str(case.id)}
    assert ids(platform_admin_headers, status_group="analyzing") == {str(kept.id)}  # groups are live cases
    summary = client.get("/cases/summary", params={"company_id": company}, headers=platform_admin_headers).json()
    assert summary["total"] == 1
