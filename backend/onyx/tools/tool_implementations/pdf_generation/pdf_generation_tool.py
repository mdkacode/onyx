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
                                "that appears on every page (e.g. 'DRAFT', "
                                "'CONFIDENTIAL'). Defaults to "
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

    def _build_brand(self, llm_kwargs: dict[str, Any]) -> BrandConfig:
        """Assemble a BrandConfig for this request.

        Priority:
          1. Prompt-supplied overrides (`primary_color`, `secondary_color`,
             `watermark_text`, `watermark_color`) — validated, then applied.
          2. Otherwise inherit defaults from `DEFAULT_BRAND`.

        The watermark default is "NaArNi · <user-local-part>" when we have an
        authenticated user. An explicit empty string from the LLM disables
        the watermark (user asked to opt out).
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

        brand = self._build_brand(llm_kwargs)

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
