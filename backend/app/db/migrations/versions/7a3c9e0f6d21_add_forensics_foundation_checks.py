"""add field_validation/issuer_verification checks and issuer_registry

Revision ID: 7a3c9e0f6d21
Revises: 9fbd0685971d
Create Date: 2026-09-13 09:00:00.000000

Adds the pieces needed for the first three forensics/validation checks
(SPECIFICATION.md sections 3.1/2.2 — date/amount/consistency validation, issuer
verification; cross-document consistency reuses the existing
cross_document_findings table and needs no schema change):

- `document_check_type`: renames the unused `issuer_validation` value to
  `issuer_verification` (matches this check's actual name) and adds a
  new `field_validation` value.
- `issuer_registry` table + its `issuer_registry_type` enum.
- Seeds `issuer_registry` with a small set of FAKE test entries — some
  matching sample-documents/ vendor names so the issuer verification
  check has something to pass in local testing, plus two purely
  illustrative entries requested in the original task write-up. THIS
  DATA MUST BE REPLACED with the client's real vendor/school/tax-ID
  registry before this check means anything in production.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7a3c9e0f6d21"
down_revision: Union[str, None] = "9fbd0685971d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ISSUER_REGISTRY_TYPE_VALUES = ("vendor", "school", "government", "other")

# name, tax_id, type — see the module docstring for provenance.
SEED_ISSUERS = (
    ("TechSource Solutions Inc.", "45-1029384", "vendor"),
    ("Oakridge International Academy", "04-3918290", "school"),
    ("Al Nukhba Technical Systems Est.", "310291840000003", "vendor"),
    ("Al-Suwaidi Nuclear & Precision Instrumentation LLC", "100294819200003", "vendor"),
    # Purely illustrative fake entries (not matched by any current
    # sample document) named in the original task write-up.
    ("Al Falah General Trading LLC", None, "vendor"),
    ("Meridian Industrial Supplies LLC", None, "vendor"),
)


def upgrade() -> None:
    op.execute(
        "ALTER TYPE document_check_type RENAME VALUE 'issuer_validation' TO 'issuer_verification'"
    )
    op.execute("ALTER TYPE document_check_type ADD VALUE IF NOT EXISTS 'field_validation'")

    # Not pre-created via a separate `.create()` call — passing the ENUM
    # straight into `op.create_table` (matching e7ce49024f9e's user_role/
    # document_check_type columns) makes CREATE TABLE responsible for
    # emitting `CREATE TYPE` itself; calling `.create()` first as well
    # double-creates it and fails with "already exists".
    issuer_type_enum = postgresql.ENUM(*ISSUER_REGISTRY_TYPE_VALUES, name="issuer_registry_type")

    op.create_table(
        "issuer_registry",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("tax_id", sa.String(64), nullable=True),
        sa.Column("type", issuer_type_enum, nullable=False),
    )
    op.create_index("ix_issuer_registry_name", "issuer_registry", ["name"])

    issuer_registry_table = sa.table(
        "issuer_registry",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("name", sa.String),
        sa.column("tax_id", sa.String),
        sa.column("type", issuer_type_enum),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        issuer_registry_table,
        [
            {
                "id": uuid.uuid4(),
                "created_at": now,
                "updated_at": now,
                "name": name,
                "tax_id": tax_id,
                "type": issuer_type,
            }
            for name, tax_id, issuer_type in SEED_ISSUERS
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_issuer_registry_name", table_name="issuer_registry")
    op.drop_table("issuer_registry")
    postgresql.ENUM(name="issuer_registry_type").drop(op.get_bind(), checkfirst=True)

    # Postgres has no "remove enum value" — reverse the additive changes
    # that ARE reversible (the rename), and leave 'field_validation' as an
    # unused value rather than requiring a full enum-type rebuild.
    op.execute(
        "ALTER TYPE document_check_type RENAME VALUE 'issuer_verification' TO 'issuer_validation'"
    )
