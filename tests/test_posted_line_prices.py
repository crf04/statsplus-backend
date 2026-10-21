"""``posted_line`` provider lines, prices and sportsbook lines (crf04/statsplus#265).

Called at the ``PostedLinesReader`` seam with a stub event catalog, player pool
and sportsbook reader; every expected value is a literal.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.services.player_pool import PlayerPool, PoolPlayer
from app.services.posted_lines import PostedLinesReader

LAL, GSW = 1610612747, 1610612744
PLAYER = 2544
GAME = "0022600012"
EVENT = {
    "nba_game_id": GAME,
    "scheduled_at": "2026-10-22T02:00:00+00:00",
    "classification": "",
    "home_team_id": LAL,
    "home_team_tricode": "LAL",
    "away_team_id": GSW,
    "away_team_tricode": "GSW",
}
DAY = date(2026, 10, 21)
SETTINGS = SimpleNamespace(nba=SimpleNamespace(model_fields_set={"current_season"}, current_season="2026-27"))
BOOK_ENTRY = {
    "as_of": "2026-10-21T21:04:11Z",
    "closing": False,
    "books": [
        {
            "book": "draftkings",
            "line": 27.5,
            "over": -115,
            "under": -105,
            "updated_at": "2026-10-21T21:02:40Z",
            "best_over": False,
            "best_under": True,
        }
    ],
}


class Catalog:
    def count_events(self, season):
        return 1

    def get_events_between(self, *args):
        return [EVENT]

    def get_events_by_ids(self, *args):
        return [EVENT]


def _pool(lines, line_prices=None):
    return PlayerPool(
        players=(
            PoolPlayer(PLAYER, "LeBron James", LAL, ("PTS",), {}, lines, line_prices=line_prices or {}),
        ),
        team_counts={LAL: 1},
        freshness={},
    )


class Pools:
    def __init__(self, live, retained=None):
        self.live, self.retained = live, retained

    def get_pool_for_game(self, *, season, game_id):
        return self.live

    def get_retained_pool_for_game(self, *, season, game_id, player_id=None, skip_providers=()):
        return self.retained


class Books:
    def __init__(self, by_player=None, fail=False):
        self.by_player, self.fail, self.calls = by_player or {}, fail, []

    def book_lines(self, game_id):
        self.calls.append(game_id)
        if self.fail:
            raise RuntimeError("book store down")
        return self.by_player


class Closing:
    def for_player(self, player_id, game_ids):
        return {GAME: SimpleNamespace(value=24.5, book="draftkings")}


def _read(pools, books=None, closing=None):
    return PostedLinesReader(
        event_catalog=Catalog(),
        player_pool=pools,
        settings=SETTINGS,
        closing_lines=closing,
        sportsbook_reader=books,
    ).for_player(PLAYER, DAY)


def test_a_live_pool_serves_every_provider_line_price_and_book_line():
    pool = _pool(
        {"prizepicks": {"PTS": 27.5}, "underdog": {"PTS": 27.5, "REB": 7.5}},
        {"underdog": {"PTS": {"over": -112, "under": -112}, "REB": {"over": None, "under": -120}}},
    )

    result = _read(Pools(pool), Books({PLAYER: {"PTS": BOOK_ENTRY}, 7: {"PTS": {}}}))

    assert result["lines"] == {
        "PTS": {"value": 27.5, "provider": "prizepicks"},
        "REB": {"value": 7.5, "provider": "underdog"},
    }
    assert result["provider_lines"] == {
        "prizepicks": {"PTS": 27.5},
        "underdog": {"PTS": 27.5, "REB": 7.5},
    }
    assert result["line_prices"] == {
        "underdog": {
            "PTS": {"over": -112, "under": -112},
            "REB": {"over": None, "under": -120},
        }
    }
    assert result["book_lines"] == {"PTS": BOOK_ENTRY}


def test_the_new_fields_are_present_and_empty_without_prices_or_a_book_store():
    result = _read(Pools(_pool({"prizepicks": {"PTS": 27.5}})))

    assert result["provider_lines"] == {"prizepicks": {"PTS": 27.5}}
    assert result["line_prices"] == {}
    assert result["book_lines"] == {}


def test_a_frozen_pool_is_supplemented_per_provider_with_lines_and_prices_together():
    frozen = _pool(
        {"dabble": {"PTS": 26.5}},
        {"dabble": {"PTS": {"over": -122, "under": -122}}},
    )
    retained = _pool(
        {"dabble": {"PTS": 99.5}, "underdog": {"PTS": 27.5}},
        {
            "dabble": {"PTS": {"over": -150, "under": 110}},
            "underdog": {"PTS": {"over": -112, "under": -112}},
        },
    )

    result = _read(Pools(frozen, retained))

    assert result["provider_lines"] == {"dabble": {"PTS": 26.5}, "underdog": {"PTS": 27.5}}
    assert result["line_prices"] == {
        "dabble": {"PTS": {"over": -122, "under": -122}},
        "underdog": {"PTS": {"over": -112, "under": -112}},
    }


def test_a_frozen_provider_without_prices_does_not_borrow_the_archives_prices():
    frozen = _pool({"dabble": {"PTS": 26.5}})
    retained = _pool({"dabble": {"PTS": 26.5}}, {"dabble": {"PTS": {"over": -150, "under": 110}}})

    assert _read(Pools(frozen, retained))["line_prices"] == {}


def test_the_closing_line_fallback_has_no_provider_lines_or_prices_but_reads_book_lines():
    books = Books({PLAYER: {"PTS": BOOK_ENTRY}})

    result = _read(Pools(None), books, Closing())

    assert result == {
        "game_id": GAME,
        "game_date": "2026-10-21",
        "matchup": "GSW @ LAL",
        "lines": {"PTS": {"value": 24.5, "provider": "draftkings"}},
        "provider_lines": {},
        "line_prices": {},
        "book_lines": {"PTS": BOOK_ENTRY},
    }
    assert books.calls == [GAME]


def test_a_book_store_failure_yields_empty_book_lines_and_keeps_the_posted_line(caplog):
    pool = _pool({"prizepicks": {"PTS": 27.5}})

    result = _read(Pools(pool), Books(fail=True))

    assert result["lines"] == {"PTS": {"value": 27.5, "provider": "prizepicks"}}
    assert result["book_lines"] == {}
    assert "Sportsbook book_lines read failed for game 0022600012" in caplog.text


def test_a_book_store_failure_keeps_the_closing_line_fallback(caplog):
    result = _read(Pools(None), Books(fail=True), Closing())

    assert result["lines"] == {"PTS": {"value": 24.5, "provider": "draftkings"}}
    assert result["book_lines"] == {}


def test_the_built_game_service_serves_stored_sportsbook_quotes_in_book_lines(monkeypatch):
    """Production wiring: ``build_dependencies`` hands the game-log reader the book store."""

    from datetime import UTC, datetime
    from unittest.mock import Mock

    from sqlalchemy import create_engine, insert

    from app.config.settings import FeatureSettings, RuntimeSettings
    from app.dependencies import build_dependencies
    from app.migrations import run_migrations
    from app.models.sportsbook_odds import SportsbookPropLatest

    engine = create_engine("sqlite:///:memory:")
    run_migrations(engine)
    monkeypatch.setattr("app.utils.db.get_engine", Mock(return_value=engine))
    monkeypatch.setattr("app.utils.cache_config.get_redis_client", Mock(return_value=None))
    seen = datetime(2026, 10, 21, 21, 2, 40, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(SportsbookPropLatest.__table__),
            dict(
                canonical_game_id=GAME,
                canonical_player_id=PLAYER,
                market_category="PTS",
                book="draftkings",
                poll_id=1,
                line=27.5,
                over_price=-115,
                under_price=-105,
                market_last_update=seen,
                observed_at=seen,
                is_closing=False,
            ),
        )
    settings = RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
        database={"url": "sqlite:///:memory:"},
        features=FeatureSettings(),
        odds_api={"enabled": True},
    )

    reader = build_dependencies(settings).game_service.posted_lines
    # Only the schedule and pool are stubbed; the book store is the built one.
    reader.event_catalog = Catalog()
    reader.player_pool = Pools(_pool({"prizepicks": {"PTS": 27.5}}))
    reader.settings = SETTINGS

    result = reader.for_player(PLAYER, DAY)

    assert result["book_lines"] == {
        "PTS": {
            "as_of": "2026-10-21T21:02:40Z",
            "closing": False,
            "books": [
                {
                    "book": "draftkings",
                    "line": 27.5,
                    "over": -115,
                    "under": -105,
                    "updated_at": "2026-10-21T21:02:40Z",
                    "best_over": False,
                    "best_under": False,
                }
            ],
        }
    }
