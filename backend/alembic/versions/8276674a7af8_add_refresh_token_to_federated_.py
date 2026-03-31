"""add refresh_token to federated_connector_oauth_token

Revision ID: 8276674a7af8
Revises: b728689f45b1
Create Date: 2026-03-31 08:34:26.418396

"""

from alembic import op
import sqlalchemy as sa

from onyx.db.models import EncryptedString


# revision identifiers, used by Alembic.
revision = "8276674a7af8"
down_revision = "b728689f45b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "federated_connector_oauth_token",
        sa.Column("refresh_token", EncryptedString(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("federated_connector_oauth_token", "refresh_token")
