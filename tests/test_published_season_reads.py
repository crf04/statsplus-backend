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


@pytest.fixture
def preview_settings(monkeypatch):
    def use(settings):
        monkeypatch.setattr(preview_tests, "SETTINGS", settings)
    return use


def _preview(reader, season=None):
    seams = bt._two_games()
    return preview_tests._preview_service(
        logs=seams["logs"],
        diets=seams["diets"],
        matchups=preview_tests.SnapshotMatchups(res._matchup()),
        reader=reader,
        injuries=object(),
    ).preview(preview_tests.DRAFT, season=season)


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

    payload = _preview(reader, CALENDAR)

    assert set(reader.captures) == {CALENDAR}
    assert payload["season"] == CALENDAR
    assert reader.lookups == 0


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
    ("settings", "requested", "expected"),
    [(_settings(), None, PUBLISHED), (_settings(pinned=CALENDAR), CALENDAR, CALENDAR)],
)
def test_a_target_defender_is_validated_against_the_published_season(
    settings, requested, expected
):
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
        {"defender": {"player_id": 1628983, "minutes": 20, "comparator": "under"}},
        "OKC",
        requested,
    )

    assert conditions["defender"]["player_id"] == 1628983
    assert seasons == [expected]


def test_a_wrong_season_read_does_not_poison_the_publication_row_cache(tmp_path):
    """Reading a 2025-26 publication as 2026-27 must not cache empty rows."""

    from datetime import timedelta

    from sqlalchemy import create_engine

    from app.migrations import run_migrations
    from app.services.database_first_activation import DatabaseFirstPublicationReader
    from app.services.player_game_log_repository import PlayerGameLogRepository
    from app.services.statistic_catalog import StatisticCatalog
    from tests.services.test_database_first_activation import (
        _seed_player_game_log_publication,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'logs.sqlite3'}")
    run_migrations(engine)
    _seed_player_game_log_publication(engine)
    reader = DatabaseFirstPublicationReader(engine)
    logs = PlayerGameLogRepository(
        engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=CALENDAR,
        stats_surface_max_age=timedelta(hours=30),
    )

    def summaries(season):
        snapshot = reader.snapshot(
            ("player_game_logs",),
            season=PUBLISHED,
            projection_only_keys=frozenset({"player_game_logs"}),
        )
        return logs.get_player_summaries(season, (2544,), publication_snapshot=snapshot)

    wrong = summaries(CALENDAR).get(2544)
    assert wrong is None or wrong.rate_rows == ()
    right = summaries(PUBLISHED)[2544]
    assert right.season_rate.game_count == 1
    assert [row.game_id for row in right.rate_rows] == ["0022500001"]
    engine.dispose()


class AdvancingSeasonReader(PublishedReader):
    """The publication advances to 2026-27 after the request's first lookup."""

    def snapshot(self, stream_keys, *, season=None, **kwargs):
        if season is None:
            self.season = PUBLISHED if self.lookups == 0 else CALENDAR
        return super().snapshot(stream_keys, season=season, **kwargs)


def test_a_preview_reads_every_seam_in_its_captured_season(preview_settings):
    preview_settings(_settings())
    reader = AdvancingSeasonReader()

    payload = _preview(reader)

    assert reader.captures == [PUBLISHED]
    assert payload["season"] == PUBLISHED


def test_backtest_all_degrades_to_uncached_when_the_season_lookup_fails():
    from app.services.statistic_catalog import StatisticCatalog
    from app.services.target_backtest import TargetBacktestService

    class FailingReader:
        def snapshot(self, *args, **kwargs):
            raise RuntimeError("publication pointers unavailable")

        def generation(self, *args, **kwargs):
            raise RuntimeError("publication pointers unavailable")

    service = TargetBacktestService(
        targets=SimpleNamespace(list_targets=lambda uid: [{"id": 7}]),
        player_logs=object(),
        player_diets=None,
        statistic_catalog=StatisticCatalog.load_default(),
        settings=_settings(),
        publication_reader=FailingReader(),
    )

    body, cache_state = service.backtest_all("owner")

    # The default season, against the configured published one.
    assert body == {
        "season": PUBLISHED,
        "season_reason": "default",
        "published_season": CALENDAR,
        "backtests": [{"target_id": 7, "status": "uncached"}],
    }


# --- opening night: the 2026-27 schedule exists, logs still publish 2025-26 --

OPENING_GAME = "0022600001"
OPENING_DATE = "2026-10-21"
TRANSITION_15 = {
    "base": "play_types",
    "slice_key": "Transition",
    "comparator": "at_or_above",
    "threshold": 0.15,
}


class ScheduleCatalog:
    """One Event Catalog serving both the Slate and the Matchup, by season."""

    def __init__(self, events_by_season):
        self.events_by_season = events_by_season
        self.event_reads = []

    def count_events(self, season):
        return len(self.events_by_season.get(season, ()))

    def get_freshness(self, season, *, now=None):
        return {"last_success_at": "2026-10-21T10:00:00+00:00", "fresh": True}

    def get_events_between(self, season, starts_at, ends_at):
        from app.domain.utc import parse_utc_iso

        return [
            event
            for event in self.events_by_season.get(season, ())
            if starts_at <= parse_utc_iso(event["scheduled_at"]) < ends_at
        ]

    def get_event(self, season, game_id):
        self.event_reads.append(season)
        return next(
            (e for e in self.events_by_season.get(season, ()) if e["nba_game_id"] == game_id),
            None,
        )

    def latest_final_scheduled_at(self, season):
        return None


def _opening_night(settings, reader, *, evidence_season, monkeypatch):
    """Real Slate, Matchup and Target resolution over season-keyed doubles.

    The Diet, log and Defense Sheet doubles serve, and assert, only
    ``evidence_season``; the schedule holds the opener only in 2026-27.
    """

    from datetime import datetime, timezone

    from app.services.matchup import MatchupService
    from app.services.slate_service import SlateService
    from app.services.stats_freshness_repository import StatsFreshness
    import tests.services.test_matchup_service as doubles

    monkeypatch.setattr(doubles, "SEASON", evidence_season)
    opener = {
        **doubles._event(),
        "nba_game_id": OPENING_GAME,
        "season": "2026-27",
        "scheduled_at": "2026-10-21T23:30:00+00:00",
    }
    last_april = {
        **doubles._event(),
        "nba_game_id": "0022501190",
        "scheduled_at": "2026-04-10T23:30:00+00:00",
    }
    catalog = ScheduleCatalog({"2025-26": [last_april], "2026-27": [opener]})
    now = datetime(2026, 10, 21, 16, tzinfo=timezone.utc)
    pool = doubles.RecordedPool(doubles._service().player_pool.pool)
    matchups = MatchupService(
        event_catalog=catalog,
        player_pool=pool,
        player_logs=doubles.RecordedLogs(),
        player_diets=doubles.RecordedDiets(),
        team_matchups=doubles.RecordedTeamWindows(
            doubles._window(), doubles._window(last_15=True)
        ),
        stats_freshness=SimpleNamespace(get=lambda: StatsFreshness(doubles.RETRIEVED_AT)),
        injuries=None,
        settings=settings,
        publication_reader=reader,
        clock=lambda: now,
    )
    slates = SlateService(catalog, settings=settings, clock=lambda: now)
    targets = SimpleNamespace(list_targets=lambda uid: [{
        "id": 1, "opponent": "BOS", "title": "BOS transition", "note": None,
        "qualifiers": [TRANSITION_15], "conditions": None,
    }])
    resolution = TargetResolutionService(
        targets=targets,
        slates=slates,
        matchups=matchups,
        publication_reader=reader,
        injuries=res._StoredNoInjuries(),
        settings=settings,
    )
    return SimpleNamespace(
        slates=slates, matchups=matchups, resolution=resolution,
        catalog=catalog, pool=pool,
    )


def test_opening_night_slate_matchup_and_targets_read_across_both_seasons(monkeypatch):
    night = _opening_night(
        _settings(), PublishedReader(), evidence_season=PUBLISHED, monkeypatch=monkeypatch
    )

    slate = night.slates.get_slate(OPENING_DATE)
    assert [game["game_id"] for game in slate["games"]] == [OPENING_GAME]

    matchup = night.matchups.get_matchup(game_id=OPENING_GAME)
    assert matchup["game"]["game_id"] == OPENING_GAME
    # Last season's published evidence scores the opener's players.
    assert [player["canonical_id"] for player in matchup["players"]] == [2544]
    assert matchup["players"][0]["season_scoring"] == 25.4
    assert night.catalog.event_reads == ["2026-27"]
    assert night.pool.calls == [("2026-27", OPENING_GAME)]

    resolved = night.resolution.resolve("owner", requested_date=OPENING_DATE)
    [target] = resolved["targets"]
    assert target["game"]["game_id"] == OPENING_GAME
    assert [(p["canonical_id"], p["shares"][0]["share"]) for p in target["players"]] == [
        (2544, 0.19)
    ]


def test_a_pinned_opening_night_reads_only_the_pin(monkeypatch):
    night = _opening_night(
        _settings(pinned=CALENDAR), PublishedReader(),
        evidence_season=CALENDAR, monkeypatch=monkeypatch,
    )

    resolved = night.resolution.resolve("owner", requested_date=OPENING_DATE)

    [target] = resolved["targets"]
    assert [(p["canonical_id"], p["shares"][0]["share"]) for p in target["players"]] == [
        (2544, 0.19)
    ]
    assert night.catalog.event_reads == [CALENDAR]


def test_a_resolve_composes_every_matchup_in_its_captured_season(monkeypatch):
    """The publication advancing mid-request must not split the evidence."""

    night = _opening_night(
        _settings(), AdvancingSeasonReader(),
        evidence_season=PUBLISHED, monkeypatch=monkeypatch,
    )

    resolved = night.resolution.resolve("owner", requested_date=OPENING_DATE)

    [target] = resolved["targets"]
    assert [(p["canonical_id"], p["shares"][0]["share"]) for p in target["players"]] == [
        (2544, 0.19)
    ]


def test_a_past_slate_game_still_reads_after_the_publication_advances(monkeypatch):
    night = _opening_night(
        _settings(), PublishedReader(season=CALENDAR),
        evidence_season=CALENDAR, monkeypatch=monkeypatch,
    )

    slate = night.slates.get_slate("2026-04-10")
    assert [game["game_id"] for game in slate["games"]] == ["0022501190"]
    matchup = night.matchups.get_matchup(game_id="0022501190")
    assert matchup["game"]["game_id"] == "0022501190"
    assert night.catalog.event_reads == [PUBLISHED]


@pytest.mark.parametrize(
    ("settings", "games"),
    [(_settings(), 1), (_settings(pinned=CALENDAR), 2), (_settings(pinned=PUBLISHED), 1)],
)
def test_game_logs_without_a_season_read_the_published_season(
    settings, games, dependencies, client, mock_db_engine
):
    """The route's default reaches the real GameService's log read."""

    from app.services.game_service import GameService
    from tests.test_game_logs import _game_logs_frame

    class Logs:
        """2025-26 holds one game for the player, 2026-27 two."""

        def get_player_logs(self, player_id, season, **kwargs):
            frame = _game_logs_frame()
            return frame.head({PUBLISHED: 1, CALENDAR: 2}.get(season, 0)).copy()

    class Catalog:
        def get_catalog(self, season, *, active_only=False):
            return [dict(player_id=2544, display_name="LeBron James",
                         is_active_for_season=True, team_id=1610612747)]

    dependencies.game_service = GameService(
        mock_db_engine,
        redis_client=SimpleNamespace(),
        settings=settings,
        athlete_catalog=Catalog(),
        game_logs_source=Logs(),
        publication_reader=PublishedReader(),
    )

    response = client.get("/api/games/game_logs?player_name=LeBron+James")

    assert response.status_code == 200
    assert len(response.get_json()["game_logs"]) == games


def test_a_pinned_matchup_reads_its_event_in_the_pinned_season(monkeypatch):
    """A pin keeps today's lookup even for a game ID naming another season."""

    from app.errors import ResourceNotFoundError

    night = _opening_night(
        _settings(pinned=PUBLISHED), PublishedReader(),
        evidence_season=PUBLISHED, monkeypatch=monkeypatch,
    )

    with pytest.raises(ResourceNotFoundError):
        night.matchups.get_matchup(game_id=OPENING_GAME)
    assert night.catalog.event_reads == [PUBLISHED]


@pytest.mark.parametrize(
    ("settings", "requested", "season"),
    [(_settings(), None, PUBLISHED), (_settings(pinned=CALENDAR), CALENDAR, CALENDAR)],
)
def test_a_saved_backtest_reads_the_published_season(settings, requested, season):
    from app.services.statistic_catalog import StatisticCatalog
    from app.services.target_backtest import TargetBacktestService

    seams = bt._two_games()
    reader = PublishedReader()
    service = TargetBacktestService(
        targets=SimpleNamespace(
            get_target=lambda uid, target_id: {**preview_tests.DRAFT, "id": target_id}
        ),
        player_logs=seams["logs"],
        player_diets=seams["diets"],
        statistic_catalog=StatisticCatalog.load_default(),
        settings=settings,
        publication_reader=reader,
    )

    body, _ = service.backtest("owner", 3, season=requested)

    # Every capture is in that season, never the calendar's or another.
    assert set(reader.captures) == {season}
    assert body["season"] == season


def test_an_explicit_game_log_season_never_discovers_the_default(dependencies, client):
    dependencies.game_service.default_season.side_effect = RuntimeError(
        "publication pointers unavailable"
    )
    dependencies.game_service.get_filtered_logs.return_value = {
        "game_logs": [], "averages": [], "season_averages": [], "next_game": None,
    }

    explicit = client.get("/api/games/game_logs?player_name=LeBron+James&season_filter=2024-25")
    malformed = client.get(
        "/api/games/game_logs?player_name=LeBron+James&season_filter=2024-25&minutes_filter=abc"
    )

    assert explicit.status_code == 200
    assert dependencies.game_service.get_filtered_logs.call_args.args[1].season_filter == "2024-25"
    assert malformed.status_code == 400
    dependencies.game_service.default_season.assert_not_called()


def test_season_minutes_stay_consistent_when_a_season_activates_mid_request(tmp_path):
    """Activation between season discovery and capture must not empty the read."""

    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine

    from app.migrations import run_migrations
    from app.services.collection_control import PublicationService
    from app.services.database_first_activation import DatabaseFirstPublicationReader
    from app.services.player_game_log_repository import PlayerGameLogRepository
    from app.services.statistic_catalog import StatisticCatalog
    from app.services.target_season_minutes import TargetSeasonMinutesService
    from tests.services.test_matchup_selection_service import BOS, LAL, _log_row

    def _game(game_id, game_date, laker, season):
        """One LAL player and one BOS opponent in one game."""

        lal = _log_row(player_id=laker, game_id=game_id, game_date=game_date,
                       points=20, minutes=30.0)
        bos = {**_log_row(player_id=1628369, game_id=game_id, game_date=game_date,
                          points=18, minutes=33.0, opponent_team_id=LAL),
               "team_id": BOS, "team_tricode": "BOS", "opponent_team_tricode": "LAL"}
        return [{**lal, "season": season}, {**bos, "season": season}]

    engine = create_engine(f"sqlite:///{tmp_path / 'race.sqlite3'}")
    run_migrations(engine)
    at = datetime(2026, 9, 30, tzinfo=timezone.utc)
    publisher = PublicationService(engine, clock=lambda: at)
    publisher.register_stream(
        "player_game_logs", provider="ledger", owner="railway",
        required_observations=(), publication_strategy="replace", enabled=True,
        freshness_rule="cutoff_current",
    )
    first = publisher.compose(
        "player_game_logs", season=PUBLISHED, cutoff=at,
        payload={"rows": _game("0022500001", "2026-01-02", 2544, PUBLISHED)},
    )

    class ActivatesAfterFirstRead:
        """The real reader; the next season activates right after its first capture."""

        def __init__(self, reader):
            self._reader = reader
            self.activated = False

        def __getattr__(self, name):
            return getattr(self._reader, name)

        def snapshot(self, *args, **kwargs):
            captured = self._reader.snapshot(*args, **kwargs)
            if not self.activated:
                self.activated = True
                later = at + timedelta(days=22)
                PublicationService(engine, clock=lambda: later).compose(
                    "player_game_logs", season=CALENDAR, cutoff=later,
                    payload={"rows": _game("0022600001", "2026-10-21", 1641705, CALENDAR)},
                    expected_fence=first.fence,
                )
            return captured

    reader = DatabaseFirstPublicationReader(engine)
    logs = PlayerGameLogRepository(
        engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=CALENDAR,
        stats_surface_max_age=timedelta(hours=30),
        publication_reader=reader,
    )
    racing = ActivatesAfterFirstRead(reader)
    service = TargetSeasonMinutesService(
        player_logs=logs, settings=_settings(), publication_reader=racing
    )

    during = service.get("LAL")
    after = service.get("LAL")
    requested = service.get("LAL", season=CALENDAR)

    assert racing.activated
    # The default stays 2025-26 across the activation, read live then retained.
    assert (during["season"], [p["player_id"] for p in during["players"]]) == (PUBLISHED, [2544])
    assert (after["season"], [p["player_id"] for p in after["players"]]) == (PUBLISHED, [2544])
    assert after["published_season"] == CALENDAR
    assert (requested["season"], [p["player_id"] for p in requested["players"]]) == (
        CALENDAR, [1641705]
    )
    engine.dispose()


def test_a_published_season_capture_reads_in_its_own_pointer_season(tmp_path):
    from sqlalchemy import create_engine

    from app.migrations import run_migrations
    from app.services.database_first_activation import (
        DatabaseFirstPublicationReader,
        PublishedSeason,
    )
    from tests.services.test_database_first_activation import (
        _seed_player_game_log_publication,
    )

    empty = create_engine(f"sqlite:///{tmp_path / 'empty.sqlite3'}")
    run_migrations(empty)
    unpublished = DatabaseFirstPublicationReader(empty).snapshot(
        ("player_game_logs",), season=PublishedSeason(CALENDAR)
    )
    assert unpublished.season == CALENDAR

    engine = create_engine(f"sqlite:///{tmp_path / 'published.sqlite3'}")
    run_migrations(engine)
    _seed_player_game_log_publication(engine)
    # A capture that does not name the player-log stream still follows it.
    captured = DatabaseFirstPublicationReader(engine).snapshot(
        ("player_diet_play_types",), season=PublishedSeason(CALENDAR)
    )
    assert captured.season == PUBLISHED
    assert captured.read("player_game_logs").available
    empty.dispose()
    engine.dispose()
