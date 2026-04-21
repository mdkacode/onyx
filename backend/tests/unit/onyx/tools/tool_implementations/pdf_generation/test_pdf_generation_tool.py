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
    _derive_user_label,
)
from onyx.tools.tool_implementations.pdf_generation.pdf_generation_tool import (
    _inline_format,
)
from onyx.tools.tool_implementations.pdf_generation.pdf_generation_tool import (
    _jinja_env,
)
from onyx.tools.tool_implementations.pdf_generation.pdf_generation_tool import (
    _safe_color,
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
        "primary_color",
        "secondary_color",
        "watermark_text",
        "watermark_color",
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


# ─── _safe_color: hex validation (CSS injection defense) ────────────────────


def test_safe_color_accepts_three_digit_hex() -> None:
    assert _safe_color("#abc", "#000") == "#abc"


def test_safe_color_accepts_six_digit_hex() -> None:
    assert _safe_color("#0052CC", "#000") == "#0052CC"


def test_safe_color_accepts_eight_digit_hex_for_alpha() -> None:
    assert _safe_color("#0052CC80", "#000") == "#0052CC80"


def test_safe_color_rejects_non_hex_named_color() -> None:
    # Named colors could still be safe in CSS, but we restrict to hex for
    # a simple bright-line validator.
    assert _safe_color("blue", "#111111") == "#111111"


def test_safe_color_rejects_injection_payload() -> None:
    # The classic payload: break out of the attribute and add a rule.
    payload = "#fff;}body{display:none"
    assert _safe_color(payload, "#000000") == "#000000"


def test_safe_color_rejects_non_string() -> None:
    assert _safe_color(None, "#fallback") == "#fallback"
    assert _safe_color(42, "#fallback") == "#fallback"


def test_safe_color_trims_whitespace() -> None:
    assert _safe_color("  #abcdef  ", "#000") == "#abcdef"


# ─── _derive_user_label: watermark name resolution ──────────────────────────


def test_derive_user_label_uses_local_part_of_email() -> None:
    user = MagicMock()
    user.email = "first.last@example.com"
    assert _derive_user_label(user) == "first.last"


def test_derive_user_label_none_when_no_user() -> None:
    assert _derive_user_label(None) is None


def test_derive_user_label_none_when_email_missing() -> None:
    user = MagicMock(spec=[])  # no attributes
    assert _derive_user_label(user) is None


# ─── _build_brand: merges prompt overrides with defaults ────────────────────


def _tool(user_email: str | None = None) -> PdfGenerationTool:
    user = None
    if user_email is not None:
        user = MagicMock()
        user.email = user_email
    return PdfGenerationTool(tool_id=1, emitter=MagicMock(), user=user)


def test_build_brand_default_watermark_uses_username() -> None:
    tool = _tool(user_email="testuser@example.com")
    brand = tool._build_brand({})
    assert brand.watermark_text == "NaArNi · testuser"


def test_build_brand_default_watermark_falls_back_to_naarni_only() -> None:
    tool = _tool(user_email=None)
    brand = tool._build_brand({})
    # No user → plain "NaArNi" (no trailing separator or empty local part).
    assert brand.watermark_text == "NaArNi"


def test_build_brand_explicit_watermark_overrides_default() -> None:
    tool = _tool(user_email="testuser@example.com")
    brand = tool._build_brand({"watermark_text": "CONFIDENTIAL"})
    assert brand.watermark_text == "CONFIDENTIAL"


def test_build_brand_empty_watermark_string_falls_back_to_default() -> None:
    # Watermark is mandatory: an empty / whitespace override must NOT
    # disable it — it falls through to the default "NaArNi · <user>".
    tool = _tool(user_email="testuser@example.com")
    brand = tool._build_brand({"watermark_text": "   "})
    assert brand.watermark_text == "NaArNi · testuser"


def test_build_brand_non_string_watermark_falls_back_to_default() -> None:
    tool = _tool(user_email="testuser@example.com")
    # Defensive: LLM could send null/number — still get a watermark.
    brand = tool._build_brand({"watermark_text": None})
    assert brand.watermark_text == "NaArNi · testuser"


def test_build_brand_applies_valid_color_overrides() -> None:
    tool = _tool()
    brand = tool._build_brand(
        {
            "primary_color": "#FF5722",
            "secondary_color": "#00796B",
            "watermark_color": "#424242",
        }
    )
    assert brand.primary_color == "#FF5722"
    assert brand.secondary_color == "#00796B"
    assert brand.watermark_color == "#424242"


def test_build_brand_ignores_invalid_colors() -> None:
    tool = _tool()
    brand = tool._build_brand(
        {
            "primary_color": "javascript:alert(1)",
            "secondary_color": "#zzz",
            "watermark_color": "red",  # named colors rejected
        }
    )
    # All three fall through to the BrandConfig defaults.
    assert brand.primary_color == "#0052CC"
    assert brand.secondary_color == "#172B4D"
    assert brand.watermark_color == "#172B4D"


# ─── Template: watermark renders once per tile + respects opt-out ──────────


def test_report_template_renders_watermark_tiles() -> None:
    from onyx.tools.tool_implementations.pdf_generation.models import BrandConfig

    sections = [Section(heading="Intro", body="Hello")]
    brand = BrandConfig(watermark_text="NaArNi · testuser")
    html = _jinja_env.get_template("report.html.j2").render(
        title="T",
        subtitle=None,
        sections=sections,
        brand=brand,
        metadata=None,
        include_toc=False,
        page_size="A4",
        generated_at="2026-04-21",
    )
    # The watermark container is present.
    assert 'class="watermark"' in html
    # Several tiles rendered with the text.
    assert html.count("NaArNi · testuser") >= 10
    # And the color from BrandConfig is interpolated into the CSS.
    assert "#172B4D" in html


def test_report_template_omits_watermark_when_disabled() -> None:
    from onyx.tools.tool_implementations.pdf_generation.models import BrandConfig

    sections = [Section(heading="Intro", body="Hello")]
    brand = BrandConfig(watermark_text=None)
    html = _jinja_env.get_template("report.html.j2").render(
        title="T",
        subtitle=None,
        sections=sections,
        brand=brand,
        metadata=None,
        include_toc=False,
        page_size="A4",
        generated_at="2026-04-21",
    )
    # The watermark *element* is gone, but the class rules can still be in
    # the stylesheet (they're harmless without the element).
    assert 'class="watermark"' not in html


def test_brief_template_renders_watermark_tiles() -> None:
    from onyx.tools.tool_implementations.pdf_generation.models import BrandConfig

    sections = [Section(heading="Summary", body="Short")]
    brand = BrandConfig(watermark_text="NaArNi · testuser")
    html = _jinja_env.get_template("brief.html.j2").render(
        title="T",
        subtitle=None,
        sections=sections,
        brand=brand,
        metadata=None,
        include_toc=False,
        page_size="A4",
        generated_at="2026-04-21",
    )
    assert 'class="watermark"' in html
    assert html.count("NaArNi · testuser") >= 10


# ─── run: threads watermark/color params end-to-end ────────────────────────


def test_run_threads_prompt_color_and_watermark_through_to_template() -> None:
    emitter = MagicMock()
    user = MagicMock()
    user.email = "testuser@example.com"
    tool = PdfGenerationTool(tool_id=9, emitter=emitter, user=user)

    fake_file_store = MagicMock()
    fake_file_store.save_file.return_value = "fid"

    captured_html: list[str] = []

    def fake_render_pdf(html_content: str) -> tuple[bytes, int]:
        captured_html.append(html_content)
        return (b"%PDF-1.4 stub", 2)

    with (
        patch.object(PdfGenerationTool, "_render_pdf", side_effect=fake_render_pdf),
        patch(f"{TOOL_MODULE}.get_default_file_store", return_value=fake_file_store),
    ):
        tool.run(
            placement=Placement(turn_index=0),
            override_kwargs=None,
            title="Branded",
            sections=[{"heading": "Intro", "body": "Hi"}],
            primary_color="#FF5722",
            watermark_text="DRAFT",
            watermark_color="#333333",
        )

    html_content = captured_html[0]
    # Primary color override made it into the stylesheet.
    assert "#FF5722" in html_content
    # Custom watermark text is tiled on the page.
    assert html_content.count("DRAFT") >= 10
    # Watermark color override applied.
    assert "#333333" in html_content


def test_run_uses_default_watermark_with_user_when_unspecified() -> None:
    emitter = MagicMock()
    user = MagicMock()
    user.email = "first.last@example.com"
    tool = PdfGenerationTool(tool_id=9, emitter=emitter, user=user)

    fake_file_store = MagicMock()
    fake_file_store.save_file.return_value = "fid"

    captured_html: list[str] = []

    def fake_render_pdf(html_content: str) -> tuple[bytes, int]:
        captured_html.append(html_content)
        return (b"%PDF-1.4 stub", 1)

    with (
        patch.object(PdfGenerationTool, "_render_pdf", side_effect=fake_render_pdf),
        patch(f"{TOOL_MODULE}.get_default_file_store", return_value=fake_file_store),
    ):
        tool.run(
            placement=Placement(turn_index=0),
            override_kwargs=None,
            title="Auto-watermark",
            sections=[{"heading": "Intro", "body": "Hi"}],
        )

    html_content = captured_html[0]
    assert "NaArNi · first.last" in html_content
