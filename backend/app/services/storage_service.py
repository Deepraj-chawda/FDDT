"""
File storage abstraction.

SPECIFICATION.md section 2 names Azure Blob Storage as the file store, but
`StorageService` keeps the upload endpoint (app/api/documents.py)
decoupled from that choice: swapping in a different backend later means
adding a new subclass here, not touching the endpoint.

Original uploaded files are stored immutably — callers only ever
`upload()`; there is no update/overwrite path exposed for a document's
original bytes once it exists (SPECIFICATION.md section 4).

The `documents` container is private (confirmed: an unsigned request to
a blob URL gets Azure's `PublicAccessNotPermitted`) — the right posture
for business documents like invoices. So the case-detail endpoint can't
just hand out the URL stored in `documents.blob_storage_path`; it needs
a short-lived signed (SAS) URL instead, via `get_download_url()`.
"""
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import BinaryIO

from azure.core.exceptions import AzureError
from azure.storage.blob import (
    BlobClient,
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
)
from fastapi import HTTPException, status

from app.core.config import settings


# ---------------------------------------------------------------------------
# Company-scoped blob layout
# ---------------------------------------------------------------------------
# Every file a company's activity creates lives under its own prefix:
#
#   companies/{company_id}/cases/{case_id}/documents/{document_id}_{sha256}{ext}
#   companies/{company_id}/cases/{case_id}/reports/{report_id}.pdf
#   companies/{company_id}/cases/{case_id}/signatures/{crop_id}.png
#   companies/{company_id}/bulk-uploads/{bulk_upload_id}_{sha256}.zip
#
# so isolation also holds at the storage layer (a path never mixes companies,
# and `ensure_company_blob` refuses to sign another company's file), and a
# company's storage footprint is everything under one prefix. Files written
# before multi-tenancy keep their original paths (`{case_id}/...`,
# `reports/...`, `signatures/...`) — originals are immutable and are never
# moved; their rows carry company_id like everything else.

COMPANIES_PREFIX = "companies"


def company_blob_prefix(company_id) -> str:
    return f"{COMPANIES_PREFIX}/{company_id}/"


def case_blob_prefix(company_id, case_id) -> str:
    return f"{company_blob_prefix(company_id)}cases/{case_id}/"


def document_blob_path(company_id, case_id, document_id, file_hash: str, extension: str) -> str:
    # Both the document's own id and its content hash appear in the name, so
    # two uploads can never collide even if a filename is reused.
    return f"{case_blob_prefix(company_id, case_id)}documents/{document_id}_{file_hash}{extension}"


def report_blob_path(company_id, case_id, report_id) -> str:
    return f"{case_blob_prefix(company_id, case_id)}reports/{report_id}.pdf"


def signature_blob_path(company_id, case_id, crop_id) -> str:
    return f"{case_blob_prefix(company_id, case_id)}signatures/{crop_id}.png"


def bulk_upload_blob_path(company_id, bulk_upload_id, file_hash: str) -> str:
    """The zip as submitted, kept as the record of a bulk upload. The
    documents extracted from it are stored separately, per case, like any
    single upload."""
    return f"{company_blob_prefix(company_id)}bulk-uploads/{bulk_upload_id}_{file_hash}.zip"


def _blob_name(file_url: str) -> str:
    """The blob name inside the container, from a durable blob URL (or a bare
    blob path)."""
    from urllib.parse import unquote, urlsplit

    path = unquote(urlsplit(file_url).path).lstrip("/")
    container = settings.azure_storage_container_name
    if path.startswith(f"{container}/"):
        path = path[len(container) + 1 :]
    return path


class CrossTenantBlobError(PermissionError):
    """A company-prefixed blob was requested on behalf of a different company."""


def ensure_company_blob(file_url: str, company_id) -> None:
    """Refuse to hand out a file that lives under ANOTHER company's prefix.
    Legacy (pre-multi-tenancy) paths carry no company and are allowed — their
    database rows are already company-scoped."""
    name = _blob_name(file_url)
    if not name.startswith(f"{COMPANIES_PREFIX}/"):
        return
    if not name.startswith(company_blob_prefix(company_id)):
        raise CrossTenantBlobError("This file belongs to another company.")


class StorageConfigurationError(RuntimeError):
    """Raised when the storage backend is missing or invalid configuration."""


class StorageOperationError(RuntimeError):
    """Raised when an otherwise-configured storage backend fails an operation."""


class StorageService(ABC):
    @abstractmethod
    def upload(
        self, blob_path: str, content: bytes | BinaryIO, content_type: str | None = None
    ) -> str:
        """Upload `content` (bytes, or a readable binary file positioned at
        its start, which a bulk zip uses so it is never held in memory whole)
        to `blob_path` and return the file's durable URL."""
        raise NotImplementedError

    @abstractmethod
    def get_download_url(self, file_url: str, expires_in_minutes: int = 15) -> str:
        """Turn a stored durable URL (from `upload()`) into one a browser can
        actually fetch — the backing store may be private, so this is
        typically a short-lived signed URL, not the durable URL itself."""
        raise NotImplementedError

    @abstractmethod
    def download_bytes(self, file_url: str) -> bytes:
        """Fetch a document's original bytes directly (not a URL a
        browser would use) — for server-side processing that needs the
        actual file content, e.g. app/services/forensics/
        metadata_forensics.py reading a PDF's own embedded metadata. The
        OCR pipeline (app/services/ocr_service.py) doesn't need this: it
        hands Azure Document Intelligence a signed URL and lets that
        service fetch the bytes itself."""
        raise NotImplementedError

    def download_to(self, file_url: str, fileobj: BinaryIO) -> None:
        """Write a stored file's bytes into `fileobj`, for files too big to
        hold in memory whole (a bulk-upload zip of up to 300 MB). Backends
        override this to stream; the default buffers once."""
        fileobj.write(self.download_bytes(file_url))


class AzureBlobStorageService(StorageService):
    def __init__(self, connection_string: str | None, container_name: str):
        if not connection_string:
            raise StorageConfigurationError(
                "AZURE_STORAGE_CONNECTION_STRING is not set. Add it to backend/.env "
                "(see .env.example) to enable document uploads."
            )
        try:
            client = BlobServiceClient.from_connection_string(connection_string)
        except ValueError as exc:
            raise StorageConfigurationError(
                f"AZURE_STORAGE_CONNECTION_STRING is not a valid Azure Storage "
                f"connection string: {exc}"
            ) from exc

        self._container = client.get_container_client(container_name)

    def upload(
        self, blob_path: str, content: bytes | BinaryIO, content_type: str | None = None
    ) -> str:
        try:
            blob_client = self._container.get_blob_client(blob_path)
            blob_client.upload_blob(
                content,
                overwrite=False,  # blob_path is content-derived; a collision would
                                   # mean something is wrong, not a legitimate re-upload.
                content_settings=(
                    ContentSettings(content_type=content_type) if content_type else None
                ),
            )
            return blob_client.url
        except AzureError as exc:
            raise StorageOperationError(
                f"Failed to upload file to Azure Blob Storage: {exc}"
            ) from exc

    def get_download_url(self, file_url: str, expires_in_minutes: int = 15) -> str:
        try:
            blob_client = BlobClient.from_blob_url(
                file_url, credential=self._container.credential
            )
            sas_token = generate_blob_sas(
                account_name=blob_client.account_name,
                container_name=blob_client.container_name,
                blob_name=blob_client.blob_name,
                account_key=self._container.credential.account_key,
                permission=BlobSasPermissions(read=True),
                expiry=datetime.now(timezone.utc) + timedelta(minutes=expires_in_minutes),
            )
            return f"{blob_client.url}?{sas_token}"
        except AzureError as exc:
            raise StorageOperationError(
                f"Failed to generate a download URL: {exc}"
            ) from exc

    def download_bytes(self, file_url: str) -> bytes:
        try:
            blob_client = BlobClient.from_blob_url(
                file_url, credential=self._container.credential
            )
            return blob_client.download_blob().readall()
        except AzureError as exc:
            raise StorageOperationError(
                f"Failed to download the file's bytes: {exc}"
            ) from exc

    def download_to(self, file_url: str, fileobj: BinaryIO) -> None:
        try:
            blob_client = BlobClient.from_blob_url(
                file_url, credential=self._container.credential
            )
            blob_client.download_blob(max_concurrency=4).readinto(fileobj)
        except AzureError as exc:
            raise StorageOperationError(
                f"Failed to download the file: {exc}"
            ) from exc


@lru_cache
def _build_storage_service() -> StorageService:
    return AzureBlobStorageService(
        connection_string=settings.azure_storage_connection_string,
        container_name=settings.azure_storage_container_name,
    )


def get_storage_service() -> StorageService:
    """FastAPI dependency. Turns a misconfigured/invalid connection string into
    a clean 503 with an explanatory message instead of an unhandled crash."""
    try:
        return _build_storage_service()
    except StorageConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


def get_storage_service_for_task() -> StorageService:
    """Plain accessor for use outside a FastAPI request (the Celery task in
    app/tasks/document_processing.py has no DI context to resolve
    `get_storage_service` through). Raises StorageConfigurationError
    directly rather than an HTTPException; callers decide how to handle it."""
    return _build_storage_service()
