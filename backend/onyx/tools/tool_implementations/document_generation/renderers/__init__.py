"""Renderer registry.

Formats whose `is_available()` is False are dropped from the tool definition
entirely, so the model is never offered a format that would fail at render
time. In practice only PDF can be unavailable (WeasyPrint system libraries);
DOCX, CSV, and XLSX are pure Python and always work.
"""

from onyx.tools.tool_implementations.document_generation.renderers.base import (
    DocumentRenderer,
)
from onyx.tools.tool_implementations.document_generation.renderers.docx import (
    DocxRenderer,
)
from onyx.tools.tool_implementations.document_generation.renderers.pdf import (
    PdfRenderer,
)
from onyx.tools.tool_implementations.document_generation.renderers.tabular import (
    CsvRenderer,
)
from onyx.tools.tool_implementations.document_generation.renderers.tabular import (
    XlsxRenderer,
)

_ALL_RENDERERS: list[type[DocumentRenderer]] = [
    PdfRenderer,
    DocxRenderer,
    CsvRenderer,
    XlsxRenderer,
]

RENDERERS: dict[str, type[DocumentRenderer]] = {r.FORMAT: r for r in _ALL_RENDERERS}


def available_formats() -> list[str]:
    """Formats renderable in this environment, in preference order."""
    return [r.FORMAT for r in _ALL_RENDERERS if r.is_available()]


def get_renderer(fmt: str) -> DocumentRenderer:
    renderer_cls = RENDERERS.get(fmt)
    if renderer_cls is None or not renderer_cls.is_available():
        raise ValueError(
            f"Unsupported document format '{fmt}'. "
            f"Available formats: {', '.join(available_formats())}."
        )
    return renderer_cls()


__all__ = [
    "RENDERERS",
    "DocumentRenderer",
    "available_formats",
    "get_renderer",
]
