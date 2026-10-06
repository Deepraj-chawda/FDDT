"""add_signature_verification_tables

Creates signature_references and signature_matches tables for the
reviewer-driven signature verification flow (SPECIFICATION.md §2.3).

signature_references: one row per reviewer-created reference region
  (person_name typed manually, bounding_box normalized 0-1 page
  fractions, is_library flag for data collection vs. in-case use).

signature_matches: one row per (reference, target_document) comparison
  performed by the vision model — comparison_scope enum kept extensible
  for future library-wide comparison even though only "in_case" is
  generated now.

Revision ID: 15437688d14c
Revises: b1f4a8d29e6c
Create Date: 2026-09-18 22:13:52.962665

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '15437688d14c'
down_revision: Union[str, None] = 'b1f4a8d29e6c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'signature_references',
        sa.Column('person_name', sa.Text(), nullable=False),
        sa.Column('signature_image_url', sa.Text(), nullable=True),
        sa.Column(
            'bounding_box',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column('source_document_id', sa.UUID(), nullable=False),
        sa.Column('source_case_id', sa.UUID(), nullable=False),
        sa.Column('created_by', sa.UUID(), nullable=False),
        sa.Column('is_library', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.ForeignKeyConstraint(['source_case_id'], ['cases.id']),
        sa.ForeignKeyConstraint(['source_document_id'], ['documents.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_signature_references_source_case_id'),
        'signature_references',
        ['source_case_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_signature_references_source_document_id'),
        'signature_references',
        ['source_document_id'],
        unique=False,
    )

    op.create_table(
        'signature_matches',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('document_id', sa.UUID(), nullable=False),
        sa.Column('case_id', sa.UUID(), nullable=False),
        sa.Column('signature_reference_id', sa.UUID(), nullable=False),
        sa.Column(
            'comparison_scope',
            sa.Enum('in_case', 'library', name='comparison_scope'),
            nullable=False,
        ),
        sa.Column(
            'result',
            sa.Enum(
                'consistent',
                'possibly_consistent',
                'inconsistent',
                'cannot_determine',
                name='signature_match_result',
            ),
            nullable=False,
        ),
        sa.Column('reasoning', sa.Text(), nullable=True),
        sa.Column('compared_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['case_id'], ['cases.id']),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id']),
        sa.ForeignKeyConstraint(
            ['signature_reference_id'], ['signature_references.id']
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_signature_matches_case_id'),
        'signature_matches',
        ['case_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_signature_matches_document_id'),
        'signature_matches',
        ['document_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_signature_matches_signature_reference_id'),
        'signature_matches',
        ['signature_reference_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_signature_matches_signature_reference_id'),
        table_name='signature_matches',
    )
    op.drop_index(
        op.f('ix_signature_matches_document_id'), table_name='signature_matches'
    )
    op.drop_index(
        op.f('ix_signature_matches_case_id'), table_name='signature_matches'
    )
    op.drop_table('signature_matches')
    op.drop_index(
        op.f('ix_signature_references_source_document_id'),
        table_name='signature_references',
    )
    op.drop_index(
        op.f('ix_signature_references_source_case_id'),
        table_name='signature_references',
    )
    op.drop_table('signature_references')
    # Drop the enums that were created with the tables
    op.execute("DROP TYPE IF EXISTS comparison_scope")
    op.execute("DROP TYPE IF EXISTS signature_match_result")
