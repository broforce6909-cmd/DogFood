"""event archival: events.archived_at

Revision ID: b1d5e8a2c479
Revises: a9c4d7e1f368

Nullable, no default: every existing event stays active, exactly as it was.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1d5e8a2c479'
down_revision: Union[str, Sequence[str], None] = 'a9c4d7e1f368'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('events', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('events', 'archived_at')
