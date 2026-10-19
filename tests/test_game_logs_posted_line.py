"""``posted_line`` on ``/api/games/game_logs`` (crf04/statsplus#238).

The route is called as the web app and the MCP call it, over the real
``GameService``, the real ``PostedLinesReader`` and the real closing-line
table; the box-score source, the event catalog and the player pool are stubs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert, text

from app.migrations import run_migrations
from app.models.closing_lines import ClosingLine
from app.services.closing_lines import ClosingLineReader
from app.services.game_service import GameService
from app.services.player_pool import PlayerPool, PoolPlayer
from app.services.posted_lines import PostedLinesReader
from tests.test_game_logs_closing_line import LEBRON, WHEN, _frame

LAL, GSW = 1610612747, 1610612744
#: 2026-10-21 20:00 Eastern.
NOW = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)
URL = "/api/games/game_logs?player_name=LeBron+James&season_filter=2025-26"


def _event(game_id, scheduled_at, home, away):
    ids = {"LAL": LAL, "GSW": GSW}
    return {
        "nba_game_id": game_id,
        "scheduled_at": scheduled_at,
        "classification": "",
        "home_team_id": ids[home],
        "home_team_tricode": home,
        "away_team_id": ids[away],
        "away_team_tricode": away,
    }


class Catalog:
    def __init__(self, *events):
        self.events = events

    def count_events(self, season):
        return len(self.events)

    def get_events_between(self, season, start, end):
        return [
            event for event in self.events if start.isoformat() <= event["scheduled_at"] < end.isoformat()
        ]


class Pools:
    def __init__(self, by_game, *, player_id=LEBRON, team_id=LAL):
        self.by_game = by_game
        self.player_id = player_id
        self.team_id = team_id

    def get_pool_for_game(self, *, season, game_id):
        lines = self.by_game.get(game_id)
        if lines is None:
            return None
        return PlayerPool(
            players=(PoolPlayer(self.player_id, "Someone", self.team_id, ("PTS",), {}, lines),),
            team_counts={self.team_id: 1},
            freshness={},
        )


#: Tonight: LAL hosts GSW. Past: LAL at DEN-less stub, GSW hosting LAL.
TONIGHT = _event("0022600012", "2026-10-22T02:00:00+00:00", "LAL", "GSW")
PAST = _event("0022500101", "2025-11-05T02:00:00+00:00", "GSW", "LAL")


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'logs.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(ClosingLine.__table__),
            [
                dict(
                    canonical_game_id="0022500101",
                    canonical_player_id=LEBRON,
                    market_category="PTS",
                    book=book,
                    line=line,
                    snapshot_at=WHEN,
                    odds_event_id="e",
                    bought_at=WHEN,
                )
                for book, line in (("draftkings", 24.5), ("fanduel", 25.5))
            ],
        )
    return engine


def _serve(dependencies, engine, *, catalog=None, pools=None, with_reader=True, no_logs=False):
    class Logs:
        def get_player_logs(self, player_id, season, **kwargs):
            return _frame().iloc[0:0] if no_logs else _frame()

    class Players:
        def get_catalog(self, season, *, active_only=False):
            return [
                dict(player_id=LEBRON, display_name="LeBron James", is_active_for_season=True, team_id=LAL)
            ]

    closing = ClosingLineReader(engine)
    posted = (
        PostedLinesReader(
            event_catalog=catalog,
            player_pool=pools,
            settings=dependencies.settings,
            closing_lines=closing,
            clock=lambda: NOW,
        )
        if with_reader
        else None
    )
    dependencies.game_service = GameService(
        engine,
        redis_client=SimpleNamespace(),
        settings=dependencies.settings,
        athlete_catalog=Players(),
        game_logs_source=Logs(),
        closing_lines=closing,
        posted_lines=posted,
    )


def test_prizepicks_wins_over_another_provider_for_every_market(dependencies, client, engine):
    pools = Pools(
        {
            "0022600012": {
                "draftkings": {"PTS": 26.5, "REB": 7.5},
                "prizepicks": {"PTS": 27.5},
            }
        }
    )
    _serve(dependencies, engine, catalog=Catalog(TONIGHT), pools=pools)

    response = client.get(URL)

    assert response.status_code == 200
    assert response.get_json()["posted_line"] == {
        "game_id": "0022600012",
        "game_date": "2026-10-21",
        "matchup": "LAL vs GSW",
        "lines": {
            "PTS": {"value": 27.5, "provider": "prizepicks"},
            "REB": {"value": 7.5, "provider": "draftkings"},
        },
    }


def test_only_another_provider_posted_and_away_games_read_from_his_side(dependencies, client, engine):
    away = _event("0022600012", "2026-10-22T02:00:00+00:00", "GSW", "LAL")
    pools = Pools({"0022600012": {"underdog": {"PTS": 28.5}}})
    _serve(dependencies, engine, catalog=Catalog(away), pools=pools)

    assert client.get(URL).get_json()["posted_line"] == {
        "game_id": "0022600012",
        "game_date": "2026-10-21",
        "matchup": "LAL @ GSW",
        "lines": {"PTS": {"value": 28.5, "provider": "underdog"}},
    }


def test_his_next_games_line_when_his_team_is_not_playing_today(dependencies, client, engine):
    tomorrow = _event("0022600020", "2026-10-23T02:00:00+00:00", "GSW", "LAL")
    pools = Pools({"0022600020": {"prizepicks": {"PTS": 27.5}}})
    _serve(dependencies, engine, catalog=Catalog(tomorrow), pools=pools)

    assert client.get(URL).get_json()["posted_line"] == {
        "game_id": "0022600020",
        "game_date": "2026-10-22",
        "matchup": "LAL @ GSW",
        "lines": {"PTS": {"value": 27.5, "provider": "prizepicks"}},
    }


def test_null_when_his_game_today_has_no_line_even_if_a_later_one_does(dependencies, client, engine):
    tomorrow = _event("0022600020", "2026-10-23T02:00:00+00:00", "GSW", "LAL")
    pools = Pools({"0022600020": {"prizepicks": {"PTS": 27.5}}})
    _serve(dependencies, engine, catalog=Catalog(TONIGHT, tomorrow), pools=pools)

    assert client.get(URL).get_json()["posted_line"] is None


def test_null_when_his_next_game_is_beyond_the_look_ahead(dependencies, client, engine):
    in_three_days = _event("0022600040", "2026-10-25T02:00:00+00:00", "LAL", "GSW")
    pools = Pools({"0022600040": {"prizepicks": {"PTS": 27.5}}})
    _serve(dependencies, engine, catalog=Catalog(in_three_days), pools=pools)

    response = client.get(URL)

    assert response.status_code == 200
    assert response.get_json()["posted_line"] is None


def test_a_named_line_day_does_not_look_ahead(dependencies, client, engine):
    tomorrow = _event("0022600020", "2026-10-23T02:00:00+00:00", "LAL", "GSW")
    pools = Pools({"0022600020": {"prizepicks": {"PTS": 27.5}}})
    _serve(dependencies, engine, catalog=Catalog(tomorrow), pools=pools)

    assert client.get(URL + "&line_date=2026-10-21").get_json()["posted_line"] is None


def test_null_when_the_game_has_no_pool(dependencies, client, engine):
    _serve(dependencies, engine, catalog=Catalog(TONIGHT), pools=Pools({}))

    response = client.get(URL)

    assert response.status_code == 200
    assert response.get_json()["posted_line"] is None


def test_null_when_the_collaborator_is_absent(dependencies, client, engine):
    _serve(dependencies, engine, with_reader=False)

    response = client.get(URL)

    assert response.status_code == 200
    assert response.get_json()["posted_line"] is None


def test_a_failing_lookup_does_not_fail_the_logs(dependencies, client, engine):
    class Broken:
        def get_pool_for_game(self, **kwargs):
            raise RuntimeError("pool store down")

    _serve(dependencies, engine, catalog=Catalog(TONIGHT), pools=Broken())

    response = client.get(URL)

    assert response.status_code == 200
    assert response.get_json()["posted_line"] is None
    assert [row["PTS"] for row in response.get_json()["game_logs"]] == [27, 22, 31]


def test_a_named_future_day_reads_that_days_pool_line(dependencies, client, engine):
    tomorrow = _event("0022600020", "2026-10-23T02:00:00+00:00", "LAL", "GSW")
    pools = Pools({"0022600012": {"prizepicks": {"PTS": 27.5}}, "0022600020": {"prizepicks": {"PTS": 29.5}}})
    _serve(dependencies, engine, catalog=Catalog(TONIGHT, tomorrow), pools=pools)

    assert client.get(URL + "&line_date=2026-10-22").get_json()["posted_line"] == {
        "game_id": "0022600020",
        "game_date": "2026-10-22",
        "matchup": "LAL vs GSW",
        "lines": {"PTS": {"value": 29.5, "provider": "prizepicks"}},
    }


def test_a_named_past_day_with_no_pool_uses_the_stored_closing_line(dependencies, client, engine):
    _serve(dependencies, engine, catalog=Catalog(PAST), pools=Pools({}))

    assert client.get(URL + "&line_date=2025-11-04").get_json()["posted_line"] == {
        "game_id": "0022500101",
        "game_date": "2025-11-04",
        "matchup": "LAL @ GSW",
        "lines": {"PTS": {"value": 24.5, "provider": "draftkings"}},
    }


def test_a_named_past_day_without_a_closing_line_is_null(dependencies, client, engine):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM closing_lines"))
    _serve(dependencies, engine, catalog=Catalog(PAST), pools=Pools({}))

    assert client.get(URL + "&line_date=2025-11-04").get_json()["posted_line"] is None


@pytest.mark.parametrize("bad", ["tomorrow", "2026-13-40", "10/21/2026"])
def test_a_malformed_line_date_is_a_400(dependencies, client, engine, bad):
    _serve(dependencies, engine, catalog=Catalog(TONIGHT), pools=Pools({}))

    response = client.get(URL + f"&line_date={bad}")

    assert response.status_code == 400


@pytest.mark.parametrize("frozen", [True, False])
def test_a_past_day_reads_the_stored_closing_pool_long_after_the_live_windows(
    dependencies, client, engine, tmp_path, frozen
):
    """The real archive reader serves a started game's frozen pool at any age.

    The pool is read three months after capture, far outside the 15-minute
    and 6-hour live windows, and still yields every market, not only PTS --
    whether or not the collector froze a closing set for the game.
    """

    from datetime import timedelta

    from app.providers.dfs import NBAMarketQuery
    from app.services.projection_archive import (
        LatestProjectionPlayerPoolReader,
        ProjectionArchive,
        ProjectionArchiveReadScope,
    )
    from app.services.statistic_catalog import StatisticCatalog
    from tests.services.test_projection_archive import (
        GAME_ID,
        OBSERVED_AT,
        SEASON,
        _line_market,
        _line_snapshot,
    )

    archive_engine = create_engine(f"sqlite:///{tmp_path / 'archive.db'}")
    run_migrations(archive_engine)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(archive_engine, catalog)
    query = NBAMarketQuery(season=SEASON)
    archive.ingest_complete_snapshot(
        _line_snapshot(
            "prizepicks",
            (
                _line_market(
                    catalog, provider="prizepicks", market_id="pts", statistic_id="points", threshold="27.5"
                ),
                _line_market(
                    catalog, provider="prizepicks", market_id="reb", statistic_id="rebounds", threshold="7.5"
                ),
            ),
        ),
        query=query,
        accepted_at=OBSERVED_AT,
    )
    tip = OBSERVED_AT + timedelta(minutes=5)
    event = {
        "nba_game_id": GAME_ID,
        "scheduled_at": tip.isoformat(),
        "classification": "",
        "status_text": "Final",
        "status_code": 3,
        "first_observed_started_at": tip.isoformat(),
        "home_team_id": GSW,
        "home_team_tricode": "GSW",
        "away_team_id": LAL,
        "away_team_tricode": "LAL",
    }
    if frozen:
        archive.freeze_closing_projection_set(
            provider="prizepicks",
            query=query,
            canonical_game_id=GAME_ID,
            started_at=tip,
            created_at=tip,
        )

    class Schedule:
        def count_events(self, season):
            return 1

        def get_events_between(self, season, start, end):
            return [event]

        def get_events_by_ids(self, season, game_ids):
            return [event]

    reader = LatestProjectionPlayerPoolReader(
        archive_engine,
        ProjectionArchiveReadScope(provider="prizepicks", query=query),
        clock=lambda: OBSERVED_AT + timedelta(days=90),
        event_reader=Schedule(),
    )
    _serve(dependencies, engine, catalog=Schedule(), pools=reader)

    assert client.get(URL + "&line_date=2026-01-02").get_json()["posted_line"] == {
        "game_id": GAME_ID,
        "game_date": "2026-01-02",
        "matchup": "LAL @ GSW",
        "lines": {
            "PTS": {"value": 27.5, "provider": "prizepicks"},
            "REB": {"value": 7.5, "provider": "prizepicks"},
        },
    }


def test_alphabetical_provider_order_breaks_the_tie_when_prizepicks_is_absent(dependencies, client, engine):
    pools = Pools({"0022600012": {"underdog": {"PTS": 30.5}, "bovada": {"PTS": 29.5}}})
    _serve(dependencies, engine, catalog=Catalog(TONIGHT), pools=pools)

    lines = client.get(URL).get_json()["posted_line"]["lines"]

    assert lines == {"PTS": {"value": 29.5, "provider": "bovada"}}


def test_another_players_pool_line_is_not_his(dependencies, client, engine):
    pools = Pools({"0022600012": {"prizepicks": {"PTS": 20.5}}}, player_id=201939)
    _serve(dependencies, engine, catalog=Catalog(TONIGHT), pools=pools)

    assert client.get(URL).get_json()["posted_line"] is None


def test_a_pool_line_for_a_team_outside_the_game_is_not_his(dependencies, client, engine):
    pools = Pools({"0022600012": {"prizepicks": {"PTS": 20.5}}}, team_id=1610612738)
    _serve(dependencies, engine, catalog=Catalog(TONIGHT), pools=pools)

    assert client.get(URL).get_json()["posted_line"] is None


def test_a_stored_closing_line_never_outranks_the_games_pool_line(dependencies, client, engine):
    pools = Pools({"0022500101": {"prizepicks": {"PTS": 30.5, "REB": 8.5}}})
    _serve(dependencies, engine, catalog=Catalog(PAST), pools=pools)

    assert client.get(URL + "&line_date=2025-11-04").get_json()["posted_line"]["lines"] == {
        "PTS": {"value": 30.5, "provider": "prizepicks"},
        "REB": {"value": 8.5, "provider": "prizepicks"},
    }


def test_a_player_without_logs_this_season_still_gets_his_line(dependencies, client, engine):
    pools = Pools({"0022600012": {"prizepicks": {"PTS": 27.5}}})
    _serve(dependencies, engine, catalog=Catalog(TONIGHT), pools=pools, no_logs=True)

    body = client.get(URL).get_json()

    assert body["game_logs"] == []
    assert body["posted_line"]["lines"] == {"PTS": {"value": 27.5, "provider": "prizepicks"}}


def test_line_date_changes_no_game_or_average(dependencies, client, engine):
    pools = Pools({"0022500101": {"prizepicks": {"PTS": 30.5}}})
    _serve(dependencies, engine, catalog=Catalog(PAST), pools=pools)

    plain = client.get(URL).get_json()

    # A day in the middle of the window and one past its last game.
    for day in ("2025-11-06", "2025-11-08", "2026-10-21"):
        named = client.get(URL + f"&line_date={day}").get_json()
        assert [row["GAME_DATE"] for row in named["game_logs"]] == [
            "2025-11-04",
            "2025-11-06",
            "2025-11-08",
        ]
        for key in ("game_logs", "averages", "season_averages", "season_game_count"):
            assert named[key] == plain[key]


@pytest.mark.parametrize("bad", ["0", "1792540800", "2026-10-21T00:00:00", "2026-10-21Z"])
def test_a_lax_line_date_spelling_is_a_400(dependencies, client, engine, bad):
    _serve(dependencies, engine, catalog=Catalog(TONIGHT), pools=Pools({}))

    assert client.get(URL + f"&line_date={bad}").status_code == 400


def test_a_frozen_provider_keeps_its_own_lines_over_the_retained_ones(dependencies, client, engine):
    class Both(Pools):
        def get_retained_pool_for_game(self, *, season, game_id):
            return Pools({game_id: {"dabble": {"PTS": 99.5}, "underdog": {"REB": 6.5}}}).get_pool_for_game(
                season=season, game_id=game_id
            )

    pools = Both({"0022500101": {"dabble": {"PTS": 26.5}}})
    _serve(dependencies, engine, catalog=Catalog(PAST), pools=pools)

    assert client.get(URL + "&line_date=2025-11-04").get_json()["posted_line"]["lines"] == {
        "PTS": {"value": 26.5, "provider": "dabble"},
        "REB": {"value": 6.5, "provider": "underdog"},
    }


def test_a_frozen_providers_markets_are_authoritative(dependencies, client, engine):
    """A retained market the provider's freeze lacks is not added to it."""

    class Both(Pools):
        def get_retained_pool_for_game(self, *, season, game_id):
            return Pools({game_id: {"dabble": {"PTS": 26.5, "REB": 8.5}}}).get_pool_for_game(
                season=season, game_id=game_id
            )

    pools = Both({"0022500101": {"dabble": {"PTS": 26.5}}})
    _serve(dependencies, engine, catalog=Catalog(PAST), pools=pools)

    assert client.get(URL + "&line_date=2025-11-04").get_json()["posted_line"]["lines"] == {
        "PTS": {"value": 26.5, "provider": "dabble"},
    }
