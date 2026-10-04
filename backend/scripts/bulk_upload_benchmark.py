"""
Bulk-upload sizing benchmark: builds a realistic zip close to the 300 MB
limit and times the two stages separately:

  * inspect: what the upload REQUEST does (central directory only)
  * ingest:  what the Celery task does (extract + validate every PDF, store,
             create cases, queue documents)

Storage is in-memory and the DB is in-memory SQLite, so the times are the
CPU floor. Production adds a Blob Storage upload per document and Postgres
round-trips.

    python -m scripts.bulk_upload_benchmark --profile scanned
    python -m scripts.bulk_upload_benchmark --profile digital --target-mb 300

Profiles (per case):
  scanned  2 documents of ~2 MB (scanned pages: image-heavy, incompressible)
  digital  3 documents of ~40 KB (born-digital invoices: text, compressible)
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf  # noqa: E402
from PIL import Image  # noqa: E402

PROFILES = {
    "scanned": {"docs": 2, "doc_bytes": 2 * 1024 * 1024},
    "digital": {"docs": 3, "doc_bytes": 40 * 1024},
}


def _noise_png(target_bytes: int) -> bytes:
    side = max(32, int((target_bytes / 3) ** 0.5))
    return _png(Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)))


def _png(img) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format="PNG", compress_level=1)
    return buffer.getvalue()


def make_scanned_pdf(target_bytes: int, seed: int) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=_noise_png(target_bytes))
    page.insert_text((72, 72), f"Scanned claim document {seed}")
    data = doc.tobytes(deflate=True)
    doc.close()
    return data


def make_digital_pdf(target_bytes: int, seed: int) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    lines = [f"Invoice {seed} line {i}: item {os.urandom(6).hex()} qty {i} price {i * 3.5:.2f}" for i in range(60)]
    page.insert_textbox(page.rect + (40, 40, -40, -40), "\n".join(lines), fontsize=7)
    # Pad with a small incompressible image to reach the target size.
    pad = max(0, target_bytes - 8000)
    if pad:
        page.insert_image(pymupdf.Rect(400, 700, 560, 800), stream=_noise_png(pad))
    data = doc.tobytes(deflate=True)
    doc.close()
    return data


def build_zip(path: Path, profile: str, target_mb: int) -> tuple[int, int, int]:
    spec = PROFILES[profile]
    maker = make_scanned_pdf if profile == "scanned" else make_digital_pdf
    # A pool of distinct documents, reused round-robin (so building is fast);
    # each zip entry is still a separate file with its own name.
    pool = [maker(spec["doc_bytes"], i) for i in range(12)]
    target = target_mb * 1024 * 1024
    cases = docs = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        while path.stat().st_size < target - spec["docs"] * spec["doc_bytes"] * 1.2:
            cases += 1
            for d in range(spec["docs"]):
                zf.writestr(f"claims/case-{cases:05d}/document-{d + 1}.pdf", pool[(cases + d) % len(pool)])
                docs += 1
            if cases % 200 == 0:
                zf.fp.flush()
    return cases, docs, path.stat().st_size


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(PROFILES), default="scanned")
    parser.add_argument("--target-mb", type=int, default=295)
    args = parser.parse_args()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core.config import settings
    from app.db import tenancy
    from app.models import Base
    from app.models.bulk_upload import BulkUpload
    from app.models.case import CaseType
    from app.models.company import Company
    from app.models.user import User, UserRole
    from app.services import bulk_upload_service
    from app.services.storage_service import StorageService

    class MemoryStorage(StorageService):
        def __init__(self):
            self.files = {}

        def upload(self, blob_path, content, content_type=None):
            self.files[blob_path] = content.read() if hasattr(content, "read") else content
            return blob_path

        def get_download_url(self, file_url, expires_in_minutes=15):
            return file_url

        def download_bytes(self, file_url):
            return self.files[file_url]

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "bulk.zip"
        t0 = time.perf_counter()
        cases, docs, size = build_zip(zip_path, args.profile, args.target_mb)
        print(f"profile={args.profile}: {cases} cases, {docs} documents, zip {size / 1024 / 1024:.1f} MB "
              f"(built in {time.perf_counter() - t0:.1f}s)")

        with zip_path.open("rb") as fh:
            t0 = time.perf_counter()
            inspection = bulk_upload_service.inspect_zip(
                fh, size, max_zip_bytes=settings.default_max_zip_size_mb * 1024 * 1024,
                max_file_bytes=settings.default_max_file_size_mb * 1024 * 1024,
            )
            inspect_s = time.perf_counter() - t0
        print(f"inspect (upload request, central directory only): {inspect_s * 1000:.0f} ms, "
              f"{inspection.case_folder_count} case folders")

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        maker = sessionmaker(bind=engine, autoflush=False)
        db = tenancy.bind_platform(maker())
        company = Company(name="Bench")
        db.add(company)
        db.commit()
        tenancy.bind_company(db, company.id)
        db.expire_on_commit = False
        user = User(email="bench@example.com", hashed_password="x", role=UserRole.user, is_active=True)
        db.add(user)
        db.commit()
        storage = MemoryStorage()
        storage.files["bulk.zip"] = zip_path.read_bytes()
        bulk = BulkUpload(uploaded_by_user_id=user.id, original_filename="bulk.zip",
                          case_type=CaseType.other, blob_storage_path="bulk.zip", file_hash="0" * 64,
                          zip_size_bytes=size, case_folder_count=inspection.case_folder_count)
        db.add(bulk)
        db.flush()
        db.add_all(bulk_upload_service.plan_rows(bulk, inspection.plan))
        db.commit()

        queued_at: list[float] = []
        t0 = time.perf_counter()
        bulk_upload_service.ingest(db, storage, bulk.id, company.id,
                                   enqueue=lambda d, c: queued_at.append(time.perf_counter() - t0))
        ingest_s = time.perf_counter() - t0
        db.refresh(bulk)
        print(f"ingest (Celery task): {ingest_s:.1f}s total, {ingest_s / max(cases, 1) * 1000:.0f} ms per case; "
              f"created {bulk.cases_created} cases / {bulk.documents_accepted} documents")
        if queued_at:
            print(f"first document queued after {queued_at[0] * 1000:.0f} ms; last after {queued_at[-1]:.1f}s")


if __name__ == "__main__":
    main()
