"""audit_action user_created enum value

Revision ID: b4c1d8e7f302
Revises: a3f0e6b19d27

Same gap as every other enum-value addition in this project (see
`4bef2bf4c0a8`): `AuditAction.USER_CREATED` was added to the Python enum for
admin-provisioned accounts (`POST /api/users`), and `--autogenerate` does not
detect a new value on an existing native `ENUM` column.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b4c1d8e7f302'
down_revision: Union[str, Sequence[str], None] = 'a3f0e6b19d27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'user_created'")


def downgrade() -> None:
    pass
