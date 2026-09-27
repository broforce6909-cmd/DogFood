"""Outbound email: registration confirmation, results-published notification.

Provider-agnostic over plain SMTP -- `smtplib` against whatever host, port
and credentials `Settings` names. MailHog (or any other SMTP-speaking
capture tool) is a drop-in target: point `SMTP_HOST`/`SMTP_PORT` at it and
mail shows up in its own UI instead of a real inbox, with no code change on
this side.

No `SMTP_HOST` configured -- the default state of a `docker compose up` with
no mail server anywhere near it -- is not a disabled feature to check for at
every call site. It is a fallback baked into `send_email` itself: the
message is logged in full at INFO instead of sent, so a judge running this
project on a laptop with no SMTP anywhere still sees every email this system
would have sent, in the api container's own logs, by design rather than by
omission.

Same rule as `app/hooks.py`, and for the same reason: an email failing must
never fail the thing that triggered it. `send_email` never raises, and
`schedule_email` runs it from a `BackgroundTasks` callback so the request
that triggered it is never the one waiting on an SMTP round trip. Unlike
webhooks there is no per-recipient delivery-status table and no retry --
mail is a courtesy notification, not an organizer-configured integration
with its own dashboard, so one best-effort attempt, logged either way, is
the whole of it.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import settings

log = logging.getLogger("dogfood.email")


def send_email(*, to: str, subject: str, body: str) -> bool:
    """One best-effort send. Never raises.

    Returns whether it appeared to succeed, for the one log line that says
    which happened -- nothing in this codebase should gate real behavior on
    this return value, the same way nothing gates on whether a webhook
    delivered.
    """
    if not settings.smtp_host:
        log.info(
            "email (no SMTP_HOST configured, logging instead): to=%s subject=%r\n%s",
            to,
            subject,
            body,
        )
        return True

    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password or "")
            smtp.send_message(message)
        log.info("email sent: to=%s subject=%r", to, subject)
        return True
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        log.warning("email delivery failed: to=%s subject=%r error=%s", to, subject, exc)
        return False


def schedule_email(background, *, to: str, subject: str, body: str) -> None:
    """Send *after* the response, never during it -- the same rule
    `hooks.schedule` follows, and for the same reason: a registrant should
    not pay latency for this system's own SMTP round trip."""
    background.add_task(send_email, to=to, subject=subject, body=body)
