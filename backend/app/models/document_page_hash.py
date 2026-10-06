"""
`document_page_hashes` table — one row per rendered PDF page's perceptual
hash, used by duplicate/near-duplicate detection (SPECIFICATION.md section 3.2;
see app/services/forensics/duplicate_check.py).

A small dedicated table rather than a `perceptual_hash` column on
`documents`, since a document can be a multi-page PDF — each page gets
its own hash, so a match can point at exactly which page of which prior
document it matched, not just "this document" vs "some document".
"""
import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.company import TenantScopedMixin


class DocumentPageHash(TenantScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "document_page_hashes"
    # One hash per page; a retried duplicate check upserts instead of
    # inserting a second copy.
    __table_args__ = (
        UniqueConstraint("document_id", "page_number", name="uq_document_page_hashes_document_page"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False, index=True
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Hex string of imagehash's 64-bit pHash (hash_size=8, the library's
    # default) — text, not a binary column, so it round-trips through
    # imagehash.hex_to_hash() with no custom type.
    phash: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    document = relationship("Document")
