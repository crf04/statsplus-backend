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

LAL, GSW, DEN = 1610612747, 1610612744, 1610612743
#: 2026-10-21 20:00 Eastern.
NOW = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)
URL = "/api/games/game_logs?player_name=LeBron+James&season_filter=2025-26"


def _event(game_id, scheduled_at, home, away):
    ids = {"LAL": LAL, "GSW": GSW, "DEN": DEN}
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


def _serve(
    dependencies,
    engine,
    *,
    catalog=None,
    pools=None,
    with_reader=True,
    no_logs=False,
    frame=None,
    team_now=LAL,
    team_reads=None,
    now=NOW,
):
    class Logs:
        def get_player_logs(self, player_id, season, **kwargs):
            logs = _frame() if frame is None else frame
            return logs.iloc[0:0] if no_logs else logs

    class Players:
        # 2025-26 resolves his name; the schedule's season (2026-27) holds his
        # team now, or no row when ``team_now`` is None.
        def get_catalog(self, season, *, active_only=False):
            if season != "2025-26" and team_reads is not None:
                team_reads.append(season)
                if team_now == "fails":
                    raise RuntimeError("catalog down")
            team = LAL if season == "2025-26" else team_now
            if team is None:
                return []
            return [
                dict(player_id=LEBRON, display_name="LeBron James", is_active_for_season=True, team_id=team)
            ]

    closing = ClosingLineReader(engine)
    posted = (
        PostedLinesReader(
            event_catalog=catalog,
            player_pool=pools,
            settings=dependencies.settings,
            closing_lines=closing,
            athlete_catalog=Players(),
            clock=lambda: now,
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
        "provider_lines": {"draftkings": {"PTS": 26.5, "REB": 7.5}, "prizepicks": {"PTS": 27.5}},
        "line_prices": {},
        "book_lines": {},
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
        "provider_lines": {"underdog": {"PTS": 28.5}},
        "line_prices": {},
        "book_lines": {},
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
        "provider_lines": {"prizepicks": {"PTS": 27.5}},
        "line_prices": {},
        "book_lines": {},
    }


def test_null_when_his_game_today_has_no_line_even_if_a_later_one_does(dependencies, client, engine):
    tomorrow = _event("0022600020", "2026-10-23T02:00:00+00:00", "GSW", "LAL")
    pools = Pools({"0022600020": {"prizepicks": {"PTS": 27.5}}})
    _serve(dependencies, engine, catalog=Catalog(TONIGHT, tomorrow), pools=pools)

    assert client.get(URL).get_json()["posted_line"] is None


def test_his_next_games_line_a_week_ahead(dependencies, client, engine):
    in_seven_days = _event("0022600070", "2026-10-29T02:00:00+00:00", "LAL", "GSW")
    pools = Pools({"0022600070": {"prizepicks": {"PTS": 25.5}}})
    _serve(dependencies, engine, catalog=Catalog(in_seven_days), pools=pools)

    assert client.get(URL).get_json()["posted_line"]["game_date"] == "2026-10-28"


def test_null_when_his_next_game_is_beyond_the_look_ahead(dependencies, client, engine):
    in_eight_days = _event("0022600080", "2026-10-30T02:00:00+00:00", "LAL", "GSW")
    pools = Pools({"0022600080": {"prizepicks": {"PTS": 27.5}}})
    _serve(dependencies, engine, catalog=Catalog(in_eight_days), pools=pools)

    response = client.get(URL)

    assert response.status_code == 200
    assert response.get_json()["posted_line"] is None


def test_his_next_games_line_two_days_ahead(dependencies, client, engine):
    in_two_days = _event("0022600030", "2026-10-24T02:00:00+00:00", "LAL", "GSW")
    pools = Pools({"0022600030": {"prizepicks": {"PTS": 26.5}}})
    _serve(dependencies, engine, catalog=Catalog(in_two_days), pools=pools)

    assert client.get(URL).get_json()["posted_line"] == {
        "game_id": "0022600030",
        "game_date": "2026-10-23",
        "matchup": "LAL vs GSW",
        "lines": {"PTS": {"value": 26.5, "provider": "prizepicks"}},
        "provider_lines": {"prizepicks": {"PTS": 26.5}},
        "line_prices": {},
        "book_lines": {},
    }


def test_null_when_his_game_tomorrow_has_no_line_even_if_a_later_one_does(dependencies, client, engine):
    tomorrow = _event("0022600020", "2026-10-23T02:00:00+00:00", "GSW", "LAL")
    in_two_days = _event("0022600030", "2026-10-24T02:00:00+00:00", "LAL", "GSW")
    pools = Pools({"0022600030": {"prizepicks": {"PTS": 26.5}}})
    _serve(dependencies, engine, catalog=Catalog(tomorrow, in_two_days), pools=pools)

    assert client.get(URL).get_json()["posted_line"] is None


def test_a_postponed_game_today_is_not_his_next_game(dependencies, client, engine):
    postponed = {**TONIGHT, "status_text": "Postponed"}
    tomorrow = _event("0022600020", "2026-10-23T02:00:00+00:00", "GSW", "LAL")
    pools = Pools({"0022600020": {"prizepicks": {"PTS": 27.5}}})
    _serve(dependencies, engine, catalog=Catalog(postponed, tomorrow), pools=pools)

    assert client.get(URL).get_json()["posted_line"]["game_id"] == "0022600020"


def test_after_a_trade_his_team_is_the_one_of_his_latest_game_by_date(dependencies, client, engine):
    # Traded to GSW: his latest game by date is for GSW, but a rescheduled
    # LAL game carries the highest game id.
    frame = _frame()
    frame.loc[0, "GAME_ID"] = "0022500200"
    frame.loc[2, "TEAM_ID"] = GSW
    tonight = _event("0022600012", "2026-10-22T02:00:00+00:00", "GSW", "DEN")
    tomorrow = _event("0022600020", "2026-10-23T02:00:00+00:00", "DEN", "GSW")
    pools = Pools({"0022600020": {"prizepicks": {"PTS": 27.5}}}, team_id=GSW)
    _serve(dependencies, engine, catalog=Catalog(tonight, tomorrow), pools=pools, frame=frame, team_now=None)

    assert client.get(URL).get_json()["posted_line"] is None


def test_after_a_summer_trade_his_team_is_the_catalogs_for_the_new_season(dependencies, client, engine):
    # His logs are all LAL; the 2026-27 catalog has him on GSW. LAL plays
    # tomorrow; GSW's opener in three days has his line.
    lal_tomorrow = _event("0022600020", "2026-10-23T02:00:00+00:00", "LAL", "DEN")
    gsw_opener = _event("0022600040", "2026-10-25T02:00:00+00:00", "GSW", "DEN")
    pools = Pools({"0022600040": {"prizepicks": {"PTS": 22.5}}}, team_id=GSW)
    _serve(dependencies, engine, catalog=Catalog(lal_tomorrow, gsw_opener), pools=pools, team_now=GSW)

    assert client.get(URL).get_json()["posted_line"] == {
        "game_id": "0022600040",
        "game_date": "2026-10-24",
        "matchup": "GSW vs DEN",
        "lines": {"PTS": {"value": 22.5, "provider": "prizepicks"}},
        "provider_lines": {"prizepicks": {"PTS": 22.5}},
        "line_prices": {},
        "book_lines": {},
    }


def test_a_later_day_reads_only_his_teams_pool(dependencies, client, engine):
    reads = []

    class CountingPools(Pools):
        def get_pool_for_game(self, *, season, game_id):
            reads.append(game_id)
            return super().get_pool_for_game(season=season, game_id=game_id)

    others = [_event(f"00226001{n}", "2026-10-23T01:00:00+00:00", "GSW", "DEN") for n in range(10, 15)]
    his_game = _event("0022600030", "2026-10-24T02:00:00+00:00", "LAL", "GSW")
    pools = CountingPools({"0022600030": {"prizepicks": {"PTS": 26.5}}})
    _serve(dependencies, engine, catalog=Catalog(*others, his_game), pools=pools)

    assert client.get(URL).get_json()["posted_line"]["game_id"] == "0022600030"
    assert reads == ["0022600030"]


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
        "provider_lines": {"prizepicks": {"PTS": 29.5}},
        "line_prices": {},
        "book_lines": {},
    }


def test_a_named_past_day_with_no_pool_uses_the_stored_closing_line(dependencies, client, engine):
    _serve(dependencies, engine, catalog=Catalog(PAST), pools=Pools({}))

    assert client.get(URL + "&line_date=2025-11-04").get_json()["posted_line"] == {
        "game_id": "0022500101",
        "game_date": "2025-11-04",
        "matchup": "LAL @ GSW",
        "lines": {"PTS": {"value": 24.5, "provider": "draftkings"}},
        "provider_lines": {},
        "line_prices": {},
        "book_lines": {},
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
        "provider_lines": {"prizepicks": {"PTS": 27.5, "REB": 7.5}},
        "line_prices": {},
        "book_lines": {},
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


def test_a_line_today_reads_no_catalog(dependencies, client, engine):
    reads = []
    pools = Pools({"0022600012": {"prizepicks": {"PTS": 27.5}}})
    _serve(dependencies, engine, catalog=Catalog(TONIGHT), pools=pools, team_reads=reads)

    assert client.get(URL).get_json()["posted_line"]["game_id"] == "0022600012"
    assert reads == []


def test_a_failing_catalog_read_falls_back_to_his_latest_games_team(dependencies, client, engine):
    reads = []
    tomorrow = _event("0022600020", "2026-10-23T02:00:00+00:00", "GSW", "LAL")
    pools = Pools({"0022600020": {"prizepicks": {"PTS": 27.5}}})
    _serve(dependencies, engine, catalog=Catalog(tomorrow), pools=pools, team_now="fails", team_reads=reads)

    assert client.get(URL).get_json()["posted_line"]["game_id"] == "0022600020"
    assert reads == ["2026-27"]


def test_a_day_in_the_new_season_uses_the_new_seasons_team(dependencies, client, engine):
    # Asked on Sep 30 (2025-26): the 2026-27 catalog has him on GSW, whose
    # Oct 2 game has his line; LAL, his old team, plays Oct 1.
    lal_oct_1 = _event("0022600001", "2026-10-01T23:00:00+00:00", "LAL", "DEN")
    gsw_oct_2 = _event("0022600002", "2026-10-02T23:00:00+00:00", "GSW", "DEN")
    pools = Pools({"0022600002": {"prizepicks": {"PTS": 22.5}}}, team_id=GSW)
    _serve(
        dependencies,
        engine,
        catalog=Catalog(PAST, lal_oct_1, gsw_oct_2),
        pools=pools,
        team_now=GSW,
        now=datetime(2026, 9, 30, 16, 0, tzinfo=UTC),
    )

    assert client.get(URL).get_json()["posted_line"]["game_id"] == "0022600002"


def test_a_failing_catalog_read_still_stops_at_his_latest_games_teams_game(dependencies, client, engine):
    # The catalog is down; his logs say LAL, whose game tonight has no line.
    tomorrow = _event("0022600020", "2026-10-23T02:00:00+00:00", "GSW", "LAL")
    pools = Pools({"0022600020": {"prizepicks": {"PTS": 27.5}}})
    _serve(
        dependencies, engine, catalog=Catalog(TONIGHT, tomorrow), pools=pools, team_now="fails", team_reads=[]
    )

    assert client.get(URL).get_json()["posted_line"] is None


def test_the_look_ahead_resolves_each_days_season_once(dependencies, client, engine):
    counts = []

    class CountingCatalog(Catalog):
        def count_events(self, season):
            counts.append(season)
            return super().count_events(season)

    _serve(dependencies, engine, catalog=CountingCatalog(), pools=Pools({}))

    assert client.get(URL).get_json()["posted_line"] is None
    # Eight days (today and seven more), one season resolution each; an empty
    # catalog also tries the previous season.
    assert len(counts) == 16
