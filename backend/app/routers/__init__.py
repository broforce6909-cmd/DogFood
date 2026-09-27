"""Feature routers, mounted in `app.main`.

Every router in here follows the same shape: load the resource, call
`require_access()`, then do the work. Anything that does not follow that shape
is a bug.
"""

from . import (
    audit,
    auth,
    certificates,
    comments,
    embed,
    events,
    exports,
    gallery,
    imports,
    judges,
    judging,
    results,
    submissions,
    teams,
    users,
    voting,
    webhooks,
)

__all__ = [
    "audit",
    "auth",
    "certificates",
    "comments",
    "embed",
    "events",
    "exports",
    "gallery",
    "imports",
    "judges",
    "judging",
    "results",
    "submissions",
    "teams",
    "users",
    "voting",
    "webhooks",
]
