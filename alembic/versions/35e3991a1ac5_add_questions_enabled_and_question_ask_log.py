"""add questions_enabled and question_ask_log

Revision ID: 35e3991a1ac5
Revises: 874cd872b6d4
Create Date: 2026-08-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '35e3991a1ac5'
down_revision: Union[str, Sequence[str], None] = '874cd872b6d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'user_settings',
        sa.Column(
            'questions_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False
        ),
    )
    op.create_table(
        'question_ask_log',
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('question_key', sa.Text(), nullable=False),
        sa.Column('last_asked_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('user_id', 'question_key'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('question_ask_log')
    op.drop_column('user_settings', 'questions_enabled')
