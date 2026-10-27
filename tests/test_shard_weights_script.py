"""scripts/shard_weights.py rewrites the shard weights only from a complete, successful run."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import shard_weights

COMMITTED = json.loads((Path(__file__).parent / "shard_weights.json").read_text())
BASE = datetime(2026, 10, 26, tzinfo=timezone.utc)
SHARDS = {"unit": ("tests", 12), "integration": ("integration", 2)}


def _log(weights):
    """A shard log in which one xdist worker spends ``weights[file]`` seconds on each file."""
    stamp = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"
    lines = [f"{stamp(BASE)} model name\t: AMD EPYC 9V45", f"{stamp(BASE)} tests/first.py::collected"]
    moment = BASE
    for name, seconds in weights.items():
        moment += timedelta(seconds=seconds)
        lines.append(f"{stamp(moment)} [gw0] [ 50%] PASSED {name}::test")
    return "\n".join(lines)


class _Actions:
    """Stands in for ``gh api``: the job list of each run attempt and the log of each job."""

    def __init__(self, attempts):
        self.attempts = attempts

    def __call__(self, *args):
        endpoint = args[-1]
        if endpoint.endswith("/logs"):
            return self.attempts["logs"][int(endpoint.split("/")[-2])]
        run = endpoint.split("/runs/")[1].split("/")[0]
        attempt = endpoint.split("/attempts/")[1].split("/")[0]
        return json.dumps({"jobs": self.attempts["jobs"][f"{run}:{attempt}"]})


def _complete_run(weights, conclusions=None):
    """Jobs and logs for a full run: twelve unit shards and two integration shards."""
    jobs, logs, job_id = [], {}, 0
    for suite, (prefix, count) in SHARDS.items():
        files = list(weights[suite].items())
        for index in range(1, count + 1):
            job_id += 1
            name = f"{prefix} ({index}/{count})"
            conclusion = (conclusions or {}).get(name, "success")
            jobs.append({"id": job_id, "name": name, "status": "completed", "conclusion": conclusion})
            logs[job_id] = _log(dict(files[index - 1 :: count]))
    jobs.append({"id": 99, "name": "integration", "status": "completed", "conclusion": "success"})
    logs[99] = ""
    return jobs, logs


@pytest.fixture
def rebuild(tmp_path, monkeypatch):
    """Run the script against fake attempts in a copy of the repository's tests tree and weights."""
    tree = tmp_path / "repo"
    for name in [*COMMITTED["unit"], *COMMITTED["integration"], "tests/live/test_odds_api_live.py"]:
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).touch()
    monkeypatch.chdir(tree)
    weights_file = tmp_path / "shard_weights.json"
    weights_file.write_text(json.dumps(COMMITTED, indent=1, sort_keys=True) + "\n")
    monkeypatch.setattr(shard_weights, "WEIGHTS", weights_file)

    def run(jobs_by_attempt, logs, *arguments):
        monkeypatch.setattr(shard_weights, "gh", _Actions({"jobs": jobs_by_attempt, "logs": logs}))
        monkeypatch.setattr(sys, "argv", ["shard_weights.py", *arguments])
        return shard_weights.main(), weights_file

    return run


def test_a_complete_run_reproduces_the_committed_weights(rebuild):
    jobs, logs = _complete_run(COMMITTED)

    code, weights_file = rebuild({"1:1": jobs}, logs, "1")

    assert code == 0
    assert json.loads(weights_file.read_text()) == COMMITTED


def test_a_cancelled_attempt_writes_nothing(rebuild, tmp_path):
    jobs, logs = _complete_run(COMMITTED, {"tests (7/12)": "cancelled", "integration (2/2)": "skipped"})
    weights_file = tmp_path / "shard_weights.json"
    before = weights_file.read_text()

    with pytest.raises(SystemExit) as refusal:
        rebuild({"1:2": jobs}, logs, "1:2")

    assert "run 1:2: unit shard 7: cancelled" in str(refusal.value)
    assert "integration shard 2: skipped" in str(refusal.value)
    assert weights_file.read_text() == before


def test_a_partial_rerun_attempt_writes_nothing(rebuild, tmp_path):
    jobs, logs = _complete_run(COMMITTED)
    rerun = [job for job in jobs if job["name"] == "tests (3/12)"]
    weights_file = tmp_path / "shard_weights.json"
    before = weights_file.read_text()

    with pytest.raises(SystemExit) as refusal:
        rebuild({"1:1": jobs, "1:2": rerun}, logs, "1:1", "1:2")

    message = str(refusal.value)
    assert "run 1:2: unit shard 1: missing" in message
    assert "integration: expected one shard count, found no shard jobs" in message
    assert "run 1:1" not in message
    assert weights_file.read_text() == before


def test_a_run_missing_a_collected_file_writes_nothing(rebuild, tmp_path):
    forgotten = "tests/test_utc.py"
    observed = {suite: {n: s for n, s in files.items() if n != forgotten} for suite, files in COMMITTED.items()}
    jobs, logs = _complete_run(observed)
    weights_file = tmp_path / "shard_weights.json"
    before = weights_file.read_text()

    with pytest.raises(SystemExit) as refusal:
        rebuild({"1:1": jobs}, logs, "1")

    assert f"unit: no time recorded for {forgotten}" in str(refusal.value)
    assert weights_file.read_text() == before
