"""Role administration -- the part that makes the role model real.

Self-registration produces a participant. Everything above that comes from
here, and only an admin can hand it out.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import Role


def test_the_user_directory_is_staff_only(client: TestClient, make_user, auth) -> None:
    make_user(name="Somebody")
    assert client.get("/api/users").status_code == 403
    assert client.get("/api/users", headers=auth(make_user())).status_code == 403
    assert client.get("/api/users", headers=auth(make_user(Role.JUDGE))).status_code == 403
    assert client.get("/api/users", headers=auth(make_user(Role.ORGANIZER))).status_code == 200
    assert client.get("/api/users", headers=auth(make_user(Role.ADMIN))).status_code == 200


def test_only_an_admin_promotes(client: TestClient, make_user, auth) -> None:
    target = make_user()
    organizer, admin = make_user(Role.ORGANIZER), make_user(Role.ADMIN)

    denied = client.patch(
        f"/api/users/{target.id}/role", json={"role": "judge"}, headers=auth(organizer)
    )
    assert denied.status_code == 403

    allowed = client.patch(
        f"/api/users/{target.id}/role", json={"role": "judge"}, headers=auth(admin)
    )
    assert allowed.status_code == 200
    assert allowed.json()["role"] == "judge"


def test_a_user_cannot_promote_themselves(client: TestClient, make_user, auth) -> None:
    user = make_user()
    response = client.patch(
        f"/api/users/{user.id}/role", json={"role": "admin"}, headers=auth(user)
    )
    assert response.status_code == 403


def test_visitor_is_not_an_assignable_role(client: TestClient, make_user, auth) -> None:
    """It is the role of a request with no session. An account holding it would
    have less access than a stranger."""
    target = make_user()
    response = client.patch(
        f"/api/users/{target.id}/role",
        json={"role": "visitor"},
        headers=auth(make_user(Role.ADMIN)),
    )
    assert response.status_code == 422


def test_a_user_can_read_themselves_but_not_a_neighbour(
    client: TestClient, make_user, auth
) -> None:
    me, them = make_user(), make_user()
    assert client.get(f"/api/users/{me.id}", headers=auth(me)).status_code == 200
    assert client.get(f"/api/users/{them.id}", headers=auth(me)).status_code == 403


def test_deactivation_is_admin_only_and_takes_effect_at_once(
    client: TestClient, make_user, auth
) -> None:
    target = make_user()
    target_headers = auth(target)
    admin = make_user(Role.ADMIN)

    assert client.get("/api/auth/me", headers=target_headers).json()["authenticated"] is True
    assert (
        client.post(f"/api/users/{target.id}/deactivate", headers=auth(make_user(Role.ORGANIZER))).status_code
        == 403
    )
    assert client.post(f"/api/users/{target.id}/deactivate", headers=auth(admin)).status_code == 200
    assert client.get("/api/auth/me", headers=target_headers).json()["authenticated"] is False


def test_an_admin_cannot_lock_themselves_out(client: TestClient, make_user, auth) -> None:
    admin = make_user(Role.ADMIN)
    response = client.post(f"/api/users/{admin.id}/deactivate", headers=auth(admin))
    assert response.status_code == 409


# --------------------------------------------------------------------------- #
# Admin-provisioned accounts (Phase 6): the *other* path to judge/organizer,
# for someone who should never touch the public registration form.
# --------------------------------------------------------------------------- #


def _sent_emails(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.routers.users.schedule_email",
        lambda background, **kwargs: sent.append(kwargs),
    )
    return sent


def test_an_admin_can_provision_a_judge_account(
    client: TestClient, auth, make_user, monkeypatch
) -> None:
    sent = _sent_emails(monkeypatch)
    admin = make_user(Role.ADMIN)
    response = client.post(
        "/api/users",
        json={"email": "new.judge@example.com", "display_name": "New Judge", "role": "judge"},
        headers=auth(admin),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["role"] == "judge"
    assert body["email"] == "new.judge@example.com"

    # The account can actually sign in with whatever password was emailed --
    # never returned by the API, so recover it from the recorded email.
    assert len(sent) == 1
    assert sent[0]["to"] == "new.judge@example.com"
    password = sent[0]["body"].split("Temporary password: ")[1].splitlines()[0]
    login = client.post(
        "/api/auth/login", json={"email": "new.judge@example.com", "password": password}
    )
    assert login.status_code == 200, login.text


def test_provisioning_an_account_is_admin_only(
    client: TestClient, auth, make_user, monkeypatch
) -> None:
    _sent_emails(monkeypatch)
    payload = {"email": "x@example.com", "display_name": "X", "role": "judge"}
    assert client.post("/api/users", json=payload).status_code == 403
    assert (
        client.post("/api/users", json=payload, headers=auth(make_user())).status_code == 403
    )
    assert (
        client.post(
            "/api/users", json=payload, headers=auth(make_user(Role.ORGANIZER))
        ).status_code
        == 403
    )


def test_provisioning_a_visitor_role_is_rejected(
    client: TestClient, auth, make_user, monkeypatch
) -> None:
    _sent_emails(monkeypatch)
    response = client.post(
        "/api/users",
        json={"email": "x@example.com", "display_name": "X", "role": "visitor"},
        headers=auth(make_user(Role.ADMIN)),
    )
    assert response.status_code == 422


def test_provisioning_a_duplicate_email_is_a_conflict(
    client: TestClient, auth, make_user, monkeypatch
) -> None:
    _sent_emails(monkeypatch)
    existing = make_user(email="taken@example.com")
    response = client.post(
        "/api/users",
        json={"email": existing.email, "display_name": "Someone Else", "role": "judge"},
        headers=auth(make_user(Role.ADMIN)),
    )
    assert response.status_code == 409


def test_the_generated_password_never_appears_in_the_audit_log_or_response(
    client: TestClient, auth, make_user, monkeypatch
) -> None:
    sent = _sent_emails(monkeypatch)
    admin = make_user(Role.ADMIN)
    response = client.post(
        "/api/users",
        json={
            "email": "new.organizer@example.com",
            "display_name": "New Organizer",
            "role": "organizer",
        },
        headers=auth(admin),
    )
    password = sent[0]["body"].split("Temporary password: ")[1].splitlines()[0]
    assert password not in str(response.json())

    rows = client.get("/api/audit", headers=auth(admin)).json()["rows"]
    entry = next(r for r in rows if r["action"] == "user_created")
    assert password not in entry["summary"]
    assert "new.organizer@example.com" in entry["summary"]


def test_self_registration_cannot_produce_a_judge_or_organizer_role(
    client: TestClient
) -> None:
    """The real boundary this whole feature depends on: no input to the
    *public* registration endpoint can result in anything but `participant`,
    including a smuggled `role` field the schema does not even declare."""
    response = client.post(
        "/api/auth/register",
        json={
            "email": "sneaky@example.com",
            "display_name": "Sneaky",
            "password": "a-real-password-1",
            "role": "admin",  # not a field RegisterIn declares -- must be ignored, not honored
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["user"]["role"] == "participant"
