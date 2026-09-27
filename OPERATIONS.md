# Operations

What an operator needs once this stops being a laptop demo and starts holding
a real event's data: backup, restore, secret rotation, and where to look when
something is wrong. Everything here assumes the `docker compose` shape
described in [README.md](README.md) and [ARCHITECTURE.md](ARCHITECTURE.md);
a deployment that replaces Postgres or the container runtime substitutes its
own equivalent commands but keeps the same properties.

## Backup

```bash
docker compose exec db pg_dump -U dogfood -Fc dogfood > backup.dump
```

`-Fc` (custom format), not plain SQL: it compresses, and it is what
`pg_restore` needs for selective or parallel restore — a plain `.sql` dump
only supports replaying the whole file in order.

**What is in the dump**: every row this project writes — events, teams,
submissions, ballots, votes, comments, the audit log, webhook registrations
and their delivery history, issued certificates. **What is not**: anything
that lives in the environment rather than the database. There is no object
storage to also back up — every image and video is an external URL
(`ARCHITECTURE.md`'s "Deliberate non-goals"), not a file this stack holds —
but two things do live outside Postgres and a database backup alone will not
recover them:

- **Secrets** (`SESSION_SECRET`, `SIGNING_ACTIVE_PRIVATE_KEY_PEM`,
  `SIGNING_ACTIVE_KID`, `SIGNING_RETIRED_KEYS`) — set via environment
  variables (see `backend/.env.example`), not stored in any table. Restoring
  a database backup to a fresh host without also restoring the *same*
  signing key makes every previously issued certificate fail verification
  (see "Rotating secrets", below) and invalidates every session, even though
  every row is intact.
- **The `dogfood` database name/role itself**, if restoring to a brand-new
  Postgres instance rather than the same running container — see "Restore".

A cron job or CI schedule that runs the command above and ships the resulting
file offsite is the operator's own choice; nothing in this stack schedules it,
the same way nothing in this stack runs a job scheduler at all (see
`app/hooks.py`'s module docstring on why there is no job runner here).

## Restore

```bash
docker compose exec -T db pg_restore -U dogfood -d dogfood --clean --if-exists < backup.dump
```

`--clean --if-exists` drops each object before recreating it, which is what
makes this safe to run against a database that already has *some* schema in
it (a fresh `docker compose up` that has already run migrations and seeded
fixtures, for instance) rather than only against a truly empty one. It is
still destructive to whatever was already in the target database — this
restores the dump's rows, it does not merge them with what is there.

After restoring, run migrations before starting the API against the restored
database:

```bash
docker compose exec api alembic upgrade head
```

A backup taken before a later migration shipped will restore a schema at an
older revision; `alembic upgrade head` is what brings it forward, the same
step `entrypoint.sh` already runs on every normal startup (see
ARCHITECTURE.md's "Startup sequence"). Restoring is not a supported way to
roll a schema *backward* — there is no tooling here for that, on the
assumption that an operator restoring a backup is recovering from data loss,
not un-shipping a migration.

## Rotating secrets

**Session secret** (`SESSION_SECRET`): signs the session cookie. Rotating it
invalidates every live session — every logged-in user is signed out — because
a cookie signed with the old secret no longer verifies against the new one.
This is the correct, intended consequence of rotation, not a bug to work
around: an operator who suspects the secret leaked wants exactly this.

**Signing key** (`SIGNING_ACTIVE_KID` / `SIGNING_ACTIVE_PRIVATE_KEY_PEM`):
asymmetric (Ed25519), so rotating it does **not** invalidate certificates
already issued — that is the entire reason `app/signing.py` uses an
asymmetric scheme instead of an HMAC. To rotate:

1. Generate a new keypair: `openssl genpkey -algorithm ed25519 -out new_key.pem`.
2. Move the *current* active key's public half into `SIGNING_RETIRED_KEYS`
   (a JSON array of `{"kid": "...", "public_key_pem": "..."}` — see
   `backend/.env.example`), so certificates it already signed keep verifying
   against `GET /api/signing/public-keys`.
3. Set `SIGNING_ACTIVE_KID` to a new, not-previously-used id and
   `SIGNING_ACTIVE_PRIVATE_KEY_PEM` to the new private key.
4. Restart the API. New certificates sign under the new key; old ones still
   verify under the retired one, forever, because the retired public key is
   never deleted.

**CORS origins / web base URL**: not secrets, but worth naming here since
getting them wrong after a domain change breaks the frontend silently (a
CORS-rejected request looks like a network error in the browser, not a
config error). `Settings.model_post_init` (see `backend/app/config.py`)
refuses to start in `ENVIRONMENT=production` with a wildcard CORS origin, but
does not know if a real origin string is merely *stale* after a domain move.

## Data retention

`voters.ip_address` and `voters.user_agent` exist for one documented reason —
abuse investigation while a voting round is live or freshly closed — and that
investigation window has an end. Nothing needs scheduling for this: every
call to `POST /{slug}/voting/claim` opportunistically scrubs both columns off
any voter row older than `VOTER_IP_RETENTION_DAYS` (default 90) before
proceeding, the same pattern `app/ratelimit.py`'s `purge()` uses for expired
rate-limit counters. An event with no further voting activity after its
window closes will still have this run the next time *any* event on the
install gets a claim request, which is a reasonable bound for a hackathon-
scale install and not a guarantee of same-day scrubbing — if that matters for
a specific compliance need, trigger a claim request (or extend this into a
real scheduled task) rather than assume it ran on a timer.

### Finishing an event: archive it, do not delete it

When an event is over, **archive** it (`POST /api/events/{slug}/archive`, or
"Archive this event" on the organizer's event page) rather than deleting it.
Nothing is removed: the gallery, the results and every certificate keep working
for anyone with the link, and the audit log keeps its history. What changes is
that the event is *frozen* -- every write to it or anything it owns is refused --
and that it drops out of the default event lists (`GET /api/events?archived=only`
lists just the archived ones).

Deleting is the destructive option and is deliberately a step further away: an
archived event cannot be deleted until it is unarchived, so removing one takes two
deliberate, separately audited actions. Certificates cannot be issued or revoked on
an archived event either -- issue them first, or unarchive, do it, and archive
again. Only staff can archive or unarchive; a read-only admin cannot.

## Seeded demo data ages

The seed sets its event windows relative to the moment it ran. A database seeded
more than about a week ago will show `raptors-winter`'s judging and voting as
closed, which is correct behaviour and looks like breakage in a demo. Reset with
`docker compose down -v && docker compose up` before a demo or a recording; see
README.md, "Reset to a clean, freshly seeded state".

## The organizers' fixtures and the acceptance checker

`fixtures.json` (forty projects, thirty judges, eight tracks) and `run.py` (the
acceptance checker) are at the repo root. Reading them settled what this section used
to leave open: **the file is data the portal is expected to be seeded with**, not data
the checker loads. `run.py` only *reads* `fixtures.json` -- for three project titles and
the event's `submissions_close` -- and never writes to the portal, and the spec says the
portal "boots, loads the fixtures, and prints" the four test logins. So the loader is
part of `docker compose up`; there is nothing for a checker to populate, and
`SEED_ON_START=false` is not the path.

### How it loads

`backend/app/seed_fixtures.py`, run by `entrypoint.sh` right after `app.seed`, when
`SEED_ON_START=true`. It is a second module rather than a section of `app.seed`
because `app.seed` is gated on one marker row: a database that already holds our own
seed would never see a new fixture set. This loader has its own marker (the event slug,
`sample-hack-2026`), so an old volume picks the fixtures up on its next boot, and a
second boot loads nothing twice. `docker-compose.yml` mounts `./fixtures.json`
read-only at `/fixtures/fixtures.json` (`FIXTURES_PATH`), so the portal and `run.py`
read the same bytes. Without the file it says so and skips; with a `FIXTURES_PATH`
that points nowhere it stops, because that is a typo, not an absence.

Our own invented events (`dogfood` and the rest) are untouched: the README tour and
the browser tests are written against them, and they load alongside.

**The close date is the file's own.** `submissions_close` is
loaded as written and is in the past, so the event is closed and "a closed event refuses
submissions" is answered by the real deadline predicate. Every other date on the event
(start, registration, judging window) is derived from it, because the file carries none.

### What the schema could not hold as written

The file is input, not our data model, and three things in it do not fit ours. Each is
adapted rather than dropped, each is printed at load (`docker compose logs api`) and
each is asserted in `tests/test_fixture_loader.py`:

| In the file | Ours | What the loader does |
| --- | --- | --- |
| `prj_07` and `prj_41`: the same team entered twice (same title and repo, a few hours apart), and both carry reviews | one submission per team (`submissions.team_id` is unique) | The later entry (`prj_41`) is the record. Reviews of the earlier entry are kept only for judges who did not also review the later one: 2 carried over, 3 dropped (`jdg_19`, `jdg_21`, `jdg_26` reviewed both, with different scores; the later review stands). 123 ballots load, not 126. |
| Team names `StillTrail` (x3), `AmberSwitch` (x2), `OpenSignal` (x2) | team names are unique per event | The first keeps the name; the others load as `StillTrail (tm_30)` and so on. |
| Nine judges list two tracks | a judge record holds one track or all | Those nine load as all-tracks judges, **wider than the file says**. No ballot in the file falls outside its judge's tracks, so nothing in the loaded data is affected, but a re-assignment would treat them as unrestricted. A real fix is a judge-to-tracks join table; it is not built. |

Two things are deliberately *not* invented: the file has no review timestamps, so
`completed_at` stays NULL (the fast-completion flag ignores NULLs), and it has no
votes, comments or audit history, so there are none. The rubric is not in the file
either: the three criteria are the ones the scores use (`functionality`, `quality`,
`innovation`), at equal weight on a 1-5 range, which an organizer can change.

The judge with a flat record (`jdg_07`, all 4s) and the judges with two or three
reviews are loaded as-is: they are the cases the normalization exists for.

### The four accounts, and where they come from

Every boot prints the acceptance logins in the shape the organizers show:

```
seeded. test logins:
  organizer    Cookie: dogfood_session=org_...
  judge_a      Cookie: dogfood_session=jdg_a_...
  judge_b      Cookie: dogfood_session=jdg_b_...
  participant  Cookie: dogfood_session=prt_...
```

followed by the five routes `.dogfood.toml` needs. The tokens are derived from
`SESSION_SECRET`, so `docker compose down -v && up` reproduces the values committed in
`.dogfood.toml`. The sessions behind them live ten years, because a committed header
that quietly expires is a checker that quietly fails, and they are **replaced on every
boot** so the expiry never runs down and a rotated secret leaves nothing stale.

`judge_a` and `judge_b` are chosen by rule, not by name: the first project in the file
reviewed by two *track* judges of its own track. With the current file that is
`Glass Signal` (`prj_01`), Security track, judges `jdg_08` and `jdg_28`: the two are
both entitled to that project and both hold a ballot on it, so the only thing between
`judge_b` and `judge_a`'s scores is the ownership check. `participant` owns the file's
first team, which owns `Glass Signal`; `organizer` is the base seed's.

**Not in production.** With `ENVIRONMENT=production` no acceptance sessions are
created (`acceptance_sessions_allowed()`). Ten-year organizer sessions with derivable
tokens are a demo convenience, and the dev `SESSION_SECRET` they derive from is public.

### Running the checker

```bash
docker compose up -d
python3 run.py .dogfood.toml > acceptance-report.txt
```

`.dogfood.toml`'s `[portal] base_url` is the API, `http://localhost:8000`: every route
the checker uses is an `/api` route, and the Next.js UI on :3000 does not proxy them.
The `[auth]` and `[routes]` values must match what the last boot printed; they do, on
the dev secret. If you change `SESSION_SECRET`, copy the new block across.

Which route stands for which check, and why (the reasons are also in `.dogfood.toml`):

- **gallery** is `GET /api/gallery?event=sample-hack-2026`: the request the UI's own
  per-event gallery page makes. The unfiltered `/api/gallery` is the *cross-event* view.
  It takes turns between events, newest first within each (`sort=mixed`, the default when
  no `event` is given; `sort=recent` is strictly newest first, and is the default for one
  event), so one event's newer projects do not fill page one. On the running stack (59
  public projects across four events) page one has 7 fixture projects of 24, where plain
  newest-first gave 5. It still fails "project from fixtures shown": the check wants one of
  the file's first three titles, and they are on pages 3, 2 and 2 of the cross-event view.
  That is a property of a multi-event front page, not a pagination bug, and it was left
  alone. (Of those three only `Deep Compass` is on page one of the event gallery; the
  check needs any one.)
- **submit** is `POST /api/submissions/{id}/submit` on the participant's own fixture
  submission, **not** `POST /api/submissions`. The latter needs `team_id` and `name`, so
  the checker's `{"title", "summary"}` body is rejected as malformed (422) before the
  deadline is consulted: a green check that says nothing about the deadline. The former
  takes no body, reaches the deadline predicate, and answers `409 The submission deadline
  has passed`.
- **judge_scores** and **peer_scores** are the same URL, `GET
  /api/judging/assignments/{ballot}`: judge_a's ballot on `Glass Signal`. judge_a gets
  200 and the scores, judge_b (who reviewed the same project) and the participant get
  403. `GET /api/judging/queue` is not used for `judge_scores`: it answers a participant
  `200 []`, which is right for a list and wrong for "a participant is not a judge".
- **csv_export** is `GET /api/events/sample-hack-2026/export/results.csv`.

The checker has checks for T1 and T2 only. T3 and T4 are built and covered by our own
suite, but nothing in `run.py` can verify them, so they are not *claimed* in
`.dogfood.toml` -- see its `[evidence]` table.
