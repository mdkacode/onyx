"""One tool that produces PDF, Word, CSV, or XLSX from structured content.

Deliberately a single tool with a `format` argument rather than one tool per
format. Three near-identical tool descriptions force the model to discriminate
on wording alone, and "make me a report" gives it no signal; a format enum
turns the choice into an argument filled straight from the user's own words.

NOTE ON THE CLASS NAME: `PdfGenerationTool` is historical and no longer
describes what this does. It is retained because `tool.in_code_tool_id` in the
database holds the literal string "PdfGenerationTool", and `BUILT_IN_TOOL_MAP`
is keyed on `cls.__name__` -- renaming the class would orphan the existing tool
row and detach it from every persona it is attached to. The user-visible and
LLM-visible names come from NAME/DISPLAY_NAME below and are free to differ.
"""

import io
import json
import re
from typing import Any
from typing import cast

from sqlalchemy.orm import Session
from typing_extensions import override

from onyx.chat.emitter import Emitter
from onyx.configs.constants import FileOrigin
from onyx.db.models import User
from onyx.file_store.file_store import get_default_file_store
from onyx.file_store.utils import build_frontend_file_url
from onyx.file_store.utils import build_full_frontend_file_url
from onyx.server.query_and_chat.placement import Placement
from onyx.server.query_and_chat.streaming_models import CustomToolDelta
from onyx.server.query_and_chat.streaming_models import CustomToolStart
from onyx.server.query_and_chat.streaming_models import GeneratedPdf
from onyx.server.query_and_chat.streaming_models import Packet
from onyx.server.query_and_chat.streaming_models import PdfGenerationFinal
from onyx.tools.interface import Tool
from onyx.tools.models import ToolResponse
from onyx.tools.tool_implementations.document_generation.models import BrandConfig
from onyx.tools.tool_implementations.document_generation.models import DocMetadata
from onyx.tools.tool_implementations.document_generation.models import DocumentSpec
from onyx.tools.tool_implementations.document_generation.models import (
    FinalDocumentGenerationResponse,
)
from onyx.tools.tool_implementations.document_generation.models import Section
from onyx.tools.tool_implementations.document_generation.models import TableData
from onyx.tools.tool_implementations.document_generation.renderers import (
    available_formats,
)
from onyx.tools.tool_implementations.document_generation.renderers import get_renderer
from onyx.utils.logger import setup_logger

logger = setup_logger()

DEFAULT_BRAND = BrandConfig()

# Strict hex color validator. The LLM interpolates these values directly into
# <style> blocks, so a lax validator would open a CSS injection channel
# (`}body{display:none` etc.). We only accept 3-, 4-, 6-, or 8-digit hex.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9A-Fa-f]{3,4}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")


def _safe_color(value: Any, fallback: str) -> str:
    """Return `value` if it's a syntactically valid hex color, else `fallback`.

    The brand color lands in raw CSS inside our Jinja template (e.g.
    `color: {{ brand.primary_color }}`). Jinja autoescape only protects HTML
    contexts — not CSS — so a malicious LLM response like
    `#fff;}body{display:none` would break the document if passed through
    untouched. Restricting to hex defuses that class of injection.
    """
    if isinstance(value, str) and _HEX_COLOR_RE.match(value.strip()):
        return value.strip()
    return fallback


def _derive_user_label(user: User | None) -> str | None:
    """Derive a short user label for the watermark from the authenticated user.

    Uses the local part of the email (everything before `@`) so the watermark
    stays short and non-PII-ish in shared docs. Returns None when no user is
    available — the caller then disables the watermark entirely.
    """
    if user is None or not getattr(user, "email", None):
        return None
    email = str(user.email)
    local = email.split("@", 1)[0]
    return local or None


class PdfGenerationTool(Tool[None]):
    NAME = "generate_document"
    DESCRIPTION = (
        "Generates a professional, downloadable document from structured content "
        "and returns a download link. Supported formats: "
        "pdf (polished, read-only report), "
        "docx (editable Word document), "
        "csv (single table of data), "
        "xlsx (spreadsheet, one sheet per table). "
        "Use whenever the user asks to create, generate, export, save, download, "
        "or 'send me' a document, report, brief, summary, spreadsheet, or data "
        "export — including when they ask for the current conversation or a "
        "previous answer to be turned into a file. Pick the format from the "
        "user's own words ('Word doc' -> docx, 'spreadsheet' -> xlsx, "
        "'CSV' -> csv, 'PDF' -> pdf); default to pdf when they just say "
        "'document' or 'report'. csv and xlsx require at least one section "
        "containing a table."
    )
    DISPLAY_NAME = "Document Generation"

    def __init__(
        self,
        tool_id: int,
        emitter: Emitter,
        user: User | None = None,
    ) -> None:
        super().__init__(emitter=emitter)
        self._id = tool_id
        self._user = user

    @property
    def id(self) -> int:
        return self._id

    @property
    def name(self) -> str:
        return self.NAME

    @property
    def description(self) -> str:
        return self.DESCRIPTION

    @property
    def display_name(self) -> str:
        return self.DISPLAY_NAME

    @override
    @classmethod
    def is_available(cls, db_session: Session) -> bool:  # noqa: ARG003
        """Available as long as at least one format can be rendered.

        Only PDF can be unavailable (WeasyPrint needs system libraries absent
        from some dev venvs); DOCX/CSV/XLSX are pure Python. So unlike the
        former PDF-only tool, this effectively never disables itself.
        """
        return bool(available_formats())

    def tool_definition(self) -> dict:
        formats = available_formats()
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.DESCRIPTION,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "format": {
                            "type": "string",
                            "enum": formats,
                            "description": (
                                "Output file format. pdf = polished read-only "
                                "report. docx = editable Word document. "
                                "csv = one table as plain CSV. xlsx = "
                                "spreadsheet with one sheet per table. "
                                "csv and xlsx ignore prose and require at "
                                "least one table."
                            ),
                        },
                        "title": {
                            "type": "string",
                            "description": "Document title (specific, date-stamped).",
                        },
                        "subtitle": {
                            "type": "string",
                            "description": "Optional subtitle shown under the title.",
                        },
                        "template": {
                            "type": "string",
                            "enum": ["report", "brief"],
                            "description": (
                                "Layout for pdf/docx. report = full "
                                "multi-section document with cover page and "
                                "TOC. brief = compact one-pager. Ignored for "
                                "csv/xlsx."
                            ),
                        },
                        "sections": {
                            "type": "array",
                            "description": (
                                "Ordered list of document sections. For prose "
                                "formats the first section should be an "
                                "Executive Summary and the last Next Steps or "
                                "Recommendations. For csv/xlsx only the "
                                "`table` of each section is used."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "heading": {
                                        "type": "string",
                                        "description": (
                                            "Section heading. Used as the "
                                            "sheet name in xlsx."
                                        ),
                                    },
                                    "body": {
                                        "type": "string",
                                        "description": (
                                            "Section body prose. Supports "
                                            "**bold** and `code` inline markup."
                                        ),
                                    },
                                    "bullet_points": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": (
                                            "5–10 word fragments. No trailing periods."
                                        ),
                                    },
                                    "callout": {
                                        "type": "string",
                                        "description": (
                                            "Highlighted box for key insights or "
                                            "warnings. Max 2 sentences."
                                        ),
                                    },
                                    "table": {
                                        "type": "object",
                                        "properties": {
                                            "headers": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                            "rows": {
                                                "type": "array",
                                                "items": {
                                                    "type": "array",
                                                    "items": {"type": "string"},
                                                },
                                            },
                                        },
                                        "required": ["headers", "rows"],
                                    },
                                },
                                "required": ["heading"],
                            },
                        },
                        "include_toc": {
                            "type": "boolean",
                            "description": (
                                "Include a table of contents before the sections. "
                                "Only applies to the 'report' template."
                            ),
                        },
                        "page_size": {
                            "type": "string",
                            "enum": ["A4", "Letter"],
                        },
                        "metadata": {
                            "type": "object",
                            "description": (
                                "Optional document metadata shown on the cover page."
                            ),
                            "properties": {
                                "author": {"type": "string"},
                                "department": {"type": "string"},
                                "date": {"type": "string"},
                                "confidentiality": {"type": "string"},
                            },
                        },
                        "primary_color": {
                            "type": "string",
                            "description": (
                                "Optional primary brand color for headings, "
                                "table headers, and callout accents. Hex "
                                "format only (e.g. '#0052CC'). Defaults to "
                                "the NaArNi brand blue."
                            ),
                        },
                        "secondary_color": {
                            "type": "string",
                            "description": (
                                "Optional secondary color for subheadings and "
                                "company-name accents. Hex format only."
                            ),
                        },
                        "watermark_text": {
                            "type": "string",
                            "description": (
                                "Optional label for the diagonal watermark "
                                "that appears on every page of a pdf (e.g. "
                                "'DRAFT', 'CONFIDENTIAL'). Defaults to "
                                "'NaArNi · <user>'. The watermark is always "
                                "rendered — it cannot be disabled."
                            ),
                        },
                        "watermark_color": {
                            "type": "string",
                            "description": (
                                "Optional color for the watermark text. Hex "
                                "format only (e.g. '#172B4D'). The watermark "
                                "is rendered at low opacity so dark colors "
                                "still appear as a soft professional tint."
                            ),
                        },
                    },
                    "required": ["format", "title", "sections"],
                },
            },
        }

    def emit_start(self, placement: Placement) -> None:
        self.emitter.emit(
            Packet(
                placement=placement,
                obj=CustomToolStart(
                    tool_name=self.name,
                    tool_id=self._id,
                ),
            )
        )

    def _build_brand(self, llm_kwargs: dict[str, Any]) -> BrandConfig:
        """Assemble a BrandConfig for this request.

        Priority:
          1. Prompt-supplied overrides (`primary_color`, `secondary_color`,
             `watermark_text`, `watermark_color`) — validated, then applied.
          2. Otherwise inherit defaults from `DEFAULT_BRAND`.
        """
        primary = _safe_color(
            llm_kwargs.get("primary_color"), DEFAULT_BRAND.primary_color
        )
        secondary = _safe_color(
            llm_kwargs.get("secondary_color"), DEFAULT_BRAND.secondary_color
        )
        watermark_color = _safe_color(
            llm_kwargs.get("watermark_color"), DEFAULT_BRAND.watermark_color
        )

        # Watermark is MANDATORY on every generated PDF — there is no
        # opt-out. A prompt-supplied string (e.g. "DRAFT", "CONFIDENTIAL")
        # replaces the default label; empty/whitespace falls back to
        # "NaArNi · <user>" so nothing can produce an un-watermarked doc.
        user_label = _derive_user_label(self._user)
        default_watermark = f"NaArNi · {user_label}" if user_label else "NaArNi"
        raw_watermark = llm_kwargs.get("watermark_text")
        if isinstance(raw_watermark, str) and raw_watermark.strip():
            watermark_text = raw_watermark.strip()
        else:
            watermark_text = default_watermark

        return BrandConfig(
            primary_color=primary,
            secondary_color=secondary,
            font_family=DEFAULT_BRAND.font_family,
            company_name=DEFAULT_BRAND.company_name,
            logo_base64=DEFAULT_BRAND.logo_base64,
            watermark_text=watermark_text,
            watermark_color=watermark_color,
        )

    @staticmethod
    def _parse_sections(raw: list[Any]) -> list[Section]:
        parsed: list[Section] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            heading = cast(str, item.get("heading", "")).strip()
            if not heading:
                continue
            table_raw = item.get("table")
            table: TableData | None = None
            if isinstance(table_raw, dict):
                headers = table_raw.get("headers") or []
                rows = table_raw.get("rows") or []
                if headers and isinstance(headers, list):
                    table = TableData(
                        headers=[str(h) for h in headers],
                        rows=[
                            [str(c) for c in row]
                            for row in rows
                            if isinstance(row, list)
                        ],
                    )
            parsed.append(
                Section(
                    heading=heading,
                    body=cast(str, item.get("body", "") or ""),
                    bullet_points=[
                        str(b) for b in (item.get("bullet_points") or []) if b
                    ],
                    callout=cast(str | None, item.get("callout")),
                    table=table,
                )
            )
        return parsed

    def _build_spec(self, llm_kwargs: dict[str, Any]) -> DocumentSpec:
        title = (
            cast(str, llm_kwargs.get("title", "Untitled Document")).strip()
            or "Untitled Document"
        )
        template_name = cast(str, llm_kwargs.get("template") or "report")
        if template_name not in ("report", "brief"):
            template_name = "report"
        page_size = cast(str, llm_kwargs.get("page_size") or "A4")
        if page_size not in ("A4", "Letter"):
            page_size = "A4"

        metadata_raw = llm_kwargs.get("metadata")
        metadata: DocMetadata | None = None
        if isinstance(metadata_raw, dict):
            metadata = DocMetadata(
                author=metadata_raw.get("author"),
                department=metadata_raw.get("department"),
                date=metadata_raw.get("date"),
                confidentiality=metadata_raw.get("confidentiality"),
            )

        sections = self._parse_sections(
            cast(list[Any], llm_kwargs.get("sections") or [])
        )
        if not sections:
            raise ValueError(
                "generate_document requires at least one section with a heading"
            )

        return DocumentSpec(
            title=title,
            subtitle=cast(str | None, llm_kwargs.get("subtitle")),
            sections=sections,
            brand=self._build_brand(llm_kwargs),
            metadata=metadata,
            template=template_name,
            include_toc=bool(llm_kwargs.get("include_toc", True)),
            page_size=page_size,
        )

    def run(
        self,
        placement: Placement,
        override_kwargs: None = None,  # noqa: ARG002
        **llm_kwargs: Any,
    ) -> ToolResponse:
        fmt = cast(str, llm_kwargs.get("format") or "pdf").strip().lower()
        renderer = get_renderer(fmt)
        spec = self._build_spec(llm_kwargs)

        try:
            rendered = renderer.render(spec)
        except ValueError:
            # Renderer-level validation (e.g. csv with no table). The message is
            # written for the model to act on, so let it through unwrapped.
            raise
        except Exception:
            logger.exception("Error generating %s document", fmt)
            raise

        file_store = get_default_file_store()
        # The generated file is handed to the user as a bare /chat/file/{id}
        # link, so it never lands in ChatMessage.files. Record the owner here —
        # it is the only thing authorizing the later download (see
        # _user_can_access_generated_report).
        file_metadata = {"user_id": str(self._user.id)} if self._user else None
        file_id = file_store.save_file(
            content=io.BytesIO(rendered.content),
            display_name=f"{spec.title}.{rendered.extension}",
            file_origin=FileOrigin.GENERATED_REPORT,
            file_type=rendered.mime_type,
            file_metadata=file_metadata,
        )
        file_url = build_frontend_file_url(file_id)

        # Legacy packet, kept for PDF only so existing behaviour is unchanged.
        # Nothing in web/src consumes it today — the download button comes from
        # the CustomToolDelta below — so it is not extended to new formats.
        if fmt == "pdf":
            self.emitter.emit(
                Packet(
                    placement=placement,
                    obj=PdfGenerationFinal(
                        pdf=GeneratedPdf(
                            file_id=file_id,
                            url=file_url,
                            title=spec.title,
                            page_count=rendered.unit_count,
                            size_bytes=rendered.size_bytes,
                        )
                    ),
                )
            )

        # Drives the download button in the chat timeline via CustomToolRenderer.
        self.emitter.emit(
            Packet(
                placement=placement,
                obj=CustomToolDelta(
                    tool_name=self.name,
                    tool_id=self._id,
                    response_type="file",
                    file_ids=[file_id],
                ),
            )
        )

        final_response = FinalDocumentGenerationResponse(
            file_id=file_id,
            file_url=file_url,
            title=spec.title,
            format=fmt,
            unit_count=rendered.unit_count,
            unit_label=rendered.unit_label,
            size_bytes=rendered.size_bytes,
            notes=rendered.notes,
        )

        # The chat UI renders a download button from the CustomToolDelta above,
        # but that button is not present on every surface (Slack, the widget),
        # so the model is also told to print the absolute URL as plain text.
        full_download_url = build_full_frontend_file_url(file_id)
        message = (
            f"Generated a {rendered.unit_count}-{rendered.unit_label.rstrip('s')} "
            f"{fmt.upper()} titled '{spec.title}'. "
            f"In your reply to the user, you MUST print the following full "
            f"download URL as PLAIN TEXT on its own line (NOT as a markdown "
            f"link, NOT wrapped in backticks, NOT modified in any way) so the "
            f"user can copy and paste it into their browser to download the "
            f"file:\n\n{full_download_url}"
        )
        if rendered.notes:
            # e.g. "CSV kept only the first of 3 tables" -- the user needs to
            # hear this, so it is put in front of the model rather than logged.
            message += "\n\nAlso tell the user: " + " ".join(rendered.notes)

        llm_facing_response = json.dumps(
            {
                "file_id": file_id,
                "title": spec.title,
                "format": fmt,
                rendered.unit_label: rendered.unit_count,
                "size_bytes": rendered.size_bytes,
                "download_url": full_download_url,
                "notes": rendered.notes,
                "message": message,
            }
        )

        return ToolResponse(
            rich_response=final_response,
            llm_facing_response=llm_facing_response,
        )
