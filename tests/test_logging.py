"""Structured logging and the request id threaded through it.

`app/logging_config.py` is pure functions and classes over the standard
library `logging` module, so most of this is tested directly against it, no
HTTP needed. The two properties that only exist once a real request happens
(the id round-tripping through a header, one id per request) go through the
`client` fixture instead.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.logging_config import (  # noqa: E402
    JsonFormatter,
    RequestIdFilter,
    clean_request_id,
    new_request_id,
    request_id_var,
)


def _record(message: str = "hello", **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="dogfood.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=message, args=(), exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# --------------------------------------------------------------------------- #
# clean_request_id: untrusted input from a header
# --------------------------------------------------------------------------- #


def test_a_normal_id_passes_through_unchanged() -> None:
    assert clean_request_id("abc-123.def_456") == "abc-123.def_456"


def test_none_and_empty_are_rejected() -> None:
    assert clean_request_id(None) is None
    assert clean_request_id("") is None


def test_an_overlong_id_is_rejected() -> None:
    assert clean_request_id("a" * 129) is None
    assert clean_request_id("a" * 128) == "a" * 128


def test_a_newline_cannot_be_smuggled_into_a_log_line() -> None:
    """The entire reason this function exists: a header value containing a
    newline, forged to look like a second log entry, must never reach a log
    line or a response header verbatim."""
    assert clean_request_id("legit-id\nlevel=ERROR fake entry") is None


def test_other_control_and_special_characters_are_rejected() -> None:
    for bad in ("has space", "has\ttab", "has\rcr", 'has"quote', "has<tag>"):
        assert clean_request_id(bad) is None


def test_new_request_id_is_safe_by_construction() -> None:
    generated = new_request_id()
    assert clean_request_id(generated) == generated


# --------------------------------------------------------------------------- #
# RequestIdFilter and JsonFormatter
# --------------------------------------------------------------------------- #


def test_the_filter_stamps_the_current_context_var() -> None:
    token = request_id_var.set("req-abc")
    try:
        record = _record()
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "req-abc"
    finally:
        request_id_var.reset(token)


def test_the_filter_falls_back_to_a_dash_outside_any_request() -> None:
    record = _record()
    RequestIdFilter().filter(record)
    assert record.request_id == "-"


def test_json_formatter_produces_one_parseable_object_per_line() -> None:
    record = _record("something happened", request_id="req-xyz")
    line = JsonFormatter().format(record)
    payload = json.loads(line)  # must not raise
    assert payload["message"] == "something happened"
    assert payload["request_id"] == "req-xyz"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "dogfood.test"
    assert "ts" in payload


def test_json_formatter_includes_a_traceback_when_present() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="dogfood.test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError" in payload["exc_info"]
    assert "boom" in payload["exc_info"]


# --------------------------------------------------------------------------- #
# End to end: a real request gets a real id
# --------------------------------------------------------------------------- #


def test_a_response_carries_an_x_request_id_header(client: TestClient):
    r = client.get("/health")
    assert r.headers["x-request-id"]


def test_two_requests_get_two_different_ids(client: TestClient):
    a = client.get("/health").headers["x-request-id"]
    b = client.get("/health").headers["x-request-id"]
    assert a != b


def test_a_caller_supplied_request_id_is_honoured_when_safe(client: TestClient):
    r = client.get("/health", headers={"x-request-id": "caller-chosen-id-123"})
    assert r.headers["x-request-id"] == "caller-chosen-id-123"


def test_an_unsafe_caller_supplied_request_id_is_replaced(client: TestClient):
    r = client.get("/health", headers={"x-request-id": "bad id with spaces"})
    assert r.headers["x-request-id"] != "bad id with spaces"
    assert clean_request_id(r.headers["x-request-id"]) == r.headers["x-request-id"]
