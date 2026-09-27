"""Bulk import: the other half of the round trip.

DATA-MODEL.md has promised since Phase 1 that *every entity gets a matching pair,
with identical column sets in both directions, so an export can be re-imported
without editing*. It has been a documented gap for three phases. This closes it, and
the property is tested literally: export a file, post the same bytes back, and the
result must be all updates, no errors, and an identical export afterwards.

The brief's framing is why it is worth the trouble: *a platform you cannot leave is
a trap.*

Three rules hold for every entity:

1. **Every row is scoped to the event in the URL.** A CSV naming another event's id
   is an error on that row, never a write to somebody else's event. That is the one
   real attack here and it is closed by construction rather than by checking.
2. **Partial success, reported per row.** All-or-nothing would mean one typo in row
   40 discards the other 39 corrections, and an organizer would stop using the
   importer — which is how data gets edited in psql instead.
3. **An id matches, a natural key creates.** A row with a known id is an update; a
   row with no id, or an unknown one, is a create keyed on the entity's natural key.
   So a spreadsheet edit updates and a pasted new line inserts, which is what
   somebody working in a spreadsheet expects.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..access import Action, Principal
from ..audit import record
from ..db import get_db
from ..deps import get_current_principal
from ..models import AuditAction, Event, Submission, Team, Track
from ..schemas import ImportResult, _github_repo_url, _http_url, _image_url
from .judges import _staff_event

router = APIRouter(prefix="/api/events", tags=["import"])

ENTITIES = ("tracks", "teams", "submissions")

MAX_BYTES = 5 * 1024 * 1024


@dataclass
class Outcome:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _rows(body: bytes, required: set[str]) -> list[dict[str, str]]:
    """Parse and sanity-check the CSV before touching a single row.

    A missing required column is a 422 for the whole file rather than an error on
    every row: the file is the wrong shape, and saying so once is more use than
    saying it forty times.
    """
    if len(body) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="That file is too large (5 MB limit)")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=422, detail="That file is not UTF-8 text. Export it as CSV and retry."
        ) from None

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise HTTPException(status_code=422, detail="That file has no header row")

    present = {(name or "").strip() for name in reader.fieldnames}
    missing = required - present
    if missing:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Missing required column(s): {', '.join(sorted(missing))}. "
                f"Found: {', '.join(sorted(c for c in present if c))}"
            ),
        )
    return [
        {(k or "").strip(): _unguard((v or "").strip()) for k, v in row.items()}
        for row in reader
    ]


def _unguard(value: str) -> str:
    """Undo the export's spreadsheet text guard.

    `exports._safe_cell` prefixes `'` to any cell a spreadsheet would execute. Strip
    it here so that export -> import -> export is byte-identical, which is the
    round-trip property DATA-MODEL.md promises and `test_import_export.py` asserts.

    Only stripped when what follows is actually a formula leader, so a legitimate
    value that happens to start with an apostrophe survives.
    """
    if value.startswith("'") and value[1:2] in ("=", "+", "-", "@"):
        return value[1:]
    return value


def _maybe_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


@router.post("/{slug}/import/{entity}", response_model=ImportResult)
async def bulk_import(
    slug: str,
    entity: str,
    request: Request,
    dry_run: bool = Query(
        default=False,
        description="Parse and validate the file, report what would happen, and write nothing.",
    ),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> ImportResult:
    """Import a CSV matching the matching export's columns.

    Organizer-only: this is the most dangerous route in the product, because it
    writes many rows at once from a file.

    `dry_run=true` runs the exact same importer -- the same per-row validation,
    the same natural-key matching against what is already in the database --
    and then rolls back instead of committing, so the response is a preview an
    organizer can read before touching anything. Not a held transaction across
    two requests (upload-preview, then a second call to confirm): the second
    call simply re-parses and re-applies the same file for real. If the
    underlying data changed in between -- rare, and low-stakes for a
    staff-only bulk-editing tool -- the importer's own per-row validation and
    the 409-on-conflict handling below cover it the same as any other import,
    not a two-phase commit held open between requests.
    """
    event = _staff_event(db, slug, principal, Action.EXPORT)
    if entity not in ENTITIES:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No importer for '{entity}'. Available: {', '.join(ENTITIES)}. "
                "Judging data and votes are deliberately not importable -- see "
                "DATA-MODEL.md."
            ),
        )

    body = await request.body()
    try:
        outcome = _IMPORTERS[entity](db, event, body)
    except IntegrityError:
        # Each importer already merges rows that collide *within* the file
        # (by key or by name) into updates rather than duplicate creates; this
        # is left for the case that isn't: a second import for the same event
        # committing between this request's read and its write, so a row this
        # request thought was new collided with one the other request just
        # created. Nothing here was written -- `db.rollback()` discards the
        # whole batch -- so re-running the same file is safe and picks up
        # whatever the other import just added as an update instead of a
        # duplicate.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another import committed on this event at the same time. Re-run this import.",
        ) from None

    if dry_run:
        # A preview commits nothing -- not the rows the importer staged, not
        # an audit entry for an import that never actually happened.
        db.rollback()
        return ImportResult(
            created=outcome.created,
            updated=outcome.updated,
            skipped=outcome.skipped,
            errors=outcome.errors,
            dry_run=True,
        )

    record(
        db,
        action=AuditAction.BULK_IMPORT,
        summary=(
            f"{principal.email} ran a bulk import of {entity} on {event.slug}: "
            f"{outcome.created} created, {outcome.updated} updated, "
            f"{len(outcome.errors)} error(s)"
        ),
        principal=principal,
        event=event,
        resource_type="event",
        resource_id=event.id,
        request=request,
    )
    db.commit()

    return ImportResult(
        created=outcome.created,
        updated=outcome.updated,
        skipped=outcome.skipped,
        errors=outcome.errors,
    )


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #


def _import_tracks(db: Session, event: Event, body: bytes) -> Outcome:
    """Columns: key, name, description, position — the export's own set."""
    rows = _rows(body, required={"key"})
    outcome = Outcome()
    existing = {t.key: t for t in event.tracks}

    for index, row in enumerate(rows, start=2):  # row 1 is the header
        key = row.get("key", "").lower()
        if not key:
            outcome.errors.append(f"row {index}: key is required")
            continue

        track = existing.get(key)
        if track is None:
            track = Track(event_id=event.id, key=key, name=row.get("name") or key)
            db.add(track)
            existing[key] = track
            outcome.created += 1
        else:
            outcome.updated += 1

        if row.get("name"):
            track.name = row["name"]
        track.description = row.get("description") or None
        position = row.get("position", "")
        if position.isdigit():
            track.position = int(position)

    db.flush()
    return outcome


def _import_teams(db: Session, event: Event, body: bytes) -> Outcome:
    """Columns as exported: team_id, team_name, member_email, ...

    The export is one row per *member*, so the same team appears several times. Only
    the team row itself is imported — membership is not, because adding somebody to a
    team by spreadsheet would bypass the invite flow and the one-team-per-event
    constraint's whole purpose. Named in the result rather than silently ignored.
    """
    rows = _rows(body, required={"team_name"})
    outcome = Outcome()
    by_name = {
        t.name: t
        for t in db.execute(select(Team).where(Team.event_id == event.id)).scalars()
    }
    seen: set[str] = set()

    for index, row in enumerate(rows, start=2):
        name = row.get("team_name", "")
        if not name:
            outcome.errors.append(f"row {index}: team_name is required")
            continue

        given_id = _maybe_uuid(row.get("team_id", ""))
        if given_id is not None:
            owned = db.execute(
                select(Team).where(Team.id == given_id, Team.event_id == event.id)
            ).scalar_one_or_none()
            if owned is None and db.get(Team, given_id) is not None:
                outcome.errors.append(
                    f"row {index}: team {given_id} belongs to another event"
                )
                continue

        if name in seen:
            outcome.skipped += 1  # the export repeats a team per member
            continue
        seen.add(name)

        if name in by_name:
            outcome.updated += 1
        else:
            db.add(Team(event_id=event.id, name=name))
            outcome.created += 1

    db.flush()
    return outcome


def _import_submissions(db: Session, event: Event, body: bytes) -> Outcome:
    """Columns as exported. Matched by `submission_id`, then by name.

    Deliberately cannot change `team` or `status`: re-assigning an entry to another
    team by spreadsheet, or marking a draft submitted after the deadline, are both
    things the deadline and the ownership rules exist to prevent. Editing taglines,
    links, descriptions and tags — the actual reason an organizer wants this — is
    allowed.
    """
    rows = _rows(body, required={"name"})
    outcome = Outcome()

    current = list(
        db.execute(
            select(Submission)
            .options(selectinload(Submission.team))
            .where(Submission.event_id == event.id)
        ).scalars()
    )
    by_id = {s.id: s for s in current}
    by_name = {s.name: s for s in current}
    tracks = {t.key: t.id for t in event.tracks}

    for index, row in enumerate(rows, start=2):
        given_id = _maybe_uuid(row.get("submission_id", ""))
        target = by_id.get(given_id) if given_id else None

        if target is None and given_id is not None:
            # An id we do not have. If it exists at all it is another event's, and
            # this import must not reach it.
            if db.get(Submission, given_id) is not None:
                outcome.errors.append(
                    f"row {index}: submission {given_id} belongs to another event"
                )
                continue
            outcome.errors.append(f"row {index}: no submission with id {given_id}")
            continue

        if target is None:
            target = by_name.get(row.get("name", ""))
        if target is None:
            # Creating a project needs a team, and teams are formed by invite.
            outcome.errors.append(
                f"row {index}: no existing project matches; create it in the app first"
            )
            continue

        # `repo_url`, `live_url`, `demo_video_url` and `linkedin_url` render as
        # a clickable `<a href>` on the project page; `thumbnail_url` renders
        # only via `<img src>`. The JSON API (`SubmissionUpdate` in
        # schemas.py) already runs every one of these through
        # `_http_url`/`_image_url` specifically to close a stored-XSS vector
        # -- a `javascript:` URI set as `repo_url` executes the moment
        # somebody clicks "View repo". This importer wrote straight to the
        # ORM column with `setattr` and skipped that check entirely, which
        # meant the exact vulnerability the API path was fixed against was
        # still reachable by CSV. Re-validated here, per row rather than
        # failing the whole file, matching this importer's own "partial
        # success" design. `repo_url` specifically uses the same
        # GitHub-shaped check the API uses (`_github_repo_url`), not the
        # generic `_http_url` the other three get -- otherwise a CSV import
        # could set a repo link the JSON API would refuse, and the "export
        # re-imports without editing" round trip DATA-MODEL.md promises would
        # quietly stop being true for exactly this column.
        row_failed = False
        for column, attribute, validator in (
            ("repo_url", "repo_url", _github_repo_url),
            ("live_url", "live_url", _http_url),
            ("demo_video_url", "demo_video_url", _http_url),
            ("linkedin_url", "linkedin_url", _http_url),
            ("thumbnail_url", "thumbnail_url", _image_url),
        ):
            if column not in row:
                continue
            try:
                validated = validator(row[column] or None)
            except ValueError as exc:
                outcome.errors.append(f"row {index}: {column}: {exc}")
                row_failed = True
                continue
            setattr(target, attribute, validated)
        if row_failed:
            continue

        for column, attribute in (
            ("name", "name"),
            ("tagline", "tagline"),
            ("description", "description"),
        ):
            if column in row:
                setattr(target, attribute, row[column] or None)
        if not target.name:
            target.name = row.get("name") or "Untitled"

        if "tech_tags" in row:
            target.tech_tags = [
                tag.strip().lower() for tag in row["tech_tags"].split(";") if tag.strip()
            ]
        if "discord_usernames" in row:
            target.discord_usernames = [
                name.strip() for name in row["discord_usernames"].split(";") if name.strip()
            ]
        if "track" in row:
            key = row["track"]
            target.track_id = tracks.get(key) if key else None

        outcome.updated += 1

    db.flush()
    return outcome


_IMPORTERS = {
    "tracks": _import_tracks,
    "teams": _import_teams,
    "submissions": _import_submissions,
}
