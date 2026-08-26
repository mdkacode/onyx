"""Format-agnostic document model shared by every generation renderer.

These shapes describe *a document*, not a rendering of one: the same
`DocumentSpec` is handed to the PDF, DOCX, CSV, and XLSX renderers and each
interprets as much of it as its format can express.
"""

from dataclasses import dataclass
from dataclasses import field

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


class DocumentSpec(BaseModel):
    """Everything a renderer needs, independent of output format."""

    title: str
    subtitle: str | None = None
    sections: list[Section] = Field(default_factory=list)
    brand: BrandConfig = Field(default_factory=BrandConfig)
    metadata: DocMetadata | None = None
    # Prose layouts (PDF/DOCX) only; tabular renderers ignore these.
    template: str = "report"
    include_toc: bool = True
    page_size: str = "A4"

    def tables(self) -> list[tuple[str, TableData]]:
        """Every table in the document, paired with its section heading."""
        return [(s.heading, s.table) for s in self.sections if s.table is not None]


@dataclass
class RenderedDocument:
    """Bytes plus the metadata the caller needs to store and describe them.

    `unit_count`/`unit_label` exist because "how big is it" has no single
    answer across formats -- pages for PDF/DOCX, rows for CSV, sheets for
    XLSX. Callers surface these to the user, so each renderer names its own.
    """

    content: bytes
    mime_type: str
    extension: str
    unit_count: int
    unit_label: str
    # Renderer-side caveats worth telling the user about (e.g. "CSV kept only
    # the first of 3 tables"). Surfaced back to the model so it can mention
    # them rather than silently returning a lossy file.
    notes: list[str] = field(default_factory=list)

    @property
    def size_bytes(self) -> int:
        return len(self.content)


class FinalDocumentGenerationResponse(BaseModel):
    """What the tool hands back to the LLM after a successful generation."""

    file_id: str
    file_url: str
    title: str
    format: str
    # Pages, rows, or sheets depending on format -- `unit_label` names which.
    unit_count: int
    unit_label: str
    size_bytes: int
    notes: list[str] = Field(default_factory=list)
