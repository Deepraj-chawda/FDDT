"""
Tenant context through PgBouncer in TRANSACTION pooling mode.

In transaction mode PgBouncer hands a server connection to a different client
as soon as a transaction ends. The app sets the tenant with
`set_config('app.current_company_id', ..., is_local => true)` at the start of
every transaction, so the setting must die with the transaction and never be
seen by whichever client gets that server connection next. If that assumption
were wrong, tenant isolation would break silently — so it is tested directly:

* `docauth_reuse_probe` is a PgBouncer database alias with pool_size=1: every
  client is forced onto the SAME server connection, so reuse is guaranteed,
  not left to chance.
* Negative control: a SESSION-level setting (is_local => false) does leak to
  the next client on that connection — proving the test can detect a leak.
* Positive: the app's transaction-local setting never does, sequentially or
  under concurrent load from many clients alternating companies.

Needs PgBouncer (docker compose up -d pgbouncer) and a migrated database:

    FDDT_PGBOUNCER_HOST=localhost FDDT_PGBOUNCER_PORT=6432 \\
    FDDT_RLS_TEST_DATABASE_URL=postgresql+psycopg2://docauth:docauth@localhost:5432/docauth \\
        pytest tests/test_pgbouncer_rls.py
"""
import os
import threading
import uuid

import psycopg2
import pytest
from sqlalchemy import create_engine, text

from app.core.config import settings

HOST = os.environ.get("FDDT_PGBOUNCER_HOST")
PORT = int(os.environ.get("FDDT_PGBOUNCER_PORT", "6432"))
OWNER_URL = os.environ.get("FDDT_RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not (HOST and OWNER_URL), reason="needs FDDT_PGBOUNCER_HOST and FDDT_RLS_TEST_DATABASE_URL"
)


def _connect(dbname="docauth_reuse_probe"):
    conn = psycopg2.connect(
        host=HOST, port=PORT, dbname=dbname,
        user=settings.database_app_user, password=settings.database_app_password,
    )
    return conn


@pytest.fixture(scope="module")
def companies():
    """Two companies with one case each, created through the owner role."""
    owner = create_engine(OWNER_URL, future=True)
    tag = uuid.uuid4().hex[:8]
    out = []
    with owner.begin() as conn:
        for key in ("A", "B"):
            company, user, case = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            conn.execute(text("INSERT INTO companies (id, name, is_active, max_file_size_mb, max_zip_size_mb) VALUES (:id, :n, true, 10, 300)"),
                         {"id": company, "n": f"PgBouncer probe {key} {tag}"})
            conn.execute(text(
                "INSERT INTO users (id, company_id, email, hashed_password, role, is_active, created_at, updated_at) "
                "VALUES (:id, :c, :e, 'x', 'user', true, now(), now())"
            ), {"id": user, "c": company, "e": f"pgb.{key}.{tag}@example.com"})
            conn.execute(text(
                "INSERT INTO cases (id, company_id, case_number, case_type, submitted_by_user_id, status, "
                "assigned_tier, created_at, updated_at) VALUES (:id, :c, :n, 'other', :u, 'submitted', 'l1', now(), now())"
            ), {"id": case, "c": company, "n": f"PGB-{key}-{tag}", "u": user})
            out.append({"company": company, "case": case})
    yield out
    with owner.begin() as conn:
        ids = [c["company"] for c in out]
        conn.execute(text("DELETE FROM cases WHERE company_id = ANY(:ids)"), {"ids": ids})
        conn.execute(text("DELETE FROM users WHERE company_id = ANY(:ids)"), {"ids": ids})
        conn.execute(text("DELETE FROM companies WHERE id = ANY(:ids)"), {"ids": ids})
    owner.dispose()


def _setting(cur):
    cur.execute("SELECT current_setting('app.current_company_id', true)")
    return cur.fetchone()[0] or ""


def test_negative_control_session_level_setting_does_leak(companies):
    """A plain session-level set_config is seen by the NEXT client on the same
    server connection — exactly the bug transaction pooling would cause if the
    app used it. (Proves the probe really reuses connections.)"""
    first = _connect()
    first.autocommit = True
    first.cursor().execute("SELECT set_config('app.current_company_id', %s, false)", (str(companies[0]["company"]),))
    first.close()
    second = _connect()
    second.autocommit = True
    cur = second.cursor()
    leaked = _setting(cur)
    cur.execute("SELECT set_config('app.current_company_id', '', false)")  # clean the server connection
    second.close()
    assert leaked == str(companies[0]["company"])


def test_transaction_local_setting_never_reaches_the_next_client(companies):
    a = companies[0]
    first = _connect()
    cur = first.cursor()
    cur.execute("SELECT set_config('app.current_company_id', %s, true)", (str(a["company"]),))
    cur.execute("SELECT count(*) FROM cases WHERE id = %s", (str(a["case"]),))
    assert cur.fetchone()[0] == 1  # visible inside its own transaction
    first.commit()
    first.close()

    second = _connect()  # same server connection (pool_size=1)
    cur = second.cursor()
    assert _setting(cur) == ""
    cur.execute("SELECT count(*) FROM cases")
    assert cur.fetchone()[0] == 0  # no company context => RLS shows nothing
    second.rollback()
    second.close()


def test_no_cross_company_leak_under_concurrent_load(companies):
    """32 clients x 25 transactions each, alternating companies, through the
    real (pooled) database alias and the single-connection probe at once.
    Every transaction must see exactly its own company's case and nothing
    else, and a transaction that sets no company must see nothing."""
    failures: list[str] = []
    lock = threading.Lock()

    def worker(n: int):
        dbname = "docauth_reuse_probe" if n % 2 else "docauth"
        conn = _connect(dbname)
        try:
            cur = conn.cursor()
            for i in range(25):
                me = companies[(n + i) % 2]
                if i % 5 == 4:
                    # A transaction with NO company: must inherit nothing.
                    cur.execute("SELECT current_setting('app.current_company_id', true), count(*) FROM cases")
                    setting, count = cur.fetchone()
                    if (setting or "") != "" or count != 0:
                        with lock:
                            failures.append(f"client {n} txn {i}: inherited setting={setting!r} rows={count}")
                    conn.commit()
                    continue
                cur.execute("SELECT set_config('app.current_company_id', %s, true)", (str(me["company"]),))
                cur.execute("SELECT company_id FROM cases")
                seen = {row[0] for row in cur.fetchall()}
                if seen != {str(me["company"])}:
                    with lock:
                        failures.append(f"client {n} txn {i}: expected {me['company']} saw {seen}")
                conn.commit()
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert failures == []


def test_the_app_engine_works_through_pgbouncer(companies, monkeypatch):
    """The real RoutingSession + tenancy hook against PgBouncer (NullPool)."""
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from app.db import tenancy

    url = (
        f"postgresql+psycopg2://{settings.database_app_user}:{settings.database_app_password}"
        f"@{HOST}:{PORT}/docauth"
    )
    engine = create_engine(url, poolclass=NullPool, future=True)
    Session = sessionmaker(bind=engine, future=True)
    for company in companies:
        session = tenancy.bind_company(Session(), company["company"])
        try:
            for _ in range(3):  # several transactions on one session
                assert set(session.execute(text("SELECT company_id FROM cases")).scalars()) == {company["company"]}
                session.commit()
        finally:
            session.close()
    engine.dispose()
