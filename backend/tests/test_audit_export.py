"""GET /audit-log/export: every matching event, no row limit, one streamed CSV,
same visibility rules as the paged list."""
import csv
import io
import json

from sqlalchemy import select

from app.db import tenancy
from app.models.audit_log import AuditLog
from app.services.audit_service import record_event, record_platform_event
from app.models.user import User


def _rows(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    text = response.content.decode("utf-8")
    assert text.startswith("﻿")  # Excel reads UTF-8 (Arabic) correctly
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


def _add_company_events(db_session, n):
    for i in range(n):
        record_event(db_session, "document_uploaded", event_data={"original_filename": f"file-{i}.pdf"})
    db_session.commit()


def test_exports_every_event_beyond_the_page_limit(client, db_session, auth_headers):
    _add_company_events(db_session, 1205)  # more than the list's 500-per-request maximum
    rows = _rows(client.get("/audit-log/export", headers=auth_headers))
    header, body = rows[0], rows[1:]
    assert header[:2] == ["Timestamp (UTC)", "Event type"]
    assert len(body) == 1205
    assert len({tuple(r) for r in body}) == 1205  # no duplicates
    # Filters apply exactly as on the list.
    filtered = _rows(client.get("/audit-log/export?event_type=no_such_event", headers=auth_headers))
    assert filtered[1:] == []


def test_formula_values_are_neutralised(client, db_session, auth_headers):
    record_event(db_session, "case_rejected", event_data={"reason": '=HYPERLINK("http://evil")'})
    db_session.commit()
    (row,) = _rows(client.get("/audit-log/export?event_type=case_rejected", headers=auth_headers))[1:]
    assert row[6] == '\'=HYPERLINK("http://evil")'
    assert json.loads(row[7])["reason"] == '=HYPERLINK("http://evil")'  # the JSON column keeps it verbatim


def test_company_export_never_contains_platform_admin_rows(
    client, db_session, auth_headers, platform_admin_headers, platform_admin_user, company
):
    _add_company_events(db_session, 3)
    tenancy.bind_platform(db_session)
    admin = db_session.get(User, platform_admin_user.id)
    record_platform_event(db_session, "case_deleted", admin=admin, company=company, event_data={"reason": "x"})
    db_session.commit()
    tenancy.bind_company(db_session, company.id)

    company_rows = _rows(client.get("/audit-log/export", headers=auth_headers))[1:]
    assert all(r[1] != "case_deleted" and "admin@" not in r[3] for r in company_rows)

    admin_rows = _rows(client.get(f"/audit-log/export?company_id={company.id}", headers=platform_admin_headers))[1:]
    assert any(r[1] == "case_deleted" and r[6] == "x" for r in admin_rows)

    # The admin's export of the company's log is recorded once, platform-only.
    tenancy.bind_platform(db_session)
    exports = [
        e for e in db_session.execute(select(AuditLog).where(AuditLog.event_type == "platform_admin_access")).scalars()
        if (e.event_data or {}).get("resource") == "audit log export"
    ]
    assert len(exports) == 1 and exports[0].company_id is None
    tenancy.bind_company(db_session, company.id)


def test_export_requires_a_reviewer(client, plain_headers):
    assert client.get("/audit-log/export", headers=plain_headers).status_code == 403
    assert client.get("/audit-log/export").status_code == 401
