"""The season stats loader replaces a season whole and never writes a bad response."""

import json

import pytest
from sqlalchemy import text

from app.services.player_season_stats import load_season
from scripts import load_player_season_stats
from tests.support.season_stats_world import (
    FETCHED_AT,
    JOKIC,
    JOKIC_ADVANCED,
    JOKIC_ADVANCED_LINE,
    JOKIC_LINE,
    RESERVE_LINE,
    FakeNBA,
    frame,
    migrated_repository,
    rows,
)


def _stored(repository, season="2023-24"):
    with repository.engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT player_id, player_name, team_abbreviation, age, gp, pts, min, plus_minus "
                    "FROM player_season_stats WHERE season = :season ORDER BY player_id"
                ),
                {"season": season},
            )
        ]


def _stored_advanced(repository, season="2023-24"):
    columns = ", ".join(JOKIC_ADVANCED)
    with repository.engine.connect() as connection:
        return {
            row[0]: dict(zip(JOKIC_ADVANCED, row[1:], strict=True))
            for row in connection.execute(
                text(f"SELECT player_id, {columns} FROM player_season_stats WHERE season = :season"),
                {"season": season},
            )
        }


def _fetching(*lines, **kwargs):
    return FakeNBA(*lines, **kwargs)


def test_loading_stores_the_nba_totals(tmp_path):
    repository = migrated_repository(tmp_path)

    count = load_season(_fetching(JOKIC_LINE, RESERVE_LINE), repository, "2023-24", now=FETCHED_AT)

    assert count == 2
    assert _stored(repository) == [
        (JOKIC, "Nikola Jokić", "DEN", 29, 79, 2085, 2736.5333333333333, 682),
        (1700001, "Reserve Guard", "MEM", 22, 20, 3, 61.0, -1),
    ]
    with repository.engine.connect() as connection:
        key = connection.execute(text("SELECT DISTINCT season_type FROM player_season_stats")).scalars().all()
    assert key == ["Regular Season"]


def test_loading_stores_jokics_advanced_stats_with_turnover_percent_as_a_fraction(tmp_path):
    repository = migrated_repository(tmp_path)

    load_season(_fetching(JOKIC_LINE, RESERVE_LINE), repository, "2023-24", now=FETCHED_AT)

    stored = _stored_advanced(repository)
    assert stored[JOKIC] == {
        "off_rating": 122.4,
        "def_rating": 110.6,
        "net_rating": 11.8,
        "ts_pct": 0.65,
        "efg_pct": 0.612,
        "usg_pct": 0.288,
        "ast_pct": 0.391,
        "ast_to": 2.99,
        "ast_ratio": 27.9,
        "oreb_pct": 0.086,
        "dreb_pct": 0.27,
        "reb_pct": 0.182,
        "tov_pct": 0.093,
        "pace": 98.05,
        "pie": 0.211,
        "poss": 5599,
    }
    assert (stored[1700001]["tov_pct"], stored[1700001]["off_rating"], stored[1700001]["poss"]) == (
        0.125,
        80.0,
        40,
    )
    assert stored[1700001]["ast_to"] is None


def test_a_nan_advanced_value_becomes_none_before_it_reaches_the_database():
    (row,) = rows("2023-24", RESERVE_LINE)

    assert row["ast_to"] is None
    assert row["tov_pct"] == 0.125


def test_a_base_player_without_an_advanced_row_stores_null_advanced_stats(tmp_path):
    repository = migrated_repository(tmp_path)

    load_season(
        _fetching(JOKIC_LINE, RESERVE_LINE, advanced=[JOKIC_ADVANCED_LINE]),
        repository,
        "2023-24",
        now=FETCHED_AT,
    )

    stored = _stored_advanced(repository)
    assert stored[JOKIC]["off_rating"] == 122.4
    assert stored[1700001] == dict.fromkeys(JOKIC_ADVANCED)
    assert [row[0] for row in _stored(repository)] == [JOKIC, 1700001]


def test_an_advanced_row_for_a_player_with_no_base_row_is_ignored(tmp_path):
    repository = migrated_repository(tmp_path)

    count = load_season(
        _fetching(JOKIC_LINE, advanced=[JOKIC_ADVANCED_LINE, {**JOKIC_ADVANCED_LINE, "PLAYER_ID": 99}]),
        repository,
        "2023-24",
        now=FETCHED_AT,
    )

    assert (count, sorted(_stored_advanced(repository))) == (1, [JOKIC])


def test_a_rerun_leaves_the_same_rows(tmp_path):
    repository = migrated_repository(tmp_path)
    fetch = _fetching(JOKIC_LINE, RESERVE_LINE)

    load_season(fetch, repository, "2023-24", now=FETCHED_AT)
    first = _stored(repository)
    count = load_season(fetch, repository, "2023-24", now=FETCHED_AT)

    assert (count, _stored(repository)) == (2, first)


def test_a_rerun_replaces_changed_rows_and_drops_players_no_longer_reported(tmp_path):
    repository = migrated_repository(tmp_path)
    load_season(_fetching(JOKIC_LINE, RESERVE_LINE), repository, "2023-24", now=FETCHED_AT)

    load_season(_fetching({**JOKIC_LINE, "GP": 80, "PTS": 2100}), repository, "2023-24", now=FETCHED_AT)

    assert _stored(repository) == [(JOKIC, "Nikola Jokić", "DEN", 29, 80, 2100, 2736.5333333333333, 682)]


def test_loading_one_season_leaves_another_alone(tmp_path):
    repository = migrated_repository(tmp_path)
    load_season(_fetching(JOKIC_LINE), repository, "2023-24", now=FETCHED_AT)

    load_season(_fetching({**JOKIC_LINE, "GP": 70}), repository, "2024-25", now=FETCHED_AT)

    assert [row[4] for row in _stored(repository, "2023-24")] == [79]
    assert [row[4] for row in _stored(repository, "2024-25")] == [70]


def _frame_missing(column):
    return FakeNBA(
        totals_frame=frame({key: value for key, value in JOKIC_LINE.items() if key != column}),
        advanced=[JOKIC_ADVANCED_LINE],
    )


def _advanced_missing(column):
    return FakeNBA(JOKIC_LINE, advanced=[{k: v for k, v in JOKIC_ADVANCED_LINE.items() if k != column}])


class _NoTotals(FakeNBA):
    def fetch_player_totals_stats(self, *, season):
        return None


@pytest.mark.parametrize(
    ("adapter", "message"),
    [
        (FakeNBA(JOKIC_LINE, totals_error=ConnectionError("stats.nba.com timed out")), "timed out"),
        (FakeNBA(), "no player rows"),
        (_NoTotals(JOKIC_LINE), "no player rows"),
        (_frame_missing("PLUS_MINUS"), "PLUS_MINUS"),
        (FakeNBA(JOKIC_LINE, JOKIC_LINE), "more than once"),
        (FakeNBA({**JOKIC_LINE, "GP": 0}), "no games played"),
        (FakeNBA({**JOKIC_LINE, "PTS": None}), "NaN|None"),
        (FakeNBA(JOKIC_LINE, advanced_error=ConnectionError("advanced timed out")), "advanced timed out"),
        (FakeNBA(JOKIC_LINE, advanced=[]), "no advanced player rows"),
        (_advanced_missing("TM_TOV_PCT"), "TM_TOV_PCT"),
        (
            FakeNBA(JOKIC_LINE, advanced=[JOKIC_ADVANCED_LINE, JOKIC_ADVANCED_LINE]),
            "advanced player more than once",
        ),
    ],
)
def test_a_bad_fetch_leaves_the_prior_rows(tmp_path, adapter, message):
    repository = migrated_repository(tmp_path)
    load_season(_fetching(JOKIC_LINE), repository, "2023-24", now=FETCHED_AT)
    before = (_stored(repository), _stored_advanced(repository))

    with pytest.raises(Exception, match=message):
        load_season(adapter, repository, "2023-24", now=FETCHED_AT)

    assert (_stored(repository), _stored_advanced(repository)) == before


def test_a_write_that_fails_part_way_leaves_the_prior_rows(tmp_path):
    repository = migrated_repository(tmp_path)
    load_season(_fetching(JOKIC_LINE), repository, "2023-24", now=FETCHED_AT)
    before = _stored(repository)
    good = {"season": "2023-24", "season_type": "Regular Season", "player_id": 1, "fetched_at": FETCHED_AT}

    with pytest.raises(Exception, match="NOT NULL"):
        repository.replace_season("2023-24", "Regular Season", [good])

    assert _stored(repository) == before


class _Adapter:
    """Stands in for the NBA adapter at the script's seam."""

    def __init__(self, lines=None, error=None, advanced_error=None):
        self.lines, self.error, self.advanced_error = lines, error, advanced_error

    def __call__(self, settings):
        return self

    def fetch_player_totals_stats(self, *, season):
        if self.error:
            raise self.error
        return frame(*self.lines)

    def fetch_player_advanced_stats(self, *, season):
        if self.advanced_error:
            raise self.advanced_error
        return frame(JOKIC_ADVANCED_LINE, {**JOKIC_ADVANCED_LINE, "PLAYER_ID": 1700001})


def _run(monkeypatch, capsys, tmp_path, adapter, *argv):
    import app.providers.nba_stats as provider

    monkeypatch.setattr(provider, "NBAStatsAdapter", adapter)
    url = f"sqlite:///{tmp_path / 'loader.sqlite3'}"
    code = load_player_season_stats.main([*argv, "--database-url", url])
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1]), url


def test_the_script_loads_a_season_and_reports_the_count(monkeypatch, capsys, tmp_path):
    code, printed, url = _run(
        monkeypatch, capsys, tmp_path, _Adapter([JOKIC_LINE, RESERVE_LINE]), "--season", "2023-24"
    )

    assert (code, printed) == (0, {"season": "2023-24", "players": 2})


def test_the_script_exits_1_and_keeps_prior_rows_when_the_fetch_fails(monkeypatch, capsys, tmp_path):
    _run(monkeypatch, capsys, tmp_path, _Adapter([JOKIC_LINE]), "--season", "2023-24")

    code, printed, url = _run(
        monkeypatch, capsys, tmp_path, _Adapter(error=ConnectionError("boom")), "--season", "2023-24"
    )

    assert (code, printed) == (1, {"season": "2023-24", "error": "ConnectionError: boom"})
    from sqlalchemy import create_engine

    with create_engine(url).connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM player_season_stats")).scalar() == 1


def test_the_script_exits_1_and_keeps_prior_rows_when_the_advanced_fetch_fails(monkeypatch, capsys, tmp_path):
    _run(monkeypatch, capsys, tmp_path, _Adapter([JOKIC_LINE]), "--season", "2023-24")

    code, printed, url = _run(
        monkeypatch,
        capsys,
        tmp_path,
        _Adapter([{**JOKIC_LINE, "GP": 1}], advanced_error=ConnectionError("advanced boom")),
        "--season",
        "2023-24",
    )

    assert (code, printed) == (1, {"season": "2023-24", "error": "ConnectionError: advanced boom"})
    from sqlalchemy import create_engine

    with create_engine(url).connect() as connection:
        assert connection.execute(text("SELECT gp FROM player_season_stats")).scalars().all() == [79]


def test_the_script_rejects_a_malformed_season(monkeypatch, capsys, tmp_path):
    with pytest.raises(SystemExit) as stop:
        _run(monkeypatch, capsys, tmp_path, _Adapter([]), "--season", "2023-25")

    assert stop.value.code == 2
