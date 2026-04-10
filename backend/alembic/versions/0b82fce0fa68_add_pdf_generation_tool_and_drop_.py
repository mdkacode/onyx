"""add_pdf_generation_tool_and_drop_generated_report

Revision ID: 0b82fce0fa68
Revises: 5ab06e5fcd89
Create Date: 2026-04-10 15:55:04.532742

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = "0b82fce0fa68"
down_revision = "5ab06e5fcd89"
branch_labels = None
depends_on = None


PDF_GENERATION_TOOL = {
    "name": "generate_pdf",
    "display_name": "PDF Generation",
    "description": (
        "Generates a professional, downloadable PDF document from structured "
        "content. Use when the user explicitly asks to create, export, save, "
        "or download a PDF report, document, brief, or summary."
    ),
    "in_code_tool_id": "PdfGenerationTool",
    "enabled": True,
}


def upgrade() -> None:
    # ── 1. Drop the legacy generated_report table ───────────────────────────
    # The prior markdown-based PDF feature has been replaced by the
    # built-in PdfGenerationTool, which persists files via the standard
    # Onyx file_store abstraction rather than a dedicated table.
    op.execute("DROP TABLE IF EXISTS generated_report CASCADE")

    # ── 2. Seed the PdfGenerationTool built-in tool row ─────────────────────
    conn = op.get_bind()

    existing = conn.execute(
        sa.text("SELECT id FROM tool WHERE in_code_tool_id = :in_code_tool_id"),
        {"in_code_tool_id": PDF_GENERATION_TOOL["in_code_tool_id"]},
    ).fetchone()

    if existing:
        conn.execute(
            sa.text(
                """
                UPDATE tool
                SET name = :name,
                    display_name = :display_name,
                    description = :description,
                    enabled = :enabled
                WHERE in_code_tool_id = :in_code_tool_id
                """
            ),
            PDF_GENERATION_TOOL,
        )
        tool_id = existing[0]
    else:
        result = conn.execute(
            sa.text(
                """
                INSERT INTO tool (name, display_name, description, in_code_tool_id, enabled)
                VALUES (:name, :display_name, :description, :in_code_tool_id, :enabled)
                RETURNING id
                """
            ),
            PDF_GENERATION_TOOL,
        )
        tool_id = result.scalar_one()

    # ── 3. Attach to every existing persona so users get it by default ──────
    conn.execute(
        sa.text(
            """
            INSERT INTO persona__tool (persona_id, tool_id)
            SELECT id, :tool_id FROM persona
            ON CONFLICT DO NOTHING
            """
        ),
        {"tool_id": tool_id},
    )


def downgrade() -> None:
    conn = op.get_bind()
    in_code_tool_id = PDF_GENERATION_TOOL["in_code_tool_id"]

    # Detach from all personas
    conn.execute(
        sa.text(
            """
            DELETE FROM persona__tool
            WHERE tool_id IN (
                SELECT id FROM tool WHERE in_code_tool_id = :in_code_tool_id
            )
            """
        ),
        {"in_code_tool_id": in_code_tool_id},
    )
    conn.execute(
        sa.text("DELETE FROM tool WHERE in_code_tool_id = :in_code_tool_id"),
        {"in_code_tool_id": in_code_tool_id},
    )

    # Recreate the legacy generated_report table so a downgrade leaves the
    # schema in its previous shape.
    op.create_table(
        "generated_report",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("s3_object_key", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
