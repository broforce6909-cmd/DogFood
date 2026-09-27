"""Certificates and signed participation records.

Three kinds, one shape: a participant took part, a judge reviewed *n* projects, a
team placed. What makes them worth having is not the content — it is that
**a third party can check the signature without an account and without trusting the
page the record is displayed on.**

So `GET /api/certificates/{code}` is open, and it returns the exact signed bytes
next to the signature and the `key_id` that signed it, rather than a re-serialised
object. A verifier who does not trust our verdict can check the Ed25519 signature
themselves against the matching public key from `GET /api/signing/public-keys`. See
`app/signing.py` for why that distinction is load-bearing.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..access import Action, Principal, require_authenticated
from ..audit import quote, record
from ..certificate_render import render_pdf, render_png
from ..db import get_db
from ..deps import get_current_principal
from ..hooks import schedule
from ..models import (
    AssignmentStatus,
    AuditAction,
    Certificate,
    CertificateKind,
    Event,
    Judge,
    JudgeAssignment,
    Role,
    Submission,
    SubmissionStatus,
    Team,
    TeamMember,
    User,
    WebhookEvent,
    utcnow,
)
from ..pagination import Page, PageParams, count_of, pages_for
from ..schemas import (
    CertificateIssueForUserIn,
    CertificateLookupRow,
    CertificateOut,
    CertificatePublic,
    IssueResult,
    RevokeIn,
    SigningKeyOut,
)
from ..signing import new_code, public_keys, sign_payload, verify
from .judges import _staff_event

router = APIRouter(prefix="/api", tags=["certificates"])


def _out(cert: Certificate) -> CertificateOut:
    return CertificateOut(
        id=cert.id,
        event_id=cert.event_id,
        kind=cert.kind,
        subject_name=cert.subject_name,
        title=cert.title,
        code=cert.code,
        payload=cert.payload,
        signature=cert.signature,
        key_id=cert.key_id,
        issued_at=cert.issued_at,
        revoked_at=cert.revoked_at,
        revoked_reason=cert.revoked_reason,
        valid=cert.is_valid,
    )


def _upsert(
    db: Session,
    event: Event,
    *,
    kind: CertificateKind,
    title: str,
    subject_name: str,
    payload: dict,
    subject_user_id: uuid.UUID | None = None,
    submission_id: uuid.UUID | None = None,
    issued_by: Principal | None = None,
) -> tuple[Certificate, bool]:
    """Create or update one record. Returns (certificate, created).

    Re-issuing **keeps the existing code**. A code gets printed, emailed and put on a
    CV; minting a new one on every re-issue would quietly break every link somebody
    already shared. The payload and signature are refreshed, so the record stays
    truthful while the handle stays stable.
    """
    query = select(Certificate).where(
        Certificate.event_id == event.id, Certificate.kind == kind
    )
    query = query.where(
        Certificate.subject_user_id == subject_user_id
        if subject_user_id is not None
        else Certificate.submission_id == submission_id
    )
    existing = db.execute(query).scalar_one_or_none()

    code = existing.code if existing is not None else new_code()
    full = {**payload, "code": code}
    body, signature, key_id = sign_payload(full)

    if existing is not None:
        existing.payload = body.decode()
        existing.signature = signature
        existing.key_id = key_id
        existing.subject_name = subject_name
        existing.title = title
        return existing, False

    cert = Certificate(
        event_id=event.id,
        kind=kind,
        subject_user_id=subject_user_id,
        submission_id=submission_id,
        subject_name=subject_name,
        title=title,
        code=code,
        payload=body.decode(),
        signature=signature,
        key_id=key_id,
        issued_by_id=issued_by.id if issued_by is not None and issued_by.id else None,
    )
    db.add(cert)
    return cert, True


@router.post("/events/{slug}/certificates/issue", response_model=IssueResult)
def issue(
    slug: str,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> IssueResult:
    """Issue records for everyone who earned one.

    Bulk, because doing this one person at a time is not a workflow an organizer
    survives at forty projects. Idempotent, because they will press it twice.

    Three passes: every member of a team that entered gets a participation record,
    every judge who completed at least one ballot gets a judging record naming the
    count, and every submitted project gets a placement record.
    """
    event = _staff_event(db, slug, principal, Action.MANAGE)
    now = utcnow()
    created = 0
    updated = 0
    issued: list[Certificate] = []

    # -- participation ----------------------------------------------------- #
    members = db.execute(
        select(User, Team, Submission)
        .join(TeamMember, TeamMember.user_id == User.id)
        .join(Team, Team.id == TeamMember.team_id)
        .join(Submission, Submission.team_id == Team.id)
        .where(
            Team.event_id == event.id,
            Submission.status == SubmissionStatus.SUBMITTED,
        )
        .distinct()
    ).all()
    for user, team, submission in members:
        cert, is_new = _upsert(
            db,
            event,
            kind=CertificateKind.PARTICIPATION,
            title=f"Participation — {event.name}",
            subject_name=user.display_name,
            subject_user_id=user.id,
            issued_by=principal,
            payload={
                "kind": CertificateKind.PARTICIPATION.value,
                "event": event.slug,
                "event_name": event.name,
                "subject": user.display_name,
                "team": team.name,
                # The project is part of what is signed, not just drawn: a
                # participation record that names a team but not the work is one
                # a verifier cannot tie to anything. Records issued before this
                # field existed gain it the next time the organizer re-issues
                # (which refreshes payload and signature and keeps the code).
                "project": submission.name,
                "issued_at": now.isoformat(),
            },
        )
        issued.append(cert)
        created += int(is_new)
        updated += int(not is_new)

    # -- judging ----------------------------------------------------------- #
    judged = db.execute(
        select(Judge, User, func.count(JudgeAssignment.id))
        .join(User, User.id == Judge.user_id)
        .join(JudgeAssignment, JudgeAssignment.judge_id == Judge.id)
        .where(
            Judge.event_id == event.id,
            JudgeAssignment.status == AssignmentStatus.COMPLETE,
        )
        .group_by(Judge.id, User.id)
    ).all()
    for _judge, user, reviews in judged:
        cert, is_new = _upsert(
            db,
            event,
            kind=CertificateKind.JUDGING,
            title=f"Judging — {event.name}",
            subject_name=user.display_name,
            subject_user_id=user.id,
            issued_by=principal,
            payload={
                "kind": CertificateKind.JUDGING.value,
                "event": event.slug,
                "event_name": event.name,
                "subject": user.display_name,
                "reviews_completed": int(reviews),
                "issued_at": now.isoformat(),
            },
        )
        issued.append(cert)
        created += int(is_new)
        updated += int(not is_new)

    # -- placement --------------------------------------------------------- #
    entries = db.execute(
        select(Submission)
        .options(selectinload(Submission.team))
        .where(
            Submission.event_id == event.id,
            Submission.status == SubmissionStatus.SUBMITTED,
        )
    ).scalars()
    for submission in entries:
        cert, is_new = _upsert(
            db,
            event,
            kind=CertificateKind.PLACEMENT,
            title=f"Entry — {submission.name}",
            subject_name=submission.team.name,
            submission_id=submission.id,
            issued_by=principal,
            payload={
                "kind": CertificateKind.PLACEMENT.value,
                "event": event.slug,
                "event_name": event.name,
                "subject": submission.team.name,
                "project": submission.name,
                "issued_at": now.isoformat(),
            },
        )
        issued.append(cert)
        created += int(is_new)
        updated += int(not is_new)

    record(
        db,
        action=AuditAction.CERTIFICATE_ISSUED,
        summary=(
            f"{principal.email} issued records on {event.slug}: "
            f"{created} new, {updated} updated"
        ),
        principal=principal,
        event=event,
        resource_type="event",
        resource_id=event.id,
        request=request,
    )
    try:
        db.commit()
    except IntegrityError:
        # Two overlapping issue calls for the same subject both saw "no
        # certificate yet" and both tried to INSERT, racing the partial unique
        # index that makes issuing idempotent in the first place. The other
        # call's record already exists with a real code; re-running `issue`
        # finds it via `_upsert`'s own lookup and updates it instead.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A concurrent issue request already created one of these records. "
                "Re-run issue to update it."
            ),
        ) from None

    schedule(
        background,
        db,
        event.id,
        WebhookEvent.CERTIFICATE_ISSUED,
        {"event": event.slug, "issued": created, "updated": updated},
    )
    return IssueResult(
        issued=created,
        updated=updated,
        certificates=[_out(c) for c in issued],
    )


@router.post("/events/{slug}/certificates/issue-for-user", response_model=CertificateOut)
def issue_for_user(
    slug: str,
    payload: CertificateIssueForUserIn,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> CertificateOut:
    """Issue one judging or organizing record for one specific person, on
    admin's own decision -- not the bulk pass above, and not something the
    subject can trigger themselves.

    A judge or organizer never sees a "generate my own certificate" flow the
    way a participant does: they see it on their own dashboard once staff
    has issued it (`GET /me/certificates`, unchanged), the same route every
    subject already uses. `PARTICIPATION` and `PLACEMENT` cannot be issued
    through this route -- those still come from the bulk pass, which is the
    one place that actually knows who submitted what.
    """
    event = _staff_event(db, slug, principal, Action.MANAGE)
    target = db.get(User, payload.user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    kind = CertificateKind(payload.kind)
    now = utcnow()
    if kind is CertificateKind.JUDGING:
        if not target.role.at_least(Role.JUDGE):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="This account does not hold the judge role",
            )
        reviews = db.execute(
            select(func.count(JudgeAssignment.id))
            .join(Judge, Judge.id == JudgeAssignment.judge_id)
            .where(
                Judge.event_id == event.id,
                Judge.user_id == target.id,
                JudgeAssignment.status == AssignmentStatus.COMPLETE,
            )
        ).scalar_one()
        title = f"Judging — {event.name}"
        cert_payload = {
            "kind": kind.value,
            "event": event.slug,
            "event_name": event.name,
            "subject": target.display_name,
            "reviews_completed": int(reviews),
            "issued_at": now.isoformat(),
        }
    else:
        if not target.role.at_least(Role.ORGANIZER):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="This account does not hold the organizer role",
            )
        title = f"Organizing — {event.name}"
        cert_payload = {
            "kind": kind.value,
            "event": event.slug,
            "event_name": event.name,
            "subject": target.display_name,
            "issued_at": now.isoformat(),
        }

    cert, is_new = _upsert(
        db,
        event,
        kind=kind,
        title=title,
        subject_name=target.display_name,
        subject_user_id=target.id,
        issued_by=principal,
        payload=cert_payload,
    )
    record(
        db,
        action=AuditAction.CERTIFICATE_ISSUED,
        summary=(
            f"{principal.email} issued a {kind.value} record for {target.email} "
            f"on {event.slug}"
        ),
        principal=principal,
        event=event,
        resource_type="certificate",
        resource_id=cert.id,
        request=request,
    )
    db.commit()
    db.refresh(cert)

    schedule(
        background,
        db,
        event.id,
        WebhookEvent.CERTIFICATE_ISSUED,
        {"event": event.slug, "issued": int(is_new), "updated": int(not is_new)},
    )
    return _out(cert)


@router.get("/events/{slug}/certificates", response_model=Page[CertificateOut])
def list_certificates(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
    pagination: PageParams = Depends(),
) -> Page[CertificateOut]:
    """Staff only, paged. The full list names every participant and judge --
    one row each at hackathon-plus scale, the same order as `/submissions` and
    `/teams` growing past a page."""
    event = _staff_event(db, slug, principal, Action.MANAGE)
    stmt = select(Certificate).where(Certificate.event_id == event.id)
    total = count_of(db, stmt)
    rows = (
        db.execute(
            stmt.order_by(Certificate.kind, Certificate.subject_name)
            .offset(pagination.offset)
            .limit(pagination.per_page)
        )
        .scalars()
        .all()
    )
    return Page(
        items=[_out(c) for c in rows],
        total=total,
        page=pagination.page,
        per_page=pagination.per_page,
        pages=pages_for(total, pagination.per_page),
    )


@router.get("/me/certificates", response_model=list[CertificateOut])
def my_certificates(
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[CertificateOut]:
    """Mine, across every event. The page somebody visits to get their own link."""
    user = require_authenticated(principal)
    rows = (
        db.execute(
            select(Certificate)
            .where(Certificate.subject_user_id == user.id)
            .order_by(Certificate.issued_at.desc())
        )
        .scalars()
        .all()
    )
    return [_out(c) for c in rows]


@router.post(
    "/events/{slug}/certificates/{certificate_id}/revoke", response_model=CertificateOut
)
def revoke(
    slug: str,
    certificate_id: uuid.UUID,
    payload: RevokeIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> CertificateOut:
    """Withdraw a record, with a reason that the public verification shows.

    Revocation that is only visible to us is not revocation: a withdrawn record
    still reads as valid wherever it was already shared.
    """
    event = _staff_event(db, slug, principal, Action.MANAGE)
    cert = db.execute(
        select(Certificate).where(
            Certificate.id == certificate_id, Certificate.event_id == event.id
        )
    ).scalar_one_or_none()
    if cert is None:
        raise HTTPException(status_code=404, detail="Certificate not found on this event")

    cert.revoked_at = utcnow()
    cert.revoked_reason = payload.reason.strip()
    record(
        db,
        action=AuditAction.CERTIFICATE_REVOKED,
        summary=(
            f"{principal.email} revoked the {cert.kind.value} record for "
            f"{quote(cert.subject_name)} on {event.slug}: {cert.revoked_reason}"
        ),
        principal=principal,
        event=event,
        resource_type="certificate",
        resource_id=cert.id,
        request=request,
    )
    db.commit()
    db.refresh(cert)
    return _out(cert)


# --------------------------------------------------------------------------- #
# Public verification
# --------------------------------------------------------------------------- #


@router.get("/signing/public-keys", response_model=list[SigningKeyOut], tags=["public"])
def signing_public_keys() -> list[SigningKeyOut]:
    """Every key a certificate signature might be checked against.

    This is the whole point of asymmetric signing: a verifier fetches this,
    picks the key named by a certificate's `key_id`, and checks the signature
    themselves with nothing else from us -- no account, no trusting our own
    `signature_valid` verdict. Retired keys stay listed (public half only) so
    certificates signed before a rotation keep verifying.
    """
    return [SigningKeyOut(**entry) for entry in public_keys()]


def _load_and_check(db: Session, code: str) -> tuple[Certificate, bool, bool, str | None]:
    """Shared by every public certificate route: look up by code, check the
    signature, check revocation. One place computing "is this genuine", so the
    JSON endpoint, the PDF and the PNG cannot disagree about it.
    """
    cert = db.execute(
        select(Certificate).where(Certificate.code == code.strip().upper())
    ).scalar_one_or_none()
    if cert is None:
        raise HTTPException(status_code=404, detail="No record with that code")

    signature_ok = verify(cert.payload.encode(), cert.signature, cert.key_id)
    revoked = cert.revoked_at is not None

    detail = None
    if not signature_ok:
        detail = (
            "The signature does not match this record's contents. It has been altered "
            "since it was issued and should not be trusted."
        )
    elif revoked:
        detail = "This record was revoked by the organizers."

    return cert, signature_ok, revoked, detail


@router.get(
    "/submissions/{submission_id}/certificates",
    response_model=list[CertificateLookupRow],
)
def submission_certificates(
    submission_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[CertificateLookupRow]:
    """Which of *your own* certificates exist for one project.

    **No longer public -- this was the actual gap.** This used to list every
    certificate issued for a project, code included, to anyone who could
    search up the project name in step 1 of the wizard: knowing a project
    existed was enough to enumerate every teammate's name and walk away with
    their certificate code, with nothing here checking that the caller *was*
    the person named. "Confirm your name" only ever proved the name was
    real, never that it was yours.

    Now: signed in, and either a member of the submitting team -- the same
    membership check `Team.member_ids()` already gives every other
    submission-scoped route -- or staff. A member sees their own
    participation record plus the team's own placement record (which names
    the team, not one person, so any member may fetch it); staff reviewing
    what has been issued sees every subject's record for this project, the
    same breadth the full per-event list already gives them. Verifying a
    code someone already has (`GET /certificates/{code}` and its PDF/PNG
    siblings) stays exactly as open as it always was -- that is the
    *intended* public feature this fixes nothing about; only *discovering* a
    code you do not already have was ever the problem.
    """
    user = require_authenticated(principal)
    submission = db.execute(
        select(Submission)
        .options(selectinload(Submission.team).selectinload(Team.members))
        .where(Submission.id == submission_id)
    ).scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")

    member_ids = {m.user_id for m in submission.team.members}
    is_staff = user.role.at_least(Role.ORGANIZER)
    if user.id not in member_ids and not is_staff:
        raise HTTPException(status_code=403, detail="You are not on this project's team")

    conditions = [Certificate.submission_id == submission_id]
    if is_staff:
        if member_ids:
            conditions.append(Certificate.subject_user_id.in_(member_ids))
    else:
        conditions.append(Certificate.subject_user_id == user.id)

    rows = db.execute(
        select(Certificate)
        .where(Certificate.event_id == submission.event_id, or_(*conditions))
        .order_by(Certificate.kind, Certificate.subject_name)
    ).scalars().all()
    return [
        CertificateLookupRow(code=c.code, kind=c.kind, title=c.title, subject_name=c.subject_name)
        for c in rows
    ]


@router.get("/certificates/{code}", response_model=CertificatePublic, tags=["public"])
def verify_certificate(code: str, db: Session = Depends(get_db)) -> CertificatePublic:
    """Check a record. **No account required — this is the point of the feature.**

    Returns the exact bytes that were signed alongside the signature and the
    `key_id` that names which key to check it against, so a third party can verify
    the Ed25519 signature themselves rather than believing our verdict. It does not
    return the *private* key, obviously -- that would defeat the point; the whole
    reason this is asymmetric is that verification only ever needs the public half,
    published at `GET /api/signing/public-keys`.
    """
    cert, signature_ok, revoked, detail = _load_and_check(db, code)

    return CertificatePublic(
        code=cert.code,
        kind=cert.kind,
        title=cert.title,
        subject_name=cert.subject_name,
        issued_at=cert.issued_at,
        payload=cert.payload,
        signature=cert.signature,
        key_id=cert.key_id,
        signature_valid=signature_ok,
        revoked=revoked,
        revoked_reason=cert.revoked_reason,
        # Valid means both: the maths checks out *and* it has not been withdrawn.
        valid=signature_ok and not revoked,
        detail=detail,
    )


@router.get("/certificates/{code}/pdf", tags=["public"])
def certificate_pdf(code: str, db: Session = Depends(get_db)) -> Response:
    """The same record as `GET /api/certificates/{code}`, drawn as a printable
    PDF rather than read as JSON. No account required, for the same reason.

    Renders whatever `_load_and_check` finds -- a revoked or tampered record
    still renders, stamped as such, rather than 404ing or quietly looking
    genuine. See `app/certificate_render.py`.
    """
    cert, signature_ok, revoked, _detail = _load_and_check(db, code)
    payload = json.loads(cert.payload)
    pdf_bytes = render_pdf(cert, payload=payload, signature_valid=signature_ok, revoked=revoked)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{cert.code}.pdf"'},
    )


@router.get("/certificates/{code}/png", tags=["public"])
def certificate_png(code: str, db: Session = Depends(get_db)) -> Response:
    """The same record, rasterized to a PNG -- for a share card or an easy
    paste into a slide, where a PDF viewer is one step too many."""
    cert, signature_ok, revoked, _detail = _load_and_check(db, code)
    payload = json.loads(cert.payload)
    png_bytes = render_png(cert, payload=payload, signature_valid=signature_ok, revoked=revoked)
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{cert.code}.png"'},
    )
