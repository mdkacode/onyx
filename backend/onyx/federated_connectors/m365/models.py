from typing import Optional

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator


class M365Credentials(BaseModel):
    """Microsoft 365 federated connector credentials."""

    client_id: str = Field(..., description="Azure AD application (client) ID")
    client_secret: str = Field(..., description="Azure AD application client secret")
    tenant_id: str = Field(..., description="Azure AD tenant (directory) ID")

    @field_validator("client_id")
    @classmethod
    def validate_client_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Client ID cannot be empty")
        return v.strip()

    @field_validator("client_secret")
    @classmethod
    def validate_client_secret(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Client secret cannot be empty")
        return v.strip()

    @field_validator("tenant_id")
    @classmethod
    def validate_tenant_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Tenant ID cannot be empty")
        return v.strip()


class M365Config(BaseModel):
    """Microsoft 365 federated connector configuration."""

    search_scope: str = Field(
        default="all",
        description="Scope of search: 'all', 'onedrive_only', or 'sharepoint_only'",
    )
    file_types: Optional[str] = Field(
        default=None,
        description="Comma-separated list of file extensions to filter (e.g. 'docx,pdf,xlsx')",
    )
    max_results: int = Field(
        default=25,
        description="Maximum number of search results to return",
    )

    @field_validator("search_scope")
    @classmethod
    def validate_search_scope(cls, v: str) -> str:
        allowed = {"all", "onedrive_only", "sharepoint_only"}
        if v not in allowed:
            raise ValueError(f"search_scope must be one of {allowed}, got '{v}'")
        return v

    @field_validator("max_results")
    @classmethod
    def validate_max_results(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_results must be at least 1")
        if v > 100:
            raise ValueError("max_results cannot exceed 100")
        return v
