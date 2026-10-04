"""
The upload limits in effect for a company. This is the ONE source both upload
paths read, single-file (app/api/documents.py) and bulk zip (app/api/
bulk_uploads.py and its ingestion, app/services/bulk_upload_service.py).
Neither path has a fallback constant of its own.

Limits live on the company row (`companies.max_file_size_mb` /
`max_zip_size_mb`). A platform admin sets them (app/api/platform.py), and new
companies start from DEFAULT_MAX_FILE_SIZE_MB / DEFAULT_MAX_ZIP_SIZE_MB. They
are read fresh on every request, so a change takes effect on the company's
next upload with no new sign-in.

1 MB = 1,048,576 bytes, the same unit `format_size` reports in rejection
messages ("the maximum allowed size is 20.0 MB").
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company

MB = 1024 * 1024


@dataclass(frozen=True)
class UploadLimits:
    max_file_size_mb: int
    max_zip_size_mb: int

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_size_mb * MB

    @property
    def max_zip_bytes(self) -> int:
        return self.max_zip_size_mb * MB


def company_upload_limits(db: Session, company_id: uuid.UUID) -> UploadLimits:
    """The company's current limits. Works on a company-bound session (the
    company may read its own row) and on the platform session."""
    row = db.execute(
        select(Company.max_file_size_mb, Company.max_zip_size_mb).where(Company.id == company_id)
    ).one()
    return UploadLimits(max_file_size_mb=row[0], max_zip_size_mb=row[1])
