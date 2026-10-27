#!/usr/bin/env python3
"""Rebuild tests/shard_weights.json from the logs of Tests workflow runs.

Each test is charged the gap between its xdist worker's previous result and
its own (setup, call and teardown included). A job's times are divided by its
runner CPU's speed relative to the fastest hosted CPU seen, so runs on
different CPUs average into one scale. A suite with no shard jobs in the
given runs keeps its current weights; files with no recorded time weigh one
second in tests/conftest.py.

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


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    samples: dict[str, dict[str, list[float]]] = {"unit": defaultdict(list), "integration": defaultdict(list)}
    for arg in sys.argv[1:]:
        run, _, attempt = arg.partition(":")
        jobs = json.loads(
            gh("api", f"repos/{REPO}/actions/runs/{run}/attempts/{attempt or 1}/jobs?per_page=100")
        )
        for job in jobs["jobs"]:
            suite = SUITES.get(job["name"].partition(" (")[0])
            if suite:
                log = gh("api", "--allow-escape-sequences", f"repos/{REPO}/actions/jobs/{job['id']}/logs")
                for path, seconds in file_seconds(log).items():
                    samples[suite][path].append(seconds)
    weights = json.loads(WEIGHTS.read_text())
    for suite, files in samples.items():
        if files:
            weights[suite] = {
                path: round(sum(values) / len(values), 1) for path, values in sorted(files.items())
            }
    WEIGHTS.write_text(json.dumps(weights, indent=1, sort_keys=True) + "\n")
    print({suite: len(files) for suite, files in weights.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
