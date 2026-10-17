"""``book_lines`` on GET /api/games/matchup (#377, crf04/statsplus#185 section 4)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.config.settings import OddsApiSettings
from app.dependencies import build_dependencies
from app.providers.dfs import NBAMarketQuery
from app.services.sportsbook_quotes import (
    ResolvedQuote,
    SportsbookQuoteReader,
    SportsbookQuoteStore,
)
from tests.routes import test_matchup_fixtures as route_fixtures
from tests.routes.test_matchup_fixtures import (
    GAME_ID,
    NOW,
    SEASON,
    _mark_event_final,
    _publish_focal_participants,
    _recorded_projection_snapshot,
)

# The route world is a fixture of the persisted-fixture module; reuse it as is.
projection_route_context = route_fixtures.projection_route_context

STAMP = datetime(2026, 1, 15, 11, 58, 20, tzinfo=UTC)


def _quote(book, line, over, under, *, category="PTS", stamp=STAMP, player=2544):
    return ResolvedQuote(player, category, book, line, over, under, stamp)


def _seed(context, quotes, *, closing=False):
    SportsbookQuoteStore(context.engine).record_poll(
        odds_event_id="evt-1",
        game_id=GAME_ID,
        started_at=NOW,
        completed_at=NOW,
        cost=5,
        remaining=19995,
        is_closing=closing,
        quotes=quotes,
    )
    service = context.client.application.extensions["dependencies"].matchup_service
    service.sportsbook_reader = SportsbookQuoteReader(context.engine)


def _lebron(response):
    payload = response.get_json()
    (row,) = [p for p in payload["players"] if p["canonical_id"] == 2544]
    return payload, row


def _post_dfs_line(context):
    context.assembled.projection_recorder.record_complete_snapshot(
        _recorded_projection_snapshot(context.catalog),
        query=NBAMarketQuery(season=SEASON),
        accepted_at=NOW,
    )


def test_current_player_rows_carry_book_lines_with_best_price_flags(projection_route_context):
    context = projection_route_context
    _post_dfs_line(context)
    _seed(
        context,
        [
            _quote("draftkings", 27.5, -115, -105),
            _quote("fanduel", 27.5, -110, -110),
            _quote("betmgm", 28.5, 100, -125),
            _quote("draftkings", 7.5, -105, -115, category="REB"),
        ],
    )

    response = context.client.get(f"/api/games/matchup?game_id={GAME_ID}")

    assert response.status_code == 200
    _, row = _lebron(response)
    assert row["book_lines"] == {
        "PTS": {
            "as_of": "2026-01-15T11:58:20Z",
            "closing": False,
            "books": [
                {
                    "book": "draftkings",
                    "line": 27.5,
                    "over": -115,
                    "under": -105,
                    "updated_at": "2026-01-15T11:58:20Z",
                    "best_over": False,
                    "best_under": True,
                },
                {
                    "book": "fanduel",
                    "line": 27.5,
                    "over": -110,
                    "under": -110,
                    "updated_at": "2026-01-15T11:58:20Z",
                    "best_over": True,
                    "best_under": False,
                },
                {
                    "book": "betmgm",
                    "line": 28.5,
                    "over": 100,
                    "under": -125,
                    "updated_at": "2026-01-15T11:58:20Z",
                    "best_over": False,
                    "best_under": False,
                },
            ],
        },
        "REB": {
            "as_of": "2026-01-15T11:58:20Z",
            "closing": False,
            "books": [
                {
                    "book": "draftkings",
                    "line": 7.5,
                    "over": -105,
                    "under": -115,
                    "updated_at": "2026-01-15T11:58:20Z",
                    "best_over": False,
                    "best_under": False,
                },
            ],
        },
    }


def test_books_never_enter_provenance_or_posted_markets(projection_route_context):
    context = projection_route_context
    _post_dfs_line(context)
    before = _lebron(context.client.get(f"/api/games/matchup?game_id={GAME_ID}"))[1]
    _seed(
        context, [_quote("draftkings", 27.5, -115, -105), _quote("fanduel", 3.5, -110, -110, category="3PM")]
    )

    after = _lebron(context.client.get(f"/api/games/matchup?game_id={GAME_ID}"))[1]

    assert before["book_lines"] == {}
    for field in ("provenance", "posted_markets", "stat_categories", "scores"):
        assert after[field] == before[field]
    assert after["provenance"] == {"dabble": ["PTS"]}
    assert set(after["book_lines"]) == {"PTS", "3PM"}  # 3PM: books only, not targetable


def test_a_game_with_no_quotes_returns_empty_book_lines_on_every_row(projection_route_context):
    context = projection_route_context
    _post_dfs_line(context)

    payload, row = _lebron(context.client.get(f"/api/games/matchup?game_id={GAME_ID}"))

    assert row["book_lines"] == {}
    assert all(player["book_lines"] == {} for player in payload["players"])


def test_a_closing_snapshot_is_flagged_closing_after_tip(projection_route_context):
    context = projection_route_context
    _post_dfs_line(context)
    _seed(
        context, [_quote("draftkings", 27.5, -115, -105), _quote("fanduel", 27.5, -110, -110)], closing=True
    )

    _, row = _lebron(context.client.get(f"/api/games/matchup?game_id={GAME_ID}"))

    assert row["book_lines"]["PTS"]["closing"] is True


def test_a_historical_matchup_has_empty_book_lines_even_when_quotes_exist(projection_route_context):
    context = projection_route_context
    participants = (route_fixtures.AWAY_PARTICIPANT, route_fixtures.HOME_PARTICIPANT)
    _seed(context, [_quote("draftkings", 27.5, -115, -105, player=p) for p in participants])
    _mark_event_final(context.engine)
    _publish_focal_participants(context.player_logs)

    response = context.client.get(f"/api/games/matchup?game_id={GAME_ID}")

    payload = response.get_json()
    assert payload["experience"]["mode"] == "historical"
    assert {p["canonical_id"] for p in payload["players"]} >= set(participants)
    assert all(p["book_lines"] == {} for p in payload["players"])


def test_a_store_failure_never_fails_the_matchup(projection_route_context, caplog):
    context = projection_route_context
    _post_dfs_line(context)

    class Broken:
        def book_lines(self, game_id, *, connection=None):
            raise RuntimeError("sportsbook store unavailable")

    service = context.client.application.extensions["dependencies"].matchup_service
    service.sportsbook_reader = Broken()

    response = context.client.get(f"/api/games/matchup?game_id={GAME_ID}")

    assert response.status_code == 200
    assert _lebron(response)[1]["book_lines"] == {}
    assert "Sportsbook book_lines read failed" in caplog.text


def test_the_sportsbook_reader_is_wired_only_when_odds_api_is_enabled(projection_route_context):
    context = projection_route_context
    assert context.settings.odds_api.enabled is False

    disabled = context.assembled.matchup_service.sportsbook_reader
    enabled_settings = context.settings.model_copy(
        update={"odds_api": OddsApiSettings(enabled=True)}  # no key: the web needs none
    )
    enabled = build_dependencies(enabled_settings).matchup_service.sportsbook_reader

    assert disabled is None
    assert isinstance(enabled, SportsbookQuoteReader)
