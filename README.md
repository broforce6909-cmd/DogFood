# Dogfood — Hackathon Submission & Judging Portal

A self-hostable platform for running a hackathon end to end: registration,
teams, submissions, a public gallery, judge assignment and scoring (scored
rubric or pairwise comparison), community voting, and results.

|  |  |
| --- | --- |
| ![The public project gallery, with search, track and tag filters](docs/screenshots/gallery.png) | ![A judge's ballot, mid-review, showing the weighted rubric](docs/screenshots/judge-ballot.png) |
| Public gallery — search, filter by track, filter by tech tag | A judge's ballot — the weighted rubric, live |
| ![Published community-vote results, with a tie shown honestly](docs/screenshots/public-results.png) | ![The organizer dashboard for one event](docs/screenshots/organizer-dashboard.png) |
| Published results — a tie shown as a tie, not broken arbitrarily | Organizer dashboard — submissions, judging, audit, integrations |

<p align="center">
  <img src="docs/screenshots/certificate-wizard.png" alt="The certificate wizard's live preview, dark-themed, with a QR code linking to public verification" width="600">
  <br>
  <em>Certificate preview and download — the exact document a verifier's QR code opens. Finding your own code needs signing in as a team member; verifying a code you already have needs no account at all.</em>
</p>

Screenshots are from the seeded fixture data `docker compose up` starts with —
every page above is one seeded login away, not staged. See
[Run it](#run-it) below to reach any of them yourself in a few minutes.
Regenerate them against a running stack with
`cd scripts/screenshots && npm install && npx playwright install chromium && npm run capture`.

**Contents:** [Run it](#run-it) · [Sign in](#sign-in) ·
[A lap of the product](#a-lap-of-the-product) ·
[What works (T1 Core)](#what-works-t1-core) ·
[Role isolation is in the backend](#role-isolation-is-in-the-backend) ·
[Layout](#layout) · [Tests](#tests) · [Documentation](#documentation) ·
[What it does not do yet](#what-it-does-not-do-yet) · [License](#license)

**Status: all four tiers built (T1-T4) and verified — no new features planned
from here.** Auth, roles, teams and a deadline that holds (T1); judge
assignment, a weighted rubric, backend-enforced role isolation and
cross-judge normalization (T2); community voting — including full
**quadratic voting** — moderated comments and results hidden until published
(T3); a documented REST API, webhooks, and signed, publicly verifiable
certificates (T4). Three passes since then closed real gaps a fresh look
found rather than ones invented for this list — six of ten webhook topics
that silently never fired, a certificate-lookup endpoint with no auth check,
voting configuration with no write path at all — and added judge
calibration, three admin levels and event archival. Full phase-by-phase
history: [ARCHITECTURE.md, "Project history"](ARCHITECTURE.md#project-history).
See [What it does not do yet](#what-it-does-not-do-yet) for the honest list
of what is still open, which is longer than the feature list in places that
matter.

---

## Run it

```bash
docker compose up
```

That is the whole thing. First run builds the images and takes a few minutes;
after that it is seconds.

| Service | URL | What it is |
| --- | --- | --- |
| Web | http://localhost:3000 | Next.js frontend |
| API | http://localhost:8000 | FastAPI backend |
| API docs | http://localhost:8000/docs | OpenAPI / Swagger UI |
| Database | `localhost:5432` | PostgreSQL 16 (`dogfood` / `dogfood`). Already using 5432? Set `DOGFOOD_DB_PORT` |

No cloud account, no API key, no external service, no signup. The database is
a local container volume. Package registries are only reached the first time
images build.

### Sign in

The seed creates a full cast. **Every fixture account uses the password
`dogfood2026`.**

| Account | Role | Use it to see |
| --- | --- | --- |
| `admin@example.com` | admin | Role assignment, deactivation, provisioning judge/organizer/admin accounts directly, posting announcements on any event |
| `manager@example.com` | admin (manager) | Everything an admin does except administering accounts: runs events, reads the global audit, cannot create users, change roles or deactivate |
| `auditor@example.com` | admin (auditor) | Read-only: reads everything an admin reads (including the audit log and its chain check); every write is refused with 403 |
| `organizer@example.com` | organizer | Event setup, tracks, prizes, custom questions, every team's draft |
| `judge.rivera@example.com` | judge | A judge mid-round: six ballots, all scored |
| `judge.whitfield@example.com` | judge | A judge who has not started — six ballots waiting |
| `judge.novak@example.com` | judge | A **track judge**: sees Civic only, and cannot reach the rest |
| `sam@example.com` | participant | A team with a submitted project |
| `ines@example.com` | participant | A team with nothing entered — so a clean voter, with no conflict of interest |

Five events are seeded, so every state is reachable without waiting for a clock:

| Event | State | What it shows |
| --- | --- | --- |
| `dogfood` | Submissions open | Teams, drafts, the deadline counting down |
| `raptors-winter` | **Judging open, half done. Voting open, results hidden** | The judge console, the progress dashboard, normalization on real ballots, a live quadratic vote, comments including one moderated, a populated audit log, a registered webhook and ten signed records |
| `raptors-summer` | Finished (deadline passed), **vote published** | The deadline refusing writes, and the one event where community totals are public |
| `nightowl` | Registration and submissions open, deliberately empty | Registering, forming a team and submitting from nothing |
| `sample-hack-2026` | Closed on the organizers' own date | The organizers' published `fixtures.json`, loaded as written: 40 projects, 30 judges, 123 ballots. The event the acceptance checker runs against (below) |

`raptors-winter` is built with three deliberately different judges — one
generous, one harsh, and one who marks everything a 3 — because that last one is
the case [JUDGING.md](JUDGING.md) argues about, and it should be visible on a
fresh clone rather than described.

### A lap of the product

**Short on time?** Steps 1, 5, 7 and 13 are the fastest tour of the actual
engineering: no-account browsing, a judge scoring the continuous rubric,
normalization visibly reordering the ranking, and a certificate verifying
with no account and no trust in our own verdict.

1. Open http://localhost:3000 — the public gallery, searchable and filterable by
   track and tech tag, with no account at all.
2. Sign in as `sam@example.com` and edit the team's draft; save, then submit.
3. Sign in as `organizer@example.com` to create an event, add tracks, prizes and
   custom questions, and see every team's draft including the ones that never
   got submitted.
4. Try the finished event as a participant: the form is locked, and so is the
   API behind it.
5. Sign in as `judge.whitfield@example.com` and open **Judge** in the nav — six
   ballots waiting on `raptors-winter`. Score one.
6. As the organizer, open `raptors-winter` → **Judging**: who has not
   started, the weighted rubric, batch assignment with a dry run, and CSV export.
7. Then **Results**: raw ranking against normalized ranking, the movement between
   them, and how each judge used the scale. Four of six projects move.
8. Sign in as any participant and open `raptors-winter` → **Vote**. You have
   100 credits; *n* votes on a project costs *n²*. Try putting 11 on one thing and
   read the refusal.
9. Open any project page and leave a comment. As the organizer, hide one — a reason
   is required, and it goes in the log verbatim.
10. Visit **Community results** as a visitor: hidden, because voting is open. As the
    organizer, publish the totals from the Judging page and reload it.
11. Then `raptors-winter` → **Audit log**: every vote, comment, moderation and
    score as a sentence, filterable, with a CSV.
12. `raptors-winter` → **Integrations**: register a webhook (try
    `http://localhost:9000/x` first and read the refusal), issue records, export a
    CSV and import it straight back, and preview the embeddable gallery.
13. Open [`/verify`](http://localhost:3000/verify) in a private window — no account —
    and paste a code from the Integrations page. Ten records are seeded, so one is
    there to check before you issue anything. It names the Ed25519 key that signed
    it, and `/api/signing/public-keys` publishes the key itself — verification
    needs nothing else from us.
14. Pairwise judging is off by default (`raptors-winter`'s Settings panel
    turns it on) because, unlike scored ballots, there is no realistic pairwise
    data to pre-seed without breaking the proof script's reproducibility — see
    [JUDGING.md §6](JUDGING.md). Turn it on, then run
    `python scripts/pairwise_proof.py` from the host to play a short real judging
    session and print the ranking it fits.
15. Sign in as `admin@example.com` and open **Admin**: provision a judge or
    organizer account (a generated password is emailed, never shown here —
    console-logged since no SMTP is configured), change a role, deactivate and
    reactivate an account, and post an announcement to any event from the same
    page an organizer would use on their own — pick one from the dropdown and
    it appears on that event's page immediately.

### Reset to a clean, freshly seeded state

```bash
docker compose down -v && docker compose up
```

Do this before a demo or a recording, not just when something is broken. The seed
sets its event windows relative to *when it ran*: `raptors-winter` has
judging open from a day before the seed to five days after it, and its voting
window closes a day earlier still. On a database seeded about a week ago, its
ballots and votes correctly read as closed, and the judging and voting
browser tests fail for exactly that reason. A fresh `down -v` restores them.

### The organizers' acceptance checker

`docker compose up` also loads the organizers' published `fixtures.json` as its own
event, **Sample Hack 2026** (`sample-hack-2026`): 40 projects, 30 judges, 8 tracks,
123 ballots, closed on the file's own date. It prints the four test logins the checker
needs, in the shape the spec shows, and they are the same after `down -v && up`:

```
seeded. test logins:
  organizer    Cookie: dogfood_session=org_...
  judge_a      Cookie: dogfood_session=jdg_a_...
  judge_b      Cookie: dogfood_session=jdg_b_...
  participant  Cookie: dogfood_session=prt_...
```

```bash
python3 run.py .dogfood.toml            # the checker, standard library only
```

`.dogfood.toml` points it at the API (`http://localhost:8000`). The latest saved output
is `acceptance-report.txt`. The checker only has checks for T1 and T2, so those are
the tiers `.dogfood.toml` claims; T3 and T4 are built and covered by our own tests
(see [What works](#what-works-t1-core)), but nothing the checker does can verify them.

Where the file did not fit our schema (a team entered twice, three team names used more
than once, judges listing two tracks) the loader adapts and says so at boot rather than
dropping anything silently. The full list, and why each route in `.dogfood.toml` is the
one it is, is in [OPERATIONS.md](OPERATIONS.md#the-organizers-fixtures-and-the-acceptance-checker).

---

## What works (T1 Core)

| T1 requirement | Where |
| --- | --- |
| Authentication and sessions | `POST /api/auth/register`, `/login`, `/logout`, `GET /api/auth/sessions` |
| Real role model | `visitor < participant < judge < organizer < admin`, all decided in `backend/app/access.py` |
| Event creation, dates, tracks, prizes | `/api/events`, `/api/events/{slug}/tracks`, `/prizes`, `/questions` |
| Team formation by invite link | `GET /api/teams/{id}/invite`, `POST /api/teams/join`, rotatable token |
| Draft-and-edit submissions | `POST /api/submissions`, `PATCH`, `/submit`, `/unsubmit` |
| Deadline enforcement that holds | One comparison, in `check_access` — every write path goes through it |
| Public gallery with search and filter | `GET /api/gallery` — free text, track, tech tag, sort, paginate |

### T2 Judging

| T2 requirement | Where |
| --- | --- |
| Judge invitation and assignment | `POST /api/events/{slug}/judges`, `POST .../assignments` — batch, balanced, conflict-free, idempotent, seedable, with a dry run |
| Weighted, organizer-configurable rubric | `/api/events/{slug}/criteria` — per-event criteria, weights and score ranges, applied at read time |
| Role isolation, enforced in the backend | `backend/app/access.py`; proved over HTTP in `tests/test_judging_isolation.py` |
| Live progress dashboard | `GET /api/events/{slug}/judging/progress` — names who has not started |
| Cross-judge normalization | `GET /api/events/{slug}/results`; method and proof in [JUDGING.md](JUDGING.md) |
| CSV export at every stage | `GET /api/events/{slug}/export/{tracks,teams,submissions,judges,assignments,scores,results,votes,audit}.csv` |

That is the whole of the spec's T2 list. Everything below it is ours, not required by
any tier — named separately so a claim about what T2 needs is never confused with a
claim about what we added on top of it:

| Beyond T2 | Where |
| --- | --- |
| Pairwise judging (bonus challenge, Phase 5) | `GET/POST /api/events/{slug}/pairwise/{next,compare,results}` — the Gavel approach: judges pick a winner between two projects, ranked by Bradley-Terry (MM). Additive to the rubric above, not a replacement. Method and proof in [JUDGING.md §6](JUDGING.md) |
| Event archival | `POST /api/events/{slug}/archive` and `/unarchive` (organizer/admin); `GET /api/events?archived=exclude\|include\|only`. An archived event is frozen -- every write to it or anything it owns is refused -- and leaves the default lists, but its gallery, results and certificates keep working. See [ARCHITECTURE.md](ARCHITECTURE.md#event-archival) |
| Admin levels | `owner` / `manager` / `auditor` on `Role.ADMIN`, set via `POST /api/users` and `PATCH /api/users/{id}/role` (owner-only). Managers run everything except account administration; auditors are read-only, enforced for every non-GET request. Existing admins are owners. See [ARCHITECTURE.md](ARCHITECTURE.md#admin-levels) |
| Judge calibration | `GET/POST/DELETE /api/events/{slug}/calibration/projects`, `GET /api/events/{slug}/calibration/report` (organizer); `GET /api/judging/calibration`, `PUT /api/judging/calibration/{id}/scores` (judge). Judges score practice projects with organizer-set expected scores before real judging; the report flags harsh/generous tendencies. Never counts toward a result. See [JUDGING.md](JUDGING.md) |

### T3 Public

| T3 requirement | Where |
| --- | --- |
| Voting with configurable access | `voting_access` per event: `open_link`, `email_gated`, `authenticated` |
| Better than one-person-one-vote | **Quadratic voting** as a full option — *n* votes cost *n²* from a fixed budget, validated as a whole ballot |
| Comments on gallery projects | `/api/gallery/{id}/comments` — authenticated to post, hidden-not-deleted moderation with a mandatory reason |
| Results hidden during the voting window | `Action.READ_TALLY`; `results_public_at` is NULL on every new event, so hidden is the default rather than a step to remember |
| Randomised ballot ordering | Seeded per voter, stable across refreshes — random *between* voters, not *within* one |
| Anti-abuse that means something | Partial unique indexes for duplicate detection, Postgres-backed rate limits, and an audit log of 50 verbs |

The abuse argument — what each mode stops, and what it does not — is
[THREAT-MODEL.md](THREAT-MODEL.md). It is deliberately as long on the failures as on
the defences.

### T4 Stretch

| T4 requirement | Where |
| --- | --- |
| REST API covering every UI action | 137 endpoints, OpenAPI at [`/openapi.json`](http://localhost:8000/openapi.json). The UI has gone through the API since T1; there is no private path. |
| Webhooks | `/api/events/{slug}/webhooks` — 13 topics, HMAC-signed deliveries, delivery history, a Test button, and an SSRF guard that refuses internal addresses |
| Certificate generation | `POST /api/events/{slug}/certificates/issue` — participation, judging and placement records for everyone at once, idempotent and bulk. Judging and organizing records are the one kind *not* self-service: `POST .../certificates/issue-for-user` names one account directly, staff-only, because neither a judge nor an organizer requests their own the way a participant does. |
| Signed, publicly verifiable records | `GET /api/certificates/{code}` — **no account needed**, returns the signed bytes, the Ed25519 signature, and the `key_id` naming which published public key ([`GET /api/signing/public-keys`](http://localhost:8000/api/signing/public-keys)) checks it — no need to trust our verdict. Human page at [`/verify`](http://localhost:3000/verify) |
| A visual certificate, not just JSON | `GET /api/certificates/{code}/pdf` and `/png` — the same signed record drawn as a printable, shareable document, with a QR code back to `/verify`. Signs nothing new: it renders exactly what the JSON endpoint already returns, so a revoked or tampered record is stamped as such rather than looking genuine with a nicer layout. See `backend/app/certificate_render.py`. |
| Embeddable gallery widget | `GET /embed/gallery/{slug}` (iframe) and `…​.js` (one-line snippet) |
| Bulk import and export | `POST /api/events/{slug}/import/{tracks,teams,submissions}` — same columns as the export, round trip tested |

What the four kinds of record look like. Each image is the PNG that
`GET /api/certificates/{code}/png` returns, rendered by the running stack from a
record issued through the product (event names and codes will differ on your
install), and each carries a QR code back to `/verify`:

|  |  |
| --- | --- |
| ![A participation certificate naming the participant, their project and team, with the signature line and a QR code](docs/screenshots/certificate-participation.png) | ![A judging certificate naming how many ballots the judge completed, with the signature line and a QR code](docs/screenshots/certificate-judging.png) |
| Participation — the participant, plus the project and team named in the signed record | Judging — the number of ballots completed is part of what is signed |
| ![An organizing certificate, with the signature line and a QR code](docs/screenshots/certificate-organizing.png) | ![A placement certificate for a team's project entry, with the signature line and a QR code](docs/screenshots/certificate-placement.png) |
| Organizing — issued one account at a time from the Integrations page | Entry (placement) — the team's project; created by "Issue records for everyone" |

Regenerate one with
`curl -o docs/screenshots/certificate-participation.png http://localhost:8000/api/certificates/<code>/png`,
using a code from the Integrations page or `/verify`.

Verifying a record takes one command and no account:

```bash
# Any code from /verify or the organizer's Integrations page.
curl -s http://localhost:8000/api/certificates/ABCD1234EFGH | python -m json.tool
```

It returns `payload` (the exact signed bytes) next to `signature` and `key_id`, so you
can fetch the named public key from `/api/signing/public-keys` and check the Ed25519
signature yourself rather than believing the `valid` field.

The submission field set is the one stable across the platforms the brief
studied: name, tagline, long description, thumbnail, image gallery, demo video
URL, repository URL, live link, tech tags, track, plus organizer-defined custom
questions.

137 endpoints are published at `/docs`. Every one of them takes either the
session cookie or `Authorization: Bearer <token>`, so the whole API is usable
from `curl` — see [ARCHITECTURE.md](ARCHITECTURE.md#request-lifecycle).

### Role isolation is in the backend

The rule this project is organised around: **removing every check from the UI
must not change what the API permits.** All authorization lives in one function,
`check_access(user, resource, action)` in
[`backend/app/access.py`](backend/app/access.py). The frontend hides buttons as a
courtesy; the API says no regardless.

You can check this the way the acceptance suite does — with `curl`, not a
browser. The three blocks below are bash (Git Bash, WSL, macOS/Linux); on
native PowerShell either run them in Git Bash instead, or replace each
trailing `` \ `` with a backtick `` ` `` — PowerShell's own line-continuation
character, not bash's.

```bash
# Log in, keep the token.
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"ines@example.com","password":"dogfood2026"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# Another team's unsubmitted draft is a 404, not a 403: a 403 would confirm
# that it exists.
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "authorization: Bearer $TOKEN" \
  http://localhost:8000/api/submissions/<some-other-teams-draft-id>

# The organizer-only user list is a 403.
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "authorization: Bearer $TOKEN" http://localhost:8000/api/users
```

And the T2 line the brief cares most about — *if I can curl another judge's scores
it is not isolation*:

```bash
# Log in as one judge, find one of their ballots.
J1=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"judge.rivera@example.com","password":"dogfood2026"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# Every ballot in the organizer's grid, as a judge: 403.
curl -s -o /dev/null -w '%{http_code}\n' -H "authorization: Bearer $J1" \
  http://localhost:8000/api/events/raptors-winter/assignments

# The aggregate, as a judge: 403.
curl -s -o /dev/null -w '%{http_code}\n' -H "authorization: Bearer $J1" \
  http://localhost:8000/api/events/raptors-winter/results

# A peer's ballot by id, as a judge: 403. (Ask the organizer for an id first.)
```

The track judge is the other half. `judge.novak@example.com` judges Civic only,
and `GET /api/judging/queue` returns three Civic ballots — not the three Logistics
projects, which they cannot reach by id either.

And the T3 line — results hidden during the voting window:

```bash
# Voting is open on raptors-winter, so the tally is organizer-only.
curl -s -o /dev/null -w '%{http_code}\n' \
  http://localhost:8000/api/events/raptors-winter/voting/results      # 403

# The finished event's vote was published, so it is public.
curl -s -o /dev/null -w '%{http_code}\n' \
  http://localhost:8000/api/events/raptors-summer/voting/results      # 200

# Publishing the community vote does NOT publish the judges' scores.
curl -s -o /dev/null -w '%{http_code}\n' \
  http://localhost:8000/api/events/raptors-summer/results             # 403
```

That last one is the distinction worth checking: `READ_TALLY` and `READ_RESULTS` are
two different rights over two different secrets.

---

## Layout

```
.
├── docker-compose.yml    one command to a seeded, running portal
├── backend/              FastAPI app, schema, seed fixtures
│   ├── app/
│   │   ├── main.py       ASGI entrypoint, router mounting
│   │   ├── access.py     ALL authorization — one function, one place to audit
│   │   ├── models.py     ORM models; Base.metadata is the schema source of truth
│   │   ├── deps.py       request-scoped session + "who is asking"
│   │   ├── security.py   Argon2id passwords, HMAC'd session tokens
│   │   ├── routers/      auth, users, events, teams, submissions, gallery
│   │   ├── schemas.py    request/response models
│   │   ├── config.py     environment-driven settings
│   │   ├── db.py         engine, session factory, declarative base
│   │   └── seed.py       idempotent fixture data
│   ├── alembic/          migrations; env.py wires Base.metadata + DATABASE_URL
│   └── entrypoint.sh     wait for db → migrate → seed → serve
├── frontend/             Next.js (App Router, TypeScript)
├── tests/                our own tests, beyond the acceptance suite
└── docs/
```

## Tests

Our own tests live in `tests/`. They split in two:

```bash
cd backend
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest ../tests -q
```

- **Pure tests** — the access-control matrix, the role ladder, schema invariants —
  need no database and always run.
- **API tests** exercise real routes against **real Postgres**, because the
  constraints being relied on (composite foreign keys, partial uniqueness,
  `timestamptz`, array containment) are Postgres behaviour and a SQLite stand-in
  would test a different program.

If Postgres is not reachable, the API tests **skip rather than fail**, and pytest
says so with `-rs`. A green run with most tests skipped is not a passing suite —
always read the skip count:

```bash
docker compose up -d db
cd backend && .venv/Scripts/python -m pytest ../tests -q -rs
```

Expected: **840 passed, 1 skipped** (the skip is
`tests/test_certificate_render.py`'s real-render test, which needs WeasyPrint's
native Pango/GObject libraries — present in the Docker image, not on a bare
Windows host; see that file's `skipif`). Any *other* skip means Postgres is not
reachable, not that this is still a passing run.

The suite splits roughly into: the access-control matrix and the judging and voting
maths (no database, instant), and the API tests (real Postgres).

Four files are worth reading even if you never run them, because they are where the
arguable parts are pinned down:

| File | What it settles |
| --- | --- |
| `tests/test_scoring.py` | The normalization, including the judge who marks everything a 3 |
| `tests/test_assignment.py` | The four assignment constraints, over generated populations |
| `tests/test_judging_isolation.py` | Role isolation, over HTTP — written before the feature |
| `tests/test_voting_abuse.py` | One named attack per test, including the ones that are **not** stopped |
| `tests/test_webhooks.py` | The SSRF guard, target by target — the sharpest surface T4 adds |
| `tests/test_signing.py` | Why a hash is not a signature, and why bytes are signed rather than objects |
| `tests/test_import_export.py` | The round trip, literally: same file out, same file back |

### Browser E2E (`e2e/`)

The suite above answers "does the API do the right thing" -- every one of its
"UI" tests is really an HTTP client with a bearer token, per `access.py`'s own
design. It cannot catch a frontend change that silently breaks a flow while
every route it calls keeps working correctly, which is exactly the gap a real
browser closes. `e2e/` is a small, separate Playwright suite that drives the
actual UI for the handful of journeys worth protecting at that level, not
exhaustive page coverage:

- registering, forming a team, and submitting a project into the gallery
- a judge scoring a ballot on the continuous 0.1-step scale
- casting a quadratic vote and seeing the cost
- a visitor viewing published community results with no account
- a participant finding and previewing their own certificate
- the three admin levels reflected on the Accounts page (owner sees account controls; manager and auditor do not)
- the sign-in role tab: the wrong tab is refused with a message and no session, the right one signs in
- an organizer archiving an event (frozen, read-only, out of the default list) and bringing it back
- an organizer adding a judge-calibration practice project, a judge scoring it low, and the report flagging that judge as harsh

Runs against a real, already-up stack rather than starting its own server, the
same pattern `scripts/screenshots/capture.js` already uses:

```bash
docker compose up -d --wait
cd e2e && npm ci && npx playwright install --with-deps chromium
npm test
```

Wired into CI as its own job (`e2e-tests` in `.github/workflows/ci.yml`),
separate from `backend-tests` and `frontend-build` on purpose: a failure here
should read as "a real browser journey broke," not get lost in an unrelated
job's log. On failure, CI uploads the Playwright HTML report (traces, videos,
screenshots) as a build artifact.

### If the tests skip with "password authentication failed"

This bites anyone who already runs Postgres natively on 5432. The host port is
then owned by *that* server, the container's published port is shadowed, and the
symptom is a credentials error rather than a port conflict — because you really
did connect, just to the wrong database.

Publish the container on a free port and point the tests at it:

```bash
DOGFOOD_DB_PORT=55432 docker compose up -d
cd backend
DOGFOOD_TEST_DATABASE_URL=postgresql+psycopg://dogfood:dogfood@localhost:55432/dogfood_test \
  .venv/Scripts/python -m pytest ../tests -q -rs
```

`DOGFOOD_DB_PORT` only changes the **host** mapping. The API always reaches the
database at `db:5432` inside the Compose network, so the portal itself is
unaffected either way.

## Documentation

| File | Contents |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The shape of the system and why |
| [DATA-MODEL.md](DATA-MODEL.md) | Schema, plus import and export paths |
| [JUDGING.md](JUDGING.md) | Assignment strategy, scoring maths, normalization |
| [THREAT-MODEL.md](THREAT-MODEL.md) | Vote and submission abuse: what is stopped, and what is not |
| [OPERATIONS.md](OPERATIONS.md) | Backup/restore, secret rotation, health checks, logs |

## What it does not do yet

Named rather than implied, because the brief is right that an honest gap list
beats an inflated claim. Most recent first, not chronological — the caveats on
the newest work are what a reader checking this list right now actually wants.

**From loading the organizers' fixtures** (`sample-hack-2026`, OPERATIONS.md):

- **Nine judges are wider than the file says.** The file gives them two tracks each;
  a judge record holds one track or all of them, so they load as all-tracks judges.
  No loaded ballot is affected, but a fresh assignment run would treat them as
  unrestricted. Fixing it means a judge-to-tracks table.
- **Three of the file's 126 reviews are dropped.** One team appears twice
  (`prj_07`, `prj_41`), and we hold one submission per team; the later entry is kept.
  Three judges reviewed both entries with different scores, and their review of the
  later one stands. The choice is defensible, not neutral, and is printed at every load.
- **The checker proves less of the deadline than it looks.** Its `{"title", "summary"}`
  body cannot create a project in our API (no `team_id`), so against `POST /api/submissions`
  its "closed event refuses submissions" would pass on a 422 that never reached the
  deadline. `.dogfood.toml` points it at `POST /api/submissions/{id}/submit`, which
  does (409, "The submission deadline has passed"); `tests/test_fixture_loader.py` also
  proves the same request is allowed an hour before the close and refused a second after.
- **The unfiltered front-page gallery does not show the checker's three fixture titles on
  page one.** It takes turns between events, newest first within each (`sort=mixed`), so on
  the running stack (59 public projects, four events) the fixture event has 7 of the 24
  slots, up from 5 under plain newest-first. "Glass Signal", "Small Meadow" and "Deep
  Compass" sit deeper in that event's own order (pages 3, 2 and 2). The per-event gallery
  has "Deep Compass" on page one, and is what the checker is pointed at.

**From the last round of features** (judge calibration, admin levels, event
archival, sign-in tab checks, the scale pass and the certificate changes):

- **Judge calibration is a noisy flag, not a measurement.** With one or two
  practice projects a verdict is a starting point for a conversation; the threshold
  (0.10 of a criterion's range) is a default, not a finding; a judge who marks
  everything a 3 can come out "aligned" on the mean and only shows in the mean
  absolute deviation. One organizer sets the expected scores, so the flag measures
  distance from *their* opinion. JUDGING.md §10.
- **Admin levels are platform-wide.** There is no admin scoped to one event, and an
  auditor can still change their own password and sign out (the `/api/auth/*`
  exception). Existing admins became owners; new ones default to manager.
- **An archived event is frozen, so some things need an unarchive first.** Deleting
  it is refused on purpose, and certificates cannot be issued or revoked on it. The
  settings forms are still drawn on an archived event's organizer page and only
  fail with a message when submitted; hiding them was left undone.
- **Older certificates lack the project name.** Participation records issued before
  it was signed keep rendering (team only) until an organizer re-issues, which
  refreshes the signed payload and keeps the code.
- **The certificate QR code points at `WEB_BASE_URL`, which defaults to
  `localhost`.** Right for a laptop, dead for anyone else; OPERATIONS.md explains
  setting it before certificates are issued or downloaded.
- **The API container runs uvicorn with `--reload`** (`backend/entrypoint.sh`),
  which is a development convenience, not a production setting, and which does not
  reliably notice edits on a Windows bind mount (a manual `docker compose restart
  api` is needed). One process, no worker pool: the scale figures in ARCHITECTURE.md
  are for that shape.
- **The measured performance is from one laptop.** Run-to-run variance was roughly
  2x, the ~8 s left on `GET /api/audit/verify` at 110,000 rows is the cost of the
  SHA-256 chain, and the browser suite is only reliably green single-worker on a
  memory-starved machine (CI runs two workers).

**Everything below is earlier — every tier was already built by this point.**
A Phase 5 self-audit closed several gaps that used to be
listed here, some named in advance and some found only by checking every
feature's read side against its write side rather than trusting that the
existence of one implied the other:

- **Asymmetric certificate signing** (Ed25519) with real key rotation, replacing
  the HMAC design a verifier had to trust rather than check.
- **A hash-chained, tamper-evident audit log**, and a migration tool (Alembic),
  both named as the largest gaps through four phases.
- **Pairwise judging** (Bradley-Terry, Gavel-style), additive to the scored rubric.
- **Dual-keyed rate limiting** on login, voting and commenting, closing a gap
  where a shared IP shared one budget and a rotating address got a fresh one.
- **A stored-XSS class in link fields**, closed on the JSON API *and* found still
  open on the CSV bulk-import path, which wrote straight to the ORM around the
  same validator.
- **Voting configuration had no write path at all.** `voting_access`,
  `voting_method`, `vote_credits` and `votes_per_voter` had been readable on
  every event since Phase 3 — every voter-facing route reads them — but neither
  `EventCreate` nor `EventUpdate` ever had a field for any of them. A real
  organizer had no way to turn on quadratic voting, open-link voting, or
  email-gated access at all; every event that ever used anything but the default
  was seeded fixture data. Fixed the same way as the audit-log and webhook gaps
  below: by checking a feature's read side against its write side.
- **Six of ten webhook topics silently never fired.** `submission.updated`,
  `team.created`, `judge.invited`, `assignments.created`, `results.published`
  and `ballot.completed` were all listed as subscribable by
  `GET .../webhooks/topics`, and none of them was ever passed to
  `hooks.schedule()` anywhere in the application — an organizer who subscribed
  to one got a hook that looked configured and never fired.

The list below is what is left after that pass.

Known gaps inside T4:

- **Webhooks are not retried.** Five consecutive failures disable a hook; there is no
  backoff and no queue, because a scheduler is a dependency this stack does not have.
  The organizer's fix is the Test button.
- **Webhook secrets are stored in plaintext**, because the HMAC must be computed from
  them. The only secret in this schema that is not a digest, and the reasoning is in
  [DATA-MODEL.md](DATA-MODEL.md#phase-4-columns).
- **A bulk import cannot be undone.** The audit entry records the counts, not the
  previous values.
- **`SIGNING_ACTIVE_PRIVATE_KEY_PEM` and `SESSION_SECRET` ship with development
  defaults.** Right for a one-command local install, wrong everywhere else, and
  nothing enforces the change. Unlike the old symmetric design, rotating the signing
  key in a real deployment does not invalidate anything already issued — see
  "Key rotation" in `backend/app/signing.py`.

Known gaps inside T3, named rather than implied — and argued at length in
[THREAT-MODEL.md](THREAT-MODEL.md#10-what-we-did-not-stop):

- **There is no real Sybil resistance in `open_link` or `email_gated` voting.** No
  email is ever sent, so an address is a claim rather than a credential. Those modes
  buy a popularity signal, not a count of people, and the tally labels itself as
  such. `authenticated` is the only mode with meaningful duplicate resistance.
- **Rate limiting is still IP-based at its core**, and IP is shared behind a NAT and
  spoofable at the edge. Login, voting and commenting also key independently on
  account/voter/user identity (`ratelimit.enforce_dual`), so a shared IP no longer
  shares one budget — but there is no per-IP defense for the rest of the API, and
  limits stay generous on purpose, because a limiter that blocks a real voter gets
  switched off.
- **No CSRF token.** `SameSite=lax` on the session cookie is what actually stops it,
  for `web` and for the API's own mutating routes directly (verified: the API accepts
  that cookie too, not bearer-only, and no mutating route is exposed over `GET`, the
  one method `SameSite=lax` does not block cross-site). Next's own `Origin` check adds
  a second layer in front of `web` specifically. Neither is the explicit per-session
  token a reviewer looks for.
- **Votes are not secret from organizers.** A real secret ballot would have to
  separate the voter from the vote at write time, which breaks "one vote per person".
- ~~`voters.ip_address` is kept indefinitely with no retention policy.~~ **Fixed
  in Phase 5**: `voter_ip_retention_days` (default 90) scrubs `ip_address` and
  `user_agent` from voter rows older than that, opportunistically on every
  `claim_ballot` call — the same pattern `ratelimit.purge` already used.

Known gaps inside T2, named rather than implied:

- ~~No inter-rater reliability reporting.~~ **Fixed in Phase 5**: `GET
  /{slug}/results/disagreement` flags a submission whose judges' normalized
  scores still differ by more than a threshold, and `POST
  .../third-review` routes it to one more, uninvolved judge — additive, never
  a replacement for the ballots that disagreed. See
  [JUDGING.md §7](JUDGING.md#7-disagreement-third-review-and-consistency-flags).
- ~~Track judges are normalized against a global mean that includes tracks they
  never saw.~~ **Fixed in Phase 5**: normalization is now scoped per submission's
  track (`normalize_by_track()`, [JUDGING.md §3](JUDGING.md#3-cross-judge-normalization)).
  A judge who reviews multiple tracks with no restriction now gets one calibration
  per track, not one averaged across all of them.
- **Assignment ignores expertise.** Tracks are the only affinity signal; a judge
  who knows Rust is no likelier to get the Rust project.
- **Pairwise judging's coverage is a greedy heuristic, not a balance guarantee.**
  `pick_next_pair` prefers the least-compared eligible projects, but nothing
  guarantees every project ends up with the same number of comparisons the way
  round-robin scheduling would. `GET .../pairwise/results` now at least reports
  the resulting balance (min/max/mean comparisons, coverage %) rather than
  leaving an organizer to infer it from the per-project table. Named honestly in
  [JUDGING.md §6](JUDGING.md#6-pairwise-judging)'s own caveats.
- **No conflict-of-interest recusal *history* surfaced anywhere but the audit
  log.** `JudgeRecusal` rows exist and are enforced by both assignment paths
  (§7), but there is no dedicated organizer UI listing every recusal on an
  event at a glance — only per-judge, via `GET
  /{slug}/judges/{judge_id}/recusals`.

Not a gap, but worth recording because it is the kind of claim readers test: the
judge console works with **JavaScript switched off**. Submitting a ballot with no
JS returns `303 → /judging/{id}?saved=1` and writes exactly the posted scores —
verified, including the weighted total.

Known gaps inside what *is* built more generally:

- **No object storage.** Images are URLs; there is no upload path.
- **Email is one-way and best-effort.** Phase 6 added outbound mail
  (`app/email.py`) for registration confirmation and a results-published
  notification, over plain SMTP with a console-log fallback when none is
  configured (MailHog is a drop-in target for local testing). It does not
  verify addresses, cannot reset a password (there is no reset flow), and
  invite links are still copied by hand rather than emailed.
- **`session_cookie_secure` defaults to false** so local HTTP works. A real
  deployment must set it to `true` behind TLS.
- **The fixture password is in this README** and `SESSION_SECRET` has
  `change-me-in-production` in its name. Both are deliberate for a seeded demo
  and both are unacceptable in production.
- ~~No pagination on organizer/admin list endpoints.~~ **Fixed in a later
  pass.** A Phase 5 load pass had measured, not assumed, that the query layer
  itself was sound (no N+1, no missing index -- see ARCHITECTURE.md's
  "computed on every request") while `GET .../submissions`,
  `GET .../assignments`, `GET .../teams` and `GET /api/users` each returned an
  unbounded array -- fine at the seeded fixture's scale, a 1MB response at
  200+ submissions. One shared `Page[T]`/`PageParams` implementation
  (`app/pagination.py`) now covers those four plus `/comments`,
  `/registrations`, the organizer's `/certificates` list and
  `/webhooks/{id}/deliveries` -- eight endpoints, all following the same
  offset/limit shape, with the frontend pages that call them updated to match.
  See ARCHITECTURE.md for the full account, including why it was named rather
  than patched hastily the first time.

## License

[MIT](LICENSE).
#   D o g F o o d  
 