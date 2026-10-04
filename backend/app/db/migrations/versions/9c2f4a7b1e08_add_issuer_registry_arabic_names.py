"""add issuer_registry.name_arabic and seed Arabic vendor names

Revision ID: 9c2f4a7b1e08
Revises: 7a3c9e0f6d21
Create Date: 2026-09-14 09:00:00.000000

Issuer verification (app/services/issuer_service.py) fuzzy-matches an
extracted issuer name against `issuer_registry.name`. A single English
`name` can never fuzzy-match an Arabic-script extracted name — different
scripts, not just spelling variance — so this adds an optional
`name_arabic` column, and seeds it for the vendors used in our Arabic
sample documents (sample-documents/Sample7_Arabic_*, Sample9_Arabic_*):
- Al Nukhba Technical Systems Est. (already seeded, English-only) gets
  its Arabic name added.
- The Sample7 hotel vendor isn't in the registry yet at all — added here
  with both names.

Still FAKE test data — see the original seed migration
(7a3c9e0f6d21)'s note about replacing this with the client's real
registry before production use.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9c2f4a7b1e08"
down_revision: Union[str, None] = "7a3c9e0f6d21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

AL_NUKHBA_NAME_ARABIC = "مؤسسة النخبة للأنظمة التقنية"
HOTEL_NAME_ENGLISH = "Qasr Al-Waha Luxury Hotel & Suites"
HOTEL_NAME_ARABIC = "فندق وأجنحة قصر الواحة الفاخر"

# References the enum type created by 7a3c9e0f6d21 — not recreated here.
_issuer_type_enum = postgresql.ENUM(name="issuer_registry_type", create_type=False)


def upgrade() -> None:
    op.add_column("issuer_registry", sa.Column("name_arabic", sa.String(255), nullable=True))
    op.create_index("ix_issuer_registry_name_arabic", "issuer_registry", ["name_arabic"])

    name_only_table = sa.table(
        "issuer_registry", sa.column("name", sa.String), sa.column("name_arabic", sa.String)
    )
    op.execute(
        name_only_table.update()
        .where(name_only_table.c.name == "Al Nukhba Technical Systems Est.")
        .values(name_arabic=AL_NUKHBA_NAME_ARABIC)
    )

    now = datetime.now(timezone.utc)
    op.bulk_insert(
        sa.table(
            "issuer_registry",
            sa.column("id", postgresql.UUID(as_uuid=True)),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
            sa.column("name", sa.String),
            sa.column("name_arabic", sa.String),
            sa.column("tax_id", sa.String),
            sa.column("type", _issuer_type_enum),
        ),
        [
            {
                "id": uuid.uuid4(),
                "created_at": now,
                "updated_at": now,
                "name": HOTEL_NAME_ENGLISH,
                "name_arabic": HOTEL_NAME_ARABIC,
                "tax_id": None,
                "type": "vendor",
            }
        ],
    )


def downgrade() -> None:
    name_only_table = sa.table(
        "issuer_registry", sa.column("name", sa.String), sa.column("name_arabic", sa.String)
    )
    op.execute(name_only_table.delete().where(name_only_table.c.name == HOTEL_NAME_ENGLISH))
    op.execute(
        name_only_table.update()
        .where(name_only_table.c.name == "Al Nukhba Technical Systems Est.")
        .values(name_arabic=None)
    )
    op.drop_index("ix_issuer_registry_name_arabic", table_name="issuer_registry")
    op.drop_column("issuer_registry", "name_arabic")
