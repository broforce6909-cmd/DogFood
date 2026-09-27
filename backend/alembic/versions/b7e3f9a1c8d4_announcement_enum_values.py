"""announcement_posted audit_action enum value

Revision ID: b7e3f9a1c8d4
Revises: 2d9d9863c330

Same gap as every other enum-value addition in this project (see
`4bef2bf4c0a8`): `AuditAction.ANNOUNCEMENT_POSTED` was added to the Python
enum for the new announcements feature, and `--autogenerate` does not detect
a new value on an existing native `ENUM` column.

`WebhookEvent.ANNOUNCEMENT_POSTED` needs no equivalent migration:
`WebhookEvent` is never stored as a native Postgres `ENUM` -- `Webhook.topics`
is a plain `ARRAY(Text)` and `WebhookDelivery.topic` a plain `String(60)`, so
there is no Postgres-side type for a new topic string to be rejected by.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b7e3f9a1c8d4'
down_revision: Union[str, Sequence[str], None] = '2d9d9863c330'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'announcement_posted'")


def downgrade() -> None:
    pass
