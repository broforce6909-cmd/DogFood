"""CSV export, at every stage.

"CSV export at every stage" is a T2 requirement, and the brief's wider point is
sharper than the requirement: _a platform you cannot leave is a trap._ So these
endpoints exist to get an organizer's data out in a form they can open, diff and
re-import, not to render a report.

Two rules hold across every entity here:

* **Streamed, not buffered.** `StreamingResponse` over a generator, so exporting
  an event with ten thousand scores does not build the whole file in memory first.
* **Stable column order, header always present.** An export with no rows still
  emits its header, because a consumer that has to special-case the empty file is
  a consumer that will break on a quiet event.
* **Cells that a spreadsheet would execute are escaped.** A participant-controlled
  project name beginning `=` is a formula to Excel, and the file carrying it is one
  we generated. `_safe_cell` prefixes a text guard, and the importer strips it back
  off so the round trip stays byte-identical.

The matching importers are T4 and are not written. The column sets here are
designed to be the ones those importers accept -- that is the round-trip property
DATA-MODEL.md promises -- but until the importers exist, this is a one-way door
and README.md says so.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..access import Action, Principal
from ..db import get_db
from ..deps import get_current_principal
from ..models import (
    AuditEntry,
    Event,
    Judge,
    JudgeAssignment,
    Submission,
    SubmissionStatus,
    Team,
    TeamMember,
    Track,
    Vote,
    Voter,
    VotingMethod,
)
from .judges import _staff_event
from .results import gather

router = APIRouter(prefix="/api/events", tags=["export"])

ENTITIES = (
    "tracks",
    "teams",
    "submissions",
    "directory",
    "judges",
    "assignments",
    "scores",
    "results",
    "votes",
    "audit",
)


# Characters that make a spreadsheet treat a cell as a formula rather than text.
# Participants control project names and descriptions, so these reach an organizer's
# Excel via a file we generated -- which is exactly what makes this our problem and
# not theirs. See THREAT-MODEL.md.
_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")

# The marker a spreadsheet reads as "the rest of this cell is literal text". Stripped
# again by the importer, so the round trip stays byte-identical.
_TEXT_GUARD = "'"


def _safe_cell(value: Any) -> Any:
    """Prefix a text guard if this cell would otherwise be read as a formula.

    Only strings are touched, and only at the start -- `a=b` is fine, `=a+b` is not.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_LEADERS):
        return _TEXT_GUARD + value
    return value


def _stream(header: list[str], rows: Iterator[list[Any]], filename: str) -> StreamingResponse:
    """Generator -> CSV response.

    `QUOTE_MINIMAL` with the default dialect, and `\\r\\n` line endings, which is
    what RFC 4180 asks for and what spreadsheets on every platform open without
    complaining.
    """

    def generate() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(header)
        yield _drain(buffer)
        for row in rows:
            # One place, every entity: a cell is escaped on the way out or not at all.
            writer.writerow([_safe_cell(cell) for cell in row])
            yield _drain(buffer)

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


def _drain(buffer: io.StringIO) -> str:
    value = buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    return value


@router.get("/{slug}/export/{entity}.csv")
def export(
    slug: str,
    entity: str,
    provisional: bool = Query(
        default=False, description="results only: include unfinished ballots"
    ),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """One route, eight entities. Organizer-only, every one of them.

    The sensitive ones are `scores` (every judge's ballot) and `votes` (who voted
    for what). Both sit behind the same `EXPORT` right as the rest, so a judge or a
    participant asking for either gets a 403, same as the aggregate.
    """
    event = _staff_event(db, slug, principal, Action.EXPORT)
    if entity not in ENTITIES:
        raise HTTPException(
            status_code=404,
            detail=f"No such export. Available: {', '.join(ENTITIES)}",
        )
    return _EXPORTERS[entity](db, event, provisional)


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #


def _tracks(db: Session, event: Event, _: bool) -> StreamingResponse:
    """Tracks. Columns chosen to match what `import/tracks` accepts, which is the
    round-trip property DATA-MODEL.md promises."""
    rows = (
        db.execute(
            select(Track).where(Track.event_id == event.id).order_by(Track.position, Track.key)
        )
        .scalars()
        .all()
    )

    def generate() -> Iterator[list[Any]]:
        for t in rows:
            yield [t.key, t.name, t.description or "", t.position]

    return _stream(
        ["key", "name", "description", "position"],
        generate(),
        f"{event.slug}-tracks.csv",
    )


def _teams(db: Session, event: Event, _: bool) -> StreamingResponse:
    rows = (
        db.execute(
            select(Team)
            .options(
                selectinload(Team.members).selectinload(TeamMember.user),
                selectinload(Team.submission),
            )
            .where(Team.event_id == event.id)
            .order_by(Team.name)
        )
        .scalars()
        .all()
    )

    def generate() -> Iterator[list[Any]]:
        for team in rows:
            for member in sorted(team.members, key=lambda m: m.joined_at):
                yield [
                    team.id,
                    team.name,
                    member.user.email,
                    member.user.display_name,
                    member.team_role.value,
                    member.joined_at.isoformat(),
                    team.submission.name if team.submission else "",
                ]

    return _stream(
        [
            "team_id", "team_name", "member_email", "member_name",
            "team_role", "joined_at", "submission",
        ],
        generate(),
        f"{event.slug}-teams.csv",
    )


def _submissions(db: Session, event: Event, _: bool) -> StreamingResponse:
    rows = (
        db.execute(
            select(Submission)
            .options(
                selectinload(Submission.team),
                selectinload(Submission.track),
                selectinload(Submission.answers),
            )
            .where(Submission.event_id == event.id)
            .order_by(Submission.name)
        )
        .scalars()
        .all()
    )

    def generate() -> Iterator[list[Any]]:
        for s in rows:
            yield [
                s.id,
                s.name,
                s.tagline or "",
                s.team.name,
                s.track.key if s.track else "",
                s.status.value,
                s.submitted_at.isoformat() if s.submitted_at else "",
                s.repo_url or "",
                s.live_url or "",
                s.demo_video_url or "",
                s.linkedin_url or "",
                s.thumbnail_url or "",
                # Semicolons, not commas: a comma inside a quoted CSV field is
                # legal and still the thing most likely to be mis-split by
                # whatever reads this next.
                ";".join(s.tech_tags or []),
                ";".join(s.discord_usernames or []),
                s.description or "",
            ]

    return _stream(
        [
            "submission_id", "name", "tagline", "team", "track", "status", "submitted_at",
            "repo_url", "live_url", "demo_video_url", "linkedin_url", "thumbnail_url",
            "tech_tags", "discord_usernames", "description",
        ],
        generate(),
        f"{event.slug}-submissions.csv",
    )


def _directory(db: Session, event: Event, _: bool) -> StreamingResponse:
    """One row per submitted project: team, links, and contact handles.

    A fixed, purpose-built column set for an outside consumer (sponsors,
    press, a directory site) -- not the round-trip family `submissions.csv`
    belongs to, so it carries none of that file's columns or column order and
    has no matching importer, the same as `votes` and `audit`.

    Draft and disqualified submissions are both left out: a draft was never
    entered, and disqualification already hides a project from the gallery,
    results and certificates everywhere else, so it stays hidden here too.
    """
    rows = (
        db.execute(
            select(Submission)
            .options(selectinload(Submission.team))
            .where(
                Submission.event_id == event.id,
                Submission.status == SubmissionStatus.SUBMITTED,
            )
            .order_by(Submission.name)
        )
        .scalars()
        .all()
    )

    def generate() -> Iterator[list[Any]]:
        for s in rows:
            yield [
                s.team.name,
                s.name,
                ", ".join(s.discord_usernames or []),
                s.linkedin_url or "",
                s.description or "",
                s.live_url or "",
                s.demo_video_url or "",
                s.repo_url or "",
            ]

    return _stream(
        [
            "team_name", "project_name", "discord_usernames", "linkedin_url",
            "description", "live_url", "video_url", "repo_url",
        ],
        generate(),
        f"{event.slug}-directory.csv",
    )


def _judges(db: Session, event: Event, _: bool) -> StreamingResponse:
    rows = (
        db.execute(
            select(Judge)
            .options(selectinload(Judge.user), selectinload(Judge.track))
            .where(Judge.event_id == event.id)
            .order_by(Judge.invited_at)
        )
        .scalars()
        .all()
    )

    def generate() -> Iterator[list[Any]]:
        for j in rows:
            yield [
                j.id,
                j.user.email,
                j.user.display_name,
                j.track.key if j.track else "",
                "yes" if j.is_active else "no",
                j.invited_at.isoformat(),
                j.accepted_at.isoformat() if j.accepted_at else "",
            ]

    return _stream(
        ["judge_id", "email", "name", "track", "active", "invited_at", "accepted_at"],
        generate(),
        f"{event.slug}-judges.csv",
    )


def _assignments(db: Session, event: Event, _: bool) -> StreamingResponse:
    rows = _ballots(db, event)

    def generate() -> Iterator[list[Any]]:
        for a in rows:
            yield [
                a.id,
                a.judge.user.email,
                a.judge.user.display_name,
                a.submission.name,
                a.submission.team.name,
                a.status.value,
                a.assigned_at.isoformat(),
                a.completed_at.isoformat() if a.completed_at else "",
                a.comment or "",
            ]

    return _stream(
        [
            "assignment_id", "judge_email", "judge_name", "submission", "team",
            "status", "assigned_at", "completed_at", "feedback",
        ],
        generate(),
        f"{event.slug}-assignments.csv",
    )


def _scores(db: Session, event: Event, _: bool) -> StreamingResponse:
    """Long format: one row per (judge, submission, criterion).

    Long rather than wide -- a column per criterion -- because the criteria are
    the organizer's own data and a wide file's header would change shape between
    two events on the same install. Long format pivots in one step in any
    spreadsheet and never changes its columns.
    """
    rows = _ballots(db, event)
    criteria = {c.id: c for c in event.criteria}

    def generate() -> Iterator[list[Any]]:
        for a in rows:
            for score in sorted(
                a.scores,
                key=lambda s: (
                    criteria[s.criterion_id].position if s.criterion_id in criteria else 0
                ),
            ):
                criterion = criteria.get(score.criterion_id)
                yield [
                    a.judge.user.email,
                    a.judge.user.display_name,
                    a.submission.name,
                    a.submission.team.name,
                    criterion.key if criterion else "",
                    criterion.name if criterion else "",
                    float(criterion.weight) if criterion else "",
                    score.value,
                    score.comment or "",
                ]

    return _stream(
        [
            "judge_email", "judge_name", "submission", "team",
            "criterion_key", "criterion_name", "criterion_weight", "value", "comment",
        ],
        generate(),
        f"{event.slug}-scores.csv",
    )


def _results(db: Session, event: Event, provisional: bool) -> StreamingResponse:
    """The same numbers the dashboard shows, via the same function.

    Raw and normalized side by side with both ranks, because a results file that
    only carries the final number is one an organizer cannot defend.
    """
    computed, meta, _criteria = gather(db, event, only_complete=not provisional)

    def generate() -> Iterator[list[Any]]:
        for row in computed.submissions:
            name, team, track = meta["submissions"].get(row.submission_id, ("?", "?", None))
            yield [
                row.normalized_rank,
                row.raw_rank,
                row.rank_delta,
                name,
                team,
                track or "",
                row.n_reviews,
                round(row.raw_mean, 4),
                round(row.normalized_mean, 4),
            ]

    return _stream(
        [
            "rank", "raw_rank", "rank_delta", "submission", "team", "track",
            "reviews", "raw_mean", "normalized_mean",
        ],
        generate(),
        f"{event.slug}-results.csv",
    )


def _ballots(db: Session, event: Event) -> list[JudgeAssignment]:
    return list(
        db.execute(
            select(JudgeAssignment)
            .options(
                selectinload(JudgeAssignment.judge).selectinload(Judge.user),
                selectinload(JudgeAssignment.scores),
                selectinload(JudgeAssignment.submission).selectinload(Submission.team),
            )
            .where(JudgeAssignment.event_id == event.id)
            .order_by(JudgeAssignment.assigned_at)
        )
        .scalars()
        .all()
    )




def _votes(db: Session, event: Event, _: bool) -> StreamingResponse:
    """Every ballot line: who voted for what, and what it cost them.

    Organizer-only, and the most sensitive file here after `scores.csv`. The voter
    column is the audit label -- an email for an account or a claimed address, a
    short pseudonym for an anonymous token -- never the raw ballot token.
    """
    rows = (
        db.execute(
            select(Vote)
            .options(
                selectinload(Vote.voter).selectinload(Voter.user),
                selectinload(Vote.submission).selectinload(Submission.team),
            )
            .where(Vote.event_id == event.id)
            .order_by(Vote.created_at)
        )
        .scalars()
        .all()
    )
    quadratic = event.voting_method is VotingMethod.QUADRATIC

    def generate() -> Iterator[list[Any]]:
        for v in rows:
            yield [
                v.voter.label,
                v.voter.access.value,
                v.submission.name,
                v.submission.team.name,
                v.credits,
                v.credits * v.credits if quadratic else v.credits,
                v.created_at.isoformat(),
            ]

    return _stream(
        ["voter", "access_mode", "submission", "team", "votes", "credit_cost", "cast_at"],
        generate(),
        f"{event.slug}-votes.csv",
    )


def _audit(db: Session, event: Event, _: bool) -> StreamingResponse:
    """The trail, oldest first.

    Oldest first here and newest first in the API, deliberately: the page answers
    "what just happened" and the file is read as a narrative or diffed.

    Ordered by `seq` (Phase 5), not `created_at` -- see the same reasoning in
    `routers/audit.py`'s `_page()`: `seq` is the column the hash chain and every
    real ordering guarantee are built on, and it is strictly increasing where a
    timestamp is not.
    """
    rows = (
        db.execute(
            select(AuditEntry)
            .where(AuditEntry.event_id == event.id)
            .order_by(AuditEntry.seq)
        )
        .scalars()
        .all()
    )

    def generate() -> Iterator[list[Any]]:
        for r in rows:
            yield [
                r.created_at.isoformat(),
                r.action.value,
                r.actor_label,
                r.actor_role or "",
                r.resource_type or "",
                str(r.resource_id or ""),
                r.summary,
                r.ip_address or "",
            ]

    return _stream(
        ["at", "action", "actor", "actor_role", "resource_type", "resource_id", "summary", "ip"],
        generate(),
        f"{event.slug}-audit.csv",
    )


_EXPORTERS = {
    "tracks": _tracks,
    "teams": _teams,
    "submissions": _submissions,
    "directory": _directory,
    "judges": _judges,
    "assignments": _assignments,
    "scores": _scores,
    "results": _results,
    "votes": _votes,
    "audit": _audit,
}
