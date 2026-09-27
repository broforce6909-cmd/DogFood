"""audit_action registration_removed enum value

Revision ID: d4f8a1c72e5b
Revises: a8e2c6f19b3d

Same gap as `4bef2bf4c0a8`/`a8e2c6f19b3d`: `REGISTRATION_REMOVED` was added to
the Python `AuditAction` enum for the new `/registrations/{id}/remove` route.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd4f8a1c72e5b'
down_revision: Union[str, Sequence[str], None] = 'a8e2c6f19b3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'registration_removed'")


def downgrade() -> None:
    pass
