"""Vote abuse, tested at the API, and written before the voting code existed.

The brief concedes that community voting is universally gameable and says the
standard advice is to keep the prize small and hide the results. It then asks for
better: _"Anti-abuse that means something: rate limits, duplicate detection, and
an audit trail an organizer can read without a database client."_

So this file is the specification for "means something". Each test names an attack
and asserts what stops it. Where the answer is "nothing stops this, by design",
that is asserted too -- `test_open_link_voting_issues_a_token_per_claim` and
`test_email_gating_is_unverified_and_we_say_so` exist so the weaknesses are
documented properties rather than surprises.

Every assertion is an HTTP request. A unit test on a budget function would not
tell you the route calls it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.models import Role, SubmissionStatus, VotingAccess, VotingMethod, utcnow


@pytest.fixture()
def gallery(db, make_user, make_team, make_submission):
    """Four submitted projects on an event, ready to be voted on."""

    def _build(event, count: int = 4):
        subs = []
        for i in range(count):
            team = make_team(event, make_user(Role.PARTICIPANT), name=f"Team {i}")
            subs.append(
                make_submission(
                    team, name=f"Project {i}", status=SubmissionStatus.SUBMITTED
                )
            )
        return subs

    return _build


def claim(client: TestClient, slug: str, *, headers=None, email=None):
    """Claim a voting identity the way the ballot page would."""
    body = {} if email is None else {"email": email}
    return client.post(f"/api/events/{slug}/voting/claim", headers=headers or {}, json=body)


def put_votes(client: TestClient, slug: str, pairs, *, headers=None):
    return client.put(
        f"/api/events/{slug}/votes",
        headers=headers or {},
        json={"votes": [{"submission_id": str(s.id), "credits": c} for s, c in pairs]},
    )


# --------------------------------------------------------------------------- #
# Duplicate detection
# --------------------------------------------------------------------------- #


def test_one_account_gets_one_ballot(
    client: TestClient, auth, make_user, voting_event, gallery
):
    """Attack: vote, then claim a second ballot from the same account.

    Stopped by a partial unique index on `(event_id, user_id)`, not by the handler
    remembering to look -- so two simultaneous claims cannot both win. Claiming
    twice is idempotent: you get the ballot you already had.
    """
    event = voting_event(access=VotingAccess.AUTHENTICATED)
    gallery(event)
    me = auth(make_user(Role.PARTICIPANT))

    first = claim(client, event.slug, headers=me)
    second = claim(client, event.slug, headers=me)
    assert first.status_code in (200, 201), first.text
    assert second.status_code in (200, 201), second.text
    assert first.json()["id"] == second.json()["id"], "a second claim minted a second ballot"


def test_two_accounts_are_two_ballots(
    client: TestClient, auth, make_user, voting_event, gallery
):
    """The control for the test above: the constraint is per account, not global."""
    event = voting_event(access=VotingAccess.AUTHENTICATED)
    gallery(event)
    one = claim(client, event.slug, headers=auth(make_user(Role.PARTICIPANT)))
    two = claim(client, event.slug, headers=auth(make_user(Role.PARTICIPANT)))
    assert one.json()["id"] != two.json()["id"]


def test_a_deactivated_account_cannot_vote(
    client: TestClient, auth, make_user, db, voting_event, gallery
):
    """A deactivated account is no account -- the same rule as everywhere else."""
    event = voting_event(access=VotingAccess.AUTHENTICATED)
    subs = gallery(event)
    user = make_user(Role.PARTICIPANT)
    headers = auth(user)
    claim(client, event.slug, headers=headers)

    user.is_active = False
    db.commit()
    r = put_votes(client, event.slug, [(subs[0], 1)], headers=headers)
    assert r.status_code in (401, 403), r.text


# --------------------------------------------------------------------------- #
# Quadratic budget
# --------------------------------------------------------------------------- #


def test_quadratic_cost_is_the_square_of_the_votes(
    client: TestClient, auth, make_user, voting_event, gallery
):
    """10 votes on one project costs 100 credits, which is the whole budget."""
    event = voting_event(method=VotingMethod.QUADRATIC, credits=100)
    subs = gallery(event)
    me = auth(make_user(Role.PARTICIPANT))
    claim(client, event.slug, headers=me)

    ok = put_votes(client, event.slug, [(subs[0], 10)], headers=me)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["credits_spent"] == 100
    assert body["credits_remaining"] == 0


def test_a_ballot_over_budget_is_refused_whole(
    client: TestClient, auth, make_user, voting_event, gallery
):
    """Attack: spend more influence than the budget allows.

    11 votes costs 121 > 100. The whole ballot is refused rather than silently
    trimmed: quietly changing somebody's vote is worse than telling them no.
    """
    event = voting_event(method=VotingMethod.QUADRATIC, credits=100)
    subs = gallery(event)
    me = auth(make_user(Role.PARTICIPANT))
    claim(client, event.slug, headers=me)

    r = put_votes(client, event.slug, [(subs[0], 11)], headers=me)
    assert r.status_code == 422, r.text
    assert "121" in r.text and "100" in r.text

    # And nothing was written.
    ballot = client.get(f"/api/events/{event.slug}/ballot", headers=me).json()
    assert ballot["credits_spent"] == 0


def test_the_budget_is_global_across_projects(
    client: TestClient, auth, make_user, voting_event, gallery
):
    """6+6+6 votes is 36+36+36 = 108 credits, over a 100 budget, even though no
    single project looks expensive. The budget is the point of the method."""
    event = voting_event(method=VotingMethod.QUADRATIC, credits=100)
    subs = gallery(event)
    me = auth(make_user(Role.PARTICIPANT))
    claim(client, event.slug, headers=me)

    r = put_votes(client, event.slug, [(subs[0], 6), (subs[1], 6), (subs[2], 6)], headers=me)
    assert r.status_code == 422
    assert "108" in r.text


def test_spreading_votes_buys_more_total_influence_than_concentrating(
    client: TestClient, auth, make_user, voting_event, gallery
):
    """The property quadratic voting exists for, asserted rather than assumed.

    100 credits buys one 10-vote shout, or four 5-vote nods (25 each) for 20 votes
    of total influence. Intensity is expressible and expensive, which is what
    stops a loud minority deciding the outcome.
    """
    event = voting_event(method=VotingMethod.QUADRATIC, credits=100)
    subs = gallery(event)
    me = auth(make_user(Role.PARTICIPANT))
    claim(client, event.slug, headers=me)

    spread = put_votes(
        client, event.slug, [(s, 5) for s in subs[:4]], headers=me
    )
    assert spread.status_code == 200, spread.text
    assert spread.json()["credits_spent"] == 100
    assert sum(v["credits"] for v in spread.json()["votes"]) == 20


def test_single_mode_caps_the_number_of_projects(
    client: TestClient, auth, make_user, voting_event, gallery
):
    """One-person-one-vote mode: `votes_per_voter` distinct picks, one vote each."""
    event = voting_event(method=VotingMethod.SINGLE, votes_per_voter=2)
    subs = gallery(event)
    me = auth(make_user(Role.PARTICIPANT))
    claim(client, event.slug, headers=me)

    assert put_votes(client, event.slug, [(subs[0], 1), (subs[1], 1)], headers=me).status_code == 200
    too_many = put_votes(
        client, event.slug, [(subs[0], 1), (subs[1], 1), (subs[2], 1)], headers=me
    )
    assert too_many.status_code == 422
    assert "2" in too_many.text


def test_single_mode_refuses_vote_stacking(
    client: TestClient, auth, make_user, voting_event, gallery
):
    """Attack: ask for 5 credits on one project in one-person-one-vote mode."""
    event = voting_event(method=VotingMethod.SINGLE, votes_per_voter=3)
    subs = gallery(event)
    me = auth(make_user(Role.PARTICIPANT))
    claim(client, event.slug, headers=me)

    r = put_votes(client, event.slug, [(subs[0], 5)], headers=me)
    assert r.status_code == 422
    assert "one vote" in r.text.lower()


def test_the_same_project_twice_in_one_ballot_is_refused(
    client: TestClient, auth, make_user, voting_event, gallery
):
    """Attack: list a project twice so the credits add up past the cost curve."""
    event = voting_event(method=VotingMethod.QUADRATIC)
    subs = gallery(event)
    me = auth(make_user(Role.PARTICIPANT))
    claim(client, event.slug, headers=me)

    r = client.put(
        f"/api/events/{event.slug}/votes",
        headers=me,
        json={
            "votes": [
                {"submission_id": str(subs[0].id), "credits": 3},
                {"submission_id": str(subs[0].id), "credits": 3},
            ]
        },
    )
    assert r.status_code == 422


def test_replacing_a_ballot_does_not_accumulate(
    client: TestClient, auth, make_user, voting_event, gallery
):
    """A PUT is a replacement. Voting 5 then 3 leaves 3, not 8 -- which is also
    what makes the unique index on (voter, submission) an UPDATE rather than a
    conflict."""
    event = voting_event(method=VotingMethod.QUADRATIC)
    subs = gallery(event)
    me = auth(make_user(Role.PARTICIPANT))
    claim(client, event.slug, headers=me)

    put_votes(client, event.slug, [(subs[0], 5)], headers=me)
    second = put_votes(client, event.slug, [(subs[0], 3)], headers=me)
    assert second.status_code == 200
    assert second.json()["credits_spent"] == 9
    assert len(second.json()["votes"]) == 1


# --------------------------------------------------------------------------- #
# You cannot vote for yourself
# --------------------------------------------------------------------------- #


def test_a_team_cannot_vote_for_its_own_project(
    client: TestClient, auth, make_user, make_team, make_submission, voting_event
):
    """Attack: enter a project, then vote for it.

    The judging side already refuses to assign a judge their own team's work; the
    public side owes voters the same rule.
    """
    event = voting_event(method=VotingMethod.QUADRATIC)
    owner = make_user(Role.PARTICIPANT)
    team = make_team(event, owner, name="Self Interest")
    mine = make_submission(team, name="Mine", status=SubmissionStatus.SUBMITTED)
    other_team = make_team(event, make_user(Role.PARTICIPANT), name="Others")
    theirs = make_submission(other_team, name="Theirs", status=SubmissionStatus.SUBMITTED)

    me = auth(owner)
    claim(client, event.slug, headers=me)

    refused = put_votes(client, event.slug, [(mine, 3)], headers=me)
    assert refused.status_code == 422
    assert "own" in refused.text.lower()

    allowed = put_votes(client, event.slug, [(theirs, 3)], headers=me)
    assert allowed.status_code == 200, allowed.text


# --------------------------------------------------------------------------- #
# The voting window
# --------------------------------------------------------------------------- #


def test_voting_before_the_window_opens_is_refused(
    client: TestClient, auth, make_user, voting_event, gallery
):
    event = voting_event(open_now=False)
    subs = gallery(event)
    me = auth(make_user(Role.PARTICIPANT))
    claim(client, event.slug, headers=me)
    r = put_votes(client, event.slug, [(subs[0], 1)], headers=me)
    assert r.status_code == 409, r.text


def test_an_event_with_no_voting_window_has_no_voting(
    client: TestClient, auth, make_user, make_event, gallery
):
    """The safe default: an organizer who never configured voting did not
    accidentally leave it open since the event was created."""
    event = make_event()
    subs = gallery(event)
    me = auth(make_user(Role.PARTICIPANT))
    r = put_votes(client, event.slug, [(subs[0], 1)], headers=me)
    assert r.status_code == 409
    assert "not" in r.json()["detail"].lower()


def test_a_draft_cannot_be_voted_for(
    client: TestClient, auth, make_user, make_team, make_submission, voting_event
):
    event = voting_event()
    team = make_team(event, make_user(Role.PARTICIPANT))
    draft = make_submission(team, status=SubmissionStatus.DRAFT)
    me = auth(make_user(Role.PARTICIPANT))
    claim(client, event.slug, headers=me)
    r = put_votes(client, event.slug, [(draft, 1)], headers=me)
    assert r.status_code in (404, 422)


# --------------------------------------------------------------------------- #
# Access modes
# --------------------------------------------------------------------------- #


def test_authenticated_voting_refuses_anonymous(client: TestClient, voting_event, gallery):
    event = voting_event(access=VotingAccess.AUTHENTICATED)
    gallery(event)
    r = claim(client, event.slug)
    assert r.status_code in (401, 403), r.text


def test_email_gated_voting_needs_an_address_and_takes_it_once(
    client: TestClient, voting_event, gallery
):
    """One ballot per claimed address, by partial unique index.

    And the honest part: nothing here proves the address belongs to the voter,
    because this system sends no mail. Asserted as a documented limitation in
    `test_email_gating_is_unverified_and_we_say_so`.
    """
    event = voting_event(access=VotingAccess.EMAIL_GATED)
    gallery(event)

    missing = claim(client, event.slug)
    assert missing.status_code == 422

    first = claim(client, event.slug, email="Voter@Example.com")
    assert first.status_code in (200, 201), first.text
    # Case-folded, so Voter@ and voter@ are one person.
    again = claim(client, event.slug, email="voter@example.com")
    assert again.status_code in (200, 201, 409)
    if again.status_code != 409:
        assert again.json()["id"] == first.json()["id"]


def test_open_link_voting_keeps_your_ballot_across_a_refresh(
    client: TestClient, voting_event, gallery
):
    """Holding the token keeps the ballot: a reload is not a new voter."""
    event = voting_event(access=VotingAccess.OPEN_LINK)
    gallery(event)
    first = claim(client, event.slug)
    second = claim(client, event.slug)  # the client still has the cookie
    assert first.status_code in (200, 201), first.text
    assert first.json()["token"] is not None
    assert second.json()["id"] == first.json()["id"]


def test_open_link_voting_is_weak_and_we_say_so(
    client: TestClient, voting_event, gallery
):
    """Deliberately weak: clear the cookie and you get another ballot.

    This is the mode an organizer picks for a friendly internal demo. The rate
    limiter and the audit log are what make it survivable, and the tally carries a
    caveat telling the reader not to treat the totals as a count of people. The
    weakness is asserted here so nobody mistakes the mode for a control.
    """
    event = voting_event(access=VotingAccess.OPEN_LINK)
    gallery(event)

    first = claim(client, event.slug)
    client.cookies.clear()  # a fresh browser, or the same one after a purge
    second = claim(client, event.slug)

    assert first.json()["id"] != second.json()["id"]
    assert first.json()["token"] != second.json()["token"]


def test_a_token_from_one_event_does_not_vote_in_another(
    client: TestClient, voting_event, gallery
):
    """Attack: reuse a ballot token across events."""
    one, two = voting_event(access=VotingAccess.OPEN_LINK), voting_event(
        access=VotingAccess.OPEN_LINK
    )
    subs_two = gallery(two)
    gallery(one)

    token = claim(client, one.slug).json()["token"]
    r = client.put(
        f"/api/events/{two.slug}/votes",
        headers={"X-Voter-Token": token},
        json={"votes": [{"submission_id": str(subs_two[0].id), "credits": 1}]},
    )
    assert r.status_code in (401, 403, 404), r.text


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


def test_claiming_ballots_in_bulk_gets_rate_limited(
    client: TestClient, voting_event, gallery
):
    """Attack: script the open-link endpoint and mint a thousand ballots.

    The limiter is the only thing between open-link voting and a script, so it is
    asserted here rather than trusted. Database-backed, so it survives a restart.
    """
    event = voting_event(access=VotingAccess.OPEN_LINK)
    gallery(event)

    statuses = [claim(client, event.slug).status_code for _ in range(40)]
    assert 429 in statuses, f"no request was limited: {sorted(set(statuses))}"
    # And it is not limiting from the very first request.
    assert statuses[0] in (200, 201)


def test_a_rate_limited_request_says_when_to_come_back(
    client: TestClient, voting_event, gallery
):
    event = voting_event(access=VotingAccess.OPEN_LINK)
    gallery(event)
    limited = None
    for _ in range(60):
        response = claim(client, event.slug)
        if response.status_code == 429:
            limited = response
            break
    assert limited is not None, "never hit the limit"
    assert "retry-after" in {k.lower() for k in limited.headers}


def test_a_tripped_limit_lands_in_the_audit_log(
    client: TestClient, auth, make_user, voting_event, gallery
):
    """`ratelimit.Limit`'s own docstring promises a name "that appears in the
    audit log" -- until Phase 5, `AuditAction.RATE_LIMITED` was defined and never
    once constructed, so a tripped limit was invisible to the one person (an
    organizer investigating abuse) who would want to see it."""
    event = voting_event(access=VotingAccess.OPEN_LINK)
    gallery(event)
    organizer = make_user(Role.ORGANIZER)

    for _ in range(60):
        if claim(client, event.slug).status_code == 429:
            break
    else:
        pytest.fail("never hit the limit")

    rows = client.get(f"/api/events/{event.slug}/audit", headers=auth(organizer)).json()[
        "rows"
    ]
    from app.models import AuditAction

    tripped = [r for r in rows if r["action"] == AuditAction.RATE_LIMITED.value]
    assert tripped, [r["action"] for r in rows]
    assert "claim_ballot" in tripped[0]["summary"]


# --------------------------------------------------------------------------- #
# Results stay hidden
# --------------------------------------------------------------------------- #


def test_vote_totals_are_hidden_while_voting_is_open(
    client: TestClient, auth, make_user, voting_event, gallery
):
    """The brief's requirement, and the obvious attack on it: watching the tally
    move tells you how to game it."""
    event = voting_event(method=VotingMethod.QUADRATIC, results_public=False)
    subs = gallery(event)
    me = auth(make_user(Role.PARTICIPANT))
    claim(client, event.slug, headers=me)
    put_votes(client, event.slug, [(subs[0], 3)], headers=me)

    for headers in ({}, me):
        r = client.get(f"/api/events/{event.slug}/voting/results", headers=headers)
        assert r.status_code == 403, f"tally leaked: {r.status_code} {r.text[:120]}"


def test_an_organizer_sees_the_tally_during_voting(
    client: TestClient, auth, make_user, voting_event, gallery
):
    event = voting_event(method=VotingMethod.QUADRATIC, results_public=False)
    subs = gallery(event)
    voter = auth(make_user(Role.PARTICIPANT))
    claim(client, event.slug, headers=voter)
    put_votes(client, event.slug, [(subs[0], 3)], headers=voter)

    r = client.get(
        f"/api/events/{event.slug}/voting/results", headers=auth(make_user(Role.ORGANIZER))
    )
    assert r.status_code == 200, r.text
    assert r.json()["rows"][0]["votes"] == 3


def test_the_tally_opens_when_an_organizer_publishes(
    client: TestClient, auth, make_user, voting_event, gallery
):
    event = voting_event(results_public=True)
    subs = gallery(event)
    me = auth(make_user(Role.PARTICIPANT))
    claim(client, event.slug, headers=me)
    put_votes(client, event.slug, [(subs[0], 1)], headers=me)

    public = client.get(f"/api/events/{event.slug}/voting/results")
    assert public.status_code == 200, public.text


def test_a_scheduled_publication_is_not_yet_a_publication(
    client: TestClient, voting_event, gallery
):
    """`results_public_at` in the future must not leak early."""
    event = voting_event(results_public=None)
    gallery(event)
    assert client.get(f"/api/events/{event.slug}/voting/results").status_code == 403


def test_judge_results_never_become_public_in_t3(
    client: TestClient, auth, make_user, voting_event, gallery
):
    """Publishing the community tally must not publish the judges' scores.

    Two different secrets. `results_public_at` opens the vote tally; the judging
    aggregate stays organizer-only because no part of T3 asked for it to open.
    """
    event = voting_event(results_public=True)
    gallery(event)
    r = client.get(f"/api/events/{event.slug}/results", headers=auth(make_user(Role.PARTICIPANT)))
    assert r.status_code == 403, r.text


# --------------------------------------------------------------------------- #
# Randomised ballot ordering
# --------------------------------------------------------------------------- #


def test_the_ballot_order_is_stable_for_one_voter(
    client: TestClient, auth, make_user, voting_event, gallery
):
    """Position bias is killed by randomising *between* voters. Randomising
    within one voter, on every refresh, would just be disorienting."""
    event = voting_event()
    gallery(event, count=8)
    me = auth(make_user(Role.PARTICIPANT))
    claim(client, event.slug, headers=me)

    first = [p["id"] for p in client.get(f"/api/events/{event.slug}/ballot", headers=me).json()["projects"]]
    second = [p["id"] for p in client.get(f"/api/events/{event.slug}/ballot", headers=me).json()["projects"]]
    assert first == second, "the ballot reshuffled under the voter"


def test_different_voters_get_different_ballot_orders(
    client: TestClient, auth, make_user, voting_event, gallery
):
    event = voting_event()
    gallery(event, count=8)

    orders = []
    for _ in range(6):
        headers = auth(make_user(Role.PARTICIPANT))
        claim(client, event.slug, headers=headers)
        body = client.get(f"/api/events/{event.slug}/ballot", headers=headers).json()
        orders.append(tuple(p["id"] for p in body["projects"]))

    assert len(set(orders)) > 1, "every voter saw the same order; position bias intact"


def test_every_ballot_contains_every_project(
    client: TestClient, auth, make_user, voting_event, gallery
):
    """Shuffling must not drop or duplicate anything."""
    event = voting_event()
    subs = gallery(event, count=8)
    me = auth(make_user(Role.PARTICIPANT))
    claim(client, event.slug, headers=me)
    body = client.get(f"/api/events/{event.slug}/ballot", headers=me).json()
    assert sorted(p["id"] for p in body["projects"]) == sorted(str(s.id) for s in subs)


# --------------------------------------------------------------------------- #
# Honest limitations, asserted so they stay documented
# --------------------------------------------------------------------------- #


def test_email_gating_is_unverified_and_we_say_so(
    client: TestClient, voting_event, gallery
):
    """Two addresses at the same domain are two ballots, and nothing checks that
    either inbox exists. The API says so in the claim response rather than
    implying a guarantee it cannot make."""
    event = voting_event(access=VotingAccess.EMAIL_GATED)
    gallery(event)
    first = claim(client, event.slug, email="a@example.com")
    assert first.json()["verified"] is False
    assert "not verified" in first.json()["caveat"].lower()

    # A second address is a second ballot, and nothing checks either inbox.
    client.cookies.clear()
    second = claim(client, event.slug, email="b@example.com")
    assert second.status_code in (200, 201), second.text
    assert first.json()["id"] != second.json()["id"]


def test_a_claimed_address_is_not_handed_to_whoever_knows_it(
    client: TestClient, voting_event, gallery
):
    """Attack: claim somebody else's address to take over their ballot.

    Unverified email gating cannot prove the address is yours, so the answer is to
    refuse rather than hand the ballot over. Refused with the reason, which is also
    the honest statement of the mode's limit.
    """
    event = voting_event(access=VotingAccess.EMAIL_GATED)
    gallery(event)
    claim(client, event.slug, email="victim@example.com")

    client.cookies.clear()
    takeover = claim(client, event.slug, email="victim@example.com")
    assert takeover.status_code == 409, takeover.text
    assert "already voted" in takeover.json()["detail"]


# --------------------------------------------------------------------------- #
# IP retention (Phase 5): named as a gap in THREAT-MODEL.md, closed here
# --------------------------------------------------------------------------- #


def test_stale_voter_ip_and_user_agent_are_scrubbed(
    client: TestClient, db, voting_event, gallery, make_voter
):
    """`ip_address`/`user_agent` exist for abuse investigation, per their own
    docstring in app/models.py -- not to be kept forever. A voter old enough to
    be past the retention window has both cleared; a fresh one is untouched."""
    from app.routers.voting import _anonymize_stale_voters

    event = voting_event(access=VotingAccess.OPEN_LINK)
    gallery(event)

    stale = make_voter(event, token_hash="stale-token")
    stale.ip_address = "203.0.113.5"
    stale.user_agent = "curl/8.0"
    stale.created_at = utcnow() - timedelta(days=200)
    fresh = make_voter(event, token_hash="fresh-token")
    fresh.ip_address = "203.0.113.9"
    fresh.user_agent = "curl/8.0"
    db.commit()

    scrubbed = _anonymize_stale_voters(db, older_than=timedelta(days=90))
    assert scrubbed == 1

    db.refresh(stale)
    db.refresh(fresh)
    assert stale.ip_address is None
    assert stale.user_agent is None
    assert fresh.ip_address == "203.0.113.9"
    assert fresh.user_agent == "curl/8.0"


def test_claiming_a_ballot_opportunistically_scrubs_stale_voters(
    client: TestClient, db, voting_event, gallery, make_voter
):
    """The route a real install actually calls, not just the helper function --
    the same shape as `ratelimit.purge` being asserted from `claim_ballot`."""
    event = voting_event(access=VotingAccess.OPEN_LINK)
    gallery(event)

    stale = make_voter(event, token_hash="another-stale-token")
    stale.ip_address = "203.0.113.5"
    stale.created_at = utcnow() - timedelta(days=(365))
    db.commit()

    r = claim(client, event.slug)
    assert r.status_code in (200, 201), r.text

    db.refresh(stale)
    assert stale.ip_address is None
