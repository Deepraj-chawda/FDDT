"""
Seeds the platform admin account.

Usage (from backend/, with the venv active and migrations applied):
    python seed.py            # create the admin if it does not exist; never touch an existing one
    python seed.py --reset    # recovery: also reset an existing admin's password/role and re-activate it

Idempotent: every release runs `python seed.py` (the migration job), so it must
not undo a password the admin changed in the app. Use --reset only to recover a
locked-out admin. The account is a `platform_admin` — it belongs to no company.
Create companies and their users from the app (Platform > Companies,
Settings > Users).

Runs on the platform (RLS-bypassing) database role: creating platform
accounts is platform-level work.
"""
import os
import sys

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import system_session
from app.models.user import User, UserRole

# Defaults are for local development; override via env for any shared/demo environment.
SEED_EMAIL = os.environ.get("SEED_ADMIN_EMAIL", "admin@example.com")
_DEFAULT_PASSWORD = "ChangeMe123!"
SEED_PASSWORD = os.environ.get("SEED_ADMIN_PASSWORD", _DEFAULT_PASSWORD)


def _refuse_default_password() -> None:
    """Outside local/test, never SET the admin password to the published
    default (an existing account is left alone, so this only stops a new
    deployment or a --reset from creating a known platform-admin login)."""
    if settings.is_dev_environment:
        return
    if SEED_PASSWORD == _DEFAULT_PASSWORD or len(SEED_PASSWORD) < 12:
        sys.exit(
            f"seed.py: refusing to set the platform admin password for ENVIRONMENT={settings.environment!r}: "
            "set SEED_ADMIN_PASSWORD to a strong value (at least 12 characters, not the default)."
        )


def main(reset: bool = False) -> None:
    with system_session() as db:
        user = db.query(User).filter(User.email == SEED_EMAIL).one_or_none()
        if user is None or reset:
            _refuse_default_password()
        if user is None:
            user = User(
                email=SEED_EMAIL,
                hashed_password=hash_password(SEED_PASSWORD),
                full_name="Platform Admin",
                role=UserRole.platform_admin,
                company_id=None,
                is_active=True,
            )
            db.add(user)
            print(f"Created platform admin {SEED_EMAIL}")
        elif reset:
            user.hashed_password = hash_password(SEED_PASSWORD)
            user.role = UserRole.platform_admin
            user.company_id = None
            user.is_active = True
            print(f"Reset platform admin {SEED_EMAIL} (password, role, active)")
        else:
            print(f"Platform admin {SEED_EMAIL} already exists; left unchanged (use --reset to recover it)")
        db.commit()


if __name__ == "__main__":
    main(reset="--reset" in sys.argv[1:])
