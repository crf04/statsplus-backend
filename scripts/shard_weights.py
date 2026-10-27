#!/usr/bin/env python3
"""Rebuild tests/shard_weights.json from the logs of Tests workflow runs.

Each test is charged the gap between its xdist worker's previous result and
its own (setup, call and teardown included). A job's times are divided by its
runner CPU's speed relative to the fastest hosted CPU seen, so runs on
different CPUs average into one scale. Files with no recorded time weigh one
second in tests/conftest.py.

A run must be complete: every shard job of both suites present and successful,
and every collected test file observed. Otherwise nothing is written, because
a partial sample would reset the files it missed to the one-second fallback.

    python scripts/shard_weights.py <run-id>[:<attempt>] ...

Needs an authenticated ``gh``. Run from the repository root.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = "crf04/statsplus-backend"
WEIGHTS = Path("tests/shard_weights.json")
# Seconds per unit of work relative to the fastest CPU, measured on the
# Tests workflow in October 2026 (crf04/statsplus-backend#613).
CPU_FACTORS = {
    "AMD EPYC 9V45": 1.0,
    "Intel(R) Xeon(R) 6973P-C": 1.0,
    "AMD EPYC 9V74": 1.28,
    "Intel(R) Xeon(R) Platinum 8370C": 1.5,
    "INTEL(R) XEON(R) PLATINUM 8573C": 1.55,
    "AMD EPYC 7763": 1.8,
}
SUITES = {"tests": "unit", "integration": "integration"}
SHARD_JOB = re.compile(r"(tests|integration) \((\d+)/(\d+)\)")
LINE = re.compile(r"^﻿?(\S+Z) (.*)$")
RESULT = re.compile(r"^\[(gw\d+)\] \[\s*\d+%\] \w+ (tests/\S+?)::")


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def file_seconds(log: str) -> dict[str, float]:
    cpu = re.search(r"model name\s*:\s*(.*)", log)
    factor = next((v for k, v in CPU_FACTORS.items() if cpu and cpu.group(1).startswith(k)), 1.0)
    seconds: dict[str, float] = defaultdict(float)
    last: dict[str, datetime] = {}
    first = None
    for line in log.splitlines():
        match = LINE.match(line)
        if not match:
            continue
        stamp = datetime.fromisoformat(match.group(1)[:26])
        body = match.group(2)
        if first is None and body.startswith("tests/") and "::" in body:
            first = stamp
        result = RESULT.match(body)
        if result:
            worker, path = result.groups()
            seconds[path] += (stamp - last.get(worker, first)).total_seconds() / factor
            last[worker] = stamp
    return seconds


def collected_files(suite: str) -> set[str]:
    """The test files a suite's shards run: Postgres tests under tests/integration, live tests never."""
    files = {path.as_posix() for path in Path("tests").rglob("test_*.py")}
    integration = {name for name in files if name.startswith("tests/integration/")}
    live = {name for name in files if name.startswith("tests/live/")}
    return integration if suite == "integration" else files - integration - live


def shard_job_problems(jobs: list[dict]) -> list[str]:
    """Name every shard job that is missing or did not succeed."""
    shards: dict[str, dict[int, str]] = {suite: {} for suite in SUITES.values()}
    counts: dict[str, set[int]] = {suite: set() for suite in SUITES.values()}
    for job in jobs:
        match = SHARD_JOB.fullmatch(job["name"])
        if match:
            suite = SUITES[match.group(1)]
            shards[suite][int(match.group(2))] = job.get("conclusion") or job.get("status") or "unknown"
            counts[suite].add(int(match.group(3)))
    problems = []
    for suite, found in shards.items():
        if len(counts[suite]) != 1:
            problems.append(f"{suite}: expected one shard count, found {sorted(counts[suite]) or 'no shard jobs'}")
            continue
        for index in range(1, counts[suite].pop() + 1):
            result = found.get(index, "missing")
            if result != "success":
                problems.append(f"{suite} shard {index}: {result}")
    return problems


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    samples: dict[str, dict[str, list[float]]] = {"unit": defaultdict(list), "integration": defaultdict(list)}
    problems = []
    for arg in sys.argv[1:]:
        run, _, attempt = arg.partition(":")
        jobs = json.loads(
            gh("api", f"repos/{REPO}/actions/runs/{run}/attempts/{attempt or 1}/jobs?per_page=100")
        )
        run_problems = shard_job_problems(jobs["jobs"])
        observed: dict[str, set[str]] = {suite: set() for suite in SUITES.values()}
        if not run_problems:
            for job in jobs["jobs"]:
                match = SHARD_JOB.fullmatch(job["name"])
                if match:
                    suite = SUITES[match.group(1)]
                    log = gh("api", "--allow-escape-sequences", f"repos/{REPO}/actions/jobs/{job['id']}/logs")
                    for path, seconds in file_seconds(log).items():
                        samples[suite][path].append(seconds)
                        observed[suite].add(path)
            for suite, seen in observed.items():
                unseen = sorted(collected_files(suite) - seen)
                if unseen:
                    run_problems.append(f"{suite}: no time recorded for {', '.join(unseen)}")
        problems += [f"run {arg}: {problem}" for problem in run_problems]
    if problems:
        sys.exit("refusing to write " + str(WEIGHTS) + ", the sample is incomplete:\n  " + "\n  ".join(problems))
    weights = json.loads(WEIGHTS.read_text())
    for suite, files in samples.items():
        weights[suite] = {path: round(sum(values) / len(values), 1) for path, values in sorted(files.items())}
    WEIGHTS.write_text(json.dumps(weights, indent=1, sort_keys=True) + "\n")
    print({suite: len(files) for suite, files in weights.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
