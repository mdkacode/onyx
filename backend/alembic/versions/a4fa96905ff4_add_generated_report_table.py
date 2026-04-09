"""add generated_report table

Revision ID: a4fa96905ff4
Revises: 8276674a7af8
Create Date: 2026-04-09 13:28:34.585010

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = "a4fa96905ff4"
down_revision = "8276674a7af8"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_table("generated_report")
