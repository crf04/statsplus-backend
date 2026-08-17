"""Offline persisted-fixture proof for the complete matchup endpoint."""

from datetime import date, datetime, timedelta, timezone
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine

from app import create_app
from app.config.settings import (
    AuthenticationSettings,
    CacheSettings,
    FeatureSettings,
    NBASeasonSettings,
    RuntimeSettings,
)
from app.migrations import run_migrations
from app.models.collection_control import (
    CollectionObservation,
    PublicationObservation,
    PublicationVersion,
)
from app.models.canonical_game_ledger import LedgerParityArtifact
from app.providers.rotowire import InjuryEntryEvidence, InjuryProviderSnapshot
from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.providers.dfs import (
    AthleteEvidence,
    CoverageEvidence,
    EventEvidence,
    MarketStatus,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    SnapshotStatus,
    StatisticEvidence,
    TeamEvidence,
)
from app.services.event_catalog_service import EventCatalogService
from app.services.database_first_activation import (
    DatabaseFirstPublicationReader,
)
from app.services.collection_control import CollectionOperationsService, PublicationService
from app.services.matchup import MatchupService
from app.services.matchup_selection import MatchupSelectionService
from app.services.player_archetype_repository import PlayerArchetypeRepository
from app.services.injury_snapshot_repository import InjurySnapshotRepository
from app.services.matchup_injuries import MatchupInjuryService
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
from app.services.projection_archive import (
    LatestProjectionPlayerPoolReader,
)
from app.services.player_pool_snapshot_repository import (
    PlayerPoolSnapshotRepository,
    PlayerPoolSnapshotScope,
)
from app.services.statistic_catalog import StatisticCatalog
from app.services.stats_freshness_repository import StatsFreshnessRepository
from app.services.slate_service import SlateService
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
COMMON_POSTED_MARKETS = (
    "PTS",
    "REB",
    "AST",
    "3PM",
    "FGA",
    "FG2A",
    "FG3A",
    "PRA",
    "PA",
    "PR",
    "RA",
    "TOV",
    "STL",
    "BLK",
    "STKS",
)


def _recorded_projection_snapshot(catalog):
    statistic = catalog.by_id["points"]
    evidence = StatisticEvidence(
        provider_id="pts",
        canonical_id=statistic.id,
        label="Points",
        components=statistic.components,
    )
    return ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.COMPLETE,
        markets=(
            PlayerProjectionMarket(
                provider="dabble",
                market_id="fixture-market-points",
                athlete=AthleteEvidence(
                    provider_id="fixture-lebron",
                    canonical_id=2544,
                    name="LeBron James",
                    team=TeamEvidence(canonical_id=LAL, abbreviation="LAL"),
                ),
                event=EventEvidence(
                    provider_id="fixture-event",
                    canonical_id=GAME_ID,
                ),
                team=TeamEvidence(canonical_id=LAL, abbreviation="LAL"),
                statistic=evidence,
                statistic_match=StatisticMatch(
                    state=MatchState.CANONICAL,
                    evidence=evidence,
                    scoring_period=ScoringPeriod.FULL_GAME,
                    canonical=statistic,
                    provider="dabble",
                ),
                status=MarketStatus.AVAILABLE,
                variant=MarketVariant.STANDARD,
                scoring_period=ScoringPeriod.FULL_GAME,
            ),
        ),
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            expected_total=1,
        ),
        retrieved_at=NOW,
    )


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


def _team_matchups(
    engine,
    *,
    asymmetric_shot_zones=False,
    missing_rebound_window=None,
    extra_traditional_window=None,
):
    repository = TeamMatchupRepository(engine)
    teams = json.loads(TEAM_FIXTURE.read_text(encoding="utf-8"))
    metrics = (
        ("play_types", "Transition", "PTS"),
        ("shot_zones", "Restricted Area", "FGA"),
        ("shot_zones", "Restricted Area", "FGM"),
        ("shot_zones", "In The Paint (Non-RA)", "FGA"),
        ("shot_zones", "In The Paint (Non-RA)", "FGM"),
        ("shot_zones", "Mid-Range", "FGA"),
        ("shot_zones", "Mid-Range", "FGM"),
        ("shot_zones", "Corner 3", "FGA"),
        ("shot_zones", "Corner 3", "FGM"),
        ("shot_zones", "Above the Break 3", "FGA"),
        ("shot_zones", "Above the Break 3", "FGM"),
        ("shot_zones", "Left Corner 3", "FGA"),
        ("shot_zones", "Left Corner 3", "FGM"),
        ("shot_zones", "Right Corner 3", "FGA"),
        ("shot_zones", "Right Corner 3", "FGM"),
        ("shot_zones", "Backcourt", "FGA"),
        ("shot_zones", "Backcourt", "FGM"),
        ("shot_types", "catch_and_shoot", "FG2M"),
        ("shot_types", "catch_and_shoot", "FG2A"),
        ("shot_types", "catch_and_shoot", "FG3M"),
        ("shot_types", "catch_and_shoot", "FG3A"),
        ("shot_types", "pullups", "FG2M"),
        ("shot_types", "pullups", "FG2A"),
        ("shot_types", "pullups", "FG3M"),
        ("shot_types", "pullups", "FG3A"),
        ("shot_types", "less_than_10_ft", "FG2M"),
        ("shot_types", "less_than_10_ft", "FG2A"),
        ("shot_types", "less_than_10_ft", "FG3M"),
        ("shot_types", "less_than_10_ft", "FG3A"),
        ("assist_locations", "Arc3Assists", "Arc3Assists"),
        ("assist_locations", "Corner3Assists", "Corner3Assists"),
        ("assist_locations", "AtRimAssists", "AtRimAssists"),
        ("assist_locations", "ShortMidRangeAssists", "ShortMidRangeAssists"),
        ("assist_locations", "LongMidRangeAssists", "LongMidRangeAssists"),
        ("traditional", "OPP_REB", "OPP_REB"),
        ("traditional", "OPP_TOV", "OPP_TOV"),
        ("traditional", "OPP_STL", "OPP_STL"),
        ("traditional", "OPP_BLK", "OPP_BLK"),
    )

    def facts(
        *,
        omit_play_types=False,
        omit_paint=False,
        omit_rebounds=False,
        include_opponent_pf=False,
    ):
        stored_metrics = (
            *metrics,
            *(
                (("traditional", "OPP_PF", "OPP_PF"),)
                if include_opponent_pf
                else ()
            ),
        )
        return tuple(
            TeamMatchupFact(
                row["team_id"],
                base,
                slice_key,
                stat_key,
                (
                    0.0
                    if base == "shot_types"
                    and slice_key == "less_than_10_ft"
                    and stat_key in {"FG3M", "FG3A"}
                    else float(row["allowed"])
                ),
                48.0,
                "minutes",
                "recorded",
            )
            for row in teams
            for base, slice_key, stat_key in stored_metrics
            if not (omit_play_types and base == "play_types")
            and not (
                omit_rebounds
                and base == "traditional"
                and slice_key == stat_key == "OPP_REB"
            )
            and not (
                omit_paint
                and base == "shot_zones"
                and slice_key == "In The Paint (Non-RA)"
                and stat_key == "FGA"
            )
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
                facts(
                    omit_rebounds=missing_rebound_window == "season",
                    include_opponent_pf=extra_traditional_window == "season",
                ),
                tuple(TeamMatchupObservation(base, "available") for base in bases),
            ),
            (
                last_scope,
                facts(
                    omit_play_types=True,
                    omit_paint=asymmetric_shot_zones,
                    omit_rebounds=missing_rebound_window == "last_15",
                    include_opponent_pf=extra_traditional_window == "last_15",
                ),
                tuple(
                    TeamMatchupObservation(
                        base,
                        "unavailable" if base == "play_types" else "available",
                        "provider_window_unsupported" if base == "play_types" else None,
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
                    "market_categories": list(COMMON_POSTED_MARKETS),
                    "provenance": {
                        "prizepicks": list(COMMON_POSTED_MARKETS),
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
                1.0,
                95.0,
                20,
                "possessions",
                "nba_synergy",
            ),
            *(
                PlayerDietFact(
                    2544,
                    "play_types",
                    slice_key,
                    0.0,
                    0.0,
                    20,
                    "possessions",
                    "nba_synergy",
                )
                for slice_key in (
                    "Isolation",
                    "PRBallHandler",
                    "PRRollMan",
                    "OffRebound",
                    "Spotup",
                    "Cut",
                    "Handoff",
                    "OffScreen",
                    "Misc",
                    "Postup",
                )
            ),
            PlayerDietFact(
                2544,
                "shot_zones",
                "Restricted Area",
                0.2,
                20.0,
                20,
                "field_goal_attempts",
                "nba_stats",
            ),
            *(
                PlayerDietFact(
                    2544,
                    "shot_zones",
                    slice_key,
                    0.2,
                    20.0,
                    20,
                    "field_goal_attempts",
                    "nba_stats",
                )
                for slice_key in (
                    "In The Paint (Non-RA)",
                    "Mid-Range",
                    "Corner 3",
                    "Above the Break 3",
                )
            ),
            PlayerDietFact(
                2544,
                "shot_types",
                "Catch and Shoot",
                0.4,
                40.0,
                20,
                "field_goal_attempts",
                "nba_stats",
            ),
            PlayerDietFact(
                2544,
                "shot_types",
                "Pullups",
                0.3,
                30.0,
                20,
                "field_goal_attempts",
                "nba_stats",
            ),
            PlayerDietFact(
                2544,
                "shot_types",
                "Less Than 10 ft",
                0.3,
                30.0,
                20,
                "field_goal_attempts",
                "nba_stats",
            ),
            PlayerDietFact(
                2544,
                "assist_locations",
                "AtRimAssists",
                0.2,
                20.0,
                20,
                "assists",
                "pbp_stats",
            ),
            *(
                PlayerDietFact(
                    2544,
                    "assist_locations",
                    slice_key,
                    0.2,
                    20.0,
                    20,
                    "assists",
                    "pbp_stats",
                )
                for slice_key in (
                    "Arc3Assists",
                    "Corner3Assists",
                    "ShortMidRangeAssists",
                    "LongMidRangeAssists",
                )
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


def _injuries(engine):
    class RecordedProvider:
        def get_snapshot(self):
            return InjuryProviderSnapshot(
                raw_payload=[
                    {
                        "ID": "6504",
                        "player": "LeBron James",
                        "team": "LAL",
                        "status": "Questionable",
                    }
                ],
                entries=(
                    InjuryEntryEvidence(
                        "rotowire:6504",
                        "6504",
                        "LeBron James",
                        "LAL",
                        "Questionable",
                        "Questionable",
                        "Left ankle soreness",
                        "https://www.rotowire.com/basketball/player/lebron-james-2344",
                    ),
                ),
                retrieved_at=NOW,
            )

    return MatchupInjuryService(
        provider=RecordedProvider(),
        snapshot_repository=InjurySnapshotRepository(engine),
        athlete_catalog=SimpleNamespace(
            get_catalog=lambda season, active_only=False: [
                {
                    "season": season,
                    "player_id": 2544,
                    "display_name": "LeBron James",
                    "team_id": LAL,
                    "team_abbreviation": "LAL",
                }
            ]
        ),
        enabled=True,
        permission_granted=True,
        clock=lambda: NOW,
    )


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
        injuries=_injuries(engine),
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
        "unavailable_reason": "provider_window_unsupported",
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
    play_diet = payload["players"][0]["diet_shares"]["play_types"]
    assert {row["key"] for row in play_diet} == {
        "Transition",
        "Isolation",
        "PRBallHandler",
        "PRRollMan",
        "OffRebound",
        "Spotup",
        "Cut",
        "Handoff",
        "OffScreen",
        "Misc",
        "Postup",
    }
    assert next(row for row in play_diet if row["key"] == "Transition") == {
        "key": "Transition",
        "season": {
            "share": 1.0,
            "volume": 95.0,
            "games_played": 20,
            "volume_unit": "possessions",
        },
    }
    assert payload["players"][0]["injury_badge_ref"] == "rotowire:6504"
    assert payload["injuries"]["status"] == "fresh"
    assert payload["injuries"]["teams"][0]["submission_state"] == "unknown"
    assert payload["injuries"]["teams"][0]["entries"][0][
        "canonical_player_id"
    ] == 2544
    scores = payload["players"][0]["scores"]
    assert set(scores) == set(COMMON_POSTED_MARKETS)
    for market, score in scores.items():
        for window_name in ("season", "last_15"):
            window = score[window_name]
            assert isinstance(window["components"], dict)
            if market in {"TOV", "STL", "BLK", "STKS"}:
                assert "blend" not in window
            else:
                assert window["components"]
                assert set(window["blend"]) == {"value", "thin"}
                assert isinstance(window["blend"]["value"], float)
                assert isinstance(window["blend"]["thin"], bool)
    for market in ("3PM", "FG3A"):
        for window_name in ("season", "last_15"):
            assert scores[market][window_name]["components"]["shot_types"] == {
                "value": -0.609677,
                "thin": True,
            }
    assert "shot_types" in scores["PA"]["season"]["components"]
    assert {
        row["key"]: row["markets"]
        for row in payload["teams"][0]["defense_sheet"]["shot_zones"]
    } == {
        "Above the Break 3:FGA": ["FGA", "FG3A"],
        "Above the Break 3:FGM": ["PTS", "3PM"],
        "Corner 3:FGA": ["FGA", "FG3A"],
        "Corner 3:FGM": ["PTS", "3PM"],
        "In The Paint (Non-RA):FGA": ["FGA", "FG2A"],
        "In The Paint (Non-RA):FGM": ["PTS"],
        "Mid-Range:FGA": ["FGA", "FG2A"],
        "Mid-Range:FGM": ["PTS"],
        "Restricted Area:FGA": ["FGA", "FG2A"],
        "Restricted Area:FGM": ["PTS"],
    }
    player_shot_types = {
        row["key"] for row in payload["players"][0]["diet_shares"]["shot_types"]
    }
    assert player_shot_types == {
        "Catch and Shoot",
        "Pullups",
        "Less Than 10 ft",
    }
    assert {
        row["key"].rsplit(":", 1)[0]
        for row in payload["league"]["defense_sheet"]["shot_types"]
    } == player_shot_types
    for team in payload["teams"]:
        assert {
            row["key"].rsplit(":", 1)[0]
            for row in team["defense_sheet"]["shot_types"]
        } == player_shot_types


def test_persisted_matchup_degrades_only_an_asymmetric_available_surface(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'asymmetric-matchup.sqlite3'}")
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
        team_matchups=_team_matchups(engine, asymmetric_shot_zones=True),
        stats_freshness=stats_freshness,
        settings=settings,
        clock=lambda: NOW,
    )
    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": SimpleNamespace(
                settings=settings,
                matchup_service=service,
                user_service=SimpleNamespace(
                    create_or_update_user=lambda _user: None
                ),
            ),
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )

    response = app.test_client().get(f"/api/games/matchup?game_id={GAME_ID}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["league"]["surface_availability"]["shot_zones"] == {
        "season": {"status": "available", "unavailable_reason": None},
        "last_15": {
            "status": "unavailable",
            "unavailable_reason": "legacy_surface_incomplete",
        },
    }
    assert payload["freshness"]["team_matchups"]["last_15"]["surfaces"][
        "shot_zones"
    ] == {
        "status": "unavailable",
        "unavailable_reason": "legacy_surface_incomplete",
        "retrieved_at": NOW.isoformat(),
    }
    league_rows = payload["league"]["defense_sheet"]["shot_zones"]
    assert {
        "In The Paint (Non-RA):FGA",
        "In The Paint (Non-RA):FGM",
    } <= {row["key"] for row in league_rows}
    assert all(row["season"] is not None for row in league_rows)
    assert all(row["last_15"] is None for row in league_rows)
    for team in payload["teams"]:
        assert [row["key"] for row in team["defense_sheet"]["shot_zones"]] == [
            row["key"] for row in league_rows
        ]
        assert all(row["season"] is not None for row in team["defense_sheet"]["shot_zones"])
        assert all(row["last_15"] is None for row in team["defense_sheet"]["shot_zones"])
    assert payload["league"]["defense_sheet"]["shot_types"][0]["last_15"] is not None


@pytest.mark.parametrize("missing_rebound_window", ("season", "last_15"))
def test_persisted_authenticated_matchup_keeps_legacy_traditional_available(
    tmp_path,
    missing_rebound_window,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / f'legacy-rebounds-{missing_rebound_window}.sqlite3'}"
    )
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
        team_matchups=_team_matchups(
            engine,
            missing_rebound_window=missing_rebound_window,
        ),
        stats_freshness=stats_freshness,
        settings=settings,
        injuries=_injuries(engine),
        clock=lambda: NOW,
    )
    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": SimpleNamespace(
                settings=settings,
                matchup_service=service,
                user_service=SimpleNamespace(
                    create_or_update_user=lambda _user: None
                ),
            ),
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )

    response = app.test_client().get(f"/api/games/matchup?game_id={GAME_ID}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["league"]["surface_availability"]["traditional"] == {
        "season": {"status": "available", "unavailable_reason": None},
        "last_15": {"status": "available", "unavailable_reason": None},
    }
    present_rebound_window = (
        "last_15" if missing_rebound_window == "season" else "season"
    )
    league_rows = {
        row["key"]: row
        for row in payload["league"]["defense_sheet"]["traditional"]
    }
    assert league_rows["OPP_REB"][missing_rebound_window] is None
    assert league_rows["OPP_REB"][present_rebound_window] is not None
    for key in ("OPP_TOV", "OPP_STL", "OPP_BLK"):
        assert league_rows[key]["season"] is not None
        assert league_rows[key]["last_15"] is not None
        assert payload["league"]["defensive_columns"][key]["season"] is not None
        assert payload["league"]["defensive_columns"][key]["last_15"] is not None
    for team in payload["teams"]:
        team_rows = {row["key"]: row for row in team["defense_sheet"]["traditional"]}
        assert team_rows["OPP_REB"][missing_rebound_window] is None
        assert team_rows["OPP_REB"][present_rebound_window] is not None
        for key in ("OPP_TOV", "OPP_STL", "OPP_BLK"):
            assert team_rows[key]["season"] is not None
            assert team_rows[key]["last_15"] is not None

    scores = payload["players"][0]["scores"]
    assert scores["REB"][missing_rebound_window] == {
        "components": {},
        "blend": None,
    }
    assert scores["REB"][present_rebound_window]["components"][
        "traditional"
    ] is not None
    assert scores["REB"][present_rebound_window]["blend"] is not None
    for market in ("TOV", "STL", "BLK"):
        assert scores[market]["season"]["components"]["traditional"] is not None
        assert scores[market]["last_15"]["components"]["traditional"] is not None
    assert payload["players"][0]["injury_badge_ref"] == "rotowire:6504"
    assert payload["injuries"]["status"] == "fresh"


@pytest.mark.parametrize("extra_traditional_window", ("season", "last_15"))
def test_persisted_matchup_degrades_non_rebound_traditional_identity_divergence(
    tmp_path,
    extra_traditional_window,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / f'traditional-divergence-{extra_traditional_window}.sqlite3'}"
    )
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
        team_matchups=_team_matchups(
            engine,
            extra_traditional_window=extra_traditional_window,
        ),
        stats_freshness=stats_freshness,
        settings=settings,
        injuries=_injuries(engine),
        clock=lambda: NOW,
    )
    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": SimpleNamespace(
                settings=settings,
                matchup_service=service,
                user_service=SimpleNamespace(
                    create_or_update_user=lambda _user: None
                ),
            ),
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )

    response = app.test_client().get(f"/api/games/matchup?game_id={GAME_ID}")

    assert response.status_code == 200
    payload = response.get_json()
    missing_window = (
        "last_15" if extra_traditional_window == "season" else "season"
    )
    assert payload["league"]["surface_availability"]["traditional"] == {
        extra_traditional_window: {
            "status": "available",
            "unavailable_reason": None,
        },
        missing_window: {
            "status": "unavailable",
            "unavailable_reason": "legacy_surface_incomplete",
        },
    }
    league_rows = payload["league"]["defense_sheet"]["traditional"]
    assert {row["key"] for row in league_rows} == {
        "OPP_BLK",
        "OPP_PF",
        "OPP_REB",
        "OPP_STL",
        "OPP_TOV",
    }
    assert all(row[extra_traditional_window] is not None for row in league_rows)
    assert all(row[missing_window] is None for row in league_rows)
    assert all(
        row[extra_traditional_window] is not None
        for team in payload["teams"]
        for row in team["defense_sheet"]["traditional"]
    )
    assert all(
        row[missing_window] is None
        for team in payload["teams"]
        for row in team["defense_sheet"]["traditional"]
    )
    assert all(
        row["season"] is not None and row["last_15"] is not None
        for row in payload["league"]["defense_sheet"]["shot_zones"]
    )
    assert payload["players"][0]["injury_badge_ref"] == "rotowire:6504"
    assert payload["injuries"]["status"] == "fresh"


def test_authenticated_slate_matchup_selection_journey_uses_one_activated_generation(
    tmp_path,
):
    """Exercise the public chain with activated, rollback, mixed, and L15 states."""

    engine = create_engine(f"sqlite:///{tmp_path / 'journey.sqlite3'}")
    run_migrations(engine)
    settings = RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
        nba=NBASeasonSettings(current_season=SEASON),
    )
    catalog = StatisticCatalog.load_default()
    event_catalog = _event_catalog(engine, settings)
    pool = _player_pool(engine)
    player_logs = _player_logs(engine, catalog)
    player_diets = _player_diets(engine)
    team_matchups = _team_matchups(engine)
    log_facts = player_logs.list_player_rows(SEASON, 2544)
    diet_facts = player_diets.repository.get_for_players(SEASON, (2544,)).players[2544]
    publication_service = PublicationService(engine, clock=lambda: NOW)
    publication_service.register_default_streams()
    operations = CollectionOperationsService(
        engine, publication_service=publication_service, clock=lambda: NOW
    )

    def candidate(stream_key, payload, *, publication_id, observation_id, provider="nba"):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        checksum = hashlib.sha256(encoded.encode()).hexdigest()
        with engine.begin() as connection:
            connection.execute(PublicationVersion.__table__.insert().values(
                publication_id=publication_id,
                stream_key=stream_key,
                season=SEASON,
                cutoff=NOW - timedelta(days=1),
                version=1,
                status="candidate",
                checksum=checksum,
                payload=encoded,
                created_at=NOW,
                reason="authenticated journey candidate",
                fence=0,
            ))
            connection.execute(CollectionObservation.__table__.insert().values(
                observation_id=observation_id,
                client_observation_id=observation_id,
                collector_id="journey-collector",
                manifest_id=None,
                environment="testing",
                provider=provider,
                observation_type=stream_key,
                scope=json.dumps({"stream": stream_key}),
                season=SEASON,
                cutoff=NOW - timedelta(days=1),
                schema_version=1,
                checksum=checksum,
                payload=encoded,
                payload_bytes=len(encoded.encode()),
                retrieved_at=NOW,
                accepted_at=NOW,
            ))
            connection.execute(PublicationObservation.__table__.insert().values(
                publication_id=publication_id,
                observation_id=observation_id,
                role="accepted_candidate",
                slice_key=None,
                created_at=NOW,
            ))
        return checksum

    def log_payload(points):
        row = asdict(log_facts[0])
        row["game_date"] = row["game_date"].isoformat()
        row["points"] = points
        return {"rows": [row]}

    first_log = "journey-log-first"
    second_log = "journey-log-second"
    first_log_payload = log_payload(25)
    second_log_payload = log_payload(26)
    first_log_checksum = candidate(
        "player_game_logs", first_log_payload, publication_id=first_log,
        observation_id="journey-observation-first",
    )
    second_log_checksum = candidate(
        "player_game_logs", second_log_payload, publication_id=second_log,
        observation_id="journey-observation-second",
    )
    first_parity = "journey-parity-first"
    second_parity = "journey-parity-second"
    with engine.begin() as connection:
        connection.execute(LedgerParityArtifact.__table__.insert(), [
            {
                "artifact_id": first_parity,
                "publication_id": first_log,
                "payload_checksum": first_log_checksum,
                "stream_key": "player_game_logs",
                "season": SEASON,
                "cutoff": NOW - timedelta(days=1),
                "status": "exact",
                "report": "{}",
                "created_at": NOW,
            },
            {
                "artifact_id": second_parity,
                "publication_id": second_log,
                "payload_checksum": second_log_checksum,
                "stream_key": "player_game_logs",
                "season": SEASON,
                "cutoff": NOW - timedelta(days=1),
                "status": "exact",
                "report": "{}",
                "created_at": NOW,
            },
        ])
    diet_rows = [
        {
            key: value
            for key, value in asdict(fact).items()
            if key not in {"base", "retrieved_at"}
        }
        for fact in diet_facts
        if fact.base == "play_types"
    ]
    diet_candidate = "journey-diet"
    candidate(
        "synergy_play_types", {"base": "play_types", "rows": diet_rows},
        publication_id=diet_candidate, observation_id="journey-observation-diet",
    )
    operations.activate_stream(
        "player_game_logs", actor="journey-operator", reason="activate first logs",
        season=SEASON, cutoff=NOW - timedelta(days=1),
        parity_artifact_id=first_parity, candidate_publication_id=first_log,
    )
    operations.activate_stream(
        "player_game_logs", actor="journey-operator", reason="advance logs",
        season=SEASON, cutoff=NOW - timedelta(days=1),
        parity_artifact_id=second_parity, candidate_publication_id=second_log,
    )
    operations.activate_stream(
        "synergy_play_types", actor="journey-operator", reason="activate diet",
        season=SEASON, cutoff=NOW - timedelta(days=1), candidate_publication_id=diet_candidate,
    )
    rollback = operations.rollback_publication(
        "player_game_logs", actor="journey-operator", reason="restore first log payload"
    )
    assert rollback.resource.payload == json.dumps(
        first_log_payload, sort_keys=True, separators=(",", ":")
    )
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: NOW)
    player_logs._publication_reader = reader
    player_diets.repository._publication_reader = reader
    team_matchups._publication_reader = reader
    stats_freshness = StatsFreshnessRepository(engine)
    stats_freshness.record_success(NOW)
    matchup_service = MatchupService(
        event_catalog=event_catalog,
        player_pool=pool,
        player_logs=player_logs,
        player_diets=player_diets,
        team_matchups=team_matchups,
        stats_freshness=stats_freshness,
        settings=settings,
        injuries=None,
        clock=lambda: NOW,
        publication_reader=reader,
    )
    selection_service = MatchupSelectionService(
        event_catalog=event_catalog,
        player_pool=pool,
        player_logs=player_logs,
        archetypes=PlayerArchetypeRepository(engine),
        statistic_catalog=catalog,
        settings=settings,
        publication_reader=reader,
    )
    provider_calls = {"nba": 0, "pbp": 0}

    class ProviderCounter:
        def __init__(self, name):
            self.name = name

        def __getattr__(self, operation):
            provider_calls[self.name] += 1
            raise AssertionError(f"unexpected {self.name}.{operation} request")

    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": SimpleNamespace(
                settings=settings,
                slate_service=SlateService(
                    event_catalog,
                    settings=settings,
                    player_pool=None,
                    injuries=None,
                    clock=lambda: NOW,
                ),
                matchup_service=matchup_service,
                matchup_selection_service=selection_service,
                nba_stats_provider=ProviderCounter("nba"),
                pbp_stats_provider=ProviderCounter("pbp"),
                user_service=SimpleNamespace(create_or_update_user=lambda _user: None),
            ),
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )
    client = app.test_client()

    slate_response = client.get("/api/games/slate?date=2026-01-15")
    matchup_response = client.get(f"/api/games/matchup?game_id={GAME_ID}")
    selection_response = client.get(
        f"/api/games/matchup/selection?game_id={GAME_ID}&player_id=2544"
    )

    restored_snapshot = reader.snapshot(
        ("player_game_logs", "synergy_play_types"), season=SEASON
    )
    assert restored_snapshot.read("player_game_logs").decoded[0].points == 25
    assert restored_snapshot.read("player_game_logs").publication_id == rollback.resource.publication_id
    assert restored_snapshot.generation

    assert slate_response.status_code == 200
    assert matchup_response.status_code == 200
    assert selection_response.status_code == 200
    matchup = matchup_response.get_json()
    assert matchup["provenance"]["player_game_logs"]["status"] == "rollback"
    assert matchup["provenance"]["player_game_logs"]["publication_id"] == rollback.resource.publication_id
    assert matchup["provenance"]["synergy_play_types"]["publication_id"] == diet_candidate
    assert matchup["provenance"]["synergy:l15"]["unavailable_reason"] == "provider_window_unsupported"
    assert matchup["coverage"]["mixed_cutoff"]
    assert matchup["coverage"]["mixed_freshness"]
    assert matchup["league"]["surface_availability"]["play_types"]["last_15"] == {
        "status": "unavailable",
        "unavailable_reason": "provider_window_unsupported",
    }
    assert selection_response.get_json()["h2h"]["rows"]
    assert provider_calls == {"nba": 0, "pbp": 0}


def test_recorded_projection_snapshot_serves_authenticated_slate_and_matchup_without_provider_calls(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'projection-route.sqlite3'}"
    engine = create_engine(database_url)
    run_migrations(engine)
    settings = RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
        database={"url": database_url},
        features=FeatureSettings(projection_archive_read_enabled=True),
        nba=NBASeasonSettings(current_season=SEASON),
    )
    monkeypatch.setattr("app.utils.db.get_engine", lambda _settings=None: engine)
    monkeypatch.setattr(
        "app.providers.nba_stats.NBAStatsAdapter",
        lambda **_kwargs: _NoProvider(),
    )
    monkeypatch.setattr(
        "app.providers.pbp_stats.PBPStatsAdapter",
        lambda **_kwargs: _NoProvider(),
    )
    monkeypatch.setattr(
        "app.providers.pbp_game_logs.PBPGameLogAdapter",
        lambda **_kwargs: _NoProvider(),
    )
    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )
    assembled = app.extensions["dependencies"]
    assert isinstance(
        assembled.projection_player_pool_reader,
        LatestProjectionPlayerPoolReader,
    )
    assert assembled.slate_service.player_pool is assembled.projection_player_pool_reader
    assert assembled.matchup_service.player_pool is assembled.projection_player_pool_reader

    catalog = StatisticCatalog.load_default()
    pool = assembled.projection_player_pool_reader
    event_catalog = _event_catalog(engine, settings)
    stats_freshness = StatsFreshnessRepository(engine)
    stats_freshness.record_success(NOW)
    matchup_service = MatchupService(
        event_catalog=event_catalog,
        player_pool=pool,
        player_logs=_player_logs(engine, catalog),
        player_diets=_player_diets(engine),
        team_matchups=_team_matchups(engine),
        stats_freshness=stats_freshness,
        settings=settings,
        injuries=None,
        clock=lambda: NOW,
    )
    app.extensions["dependencies"] = replace(
        assembled,
        slate_service=SlateService(
            event_catalog,
            settings=settings,
            player_pool=pool,
            injuries=None,
            clock=lambda: NOW,
        ),
        matchup_service=matchup_service,
    )
    client = app.test_client()

    missing_selection = client.get(
        f"/api/games/matchup/selection?game_id={GAME_ID}&player_id=2544"
    )
    assert missing_selection.status_code == 503
    assert missing_selection.get_json()["error"]["code"] == "provider_unavailable"

    assembled.projection_recorder.record_complete_snapshot(
        _recorded_projection_snapshot(catalog),
        query=NBAMarketQuery(season=SEASON),
        accepted_at=NOW,
    )

    slate = client.get("/api/games/slate?date=2026-01-15")
    matchup = client.get(f"/api/games/matchup?game_id={GAME_ID}")

    assert slate.status_code == 200
    assert matchup.status_code == 200
    assert slate.get_json()["games"][0]["projection_state"] == {
        "state": "live",
        "observed_at": NOW.isoformat(),
    }
    assert matchup.get_json()["freshness"]["pool"] == {
        "status": "fresh",
        "state": "live",
        "observed_at": NOW.isoformat(),
        "retrieved_at": NOW.isoformat(),
        "providers": {
            "dabble": {"status": "fresh", "retrieved_at": NOW.isoformat()}
        },
    }
    assert [player["canonical_id"] for player in matchup.get_json()["players"]] == [
        2544
    ]
