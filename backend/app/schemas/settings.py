"""Pydantic schemas for the admin Settings API (app/api/settings.py)."""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.models.issuer_registry import IssuerType
from app.models.user import UserRole
from app.services.risk_rule_catalog import CATEGORIES, build_condition

# ------------------------------------------------------------------ issuers


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


class IssuerResponse(BaseModel):
    id: uuid.UUID
    name: str
    name_arabic: str | None
    tax_id: str | None
    type: IssuerType
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class IssuerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="English/Latin issuer name.")
    name_arabic: str | None = Field(
        default=None, max_length=255, description="Arabic-script name, matched separately during issuer verification."
    )
    tax_id: str | None = Field(default=None, max_length=64, description="Tax/VAT registration number, if known.")
    type: IssuerType = Field(default=IssuerType.other, description="Kind of issuer (vendor, school, ...).")

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name is required")
        return v

    _clean_optional = field_validator("name_arabic", "tax_id")(_blank_to_none)


class IssuerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    name_arabic: str | None = Field(default=None, max_length=255)
    tax_id: str | None = Field(default=None, max_length=64)
    type: IssuerType | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be blank")
        return v

    _clean_optional = field_validator("name_arabic", "tax_id")(_blank_to_none)


# --------------------------------------------------------------- risk rules

Severity = Literal["low", "medium", "high"]


class RiskRuleResponse(BaseModel):
    """One (current or historical) version of a rule."""

    id: uuid.UUID
    rule_id: str
    category: str
    check_type: str
    weight: float
    severity: str
    reason_template: str
    is_active: bool
    version: int
    effective_from: datetime
    updated_by_name: str | None
    change_note: str | None


class RiskRuleUpdate(BaseModel):
    """An edit. Never mutates the existing row: the API inserts version+1."""

    weight: float | None = Field(
        default=None, ge=-100, le=100, description="Points added to the case score when the rule fires."
    )
    severity: Severity | None = Field(default=None, description="Severity shown beside the rule's reason.")
    is_active: bool | None = Field(default=None, description="Inactive rules are evaluated but never fire.")
    change_note: str | None = Field(default=None, max_length=1024, description="Why the change was made.")

    @model_validator(mode="after")
    def _something_changed(self) -> "RiskRuleUpdate":
        if self.weight is None and self.severity is None and self.is_active is None:
            raise ValueError("Provide at least one of weight, severity or is_active.")
        return self


class RiskRuleCreate(BaseModel):
    """A brand-new rule (version 1). The admin picks a rule TYPE and its
    parameters from the catalog (GET /settings/risk-rule-options); the API
    builds and validates the engine `condition` from them — nobody writes
    JSON, and a choice the engine can't evaluate is rejected."""

    rule_id: str = Field(
        ..., min_length=3, max_length=64,
        description="Stable key, lower-case with a dot: e.g. custom.editing_tool_seen",
    )
    category: Literal["forensics", "consistency", "verification", "duplication"]
    match: Literal["finding", "check_result", "sub_check", "cross_document", "signature_match"]
    check_type: str | None = None
    finding: str | None = None
    severity_in: list[Severity] | None = None
    sub_check: str | None = None
    field_name: str | None = None
    signature_result: str | None = None
    weight: float = Field(..., ge=-100, le=100)
    severity: Severity
    reason_template: str = Field(..., min_length=3, max_length=1024)
    is_active: bool = True
    change_note: str | None = Field(default=None, max_length=1024)

    @field_validator("rule_id")
    @classmethod
    def _rule_id_shape(cls, v: str) -> str:
        v = v.strip().lower()
        import re

        if not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", v):
            raise ValueError("Use lower-case words joined by _ with one dot, e.g. custom.editing_tool_seen")
        return v

    @field_validator("reason_template")
    @classmethod
    def _reason_not_blank(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("Write the reason a reviewer will read when this rule fires.")
        return v

    @model_validator(mode="after")
    def _condition_is_evaluable(self) -> "RiskRuleCreate":
        assert self.category in CATEGORIES
        build_condition(
            self.match, check_type=self.check_type, finding=self.finding, severity_in=self.severity_in,
            sub_check=self.sub_check, field_name=self.field_name, signature_result=self.signature_result,
        )
        return self


class RiskThresholds(BaseModel):
    medium_threshold: int = Field(..., ge=1, le=100, description="Score at or above which a case is `medium` risk.")
    high_threshold: int = Field(
        ..., ge=2, le=100, description="Score at or above which a case is `high` risk. Must exceed the medium threshold."
    )
    metadata_score_cap: int | None = Field(
        None, ge=1, le=100,
        description=(
            "Most points the metadata rules (`metadata.*`) may add together — one edit leaves several metadata "
            "traces. 100 = no cap. Omitted: unchanged."
        ),
    )

    @model_validator(mode="after")
    def _ordered(self) -> "RiskThresholds":
        if self.medium_threshold >= self.high_threshold:
            raise ValueError("The medium threshold must be lower than the high threshold.")
        return self


class RiskThresholdsResponse(RiskThresholds):
    metadata_score_cap: int
    updated_at: datetime
    updated_by_name: str | None


# -------------------------------------------------------------------- users


class UserAdminResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str | None
    role: UserRole
    is_active: bool
    created_at: datetime
    company_id: uuid.UUID | None = Field(description="Null for platform admins.")
    company_name: str | None

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    """An account created by an administrator (there is no self-service
    sign-up). The password is set here and only ever stored hashed."""

    email: EmailStr = Field(description="Login email. Unique regardless of letter case.")
    full_name: str | None = Field(default=None, max_length=255, description="Display name (optional).")
    role: UserRole = Field(
        default=UserRole.user, description="`user`, `reviewer_l1`, `reviewer_l2` or `platform_admin`."
    )
    company_id: uuid.UUID | None = Field(
        default=None, description="Required for company roles; must be empty for `platform_admin`."
    )
    password: str = Field(..., min_length=8, max_length=128, description="Initial password (8-128 chars); stored hashed.")

    _clean_name = field_validator("full_name")(_blank_to_none)


class UserAdminUpdate(BaseModel):
    role: UserRole | None = Field(default=None, description="New role. Cannot be changed on your own account.")
    company_id: uuid.UUID | None = Field(
        default=None, description="Move the user to another company (company roles only)."
    )
    is_active: bool | None = Field(default=None, description="False deactivates the account. Not allowed on yourself.")

    @model_validator(mode="after")
    def _something_changed(self) -> "UserAdminUpdate":
        if self.role is None and self.is_active is None and "company_id" not in self.model_fields_set:
            raise ValueError("Provide at least one of role, company_id or is_active.")
        return self


class UserPasswordReset(BaseModel):
    password: str = Field(..., min_length=8, max_length=128, description="New password (8-128 chars); stored hashed.")


# ----------------------------------------------------------------- audit log


class AuditEventResponse(BaseModel):
    id: uuid.UUID
    created_at: datetime
    event_type: str
    actor_name: str | None
    actor_email: str | None
    case_id: uuid.UUID | None
    case_number: str | None
    document_id: uuid.UUID | None
    document_filename: str | None
    event_data: dict | None


class AuditPage(BaseModel):
    items: list[AuditEventResponse]
    total: int
