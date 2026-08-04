"""add pinned_message_id to user_settings

Revision ID: 874cd872b6d4
Revises: c8a1471568ff
Create Date: 2026-08-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '874cd872b6d4'
down_revision: Union[str, Sequence[str], None] = 'c8a1471568ff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('user_settings', sa.Column('pinned_message_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('user_settings', 'pinned_message_id')
