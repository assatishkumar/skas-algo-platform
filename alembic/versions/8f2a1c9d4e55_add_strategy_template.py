"""add strategy_template (per-strategy default backtest params)

Revision ID: 8f2a1c9d4e55
Revises: 43291c1c119b
Create Date: 2026-06-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8f2a1c9d4e55'
down_revision: Union[str, None] = '43291c1c119b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'strategy_template',
        sa.Column('strategy_id', sa.String(length=64), primary_key=True),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(length=128), nullable=True),
        sa.Column('capital', sa.Float(), nullable=False, server_default='0'),
        sa.Column('params', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('strategy_template')
