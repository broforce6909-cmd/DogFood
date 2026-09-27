"""Print the normalization proof for a seeded event.

The brief asks for the raw scores, the normalized scores and the ranking change,
"reproducible by anyone who runs `docker compose up`". This is that command:

    docker compose exec api python -m scripts.normalization_proof
    # or, against a stack already up, from the host:
    python scripts/normalization_proof.py --base http://localhost:8000

It reads the API as an organizer rather than the database directly, which means
what it prints is exactly what the product serves -- if the endpoint were wrong,
this would be wrong with it, rather than quietly agreeing with a second
implementation of the same maths.

The output of this script is pasted into JUDGING.md, so the numbers in that
document are generated rather than asserted.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_EVENT = "raptors-winter"
DEFAULT_EMAIL = "organizer@example.com"
DEFAULT_PASSWORD = "dogfood2026"


def call(base: str, path: str, *, token: str | None = None, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base.rstrip("/") + path, data=data)
    request.add_header("content-type", "application/json")
    if token:
        request.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:  # pragma: no cover - operator feedback
        sys.exit(f"{path} -> HTTP {exc.code}: {exc.read().decode()[:300]}")
    except urllib.error.URLError as exc:  # pragma: no cover - operator feedback
        sys.exit(f"cannot reach {base}: {exc.reason}\nIs the stack up? docker compose up -d")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--event", default=DEFAULT_EVENT, help="Event slug")
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    args = parser.parse_args()

    session = call(
        args.base, "/api/auth/login", body={"email": args.email, "password": args.password}
    )
    results = call(args.base, f"/api/events/{args.event}/results", token=session["token"])

    rows = results["rows"]
    cals = results["calibrations"]

    print(f"Event:  {results['event_slug']}")
    print(f"Method: {results['method']}")
    print(
        f"Global mean {results['global_mean']:.4f}, "
        f"global sd {results['global_sd']:.4f}, shrinkage k = {results['shrinkage_k']}"
    )

    print()
    print("HOW EACH JUDGE USED THE SCALE")
    print(
        f"  {'judge':<16} {'n':>2} {'mean':>6} {'sd':>6}   "
        f"{'shrunk mean':>11} {'shrunk sd':>9}   note"
    )
    for cal in sorted(cals, key=lambda c: -c["mean"]):
        print(
            f"  {cal['judge_name']:<16} {cal['n']:>2} {cal['mean']:>6.3f} {cal['sd']:>6.3f}   "
            f"{cal['shrunk_mean']:>11.3f} {cal['shrunk_sd']:>9.3f}   {cal['note']}"
        )

    print()
    print("RAW vs NORMALIZED")
    print(
        f"  {'rank':>4} {'was':>4} {'move':>5}   {'project':<13} {'track':<11} "
        f"{'n':>2} {'raw':>7} {'normalized':>11}"
    )
    for row in rows:
        print(
            f"  {row['normalized_rank']:>4} {row['raw_rank']:>4} {row['rank_delta']:>+5}   "
            f"{row['submission_name']:<13} {str(row['track'] or '-'):<11} "
            f"{row['n_reviews']:>2} {row['raw_mean']:>7.3f} {row['normalized_mean']:>11.3f}"
        )

    moved = [r for r in rows if r["rank_delta"] != 0]
    print()
    print(f"RANK MOVEMENT: {len(moved)} of {len(rows)} projects changed position.")
    for row in sorted(moved, key=lambda r: -abs(r["rank_delta"])):
        direction = "up" if row["rank_delta"] > 0 else "down"
        print(
            f"  {row['submission_name']:<13} {direction:>4} "
            f"{abs(row['rank_delta'])}: raw #{row['raw_rank']} -> #{row['normalized_rank']}"
        )

    flat = [c for c in cals if c["flat"]]
    if flat:
        print()
        print("THE JUDGE WHO MARKS EVERYTHING THE SAME")
        for cal in flat:
            print(
                f"  {cal['judge_name']} scored {cal['n']} projects, sd {cal['sd']:.3f}. "
                f"Every one of their ballots is mapped to the global mean "
                f"({results['global_mean']:.3f}), so they move no project relative to another."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
