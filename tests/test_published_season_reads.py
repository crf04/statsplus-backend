"""Published reads follow the published season across the October rollover.

On 2026-10-01 the calendar default became 2026-27 while every published
stream still held 2025-26. An unpinned read must use the active
player-game-log publication's season; an explicit ``NBA_CURRENT_SEASON`` pin
must keep winning, exactly as research_season does for Search (#319).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config.settings import (
    MatchupScoreSettings,
    NBASeasonSettings,
    RuntimeSettings,
)
from app.services.target_resolution import TargetResolutionService
from tests import test_target_backtest as bt
from tests import test_target_preview as preview_tests
from tests import test_target_resolution as res


PUBLISHED = "2025-26"
CALENDAR = "2026-27"


def _settings(*, pinned: str | None = None) -> RuntimeSettings:
    nba = (
        NBASeasonSettings(current_season=pinned)
        if pinned is not None
        # The calendar default on 2026-10-01, without depending on today.
        else NBASeasonSettings.model_construct(_fields_set=set(), current_season=CALENDAR)
    )
    return RuntimeSettings(
        environment="testing", nba=nba, matchup_scores=MatchupScoreSettings()
    )


class PublishedReader:
    """A publication reader whose active game-log publication is 2025-26.

    A capture without a season is the season lookup; every other capture is a
    request's generation and records the season it asked for.
    """

    def __init__(self, season=PUBLISHED):
        self.season = season
        self.lookups = 0
        self.captures = []

    def snapshot(self, stream_keys, *, season=None, projection_only_keys=None,
                 decoded_only_keys=None):
        if season is None:
            self.lookups += 1
            return SimpleNamespace(read=lambda key: SimpleNamespace(
                available=True, season=self.season,
                publication_id="publication-1", fence=1, version=1,
            ))
        self.captures.append(season)
        return res._FakeSnapshot(f"generation-{len(self.captures)}")


def _resolution(reader, settings):
    return TargetResolutionService(
        targets=SimpleNamespace(list_targets=lambda uid: []),
        slates=res.FakeSlate(),
        matchups=res._RecordedSnapshotMatchups({}),
        publication_reader=reader,
        injuries=object(),
        settings=settings,
    )


def test_an_unpinned_resolve_captures_the_published_season():
    reader = PublishedReader()

    _resolution(reader, _settings()).resolve("owner", requested_date=res.SLATE_DATE)

    assert reader.captures == [PUBLISHED]


def test_a_pinned_resolve_captures_the_pinned_season():
    reader = PublishedReader()

    _resolution(reader, _settings(pinned=CALENDAR)).resolve(
        "owner", requested_date=res.SLATE_DATE
    )

    assert reader.captures == [CALENDAR]
    assert reader.lookups == 0


@pytest.fixture
def preview_settings(monkeypatch):
    def use(settings):
        monkeypatch.setattr(preview_tests, "SETTINGS", settings)
    return use


def _preview(reader):
    seams = bt._two_games()
    return preview_tests._preview_service(
        logs=seams["logs"],
        diets=seams["diets"],
        matchups=preview_tests.SnapshotMatchups(res._matchup()),
        reader=reader,
        injuries=object(),
    ).preview(preview_tests.DRAFT)


def test_an_unpinned_preview_reads_the_published_season(preview_settings):
    preview_settings(_settings())
    reader = PublishedReader()

    payload = _preview(reader)

    assert reader.captures == [PUBLISHED]
    assert payload["season"] == PUBLISHED
    assert payload["summary"]["games"] == 2


def test_a_pinned_preview_reads_the_pinned_season(preview_settings):
    preview_settings(_settings(pinned=CALENDAR))
    reader = PublishedReader()

    payload = _preview(reader)

    assert reader.captures == [CALENDAR]
    assert payload["season"] == CALENDAR
    assert reader.lookups == 0


def _game_service(reader, settings, monkeypatch):
    from app.services import game_service as game_service_module

    monkeypatch.setattr(game_service_module, "get_redis_client", lambda *a, **k: None)
    return game_service_module.GameService(
        object(), settings=settings, publication_reader=reader
    )


def test_unpinned_game_logs_default_to_the_published_season(monkeypatch):
    service = _game_service(PublishedReader(), _settings(), monkeypatch)

    assert service.default_season() == PUBLISHED


def test_pinned_game_logs_default_to_the_pinned_season(monkeypatch):
    reader = PublishedReader()
    service = _game_service(reader, _settings(pinned=CALENDAR), monkeypatch)

    assert service.default_season() == CALENDAR
    assert reader.lookups == 0


def test_game_logs_without_a_publication_default_to_settings(monkeypatch):
    service = _game_service(None, _settings(), monkeypatch)

    assert service.default_season() == CALENDAR


class SeasonEvents:
    def __init__(self, events_by_season):
        self.events_by_season = events_by_season

    def count_events(self, season):
        return len(self.events_by_season.get(season, ()))

    def get_events(self, season):
        return list(self.events_by_season.get(season, ()))


def _next_opponent(settings, events, now):
    from datetime import datetime, timezone

    from app.services.next_opponent import NextOpponentService

    seasons = []
    game = SimpleNamespace(
        settings=settings,
        default_season=lambda: PUBLISHED,
        get_player_id=lambda name, season: seasons.append(season) or 1,
        athlete_catalog=SimpleNamespace(
            get_catalog=lambda season, active_only=False: [
                {"player_id": 1, "team_id": 1610612738}
            ]
        ),
    )
    service = NextOpponentService(
        game,
        SeasonEvents(events),
        SimpleNamespace(publication_reader=None),
        clock=lambda: datetime(*now, tzinfo=timezone.utc),
    )
    return service.get_next_opponent("Jaylen Brown"), seasons


def _scheduled(game_id, at):
    return {
        "nba_game_id": game_id,
        "scheduled_at": at,
        "status_code": 1,
        "home_team": {"id": 1610612747, "tricode": "LAL", "name": "Los Angeles Lakers"},
        "away_team": {"id": 1610612738, "tricode": "BOS", "name": "Boston Celtics"},
    }


def test_the_next_opponent_reads_the_new_schedule_for_a_published_player():
    events = {
        "2025-26": [_scheduled("0022501200", "2026-04-12T23:00:00+00:00")],
        "2026-27": [_scheduled("0022600010", "2026-10-22T23:30:00+00:00")],
    }

    body, seasons = _next_opponent(_settings(), events, (2026, 10, 21, 16))

    assert body["next_game"]["game_id"] == "0022600010"
    assert seasons == [PUBLISHED]


def test_the_next_opponent_is_empty_before_the_new_schedule_is_collected():
    events = {"2025-26": [_scheduled("0022501200", "2026-04-12T23:00:00+00:00")]}

    body, _ = _next_opponent(_settings(), events, (2026, 10, 1, 16))

    assert body == {"next_game": None, "opponent_ranks": []}


def test_a_pinned_next_opponent_reads_the_pinned_schedule():
    events = {
        "2025-26": [_scheduled("0022501200", "2026-04-12T23:00:00+00:00")],
        "2026-27": [_scheduled("0022600010", "2026-10-22T23:30:00+00:00")],
    }

    # The pinned schedule has no game left, though 2026-27's does.
    body, _ = _next_opponent(_settings(pinned="2025-26"), events, (2026, 10, 21, 16))

    assert body["next_game"] is None


@pytest.mark.parametrize(
    ("settings", "expected"),
    [(_settings(), PUBLISHED), (_settings(pinned=CALENDAR), CALENDAR)],
)
def test_a_target_defender_is_validated_against_the_published_season(settings, expected):
    from app.services.user_service import UserService

    seasons = []

    class Logs:
        def list_player_rows(self, season, player_id):
            seasons.append(season)
            return [SimpleNamespace(team_id=1610612760, season_type="Regular Season")]

    service = UserService(
        object(), settings=settings, player_logs=Logs(), publication_reader=PublishedReader()
    )

    conditions = service._validated_conditions(
        {"defender": {"player_id": 1628983, "minutes": 20, "comparator": "under"}}, "OKC"
    )

    assert conditions["defender"]["player_id"] == 1628983
    assert seasons == [expected]
