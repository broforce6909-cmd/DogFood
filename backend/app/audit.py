"""The audit trail.

The brief's requirement is specific and it is about the reader, not the schema:
_"an audit trail an organizer can read without a database client"_. That single
sentence decides the design.

**Every entry carries a complete English sentence**, written at the moment of the
action, while the context to write it still exists:

    Priya Rivera scored "Switchyard" 4.25 (impact 5, execution 4, craft 3,
    presentation 2) on raptors-winter.

Not a template applied later over ids nobody can resolve. A log that says
`UPDATE scores SET value=4 WHERE id='3bc0...'` is a changelog for a database, and
an organizer reading it mid-incident needs a sentence.

The structured columns exist so the log can be *filtered* -- by actor, by action,
by event. The sentence exists so it can be *read*. Both, not either.

**Append-only, and now tamper-evident.** There is no update or delete route for
`audit_log`, and nothing in this module offers one -- that much has been true
since Phase 3. What was still true through Phase 4, and named as a gap in
THREAT-MODEL.md, is that nothing stopped someone with direct database access
from editing a row, and the log had no way to tell an organizer it had happened.

Phase 5 closes that with a hash chain: every entry's `entry_hash` covers its own
fields *and* the previous entry's hash (`prev_hash`), so altering any row, or
removing one from the middle, changes what every later hash in the table should
be. `verify_chain()` walks the whole table in `seq` order and recomputes every
hash to check. This cannot stop a database-level attacker from rewriting the
entire chain forward from the point of tampering -- nothing running inside the
database it protects can promise that -- but it makes silent, surgical tampering
impossible: the only way to hide one changed row is to also rewrite the hash of
every row after it, which is a different, much louder problem for an attacker to
solve, and is exactly what `verify_chain()` is built to notice.

**Written in the same transaction as the change it records.** `record()` adds to
the caller's session and does not commit; the route commits both together. So an
organizer reading the log is reading what actually committed -- there is no state
where the score changed but the entry is missing, or the reverse.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime

from fastapi import Request
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .access import ANONYMOUS, Principal
from .models import AuditAction, AuditEntry, Event, User, utcnow
from .signing import canonical

log = logging.getLogger("dogfood.audit")

# A fixed key for a Postgres advisory lock, held for the duration of the
# transaction that calls `record()`. Two requests committing an audit entry at
# the same moment would otherwise both read the same "previous" hash and each
# believe their own write is the next link -- the chain would fork rather than
# extend, and `verify_chain` would find a broken link that was never tampering,
# just a race. The lock serialises exactly the few milliseconds it takes to read
# the last hash and write the next one; nothing else in the app waits on it.
_CHAIN_LOCK_KEY = 0x446F_6746_6F6F_6431  # "DogFood1" as bytes, for a memorable key


def actor_of(principal: Principal) -> tuple[uuid.UUID | None, str, str | None]:
    """Resolve who did it into (id, label, role).

    The label is stored as text *as well as* the foreign key, because deleting a
    user must not erase what they did. `ON DELETE SET NULL` on `actor_id` keeps the
    row; the label keeps its meaning.
    """
    if isinstance(principal, User):
        return principal.id, principal.email, principal.role.value
    return None, "anonymous visitor", "visitor"


def _hash_fields(entry: AuditEntry) -> dict:
    """The fields covered by the hash, as one canonical, order-independent dict.

    Everything an editor could plausibly change is in here: who, what, when,
    about what, and the sentence itself. `id` is deliberately excluded -- it is
    a random UUID with no semantic content, and including it would not make a
    forged entry any harder to construct, only make this dict larger.
    """
    return {
        "seq": entry.seq,
        "event_id": str(entry.event_id) if entry.event_id else None,
        "event_slug": entry.event_slug,
        "action": entry.action.value,
        "actor_id": str(entry.actor_id) if entry.actor_id else None,
        "actor_label": entry.actor_label,
        "actor_role": entry.actor_role,
        "resource_type": entry.resource_type,
        "resource_id": str(entry.resource_id) if entry.resource_id else None,
        "summary": entry.summary,
        "ip_address": entry.ip_address,
        "created_at": entry.created_at.isoformat(),
    }


def _chain_hash(prev_hash: str | None, entry: AuditEntry) -> str:
    """sha256 of the previous link plus this entry's own fields.

    Plain SHA-256, not a signature: a hash chain's job is detecting that a row
    changed for someone who already has database access, not proving authorship to
    someone who does not. `app/signing.py`'s Ed25519 signatures are for records a
    *stranger with no database access at all* has to verify; this is for an *admin*
    checking that nobody with access edited what is already in front of them.
    """
    payload = {"prev_hash": prev_hash, **_hash_fields(entry)}
    return hashlib.sha256(canonical(payload)).hexdigest()


def record(
    db: Session,
    *,
    action: AuditAction,
    summary: str,
    principal: Principal = ANONYMOUS,
    event: Event | None = None,
    event_id: uuid.UUID | None = None,
    event_slug: str | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    request: Request | None = None,
    actor_label: str | None = None,
    created_at: datetime | None = None,
) -> AuditEntry:
    """Add one entry to the caller's session. Does not commit -- see the module docstring.

    `actor_label` overrides the resolved label, which is how an anonymous voter
    gets recorded as `anonymous:3bc01487` rather than as an indistinguishable
    "anonymous visitor": a pseudonym an organizer can count is worth more than none.

    `event_slug` is denormalised onto the row for the same reason `actor_label`
    is: `event_id` is `ON DELETE SET NULL`, so a row has to keep reading
    correctly after the event it names is gone. Passing `event=` fills it in
    automatically; pass `event_slug=` directly only for the rare call site that
    has an id but not the loaded row.

    `created_at` overrides the real clock. The only caller that should ever pass
    it is `seed.py`, which backdates fixture entries to tell a coherent story
    ("assigned yesterday, scored this morning") -- every real route leaves this
    unset and gets `utcnow()`. Chaining does not care either way: the chain's
    order is `seq`, not `created_at` (see `AuditEntry`'s docstring on why), so a
    backdated timestamp changes what a row *says* happened without changing
    where it sits in the chain or how its hash is checked.

    Chaining happens here, transparently to every caller: an advisory lock, a
    flush so a pending-but-unflushed entry from earlier in this same request is
    visible, a read of the current chain head, and a hash covering the new
    entry's own fields plus that head. `seq` is assigned by Postgres on flush
    (it is a Postgres `IDENTITY`), which is why the entry is flushed before its
    hash is computed -- the hash covers `seq`, so `seq` has to exist first.
    """
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CHAIN_LOCK_KEY})
    db.flush()  # make any earlier, still-pending entry in this request visible

    head = db.execute(
        select(AuditEntry.entry_hash).order_by(AuditEntry.seq.desc()).limit(1)
    ).scalar_one_or_none()

    actor_id, label, role = actor_of(principal)
    entry = AuditEntry(
        action=action,
        actor_id=actor_id,
        actor_label=actor_label or label,
        actor_role=role,
        event_id=event.id if event is not None else event_id,
        event_slug=event.slug if event is not None else event_slug,
        resource_type=resource_type,
        resource_id=resource_id,
        summary=summary.strip(),
        ip_address=(request.client.host if request and request.client else None),
        created_at=created_at or utcnow(),
        prev_hash=head,
        entry_hash="",  # placeholder; real value needs `seq`, assigned below
    )
    db.add(entry)
    db.flush()  # assigns `entry.seq` from the IDENTITY sequence
    entry.entry_hash = _chain_hash(head, entry)

    # A second, independent record of the chain head, outside the database the
    # chain protects: the container's stdout, which `docker compose logs`
    # captures and a real deployment typically ships to a log aggregator this
    # app has no access to. `verify_chain()` alone only catches a rewrite that
    # stops short of recomputing every hash after it -- someone with database
    # access who *also* recomputes the whole chain forward leaves no
    # disagreement inside the database for it to find. This line does not
    # change what verify_chain can prove; it gives an admin comparing "what the
    # database says the head was at some past point" against "what this line
    # said it was at the time" a place to catch that specific case, cheaply, without
    # building the append-only external store that would close it completely.
    log.info("audit chain seq=%d entry_hash=%s prev_hash=%s", entry.seq, entry.entry_hash, head)
    return entry


class BrokenLink:
    """Where `verify_chain` found the chain to disagree with itself."""

    __slots__ = ("seq", "id", "reason")

    def __init__(self, seq: int, id_: uuid.UUID, reason: str) -> None:
        self.seq = seq
        self.id = id_
        self.reason = reason


# The exact columns `_hash_fields`/`_chain_hash` read off an entry, selected
# individually rather than as `select(AuditEntry)`. A load test at 100,000+
# rows (see ARCHITECTURE.md's "Performance" section) found this endpoint's
# cost is almost entirely the SHA-256 + canonical-JSON work every row needs
# regardless -- that part cannot get cheaper without weakening what a hash
# chain actually proves. What *was* free to cut was hydrating each row into a
# full mapped `AuditEntry` (identity-map bookkeeping, relationship-lazy-loader
# setup) it never uses beyond these fields. Plain columns plus `yield_per`
# stream the table instead of materialising all 100,000+ rows as ORM objects
# before the loop even starts.
_VERIFY_COLUMNS = (
    AuditEntry.id,
    AuditEntry.seq,
    AuditEntry.event_id,
    AuditEntry.event_slug,
    AuditEntry.action,
    AuditEntry.actor_id,
    AuditEntry.actor_label,
    AuditEntry.actor_role,
    AuditEntry.resource_type,
    AuditEntry.resource_id,
    AuditEntry.summary,
    AuditEntry.ip_address,
    AuditEntry.created_at,
    AuditEntry.prev_hash,
    AuditEntry.entry_hash,
)


def verify_chain(db: Session) -> tuple[bool, int, BrokenLink | None]:
    """Walk the whole table in `seq` order and recompute every hash.

    Returns `(valid, checked, first_break)`. Stops at the first disagreement
    rather than cataloguing every one after it -- once a link is broken, every
    later hash is expected to disagree too (that is the entire point of
    chaining them), so reporting all of them would just be noise around the one
    fact that matters: where the chain first stops matching itself.

    An empty table is valid by definition (`checked == 0`); there is nothing to
    have tampered with.
    """
    stmt = (
        select(*_VERIFY_COLUMNS)
        .order_by(AuditEntry.seq.asc())
        .execution_options(yield_per=2000)
    )
    expected_prev: str | None = None
    checked = 0
    for row in db.execute(stmt):
        checked += 1
        if row.prev_hash != expected_prev:
            return False, checked, BrokenLink(
                row.seq, row.id, "prev_hash does not match the previous entry's hash"
            )
        recomputed = _chain_hash(expected_prev, row)
        if recomputed != row.entry_hash:
            return False, checked, BrokenLink(
                row.seq, row.id, "entry_hash does not match this entry's own fields"
            )
        expected_prev = row.entry_hash
    return True, checked, None


def quote(value: str | None, limit: int = 80) -> str:
    """Project and team names go in the sentence, so they need quoting and a cap.

    A 200-character project name should not make one log line unreadable.
    """
    if not value:
        return '""'
    text_value = value if len(value) <= limit else value[: limit - 1] + "…"
    return f'"{text_value}"'
