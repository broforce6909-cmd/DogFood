"""One pagination implementation, shared by every list endpoint large enough
to need it, rather than each hand-rolling its own offset/limit/count arithmetic
that could quietly drift from the others.

`GalleryPage` (schemas.py) predates this and is left alone deliberately -- it
already works, is already tested, and changing it buys nothing functional. This
module is for the endpoints that had no pagination at all: `/submissions`,
`/assignments`, `/teams`, `/users`, `/comments`, the organizer's `/certificates`
list, and `/webhooks/{id}/deliveries`. Every one of the seven follows this exact
shape now, so a frontend that has learned to page one has learned to page all.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

DEFAULT_PER_PAGE = 50
MAX_PER_PAGE = 200

ItemT = TypeVar("ItemT")


class Page(BaseModel, Generic[ItemT]):
    items: list[ItemT]
    total: int
    page: int
    per_page: int
    pages: int


class PageParams:
    """`Depends(PageParams)` gives every route the same two query params, with
    the same validation, so `?page=` and `?per_page=` mean the same thing and
    have the same limits everywhere they appear."""

    def __init__(
        self,
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=DEFAULT_PER_PAGE, ge=1, le=MAX_PER_PAGE),
    ) -> None:
        self.page = page
        self.per_page = per_page

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page


def count_of(db: Session, stmt: Select) -> int:
    """The total row count a filtered-but-not-yet-paged statement would return.

    A separate `SELECT count(*)` over the same filters, not `len(rows)` after
    the fact -- the whole point of paging is to never materialise every row.
    """
    return db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()


def pages_for(total: int, per_page: int) -> int:
    """Ceiling division without importing math.ceil for one call site.
    `max(1, ...)` so an empty collection is still "page 1 of 1", not "of 0" --
    there is a page to look at, it is just empty.
    """
    return max(1, -(-total // per_page))
