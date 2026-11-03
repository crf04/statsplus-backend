"""On/off parity against recorded PBP Stats WOWY splits (crf04/statsplus#410).

The recorded BOS 2025-26 season (12 splits) and DEN 2026-27 games (Jokic on
and off) go through the rotation job's write path and ``OnOffService``'s
segment read, and each split must stay within the parent issue's tolerances:
counts exact except at most one event, minutes within 0.05%, possessions
within 0.5%, usage within 5% per player with at least 100 minutes.
``python scripts/on_off_parity.py report`` prints the full table.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from scripts.on_off_parity import load_wowy, parity

FIXTURES = ("bos_2025_26", "den_2026_27")
SPLITS = [(name, split) for name in FIXTURES for split in load_wowy(name)["splits"]]


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    out = {}
    for name in FIXTURES:
        engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp(name) / 'parity.db'}")
        run_migrations(engine)
        out[name] = parity(name, engine)
        engine.dispose()
    return out


def test_every_recorded_split_is_compared():
    assert len(SPLITS) == 14


@pytest.mark.parametrize(("name", "split"), SPLITS)
def test_the_split_matches_pbp_stats_within_tolerance(results, name, split):
    result = results[name][split]

    assert result["count_events"] <= 1, result["count_mismatches"]
    assert result["minutes_pct"] <= 0.05
    assert result["possessions_pct"] <= 0.5
    assert result["usage_pct_worst"] <= 5.0


def test_the_only_count_difference_is_one_jaylen_brown_rebound(results):
    mismatches = {
        (name, split): [(player, key) for player, key, _ours, _theirs in result["count_mismatches"]]
        for name, splits in results.items()
        for split, result in splits.items()
        if result["count_mismatches"]
    }

    assert mismatches == {
        ("bos_2025_26", split): [(1627759, "reb")]
        for split in ("brown_on_tatum_off", "garza_off", "tatum_off", "white_brown_on")
    }
