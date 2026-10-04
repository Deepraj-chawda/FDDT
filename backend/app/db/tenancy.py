"""
Application-layer tenant isolation (layer 1 of 2 — layer 2 is the PostgreSQL
Row-Level Security policies created by the multi-tenancy migration).

Every SQLAlchemy session is in exactly one of three states:

* unbound  — the default. It REFUSES to query or write any tenant-owned table
             (`TenantScopedMixin` models): a code path that forgot to say
             which company it works for fails loudly instead of silently
             reading everything.
* company  — bound to one company (`bind_company`). Every ORM SELECT/UPDATE/
             DELETE that touches a tenant-owned model gets
             `company_id = <bound company>` added automatically (including
             relationship and `session.get()` loads); every INSERT is stamped
             with that company, and an object carrying a different company is
             rejected. On PostgreSQL the session also runs on the RLS-enforced
             app role with `app.current_company_id` set at the start of every
             transaction, so the database applies the same filter
             independently.
* platform — the explicit, RLS-bypassing platform path (`bind_platform`), for
             sign-in lookups and platform-admin operations only. No automatic
             filter: platform code passes company ids explicitly.

The automatic filter is a backstop, not a replacement for explicit filtering:
service functions that read tenant data still take a `company_id` argument and
filter by it themselves.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from app.models.company import TenantScopedMixin

_MODE = "tenancy_mode"
_COMPANY = "tenancy_company_id"
_COMPANY_MODE = "company"
_PLATFORM_MODE = "platform"

# Name of the PostgreSQL setting the RLS policies read.
COMPANY_SETTING = "app.current_company_id"


class TenantContextError(RuntimeError):
    """A session touched tenant data without a tenant context, or tried to
    write a row belonging to a different company."""


def _as_uuid(company_id: Any) -> uuid.UUID:
    if isinstance(company_id, uuid.UUID):
        return company_id
    return uuid.UUID(str(company_id))


def _check_rebind(session: Session, mode: str, company_id: uuid.UUID | None) -> None:
    """Changing a PostgreSQL session's binding mid-transaction would leave the
    open transaction on the old engine/company setting. Callers must bind
    before first use (or commit/rollback first)."""
    current = (session.info.get(_MODE), session.info.get(_COMPANY))
    if current == (mode, company_id) or current == (None, None):
        return
    if not session.in_transaction():
        return
    for conn in _connections(session):
        if conn.dialect.name == "postgresql":
            raise TenantContextError(
                "Cannot change a session's tenant binding while a transaction is open; "
                "commit or roll back first."
            )


def _connections(session: Session):
    transaction = session.get_transaction()
    if transaction is None:
        return []
    return [conn for conn, *_ in transaction._connections.values()]  # noqa: SLF001


def _evict_other_companies(session: Session, company_id: uuid.UUID) -> None:
    """`session.get()` answers from the identity map without running a query,
    so the automatic filter would never see it. Objects of any other company
    still cached from an earlier binding (platform mode, another company) are
    removed when the session is confined to `company_id`."""
    from sqlalchemy import inspect as sa_inspect

    for obj in list(session.identity_map.values()):
        if not isinstance(obj, TenantScopedMixin):
            continue
        loaded = sa_inspect(obj).dict
        # An expired object (attributes not loaded) is safe to keep: touching
        # it re-SELECTs through the filtered query, which won't return a row
        # of another company.
        if "company_id" in loaded and loaded["company_id"] != company_id:
            session.expunge(obj)


def bind_company(session: Session, company_id: Any) -> Session:
    if company_id is None:
        raise TenantContextError("bind_company needs a company id")
    cid = _as_uuid(company_id)
    _check_rebind(session, _COMPANY_MODE, cid)
    if snapshot(session) != (_COMPANY_MODE, cid):
        _evict_other_companies(session, cid)
    session.info[_MODE] = _COMPANY_MODE
    session.info[_COMPANY] = cid
    return session


def bind_platform(session: Session) -> Session:
    _check_rebind(session, _PLATFORM_MODE, None)
    session.info[_MODE] = _PLATFORM_MODE
    session.info[_COMPANY] = None
    return session


def is_platform(session: Session) -> bool:
    return session.info.get(_MODE) == _PLATFORM_MODE


def bound_company_id(session: Session) -> uuid.UUID | None:
    """The company a session is confined to (None in platform/unbound mode)."""
    if session.info.get(_MODE) == _COMPANY_MODE:
        return session.info[_COMPANY]
    return None


def snapshot(session: Session) -> tuple[Any, Any]:
    return session.info.get(_MODE), session.info.get(_COMPANY)


def restore(session: Session, state: tuple[Any, Any]) -> None:
    session.info[_MODE], session.info[_COMPANY] = state


def _touches_tenant_tables(state: ORMExecuteState) -> bool:
    return any(
        issubclass(mapper.class_, TenantScopedMixin) for mapper in state.all_mappers
    )


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_filter(state: ORMExecuteState) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    mode = state.session.info.get(_MODE)
    if mode == _PLATFORM_MODE:
        return
    if mode != _COMPANY_MODE:
        if _touches_tenant_tables(state):
            raise TenantContextError(
                "Tenant-owned data queried from a session with no tenant context. "
                "Bind the session with bind_company() (or bind_platform() for the "
                "platform-admin path) first."
            )
        return
    company_id = state.session.info[_COMPANY]
    state.statement = state.statement.options(
        with_loader_criteria(
            TenantScopedMixin,
            lambda cls: cls.company_id == company_id,
            include_aliases=True,
        )
    )


@event.listens_for(Session, "before_flush")
def _stamp_and_check_company(session: Session, flush_context, instances) -> None:
    mode = session.info.get(_MODE)
    company_id = session.info.get(_COMPANY) if mode == _COMPANY_MODE else None
    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, TenantScopedMixin):
            continue
        if mode is None:
            raise TenantContextError(
                f"Cannot write {type(obj).__name__} from a session with no tenant context."
            )
        if mode == _PLATFORM_MODE:
            continue  # platform code sets company_id explicitly
        if obj.company_id is None:
            if obj in session.new:
                obj.company_id = company_id
            else:
                raise TenantContextError(f"{type(obj).__name__}.company_id cannot be cleared.")
        elif obj.company_id != company_id:
            raise TenantContextError(
                f"Refusing to write a {type(obj).__name__} belonging to another company."
            )


@event.listens_for(Session, "after_begin")
def _set_postgres_company_setting(session: Session, transaction, connection) -> None:
    """Hand the session's company to PostgreSQL for the RLS policies. Set
    transaction-locally (`is_local = true`) so a pooled connection never
    carries one request's company into the next."""
    if connection.dialect.name != "postgresql":
        return
    company_id = bound_company_id(session)
    connection.exec_driver_sql(
        "SELECT set_config(%(name)s, %(value)s, true)",
        {"name": COMPANY_SETTING, "value": str(company_id) if company_id else ""},
    )
