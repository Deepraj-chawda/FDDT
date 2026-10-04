import uuid
from datetime import datetime

from pydantic import BaseModel


class CaseReportResponse(BaseModel):
    """One generated report. `download_url` is a short-lived signed URL (the
    container is private — see app/services/storage_service.py), so it is
    minted fresh on every response rather than stored."""

    id: uuid.UUID
    case_id: uuid.UUID
    generated_by_name: str | None
    generated_at: datetime
    file_size_bytes: int
    page_count: int
    report_sha256: str
    risk_tier: str | None
    risk_score: int | None
    download_url: str
