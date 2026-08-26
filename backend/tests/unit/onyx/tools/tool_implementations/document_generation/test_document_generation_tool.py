"""Unit tests for the document generation tool.

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
from onyx.tools.tool_implementations.document_generation.disclaimer import (
    DISCLAIMER_TEXT,
)
from onyx.tools.tool_implementations.document_generation.document_generation_tool import (
    _derive_user_label,
)
from onyx.tools.tool_implementations.document_generation.document_generation_tool import (
    _safe_color,
)
from onyx.tools.tool_implementations.document_generation.document_generation_tool import (
    PdfGenerationTool,
)
from onyx.tools.tool_implementations.document_generation.models import BrandConfig
from onyx.tools.tool_implementations.document_generation.models import (
    FinalDocumentGenerationResponse,
)
from onyx.tools.tool_implementations.document_generation.models import RenderedDocument
from onyx.tools.tool_implementations.document_generation.models import Section
from onyx.tools.tool_implementations.document_generation.models import TableData
from onyx.tools.tool_implementations.document_generation.renderers import (
    available_formats,
)
from onyx.tools.tool_implementations.document_generation.renderers.pdf import (
    _inline_format,
)
from onyx.tools.tool_implementations.document_generation.renderers.pdf import _jinja_env
from onyx.tools.tool_implementations.document_generation.renderers.pdf import (
    PdfRenderer,
)

TOOL_MODULE = (
    "onyx.tools.tool_implementations.document_generation.document_generation_tool"
)


# ─── tool_definition ────────────────────────────────────────────────────────


def test_tool_definition_schema_shape() -> None:
    tool = PdfGenerationTool(tool_id=1, emitter=MagicMock())
    defn = tool.tool_definition()

    assert defn["type"] == "function"
    assert defn["function"]["name"] == "generate_document"

    params = defn["function"]["parameters"]
    assert params["type"] == "object"
    assert params["required"] == ["format", "title", "sections"]

    props = params["properties"]
    assert set(props.keys()) == {
        "format",
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
    # Only formats renderable in this environment are offered, so the model is
    # never handed an option that would fail at render time.
    assert set(props["format"]["enum"]) == set(available_formats())
    assert {"docx", "csv", "xlsx"}.issubset(set(props["format"]["enum"]))

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
    assert _inline_format(None) == ""  # ty: ignore[invalid-argument-type]


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


def test_tool_is_available_whenever_any_format_renders() -> None:
    """The tool no longer disables itself when WeasyPrint is missing.

    It used to be PDF-only, so a dev box without Cairo/Pango lost the whole
    capability. DOCX/CSV/XLSX are pure Python, so only PDF can drop out.
    """
    db_session = MagicMock()
    assert PdfGenerationTool.is_available(db_session) is True


def test_pdf_renderer_reports_unavailable_when_weasyprint_oserror() -> None:
    """The WeasyPrint availability check moved down to the PDF renderer."""
    original_import = (
        __builtins__["__import__"]
        if isinstance(__builtins__, dict)
        else __builtins__.__import__  # type: ignore[union-attr]
    )

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "weasyprint":
            raise OSError("cannot load library 'gobject-2.0-0'")
        return original_import(name, *args, **kwargs)  # type: ignore[operator]

    with patch("builtins.__import__", side_effect=fake_import):
        assert PdfRenderer.is_available() is False


def test_unavailable_format_is_not_offered_to_the_model() -> None:
    with patch.object(PdfRenderer, "is_available", return_value=False):
        tool = PdfGenerationTool(tool_id=1, emitter=MagicMock())
        assert (
            "pdf"
            not in tool.tool_definition()["function"]["parameters"]["properties"][
                "format"
            ]["enum"]
        )


# ─── run: end-to-end with WeasyPrint stubbed ─────────────────────────────────


def test_run_happy_path_saves_file_and_returns_download_url() -> None:
    """Runs a real DOCX render -- no stubbing of the renderer at all."""
    emitter = MagicMock()
    tool = PdfGenerationTool(tool_id=7, emitter=emitter)

    fake_file_store = MagicMock()
    fake_file_store.save_file.return_value = "file-abc-123"

    with patch(f"{TOOL_MODULE}.get_default_file_store", return_value=fake_file_store):
        response = tool.run(
            placement=Placement(turn_index=0),
            override_kwargs=None,
            format="docx",
            title="Q1 Review",
            subtitle="Draft",
            sections=[
                {
                    "heading": "Executive Summary",
                    "body": "All systems green.",
                    "bullet_points": ["Item one", "Item two"],
                },
                {"heading": "Next Steps", "body": "Ship it."},
            ],
        )

    save_call = fake_file_store.save_file.call_args
    assert save_call.kwargs["file_origin"].value == "generated_report"
    assert save_call.kwargs["display_name"] == "Q1 Review.docx"
    assert "wordprocessingml" in save_call.kwargs["file_type"]

    # The download button in chat is driven by this packet, so losing it would
    # silently strip the only affordance most users see.
    deltas = [
        call.args[0]
        for call in emitter.emit.call_args_list
        if call.args[0].obj.type == "custom_tool_delta"
    ]
    assert len(deltas) == 1
    assert deltas[0].obj.file_ids == ["file-abc-123"]

    # `rich_response` is a wide union; narrow before touching format-specific
    # fields so the assertion is checked rather than assumed.
    rich = response.rich_response
    assert isinstance(rich, FinalDocumentGenerationResponse)
    assert rich.format == "docx"
    assert "file-abc-123" in response.llm_facing_response


def test_run_records_owner_so_only_they_can_download() -> None:
    """`file_metadata["user_id"]` is the *only* thing gating the later download.

    Generated files never land in ChatMessage.files, so if this metadata is
    missing the file is either unreachable or reachable by the wrong person
    (see `_user_can_access_generated_report`).
    """
    user = MagicMock()
    user.id = "user-uuid-1"
    user.email = "mayank@naarni.com"
    tool = PdfGenerationTool(tool_id=1, emitter=MagicMock(), user=user)

    fake_file_store = MagicMock()
    fake_file_store.save_file.return_value = "fid"

    with patch(f"{TOOL_MODULE}.get_default_file_store", return_value=fake_file_store):
        tool.run(
            placement=Placement(turn_index=0),
            override_kwargs=None,
            format="docx",
            title="Owned",
            sections=[{"heading": "S", "body": "b"}],
        )

    assert fake_file_store.save_file.call_args.kwargs["file_metadata"] == {
        "user_id": "user-uuid-1"
    }


def test_run_emits_legacy_pdf_packet_only_for_pdf() -> None:
    """PDF keeps emitting PdfGenerationFinal; new formats do not.

    Nothing in web/src consumes that packet, so it is preserved for PDF purely
    to avoid changing existing behaviour rather than extended to new formats.
    """
    stub_renderer = MagicMock()
    stub_renderer.render.return_value = RenderedDocument(
        content=b"%PDF-1.4 stub",
        mime_type="application/pdf",
        extension="pdf",
        unit_count=3,
        unit_label="pages",
    )
    fake_file_store = MagicMock()
    fake_file_store.save_file.return_value = "file-pdf-1"

    emitter = MagicMock()
    tool = PdfGenerationTool(tool_id=1, emitter=emitter)

    with (
        patch(f"{TOOL_MODULE}.get_renderer", return_value=stub_renderer),
        patch(f"{TOOL_MODULE}.get_default_file_store", return_value=fake_file_store),
    ):
        response = tool.run(
            placement=Placement(turn_index=0),
            override_kwargs=None,
            format="pdf",
            title="Q1 Review",
            sections=[{"heading": "S", "body": "b"}],
        )

    finals = [
        call.args[0]
        for call in emitter.emit.call_args_list
        if call.args[0].obj.type == "pdf_generation_final"
    ]
    assert len(finals) == 1
    assert finals[0].obj.pdf.page_count == 3
    assert "3-page" in response.llm_facing_response


def test_run_does_not_emit_pdf_packet_for_docx() -> None:
    emitter = MagicMock()
    tool = PdfGenerationTool(tool_id=1, emitter=emitter)
    fake_file_store = MagicMock()
    fake_file_store.save_file.return_value = "fid"

    with patch(f"{TOOL_MODULE}.get_default_file_store", return_value=fake_file_store):
        tool.run(
            placement=Placement(turn_index=0),
            override_kwargs=None,
            format="docx",
            title="T",
            sections=[{"heading": "S"}],
        )

    assert not [
        c
        for c in emitter.emit.call_args_list
        if c.args[0].obj.type == "pdf_generation_final"
    ]


def test_run_surfaces_renderer_notes_to_the_model() -> None:
    """A lossy export must be reported, not silently returned."""
    emitter = MagicMock()
    tool = PdfGenerationTool(tool_id=1, emitter=emitter)
    fake_file_store = MagicMock()
    fake_file_store.save_file.return_value = "fid"

    with patch(f"{TOOL_MODULE}.get_default_file_store", return_value=fake_file_store):
        response = tool.run(
            placement=Placement(turn_index=0),
            override_kwargs=None,
            format="csv",
            title="Data",
            sections=[
                {"heading": "One", "table": {"headers": ["a"], "rows": [["1"]]}},
                {"heading": "Two", "table": {"headers": ["b"], "rows": [["2"]]}},
            ],
        )

    assert "Also tell the user" in response.llm_facing_response
    assert "first of 2" in response.llm_facing_response


def test_run_rejects_request_with_no_valid_sections() -> None:
    tool = PdfGenerationTool(tool_id=1, emitter=MagicMock())
    with pytest.raises(ValueError, match="at least one section"):
        tool.run(
            placement=Placement(turn_index=0),
            override_kwargs=None,
            format="docx",
            title="Empty",
            sections=[{"body": "no heading"}],
        )


def test_run_rejects_unknown_format() -> None:
    tool = PdfGenerationTool(tool_id=1, emitter=MagicMock())
    with pytest.raises(ValueError, match="Available formats"):
        tool.run(
            placement=Placement(turn_index=0),
            override_kwargs=None,
            format="pages",
            title="T",
            sections=[{"heading": "S"}],
        )


def test_invalid_template_falls_back_to_report() -> None:
    tool = PdfGenerationTool(tool_id=1, emitter=MagicMock())
    spec = tool._build_spec(
        {"title": "T", "template": "bogus", "sections": [{"heading": "S"}]}
    )
    assert spec.template == "report"


def test_invalid_page_size_falls_back_to_a4() -> None:
    tool = PdfGenerationTool(tool_id=1, emitter=MagicMock())
    spec = tool._build_spec(
        {"title": "T", "page_size": "A0", "sections": [{"heading": "S"}]}
    )
    assert spec.page_size == "A4"


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
    from onyx.tools.tool_implementations.document_generation.models import BrandConfig

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
    from onyx.tools.tool_implementations.document_generation.models import BrandConfig

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
    from onyx.tools.tool_implementations.document_generation.models import BrandConfig

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


def test_prompt_colors_and_watermark_reach_the_renderer() -> None:
    """Brand overrides now land on the DocumentSpec handed to the renderer.

    Previously asserted against rendered HTML; the spec is the real seam now
    that every format consumes the same brand config.
    """
    user = MagicMock()
    user.email = "testuser@example.com"
    tool = PdfGenerationTool(tool_id=9, emitter=MagicMock(), user=user)

    spec = tool._build_spec(
        {
            "title": "Branded",
            "sections": [{"heading": "Intro", "body": "Hi"}],
            "primary_color": "#FF5722",
            "watermark_text": "DRAFT",
            "watermark_color": "#333333",
        }
    )

    assert spec.brand.primary_color == "#FF5722"
    assert spec.brand.watermark_text == "DRAFT"
    assert spec.brand.watermark_color == "#333333"


def test_pdf_template_still_tiles_the_watermark() -> None:
    """The rendered HTML remains the ultimate check for PDF watermarking."""
    brand = BrandConfig(watermark_text="DRAFT", watermark_color="#333333")
    html = _jinja_env.get_template("report.html.j2").render(
        title="T",
        subtitle=None,
        sections=[Section(heading="Intro", body="Hi")],
        brand=brand,
        metadata=None,
        include_toc=False,
        page_size="A4",
        generated_at="2026-04-21",
        disclaimer=DISCLAIMER_TEXT,
    )
    assert html.count("DRAFT") >= 10
    assert "#333333" in html


def test_default_watermark_uses_the_authenticated_user() -> None:
    user = MagicMock()
    user.email = "first.last@example.com"
    tool = PdfGenerationTool(tool_id=9, emitter=MagicMock(), user=user)

    spec = tool._build_spec({"title": "T", "sections": [{"heading": "S"}]})

    assert spec.brand.watermark_text == "NaArNi · first.last"


def test_watermark_cannot_be_disabled_by_an_empty_string() -> None:
    """An un-watermarked document must not be reachable from prompt input."""
    user = MagicMock()
    user.email = "first.last@example.com"
    tool = PdfGenerationTool(tool_id=9, emitter=MagicMock(), user=user)

    spec = tool._build_spec(
        {"title": "T", "sections": [{"heading": "S"}], "watermark_text": "   "}
    )

    assert spec.brand.watermark_text == "NaArNi · first.last"


def test_pdf_disclaimer_is_rendered_into_the_template() -> None:
    html = _jinja_env.get_template("report.html.j2").render(
        title="T",
        subtitle=None,
        sections=[Section(heading="Intro", body="Hi")],
        brand=BrandConfig(),
        metadata=None,
        include_toc=False,
        page_size="A4",
        generated_at="2026-04-21",
        disclaimer=DISCLAIMER_TEXT,
    )
    assert DISCLAIMER_TEXT in html
