"""PDF rendering via Jinja templates and WeasyPrint.

Moved verbatim from the former `pdf_generation` package -- output is intended
to be byte-for-byte what it was before the shared-renderer refactor.
"""

import html
import re
from datetime import datetime
from datetime import timezone
from pathlib import Path

from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import select_autoescape
from typing_extensions import override

from onyx.tools.tool_implementations.document_generation.disclaimer import (
    DISCLAIMER_TEXT,
)
from onyx.tools.tool_implementations.document_generation.models import DocumentSpec
from onyx.tools.tool_implementations.document_generation.models import RenderedDocument
from onyx.tools.tool_implementations.document_generation.renderers.base import (
    DocumentRenderer,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()

TEMPLATES_DIR = Path(__file__).parent / "templates"

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


class PdfRenderer(DocumentRenderer):
    FORMAT = "pdf"
    MIME_TYPE = "application/pdf"
    EXTENSION = "pdf"

    @override
    @classmethod
    def is_available(cls) -> bool:
        """Available iff the WeasyPrint Python package can be imported.

        The system libraries (Cairo, Pango, GDK-pixbuf) are present in the
        backend Docker image but may be missing in local dev venvs.
        """
        try:
            import weasyprint  # noqa: F401
        except (ImportError, OSError) as exc:
            logger.warning(
                "PDF rendering unavailable: weasyprint cannot be imported (%s)",
                exc,
            )
            return False
        return True

    @override
    def render(self, spec: DocumentSpec) -> RenderedDocument:
        template_name = (
            spec.template if spec.template in ("report", "brief") else "report"
        )
        template = _jinja_env.get_template(f"{template_name}.html.j2")
        html_content = template.render(
            title=spec.title,
            subtitle=spec.subtitle,
            sections=spec.sections,
            brand=spec.brand,
            metadata=spec.metadata,
            include_toc=spec.include_toc,
            page_size=spec.page_size,
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            disclaimer=DISCLAIMER_TEXT,
        )

        # Lazy import so this module stays importable on machines without the
        # WeasyPrint system libraries installed (e.g. local Mac dev).
        from weasyprint import HTML

        document = HTML(string=html_content).render()
        pdf_bytes = document.write_pdf()
        if pdf_bytes is None:
            raise RuntimeError("WeasyPrint returned no PDF bytes")

        return RenderedDocument(
            content=pdf_bytes,
            mime_type=self.MIME_TYPE,
            extension=self.EXTENSION,
            unit_count=len(document.pages),
            unit_label="pages",
        )
