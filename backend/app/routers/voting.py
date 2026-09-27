"""Community voting: claim a ballot, cast it, read the tally.

Three access modes, and the difference between them is *how a ballot is
identified*, not how it is counted:

* `authenticated` -- the ballot is the account. No claim step is needed; casting a
  vote resolves (or creates) the voter row for that user.
* `email_gated` -- the ballot is a claimed address, and the voter carries a token
  afterwards. Nothing verifies the address, because this system sends no mail.
* `open_link` -- the ballot is a token and nothing else.

The token is handled exactly like a session: a random value to the client, an HMAC
in the database. It travels in `X-Voter-Token` or a cookie, so the ballot survives
a refresh without JavaScript holding it.

Everything that writes is rate-limited and audited. Everything that reads a tally
goes through `check_access`, which is where "results hidden during the voting
window" actually lives.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .. import ratelimit
from ..access import Action, Principal, check_access, require_access
from ..audit import quote, record
from ..config import settings
from ..db import get_db
from ..deps import get_current_principal
from ..email import schedule_email
from ..hooks import schedule
from ..models import (
    COMMUNITY_VOTE_REGISTRATION_CUTOFF_DAYS,
    AuditAction,
    Event,
    EventRegistration,
    Submission,
    SubmissionStatus,
    Team,
    TeamMember,
    Track,
    Vote,
    Voter,
    VotingAccess,
    VotingMethod,
    WebhookEvent,
    utcnow,
)
from ..schemas import (
    BallotIn2,
    BallotOut,
    ClaimIn,
    TallyOut,
    TallyRow,
    VoteOut,
    VoterOut,
)
from ..security import hash_session_token, new_session_token, normalize_email
from ..serializers import submission_card
from ..voting import Choice, ballot_order, check_ballot, cost_of, credits_spent, new_ordering_seed
from .events import readable_event
from .results import compute_tiers, effective_tiers, gather

router = APIRouter(prefix="/api/events", tags=["voting"])

VOTER_COOKIE = "dogfood_voter"
VOTER_HEADER = "X-Voter-Token"

UNVERIFIED_CAVEAT = (
    "This address is not verified -- no email is sent, so nothing proves the "
    "address belongs to you. It stops the same address voting twice, and nothing "
    "more."
)


# --------------------------------------------------------------------------- #
# Identifying a voter
# --------------------------------------------------------------------------- #


def _token_from(request: Request) -> str | None:
    return request.headers.get(VOTER_HEADER) or request.cookies.get(VOTER_COOKIE)


def find_voter(
    db: Session, event: Event, principal: Principal, request: Request
) -> Voter | None:
    """Resolve the caller's ballot for this event, or None.

    Scoped to the event in both branches. A token issued for one event does not
    resolve against another -- the lookup carries `event_id`, so reusing a token
    across events finds nothing rather than finding somebody else's ballot.
    """
    if principal.id is not None:
        row = db.execute(
            select(Voter).where(Voter.event_id == event.id, Voter.user_id == principal.id)
        ).scalar_one_or_none()
        if row is not None:
            return row

    token = _token_from(request)
    if not token:
        return None
    return db.execute(
        select(Voter).where(
            Voter.event_id == event.id, Voter.token_hash == hash_session_token(token)
        )
    ).scalar_one_or_none()


def _require_voter(
    db: Session, event: Event, principal: Principal, request: Request
) -> Voter:
    """The ballot, or the reason there isn't one.

    In authenticated mode a ballot is implicit in having an account, so one is
    created on demand: requiring a separate claim step would be ceremony. In the
    token modes the claim step is what issues the token, so there is nothing to
    create here.
    """
    voter = find_voter(db, event, principal, request)
    if voter is not None:
        return voter

    # The community-tier round is always effectively authenticated mode --
    # see `access._check_event`'s `CLAIM_BALLOT` branch -- whatever this
    # event's own `voting_access` says, so auto-create-on-demand applies here
    # too, not just when `voting_access` is literally `AUTHENTICATED`.
    if (
        event.voting_access is VotingAccess.AUTHENTICATED or event.community_vote_slots > 0
    ) and principal.id is not None:
        _enforce_community_vote_eligibility(principal, event)
        require_access(principal, event, Action.CLAIM_BALLOT)
        return _create_voter(db, event, principal, request)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "No ballot for this event. Claim one at "
            f"POST /api/events/{event.slug}/voting/claim"
        ),
    )


def _create_voter(
    db: Session,
    event: Event,
    principal: Principal,
    request: Request,
    *,
    email: str | None = None,
    token: str | None = None,
) -> Voter:
    voter = Voter(
        event_id=event.id,
        access=event.voting_access,
        user_id=principal.id if principal.id is not None else None,
        email=email,
        token_hash=hash_session_token(token) if token else None,
        ordering_seed=new_ordering_seed(),
        ip_address=request.client.host if request.client else None,
        user_agent=(request.headers.get("user-agent") or "")[:400] or None,
    )
    db.add(voter)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race against a concurrent claim, or the same address is already
        # taken. The constraint is the check; this is just how it reads.
        db.rollback()
        existing = find_voter(db, event, principal, request)
        if existing is not None:
            return existing
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A ballot already exists for that identity on this event.",
        ) from None
    db.refresh(voter)
    return voter


# --------------------------------------------------------------------------- #
# Budget helpers
# --------------------------------------------------------------------------- #


def _budget(event: Event) -> int:
    return event.vote_credits if event.voting_method is VotingMethod.QUADRATIC else (
        event.votes_per_voter
    )


def _max_projects(event: Event) -> int:
    return (
        event.votes_per_voter
        if event.voting_method is VotingMethod.SINGLE
        else event.vote_credits  # quadratic: the budget is the only cap
    )


def _votes_of(db: Session, voter: Voter) -> list[Vote]:
    return list(
        db.execute(
            select(Vote)
            .options(selectinload(Vote.submission).selectinload(Submission.team))
            .where(Vote.voter_id == voter.id)
            .order_by(Vote.created_at)
        )
        .scalars()
        .all()
    )


def _votable(db: Session, event: Event) -> list[Submission]:
    """Submitted projects only. A draft is not in the gallery and is not votable.

    When this event uses the winner/community-vote split
    (`community_vote_slots > 0`), the round this whole module implements is
    Part 3's *community-tier* round specifically -- scoped down further to
    just the submissions ranked between the outright winners and the rest,
    the subset the round exists to help decide between. An event that does
    not use the split (both slots default to 0) behaves exactly as before:
    every submitted project is votable.
    """
    all_submitted = list(
        db.execute(
            select(Submission)
            .options(selectinload(Submission.team), selectinload(Submission.track))
            .where(
                Submission.event_id == event.id,
                Submission.status == SubmissionStatus.SUBMITTED,
            )
        )
        .scalars()
        .all()
    )
    if event.community_vote_slots <= 0:
        return all_submitted
    computed, _, _ = gather(db, event, only_complete=True)
    # `effective_tiers()`, not `compute_tiers()` alone: an admin override
    # (Part 4) is the authoritative final answer for which tier a submission
    # is in, and the community vote round must follow it -- a submission an
    # admin moved into or out of the tier is moved into or out of the ballot
    # too, not just relabelled on the results page.
    tiers = effective_tiers(db, event, compute_tiers(event, computed))
    return [s for s in all_submitted if tiers.get(s.id) == "community_tier"]


def _own_submission_ids(db: Session, event: Event, principal: Principal) -> frozenset[uuid.UUID]:
    """The projects this voter's own teams entered.

    The judging side refuses to assign a judge their own team's work; the public
    side owes voters the same rule, and for the same reason.
    """
    if principal.id is None:
        return frozenset()
    rows = db.execute(
        select(Submission.id)
        .join(Team, Team.id == Submission.team_id)
        .join(TeamMember, TeamMember.team_id == Team.id)
        .where(Submission.event_id == event.id, TeamMember.user_id == principal.id)
    ).scalars()
    return frozenset(rows)


def _ballot_out(
    db: Session, event: Event, voter: Voter, *, with_projects: bool, now
) -> BallotOut:
    votes = _votes_of(db, voter)
    spent = sum(cost_of(v.credits, event.voting_method) for v in votes)
    budget = _budget(event)

    projects: list = []
    if with_projects:
        votable = _votable(db, event)
        by_id = {s.id: s for s in votable}
        order = ballot_order([s.id for s in votable], voter.ordering_seed)
        projects = [submission_card(by_id[i]) for i in order]

    return BallotOut(
        voter_id=voter.id,
        event_slug=event.slug,
        method=event.voting_method,
        credits_total=budget,
        credits_spent=spent,
        credits_remaining=max(0, budget - spent),
        max_projects=_max_projects(event),
        voting_open=event.voting_open(now),
        votes=[
            VoteOut(
                submission_id=v.submission_id,
                submission_name=v.submission.name,
                credits=v.credits,
                cost=cost_of(v.credits, event.voting_method),
            )
            for v in votes
        ],
        projects=projects,
    )


def _anonymize_stale_voters(db: Session, *, older_than: timedelta | None = None) -> int:
    """Scrub `ip_address`/`user_agent` off ballots old enough that their one
    documented purpose -- abuse investigation while a round is live or freshly
    closed -- has passed.

    Not scheduled, the same reason `ratelimit.purge` is not: no job runner in
    this stack, so this runs opportunistically from the highest-traffic voting
    route instead of on a timer. `credits`/`submission_id` are untouched --
    this is retention for the two columns that are personal data with no
    judging purpose, not a right-to-be-forgotten eraser for the vote itself.
    """
    cutoff = utcnow() - (older_than or timedelta(days=settings.voter_ip_retention_days))
    result = db.execute(
        update(Voter)
        .where(
            Voter.created_at < cutoff,
            (Voter.ip_address.is_not(None)) | (Voter.user_agent.is_not(None)),
        )
        .values(ip_address=None, user_agent=None)
    )
    db.commit()
    return result.rowcount or 0


def _ineligibility_message(event: Event) -> str:
    cutoff = event.starts_at - timedelta(days=COMMUNITY_VOTE_REGISTRATION_CUTOFF_DAYS)
    return (
        "This event's community vote round is limited to signed-in participants "
        f"who registered for {event.slug} by {cutoff.date().isoformat()} "
        f"({COMMUNITY_VOTE_REGISTRATION_CUTOFF_DAYS} days before it started). "
        "A new account, or one that registered after that date, cannot take part "
        "in this round."
    )


def _enforce_community_vote_eligibility(principal: Principal, event: Event) -> None:
    """The clear, actionable 403 for Part 3's registration cutoff, in place
    of the generic one `require_access` would give. Called from every path
    that can mint or use a ballot on this event -- `claim_ballot` directly,
    `_require_voter` for a ballot created on demand -- so the message reads
    the same no matter which route the caller reached it through.
    """
    if event.community_vote_slots > 0 and not check_access(principal, event, Action.CLAIM_BALLOT):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=_ineligibility_message(event)
        )


# --------------------------------------------------------------------------- #
# Claim
# --------------------------------------------------------------------------- #


@router.post("/{slug}/voting/claim", response_model=VoterOut)
def claim_ballot(
    slug: str,
    payload: ClaimIn,
    request: Request,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> VoterOut:
    """Get a ballot for this event.

    Rate-limited hard, because in open-link mode this is the only endpoint that
    mints an identity and therefore the entire attack surface. Idempotent for an
    account or a claimed address; a fresh token every time for open-link, which is
    that mode's documented weakness rather than a bug.
    """
    event = readable_event(db, slug, principal)
    _enforce_community_vote_eligibility(principal, event)
    require_access(principal, event, Action.CLAIM_BALLOT)
    now = utcnow()

    ratelimit.enforce(
        db,
        ratelimit.CLAIM_BALLOT,
        ratelimit.client_key(request, slug),
        request=request,
        event=event,
    )
    # Opportunistic housekeeping; there is no job runner in this stack.
    ratelimit.purge(db)
    _anonymize_stale_voters(db)

    email: str | None = None
    token: str | None = None

    if event.voting_access is VotingAccess.EMAIL_GATED:
        if not payload.email:
            raise HTTPException(
                status_code=422,
                detail="This event is email-gated; an email address is required to vote.",
            )
        email = normalize_email(payload.email)

    if email is not None:
        # In email-gated mode the *address* is the identity, so it is resolved
        # first. Falling through to the cookie would mean a voter who typed a
        # different address silently kept their old ballot -- input ignored without
        # being told, which is worse than either outcome it was choosing between.
        claimed = db.execute(
            select(Voter).where(Voter.event_id == event.id, Voter.email == email)
        ).scalar_one_or_none()
        if claimed is not None:
            presented = _token_from(request)
            same_browser = (
                presented is not None
                and claimed.token_hash == hash_session_token(presented)
            )
            if not same_browser:
                # That address has a ballot and this caller cannot show they own
                # it. Handing the ballot over on the strength of knowing an address
                # would make takeover trivial -- and nothing here can verify the
                # address, because this install sends no mail. So: refused, with the
                # reason, rather than a silent hijack or a silent second ballot.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"{email} has already voted on this event. This install "
                        "sends no email, so there is no way to prove the address is "
                        "yours and recover that ballot -- an organizer can clear it "
                        "if this is a mistake."
                    ),
                )
        existing = claimed
    else:
        existing = find_voter(db, event, principal, request)

    if existing is None:
        if event.voting_access is not VotingAccess.AUTHENTICATED:
            token = new_session_token()
        voter = _create_voter(
            db, event, principal, request, email=email, token=token
        )
        record(
            db,
            action=AuditAction.VOTE_CAST,
            summary=(
                f"{voter.label} claimed a ballot on {event.slug} "
                f"({event.voting_access.value})"
            ),
            principal=principal,
            event=event,
            resource_type="voter",
            resource_id=voter.id,
            request=request,
            actor_label=voter.label,
        )
        db.commit()
    else:
        voter = existing

    if token:
        # Same cookie policy as the session: HttpOnly so script cannot read it.
        response.set_cookie(
            VOTER_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            secure=settings.session_cookie_secure,
            path="/",
        )

    votes = _votes_of(db, voter)
    spent = credits_spent(
        [Choice(v.submission_id, v.credits) for v in votes], event.voting_method
    )
    budget = _budget(event)
    unverified = event.voting_access is not VotingAccess.AUTHENTICATED

    return VoterOut(
        id=voter.id,
        event_slug=event.slug,
        access=event.voting_access,
        method=event.voting_method,
        token=token,
        email=voter.email,
        verified=not unverified,
        caveat=UNVERIFIED_CAVEAT if unverified else None,
        credits_total=budget,
        credits_spent=spent,
        credits_remaining=max(0, budget - spent),
        max_projects=_max_projects(event),
        voting_open=event.voting_open(now),
        voting_closes_at=event.voting_closes_at,
    )


# --------------------------------------------------------------------------- #
# The ballot
# --------------------------------------------------------------------------- #


@router.get("/{slug}/ballot", response_model=BallotOut)
def get_ballot(
    slug: str,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> BallotOut:
    """The projects to vote on, in this voter's own order, plus their current votes.

    The order is a pure function of the voter's stored seed, so it is random between
    voters -- which is what kills position bias -- and identical on a refresh, which
    is what stops the page moving under somebody mid-decision.
    """
    event = readable_event(db, slug, principal)
    voter = _require_voter(db, event, principal, request)
    require_access(principal, voter, Action.READ)
    return _ballot_out(db, event, voter, with_projects=True, now=utcnow())


@router.put("/{slug}/votes", response_model=BallotOut)
def cast_votes(
    slug: str,
    payload: BallotIn2,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> BallotOut:
    """Replace this voter's ballot.

    Whole-ballot replacement, because in quadratic mode the budget is global: three
    individually modest choices can be collectively unaffordable, and a per-project
    endpoint would have to re-validate the rest anyway.

    Refused whole if it does not fit. Silently trimming somebody's vote to make it
    affordable would be a worse answer than telling them the number.
    """
    event = readable_event(db, slug, principal)
    now = utcnow()
    voter = _require_voter(db, event, principal, request)

    if not check_access(principal, voter, Action.VOTE, now=now):
        # Distinguish "not now" from "not you", the same way scoring does.
        if not event.voting_open(now):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Voting is not open for this event"
                    if event.voting_opens_at
                    else "Voting has not been scheduled for this event"
                ),
            )
        _enforce_community_vote_eligibility(principal, event)
        require_access(principal, voter, Action.VOTE, now=now)

    # Two keys: the voter identity (uniform across all three access modes -- an
    # authenticated user, a claimed email, and an open-link token are all just a
    # `Voter` row) and the caller's address, scoped to this event. Keying by
    # identity alone would let a NAT full of legitimate voters look like one
    # abuser; keying by address alone would let a scripted voter outrun the
    # limit by rotating address. Both must hold.
    ratelimit.enforce_dual(
        db,
        ratelimit.CAST_VOTE,
        f"voter:{voter.id}",
        ratelimit.client_key(request, slug),
        request=request,
        event=event,
    )

    votable = {s.id: s for s in _votable(db, event)}
    choices = [Choice(v.submission_id, v.credits) for v in payload.votes]
    problem = check_ballot(
        choices,
        method=event.voting_method,
        budget=_budget(event),
        max_projects=_max_projects(event),
        own_submission_ids=_own_submission_ids(db, event, principal),
        votable_ids=frozenset(votable),
    )
    if problem is not None:
        raise HTTPException(status_code=422, detail=problem.detail)

    previous = {v.submission_id: v for v in _votes_of(db, voter)}
    wanted = {c.submission_id: c.credits for c in choices}

    for submission_id, credits in wanted.items():
        row = previous.get(submission_id)
        if row is None:
            db.add(
                Vote(
                    event_id=event.id,
                    voter_id=voter.id,
                    submission_id=submission_id,
                    credits=credits,
                )
            )
        else:
            row.credits = credits
    for submission_id, row in previous.items():
        if submission_id not in wanted:
            db.delete(row)

    spent = credits_spent(choices, event.voting_method)
    changed = bool(previous)
    record(
        db,
        action=AuditAction.VOTE_CHANGED if changed else AuditAction.VOTE_CAST,
        summary=(
            f"{voter.label} {'changed their ballot to' if changed else 'voted'} "
            + (
                ", ".join(
                    f"{credits} on {quote(votable[sid].name)}"
                    for sid, credits in wanted.items()
                )
                or "nothing"
            )
            + f" ({spent} of {_budget(event)} credits) on {event.slug}"
        ),
        principal=principal,
        event=event,
        resource_type="voter",
        resource_id=voter.id,
        request=request,
        actor_label=voter.label,
    )
    try:
        db.commit()
    except IntegrityError:
        # Same class of race as a double-submitted score: two requests for the
        # same voter both read "no vote on this project yet" and both tried to
        # INSERT, racing `uq_votes_one_per_project`. Whichever committed first
        # holds a row for the same voter and the same choices this request was
        # about to write; resending converts cleanly into the UPDATE branch.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This ballot was updated by a concurrent request. Please resend your vote.",
        ) from None

    schedule(
        background,
        db,
        event.id,
        WebhookEvent.VOTE_CAST,
        {
            "event": event.slug,
            "voter": voter.label,
            "credits_spent": spent,
            "projects": [votable[sid].name for sid in wanted],
        },
    )
    return _ballot_out(db, event, voter, with_projects=False, now=now)


@router.delete("/{slug}/votes", response_model=BallotOut)
def withdraw_votes(
    slug: str,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> BallotOut:
    """Withdraw the whole ballot. Same window rules as casting it."""
    event = readable_event(db, slug, principal)
    now = utcnow()
    voter = _require_voter(db, event, principal, request)

    if not check_access(principal, voter, Action.VOTE, now=now):
        if not event.voting_open(now):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Voting is not open for this event"
            )
        _enforce_community_vote_eligibility(principal, event)
        require_access(principal, voter, Action.VOTE, now=now)

    for row in _votes_of(db, voter):
        db.delete(row)
    record(
        db,
        action=AuditAction.VOTE_WITHDRAWN,
        summary=f"{voter.label} withdrew their ballot on {event.slug}",
        principal=principal,
        event=event,
        resource_type="voter",
        resource_id=voter.id,
        request=request,
        actor_label=voter.label,
    )
    db.commit()
    return _ballot_out(db, event, voter, with_projects=False, now=now)


# --------------------------------------------------------------------------- #
# The tally
# --------------------------------------------------------------------------- #


@router.get("/{slug}/voting/results", response_model=TallyOut)
def tally(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> TallyOut:
    """Vote totals. Organizers always; the public only once published.

    `Action.READ_TALLY` is the whole gate, and it is the same predicate for both
    readers -- which is what stops the tally leaking through a route somebody
    forgot. Note it is deliberately a *different* right from `READ_RESULTS`:
    publishing the community vote does not publish the judges' scores.
    """
    event = readable_event(db, slug, principal)
    now = utcnow()
    require_access(principal, event, Action.READ_TALLY, now=now)

    # Same pool `_votable()` scopes the ballot to -- the community-tier subset
    # when this event uses the winner/community-vote split, every submitted
    # project otherwise. `community_vote_slots <= 0` (the common case -- every
    # event that predates the split, and any event that just wants a plain
    # tally) is the one this endpoint short-circuits: `_votable()`'s own
    # early return for that case is already exactly
    # `event_id == event.id AND status == SUBMITTED`, the same two conditions
    # the aggregate query below already carries, so the extra
    # `id.in_(votable_ids)` narrows nothing and the full `Submission`+`Team`+
    # `Track` ORM hydration `_votable()` does to build it is pure overhead --
    # a load test at 500+ concurrent voters (ARCHITECTURE.md's "Performance"
    # section) found this endpoint's own Python-side hydration cost, repeated
    # on every poll, was serializing concurrent requests behind the GIL far
    # more than the database work underneath it. The one thing still needed
    # in that case, a submission -> track-name lookup for the response, is
    # fetched directly as two plain columns instead.
    if event.community_vote_slots > 0:
        votable = _votable(db, event)
        votable_ids: set[uuid.UUID] | None = {s.id for s in votable}
        tracks = {s.id: (s.track.name if s.track else None) for s in votable}
    else:
        votable_ids = None
        tracks = dict(
            db.execute(
                select(Submission.id, Track.name)
                .outerjoin(Track, Track.id == Submission.track_id)
                .where(
                    Submission.event_id == event.id,
                    Submission.status == SubmissionStatus.SUBMITTED,
                )
            ).all()
        )

    stmt = (
        select(
            Submission.id,
            Submission.name,
            Team.name,
            func.coalesce(func.sum(Vote.credits), 0).label("votes"),
            func.count(Vote.id).label("voters"),
        )
        .join(Team, Team.id == Submission.team_id)
        .outerjoin(Vote, Vote.submission_id == Submission.id)
        .where(
            Submission.event_id == event.id,
            Submission.status == SubmissionStatus.SUBMITTED,
        )
        .group_by(Submission.id, Submission.name, Team.name)
    )
    if votable_ids is not None:
        stmt = stmt.where(Submission.id.in_(votable_ids))
    rows = db.execute(stmt).all()

    # Cost is recomputed rather than stored: `credits` on a vote is the count, and
    # the cost curve is a property of the event's method, which can change.
    enriched = []
    for submission_id, name, team_name, votes, voters in rows:
        enriched.append(
            {
                "submission_id": submission_id,
                "submission_name": name,
                "team_name": team_name,
                "track": tracks.get(submission_id),
                "votes": int(votes or 0),
                "voters": int(voters or 0),
            }
        )

    credits_by_submission = {
        submission_id: int(total or 0)
        for submission_id, total in db.execute(
            select(
                Vote.submission_id,
                func.sum(
                    Vote.credits * Vote.credits
                    if event.voting_method is VotingMethod.QUADRATIC
                    else Vote.credits
                ),
            )
            .where(Vote.event_id == event.id)
            .group_by(Vote.submission_id)
        ).all()
    }

    enriched.sort(key=lambda r: (-r["votes"], r["submission_name"]))
    ranked: list[TallyRow] = []
    previous, rank = None, 0
    for index, row in enumerate(enriched, start=1):
        if previous is None or row["votes"] != previous:
            rank, previous = index, row["votes"]
        ranked.append(
            TallyRow(
                **row,
                rank=rank,
                credits=credits_by_submission.get(row["submission_id"], 0),
            )
        )

    total_voters = db.execute(
        select(func.count()).select_from(Voter).where(Voter.event_id == event.id)
    ).scalar_one()

    caveat = None
    if event.voting_access is VotingAccess.OPEN_LINK:
        caveat = (
            "Open-link voting: a ballot is a token and clearing cookies gets "
            "another. Read these totals as a popularity signal, not a count of "
            "people. See THREAT-MODEL.md."
        )
    elif event.voting_access is VotingAccess.EMAIL_GATED:
        caveat = (
            "Email-gated voting: addresses are not verified, because this system "
            "sends no mail. One address is one ballot, and nothing more."
        )

    return TallyOut(
        event_slug=event.slug,
        method=event.voting_method,
        access=event.voting_access,
        voting_open=event.voting_open(now),
        public=event.results_public(now),
        total_voters=total_voters,
        rows=ranked,
        caveat=caveat,
    )


@router.post("/{slug}/voting/publish", response_model=TallyOut)
def publish_tally(
    slug: str,
    request: Request,
    background: BackgroundTasks,
    public: bool = True,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> TallyOut:
    """Open or re-close the community tally to the public.

    Reversible, and both directions are audited. An organizer who publishes by
    mistake during the voting window needs to be able to undo it, and a log entry
    is what makes that accountable rather than quiet.
    """
    event = readable_event(db, slug, principal)
    require_access(principal, event, Action.MANAGE)
    now = utcnow()
    was_public = event.results_public(now)

    event.results_public_at = now if public else None
    record(
        db,
        action=AuditAction.RESULTS_PUBLISHED,
        summary=(
            f"Community vote totals for {event.slug} were made "
            f"{'public' if public else 'private again'}"
        ),
        principal=principal,
        event=event,
        resource_type="event",
        resource_id=event.id,
        request=request,
    )
    db.commit()

    # `results.published` was a documented, subscribable webhook topic that
    # nothing ever scheduled. Same gap as the other topics fixed in this pass.
    # Fired both ways -- publishing and un-publishing are both events an
    # integration would want to hear about, same as the audit log treats them
    # as two distinguishable facts rather than one.
    schedule(
        background,
        db,
        event.id,
        WebhookEvent.RESULTS_PUBLISHED,
        {"event": event.slug, "public": public},
    )

    # The email notification, unlike the webhook, fires once: on the actual
    # transition to public, not on every toggle. `results_public_at` also
    # gates the community-vote tally, Part 4's team-facing tier, and Part 5's
    # judge report and public dashboard -- this is the one moment all of
    # those become visible at once, and the one moment worth a registrant's
    # inbox. Un-publishing sends nothing: "results are no longer public" is
    # not a notification anyone asked for.
    if public and not was_public:
        _notify_results_published(background, db, event)

    return tally(slug, principal=principal, db=db)


def _notify_results_published(background: BackgroundTasks, db: Session, event: Event) -> int:
    """Broadcast to everyone on the event's own roster (`event_registrations`
    -- see its docstring) that results are up. Best-effort per recipient,
    same as every other email in this codebase: one registrant's bounced
    address must not stop the next one from being notified."""
    recipients = list(
        db.execute(
            select(EventRegistration.email).where(EventRegistration.event_id == event.id)
        ).scalars()
    )
    url = f"{settings.web_base_url.rstrip('/')}/events/{event.slug}"
    for email in recipients:
        schedule_email(
            background,
            to=email,
            subject=f"Results are up for {event.name}",
            body=f"Results for {event.name} are now public.\n\n{url}",
        )
    return len(recipients)
