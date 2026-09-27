"""admin levels: owner / manager / auditor

Revision ID: a9c4d7e1f368
Revises: f8b3c6d0e257

Adds `users.admin_level`. Existing rows take the server default, `owner`, so
every admin that existed before levels did keeps every power it had. The column
is only meaningful while `role = 'admin'`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9c4d7e1f368'
down_revision: Union[str, Sequence[str], None] = 'f8b3c6d0e257'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

admin_level = sa.Enum('owner', 'manager', 'auditor', name='admin_level')


def upgrade() -> None:
    admin_level.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'users',
        sa.Column('admin_level', admin_level, server_default='owner', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('users', 'admin_level')
    admin_level.drop(op.get_bind(), checkfirst=True)
