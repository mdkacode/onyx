"""Renderer contract shared by every output format."""

import abc

from onyx.tools.tool_implementations.document_generation.models import DocumentSpec
from onyx.tools.tool_implementations.document_generation.models import RenderedDocument


class DocumentRenderer(abc.ABC):
    """Turns a `DocumentSpec` into bytes of one specific format."""

    # Value of the tool's `format` argument that selects this renderer.
    FORMAT: str
    MIME_TYPE: str
    EXTENSION: str
    # Prose formats consume sections; tabular ones consume tables and reject a
    # spec that has none. Drives both validation and the tool description.
    IS_TABULAR: bool = False

    @classmethod
    def is_available(cls) -> bool:
        """Whether this renderer can run in the current environment.

        Only the PDF renderer answers False in practice -- WeasyPrint needs
        system libraries that are present in the Docker image but often absent
        from local dev venvs. Unavailable formats are dropped from the tool
        definition so the model is never offered one that would fail.
        """
        return True

    @abc.abstractmethod
    def render(self, spec: DocumentSpec) -> RenderedDocument:
        raise NotImplementedError
