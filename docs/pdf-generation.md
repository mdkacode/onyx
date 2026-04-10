# PDF Generation Tool

## Overview

Onyx ships a built-in `PdfGenerationTool` that lets the LLM produce a
professional, downloadable PDF directly from a conversation. When a user
asks the agent to "make a PDF", "export this as a report", or similar,
the LLM calls the tool with a structured outline and the backend renders
a styled PDF using WeasyPrint.

## How it works

The flow has three stages:

1. **LLM fills the schema.** The tool advertises an OpenAI-function-shaped
   JSON schema describing `title`, `subtitle`, `template`, `sections`
   (each with heading, body, optional bullet points, callout, and table),
   `include_toc`, and `page_size`. The LLM populates these fields from
   the conversation context.
2. **Backend renders HTML.** The tool loads one of two Jinja2 templates
   (`report.html.j2` or `brief.html.j2`) from
   [`backend/onyx/tools/tool_implementations/pdf_generation/templates/`](../backend/onyx/tools/tool_implementations/pdf_generation/templates/),
   applies a brand config (colors, fonts, optional logo), escapes the
   LLM-supplied content, and renders inline `**bold**` / `` `code` ``
   markers.
3. **WeasyPrint produces the PDF.** The HTML is handed to WeasyPrint,
   which returns the final PDF bytes. The file is persisted via the
   Onyx `file_store` abstraction (S3 in Naarni prod) with
   `FileOrigin.GENERATED_REPORT`, and a download URL in the form
   `/api/chat/file/<file_id>` is returned to the chat UI and the LLM.

## Enabling the tool

The tool is seeded automatically by the alembic migration
[`0b82fce0fa68_add_pdf_generation_tool_and_drop_.py`](../backend/alembic/versions/0b82fce0fa68_add_pdf_generation_tool_and_drop_.py)
and attached to every existing persona. After running `alembic upgrade
head` it will appear in the tools list for all assistants automatically.

To enable or disable it on a specific persona after that, go to the
admin assistants page and toggle it via the tool list, same as any
other built-in tool.

`PdfGenerationTool.is_available()` returns `True` only when the
WeasyPrint Python package can be imported. On machines missing the
system libraries (Cairo, Pango, GDK-pixbuf) the tool silently reports
itself unavailable and is skipped during tool construction rather than
crashing at call time.

## Configuration

There is no service-level configuration. The tool uses default brand
values baked into `DEFAULT_BRAND` in
[`pdf_generation_tool.py`](../backend/onyx/tools/tool_implementations/pdf_generation/pdf_generation_tool.py).
To change the default colors, fonts, or company name, edit the
`DEFAULT_BRAND` definition directly.

Per-request brand overrides are supported by the underlying
`BrandConfig` model but are not currently exposed through the tool's
LLM-facing schema; extending the schema to accept a `brand` object is a
straightforward follow-up.

## Customizing templates

The two templates in
[`templates/`](../backend/onyx/tools/tool_implementations/pdf_generation/templates/)
are standalone Jinja2 documents that include their own embedded CSS
(WeasyPrint supports CSS Paged Media Module for headers, footers, and
page counters). To customize:

- **Typography / color**: change the brand block at the top of each
  template's `<style>` section — it references `{{ brand.primary_color }}`,
  `{{ brand.secondary_color }}`, and `{{ brand.font_family }}`.
- **Layout**: edit the HTML structure directly. The cover page is a
  single `.cover-page` div; the TOC is a `<nav class="toc">`; each
  section is a `<section class="section">`.
- **Page size / margins**: edit the `@page` rule at the top of the
  `<style>` block.

Template tests live in
[`backend/tests/unit/onyx/tools/tool_implementations/pdf_generation/test_pdf_generation_tool.py`](../backend/tests/unit/onyx/tools/tool_implementations/pdf_generation/test_pdf_generation_tool.py)
and verify that both templates render with common feature combinations.

## Adding a company logo

The `BrandConfig` model accepts an optional `logo_base64` field. When
present, the templates render it as a `<img src="data:image/png;base64,...">`
on the cover page (report template) or in the header bar (brief
template). To wire in a permanent logo, set `DEFAULT_BRAND.logo_base64`
to the base64-encoded PNG string in `pdf_generation_tool.py`.

## Troubleshooting

### `OSError: cannot load library 'gobject-2.0-0'`

WeasyPrint cannot find the system libraries. Expected in a local macOS
venv without the underlying libs. Fix:

```bash
brew install cairo pango gdk-pixbuf libffi
```

The backend Docker image already installs these via
[`backend/Dockerfile`](../backend/Dockerfile) (`libcairo2`,
`libpango-1.0-0`, `libgdk-pixbuf-2.0-0`), so prod builds work out of
the box.

### Font substitution / boxes instead of characters

WeasyPrint falls back to whatever fonts are installed on the host. The
backend image ships with `fonts-liberation` and `fonts-dejavu`, which
cover Latin, Greek, and Cyrillic scripts. For non-Latin scripts (CJK,
Arabic, etc.), add the relevant font package to `backend/Dockerfile`.

### Tool missing from the assistant's tool list

1. Confirm the alembic migration has been applied:
   ```bash
   docker exec -it onyx-relational_db-1 psql -U postgres -c \
     "SELECT id, name, in_code_tool_id FROM tool WHERE in_code_tool_id = 'PdfGenerationTool';"
   ```
2. If the row exists but the tool still isn't showing, restart the
   `api_server` container so `construct_tools` picks up the new
   `BUILT_IN_TOOL_MAP` entry.
3. Check the API server logs for
   `PdfGenerationTool unavailable: weasyprint cannot be imported` —
   that means the Python package is installed but the system libs are
   missing on whichever host is running the server.
