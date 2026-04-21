from pydantic import BaseModel
from pydantic import Field


class TableData(BaseModel):
    headers: list[str]
    rows: list[list[str]]


class Section(BaseModel):
    heading: str
    body: str = ""
    bullet_points: list[str] = Field(default_factory=list)
    callout: str | None = None
    table: TableData | None = None


class BrandConfig(BaseModel):
    primary_color: str = "#0052CC"
    secondary_color: str = "#172B4D"
    font_family: str = "Inter, DejaVu Sans, sans-serif"
    company_name: str | None = None
    logo_base64: str | None = None
    # Watermark text stamped diagonally across every page. None disables the
    # watermark entirely (e.g., for unauthenticated contexts where we can't
    # resolve a user identity). Typical value: "NaArNi · <user-name>".
    watermark_text: str | None = None
    # Color of the watermark text. Kept very translucent at render time so
    # any dark-ish color still prints as a "soft gray" tint. Accepts
    # #RGB / #RRGGBB hex only (validated server-side).
    watermark_color: str = "#172B4D"


class DocMetadata(BaseModel):
    author: str | None = None
    department: str | None = None
    date: str | None = None
    confidentiality: str | None = None


class FinalPdfGenerationResponse(BaseModel):
    file_id: str
    file_url: str
    title: str
    page_count: int
    size_bytes: int
