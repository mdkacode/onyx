"""Unit tests for PdfGenerationTool.

These tests deliberately avoid invoking WeasyPrint directly, since its
system dependencies (Cairo, Pango, GDK-pixbuf) are not present in every
local dev environment. The PDF render step is stubbed out; full
end-to-end rendering is validated inside the Docker backend image where
the system libs are installed.
"""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from onyx.server.query_and_chat.placement import Placement
from onyx.tools.tool_implementations.pdf_generation.models import BrandConfig
from onyx.tools.tool_implementations.pdf_generation.models import Section
from onyx.tools.tool_implementations.pdf_generation.models import TableData
from onyx.tools.tool_implementations.pdf_generation.pdf_generation_tool import (
    _inline_format,
)
from onyx.tools.tool_implementations.pdf_generation.pdf_generation_tool import (
    _jinja_env,
)
from onyx.tools.tool_implementations.pdf_generation.pdf_generation_tool import (
    PdfGenerationTool,
)


TOOL_MODULE = "onyx.tools.tool_implementations.pdf_generation.pdf_generation_tool"


# ─── tool_definition ────────────────────────────────────────────────────────


def test_tool_definition_schema_shape() -> None:
    tool = PdfGenerationTool(tool_id=1, emitter=MagicMock())
    defn = tool.tool_definition()

    assert defn["type"] == "function"
    assert defn["function"]["name"] == "generate_pdf"

    params = defn["function"]["parameters"]
    assert params["type"] == "object"
    assert params["required"] == ["title", "sections"]

    props = params["properties"]
    assert set(props.keys()) == {
        "title",
        "subtitle",
        "template",
        "sections",
        "include_toc",
        "page_size",
        "metadata",
    }
    assert props["template"]["enum"] == ["report", "brief"]
    assert props["page_size"]["enum"] == ["A4", "Letter"]

    section_schema = props["sections"]["items"]
    assert section_schema["required"] == ["heading"]
    assert set(section_schema["properties"].keys()) >= {
        "heading",
        "body",
        "bullet_points",
        "callout",
        "table",
    }


# ─── _inline_format: escaping + inline markup ────────────────────────────────


def test_inline_format_escapes_html_before_applying_markers() -> None:
    # The user / LLM could pass raw HTML — we must escape before substituting.
    result = _inline_format("<script>alert(1)</script>")
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_inline_format_converts_bold_and_code() -> None:
    result = _inline_format("This is **bold** and `code`.")
    assert "<strong>bold</strong>" in result
    assert "<code>code</code>" in result


def test_inline_format_empty_string() -> None:
    assert _inline_format("") == ""
    assert _inline_format(None) == ""  # type: ignore[arg-type]


def test_inline_format_code_inside_escaped_html() -> None:
    # `<b>` written as inline code should round-trip as escaped HTML within a <code> tag.
    result = _inline_format("Use `<b>` for bold.")
    assert "<code>&lt;b&gt;</code>" in result


# ─── _parse_sections ─────────────────────────────────────────────────────────


def test_parse_sections_skips_items_without_heading() -> None:
    raw = [
        {"heading": "First", "body": "a"},
        {"body": "no heading"},
        {"heading": "   ", "body": "blank heading"},
        {"heading": "Second"},
    ]
    parsed = PdfGenerationTool._parse_sections(raw)
    assert [s.heading for s in parsed] == ["First", "Second"]


def test_parse_sections_builds_table() -> None:
    raw = [
        {
            "heading": "Stats",
            "table": {
                "headers": ["A", "B"],
                "rows": [["1", "2"], ["3", "4"]],
            },
        }
    ]
    parsed = PdfGenerationTool._parse_sections(raw)
    assert len(parsed) == 1
    table = parsed[0].table
    assert table is not None
    assert table.headers == ["A", "B"]
    assert table.rows == [["1", "2"], ["3", "4"]]


def test_parse_sections_coerces_non_string_cells_to_str() -> None:
    raw = [
        {
            "heading": "Nums",
            "table": {"headers": ["x"], "rows": [[42], [3.14]]},
        }
    ]
    parsed = PdfGenerationTool._parse_sections(raw)
    table = parsed[0].table
    assert table is not None
    assert table.rows == [["42"], ["3.14"]]


def test_parse_sections_drops_table_without_headers() -> None:
    raw = [{"heading": "X", "table": {"headers": [], "rows": [["a"]]}}]
    parsed = PdfGenerationTool._parse_sections(raw)
    assert parsed[0].table is None


# ─── Template rendering ──────────────────────────────────────────────────────


def test_report_template_renders_with_all_features() -> None:
    sections = [
        Section(
            heading="Executive Summary",
            body="The **metric** rose `42%`.",
            bullet_points=["Point A", "Point B"],
        ),
        Section(
            heading="Data",
            table=TableData(headers=["Col1", "Col2"], rows=[["1", "2"]]),
            callout="Key insight here.",
        ),
    ]
    html = _jinja_env.get_template("report.html.j2").render(
        title="Q1 Report",
        subtitle="Preliminary",
        sections=sections,
        brand=BrandConfig(company_name="Acme Corp"),
        metadata=None,
        include_toc=True,
        page_size="A4",
        generated_at="2026-04-10",
    )
    # Inline markup applied
    assert "<strong>metric</strong>" in html
    assert "<code>42%</code>" in html
    # TOC present when >1 section
    assert "Contents" in html
    assert 'href="#section-1"' in html
    # Cover page shows company name
    assert "Acme Corp" in html
    # Table rendered
    assert "<th>Col1</th>" in html
    # Callout box
    assert 'class="callout"' in html


def test_brief_template_renders_without_toc_or_cover() -> None:
    sections = [Section(heading="Summary", body="Short doc.")]
    html = _jinja_env.get_template("brief.html.j2").render(
        title="Brief",
        subtitle=None,
        sections=sections,
        brand=BrandConfig(),
        metadata=None,
        include_toc=False,
        page_size="A4",
        generated_at="2026-04-10",
    )
    assert "Contents" not in html
    assert "cover-page" not in html
    assert "header-bar" in html


# ─── is_available ────────────────────────────────────────────────────────────


def test_is_available_true_when_weasyprint_imports() -> None:
    db_session = MagicMock()
    with patch(f"{TOOL_MODULE}.logger"):
        # Stub `import weasyprint` to succeed inside the method.
        with patch.dict("sys.modules", {"weasyprint": MagicMock()}, clear=False):
            assert PdfGenerationTool.is_available(db_session) is True


def test_is_available_false_when_weasyprint_oserror() -> None:
    db_session = MagicMock()

    original_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__  # type: ignore[index]

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "weasyprint":
            raise OSError("cannot load library 'gobject-2.0-0'")
        return original_import(name, *args, **kwargs)  # type: ignore[operator]

    with patch("builtins.__import__", side_effect=fake_import):
        assert PdfGenerationTool.is_available(db_session) is False


# ─── run: end-to-end with WeasyPrint stubbed ─────────────────────────────────


def test_run_happy_path_saves_file_and_emits_final_packet() -> None:
    emitter = MagicMock()
    tool = PdfGenerationTool(tool_id=7, emitter=emitter)

    fake_file_store = MagicMock()
    fake_file_store.save_file.return_value = "file-abc-123"

    with (
        patch.object(
            PdfGenerationTool,
            "_render_pdf",
            return_value=(b"%PDF-1.4 stub bytes", 3),
        ),
        patch(f"{TOOL_MODULE}.get_default_file_store", return_value=fake_file_store),
    ):
        response = tool.run(
            placement=Placement(turn_index=0),
            override_kwargs=None,
            title="Q1 Review",
            subtitle="Draft",
            template="report",
            sections=[
                {
                    "heading": "Executive Summary",
                    "body": "All systems green.",
                    "bullet_points": ["Item one", "Item two"],
                },
                {"heading": "Next Steps", "body": "Ship it."},
            ],
            include_toc=True,
            page_size="A4",
        )

    # File store was called with the correct origin + MIME type
    save_call = fake_file_store.save_file.call_args
    assert save_call.kwargs["file_origin"].value == "generated_report"
    assert save_call.kwargs["file_type"] == "application/pdf"
    assert save_call.kwargs["display_name"] == "Q1 Review.pdf"

    # Final packet emitted
    emit_call = emitter.emit.call_args
    packet = emit_call.args[0]
    assert packet.obj.type == "pdf_generation_final"
    assert packet.obj.pdf.file_id == "file-abc-123"
    assert packet.obj.pdf.page_count == 3
    assert packet.obj.pdf.title == "Q1 Review"

    # ToolResponse shape
    assert response.rich_response is not None
    assert "file-abc-123" in response.llm_facing_response
    assert "3-page" in response.llm_facing_response


def test_run_rejects_request_with_no_valid_sections() -> None:
    tool = PdfGenerationTool(tool_id=1, emitter=MagicMock())
    with pytest.raises(ValueError, match="at least one section"):
        tool.run(
            placement=Placement(turn_index=0),
            override_kwargs=None,
            title="Empty",
            sections=[{"body": "no heading"}],
        )


def test_run_invalid_template_falls_back_to_report() -> None:
    emitter = MagicMock()
    tool = PdfGenerationTool(tool_id=1, emitter=emitter)
    fake_file_store = MagicMock()
    fake_file_store.save_file.return_value = "fid"

    with (
        patch.object(PdfGenerationTool, "_render_pdf", return_value=(b"%PDF-", 1)),
        patch(f"{TOOL_MODULE}.get_default_file_store", return_value=fake_file_store),
        patch.object(
            PdfGenerationTool, "_render_html", wraps=PdfGenerationTool._render_html
        ) as spy,
    ):
        tool.run(
            placement=Placement(turn_index=0),
            override_kwargs=None,
            title="T",
            template="bogus",
            sections=[{"heading": "S"}],
        )
    # Fell back to 'report'
    assert spy.call_args.kwargs["template_name"] == "report"
