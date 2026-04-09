"""add naarni_user_token table

Revision ID: 5ab06e5fcd89
Revises: a4fa96905ff4
Create Date: 2026-04-09 13:59:41.384508

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from onyx.db.models import EncryptedString


# revision identifiers, used by Alembic.
revision = "5ab06e5fcd89"
down_revision = "a4fa96905ff4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "naarni_user_token",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
            index=True,
        ),
        sa.Column("phone_number", sa.String(), nullable=False),
        sa.Column("naarni_device_id", sa.Integer(), nullable=False),
        sa.Column("access_token", EncryptedString(), nullable=False),
        sa.Column("refresh_token", EncryptedString(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("naarni_user_token")
