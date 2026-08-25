"""field mappings unique constraint

Revision ID: a91ac556e233
Revises: aebdd3c856df
Create Date: 2026-08-25 17:06:47.049128

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a91ac556e233'
down_revision: Union[str, None] = 'aebdd3c856df'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "ux_field_mappings_source",
        "field_mappings",
        ["business_id", "source_provider", "source_field", "version"],
    )


def downgrade() -> None:
    op.drop_constraint("ux_field_mappings_source", "field_mappings", type_="unique")
