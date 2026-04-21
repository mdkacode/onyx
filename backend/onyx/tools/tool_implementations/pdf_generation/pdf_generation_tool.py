import html
import io
import json
import re
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import cast

from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import select_autoescape
from sqlalchemy.orm import Session
from typing_extensions import override

from onyx.chat.emitter import Emitter
from onyx.configs.constants import FileOrigin
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
from onyx.tools.tool_implementations.pdf_generation.models import BrandConfig
from onyx.tools.tool_implementations.pdf_generation.models import DocMetadata
from onyx.tools.tool_implementations.pdf_generation.models import (
    FinalPdfGenerationResponse,
)
from onyx.tools.tool_implementations.pdf_generation.models import Section
from onyx.tools.tool_implementations.pdf_generation.models import TableData
from onyx.utils.logger import setup_logger

logger = setup_logger()


TEMPLATES_DIR = Path(__file__).parent / "templates"

DEFAULT_BRAND = BrandConfig()

# Matches `code` spans (non-greedy, single backticks, non-empty).
_INLINE_CODE_RE = re.compile(r"`([^`\n]+?)`")
# Matches **bold** spans.
_INLINE_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")


def _inline_format(text: str) -> str:
    """Escape HTML, then re-apply inline **bold** and `code` markers.

    The Jinja template marks the output `| safe`, so we must escape raw HTML
    ourselves to prevent injection from LLM-supplied content.
    """
    if not text:
        return ""
    escaped = html.escape(text)
    escaped = _INLINE_CODE_RE.sub(r"<code>\1</code>", escaped)
    escaped = _INLINE_BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    return escaped


_jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "htm", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_jinja_env.filters["inline_format"] = _inline_format


class PdfGenerationTool(Tool[None]):
    NAME = "generate_pdf"
    DESCRIPTION = (
        "Generates a professional, downloadable PDF document from structured content. "
        "Use when the user explicitly asks to create, export, save, or download a PDF "
        "report, document, brief, or summary."
    )
    DISPLAY_NAME = "PDF Generation"

    def __init__(
        self,
        tool_id: int,
        emitter: Emitter,
    ) -> None:
        super().__init__(emitter=emitter)
        self._id = tool_id

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
    def is_available(cls, db_session: Session) -> bool:
        """Available iff the WeasyPrint Python package can be imported.

        The system libraries (Cairo, Pango, GDK-pixbuf) are present in the
        backend Docker image but may be missing in local dev venvs.
        """
        try:
            import weasyprint  # noqa: F401
        except (ImportError, OSError) as exc:
            logger.warning(
                "PdfGenerationTool unavailable: weasyprint cannot be imported (%s)",
                exc,
            )
            return False
        return True

    def tool_definition(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.DESCRIPTION,
                "parameters": {
                    "type": "object",
                    "properties": {
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
                                "report = full multi-section document with cover page "
                                "and TOC. brief = compact one-pager."
                            ),
                        },
                        "sections": {
                            "type": "array",
                            "description": (
                                "Ordered list of document sections. First section "
                                "should be an Executive Summary, last should be "
                                "Next Steps or Recommendations."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "heading": {"type": "string"},
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
                                            "5–10 word fragments. No trailing "
                                            "periods."
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
                    },
                    "required": ["title", "sections"],
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

    @staticmethod
    def _render_html(
        *,
        template_name: str,
        title: str,
        subtitle: str | None,
        sections: list[Section],
        brand: BrandConfig,
        metadata: DocMetadata | None,
        include_toc: bool,
        page_size: str,
    ) -> str:
        template = _jinja_env.get_template(f"{template_name}.html.j2")
        return template.render(
            title=title,
            subtitle=subtitle,
            sections=sections,
            brand=brand,
            metadata=metadata,
            include_toc=include_toc,
            page_size=page_size,
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        )

    @staticmethod
    def _render_pdf(html_content: str) -> tuple[bytes, int]:
        """Render HTML to PDF bytes using WeasyPrint.

        Lazy import so the tool module can be imported on machines without the
        WeasyPrint system libraries installed (e.g. local Mac dev without brew).
        """
        from weasyprint import HTML  # lazy: requires system libs (Cairo/Pango)

        document = HTML(string=html_content).render()
        pdf_bytes = document.write_pdf()
        page_count = len(document.pages)
        if pdf_bytes is None:
            raise RuntimeError("WeasyPrint returned no PDF bytes")
        return pdf_bytes, page_count

    def run(
        self,
        placement: Placement,
        override_kwargs: None = None,  # noqa: ARG002
        **llm_kwargs: Any,
    ) -> ToolResponse:
        title = (
            cast(str, llm_kwargs.get("title", "Untitled Report")).strip()
            or "Untitled Report"
        )
        subtitle = cast(str | None, llm_kwargs.get("subtitle"))
        template_name = cast(str, llm_kwargs.get("template") or "report")
        if template_name not in ("report", "brief"):
            template_name = "report"
        raw_sections = cast(list[Any], llm_kwargs.get("sections") or [])
        include_toc = bool(llm_kwargs.get("include_toc", True))
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

        sections = self._parse_sections(raw_sections)
        if not sections:
            raise ValueError(
                "PdfGenerationTool requires at least one section with a heading"
            )

        brand = DEFAULT_BRAND

        try:
            html_content = self._render_html(
                template_name=template_name,
                title=title,
                subtitle=subtitle,
                sections=sections,
                brand=brand,
                metadata=metadata,
                include_toc=include_toc,
                page_size=page_size,
            )
            pdf_bytes, page_count = self._render_pdf(html_content)
        except Exception:
            logger.exception("Error generating PDF document")
            raise

        size_bytes = len(pdf_bytes)
        buffer = io.BytesIO(pdf_bytes)

        file_store = get_default_file_store()
        file_id = file_store.save_file(
            content=buffer,
            display_name=f"{title}.pdf",
            file_origin=FileOrigin.GENERATED_REPORT,
            file_type="application/pdf",
        )
        file_url = build_frontend_file_url(file_id)

        generated_pdf = GeneratedPdf(
            file_id=file_id,
            url=file_url,
            title=title,
            page_count=page_count,
            size_bytes=size_bytes,
        )

        self.emitter.emit(
            Packet(
                placement=placement,
                obj=PdfGenerationFinal(pdf=generated_pdf),
            )
        )

        # Emit CustomToolDelta with file_ids so the CustomToolRenderer
        # shows a download button in the chat timeline.
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

        final_response = FinalPdfGenerationResponse(
            file_id=file_id,
            file_url=file_url,
            title=title,
            page_count=page_count,
            size_bytes=size_bytes,
        )

        full_download_url = build_full_frontend_file_url(file_id)
        llm_facing_response = json.dumps(
            {
                "file_id": file_id,
                "title": title,
                "page_count": page_count,
                "size_bytes": size_bytes,
                "download_url": full_download_url,
                "message": (
                    f"Generated a {page_count}-page PDF titled '{title}'. "
                    f"In your reply to the user, you MUST print the following "
                    f"full download URL as PLAIN TEXT on its own line (NOT as "
                    f"a markdown link, NOT wrapped in backticks, NOT modified "
                    f"in any way) so the user can copy and paste it into their "
                    f"browser to download the file:\n\n{full_download_url}"
                ),
            }
        )

        return ToolResponse(
            rich_response=final_response,
            llm_facing_response=llm_facing_response,
        )
