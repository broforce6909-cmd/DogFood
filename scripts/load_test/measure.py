"""Measure real API latency (p50/p95/p99) against the `load-test` event
that `seed_scale.py` creates, plus CPU/memory of the `api`/`db` containers
while a concurrent mix of requests is in flight.

Run after `docker compose up` and `seed_scale.py`, from the host:

    backend/.venv/Scripts/python.exe scripts/load_test/measure.py

Two kinds of measurement, because they answer different questions:

* **Heavy, staff-only, one-at-a-time endpoints** (`/results`, `/results/
  disagreement`, `/results/consistency`, `/audit/verify`, the results CSV
  export): these are the ones that recompute something over every ballot,
  vote or audit row -- exactly what "up from 200/50/1,500" is meant to stress.
  Nobody hits these concurrently in practice (one organizer, occasionally), so
  they are timed sequentially, repeated `--repeat` times, for percentiles that
  mean "how long does an organizer actually wait", not "what happens under
  contention that will never occur".
* **Paginated, frequently-hit endpoints** (submissions/assignments/audit
  lists, the public gallery, the vote tally): these *are* hit concurrently in
  practice (many judges, many visitors), so they are measured under
  `--concurrency` simultaneous workers for `--duration` seconds.

Prints a Markdown table at the end, ready to paste into ARCHITECTURE.md.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass, field

import httpx

BASE = "http://localhost:8000"
EVENT = "load-test"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "dogfood2026"


@dataclass
class Timings:
    name: str
    samples_ms: list[float] = field(default_factory=list)
    errors: int = 0

    def percentile(self, p: float) -> float:
        if not self.samples_ms:
            return float("nan")
        return statistics.quantiles(self.samples_ms, n=100, method="inclusive")[int(p) - 1]

    def summary(self) -> dict:
        return {
            "name": self.name,
            "n": len(self.samples_ms),
            "errors": self.errors,
            "p50": round(self.percentile(50), 1) if self.samples_ms else float("nan"),
            "p95": round(self.percentile(95), 1) if self.samples_ms else float("nan"),
            "p99": round(self.percentile(99), 1) if self.samples_ms else float("nan"),
            "max": round(max(self.samples_ms), 1) if self.samples_ms else float("nan"),
        }


async def _login(client: httpx.AsyncClient) -> str:
    r = await client.post(
        f"{BASE}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    r.raise_for_status()
    return r.json()["token"]


async def _timed_get(client: httpx.AsyncClient, url: str, headers: dict) -> float:
    t0 = time.perf_counter()
    r = await client.get(url, headers=headers)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    return elapsed_ms


async def run_sequential(name: str, client: httpx.AsyncClient, url: str, headers: dict, repeat: int) -> Timings:
    timings = Timings(name=name)
    for _ in range(repeat):
        try:
            timings.samples_ms.append(await _timed_get(client, url, headers))
        except httpx.HTTPStatusError:
            timings.errors += 1
    return timings


async def run_concurrent(
    name: str, client: httpx.AsyncClient, url: str, headers: dict, concurrency: int, duration: float
) -> Timings:
    timings = Timings(name=name)
    lock = asyncio.Lock()
    stop_at = time.monotonic() + duration

    async def worker() -> None:
        while time.monotonic() < stop_at:
            try:
                elapsed_ms = await _timed_get(client, url, headers)
                async with lock:
                    timings.samples_ms.append(elapsed_ms)
            except httpx.HTTPStatusError:
                async with lock:
                    timings.errors += 1

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return timings


class DockerStatsSampler:
    """Polls `docker stats --no-stream` in a background thread while a
    concurrent phase runs, so CPU/memory numbers reflect load, not idle."""

    def __init__(self, containers: list[str], interval: float = 1.0) -> None:
        self.containers = containers
        self.interval = interval
        self._stop = threading.Event()
        self._samples: dict[str, list[tuple[float, float]]] = {c: [] for c in containers}
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    [
                        "docker", "stats", "--no-stream", "--format",
                        "{{.Name}},{{.CPUPerc}},{{.MemUsage}}",
                    ],
                    capture_output=True, text=True, timeout=5,
                )
                for line in out.stdout.strip().splitlines():
                    parts = line.split(",")
                    if len(parts) < 3:
                        continue
                    name, cpu, mem = parts[0], parts[1], parts[2]
                    for c in self.containers:
                        if c in name:
                            cpu_pct = float(cpu.strip().rstrip("%"))
                            mem_mb = _parse_mem_mb(mem.split("/")[0].strip())
                            self._samples[c].append((cpu_pct, mem_mb))
            except Exception:
                pass
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread.start()

    def stop_and_report(self) -> dict[str, dict]:
        self._stop.set()
        self._thread.join(timeout=5)
        report = {}
        for c, samples in self._samples.items():
            if not samples:
                report[c] = {"cpu_avg": float("nan"), "cpu_max": float("nan"), "mem_avg_mb": float("nan"), "mem_max_mb": float("nan")}
                continue
            cpus = [s[0] for s in samples]
            mems = [s[1] for s in samples]
            report[c] = {
                "cpu_avg": round(sum(cpus) / len(cpus), 1),
                "cpu_max": round(max(cpus), 1),
                "mem_avg_mb": round(sum(mems) / len(mems), 1),
                "mem_max_mb": round(max(mems), 1),
            }
        return report


def _parse_mem_mb(text: str) -> float:
    text = text.strip()
    if text.endswith("GiB"):
        return float(text[:-3]) * 1024
    if text.endswith("MiB"):
        return float(text[:-3])
    if text.endswith("KiB"):
        return float(text[:-3]) / 1024
    return 0.0


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=10, help="samples for sequential (heavy) endpoints")
    parser.add_argument("--concurrency", type=int, default=40, help="concurrent workers for list endpoints")
    parser.add_argument("--duration", type=float, default=15.0, help="seconds per concurrent-phase endpoint")
    args = parser.parse_args()

    async with httpx.AsyncClient(timeout=60.0) as client:
        token = await _login(client)
        headers = {"authorization": f"Bearer {token}"}

        heavy = [
            ("GET /results (gather, full computed table)", f"{BASE}/api/events/{EVENT}/results"),
            ("GET /results/disagreement", f"{BASE}/api/events/{EVENT}/results/disagreement"),
            ("GET /results/consistency", f"{BASE}/api/events/{EVENT}/results/consistency"),
            ("GET /audit/verify (global, whole chain)", f"{BASE}/api/audit/verify"),
            ("GET /export/results.csv", f"{BASE}/api/events/{EVENT}/export/results.csv"),
        ]

        concurrent_endpoints = [
            ("GET /submissions (paginated, per_page=50)", f"{BASE}/api/events/{EVENT}/submissions?per_page=50", headers),
            ("GET /assignments (paginated, per_page=50)", f"{BASE}/api/events/{EVENT}/assignments?per_page=50", headers),
            ("GET /audit (per-event, paginated)", f"{BASE}/api/events/{EVENT}/audit?per_page=50", headers),
            ("GET /voting/results (tally, GROUP BY)", f"{BASE}/api/events/{EVENT}/voting/results", headers),
            ("GET /gallery (public, filtered)", f"{BASE}/api/gallery?event={EVENT}&per_page=24", {}),
        ]

        print(f"=== Sequential timings ({args.repeat} calls each), no concurrent load ===")
        results = []
        for name, url in heavy:
            t = await run_sequential(name, client, url, headers, args.repeat)
            results.append(t.summary())
            print(f"  {name}: {t.summary()}")

        print()
        print(f"=== Concurrent load ({args.concurrency} workers x {args.duration}s each) ===")
        sampler = DockerStatsSampler(["dogfood-api-1", "dogfood-db-1"])
        sampler.start()
        for name, url, hdrs in concurrent_endpoints:
            t = await run_concurrent(name, client, url, hdrs, args.concurrency, args.duration)
            results.append(t.summary())
            print(f"  {name}: {t.summary()}")
        resource_report = sampler.stop_and_report()

        print()
        print("=== Container resource usage during the concurrent phase ===")
        for container, stats in resource_report.items():
            print(f"  {container}: {stats}")

        print()
        print("=== Markdown table ===")
        print("| Endpoint | n | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) | errors |")
        print("|---|---|---|---|---|---|---|")
        for r in results:
            print(
                f"| {r['name']} | {r['n']} | {r['p50']} | {r['p95']} | {r['p99']} | {r['max']} | {r['errors']} |"
            )


if __name__ == "__main__":
    asyncio.run(main())
