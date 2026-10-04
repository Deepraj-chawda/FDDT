"""Pydantic schemas for the auth endpoints."""
import uuid

from pydantic import BaseModel, EmailStr, Field

from app.models.user import UserRole


class LoginRequest(BaseModel):
    email: EmailStr = Field(description="Account email (matched exactly as stored).")
    password: str = Field(description="Account password.")


class TokenResponse(BaseModel):
    access_token: str = Field(description="JWT. Send as `Authorization: Bearer <token>`.")
    token_type: str = Field(default="bearer", description="Always `bearer`.")


class CurrentUserResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str | None = Field(description="Display name, if one was set.")
    role: UserRole = Field(
        description="`user` (submitter), `reviewer_l1`, `reviewer_l2` (company roles) or `platform_admin`."
    )
    role_label: str = Field(description='Display label, e.g. "Reviewer L2".')
    is_active: bool = Field(description="Deactivated accounts are rejected on every request.")
    is_platform_admin: bool = Field(description="True for platform admins, who belong to no company.")
    company_id: uuid.UUID | None = Field(description="The user's company; null for a platform admin.")
    company_name: str | None = Field(description="Display name of the user's company.")
