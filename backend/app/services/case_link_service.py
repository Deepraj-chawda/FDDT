"""
Cross-case links — a REVIEWER NOTE, never scored: other cases of the same
company that share something with this one, so a reviewer looking at one
document from a family/office sees the others.

A pair of cases is linked by any of:
  - the same scanning device: the PDF Creator together with the first 12
    characters of its Title, when those are a device serial (12 hex
    characters, as office MFPs write: "00206BE9FB63" + "PPS - SFO2-A-MFP01").
    A plain title ("Invoice") is not a device signature and never links.
  - the same parent / guardian: a parent/guardian/father name on one
    document matching one on the other, or the father's name carried in a
    child's name (Arabic naming: "HAMDA AHMED SALEM ALKINDI" is the daughter
    of "AHMED SALEM (ABDULLA) ALKINDI"). Two names match when they share
    their first and last names and the shorter one's names all appear, in
    order, in the longer (a middle name may be left out); at least three
    names are needed, so common two-part names never link.
  - the same family number or student ID (compared within the same kind of ID).

Shared office scanners are common, so a link is information only: nothing
here feeds the risk score.

Reads every other document of the company (newest MAX_DOCUMENTS); a case
with no extractable signals has no links.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType

MAX_DOCUMENTS = 5000

_DEVICE_SERIAL_RE = re.compile(r"^[0-9A-F]{12}")
_PARENT_FIELD_RE = re.compile(r"parent|guardian|father|sponsor", re.IGNORECASE)
_CHILD_FIELD_RE = re.compile(r"student_name|child_name|pupil_name|students_name", re.IGNORECASE)
_ID_FIELDS = {
    "family number": re.compile(r"family_(?:number|no|id)", re.IGNORECASE),
    "student ID": re.compile(r"student_(?:id|number|no)", re.IGNORECASE),
}
_HONORIFICS = {"MR", "MRS", "MS", "MISS", "DR", "SHEIKH", "SH", "ENG", "PROF", "SAYED", "SAYYID"}
_MIN_NAME_TOKENS = 3


@dataclass
class LinkedCase:
    case_id: uuid.UUID
    case_number: str
    reasons: list[str] = field(default_factory=list)


def _name_tokens(value: str | None) -> list[str]:
    tokens = re.sub(r"[^A-Za-z\s]", " ", value or "").upper().split()
    return [t for t in tokens if t not in _HONORIFICS]


def _names_match(a: list[str], b: list[str]) -> bool:
    short, long = sorted((a, b), key=len)
    if len(short) < _MIN_NAME_TOKENS or short[0] != long[0] or short[-1] != long[-1]:
        return False
    it = iter(long)
    return all(token in it for token in short)


@dataclass
class _Signals:
    devices: set[tuple[str, str]] = field(default_factory=set)
    guardians: list[tuple[list[str], str]] = field(default_factory=list)  # (tokens, as printed)
    ids: set[tuple[str, str]] = field(default_factory=set)


def _device(info: dict[str, Any] | None) -> tuple[str, str] | None:
    if not isinstance(info, dict):
        return None
    creator = str(info.get("/Creator") or info.get("Creator") or "").strip()
    title = str(info.get("/Title") or info.get("Title") or "").strip().upper()
    if creator and _DEVICE_SERIAL_RE.match(title):
        return title[:12], creator
    return None


def _metadata_info(check_result: Any) -> dict[str, Any] | None:
    details = check_result.get("details") if isinstance(check_result, dict) else None
    for finding in details if isinstance(details, list) else []:
        if isinstance(finding, dict) and finding.get("finding") == "info_dictionary":
            return finding.get("data")
    return None


def _collect(signals: _Signals, extracted: dict[str, Any] | None, metadata: Any) -> None:
    for info in ((extracted or {}).get("pdf_info"), _metadata_info(metadata)):
        if (device := _device(info)) is not None:
            signals.devices.add(device)
    for f in (extracted or {}).get("additional_fields") or []:
        if not isinstance(f, dict) or not f.get("value"):
            continue
        name, value = f.get("field_name") or "", str(f["value"])
        if _PARENT_FIELD_RE.search(name):
            signals.guardians.insert(0, (_name_tokens(value), value))  # printed names first
        elif _CHILD_FIELD_RE.search(name):
            tokens = _name_tokens(value)
            if len(tokens) > _MIN_NAME_TOKENS:  # child's given name + father's name
                signals.guardians.append((tokens[1:], f"{' '.join(tokens[1:]).title()} (father's name in {value})"))
        for kind, pattern in _ID_FIELDS.items():
            if pattern.search(name):
                signals.ids.add((kind, re.sub(r"\s", "", value).upper()))


def find_linked_cases(db: Session, company_id: uuid.UUID, case_id: uuid.UUID) -> list[LinkedCase]:
    rows = db.execute(
        select(Document.id, Document.case_id, Document.extracted_fields, Case.case_number)
        .join(Case, Case.id == Document.case_id)
        .where(Document.company_id == company_id)
        .order_by(Document.created_at.desc())
        .limit(MAX_DOCUMENTS)
    ).all()
    metadata = dict(
        db.execute(
            select(DocumentCheck.document_id, DocumentCheck.result).where(
                DocumentCheck.company_id == company_id,
                DocumentCheck.check_type == DocumentCheckType.metadata_forensics,
                DocumentCheck.status == DocumentCheckStatus.completed,
                DocumentCheck.document_id.in_([r.id for r in rows]),
            )
        ).all()
    )
    mine = _Signals()
    others: dict[uuid.UUID, tuple[str, _Signals]] = {}
    for doc_id, doc_case, extracted, number in rows:
        target = mine if doc_case == case_id else others.setdefault(doc_case, (number, _Signals()))[1]
        _collect(target, extracted, metadata.get(doc_id))

    links: list[LinkedCase] = []
    for other_id, (number, theirs) in others.items():
        reasons: list[str] = []
        for serial, creator in sorted(mine.devices & theirs.devices):
            reasons.append(f"same scanning device ({creator}, serial {serial})")
        for tokens, printed in mine.guardians:
            match = next((p for t, p in theirs.guardians if _names_match(tokens, t)), None)
            if match is not None:
                reasons.append(f"same parent/guardian ('{printed}' / '{match}')")
                break
        for kind, value in sorted(mine.ids & theirs.ids):
            reasons.append(f"same {kind} ({value})")
        if reasons:
            links.append(LinkedCase(other_id, number, reasons))
    return sorted(links, key=lambda link: link.case_number)
