"""Structured logging: JSON lines, and a request id threaded through every one
of them for the duration of a request.

Why JSON rather than the plain `%(asctime)s %(levelname)s ...` line this
project shipped with through T4: a log line nobody can machine-parse is a log
line an operator greps by hand in a hurry. `docker compose logs api | jq` (or
CloudWatch/Loki/whatever ingests it in a real deployment) needing a real
parser rather than a regex is the entire point.

Why a `ContextVar` rather than passing `request_id` through every function
call: nothing else in this codebase's call chain — `record()`, a router's own
`log.info`, a background task — has a reason to accept a `request_id`
parameter it would otherwise never need. A context-local variable, read by a
`logging.Filter` at the point a record is emitted, gets every log statement
correlated to the request that caused it without threading a new parameter
through every signature in the codebase.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# A request id is either generated here or taken from an incoming
# `X-Request-ID` header (a caller, or a proxy in front of this service, may
# already have one it wants correlated). Capped and character-restricted
# before it ever reaches a log line or a response header: an attacker-supplied
# header is untrusted input, and log injection (a value containing a newline,
# forged to look like a second log entry) is a real, cheap attack against
# anything that logs a request header verbatim.
_MAX_REQUEST_ID_LEN = 128
_SAFE_REQUEST_ID = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)


def clean_request_id(value: str | None) -> str | None:
    """A caller-supplied id, kept only if it cannot smuggle anything into a
    log line or a header. `None` if it's missing or fails the check --
    the caller generates a fresh one in that case, never silently sanitises
    the untrusted value into something else."""
    if not value or len(value) > _MAX_REQUEST_ID_LEN:
        return None
    if not all(ch in _SAFE_REQUEST_ID for ch in value):
        return None
    return value


def new_request_id() -> str:
    return uuid.uuid4().hex


class RequestIdFilter(logging.Filter):
    """Stamps the current request id (or `-` outside any request, e.g. at
    startup) onto every record, so the formatter below always has something
    to read regardless of where the log call happened."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Deliberately flat (no nested objects) --
    every common log shipper's default parser handles a flat line without
    configuration, and this project has nothing that needs a nested shape."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Call once, at process startup, before anything else logs.

    Replaces the plain-text `logging.basicConfig` this project shipped with:
    same root logger, same handler count (one), different formatter and one
    filter that makes the request id available to it.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
