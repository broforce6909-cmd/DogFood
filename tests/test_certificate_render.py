"""Visual certificates: `GET /api/certificates/{code}/pdf` and `/png`.

WeasyPrint links Pango/GObject at the OS level (see `backend/Dockerfile`), which
a typical Windows dev machine does not have and is not expected to install --
the whole reason the backend README's test command runs on any machine that
has Python and Postgres. So this file tests the *route*, not the rendering
library, with `render_pdf`/`render_png` mocked out: the right certificate is
looked up, the right verdict (signature/revoked) is computed and handed to the
renderer, 404s and content types are right. One real, unmocked render is
included, skipped on a machine where WeasyPrint's native libraries are not
installed rather than failing the suite over an environment fact this file did
not create -- the same shape as this project's own Postgres-unreachable skip.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.models import AssignmentStatus, Role, SubmissionStatus, utcnow


def _weasyprint_available() -> bool:
    try:
        from app.certificate_render import render_pdf  # noqa: F401
        import weasyprint  # noqa: F401

        weasyprint.HTML(string="<p>probe</p>").write_pdf()
    except Exception:
        return False
    return True


WEASYPRINT_AVAILABLE = _weasyprint_available()


@pytest.fixture()
def finished_event(
    db, make_event, make_user, make_team, make_submission, make_judge, make_assignment,
    make_criterion, make_score,
):
    def _build():
        event = make_event(deadline=utcnow() - timedelta(hours=1))
        event.judging_opens_at = utcnow() - timedelta(days=1)
        event.judging_closes_at = utcnow() - timedelta(hours=1)
        db.commit()
        criterion = make_criterion(event, key="overall", name="Overall", weight=1)

        participant = make_user(Role.PARTICIPANT, name="Wei Zhang")
        team = make_team(event, participant, name="Quorum Collective")
        submission = make_submission(team, name="Quorum", status=SubmissionStatus.SUBMITTED)

        judge_user = make_user(Role.JUDGE, name="Priya Rivera")
        judge = make_judge(event, judge_user)
        ballot = make_assignment(judge, submission, status=AssignmentStatus.COMPLETE)
        make_score(ballot, criterion, 5)

        return {"event": event, "organizer": make_user(Role.ORGANIZER)}

    return _build


def _issue_one(client: TestClient, auth, finished_event) -> str:
    s = finished_event()
    headers = auth(s["organizer"])
    issued = client.post(f"/api/events/{s['event'].slug}/certificates/issue", headers=headers, json={})
    code = issued.json()["certificates"][0]["code"]
    return s, headers, code


# --------------------------------------------------------------------------- #
# Route behaviour, rendering mocked out
# --------------------------------------------------------------------------- #


def test_pdf_route_is_public_and_returns_the_right_content_type(
    client: TestClient, auth, finished_event
):
    _s, _headers, code = _issue_one(client, auth, finished_event)

    with patch("app.routers.certificates.render_pdf", return_value=b"%PDF-fake") as mocked:
        r = client.get(f"/api/certificates/{code}/pdf")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == b"%PDF-fake"
    mocked.assert_called_once()


def test_png_route_is_public_and_returns_the_right_content_type(
    client: TestClient, auth, finished_event
):
    _s, _headers, code = _issue_one(client, auth, finished_event)

    with patch("app.routers.certificates.render_png", return_value=b"\x89PNG-fake") as mocked:
        r = client.get(f"/api/certificates/{code}/png")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.content == b"\x89PNG-fake"
    mocked.assert_called_once()


def test_an_unknown_code_is_a_404_for_both_formats(client: TestClient):
    assert client.get("/api/certificates/NOTAREALCODE/pdf").status_code == 404
    assert client.get("/api/certificates/NOTAREALCODE/png").status_code == 404


def test_a_revoked_record_still_renders_but_is_told_so(
    client: TestClient, auth, finished_event
):
    """The same honesty rule as the JSON endpoint: a revoked record does not
    quietly look genuine just because it is prettier now."""
    s, headers, _ = None, None, None
    s = finished_event()
    headers = auth(s["organizer"])
    cert = client.post(
        f"/api/events/{s['event'].slug}/certificates/issue", headers=headers, json={}
    ).json()["certificates"][0]
    client.post(
        f"/api/events/{s['event'].slug}/certificates/{cert['id']}/revoke",
        headers=headers,
        json={"reason": "issued to the wrong person"},
    )

    with patch("app.routers.certificates.render_pdf", return_value=b"%PDF-fake") as mocked:
        r = client.get(f"/api/certificates/{cert['code']}/pdf")

    assert r.status_code == 200, r.text
    _cert_arg, kwargs = mocked.call_args
    assert kwargs["revoked"] is True


def test_render_is_handed_the_actual_signature_verdict(client: TestClient, auth, finished_event):
    """Wires the render call to the same `_load_and_check` the JSON endpoint
    uses, rather than a second, independently-computed verdict that could drift
    from it."""
    _s, _headers, code = _issue_one(client, auth, finished_event)
    public = client.get(f"/api/certificates/{code}").json()

    with patch("app.routers.certificates.render_pdf", return_value=b"%PDF-fake") as mocked:
        client.get(f"/api/certificates/{code}/pdf")

    _cert_arg, kwargs = mocked.call_args
    assert kwargs["signature_valid"] == public["signature_valid"]
    assert kwargs["revoked"] == public["revoked"]
    assert kwargs["payload"]["subject"] == "Wei Zhang" or "subject" in kwargs["payload"]


# --------------------------------------------------------------------------- #
# One real render, skipped where WeasyPrint's native libraries are not installed
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    not WEASYPRINT_AVAILABLE,
    reason="WeasyPrint needs Pango/GObject at the OS level; not installed on this machine",
)
def test_a_real_pdf_and_png_are_produced(client: TestClient, auth, finished_event):
    _s, _headers, code = _issue_one(client, auth, finished_event)

    pdf = client.get(f"/api/certificates/{code}/pdf")
    assert pdf.status_code == 200, pdf.text
    assert pdf.content.startswith(b"%PDF")

    png = client.get(f"/api/certificates/{code}/png")
    assert png.status_code == 200, png.text
    assert png.content.startswith(b"\x89PNG\r\n\x1a\n")


# --------------------------------------------------------------------------- #
# What the rows say (pure -- no WeasyPrint needed)
# --------------------------------------------------------------------------- #


def _cert(kind_value: str):
    from types import SimpleNamespace

    from app.models import CertificateKind

    return SimpleNamespace(kind=CertificateKind(kind_value))


def test_a_team_named_team_something_is_not_said_twice() -> None:
    from app.certificate_render import _team_display

    assert _team_display("Team Switchyard") == "Switchyard"
    assert _team_display("team  Switchyard") == "Switchyard"
    # Only a leading whole word "Team", and only when something remains.
    assert _team_display("Teamwork Labs") == "Teamwork Labs"
    assert _team_display("Team") == "Team"
    assert _team_display("Quorum Collective") == "Quorum Collective"


def test_a_participation_record_shows_the_project_then_the_team() -> None:
    from app.certificate_render import _extra_rows

    rows = _extra_rows(
        _cert("participation"), {"team": "Team Switchyard", "project": "Switchyard"}
    )
    assert rows == [("Project", "Switchyard"), ("Team", "Switchyard")]


def test_a_participation_record_from_before_the_project_was_signed_still_renders() -> None:
    from app.certificate_render import _extra_rows

    assert _extra_rows(_cert("participation"), {"team": "Quorum Collective"}) == [
        ("Team", "Quorum Collective")
    ]
