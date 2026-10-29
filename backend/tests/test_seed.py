"""seed.py runs on every release (the migration job), so it must create the
platform admin once and never undo a password changed in the app; --reset is
the explicit recovery path."""
from contextlib import contextmanager

import pytest

import seed
from app.core.security import hash_password, verify_password
from app.db import tenancy
from app.models.user import User, UserRole


@pytest.fixture()
def run_seed(monkeypatch, db_session):
    tenancy.bind_platform(db_session)

    @contextmanager
    def _system_session():
        yield db_session

    monkeypatch.setattr(seed, "system_session", _system_session)
    monkeypatch.setattr(seed, "SEED_EMAIL", "root@example.com")
    monkeypatch.setattr(seed, "SEED_PASSWORD", "Seeded-Pass-1")
    return seed.main


def _admin(db_session):
    return db_session.query(User).filter(User.email == "root@example.com").one()


def test_creates_the_admin_when_missing(run_seed, db_session):
    run_seed()
    admin = _admin(db_session)
    assert admin.role == UserRole.platform_admin and admin.company_id is None and admin.is_active
    assert verify_password("Seeded-Pass-1", admin.hashed_password)


def test_rerun_keeps_a_changed_password(run_seed, db_session):
    run_seed()
    _admin(db_session).hashed_password = hash_password("Changed-In-App-2")
    db_session.commit()

    run_seed()  # the next release
    assert verify_password("Changed-In-App-2", _admin(db_session).hashed_password)


def test_reset_restores_the_seed_password_and_reactivates(run_seed, db_session):
    run_seed()
    admin = _admin(db_session)
    admin.hashed_password = hash_password("Forgotten-3")
    admin.is_active = False
    db_session.commit()

    run_seed(reset=True)
    admin = _admin(db_session)
    assert verify_password("Seeded-Pass-1", admin.hashed_password) and admin.is_active


# Outside local/test the published default password is never SET (a new
# deployment or --reset); a running deployment's existing admin is untouched.


@pytest.mark.parametrize("password", [seed._DEFAULT_PASSWORD, "short"])
def test_refuses_to_create_an_admin_with_a_default_or_weak_password_outside_dev(
    run_seed, db_session, monkeypatch, password
):
    monkeypatch.setattr(seed.settings, "environment", "production")
    monkeypatch.setattr(seed, "SEED_PASSWORD", password)
    with pytest.raises(SystemExit):
        run_seed()
    assert db_session.query(User).filter(User.email == "root@example.com").one_or_none() is None


def test_refuses_a_reset_to_the_default_outside_dev(run_seed, db_session, monkeypatch):
    run_seed()
    monkeypatch.setattr(seed.settings, "environment", "production")
    monkeypatch.setattr(seed, "SEED_PASSWORD", seed._DEFAULT_PASSWORD)
    with pytest.raises(SystemExit):
        run_seed(reset=True)
    assert verify_password("Seeded-Pass-1", _admin(db_session).hashed_password)


def test_an_existing_admin_is_left_alone_outside_dev_even_with_the_default(run_seed, db_session, monkeypatch):
    run_seed()
    monkeypatch.setattr(seed.settings, "environment", "production")
    monkeypatch.setattr(seed, "SEED_PASSWORD", seed._DEFAULT_PASSWORD)
    run_seed()  # what every restart of a running deployment does: no error, no change
    assert verify_password("Seeded-Pass-1", _admin(db_session).hashed_password)


def test_a_strong_password_is_used_outside_dev(run_seed, db_session, monkeypatch):
    monkeypatch.setattr(seed.settings, "environment", "production")
    monkeypatch.setattr(seed, "SEED_PASSWORD", "a-long-random-admin-password")
    run_seed()
    assert verify_password("a-long-random-admin-password", _admin(db_session).hashed_password)
