"""Offline persisted-fixture proof for the complete matchup endpoint."""

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import create_engine

from app import create_app
from app.config.settings import (
    AuthenticationSettings,
    CacheSettings,
    NBASeasonSettings,
    RuntimeSettings,
)
from app.migrations import run_migrations
from app.services.event_catalog_service import EventCatalogService
from app.services.matchup import MatchupService
from app.services.player_diet import (
    PlayerDietFact,
    PlayerDietObservation,
    PlayerDietService,
)
from app.services.player_game_log_repository import (
    PlayerGameLogRecord,
    PlayerGameLogRepository,
)
from app.services.player_pool import StoredPlayerPoolReader
from app.services.player_pool_snapshot_repository import (
    PlayerPoolSnapshotRepository,
    PlayerPoolSnapshotScope,
)
from app.services.statistic_catalog import StatisticCatalog
from app.services.stats_freshness_repository import StatsFreshnessRepository
from app.services.team_matchup_query import TeamMatchupQueryService
from app.services.team_matchup_repository import (
    TeamMatchupFact,
    TeamMatchupObservation,
    TeamMatchupRepository,
    TeamMatchupSnapshotScope,
)


TEAM_FIXTURE = Path(__file__).parents[1] / "fixtures/team_matchups/thirty_teams.json"
NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
SEASON = "2025-26"
GAME_ID = "0022500584"
LAL = 1610612747
BOS = 1610612738


class _NoProvider:
    def __getattr__(self, name):
        raise AssertionError(f"request-time provider access is forbidden: {name}")


def _event_catalog(engine, settings):
    service = EventCatalogService(
        engine,
        nba_stats_provider=_NoProvider(),
        settings=settings,
        clock=lambda: NOW,
    )
    service.repository.publish(
        SEASON,
        pd.DataFrame(
            [
                {
                    "nba_game_id": GAME_ID,
                    "season": SEASON,
                    "home_team_id": BOS,
                    "home_team_name": "Boston Celtics",
                    "home_team_tricode": "BOS",
                    "away_team_id": LAL,
                    "away_team_name": "Los Angeles Lakers",
                    "away_team_tricode": "LAL",
                    "scheduled_at": datetime(2026, 1, 16, 0, 30, tzinfo=timezone.utc),
                    "status_text": "Scheduled",
                    "status_code": 1,
                    "postponed_status": None,
                    "postponement_evidence": None,
                    "classification": "Regular Season",
                }
            ]
        ),
        NOW,
    )
    return service


def _team_matchups(engine):
    repository = TeamMatchupRepository(engine)
    teams = json.loads(TEAM_FIXTURE.read_text(encoding="utf-8"))
    metrics = (
        ("play_types", "Transition", "PTS"),
        ("shot_zones", "Restricted Area", "FGA"),
        ("shot_types", "catch_and_shoot", "FG3A"),
        ("assist_locations", "AtRimAssists", "AtRimAssists"),
        ("traditional", "OPP_TOV", "OPP_TOV"),
        ("traditional", "OPP_STL", "OPP_STL"),
        ("traditional", "OPP_BLK", "OPP_BLK"),
    )

    def facts(*, omit_play_types=False):
        return tuple(
            TeamMatchupFact(
                row["team_id"],
                base,
                slice_key,
                stat_key,
                float(row["allowed"]),
                48.0,
                "minutes",
                "recorded",
            )
            for row in teams
            for base, slice_key, stat_key in metrics
            if not (omit_play_types and base == "play_types")
        )

    season_scope = TeamMatchupSnapshotScope(SEASON, date(2026, 1, 15))
    last_scope = TeamMatchupSnapshotScope(SEASON, date(2026, 1, 15), 15)
    bases = (
        "assist_locations",
        "play_types",
        "shot_types",
        "shot_zones",
        "traditional",
    )
    repository.replace_snapshots(
        (
            (
                season_scope,
                facts(),
                tuple(TeamMatchupObservation(base, "available") for base in bases),
            ),
            (
                last_scope,
                facts(omit_play_types=True),
                tuple(
                    TeamMatchupObservation(
                        base,
                        "unavailable" if base == "play_types" else "available",
                        "provider_unsupported" if base == "play_types" else None,
                    )
                    for base in bases
                ),
            ),
        ),
        retrieved_at=NOW,
    )
    return TeamMatchupQueryService(repository, clock=lambda: NOW)


def _player_pool(engine):
    repository = PlayerPoolSnapshotRepository(engine)
    scope = PlayerPoolSnapshotScope.create(SEASON, (GAME_ID,))
    assert repository.try_acquire_refresh(
        scope, owner="fixture", now=NOW, lease_seconds=60
    )
    assert repository.replace_owned(
        scope,
        owner="fixture",
        payload={
            "players": [
                {
                    "canonical_player_id": 2544,
                    "name": "LeBron James",
                    "team_id": LAL,
                    "market_categories": ["PTS", "FGA"],
                    "provenance": {
                        "prizepicks": ["PTS", "FGA"],
                        "underdog": ["PTS"],
                    },
                }
            ],
            "team_counts": {str(LAL): 1},
            "freshness": {
                "status": "fresh",
                "retrieved_at": NOW.isoformat(),
                "providers": {
                    "prizepicks": {
                        "status": "fresh",
                        "retrieved_at": NOW.isoformat(),
                    }
                },
            },
        },
        retrieved_at=NOW,
        now=NOW,
    )
    return StoredPlayerPoolReader(repository, clock=lambda: NOW)


def _player_logs(engine, catalog):
    repository = PlayerGameLogRepository(
        engine,
        statistic_catalog=catalog,
        stats_surface_max_age=timedelta(hours=30),
        stats_surface_season=SEASON,
        clock=lambda: NOW,
    )
    repository.publish(
        SEASON,
        (
            PlayerGameLogRecord(
                season=SEASON,
                season_type="Regular Season",
                player_id=2544,
                game_id="0022500500",
                player_name="LeBron James",
                game_date=date(2026, 1, 10),
                team_id=LAL,
                team_tricode="LAL",
                opponent_team_id=BOS,
                opponent_team_tricode="BOS",
                is_home=True,
                minutes=35.0,
                points=25,
                rebounds=8,
                assists=7,
                field_goals_made=10,
                field_goals_attempted=18,
                three_pointers_made=3,
                three_pointers_attempted=7,
                turnovers=3,
                steals=1,
                blocks=1,
            ),
        ),
        retrieved_at=NOW,
        source_provider="recorded",
        source_row_count=1,
    )
    return repository


def _player_diets(engine):
    service = PlayerDietService(
        engine,
        athlete_catalog=_NoProvider(),
        nba_stats_provider=_NoProvider(),
        pbp_stats_provider=_NoProvider(),
        clock=lambda: NOW,
    )
    service.repository.publish(
        SEASON,
        (
            PlayerDietFact(
                2544,
                "play_types",
                "Transition",
                0.19,
                95.0,
                20,
                "possessions",
                "nba_synergy",
            ),
            PlayerDietFact(
                2544,
                "shot_zones",
                "Restricted Area",
                0.27,
                108.0,
                20,
                "field_goal_attempts",
                "nba_stats",
            ),
            PlayerDietFact(
                2544,
                "shot_types",
                "Catch and Shoot",
                0.36,
                72.0,
                20,
                "field_goal_attempts",
                "nba_stats",
            ),
            PlayerDietFact(
                2544,
                "assist_locations",
                "AtRimAssists",
                0.31,
                31.0,
                20,
                "assists",
                "pbp_stats",
            ),
        ),
        tuple(
            PlayerDietObservation(base, "available")
            for base in (
                "assist_locations",
                "play_types",
                "shot_types",
                "shot_zones",
            )
        ),
        retrieved_at=NOW,
    )
    return service


def test_persisted_matchup_fixture_serves_exact_windows_and_raw_player_facts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'matchup.sqlite3'}")
    run_migrations(engine)
    settings = RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
        nba=NBASeasonSettings(current_season=SEASON),
    )
    catalog = StatisticCatalog.load_default()
    stats_freshness = StatsFreshnessRepository(engine)
    stats_freshness.record_success(NOW)
    service = MatchupService(
        event_catalog=_event_catalog(engine, settings),
        player_pool=_player_pool(engine),
        player_logs=_player_logs(engine, catalog),
        player_diets=_player_diets(engine),
        team_matchups=_team_matchups(engine),
        stats_freshness=stats_freshness,
        settings=settings,
        clock=lambda: NOW,
    )
    dependencies = SimpleNamespace(
        settings=settings,
        matchup_service=service,
        user_service=SimpleNamespace(create_or_update_user=lambda _user: None),
    )
    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": dependencies,
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )

    response = app.test_client().get(f"/api/games/matchup?game_id={GAME_ID}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["league"]["surface_availability"]["play_types"]["last_15"] == {
        "status": "unavailable",
        "unavailable_reason": "provider_unsupported",
    }
    assert payload["league"]["defense_sheet"]["play_types"][0]["last_15"] is None
    assert payload["teams"][0]["defense_sheet"]["play_types"][0]["last_15"] is None
    # LAL is the 11th deterministic fixture team; derived values come from the
    # stored 30-team facts, not request-time provider work.
    season_row = payload["teams"][0]["defense_sheet"]["play_types"][0]["season"]
    assert season_row["allowed_per_48"] == 11.0
    assert season_row["percent_vs_league_average"] == -29.032258
    assert season_row["rank"] == 11
    assert payload["players"][0]["canonical_id"] == 2544
    assert payload["players"][0]["season_scoring"] == 25.0
    assert payload["players"][0]["last_10_minutes"] == [35.0]
    assert payload["players"][0]["diet_shares"]["play_types"] == [
        {
            "key": "Transition",
            "season": {
                "share": 0.19,
                "volume": 95.0,
                "games_played": 20,
                "volume_unit": "possessions",
            },
        }
    ]
    assert payload["players"][0]["scores"]["status"] == "unavailable"
