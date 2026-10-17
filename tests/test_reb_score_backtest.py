"""The REB Matchup Score backtest on a synthetic season with a known answer (#447).

``tests/support/synthetic_rebound_season.py`` documents the season: every
opponent allows the same total rebounds, only the mix of two- and three-point
miss rebounds differs, so the flat ``OPP_REB`` score is constant while a
guard-heavy diet is predicted by the rebound-type score.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pytest
from sqlalchemy import text

from scripts import reb_score_backtest as backtest
from tests.support.synthetic_rebound_season import (
    FIRST_DAY,
    SEASON,
    synthetic_season,
    tendency,
)

RESAMPLES = 400
RHO_DIFFERENCE = 0.36075923209656047
CLUSTER_CI = (-0.03877560846152613, 0.8774789273726262)
ROW_CI = (0.0007539196153944106, 0.7393533029655015)


@pytest.fixture(scope="module")
def season():
    return synthetic_season()


@pytest.fixture(scope="module")
def samples(season):
    return backtest.build_samples(season, season=SEASON)


@pytest.fixture(scope="module")
def report(samples):
    return backtest.evaluate(samples, resamples=RESAMPLES, seed=7)


def test_guard_heavy_diet_beats_the_flat_score_overall_and_among_bigs(report):
    for window in ("season", "last_15"):
        overall = report["windows"][window]["overall"]
        bigs = report["windows"][window]["bigs"]
        # 20 test days x 30 teams x 8 players of 28 minutes; the two scrubs
        # per team (8 minutes) are under the floor.  Half of them are bigs.
        assert overall["n"] == 4800
        assert bigs["n"] == 2400
        for cell in (overall, bigs):
            assert cell["rho_current"] == 0.0
            assert cell["rho_new"] > 0.9
            assert cell["ci95"][0] > 0
            assert cell["win"] is True
    assert report["verdict"] == "win"


def test_a_big_is_scored_on_the_two_point_slice_he_collects(samples):
    # Day 21 is the first with 20 prior games.  A big's diet is all two-point
    # misses, so the score is that slice's ratio - 1 = -2t/15 (opponent allows
    # 30 - 4t per 48 against a league average of 30).
    first_day = FIRST_DAY + timedelta(days=20)
    bigs = [s for s in samples if s.game_date == first_day and s.two_point_share == pytest.approx(1.0)]
    assert len(bigs) == 120
    assert {round(s.scores["season"][1], 6) for s in bigs} == {
        round(-2 * t / 15, 6) for t in (-2, -1, 0, 1, 2)
    }
    assert {s.scores["season"][0] for s in bigs} == {0.0}


def test_no_fact_dated_on_or_after_the_test_game_is_read(season, samples):
    outlier_day = FIRST_DAY + timedelta(days=24)
    shocked = backtest.build_samples(synthetic_season(outlier_from=outlier_day), season=SEASON)

    def on(rows, day):
        return {(s.game_id, s.player_id): s for s in rows if s.game_date == day}

    before, after = on(samples, outlier_day), on(shocked, outlier_day)
    assert len(before) == len(after) == 240
    for key, sample in before.items():
        assert after[key].scores == sample.scores
        assert after[key].two_point_share == sample.two_point_share
    # The test game itself did change (the target), and the next day, which may
    # read the outlier day, scores differently: the comparison is not vacuous.
    assert any(after[k].target != before[k].target for k in before)
    next_day = outlier_day + timedelta(days=1)
    assert on(shocked, next_day) != on(samples, next_day)


def test_target_is_rebounds_minus_the_prior_games_average(season, samples):
    # Player 110, day 25: his rebounds in that game less his mean over the
    # 24 games before it, read straight off the fixture.
    day = FIRST_DAY + timedelta(days=24)
    rebounds = {
        game.game_date: player.rebounds
        for game in season
        for player in game.player_facts
        if player.player_id == 110
    }
    prior = [value for date, value in rebounds.items() if date < day]
    assert len(prior) == 24
    (sample,) = [s for s in samples if s.player_id == 110 and s.game_date == day]
    assert sample.target == pytest.approx(rebounds[day] - sum(prior) / 24)
    assert sample.target != pytest.approx(rebounds[day] - sum(prior) / 24 + 1e-3)


def test_offensive_two_point_rebounds_count_toward_a_bigs_diet():
    # Four offensive rebounds off two-point misses per big per game: his
    # two-point share is still 1.0, but the defensive part alone is only
    # (6 - t) / (10 - t), which is under 60% against a t = +2 opponent.
    rows = backtest.build_samples(synthetic_season(big_offensive=4), season=SEASON)
    bigs = [s for s in rows if backtest.is_big(s.two_point_share)]
    assert len(bigs) == 2400
    assert all(s.two_point_share == pytest.approx(1.0) for s in bigs)
    report = backtest.evaluate(rows, resamples=RESAMPLES, seed=7)
    assert report["windows"]["season"]["bigs"]["win"] is True


def test_bigs_threshold_is_inclusive_at_sixty_percent():
    assert backtest.is_big(0.60) is True
    assert backtest.is_big(0.5999) is False


def test_player_without_enough_prior_games_is_excluded(samples):
    # 19 prior games on day 20, 20 on day 21.
    days = {s.game_date for s in samples}
    assert min(days) == FIRST_DAY + timedelta(days=20)


def test_spearman_ranks_ties_by_their_average():
    assert backtest.spearman([1, 2, 3, 4], [1, 3, 2, 4]) == pytest.approx(0.8)
    assert backtest.spearman([1, 1, 2, 2], [1, 2, 3, 4]) == pytest.approx(0.894427, abs=1e-6)
    assert backtest.spearman([5, 5, 5], [1, 2, 3]) == 0.0


def test_opponent_tendency_fixture_is_flat_in_total():
    # 4 guards x (5 + t) + 4 bigs x (6 - t) + 2 scrubs = 46 for every t.
    assert {4 * (5 + tendency(i)) + 4 * (6 - tendency(i)) + 2 for i in range(1, 31)} == {46}


def test_cli_prints_summary_and_json_and_reads_a_ledger(tmp_path, monkeypatch, capsys, season):
    monkeypatch.setattr(backtest, "load_games", lambda url, name: season)
    output = tmp_path / "report.json"
    assert backtest.main(["--database-url", "sqlite://", "--output", str(output)]) == 0
    assert "verdict: win" in capsys.readouterr().out
    saved = json.loads(output.read_text())
    assert saved["verdict"] == "win"
    assert saved["bootstrap"] == {"resamples": 2000, "seed": 234447, "unit": "game day"}
    assert saved["windows"]["season"]["overall"]["n"] == 4800


def test_cli_refuses_too_few_resamples():
    with pytest.raises(SystemExit):
        backtest.main(["--database-url", "sqlite://", "--resamples", "1999"])


def test_cli_rejects_games_without_the_miss_type_split(monkeypatch, capsys, season):
    from dataclasses import replace

    game = season[0]
    stripped = replace(
        game,
        player_facts=tuple(replace(p, def_two_pt_rebounds=None) for p in game.player_facts),
    )
    monkeypatch.setattr(backtest, "load_games", lambda url, name: (stripped, *season[1:]))
    assert backtest.main(["--database-url", "sqlite://"]) == 2
    assert "rebound_types_not_derived" in capsys.readouterr().err


def test_load_games_cannot_write(tmp_path):
    from sqlalchemy import create_engine

    from app.migrations import run_migrations

    path = tmp_path / "ledger.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    run_migrations(engine)
    engine.dispose()
    assert backtest.load_games(f"sqlite:///{path}", SEASON) == ()
    guarded = backtest.open_read_only_engine(f"sqlite:///{path}")
    with guarded.begin() as connection, pytest.raises(Exception, match="readonly"):
        connection.execute(text("DELETE FROM canonical_game_ledger_games"))
    guarded.dispose()


# --- the decision statistics, on literal data --------------------------------------

#: 8 game days x 4 player-games.  The candidate is better than the current score
#: in rank terms, but a day-level shock drives both it and the target, so the
#: evidence is eight days, not thirty-two independent rows.
_CURRENT = [
    1.2,
    0.7,
    -2.0,
    0.3,
    -1.1,
    0.0,
    0.0,
    -2.0,
    -0.2,
    -0.3,
    1.0,
    -1.2,
    0.7,
    -1.1,
    -0.3,
    -0.8,
    1.4,
    0.6,
    2.4,
    0.6,
    0.8,
    0.8,
    -0.6,
    -0.1,
    1.4,
    -0.4,
    0.2,
    0.0,
    0.6,
    -0.4,
    -0.2,
    0.2,
]
_NEW = [
    -1.0,
    -2.5,
    0.3,
    -4.8,
    -5.5,
    -4.4,
    -0.6,
    -4.2,
    -2.5,
    -3.4,
    0.0,
    -2.9,
    -2.1,
    2.3,
    -1.6,
    -0.1,
    2.5,
    1.8,
    1.3,
    4.8,
    -1.3,
    -2.4,
    -4.1,
    -0.2,
    -1.8,
    -0.7,
    -2.6,
    -2.3,
    -3.6,
    -3.3,
    -1.1,
    -2.9,
]
_TARGET = [
    -0.9,
    0.0,
    -1.3,
    -2.8,
    -3.6,
    -1.0,
    -2.4,
    -4.4,
    -0.6,
    -1.7,
    -1.1,
    -1.0,
    0.1,
    1.4,
    0.8,
    0.3,
    2.7,
    3.1,
    0.6,
    2.0,
    -0.8,
    0.0,
    -1.1,
    0.2,
    -1.1,
    -1.4,
    -2.2,
    -1.5,
    -2.7,
    -2.9,
    -1.3,
    -2.7,
]


def _rows():
    return [
        (date(2026, 1, 1) + timedelta(days=i // 4), c, n, t)
        for i, (c, n, t) in enumerate(zip(_CURRENT, _NEW, _TARGET))
    ]


def test_spearman_is_rank_based_not_linear():
    # Monotone but wildly non-linear: Spearman is exactly 1, Pearson is 0.725.
    x, y = [1, 2, 3, 4, 5], [1, 2, 3, 4, 100]
    assert backtest.spearman(x, y) == pytest.approx(1.0, abs=1e-12)
    assert float(np.corrcoef(x, y)[0, 1]) == pytest.approx(0.725, abs=1e-3)


def test_better_rho_with_an_interval_through_zero_is_a_loss():
    cell = backtest.evaluate_cell(_rows(), resamples=2000, seed=3)
    assert cell["difference"] == pytest.approx(RHO_DIFFERENCE, abs=1e-9)
    assert cell["rho_new"] > cell["rho_current"]
    assert cell["ci95"][0] < 0 < cell["ci95"][1]
    assert cell["win"] is False


def test_the_interval_resamples_game_days_not_rows():
    days = np.array([row[0].toordinal() for row in _rows()])
    args = (np.array(_CURRENT), np.array(_NEW), np.array(_TARGET))
    by_day = backtest.cluster_bootstrap_difference(*args, days, resamples=2000, seed=3)
    by_row = backtest.cluster_bootstrap_difference(*args, np.arange(len(days)), resamples=2000, seed=3)
    assert by_day == pytest.approx(CLUSTER_CI, abs=1e-9)
    assert by_row == pytest.approx(ROW_CI, abs=1e-9)
    # Treating the 32 rows as independent would call this a win.
    assert by_day[0] < 0 < by_row[0]


# --- windows, minutes and the missing diet -----------------------------------------


def _players_on(rows, day, *, kind):
    return [s for s in rows if s.game_date == day and s.player_id % 100 // 10 == kind]


def _opponent_tendency(season, sample):
    game = next(g for g in season if g.game_id == sample.game_id)
    team = next(p.team_id for p in game.player_facts if p.player_id == sample.player_id)
    return tendency(next(f.opponent_team_id for f in game.team_facts if f.team_id == team))


def test_season_and_last_15_scores_differ_when_the_opponents_change():
    # From day 21 (index 20) every opponent's tendency flips.  On day index 37
    # the 37 prior days hold 20 unflipped and 17 flipped; the last 15 games
    # (days 22-36) are all flipped.  A big's score is the two-point slice
    # ratio - 1: Season -12t/(37*30) = -2t/185, Last 15 +4t/30 = 2t/15.
    flipped = synthetic_season(shift_from=FIRST_DAY + timedelta(days=20))
    rows = backtest.build_samples(flipped, season=SEASON)
    day = FIRST_DAY + timedelta(days=37)
    seen = {
        _opponent_tendency(flipped, s): (
            round(s.scores["season"][1], 6),
            round(s.scores["last_15"][1], 6),
        )
        for s in _players_on(rows, day, kind=1)
    }
    assert seen == {t: (round(-2 * t / 185, 6), round(2 * t / 15, 6)) for t in (-2, -1, 0, 1, 2)}


@pytest.mark.parametrize(("minutes", "included"), [((15.0, 1.0), True), ((14.98, 1.02), False)])
def test_minutes_floor_is_fifteen_inclusive(minutes, included):
    rows = backtest.build_samples(synthetic_season(scrub_minutes=minutes), season=SEASON)
    scrubs = [s for s in rows if s.player_id % 100 == 20]
    # One scrub per team on each of the 20 test days, or none.
    assert len(scrubs) == (600 if included else 0)


def test_player_without_a_rebound_diet_keeps_the_current_score():
    # The first scrub plays 15 minutes and never rebounds, so he has no diet
    # and the Matchup would fall back to the current score.  Totals vary by
    # opponent here (4(7 + 2t) + 4(6 - t) = 52 + 4t), so the current score is
    # t/13, not zero.
    season = synthetic_season(scrub_minutes=(15.0, 1.0), scrub_rebounds=0, varying_total=True)
    rows = backtest.build_samples(season, season=SEASON)
    silent = [s for s in rows if s.player_id % 100 == 20]
    assert len(silent) == 600
    assert {_opponent_tendency(season, s): s.scores["season"] for s in silent} == {
        t: (round(t / 13, 6),) * 2 for t in (-2, -1, 0, 1, 2)
    }
    assert all(s.fell_back for s in silent)
    report = backtest.evaluate(rows, resamples=RESAMPLES, seed=7)
    assert report["population"]["candidate_fell_back_to_current"] == 600
