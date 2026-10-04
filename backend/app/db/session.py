"""
SQLAlchemy engine/session setup. Connection settings are entirely env-var
driven (`settings.database_url` + the app/platform role credentials) — never
hardcoded.

Two engines, one per database role (see app/core/config.py):

* `app_engine` — the ordinary, RLS-enforced role. Every company-scoped
  request and every pipeline task runs here, with the company set on each
  transaction (app/db/tenancy.py).
* `platform_engine` — the BYPASSRLS role. Only reachable through a session
  explicitly bound to platform mode (`system_session()` / `get_system_db`):
  sign-in lookups, platform-admin screens, usage reconciliation.

A session picks its engine from its tenancy binding (`RoutingSession`), so the
same `SessionLocal` serves both and the choice is made by the code that binds
it, never by a default: an unbound session refuses to touch tenant tables at
all.
"""
from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db import tenancy

_owner_url = make_url(settings.database_url)


def _role_engine(user: str, password: str):
    """Engine for one app role. Through PgBouncer (transaction pooling) the
    process holds no pool of its own; the per-transaction company setting is
    `set_config(..., is_local => true)`, which ends with the transaction —
    exactly when PgBouncer hands the server connection to another client —
    so it can never leak between tenants (tests/test_pgbouncer_rls.py)."""
    url = _owner_url.set(username=user, password=password)
    if settings.database_pooler_host:
        url = url.set(host=settings.database_pooler_host, port=settings.database_pooler_port)
        return create_engine(url, poolclass=NullPool, future=True)
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        future=True,
    )


app_engine = _role_engine(settings.database_app_user, settings.database_app_password)
platform_engine = _role_engine(settings.database_platform_user, settings.database_platform_password)

# Kept for code that needs "the" engine for metadata/inspection; queries go
# through a bound session.
engine = app_engine


class RoutingSession(Session):
    def get_bind(self, mapper=None, clause=None, **kw):
        if self.bind is not None:  # explicitly bound (tests)
            return self.bind
        if tenancy.is_platform(self):
            return platform_engine
        return app_engine


SessionLocal = sessionmaker(
    class_=RoutingSession, autocommit=False, autoflush=False, future=True
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: the request's data session. It starts UNBOUND —
    `get_tenant_db` (app/api/auth.py) binds it to the caller's company, or a
    platform-admin route binds it to the company it is acting on."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_system_db() -> Generator[Session, None, None]:
    """FastAPI dependency: a platform-mode (RLS-bypassing) session, for the
    explicit platform code path only — identity lookup at sign-in/token check
    and platform-admin endpoints."""
    db = SessionLocal()
    tenancy.bind_platform(db)
    try:
        yield db
    finally:
        db.close()


@contextmanager
def tenant_session(company_id) -> Iterator[Session]:
    """A session confined to one company — for Celery tasks and scripts."""
    db = SessionLocal()
    tenancy.bind_company(db, company_id)
    try:
        yield db
    finally:
        db.close()


@contextmanager
def system_session() -> Iterator[Session]:
    """A platform-mode (RLS-bypassing) session — for platform jobs (usage
    reconciliation, resolving which company a queued task belongs to) and
    scripts (seed.py)."""
    db = SessionLocal()
    tenancy.bind_platform(db)
    try:
        yield db
    finally:
        db.close()
