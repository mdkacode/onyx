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
