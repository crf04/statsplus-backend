#!/usr/bin/env python3
"""Check that the CI test shards partition the full collection exactly.

Collects the whole suite once and each STATSPLUS_TEST_SHARD slice, then fails
unless every collected node id lands in exactly one shard. Run from the
repository root: ``python scripts/verify_test_shards.py [shard-count] [unit|integration]``;
the second argument picks the suite and its recorded shard weights.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter

MARKERS = {"unit": "not live and not integration", "integration": "integration"}


def collect(shard: str | None, suite: str) -> list[str]:
    env = {k: v for k, v in os.environ.items() if k != "STATSPLUS_TEST_SHARD"}
    env["STATSPLUS_TEST_SHARD_WEIGHTS"] = suite
    if shard:
        env["STATSPLUS_TEST_SHARD"] = shard
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            # pytest.ini's addopts carries -v, which would hide the node id list;
            # restate its marker filter so the same tests are collected.
            "-o",
            "addopts=",
            "-m",
            MARKERS[suite],
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout[-3000:] + result.stderr[-3000:])
        raise SystemExit(f"collection failed for shard {shard or 'full'}")
    return [line for line in result.stdout.splitlines() if "::" in line]


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    suite = sys.argv[2] if len(sys.argv) > 2 else "unit"
    full = collect(None, suite)
    shards = {i: collect(f"{i}/{count}", suite) for i in range(1, count + 1)}
    seen = Counter(nodeid for ids in shards.values() for nodeid in ids)
    missing = sorted(set(full) - set(seen))
    extra = sorted(set(seen) - set(full))
    duplicated = sorted(nodeid for nodeid, n in seen.items() if n > 1)
    sizes = {i: len(ids) for i, ids in shards.items()}
    print(f"full={len(full)} shards={sizes} union={sum(sizes.values())}")
    if missing or extra or duplicated or len(set(full)) != len(full):
        print(f"missing={missing[:10]} extra={extra[:10]} duplicated={duplicated[:10]}")
        return 1
    if not all(sizes.values()):
        print("an empty shard means the split is broken")
        return 1
    print("shards partition the full collection exactly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
