"""certificate_kind organizing enum value

Revision ID: c5d2e9f1a614
Revises: b4c1d8e7f302

Same gap as every other enum-value addition in this project (see
`4bef2bf4c0a8`): `CertificateKind.ORGANIZING` was added to the Python enum
for admin-issued organizing certificates, and `--autogenerate` does not
detect a new value on an existing native `ENUM` column.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c5d2e9f1a614'
down_revision: Union[str, Sequence[str], None] = 'b4c1d8e7f302'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE certificate_kind ADD VALUE IF NOT EXISTS 'organizing'")


def downgrade() -> None:
    pass
