"""
Seeds the platform admin account for local development/manual testing.

Usage (from backend/, with the venv active and migrations applied):
    python seed.py

Idempotent: re-running it resets the seeded account's password/role instead of
failing on a duplicate email. The account is a `platform_admin` — it belongs
to no company. Create companies and their users from the app (Platform >
Companies, Settings > Users).

Runs on the platform (RLS-bypassing) database role: creating platform
accounts is platform-level work.
"""
import os

from app.core.security import hash_password
from app.db.session import system_session
from app.models.user import User, UserRole

# Defaults are for local development; override via env for any shared/demo environment.
SEED_EMAIL = os.environ.get("SEED_ADMIN_EMAIL", "admin@example.com")
SEED_PASSWORD = os.environ.get("SEED_ADMIN_PASSWORD", "ChangeMe123!")


def main() -> None:
    with system_session() as db:
        user = db.query(User).filter(User.email == SEED_EMAIL).one_or_none()
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
        else:
            user.hashed_password = hash_password(SEED_PASSWORD)
            user.role = UserRole.platform_admin
            user.company_id = None
            user.is_active = True
            print(f"Updated existing platform admin {SEED_EMAIL}")
        db.commit()


if __name__ == "__main__":
    main()
