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


class UploadLimitsResponse(BaseModel):
    """The calling company's upload limits, as enforced on its next upload."""

    max_file_size_mb: int = Field(description="Largest document, in MB (1 MB = 1,048,576 bytes).")
    max_zip_size_mb: int = Field(description="Largest bulk-upload zip, in MB.")
    max_file_size_bytes: int
    max_zip_size_bytes: int


class ChangePasswordRequest(BaseModel):
    """Change your own password. The current password is required; a
    forgotten password is reset by a platform admin instead (there is no
    self-service reset)."""

    current_password: str = Field(description="Your current password.")
    new_password: str = Field(
        ..., min_length=8, max_length=128, description="New password (8-128 chars); stored hashed."
    )
