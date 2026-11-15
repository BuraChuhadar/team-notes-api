"""Run a paced HTTP workload in a unique disposable, two-replica Docker stack.

No developer database is selected. Results describe this laptop run only.
Run from the repository: python3 -m scripts.load_test --duration 15 --rate 50.
"""

import argparse
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import threading
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    return sorted(values)[max(0, math.ceil(len(values) * percent) - 1)]


@dataclass(frozen=True)
class PacedResult:
    records: list[dict[str, Any]]
    elapsed_seconds: float
    planned_requests: int
    offered_requests: int
    capacity_dropped_requests: int
    unoffered_requests: int

    @property
    def dropped_requests(self) -> int:
        return self.capacity_dropped_requests + self.unoffered_requests


def run_paced_workload(
    request: Callable[[int, float], dict[str, Any]],
    *,
    duration: float,
    rate: float,
    concurrency: int,
    clock: Callable[[], float] = monotonic,
    pause: Callable[[float], None] = sleep,
) -> PacedResult:
    """Account for every arrival slot, including slots missed by the generator.

    Offered requests are submitted to the bounded client executor and drained
    before reporting. Capacity drops and arrivals missed after the deadline are
    distinct, and both contribute to the total dropped count.
    """
    planned = math.ceil(duration * rate)
    # Exclude a boundary slot if floating-point multiplication rounded upward.
    while planned and (planned - 1) / rate >= duration:
        planned -= 1
    records: list[dict[str, Any]] = []
    pending: set[Future[dict[str, Any]]] = set()
    offered = 0
    capacity_dropped = 0
    sequence = 0
    started = clock()
    deadline = started + duration
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        while sequence < planned:
            now = clock()
            if now >= deadline:
                break
            scheduled = started + sequence / rate
            if scheduled > now:
                pause(scheduled - now)
                if clock() >= deadline:
                    break
            done = {future for future in pending if future.done()}
            for future in done:
                records.append(future.result())
            pending -= done
            if len(pending) >= concurrency * 2:
                capacity_dropped += 1
            else:
                pending.add(executor.submit(request, sequence, scheduled))
                offered += 1
            sequence += 1
        for future in pending:
            records.append(future.result())
    return PacedResult(
        records=records,
        elapsed_seconds=clock() - started,
        planned_requests=planned,
        offered_requests=offered,
        capacity_dropped_requests=capacity_dropped,
        unoffered_requests=planned - sequence,
    )


def execute(
    command: list[str], *, environment: dict[str, str], capture: bool = True
) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if result.returncode:
        # Compose output has no credential-bearing CLI arguments in this harness.
        raise RuntimeError(
            "Command failed: "
            + " ".join(command[:5])
            + "\n"
            + (result.stderr or "")[-1500:]
        )
    return result.stdout or ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=15)
    parser.add_argument(
        "--rate",
        type=float,
        default=50,
        help="Target requests per second (paced arrivals).",
    )
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--users", type=int, default=8)
    parser.add_argument("--notes", type=int, default=10000)
    parser.add_argument(
        "--output", type=Path, default=Path("work/load-test-report.json")
    )
    parser.add_argument("--keep-stack", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    if (
        not 1 <= args.duration <= 300
        or not 1 <= args.rate <= 1000
        or not 1 <= args.concurrency <= 128
    ):
        parser.error("duration1..300, rate1..1000 and concurrency1..128 are required.")
    if not 2 <= args.users <= 100 or not args.users <= args.notes <= 100000:
        parser.error("users must be 2..100 and notes must be users..100000.")
    project = "notes-bench-" + uuid4().hex[:10]
    environment = {**os.environ, "API_PORT": "0", "POSTGRES_PORT": "0"}
    compose = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        "compose.yaml",
        "-f",
        "compose.scale.yaml",
        "-f",
        "deploy/benchmark.override.yaml",
    ]

    def call(tail: list[str], capture: bool = True) -> str:
        return execute(compose + tail, environment=environment, capture=capture)

    samples = []
    sample_errors = []
    stop = threading.Event()
    state_lock = threading.Lock()
    try:
        print(f"Starting disposable project {project}", flush=True)
        call(
            ["up", "-d", "--wait", "--wait-timeout", "180"]
            + ([] if args.skip_build else ["--build"]),
            capture=False,
        )
        port = int(call(["port", "gateway", "8080"]).strip().rsplit(":", 1)[1])
        base = f"http://127.0.0.1:{port}"
        fixture = json.loads(
            call(
                [
                    "exec",
                    "-T",
                    "--index",
                    "1",
                    "api",
                    "python",
                    "-m",
                    "scripts.benchmark_data",
                    "seed",
                    "--users",
                    str(args.users),
                    "--notes",
                    str(args.notes),
                ]
            )
        )
        accounts = fixture["accounts"]
        print(
            f"Seeded {args.notes} notes and {args.users} users; measuring {args.duration:g}s at {args.rate:g} requests/s.",
            flush=True,
        )

        def sampler() -> None:
            while not stop.is_set():
                try:
                    value = json.loads(
                        call(
                            [
                                "exec",
                                "-T",
                                "--index",
                                "1",
                                "api",
                                "python",
                                "-m",
                                "scripts.benchmark_data",
                                "sample",
                            ]
                        )
                    )
                    samples.append(value)
                except (RuntimeError, ValueError) as error:
                    sample_errors.append(type(error).__name__)
                stop.wait(1)

        def request(sequence: int, scheduled: float) -> dict[str, Any]:
            account = accounts[(sequence + sequence // 20) % len(accounts)]
            kind = sequence % 20
            method = "GET"
            headers = {"Authorization": "Bearer " + account["token"]}
            body = None
            if kind == 19:
                operation = "login"
                method = "POST"
                path = "/auth/login"
                body = {"email": account["email"], "password": fixture["password"]}
            elif kind in {17, 18}:
                operation = "update"
                method = "PUT"
                path = f"/notes/{account['note_id']}"
                with state_lock:
                    version = account["version"]
                body = {
                    "title": account["title"],
                    "content": f"Load test update {sequence}",
                    "version": version,
                }
            elif kind in {13, 14, 15, 16}:
                operation = "get_note"
                path = f"/notes/{account['note_id']}"
            else:
                operation = "search" if kind % 4 == 0 else "list"
                query = {"limit": "25"}
                if operation == "search":
                    query["q"] = "rarebenchmarkneedle"
                path = "/notes?" + urlencode(query)
            data = json.dumps(body).encode() if body is not None else None
            if data is not None:
                headers["Content-Type"] = "application/json"
            started = monotonic()
            try:
                try:
                    response = urlopen(
                        Request(base + path, data=data, headers=headers, method=method),
                        timeout=12,
                    )
                except HTTPError as error:
                    response = error
                with response:
                    status = int(response.status or 0)
                    payload = response.read()
                if status == 200 and operation in {"get_note", "update"}:
                    value = json.loads(payload)
                    with state_lock:
                        account["version"] = max(account["version"], value["version"])
                error = (
                    status == 0
                    or status >= 500
                    or (status >= 400 and status not in {409, 429})
                )
            except (OSError, URLError, ValueError):
                status = 0
                error = True
            completed = monotonic()
            return {
                "operation": operation,
                "status": status,
                "error": error,
                "latency_ms": (completed - started) * 1000,
                "end_to_end_ms": (completed - scheduled) * 1000,
                "scheduling_lag_ms": (started - scheduled) * 1000,
            }

        monitor = threading.Thread(target=sampler, daemon=True)
        monitor.start()
        paced = run_paced_workload(
            request,
            duration=args.duration,
            rate=args.rate,
            concurrency=args.concurrency,
        )
        records = paced.records
        elapsed = paced.elapsed_seconds
        stop.set()
        monitor.join(timeout=15)
        statuses = {
            str(status): sum(row["status"] == status for row in records)
            for status in sorted({row["status"] for row in records})
        }
        latencies = [row["latency_ms"] for row in records]
        report = {
            "environment": "Disposable local Docker: two single-worker API replicas, NGINX, PostgreSQL; not a cloud capacity guarantee.",
            "project": project,
            "dataset": {"users": args.users, "notes": args.notes},
            "workload": "65% list/search,20% direct reads,10% versioned writes,5% login",
            "target_requests_per_second": args.rate,
            "concurrency": args.concurrency,
            "requested_duration_seconds": args.duration,
            "elapsed_seconds": round(elapsed, 3),
            "planned_requests": paced.planned_requests,
            "offered_requests": paced.offered_requests,
            "completed_requests": len(records),
            "achieved_requests_per_second": round(len(records) / elapsed, 2),
            "dropped_scheduled_requests": paced.dropped_requests,
            "capacity_dropped_requests": paced.capacity_dropped_requests,
            "unoffered_requests": paced.unoffered_requests,
            "arrival_accounting": (
                "Offered requests are submitted to the bounded client executor and "
                "all are drained before reporting. Capacity drops and arrival slots "
                "missed after the generator deadline are both included in dropped "
                "scheduled requests; completed plus dropped equals planned."
            ),
            "error_requests": sum(row["error"] for row in records),
            "successful_requests": sum(200 <= row["status"] < 300 for row in records),
            "successful_requests_per_second": round(
                sum(200 <= row["status"] < 300 for row in records) / elapsed, 2
            ),
            "version_conflicts": statuses.get("409", 0),
            "throttled": statuses.get("429", 0),
            "statuses": statuses,
            "latency_ms": {
                "p50": round(percentile(latencies, 0.5), 2),
                "p95": round(percentile(latencies, 0.95), 2),
                "p99": round(percentile(latencies, 0.99), 2),
                "max": round(max(latencies, default=0), 2),
            },
            "end_to_end_p95_ms": round(
                percentile([row["end_to_end_ms"] for row in records], 0.95), 2
            ),
            "max_scheduling_lag_ms": round(
                max((row["scheduling_lag_ms"] for row in records), default=0), 2
            ),
            "database": {
                "samples": len(samples),
                "minimum_sample_interval_seconds": 1,
                "note": "Observed sampled maxima include the sampling connection. Docker command time adds to the minimum interval; brief spikes may be missed.",
                "sample_errors": len(sample_errors),
                "max_connections": max(
                    (row["connections"] for row in samples), default=None
                ),
                "max_lock_waiting_sessions": max(
                    (row["lock_waiting_sessions"] for row in samples), default=None
                ),
                "max_ungranted_locks": max(
                    (row["ungranted_database_locks"] for row in samples), default=None
                ),
            },
            "outcome": "measured; choose acceptance budgets before a production capacity claim",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
        return 1 if report["error_requests"] else 0
    finally:
        stop.set()
        if not args.keep_stack:
            call(["down", "--volumes", "--remove-orphans"], capture=False)
        else:
            print("Kept isolated project for diagnostics: " + project, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
