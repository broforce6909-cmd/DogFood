"""`app/email.py`'s pure send path: no test here touches a real network or a
real mail server. `send_email` never raises -- that is the property every
test in this file checks, one failure mode at a time.
"""

from __future__ import annotations

import smtplib

from app import email as email_module


def test_with_no_smtp_host_the_message_is_logged_not_sent(monkeypatch, caplog) -> None:
    monkeypatch.setattr(email_module.settings, "smtp_host", None)
    with caplog.at_level("INFO", logger="dogfood.email"):
        result = email_module.send_email(to="a@example.com", subject="Hi", body="Body text")
    assert result is True
    assert any("a@example.com" in r.message for r in caplog.records)
    assert any("Body text" in r.message for r in caplog.records)


def test_a_successful_smtp_send_returns_true(monkeypatch) -> None:
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = host
            sent["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, username, password):
            sent["login"] = (username, password)

        def send_message(self, message):
            sent["message"] = message

    monkeypatch.setattr(email_module.settings, "smtp_host", "smtp.example.test")
    monkeypatch.setattr(email_module.settings, "smtp_port", 1025)
    monkeypatch.setattr(email_module.settings, "smtp_username", "user")
    monkeypatch.setattr(email_module.settings, "smtp_password", "pass")
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    result = email_module.send_email(to="a@example.com", subject="Hi", body="Body")
    assert result is True
    assert sent["host"] == "smtp.example.test"
    assert sent["tls"] is True
    assert sent["login"] == ("user", "pass")
    assert sent["message"]["To"] == "a@example.com"
    assert sent["message"]["Subject"] == "Hi"


def test_an_smtp_failure_is_swallowed_not_raised(monkeypatch) -> None:
    class FailingSMTP:
        def __init__(self, host, port, timeout=None):
            pass

        def __enter__(self):
            raise smtplib.SMTPConnectError(421, "no connection")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(email_module.settings, "smtp_host", "smtp.example.test")
    monkeypatch.setattr(smtplib, "SMTP", FailingSMTP)

    result = email_module.send_email(to="a@example.com", subject="Hi", body="Body")
    assert result is False  # reported, not raised


def test_schedule_email_defers_to_a_background_task() -> None:
    calls = []

    class FakeBackground:
        def add_task(self, func, **kwargs):
            calls.append((func, kwargs))

    background = FakeBackground()
    email_module.schedule_email(background, to="a@example.com", subject="Hi", body="Body")
    assert len(calls) == 1
    func, kwargs = calls[0]
    assert func is email_module.send_email
    assert kwargs == {"to": "a@example.com", "subject": "Hi", "body": "Body"}
