"""Run real pairwise judging against a seeded event and print the fitted ranking.

    docker compose exec api python -m scripts.pairwise_proof
    # or, against a stack already up, from the host:
    python scripts/pairwise_proof.py --base http://localhost:8000

Unlike `normalization_proof.py`, which reads scores that already exist in the
fixture, there is no pre-seeded set of pairwise comparisons to read -- Gavel-style
judging did not exist when the fixtures were written. So this script *plays* a
short, realistic session: it logs in as two seeded judges and calls the exact
same `GET .../pairwise/next` / `POST .../pairwise/compare` endpoints a real judge's
browser would, the same number of rounds each, then prints what
`GET .../pairwise/results` fits from what they produced.

That is deliberate, not a shortcut. It means this script exercises
`app.pairwise.pick_next_pair`'s real coverage behaviour (least-compared projects
first, no immediate repeats for one judge) rather than a hand-picked sequence of
comparisons chosen to make a tidy example -- if the pairing logic were wrong, this
script's output would be wrong with it.

Mutates the database it points at (it is, after all, playing real ballots) --
meant to be run once against a freshly-seeded stack, same as the normalization
proof. Running it twice adds a second round of comparisons on top of the first
rather than failing, since a judge comparing an already-seen pair again is a
real, allowed thing to happen (see `app.pairwise`'s module docstring, point 2),
just not the *tidiest* demonstration of cold start.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_EVENT = "raptors-winter"
DEFAULT_ORGANIZER = ("organizer@example.com", "dogfood2026")
JUDGES = [
    ("judge.rivera@example.com", "dogfood2026"),
    ("judge.whitfield@example.com", "dogfood2026"),
]
ROUNDS_PER_JUDGE = 4


def call(
    base: str, path: str, *, method: str = "GET", token: str | None = None, body: dict | None = None
) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    request.add_header("content-type", "application/json")
    if token:
        request.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:  # pragma: no cover - operator feedback
        sys.exit(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode()[:300]}")
    except urllib.error.URLError as exc:  # pragma: no cover - operator feedback
        sys.exit(f"cannot reach {base}: {exc.reason}\nIs the stack up? docker compose up -d")


def login(base: str, email: str, password: str) -> str:
    return call(base, "/api/auth/login", method="POST", body={"email": email, "password": password})[
        "token"
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--event", default=DEFAULT_EVENT, help="Event slug")
    parser.add_argument(
        "--rounds", type=int, default=ROUNDS_PER_JUDGE, help="Comparisons per judge"
    )
    args = parser.parse_args()

    organizer_token = login(args.base, *DEFAULT_ORGANIZER)
    event = call(args.base, f"/api/events/{args.event}", token=organizer_token)
    if not event["pairwise_enabled"]:
        call(
            args.base,
            f"/api/events/{args.event}",
            method="PATCH",
            token=organizer_token,
            body={"pairwise_enabled": True},
        )
        print(f"(enabled pairwise judging on {args.event} -- it was off)")

    print(f"Event: {args.event}")
    print(f"Playing {args.rounds} comparison(s) each for {len(JUDGES)} judge(s)...")
    print()

    for email, password in JUDGES:
        token = login(args.base, email, password)
        for round_number in range(1, args.rounds + 1):
            pair = call(
                args.base, f"/api/events/{args.event}/pairwise/next", token=token
            )
            a, b = pair["submission_a"], pair["submission_b"]
            # A deterministic, not-quite-arbitrary "judge": prefer the
            # alphabetically-earlier name in the first three rounds so a
            # consistent signal accumulates for *something*, then answer "tie"
            # on the fourth so the proof demonstrates that path too, in the
            # same session rather than a separate hand-written example.
            if round_number == args.rounds:
                winner_id = None
                verdict = "tie"
            else:
                winner_id = a["id"] if a["name"] < b["name"] else b["id"]
                verdict = (a if winner_id == a["id"] else b)["name"]
            call(
                args.base,
                f"/api/events/{args.event}/pairwise/compare",
                method="POST",
                token=token,
                body={
                    "submission_a_id": a["id"],
                    "submission_b_id": b["id"],
                    "winner_id": winner_id,
                },
            )
            print(f"  {email}: {a['name']!r} vs {b['name']!r} -> {verdict}")

    print()
    results = call(
        args.base, f"/api/events/{args.event}/pairwise/results", token=organizer_token
    )
    print(f"Method: {results['method']}")
    print(f"Total comparisons: {results['total_comparisons']}  Converged: {results['converged']}")
    print()
    print("PAIRWISE RANKING")
    print(f"  {'rank':>4} {'project':<13} {'track':<11} {'n':>2} {'record':>8} {'rating':>8}")
    for row in results["rows"]:
        record_str = f"{row['wins']:g}-{row['losses']:g}" if row["n_comparisons"] else "-"
        print(
            f"  {row['rank']:>4} {row['submission_name']:<13} "
            f"{str(row['track'] or '-'):<11} {row['n_comparisons']:>2} "
            f"{record_str:>8} {row['rating']:>8.3f}"
        )

    uncompared = [r for r in results["rows"] if r["n_comparisons"] == 0]
    if uncompared:
        print()
        print("COLD START: never compared, ranked at the anchor rather than omitted")
        for row in uncompared:
            print(f"  {row['submission_name']} (rating {row['rating']:.3f})")

    print()
    print("JUDGE PROGRESS")
    for j in results["judges"]:
        print(f"  {j['judge_name']}: {j['n_comparisons']} comparison(s)")

    cov = results["coverage"]
    print()
    print(
        f"COVERAGE: {cov['total_submissions']} projects, {cov['total_comparisons']} comparisons, "
        f"{cov['coverage_pct']:.1f}% compared at least once "
        f"(min {cov['min_comparisons']}, max {cov['max_comparisons']}, "
        f"mean {cov['mean_comparisons']:.1f})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
