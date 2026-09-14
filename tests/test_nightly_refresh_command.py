"""Offline behavior of the Railway Nightly Refresh process command."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine, delete, inspect, text, update

from app.migrations import run_migrations
from app.models.collection_control import PublicationStream
from app.services.collection_control import ControlPlaneError, PublicationService
from app.services.stats_freshness_repository import (
    PLAYER_GAME_LOG_SURFACE,
    StatsFreshnessRepository,
)
from scripts import nightly_refresh
from scripts.nightly_refresh import run_hosted_refresh, run_nightly_refresh

#: The four replacement streams hosted metadata requires before it may run.
HOSTED_METADATA_STREAMS = (
    "player_per36",
    "exact_shot_zones_opponent_season",
    "exact_shot_zones",
    "assist_locations_season",
)


def test_fresh_database_bootstrap_allows_inactive_and_fences_enabled_stream(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh-nightly.sqlite3'}")
    run_migrations(engine)

    fence = nightly_refresh._bootstrap_legacy_write_fence(engine)
    fence.assert_writable("player_per36")

    with engine.begin() as connection:
        connection.execute(update(PublicationStream).where(
            PublicationStream.stream_key == "player_per36"
        ).values(enabled=True))
    with pytest.raises(ControlPlaneError, match="legacy_write_fenced"):
        fence.assert_writable("player_per36")


def _hosted_settings():
    return SimpleNamespace(
        nba=SimpleNamespace(current_season="2025-26"),
        catalog=SimpleNamespace(
            player_game_log_max_age_hours=30,
            player_game_log_min_active_players_per_team_game=5,
            player_game_log_reconciliation_days=3,
        ),
    )


def _hosted_database(
    tmp_path,
    *,
    activated=HOSTED_METADATA_STREAMS,
    absent=(),
    unreadable=False,
):
    """Build a real migrated database with the requested activation state."""

    database_url = f"sqlite:///{tmp_path / 'nightly.sqlite3'}"
    engine = create_engine(database_url)
    run_migrations(engine)
    PublicationService(engine).register_default_streams()
    with engine.begin() as connection:
        connection.execute(
            update(PublicationStream)
            .where(PublicationStream.stream_key.in_(activated))
            .values(enabled=True)
        )
        if absent:
            connection.execute(
                delete(PublicationStream).where(
                    PublicationStream.stream_key.in_(absent)
                )
            )
        if unreadable:
            connection.execute(
                text(
                    "ALTER TABLE publication_streams "
                    "RENAME TO publication_streams_unreadable"
                )
            )
    engine.dispose()
    return database_url


def _install_hosted_boundaries(monkeypatch, calls, *, game_log_result="ok"):
    """Inject the provider boundaries the hosted command must not call."""

    settings = _hosted_settings()
    pbp_log_provider = object()
    player_log_repository = object()

    def refuse_adapter(**kwargs):
        raise AssertionError("hosted refresh constructed an upstream adapter")

    monkeypatch.setattr(nightly_refresh, "load_settings", lambda **kwargs: settings)
    monkeypatch.setattr(nightly_refresh, "NBAStatsAdapter", refuse_adapter)
    monkeypatch.setattr(nightly_refresh, "PBPStatsAdapter", refuse_adapter)
    monkeypatch.setattr(
        nightly_refresh, "PBPGameLogAdapter", lambda **kwargs: pbp_log_provider
    )
    monkeypatch.setattr(
        nightly_refresh, "EventCatalogService", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        nightly_refresh, "AthleteCatalogService", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        nightly_refresh,
        "PlayerGameLogRepository",
        lambda *args, **kwargs: player_log_repository,
    )
    monkeypatch.setattr(
        nightly_refresh,
        "StatisticCatalog",
        SimpleNamespace(load_default=lambda: object()),
    )

    def build_ingest_service(**kwargs):
        def refresh(season):
            calls.append(("player_game_logs", season))
            if isinstance(game_log_result, Exception):
                raise game_log_result
            return game_log_result

        return SimpleNamespace(refresh=refresh)

    monkeypatch.setattr(
        nightly_refresh, "PlayerGameLogIngestService", build_ingest_service
    )


def _record_active_players(monkeypatch, collection):
    """Observe the offline player-list collection the metadata step performs."""

    from app.services import data_service as data_service_module

    def get_active_players():
        collection.append("player_information")
        return [{"id": 2544, "full_name": "LeBron James"}]

    monkeypatch.setattr(
        data_service_module.players, "get_active_players", get_active_players
    )


def test_hosted_refresh_publishes_activation_gated_metadata_and_game_logs(
    tmp_path, monkeypatch
):
    database_url = _hosted_database(tmp_path)
    calls = []
    _install_hosted_boundaries(monkeypatch, calls)
    collection = []
    _record_active_players(monkeypatch, collection)

    assert nightly_refresh._run(database_url, hosted_only=True) == 0

    # The game-log step still ran, and the metadata step collected only the
    # offline player list: every other frame was refused by activation.
    assert calls == [("player_game_logs", "2025-26")]
    assert collection == ["player_information"]

    engine = create_engine(database_url)
    try:
        assert pd.read_sql(
            "SELECT full_name FROM player_information", engine
        )["full_name"].tolist() == ["LeBron James"]
        assert (
            StatsFreshnessRepository(engine).get().last_successful_completion
            is not None
        )
        # Game-log completion is separate evidence and untouched here.
        assert (
            StatsFreshnessRepository(
                engine, surface=PLAYER_GAME_LOG_SURFACE
            ).get().last_successful_completion
            is None
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize("disabled_stream", HOSTED_METADATA_STREAMS)
def test_hosted_metadata_fails_closed_when_a_required_stream_is_disabled(
    tmp_path, monkeypatch, capsys, disabled_stream
):
    activated = tuple(
        stream for stream in HOSTED_METADATA_STREAMS if stream != disabled_stream
    )
    database_url = _hosted_database(tmp_path, activated=activated)
    calls = []
    _install_hosted_boundaries(monkeypatch, calls)
    collection = []
    _record_active_players(monkeypatch, collection)

    assert nightly_refresh._run(database_url, hosted_only=True) == 1

    # The failed metadata step never collected a frame, and it did not stop
    # PBP game-log ingestion from being attempted.
    assert collection == []
    assert calls == [("player_game_logs", "2025-26")]
    engine = create_engine(database_url)
    try:
        assert not inspect(engine).has_table("player_information")
        assert (
            StatsFreshnessRepository(engine).get().last_successful_completion
            is None
        )
    finally:
        engine.dispose()
    assert "metadata refresh" in capsys.readouterr().err


@pytest.mark.parametrize("missing_stream", HOSTED_METADATA_STREAMS)
def test_hosted_metadata_fails_closed_when_a_required_stream_is_missing(
    tmp_path, monkeypatch, capsys, missing_stream
):
    database_url = _hosted_database(tmp_path, absent=(missing_stream,))
    calls = []
    _install_hosted_boundaries(monkeypatch, calls)
    collection = []
    _record_active_players(monkeypatch, collection)

    assert nightly_refresh._run(database_url, hosted_only=True) == 1

    assert collection == []
    assert calls == [("player_game_logs", "2025-26")]
    engine = create_engine(database_url)
    try:
        assert not inspect(engine).has_table("player_information")
        assert (
            StatsFreshnessRepository(engine).get().last_successful_completion
            is None
        )
    finally:
        engine.dispose()
    assert "metadata refresh" in capsys.readouterr().err


def test_hosted_metadata_fails_closed_when_activation_state_is_unreadable(
    tmp_path, monkeypatch, capsys
):
    database_url = _hosted_database(tmp_path, unreadable=True)
    calls = []
    _install_hosted_boundaries(monkeypatch, calls)
    collection = []
    _record_active_players(monkeypatch, collection)

    assert nightly_refresh._run(database_url, hosted_only=True) == 1

    assert collection == []
    assert calls == [("player_game_logs", "2025-26")]
    assert "metadata refresh" in capsys.readouterr().err


def test_failed_hosted_metadata_publication_preserves_last_good(
    tmp_path, monkeypatch
):
    database_url = _hosted_database(tmp_path)
    first = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE player_information (value TEXT)"))
        connection.execute(text("INSERT INTO player_information VALUES ('old')"))
    StatsFreshnessRepository(engine).record_success(first)
    engine.dispose()

    class FailingFreshness:
        def record_success(self, *_args, **_kwargs):
            raise RuntimeError("write failed")

    calls = []
    _install_hosted_boundaries(monkeypatch, calls)
    monkeypatch.setattr(
        nightly_refresh,
        "StatsFreshnessRepository",
        lambda actual_engine: FailingFreshness(),
    )
    collection = []
    _record_active_players(monkeypatch, collection)

    assert nightly_refresh._run(database_url, hosted_only=True) == 1

    # The metadata step collected but its publication rolled back, so the
    # previous table and completion record survive; game logs still ran.
    assert collection == ["player_information", "player_information"]
    assert calls == [("player_game_logs", "2025-26")]
    engine = create_engine(database_url)
    try:
        assert pd.read_sql(
            "SELECT value FROM player_information", engine
        )["value"].tolist() == ["old"]
        assert (
            StatsFreshnessRepository(engine).get().last_successful_completion
            == first
        )
    finally:
        engine.dispose()


def test_hosted_metadata_succeeds_even_when_game_logs_fail(tmp_path, monkeypatch):
    database_url = _hosted_database(tmp_path)
    calls = []
    _install_hosted_boundaries(monkeypatch, calls, game_log_result=False)
    collection = []
    _record_active_players(monkeypatch, collection)

    assert nightly_refresh._run(database_url, hosted_only=True) == 1

    # Only the failed game-log step was retried; metadata ran once.
    assert calls == [
        ("player_game_logs", "2025-26"),
        ("player_game_logs", "2025-26"),
    ]
    assert collection == ["player_information"]
    engine = create_engine(database_url)
    try:
        assert inspect(engine).has_table("player_information")
        assert (
            StatsFreshnessRepository(engine).get().last_successful_completion
            is not None
        )
    finally:
        engine.dispose()


def test_run_wires_owner_services_into_the_six_step_refresh(monkeypatch):
    calls = []
    provider = object()
    pbp_provider = object()
    pbp_log_provider = object()
    catalog = object()
    stats_freshness = object()
    write_fence = object()
    player_log_repository = object()
    team_matchup_repository = object()

    class FakeEngine:
        def dispose(self):
            calls.append("dispose")

    engine = FakeEngine()
    stats_service = SimpleNamespace(
        update_all_data=lambda: calls.append("stats") or True
    )
    event_service = SimpleNamespace(
        refresh=lambda season: calls.append(("schedule", season)) or object()
    )
    athlete_service = SimpleNamespace(
        refresh_season=lambda season: calls.append(("athlete_catalog", season))
        or SimpleNamespace(status="succeeded")
    )
    player_log_service = SimpleNamespace(
        refresh=lambda season: calls.append(("player_game_logs", season)) or object()
    )
    player_diet_service = SimpleNamespace(
        refresh=lambda season: calls.append(("player_diets", season)) or object()
    )
    team_matchup_service = SimpleNamespace(
        refresh=lambda season: calls.append(("team_matchups", season)) or object()
    )
    settings = SimpleNamespace(
        nba=SimpleNamespace(current_season="2025-26"),
        catalog=SimpleNamespace(
            player_game_log_max_age_hours=30,
            player_game_log_min_active_players_per_team_game=5,
            player_game_log_reconciliation_days=3,
        ),
    )

    def build_data_service(actual_engine, **kwargs):
        assert actual_engine is engine
        assert kwargs == {
            "settings": settings,
            "pbp_provider": pbp_provider,
            "nba_stats_provider": provider,
            "stats_freshness": stats_freshness,
            "write_fence": write_fence,
        }
        return stats_service

    def build_event_service(actual_engine, **kwargs):
        assert actual_engine is engine
        assert kwargs == {"settings": settings, "nba_stats_provider": provider}
        return event_service

    def build_athlete_service(actual_engine, **kwargs):
        assert actual_engine is engine
        assert kwargs == {"settings": settings, "nba_stats_provider": provider}
        return athlete_service

    def build_log_repository(actual_engine, **kwargs):
        assert actual_engine is engine
        assert kwargs == {
            "statistic_catalog": catalog,
            "stats_surface_season": "2025-26",
            "stats_surface_max_age": nightly_refresh.time_window_timedelta(
                settings.catalog.player_game_log_max_age_hours,
                unit_seconds=3600,
                field="PLAYER_GAME_LOG_MAX_AGE_HOURS",
            ),
        }
        return player_log_repository

    def build_ingest_service(**kwargs):
        assert kwargs == {
            "pbp_provider": pbp_log_provider,
            "repository": player_log_repository,
            "athlete_catalog": athlete_service,
            "event_catalog": event_service,
            "minimum_active_players_per_team_game": 5,
            "reconciliation_days": 3,
        }
        return player_log_service

    def build_team_matchup_service(**kwargs):
        assert kwargs == {
            "repository": team_matchup_repository,
            "event_catalog": event_service,
            "nba_stats_provider": provider,
            "pbp_stats_provider": pbp_provider,
        }
        return team_matchup_service

    def build_player_diet_service(actual_engine, **kwargs):
        assert actual_engine is engine
        assert kwargs == {
            "athlete_catalog": athlete_service,
            "nba_stats_provider": provider,
            "pbp_stats_provider": pbp_provider,
            "write_fence": write_fence,
        }
        return player_diet_service

    monkeypatch.setattr(nightly_refresh, "load_settings", lambda **kwargs: settings)
    monkeypatch.setattr(nightly_refresh, "create_engine", lambda url: engine)
    monkeypatch.setattr(nightly_refresh, "_normalize_database_url", lambda url: url)
    monkeypatch.setattr(
        nightly_refresh,
        "run_migrations",
        lambda actual_engine: calls.append("migrations"),
    )
    monkeypatch.setattr(
        nightly_refresh,
        "PublicationService",
        lambda actual_engine: SimpleNamespace(
            register_default_streams=lambda: calls.append("streams")
        ),
    )
    monkeypatch.setattr(nightly_refresh, "NBAStatsAdapter", lambda **kwargs: provider)
    monkeypatch.setattr(
        nightly_refresh, "PBPStatsAdapter", lambda **kwargs: pbp_provider
    )
    monkeypatch.setattr(
        nightly_refresh, "PBPGameLogAdapter", lambda **kwargs: pbp_log_provider
    )
    monkeypatch.setattr(
        nightly_refresh,
        "StatsFreshnessRepository",
        lambda actual_engine: stats_freshness,
    )
    monkeypatch.setattr(nightly_refresh, "DataService", build_data_service)
    monkeypatch.setattr(nightly_refresh, "EventCatalogService", build_event_service)
    monkeypatch.setattr(nightly_refresh, "AthleteCatalogService", build_athlete_service)
    monkeypatch.setattr(nightly_refresh, "PlayerGameLogRepository", build_log_repository)
    monkeypatch.setattr(
        nightly_refresh,
        "StatisticCatalog",
        SimpleNamespace(load_default=lambda: catalog),
    )
    monkeypatch.setattr(
        nightly_refresh,
        "PlayerGameLogIngestService",
        build_ingest_service,
    )
    monkeypatch.setattr(
        nightly_refresh,
        "PlayerDietService",
        build_player_diet_service,
    )
    def build_team_matchup_repository(actual_engine, **kwargs):
        assert actual_engine is engine
        assert kwargs == {"write_fence": write_fence}
        return team_matchup_repository

    monkeypatch.setattr(nightly_refresh, "LegacyWriteFence", lambda actual_engine: write_fence)
    monkeypatch.setattr(nightly_refresh, "TeamMatchupRepository", build_team_matchup_repository)
    monkeypatch.setattr(
        nightly_refresh,
        "TeamMatchupRefreshService",
        build_team_matchup_service,
    )

    assert nightly_refresh._run("sqlite:///nightly.sqlite3") == 0
    assert calls == [
        "migrations",
        "streams",
        "stats",
        ("schedule", "2025-26"),
        ("athlete_catalog", "2025-26"),
        ("player_game_logs", "2025-26"),
        ("player_diets", "2025-26"),
        ("team_matchups", "2025-26"),
        "dispose",
    ]


def test_nightly_refresh_runs_all_six_steps_once_on_success():
    calls = []

    assert (
        run_nightly_refresh(
            refresh_stats=lambda: calls.append("stats") or True,
            refresh_athlete_catalog=lambda: calls.append("athlete_catalog") or True,
            refresh_schedule=lambda: calls.append("schedule") or object(),
            refresh_player_game_logs=lambda: calls.append("player_game_logs")
            or object(),
            refresh_player_diets=lambda: calls.append("player_diets") or object(),
            refresh_team_matchups=lambda: calls.append("team_matchups") or object(),
        )
        == 0
    )
    assert calls == [
        "stats",
        "schedule",
        "athlete_catalog",
        "player_game_logs",
        "player_diets",
        "team_matchups",
    ]


def test_nightly_refresh_retries_the_whole_unit_exactly_once(capsys):
    calls = []
    stats_results = iter([False, True])

    assert (
        run_nightly_refresh(
            refresh_stats=lambda: calls.append("stats") or next(stats_results),
            refresh_athlete_catalog=lambda: calls.append("athlete_catalog") or True,
            refresh_schedule=lambda: calls.append("schedule") or object(),
            refresh_player_game_logs=lambda: calls.append("player_game_logs")
            or object(),
            refresh_player_diets=lambda: calls.append("player_diets") or object(),
            refresh_team_matchups=lambda: calls.append("team_matchups") or object(),
        )
        == 0
    )
    assert calls == [
        "stats",
        "stats",
        "schedule",
        "athlete_catalog",
        "player_game_logs",
        "player_diets",
        "team_matchups",
    ]
    assert "attempt 1 failed during stats refresh; retrying" in capsys.readouterr().err


def test_nightly_refresh_keeps_schedule_before_retrying_athlete_catalog(capsys):
    calls = []
    athlete_results = iter([False, True])

    assert (
        run_nightly_refresh(
            refresh_stats=lambda: calls.append("stats") or True,
            refresh_athlete_catalog=lambda: calls.append("athlete_catalog")
            or next(athlete_results),
            refresh_schedule=lambda: calls.append("schedule") or object(),
            refresh_player_game_logs=lambda: calls.append("player_game_logs")
            or object(),
            refresh_player_diets=lambda: calls.append("player_diets") or object(),
            refresh_team_matchups=lambda: calls.append("team_matchups") or object(),
        )
        == 0
    )
    assert calls == [
        "stats",
        "schedule",
        "athlete_catalog",
        "stats",
        "schedule",
        "athlete_catalog",
        "player_game_logs",
        "player_diets",
        "team_matchups",
    ]
    assert "attempt 1 failed during athlete catalog refresh; retrying" in (
        capsys.readouterr().err
    )


def test_nightly_refresh_reports_athlete_failure_without_running_player_logs(capsys):
    calls = []

    assert (
        run_nightly_refresh(
            refresh_stats=lambda: calls.append("stats") or True,
            refresh_athlete_catalog=lambda: calls.append("athlete_catalog") or False,
            refresh_schedule=lambda: calls.append("schedule") or object(),
            refresh_player_game_logs=lambda: calls.append("player_game_logs")
            or object(),
            refresh_player_diets=lambda: calls.append("player_diets") or object(),
            refresh_team_matchups=lambda: calls.append("team_matchups") or object(),
        )
        == 1
    )
    assert calls == [
        "stats",
        "schedule",
        "athlete_catalog",
        "stats",
        "schedule",
        "athlete_catalog",
    ]
    diagnostics = capsys.readouterr().err
    assert "attempt 1 failed during athlete catalog refresh; retrying" in diagnostics
    assert (
        "attempt 2 failed during athlete catalog refresh; no retries remain"
        in diagnostics
    )


def test_nightly_refresh_returns_failure_after_two_attempts(capsys):
    calls = []

    assert (
        run_nightly_refresh(
            refresh_stats=lambda: calls.append("stats") or True,
            refresh_athlete_catalog=lambda: calls.append("athlete_catalog") or True,
            refresh_schedule=lambda: calls.append("schedule")
            or (_ for _ in ()).throw(RuntimeError("offline failure")),
            refresh_player_game_logs=lambda: calls.append("player_game_logs")
            or object(),
            refresh_player_diets=lambda: calls.append("player_diets") or object(),
            refresh_team_matchups=lambda: calls.append("team_matchups") or object(),
        )
        == 1
    )
    assert calls == [
        "stats",
        "schedule",
        "stats",
        "schedule",
    ]
    diagnostics = capsys.readouterr().err
    assert "attempt 1 failed during schedule refresh; retrying" in diagnostics
    assert "attempt 2 failed during schedule refresh; no retries remain" in diagnostics
    assert "offline failure" not in diagnostics


def test_nightly_refresh_retries_whole_unit_when_player_logs_fail(capsys):
    calls = []
    log_attempts = iter([RuntimeError("offline failure"), None])

    def refresh_logs():
        calls.append("player_game_logs")
        error = next(log_attempts)
        if error is not None:
            raise error

    assert (
        run_nightly_refresh(
            refresh_stats=lambda: calls.append("stats") or True,
            refresh_athlete_catalog=lambda: calls.append("athlete_catalog") or True,
            refresh_schedule=lambda: calls.append("schedule") or object(),
            refresh_player_game_logs=refresh_logs,
            refresh_player_diets=lambda: calls.append("player_diets") or object(),
            refresh_team_matchups=lambda: calls.append("team_matchups") or object(),
        )
        == 0
    )
    assert calls == [
        "stats",
        "schedule",
        "athlete_catalog",
        "player_game_logs",
        "stats",
        "schedule",
        "athlete_catalog",
        "player_game_logs",
        "player_diets",
        "team_matchups",
    ]
    assert "failed during player game logs refresh" in capsys.readouterr().err


def test_nightly_refresh_retries_whole_unit_when_team_matchups_fail(capsys):
    calls = []
    matchup_attempts = iter([RuntimeError("provider down"), None])

    def refresh_matchups():
        calls.append("team_matchups")
        error = next(matchup_attempts)
        if error is not None:
            raise error

    assert (
        run_nightly_refresh(
            refresh_stats=lambda: calls.append("stats") or True,
            refresh_schedule=lambda: calls.append("schedule") or object(),
            refresh_athlete_catalog=lambda: calls.append("athlete_catalog") or True,
            refresh_player_game_logs=lambda: calls.append("player_game_logs")
            or object(),
            refresh_player_diets=lambda: calls.append("player_diets") or object(),
            refresh_team_matchups=refresh_matchups,
        )
        == 0
    )
    assert calls == [
        "stats",
        "schedule",
        "athlete_catalog",
        "player_game_logs",
        "player_diets",
        "team_matchups",
        "stats",
        "schedule",
        "athlete_catalog",
        "player_game_logs",
        "player_diets",
        "team_matchups",
    ]
    assert "failed during team matchups refresh; retrying" in capsys.readouterr().err


def test_nightly_refresh_retries_whole_unit_when_player_diets_fail(capsys):
    calls = []
    diet_attempts = iter([RuntimeError("provider down"), None])

    def refresh_diets():
        calls.append("player_diets")
        error = next(diet_attempts)
        if error is not None:
            raise error

    assert (
        run_nightly_refresh(
            refresh_stats=lambda: calls.append("stats") or True,
            refresh_schedule=lambda: calls.append("schedule") or object(),
            refresh_athlete_catalog=lambda: calls.append("athlete_catalog") or True,
            refresh_player_game_logs=lambda: calls.append("player_game_logs")
            or object(),
            refresh_player_diets=refresh_diets,
            refresh_team_matchups=lambda: calls.append("team_matchups") or object(),
        )
        == 0
    )
    assert calls == [
        "stats",
        "schedule",
        "athlete_catalog",
        "player_game_logs",
        "player_diets",
        "stats",
        "schedule",
        "athlete_catalog",
        "player_game_logs",
        "player_diets",
        "team_matchups",
    ]
    assert "failed during player diets refresh; retrying" in capsys.readouterr().err


def test_hosted_refresh_runs_both_steps_once_on_success():
    calls = []

    assert (
        run_hosted_refresh(
            refresh_metadata=lambda: calls.append("metadata") or True,
            refresh_player_game_logs=lambda: calls.append("player_game_logs")
            or object(),
        )
        == 0
    )
    assert calls == ["metadata", "player_game_logs"]


def test_hosted_refresh_retries_only_the_failed_metadata_step(capsys):
    calls = []
    metadata_results = iter([False, True])

    assert (
        run_hosted_refresh(
            refresh_metadata=lambda: calls.append("metadata")
            or next(metadata_results),
            refresh_player_game_logs=lambda: calls.append("player_game_logs")
            or object(),
        )
        == 0
    )
    # The successful game-log step is not rerun with the failed metadata step.
    assert calls == ["metadata", "player_game_logs", "metadata"]
    assert (
        "attempt 1 failed during metadata refresh; retrying"
        in capsys.readouterr().err
    )


def test_hosted_refresh_retries_only_the_failed_game_log_step(capsys):
    calls = []
    log_attempts = iter([RuntimeError("offline failure"), None])

    def refresh_logs():
        calls.append("player_game_logs")
        error = next(log_attempts)
        if error is not None:
            raise error

    assert (
        run_hosted_refresh(
            refresh_metadata=lambda: calls.append("metadata") or True,
            refresh_player_game_logs=refresh_logs,
        )
        == 0
    )
    assert calls == ["metadata", "player_game_logs", "player_game_logs"]
    diagnostics = capsys.readouterr().err
    assert (
        "attempt 1 failed during player game logs refresh; retrying" in diagnostics
    )
    assert "offline failure" not in diagnostics


def test_hosted_refresh_returns_failure_after_independent_retries(capsys):
    calls = []
    metadata_results = iter([False, False])
    log_results = iter([False, False])

    assert (
        run_hosted_refresh(
            refresh_metadata=lambda: calls.append("metadata")
            or next(metadata_results),
            refresh_player_game_logs=lambda: calls.append("player_game_logs")
            or next(log_results),
        )
        == 1
    )
    assert calls == [
        "metadata",
        "player_game_logs",
        "metadata",
        "player_game_logs",
    ]
    diagnostics = capsys.readouterr().err
    assert "attempt 1 failed during metadata refresh; retrying" in diagnostics
    assert "attempt 1 failed during player game logs refresh; retrying" in diagnostics
    assert "attempt 2 failed during metadata refresh" in diagnostics
    assert "attempt 2 failed during player game logs refresh" in diagnostics


def test_main_reports_success_without_live_calls(tmp_path, monkeypatch, capsys):
    database_url = f"sqlite:///{tmp_path / 'nightly.sqlite3'}"
    monkeypatch.setattr(nightly_refresh, "_run", lambda value: 0)

    assert nightly_refresh.main(["--database-url", database_url]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "succeeded"}


def test_main_selects_hosted_only_refresh_without_live_calls(
    tmp_path, monkeypatch, capsys
):
    database_url = f"sqlite:///{tmp_path / 'nightly.sqlite3'}"
    calls = []
    monkeypatch.setattr(
        nightly_refresh,
        "_run",
        lambda value, *, hosted_only: calls.append((value, hosted_only)) or 0,
    )

    assert (
        nightly_refresh.main(
            ["--database-url", database_url, "--hosted-only"]
        )
        == 0
    )
    assert calls == [(database_url, True)]
    assert json.loads(capsys.readouterr().out) == {"status": "succeeded"}


def test_main_reports_failure_without_live_calls(tmp_path, monkeypatch, capsys):
    database_url = f"sqlite:///{tmp_path / 'nightly.sqlite3'}"
    monkeypatch.setattr(nightly_refresh, "_run", lambda value: 1)

    assert nightly_refresh.main(["--database-url", database_url]) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "failed"}


def test_main_requires_database_target(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as raised:
        nightly_refresh.main([])

    assert raised.value.code == 2


def test_main_rejects_demo_database(capsys):
    with pytest.raises(SystemExit) as raised:
        nightly_refresh.main(["--database-url", "sqlite:///nba_play_types.db"])

    assert raised.value.code == 2
    assert "read-only demo" in capsys.readouterr().err
