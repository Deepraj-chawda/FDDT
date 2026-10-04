"""
Bulk upload: one zip, one top-level folder per case, that case's documents
directly inside it.

    claims.zip
    ├── case-001/  invoice.pdf  receipt.pdf
    ├── case-002/  فاتورة.pdf
    └── case-003/  quote.pdf

The zip is only a way to get cases in. Two stages:

1. `inspect_zip`: synchronous, in the upload request (app/api/bulk_uploads.py).
   It reads the zip's central directory only and never decompresses a
   document. Zip-level problems are rejected right away (empty, over 300 MB,
   not a zip, corrupted, too many entries, no case folders). It also builds
   the per-case PLAN, which already holds every structural failure that
   needs no decompression (nested folder, empty folder, 0-byte or oversized
   file). The uploader sees those failures in the upload response, before
   anything is processed.

2. `ingest`: the Celery task (app/tasks/bulk_upload_task.py). It works case
   by case. It extracts each document, runs the single-file checks on it
   (app/services/upload_validation.py: size, PDF header bytes, extension,
   parse, password), stores the accepted ones and creates the case. It
   QUEUES THAT CASE'S DOCUMENTS IMMEDIATELY and only then moves to the next
   case, so no document waits for the rest of its zip. Each document's
   pipeline is the ordinary per-document one with the usual fair-share
   priority (app/services/document_intake.py), and nothing ever reports back
   to the upload.

Partial success: a bad file is rejected on its own and the rest of its case
proceeds. A case with no valid document fails on its own and the other cases
proceed. A case folder containing a subfolder fails as a whole. Files are
never guessed into a case.

Idempotent resume: each case's creation commits together with its
`bulk_upload_cases` row. A redelivered or retried task skips rows that are no
longer "pending", so a crash mid-zip never duplicates a case.

Transactions: no DB transaction is held across the zip download, document
extraction, validation or blob uploads. Each case is one short write
transaction.
"""
from __future__ import annotations

import binascii
import copy
import logging
import re
import struct
import tempfile
import unicodedata
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, BinaryIO, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.models.base import utcnow
from app.models.bulk_upload import BulkUpload, BulkUploadCase, BulkUploadStatus
from app.models.case import Case, CaseStatus
from app.services.audit_service import record_event
from app.services.document_intake import enqueue_document_pipeline, record_document, store_original
from app.services.storage_service import StorageService
from app.services.upload_limits import company_upload_limits
from app.services.upload_validation import UploadRejected, check_size, format_size, validate_upload
from app.services.usage_service import record_case_created

logger = logging.getLogger("fddt.bulk_upload")

ZIP_CONTENT_TYPE = "application/zip"

# Zip local-file-header / empty-archive signatures.
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06")
# General-purpose flag bits.
_FLAG_ENCRYPTED = 0x1
_FLAG_UTF8 = 0x800
# Info-ZIP "Unicode Path" extra field: version(1) + CRC-32 of the raw name(4)
# + the name in UTF-8.
_UNICODE_PATH_EXTRA = 0x7075

# OS clutter that is never a document and never a case folder.
_JUNK_COMPONENTS = frozenset({"__MACOSX"})
_JUNK_NAMES = frozenset({"thumbs.db", "desktop.ini"})
# Kept short: the list is for the uploader's information only.
_MAX_IGNORED_LISTED = 200


def _rejected(code: str, message: str, status_code: int, **extra: Any) -> UploadRejected:
    return UploadRejected(code, message, status_code, **extra)


# ---------------------------------------------------------------------------
# Entry names
# ---------------------------------------------------------------------------


def _unicode_path_extra(info: zipfile.ZipInfo, raw_name: bytes) -> str | None:
    extra = info.extra or b""
    offset = 0
    while offset + 4 <= len(extra):
        header_id, size = struct.unpack_from("<HH", extra, offset)
        body = extra[offset + 4 : offset + 4 + size]
        offset += 4 + size
        if header_id != _UNICODE_PATH_EXTRA or len(body) < 5 or body[0] != 1:
            continue
        (name_crc,) = struct.unpack_from("<I", body, 1)
        if name_crc != (binascii.crc32(raw_name) & 0xFFFFFFFF):
            return None  # the name was changed after the extra field was written
        try:
            return body[5:].decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def decode_entry_name(info: zipfile.ZipInfo) -> str:
    """The entry's real name, including non-ASCII (e.g. Arabic) names from
    zip tools that don't set the UTF-8 flag.

    * UTF-8 flag set: Python already decoded it correctly.
    * Otherwise Python decoded the raw bytes as CP437 (the zip default). The
      raw bytes are recovered and, in order, (a) the Info-ZIP Unicode Path
      extra field is used if present and valid, then (b) the bytes are tried
      as UTF-8. Many tools write UTF-8 names without the flag, and CP437 bytes
      that happen to form valid multi-byte UTF-8 are vanishingly rare in
      practice.
    * Else the CP437 reading stands. A legacy zip whose names are in a
      regional code page (e.g. CP720 Arabic from an old Windows "Send to
      compressed folder") can't be detected reliably and keeps its CP437
      reading.
    """
    name = info.filename
    if info.flag_bits & _FLAG_UTF8:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name  # decoded with a custom metadata_encoding; trust it
    from_extra = _unicode_path_extra(info, raw)
    if from_extra:
        return from_extra
    if raw.isascii():
        return name
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return name


def _parts(name: str) -> list[str]:
    """Path components, normalised. Windows separators become "/", and
    leading "./" and "/" are dropped. NFC is applied so a name typed on macOS
    (decomposed accents) matches the same name typed elsewhere."""
    name = unicodedata.normalize("NFC", name.replace("\\", "/"))
    return [p for p in name.split("/") if p not in ("", ".")]


def _is_junk(parts: list[str]) -> bool:
    return any(
        p in _JUNK_COMPONENTS or p.startswith(".") or p.lower() in _JUNK_NAMES for p in parts
    )


def _natural_key(text: str) -> list:
    """'case-2' before 'case-10'."""
    return [int(chunk) if chunk.isdigit() else chunk.lower() for chunk in re.split(r"(\d+)", text)]


# ---------------------------------------------------------------------------
# Plan (from the central directory only)
# ---------------------------------------------------------------------------


@dataclass
class _Entry:
    index: int  # position in ZipFile.infolist()
    parts: list[str]
    is_dir: bool
    size: int
    encrypted: bool


@dataclass
class ZipInspection:
    plan: dict[str, Any]
    case_folder_count: int
    entry_count: int
    warnings: list[str] = field(default_factory=list)


def _entries(zf: zipfile.ZipFile) -> tuple[list[_Entry], list[str]]:
    entries: list[_Entry] = []
    ignored: list[str] = []
    for index, info in enumerate(zf.infolist()):
        parts = _parts(decode_entry_name(info))
        if not parts:
            continue
        if ".." in parts:
            ignored.append("/".join(parts))  # never resolved against a path anyway
            continue
        if _is_junk(parts):
            continue
        entries.append(
            _Entry(
                index=index,
                parts=parts,
                is_dir=info.is_dir(),
                size=info.file_size,
                encrypted=bool(info.flag_bits & _FLAG_ENCRYPTED),
            )
        )
    return entries, ignored


def _unwrap(entries: list[_Entry]) -> tuple[list[_Entry], str | None]:
    """Zipping a folder of case folders (right-click → Compress) puts one
    extra folder around everything: `claims/case-001/a.pdf`. If every file
    sits two or more folders deep under the same single top folder, that top
    folder is a wrapper. It is stripped ONCE, and the case folders are the
    level below. It is never a case folder with nested subfolders, which
    would fail every case for a packaging detail."""
    files = [e for e in entries if not e.is_dir]
    if not files:
        return entries, None
    tops = {e.parts[0] for e in files}
    if len(tops) != 1 or any(len(e.parts) < 3 for e in files):
        return entries, None
    wrapper = next(iter(tops))
    stripped = [
        _Entry(e.index, e.parts[1:], e.is_dir, e.size, e.encrypted)
        for e in entries
        if e.parts[0] == wrapper and len(e.parts) > 1
    ]
    return stripped, wrapper


def _file_entry(entry: _Entry, max_file_bytes: int) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": entry.parts[-1],
        "entry_index": entry.index,
        "size_bytes": entry.size,
        "status": "pending",
        "code": None,
        "message": None,
        "document_id": None,
    }
    # Structural checks that need no decompression. The declared size is
    # re-checked against the real bytes at extraction, since a zip can lie.
    try:
        check_size(entry.size, max_file_bytes)
    except UploadRejected as rejection:
        item.update(status="rejected", code=rejection.code, message=rejection.message)
        return item
    if entry.encrypted:
        item.update(
            status="rejected",
            code="file_encrypted_in_zip",
            message="This file is password-protected inside the zip. "
            "Please zip it without a password and re-upload.",
        )
    return item


def build_plan(zf: zipfile.ZipFile, *, max_file_bytes: int) -> ZipInspection:
    entries, ignored = _entries(zf)
    entries, wrapper = _unwrap(entries)

    folders: dict[str, dict[str, list]] = {}
    for entry in entries:
        if len(entry.parts) == 1:
            if not entry.is_dir:
                ignored.append(entry.parts[0])  # loose file, not in any case folder
            else:
                folders.setdefault(entry.parts[0], {"files": [], "subfolders": []})
            continue
        folder = folders.setdefault(entry.parts[0], {"files": [], "subfolders": []})
        if len(entry.parts) == 2 and not entry.is_dir:
            folder["files"].append(entry)
        elif len(entry.parts) >= 3 and not entry.is_dir:
            # A FILE inside a subfolder: that case's contents are ambiguous.
            sub = entry.parts[1]
            if sub not in folder["subfolders"]:
                folder["subfolders"].append(sub)
        # An empty subfolder (directory entry only) holds nothing to
        # misattribute, so it is ignored.

    cases: list[dict[str, Any]] = []
    for index, name in enumerate(sorted(folders, key=_natural_key)):
        folder = folders[name]
        case: dict[str, Any] = {
            "index": index,
            "folder": name,
            "status": "pending",
            "error_code": None,
            "error_message": None,
            "case_id": None,
            "case_number": None,
            "files": [_file_entry(e, max_file_bytes) for e in folder["files"]],
        }
        if folder["subfolders"]:
            listed = ", ".join(f"'{s}'" for s in folder["subfolders"][:5])
            for item in case["files"]:
                if item["status"] == "pending":
                    item["status"] = "skipped"  # its folder was rejected; never examined
            case.update(
                status="failed",
                error_code="case_nested_folder",
                error_message=(
                    f"This case folder contains a subfolder ({listed}). Put every document of a case "
                    "directly in its case folder, with no subfolders, and re-upload this case."
                ),
            )
        elif not folder["files"]:
            case.update(
                status="failed",
                error_code="case_no_documents",
                error_message="This case folder contains no files.",
            )
        cases.append(case)

    plan = {
        "wrapper_folder": wrapper,
        "ignored_entries": ignored[:_MAX_IGNORED_LISTED],
        "ignored_entry_count": len(ignored),
        "cases": cases,
    }
    warnings: list[str] = []
    threshold = settings.bulk_upload_case_warning_threshold
    if len(cases) > threshold:
        warnings.append(
            f"This zip contains {len(cases)} cases (more than {threshold}). They will all be "
            "processed, but results for the later cases will take longer to appear."
        )
    return ZipInspection(plan=plan, case_folder_count=len(cases), entry_count=len(zf.infolist()), warnings=warnings)


def inspect_zip(fileobj: BinaryIO, size: int, *, max_zip_bytes: int, max_file_bytes: int) -> ZipInspection:
    """Zip-level checks, in order (the first failure wins), then the plan.
    Reads the central directory only. `fileobj` is the whole zip, positioned
    anywhere. Raises UploadRejected."""
    if size == 0:
        raise _rejected("zip_empty", "The zip file is empty (0 bytes).", 400, size_bytes=0)
    if size > max_zip_bytes:
        raise zip_too_large(size, max_zip_bytes)
    fileobj.seek(0)
    head = fileobj.read(4)
    if head not in _ZIP_SIGNATURES:
        raise _rejected(
            "not_a_zip",
            "This file is not a zip archive. Bulk upload takes one .zip file with one folder per case.",
            415,
        )
    fileobj.seek(0)
    try:
        zf = zipfile.ZipFile(fileobj)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError):
        raise _rejected(
            "zip_corrupted", "The zip file appears to be corrupted or incomplete.", 422
        ) from None
    with zf:
        entry_count = len(zf.infolist())
        if entry_count > settings.bulk_upload_max_entries:
            raise _rejected(
                "zip_too_many_entries",
                f"The zip contains {entry_count} entries; at most "
                f"{settings.bulk_upload_max_entries} are accepted. Split it into smaller zips.",
                422,
                entry_count=entry_count,
                max_entries=settings.bulk_upload_max_entries,
            )
        inspection = build_plan(zf, max_file_bytes=max_file_bytes)
    if inspection.case_folder_count == 0:
        raise _rejected(
            "zip_no_case_folders",
            "The zip contains no case folders. Put each case's documents in its own folder "
            "inside the zip (e.g. case-001/invoice.pdf).",
            422,
        )
    return inspection


def zip_too_large(size: int | None, max_zip_bytes: int) -> UploadRejected:
    actual = f"{format_size(size)}" if size is not None else f"more than {format_size(max_zip_bytes)}"
    return _rejected(
        "zip_too_large",
        f"The zip file is {actual}; the maximum allowed size is {format_size(max_zip_bytes)}.",
        413,
        size_bytes=size,
        max_size_bytes=max_zip_bytes,
    )


# ---------------------------------------------------------------------------
# Ingestion (Celery task body)
# ---------------------------------------------------------------------------


def _extract(zf: zipfile.ZipFile, info: zipfile.ZipInfo, max_file_bytes: int) -> bytes:
    """One document's bytes, never reading more than the size limit + 1
    whatever the zip header claims (zip-bomb safe). Raises UploadRejected."""
    try:
        with zf.open(info) as fh:
            data = fh.read(max_file_bytes + 1)
    except RuntimeError as exc:  # "File ... is encrypted, password required"
        if "encrypt" in str(exc).lower() or "password" in str(exc).lower():
            raise _rejected(
                "file_encrypted_in_zip",
                "This file is password-protected inside the zip. "
                "Please zip it without a password and re-upload.",
                422,
            ) from None
        raise
    except NotImplementedError:
        raise _rejected(
            "zip_entry_unsupported",
            "This file uses a zip compression method that isn't supported. "
            "Re-create the zip with standard (Deflate) compression.",
            422,
        ) from None
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError):
        raise _rejected(
            "file_corrupted", "The file could not be extracted from the zip (it appears corrupted).", 422
        ) from None
    if len(data) > max_file_bytes:
        check_size(max(info.file_size, len(data)), max_file_bytes)
    return data


@dataclass
class _Accepted:
    item: dict[str, Any]
    content: bytes
    content_type: str


def _validate_case_files(
    zf: zipfile.ZipFile, infos: list[zipfile.ZipInfo], case: dict[str, Any], max_file_bytes: int
) -> list[_Accepted]:
    accepted: list[_Accepted] = []
    for item in case["files"]:
        if item["status"] != "pending":
            continue  # rejected structurally in the plan
        try:
            content = _extract(zf, infos[item["entry_index"]], max_file_bytes)
            validated = validate_upload(content, item["name"], max_bytes=max_file_bytes)
        except UploadRejected as rejection:
            item.update(status="rejected", code=rejection.code, message=rejection.message)
            continue
        item["size_bytes"] = len(content)
        accepted.append(_Accepted(item, content, validated.content_type))
    return accepted


def plan_rows(bulk: BulkUpload, plan: dict[str, Any]) -> list[BulkUploadCase]:
    """The `bulk_upload_cases` rows for a freshly inspected zip, one per case
    folder, structural failures already marked."""
    return [
        BulkUploadCase(
            company_id=bulk.company_id,
            bulk_upload_id=bulk.id,
            folder_index=case["index"],
            folder=case["folder"],
            status=case["status"],
            error_code=case["error_code"],
            error_message=case["error_message"],
            files=case["files"],
        )
        for case in plan["cases"]
    ]


def _tally(bulk: BulkUpload, row: BulkUploadCase, before: list[dict[str, Any]]) -> None:
    """Move the parent's counters by what this case's ingestion changed.
    (Structural failures were counted when the plan was written.)"""
    if row.status == "created":
        bulk.cases_created += 1
    elif row.status == "failed":
        bulk.cases_failed += 1
    was_rejected = {i for i, f in enumerate(before) if f["status"] == "rejected"}
    for i, f in enumerate(row.files):
        if f["status"] == "accepted":
            bulk.documents_accepted += 1
        elif f["status"] == "rejected" and i not in was_rejected:
            bulk.documents_rejected += 1


Enqueue = Callable[[uuid.UUID, uuid.UUID], None]


def ingest(
    db: Session,
    storage: StorageService,
    bulk_upload_id: uuid.UUID,
    company_id: uuid.UUID,
    *,
    enqueue: Enqueue | None = None,
) -> None:
    """Ingest one stored zip: case by case, validate → store → create →
    queue. `db` is bound to `company_id`. Safe to re-run (see module doc)."""
    enqueue = enqueue or enqueue_document_pipeline
    bulk = db.execute(
        select(BulkUpload).where(BulkUpload.id == bulk_upload_id, BulkUpload.company_id == company_id)
    ).scalar_one_or_none()
    if bulk is None or bulk.status in (BulkUploadStatus.complete, BulkUploadStatus.failed):
        return
    bulk.status = BulkUploadStatus.ingesting
    bulk.started_at = bulk.started_at or utcnow()
    pending_ids = db.execute(
        select(BulkUploadCase.id)
        .where(
            BulkUploadCase.bulk_upload_id == bulk.id,
            BulkUploadCase.company_id == company_id,
            BulkUploadCase.status == "pending",
        )
        .order_by(BulkUploadCase.folder_index)
    ).scalars().all()
    db.commit()  # nothing is held open during the download below

    # The same per-company source the upload request used.
    max_file_bytes = company_upload_limits(db, company_id).max_file_bytes
    db.commit()
    if pending_ids:
        with tempfile.TemporaryFile() as tmp:
            storage.download_to(bulk.blob_storage_path, tmp)
            tmp.seek(0)
            try:
                zf = zipfile.ZipFile(tmp)
            except (zipfile.BadZipFile, OSError, ValueError, EOFError):
                bulk.status = BulkUploadStatus.failed
                bulk.error_code = "zip_corrupted"
                bulk.error_message = "The zip file appears to be corrupted or incomplete."
                bulk.finished_at = utcnow()
                db.commit()
                return
            with zf:
                infos = zf.infolist()
                for row_id in pending_ids:
                    row = db.get(BulkUploadCase, row_id)
                    db.commit()  # end the read before extraction / blob uploads
                    _ingest_case(db, storage, zf, infos, bulk, row, company_id, max_file_bytes, enqueue)

    bulk.status = BulkUploadStatus.complete
    bulk.finished_at = utcnow()
    record_event(
        db,
        "bulk_upload_ingested",
        actor_user_id=bulk.uploaded_by_user_id,
        event_data={
            "bulk_upload_id": str(bulk.id),
            "original_filename": bulk.original_filename,
            "cases_created": bulk.cases_created,
            "cases_failed": bulk.cases_failed,
            "documents_accepted": bulk.documents_accepted,
            "documents_rejected": bulk.documents_rejected,
        },
    )
    db.commit()


def _ingest_case(
    db: Session,
    storage: StorageService,
    zf: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    bulk: BulkUpload,
    row: BulkUploadCase,
    company_id: uuid.UUID,
    max_file_bytes: int,
    enqueue: Enqueue,
) -> None:
    before = copy.deepcopy(row.files)
    entry = {"folder": row.folder, "files": copy.deepcopy(row.files)}
    # CPU work and blob uploads: no transaction is open here.
    accepted = _validate_case_files(zf, infos, entry, max_file_bytes)
    if not accepted:
        row.status = "failed"
        row.error_code = "case_no_valid_documents"
        row.error_message = "None of this case's files passed validation, so no case was created."
        row.files = entry["files"]
        flag_modified(row, "files")
        _tally(bulk, row, before)
        db.commit()
        return

    case_id = uuid.uuid4()
    stored = [
        store_original(storage, company_id, case_id, a.item["name"], a.content, a.content_type)
        for a in accepted
    ]

    # One short write transaction: the case, its documents, their audit
    # events and counters, and this folder's row. They are committed
    # together, which is what makes a re-run skip this case.
    new_case = Case(
        id=case_id,
        company_id=company_id,
        case_number=f"CASE-{case_id.hex[:8].upper()}",
        case_type=bulk.case_type,
        submitted_by_user_id=bulk.uploaded_by_user_id,
        status=CaseStatus.submitted,
        bulk_upload_id=bulk.id,
        reference_label=row.folder[:255],
    )
    db.add(new_case)
    db.flush()
    record_event(
        db,
        "case_created",
        case_id=case_id,
        actor_user_id=bulk.uploaded_by_user_id,
        event_data={
            "case_type": bulk.case_type.value,
            "bulk_upload_id": str(bulk.id),
            "reference_label": new_case.reference_label,
        },
    )
    record_case_created(db, company_id)
    document_ids: list[uuid.UUID] = []
    for a, original in zip(accepted, stored):
        document = record_document(
            db,
            company_id=company_id,
            case_id=case_id,
            uploaded_by_user_id=bulk.uploaded_by_user_id,
            filename=a.item["name"],
            content_type=a.content_type,
            stored=original,
            audit_extra={"bulk_upload_id": str(bulk.id)},
        )
        a.item.update(status="accepted", document_id=str(document.id))
        document_ids.append(document.id)
    row.status = "created"
    row.case_id = case_id
    row.case_number = new_case.case_number
    row.files = entry["files"]
    flag_modified(row, "files")
    _tally(bulk, row, before)
    db.commit()

    # Queued now, before the next case is even extracted: this case's
    # documents can be processed (and finish) while the rest of the zip is
    # still being ingested.
    for document_id in document_ids:
        enqueue(document_id, company_id)
