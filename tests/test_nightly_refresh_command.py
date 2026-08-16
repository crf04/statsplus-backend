"""Offline behavior of the Railway Nightly Refresh process command."""

import json
from types import SimpleNamespace

import pytest

from scripts import nightly_refresh
from scripts.nightly_refresh import run_nightly_refresh


def test_hosted_refresh_never_constructs_or_calls_nba_stats(monkeypatch):
    calls = []
    settings = SimpleNamespace(
        nba=SimpleNamespace(current_season="2025-26"),
        catalog=SimpleNamespace(
            player_game_log_max_age_hours=30,
            player_game_log_min_active_players_per_team_game=5,
            player_game_log_reconciliation_days=3,
        ),
    )

    class FakeEngine:
        def dispose(self):
            calls.append("dispose")

    engine = FakeEngine()
    player_log_repository = object()
    event_service = object()
    athlete_service = object()
    pbp_log_provider = object()

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
        "NBAStatsAdapter",
        lambda **kwargs: pytest.fail("hosted refresh constructed NBA Stats"),
    )
    monkeypatch.setattr(
        nightly_refresh, "PBPGameLogAdapter", lambda **kwargs: pbp_log_provider
    )
    monkeypatch.setattr(
        nightly_refresh,
        "EventCatalogService",
        lambda actual_engine, **kwargs: event_service,
    )
    monkeypatch.setattr(
        nightly_refresh,
        "AthleteCatalogService",
        lambda actual_engine, **kwargs: athlete_service,
    )
    monkeypatch.setattr(
        nightly_refresh,
        "PlayerGameLogRepository",
        lambda actual_engine, **kwargs: player_log_repository,
    )
    monkeypatch.setattr(
        nightly_refresh,
        "StatisticCatalog",
        SimpleNamespace(load_default=lambda: object()),
    )

    def build_ingest_service(**kwargs):
        assert kwargs == {
            "pbp_provider": pbp_log_provider,
            "repository": player_log_repository,
            "athlete_catalog": athlete_service,
            "event_catalog": event_service,
            "minimum_active_players_per_team_game": 5,
            "reconciliation_days": 3,
        }
        return SimpleNamespace(
            refresh=lambda season: calls.append(("player_game_logs", season))
            or object()
        )

    monkeypatch.setattr(
        nightly_refresh, "PlayerGameLogIngestService", build_ingest_service
    )

    assert nightly_refresh._run("sqlite:///nightly.sqlite3", hosted_only=True) == 0
    assert calls == [
        "migrations",
        ("player_game_logs", "2025-26"),
        "dispose",
    ]


def test_run_wires_owner_services_into_the_six_step_refresh(monkeypatch):
    calls = []
    provider = object()
    pbp_provider = object()
    pbp_log_provider = object()
    catalog = object()
    stats_freshness = object()
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
    monkeypatch.setattr(
        nightly_refresh,
        "TeamMatchupRepository",
        lambda actual_engine: team_matchup_repository,
    )
    monkeypatch.setattr(
        nightly_refresh,
        "TeamMatchupRefreshService",
        build_team_matchup_service,
    )

    assert nightly_refresh._run("sqlite:///nightly.sqlite3") == 0
    assert calls == [
        "migrations",
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
