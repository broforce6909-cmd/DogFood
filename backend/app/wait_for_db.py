"""Block until Postgres accepts connections, or give up loudly.

Compose gates `api` on the db healthcheck already; this is belt-and-braces for
cold laptops where the first Postgres boot (initdb) runs long.
"""

from __future__ import annotations

import sys
import time

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .db import engine

MAX_ATTEMPTS = 60
DELAY_SECONDS = 1.0


def main() -> int:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            print(f"[wait_for_db] database ready after {attempt} attempt(s)")
            return 0
        except SQLAlchemyError as exc:
            if attempt == MAX_ATTEMPTS:
                print(f"[wait_for_db] giving up after {attempt} attempts: {exc}", file=sys.stderr)
                return 1
            time.sleep(DELAY_SECONDS)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
