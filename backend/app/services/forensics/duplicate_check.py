"""
Duplicate/near-duplicate detection (SPECIFICATION.md section 3.2). Computes a
perceptual hash (imagehash pHash, 64-bit/hash_size=8 default) for every
rendered page of a newly uploaded PDF and compares each against every
other document's previously stored page hashes (app/models/
document_page_hash.py). A small Hamming distance between two page hashes
means the two pages look near-identical, regardless of file-level
differences (re-saving, re-compressing, or minor metadata edits) that
would defeat a simple byte/SHA-256 comparison (already covered
separately by `documents.file_hash`, which only catches byte-for-byte
identical files).

Deliberately conservative (see settings.duplicate_hash_hamming_threshold):
this is meant to catch near-identical resubmissions/copies — e.g. the
same quotation re-uploaded under a slightly different vendor name — not
merely similar-looking documents that happen to share a template. Like
app/services/issuer_service.py, this needs a DB session directly (to
read prior documents' stored hashes), so it isn't a pure function over
just the rendered pages the way ela.py/copy_move.py are.

Tenancy: comparison is confined to the uploading company's own documents. A
match against another company's document would both leak that company's
filenames/case numbers into this company's findings and judge one client's
documents by another's, so cross-company duplicates are deliberately out of
scope (each company's history is its own reference set).

Like the other forensics checks, this only reads the PDF's own rendered
pages — no OCR/classification needed — so it runs independently/in
parallel, enqueued straight from the upload endpoint (see
app/tasks/duplicate_check_task.py).
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

import cv2
import imagehash
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.case import Case
from app.models.document import Document
from app.models.document_page_hash import DocumentPageHash
from app.models.user import User
from app.services.forensics.pdf_render import RenderedPage


@dataclass
class PageMatch:
    page: int
    matched_document_id: uuid.UUID
    matched_page: int
    distance: int
    # Captured at check time (not resolved later via a join at read time)
    # so the finding stays self-contained and readable on its own even if
    # the matched document later moves case, gets renamed, or (per
    # SPECIFICATION.md's audit/evidence conventions) the reviewer looking at
    # THIS finding has no other reason to have that other case loaded —
    # duplicate matches are deliberately cross-case, so the matched
    # document is often in a case the current view knows nothing about.
    matched_document_filename: str
    matched_case_id: uuid.UUID
    matched_case_number: str
    # Whether the matched document is the very same file (identical SHA-256)
    # rather than only a perceptually similar page, and who submitted it —
    # so a reviewer can tell a re-upload of the same file (often their own
    # test upload) from a different file that looks the same.
    identical_file: bool = False
    matched_submitter: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # Shaped as a Finding dict — the same {finding, severity,
        # description, page, data} shape app/services/forensics/ela.py
        # and copy_move.py use — so the existing generic findings-list UI
        # (frontend/src/components/case/DocumentChecksPanel.tsx) renders
        # this with no frontend changes, same as those checks. `page` and
        # `matched_page` are 1-based (see RenderedPage.page_number).
        # Match details live under `data` (not top-level) per that same
        # convention, satisfying "store which prior document(s) it
        # matched against, and the distance" without inventing a new
        # details shape.
        submitter = f", submitted by {self.matched_submitter}" if self.matched_submitter else ""
        if self.identical_file:
            what = (
                f"Page {self.page} matches page {self.matched_page} of \"{self.matched_document_filename}\" in case "
                f"{self.matched_case_number}{submitter}, and the two files are byte-for-byte identical "
                "(same SHA-256) — the exact same file re-uploaded."
            )
        else:
            what = (
                f"Page {self.page} is a near-identical match (perceptual hash "
                f"Hamming distance {self.distance}) to page {self.matched_page} "
                f"of \"{self.matched_document_filename}\" in case "
                f"{self.matched_case_number}{submitter} — a possible duplicate or "
                "near-duplicate resubmission. The files themselves differ (different SHA-256): "
                "only the page images are perceptually similar."
            )
        return {
            "finding": "near_duplicate_page",
            # High, not medium: unlike ELA's recompression-error signal
            # (a probabilistic hint), the default threshold here is
            # deliberately conservative (settings.duplicate_hash_hamming_
            # threshold) — a match that clears it is a strong signal, not
            # a borderline one.
            "severity": "high",
            "description": what,
            "page": self.page,
            "data": {
                "matched_document_id": str(self.matched_document_id),
                "matched_document_filename": self.matched_document_filename,
                "matched_case_id": str(self.matched_case_id),
                "matched_case_number": self.matched_case_number,
                "matched_page": self.matched_page,
                "distance": self.distance,
                "identical_file": self.identical_file,
                "matched_submitter": self.matched_submitter,
            },
        }


def compute_page_hashes(pages: list[RenderedPage]) -> list[str]:
    """One hex-string pHash per page, in page order (1:1 with `pages`)."""
    hashes = []
    for page in pages:
        rgb = cv2.cvtColor(page.image, cv2.COLOR_BGR2RGB)
        hashes.append(str(imagehash.phash(Image.fromarray(rgb))))
    return hashes


def find_matches(
    db: Session,
    *,
    company_id: uuid.UUID,
    document_id: uuid.UUID,
    page_hashes: list[str],
    threshold: int | None = None,
) -> list[PageMatch]:
    """For each of this document's page hashes, find the closest prior
    page hash (from any OTHER document of the same company already in
    `document_page_hashes`)
    within `threshold` Hamming distance. One match per new page at most
    (its single closest prior match) — a page either is or isn't a
    near-duplicate of something specific; it doesn't need every prior
    page it happens to also be somewhat close to."""
    effective_threshold = (
        threshold if threshold is not None else settings.duplicate_hash_hamming_threshold
    )

    # Joined with Document/Case up front (not resolved per-match after the
    # fact) — see PageMatch's docstring for why the finding needs this
    # captured now rather than via a read-time lookup.
    own_hash = db.execute(select(Document.file_hash).where(Document.id == document_id)).scalar_one_or_none()
    prior_rows = db.execute(
        select(
            DocumentPageHash, Document.original_filename, Document.case_id, Case.case_number,
            Document.file_hash, User.full_name, User.email,
        )
        .join(Document, Document.id == DocumentPageHash.document_id)
        .join(Case, Case.id == Document.case_id)
        .outerjoin(User, User.id == Case.submitted_by_user_id)
        .where(
            DocumentPageHash.company_id == company_id,
            Document.company_id == company_id,
            DocumentPageHash.document_id != document_id,
        )
    ).all()
    if not prior_rows:
        return []

    matches: list[PageMatch] = []
    for page_number, hex_hash in enumerate(page_hashes, start=1):
        this_hash = imagehash.hex_to_hash(hex_hash)
        best: PageMatch | None = None
        for hash_row, filename, case_id, case_number, file_hash, submitter_name, submitter_email in prior_rows:
            # imagehash's `-` returns numpy.int64 (not JSON-serializable
            # into the result jsonb column) — cast to a plain int here,
            # the one place the value originates.
            distance = int(this_hash - imagehash.hex_to_hash(hash_row.phash))
            if distance <= effective_threshold and (best is None or distance < best.distance):
                best = PageMatch(
                    page=page_number,
                    matched_document_id=hash_row.document_id,
                    matched_page=hash_row.page_number,
                    distance=distance,
                    matched_document_filename=filename,
                    matched_case_id=case_id,
                    matched_case_number=case_number,
                    identical_file=bool(own_hash) and file_hash == own_hash,
                    matched_submitter=submitter_name or submitter_email,
                )
        if best is not None:
            matches.append(best)
    return matches


def store_page_hashes(
    db: Session, *, company_id: uuid.UUID, document_id: uuid.UUID, page_hashes: list[str]
) -> None:
    """Persists this document's own page hashes so later uploads can be
    compared against it. Called after `find_matches` (not before) so a
    document never matches against its own just-inserted rows. Flushes
    (not commits — the caller owns the transaction, same convention as
    app/api/documents.py's document/audit_log flush) so a subsequent
    `find_matches` call against the same session/transaction — e.g. a
    second document uploaded before this one's task has committed — sees
    these rows rather than missing them under the session's autoflush
    setting.

    Idempotent: one row per (document, page) — a retried task (Celery
    redelivery under task_acks_late) leaves the existing row instead of
    adding a duplicate. DO NOTHING rather than DO UPDATE: a page's pHash is
    a pure function of the immutable original, so the existing row is
    already right, and the table stays insert-only — the app role is
    granted only SELECT + INSERT on it, and ON CONFLICT DO UPDATE would
    need UPDATE (it failed with "permission denied" under that role)."""
    if not page_hashes:
        return
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    from app.models.base import utcnow

    now = utcnow()
    table = DocumentPageHash.__table__
    stmt = insert(table).values(
        [
            {
                "id": uuid.uuid4(),
                "company_id": company_id,
                "document_id": document_id,
                "page_number": page_number,
                "phash": hex_hash,
                "created_at": now,
                "updated_at": now,
            }
            for page_number, hex_hash in enumerate(page_hashes, start=1)
        ]
    )
    db.execute(
        stmt.on_conflict_do_nothing(index_elements=[table.c.document_id, table.c.page_number])
    )
    db.flush()


def run_duplicate_check(
    db: Session,
    *,
    company_id: uuid.UUID,
    document_id: uuid.UUID,
    pages: list[RenderedPage],
    threshold: int | None = None,
) -> dict[str, Any]:
    """Returns {"result": "pass"|"flag", "details": [...]} ready to store
    directly in a document_checks.result jsonb column, same shape as
    app/services/forensics/ela.py/copy_move.py. Also persists this
    document's own page hashes as a side effect, regardless of outcome,
    so it's available for future documents to match against — callers
    don't need a separate step for that."""
    page_hashes = compute_page_hashes(pages)
    matches = find_matches(
        db, company_id=company_id, document_id=document_id, page_hashes=page_hashes, threshold=threshold
    )
    store_page_hashes(db, company_id=company_id, document_id=document_id, page_hashes=page_hashes)
    return {"result": "flag" if matches else "pass", "details": [m.to_dict() for m in matches]}


# --- field-aware: resubmission, or the same template with other values ------------

# Additional fields that name the student / customer the document is for.
_PERSON_FIELD_RE = re.compile(r"student|pupil|child|learner|bill_to|billed_to|customer|parent", re.IGNORECASE)
_PERSON_MATCH = 90.0
# A resubmission needs at least this many fields compared, all equal.
_MIN_COMPARED = 2


def _fields_on_page(extracted: dict[str, Any] | None, page: int) -> dict[str, Any]:
    """The core/additional fields of the invoice printed on `page` (a
    multi-invoice file), else the document's own."""
    from app.services.multi_invoice import invoice_on_page

    return invoice_on_page(extracted, page) or (extracted or {})


def _value(fields: dict[str, Any], name: str) -> Any:
    return ((fields.get("core_fields") or {}).get(name) or {}).get("value")


def _person(fields: dict[str, Any]) -> str | None:
    for item in fields.get("additional_fields") or []:
        if isinstance(item, dict) and _PERSON_FIELD_RE.search(str(item.get("field_name") or "")) and item.get("value"):
            return str(item["value"])
    return None


def compare_fields(mine: dict[str, Any], theirs: dict[str, Any]) -> list[dict[str, Any]]:
    """[{field, mine, theirs, equal}] for the identifying fields both sides have."""
    from rapidfuzz import fuzz

    rows = []
    a, b = _value(mine, "reference_number"), _value(theirs, "reference_number")
    if a and b:
        norm = lambda v: re.sub(r"[^0-9A-Z]", "", str(v).upper())  # noqa: E731
        rows.append({"field": "invoice number", "mine": a, "theirs": b, "equal": norm(a) == norm(b)})
    a, b = _value(mine, "date"), _value(theirs, "date")
    if a and b:
        rows.append({"field": "date", "mine": a, "theirs": b, "equal": str(a) == str(b)})
    a, b = _value(mine, "amount"), _value(theirs, "amount")
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        rows.append({"field": "amount", "mine": a, "theirs": b, "equal": abs(a - b) < 0.01})
    a, b = _person(mine), _person(theirs)
    if a and b:
        rows.append({"field": "student / customer", "mine": a, "theirs": b,
                     "equal": fuzz.token_set_ratio(a.casefold(), b.casefold()) >= _PERSON_MATCH})
    return rows


def _shown(value: Any) -> str:
    return f"{value:,.2f}" if isinstance(value, float) else str(value)


def refine_with_fields(
    result: dict[str, Any], mine: dict[str, Any] | None, matched: dict[str, dict[str, Any] | None]
) -> dict[str, Any]:
    """`result` with each near-duplicate page match (not the same file)
    judged on the fields: the same invoice number, date, amount and student
    as the matched document → a resubmission (stays high); any of them
    different → `same_template_page`, low: the same layout with other values,
    shown for review, not scored. `matched`: {document_id: its extracted
    fields}. A match whose fields cannot be compared yet is left as it is."""
    details = []
    changed = False
    for f in result.get("details") or []:
        data = f.get("data") if isinstance(f, dict) else None
        if f.get("finding") != "near_duplicate_page" or not isinstance(data, dict) or data.get("identical_file"):
            details.append(f)
            continue
        theirs_all = matched.get(str(data.get("matched_document_id")))
        if not mine or not theirs_all:
            details.append(f)
            continue
        rows = compare_fields(
            _fields_on_page(mine, int(f.get("page") or 1)),
            _fields_on_page(theirs_all, int(data.get("matched_page") or 1)),
        )
        if not rows:
            details.append(f)
            continue
        changed = True
        same = [r["field"] for r in rows if r["equal"]]
        differ = [r for r in rows if not r["equal"]]
        where = f"page {data.get('matched_page')} of \"{data.get('matched_document_filename')}\" in case {data.get('matched_case_number')}"
        if not differ and len(rows) >= _MIN_COMPARED:
            details.append({
                **f,
                "description": f"Page {f.get('page')} looks the same as {where} and carries the same "
                               f"{', '.join(same)}: the same document submitted again.",
                "data": {**data, "field_comparison": rows, "verdict": "resubmission"},
            })
            continue
        if not differ:
            details.append({**f, "data": {**data, "field_comparison": rows}})
            continue
        differences = "; ".join(f"{r['field']} {_shown(r['mine'])} vs {_shown(r['theirs'])}" for r in differ)
        details.append({
            **f,
            "finding": "same_template_page",
            "severity": "low",
            "description": f"Page {f.get('page')} has the same layout as {where}, but different values "
                           f"({differences}{'; same ' + ', '.join(same) if same else ''}): the same template "
                           "with other values, not the same document. Shown for review, not scored.",
            "data": {**data, "field_comparison": rows, "verdict": "same_template"},
        })
    if not changed:
        return result
    flagged = any(d.get("severity") in ("medium", "high") for d in details if isinstance(d, dict))
    return {**result, "result": "flag" if flagged else "pass", "details": details}
