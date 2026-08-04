"""add diary_id to theses

Revision ID: c8a1471568ff
Revises: f88ca8813259
Create Date: 2026-08-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8a1471568ff'
down_revision: Union[str, Sequence[str], None] = 'f88ca8813259'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # diary_id starts nullable so it can be backfilled from messages.diary_id below.
    op.add_column('theses', sa.Column('diary_id', sa.Integer(), nullable=True))

    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE theses SET diary_id = messages.diary_id "
            "FROM messages WHERE messages.id = theses.message_id"
        )
    )

    op.alter_column('theses', 'diary_id', nullable=False)
    op.create_index(op.f('ix_theses_diary_id'), 'theses', ['diary_id'], unique=False)
    op.create_foreign_key(None, 'theses', 'diaries', ['diary_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(None, 'theses', type_='foreignkey')
    op.drop_index(op.f('ix_theses_diary_id'), table_name='theses')
    op.drop_column('theses', 'diary_id')
