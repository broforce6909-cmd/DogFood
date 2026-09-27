"""Authentication and sessions, over HTTP."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Role, User, UserSession
from conftest import PASSWORD


def test_register_creates_a_participant_and_signs_them_in(client: TestClient) -> None:
    response = client.post(
        "/api/auth/register",
        json={"email": "New.Person@Example.com", "display_name": "New Person", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["user"]["role"] == Role.PARTICIPANT.value
    # Stored lower-cased, so the unique index is the whole duplicate defence.
    assert body["user"]["email"] == "new.person@example.com"
    assert body["token"]
    assert settings.session_cookie_name in response.cookies


def test_registration_cannot_pick_its_own_role(client: TestClient) -> None:
    """An unknown field is ignored rather than honoured -- there is no code path
    from self-registration to a privileged role."""
    response = client.post(
        "/api/auth/register",
        json={
            "email": "sneaky@example.com",
            "display_name": "Sneaky",
            "password": PASSWORD,
            "role": "admin",
        },
    )
    assert response.status_code == 201
    assert response.json()["user"]["role"] == Role.PARTICIPANT.value


def test_duplicate_email_is_a_conflict(client: TestClient, make_user) -> None:
    existing = make_user()
    response = client.post(
        "/api/auth/register",
        json={"email": existing.email, "display_name": "Twin", "password": PASSWORD},
    )
    assert response.status_code == 409


def test_short_passwords_are_refused(client: TestClient) -> None:
    response = client.post(
        "/api/auth/register",
        json={"email": "short@example.com", "display_name": "Short", "password": "abc"},
    )
    assert response.status_code == 422


def test_login_failures_are_indistinguishable(client: TestClient, make_user) -> None:
    """Wrong password and unknown account must answer identically, or this
    endpoint becomes an account-enumeration oracle."""
    user = make_user()
    wrong_password = client.post(
        "/api/auth/login", json={"email": user.email, "password": "not-the-password"}
    )
    no_such_user = client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )

    assert wrong_password.status_code == no_such_user.status_code == 401
    assert wrong_password.json() == no_such_user.json()


def test_the_password_is_never_stored_in_the_clear(db: Session, make_user) -> None:
    user = make_user()
    assert PASSWORD not in user.password_hash
    assert user.password_hash.startswith("$argon2")


def test_the_session_token_is_never_stored_in_the_clear(
    client: TestClient, db: Session, make_user, auth
) -> None:
    """A database dump must not yield usable cookies."""
    user = make_user()
    token = auth(user)["Authorization"].removeprefix("Bearer ")
    rows = db.query(UserSession).all()
    assert len(rows) == 1
    assert rows[0].token_hash != token
    assert token not in rows[0].token_hash


def test_me_answers_for_a_visitor_too(client: TestClient) -> None:
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json() == {"authenticated": False, "user": None, "role": "visitor"}


def test_me_identifies_the_caller(client: TestClient, make_user, auth) -> None:
    user = make_user(Role.JUDGE, name="Judge Fixture")
    body = client.get("/api/auth/me", headers=auth(user)).json()
    assert body["authenticated"] is True
    assert body["role"] == "judge"
    assert body["user"]["display_name"] == "Judge Fixture"


def test_a_garbage_token_is_simply_anonymous(client: TestClient) -> None:
    body = client.get("/api/auth/me", headers={"Authorization": "Bearer nonsense"}).json()
    assert body["authenticated"] is False


def test_logout_deletes_the_session_row(
    client: TestClient, db: Session, make_user, auth
) -> None:
    user = make_user()
    headers = auth(user)
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    assert db.query(UserSession).count() == 0
    # And the token is dead immediately, not at expiry.
    assert client.get("/api/auth/me", headers=headers).json()["authenticated"] is False


def test_a_user_can_list_and_revoke_their_own_sessions(
    client: TestClient, make_user, auth
) -> None:
    user = make_user()
    first = auth(user)
    second = auth(user)

    sessions = client.get("/api/auth/sessions", headers=second).json()
    assert len(sessions) == 2

    target = sessions[0]["id"]
    assert client.delete(f"/api/auth/sessions/{target}", headers=second).status_code == 204
    assert len(client.get("/api/auth/sessions", headers=second).json()) == 1
    # One of the two tokens is now dead; the other still works.
    live = [h for h in (first, second) if client.get("/api/auth/me", headers=h).json()["authenticated"]]
    assert len(live) == 1


def test_one_user_cannot_revoke_anothers_session(client: TestClient, make_user, auth) -> None:
    victim, attacker = make_user(), make_user()
    victim_session = client.get("/api/auth/sessions", headers=auth(victim)).json()[0]["id"]

    response = client.delete(f"/api/auth/sessions/{victim_session}", headers=auth(attacker))
    assert response.status_code == 404


def test_changing_a_password_ends_every_other_session(
    client: TestClient, make_user, auth
) -> None:
    user = make_user()
    old = auth(user)
    response = client.patch(
        "/api/auth/me", headers=old, json={"password": "a-completely-new-password"}
    )
    assert response.status_code == 200
    assert client.get("/api/auth/me", headers=old).json()["authenticated"] is False
    assert (
        client.post(
            "/api/auth/login",
            json={"email": user.email, "password": "a-completely-new-password"},
        ).status_code
        == 200
    )


def test_a_deactivated_user_cannot_log_in(
    client: TestClient, db: Session, make_user
) -> None:
    user = make_user()
    user.is_active = False
    db.commit()
    response = client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 403


def test_deactivation_kills_a_live_session(
    client: TestClient, db: Session, make_user, auth
) -> None:
    """Not at next login -- immediately, on the next request."""
    user = make_user()
    headers = auth(user)
    assert client.get("/api/auth/me", headers=headers).json()["authenticated"] is True

    db.get(User, user.id).is_active = False
    db.commit()
    assert client.get("/api/auth/me", headers=headers).json()["authenticated"] is False


# --------------------------------------------------------------------------- #
# Sign-in role tab (`expected_role`)
# --------------------------------------------------------------------------- #


def _login(client: TestClient, email: str, **extra):
    return client.post(
        "/api/auth/login", json={"email": email, "password": PASSWORD, **extra}
    )


def test_the_matching_tab_signs_in_as_before(client: TestClient, make_user) -> None:
    user = make_user(Role.JUDGE)
    response = _login(client, user.email, expected_role="judge")
    assert response.status_code == 200, response.text
    assert response.json()["user"]["role"] == "judge"


def test_no_tab_is_unchanged(client: TestClient, make_user) -> None:
    """Omitting `expected_role` -- every existing caller -- behaves exactly as
    before, whatever the account's role."""
    user = make_user(Role.ORGANIZER)
    assert _login(client, user.email).status_code == 200


def test_the_wrong_tab_is_refused_with_a_message_and_no_session(
    client: TestClient, make_user, db: Session
) -> None:
    user = make_user(Role.ADMIN)
    before = db.query(UserSession).filter(UserSession.user_id == user.id).count()

    response = _login(client, user.email, expected_role="participant")
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "wrong_role_tab"
    assert detail["actual_role"] == "admin"
    assert "This account is an Admin account, not a Participant one" in detail["message"]
    assert "try the Admin tab" in detail["message"]

    # Refused means refused: no cookie, and no session row was written.
    assert settings.session_cookie_name not in response.cookies
    db.expire_all()
    assert db.query(UserSession).filter(UserSession.user_id == user.id).count() == before


def test_a_wrong_password_never_reveals_the_role(client: TestClient, make_user) -> None:
    """The tab check sits behind credential verification, so it cannot be used
    to learn what role an address holds."""
    user = make_user(Role.ADMIN)
    wrong = client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "not-the-password", "expected_role": "participant"},
    )
    unknown = client.post(
        "/api/auth/login",
        json={"email": "nobody@example.com", "password": PASSWORD, "expected_role": "participant"},
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert "admin" not in wrong.text.lower()


def test_visitor_is_not_a_tab(client: TestClient, make_user) -> None:
    user = make_user(Role.PARTICIPANT)
    assert _login(client, user.email, expected_role="visitor").status_code == 422


def test_each_role_is_named_with_the_right_article(client: TestClient, make_user) -> None:
    organizer = make_user(Role.ORGANIZER)
    message = _login(client, organizer.email, expected_role="judge").json()["detail"]["message"]
    assert "This account is an Organizer account, not a Judge one" in message
