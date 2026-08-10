"""Behavioral tests for the live Player Pool assembled from DFS boards."""

from datetime import datetime, timezone
from dataclasses import fields, replace
import json
from pathlib import Path
import pytest

from app.domain.statistics import MatchReason, MatchState, ScoringPeriod, StatisticMatch
from app.providers.dfs import (
    AthleteEvidence,
    EventEvidence,
    MarketStatus,
    MarketVariant,
    PlayerProjectionMarket,
    StatisticEvidence,
    TeamEvidence,
    CoverageEvidence,
    NBAMarketQuery,
    ProviderSnapshot,
    SnapshotStatus,
)
from app.services.player_pool import PlayerPoolService
from app.services.athlete_resolver import (
    AthleteResolution,
    AthleteResolver,
    CanonicalAthlete,
    MappingResolutionState,
)
from app.services.athlete_mapping_repository import (
    BoardMappingOutcome,
    ProviderAthleteMappingRecord,
)
from app.services.dfs_board import DFSBoard, ProviderOutcome, ProviderOutcomeStatus
from app.services.event_resolver import (
    CanonicalEvent,
    EventResolution,
    EventResolutionState,
)
from app.services.event_mapping_repository import (
    BoardEventMappingOutcome,
    ProviderEventMappingRecord,
)
from app.services.statistic_catalog import StatisticCatalog, StatisticResolver
from app.services.slate_service import SlateService
from app.config.settings import NBASeasonSettings, RuntimeSettings


NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
CATALOG = StatisticCatalog.load_default()


class RecordedAthleteCatalog:
    def __init__(self, team_abbreviation="PHX"):
        self.team_abbreviation = team_abbreviation

    def get_catalog(self, season, active_only=False):
        assert season == "2025-26"
        return [
            {
                "season": season,
                "player_id": 101,
                "display_name": "Luka Dončić III",
                "roster_status": "active",
                "is_active": True,
                "is_active_for_season": True,
                "team_id": 1,
                "team_name": "Phoenix Suns",
                "team_abbreviation": self.team_abbreviation,
            }
        ]


class RecordedBoardService:
    def __init__(self, board):
        self.board = board
        self.queries = []

    def get_board(self, query):
        self.queries.append(query)
        return self.board


class RecordedTelemetry:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


def _market(
    provider,
    athlete_id,
    category,
    *,
    event_id="game-1",
    status=MarketStatus.AVAILABLE,
    variant=MarketVariant.STANDARD,
    period=ScoringPeriod.FULL_GAME,
    unknown_label="Mystery Stat",
):
    match = None
    if category is not None:
        match = StatisticMatch(
            state=MatchState.CANONICAL,
            evidence=StatisticEvidence(label=category),
            provider=provider,
            scoring_period=period,
            canonical=CATALOG.by_id[category],
        )
    else:
        match = StatisticMatch(
            state=MatchState.UNMAPPED,
            evidence=StatisticEvidence(label=unknown_label),
            provider=provider,
            scoring_period=period,
            reason=MatchReason.UNKNOWN_PROVIDER_LABEL,
        )
    return PlayerProjectionMarket(
        provider=provider,
        athlete=AthleteEvidence(provider_id=athlete_id, name="Board Name"),
        event=EventEvidence(provider_id=event_id),
        statistic=StatisticEvidence(label=category or unknown_label),
        statistic_match=match,
        status=status,
        variant=variant,
        scoring_period=period,
    )


def _athlete_outcome(provider, provider_id, player_id, team_id, name):
    evidence = AthleteEvidence(provider_id=provider_id, name=name)
    canonical = CanonicalAthlete(
        season="2025-26", player_id=player_id, display_name=name,
        roster_status="Active", is_active=True, is_active_for_season=True,
        team_id=team_id,
    )
    resolution = AthleteResolution(
        provider=provider, provider_evidence=evidence, season="2025-26",
        state=MappingResolutionState.AUTO, canonical_athlete=canonical,
    )
    mapping = None
    if provider_id:
        mapping = _record(
            ProviderAthleteMappingRecord, provider=provider,
            provider_athlete_id=provider_id, mapping_state="auto", is_active=True,
            season="2025-26", canonical_player_id=player_id,
            canonical_name=name, canonical_team_id=team_id,
            first_seen_at=NOW.isoformat(), last_seen_at=NOW.isoformat(),
        )
    return BoardMappingOutcome(
        resolution=resolution, state=MappingResolutionState.AUTO,
        persisted=True, mapping=mapping,
    )


def _event_outcome(provider, provider_id, game_id, team_ids=(1, 2)):
    evidence = EventEvidence(provider_id=provider_id)
    resolution = EventResolution(
        provider=provider, provider_evidence=evidence, season="2025-26",
        state=EventResolutionState.AUTO,
        canonical_event=CanonicalEvent(
            game_id, "2025-26", None,
            home_team_id=team_ids[0], away_team_id=team_ids[1],
        ),
    )
    mapping = None
    if provider_id:
        mapping = _record(
            ProviderEventMappingRecord, provider=provider,
            provider_event_id=provider_id, mapping_state="auto", is_active=True,
            season="2025-26", canonical_event_id=game_id,
            first_seen_at=NOW.isoformat(), last_seen_at=NOW.isoformat(),
        )
    return BoardEventMappingOutcome(
        resolution=resolution, state=EventResolutionState.AUTO,
        persisted=True, mapping=mapping,
    )


def _provider_outcome(provider, markets=None, *, failed=False):
    if failed:
        return ProviderOutcome(provider, ProviderOutcomeStatus.FAILED, reason="timeout")
    snapshot = ProviderSnapshot(
        provider=provider, status=SnapshotStatus.COMPLETE,
        markets=tuple(markets or ()), coverage=CoverageEvidence(), retrieved_at=NOW,
    )
    return ProviderOutcome(provider, ProviderOutcomeStatus.COMPLETE, snapshot=snapshot)


def _record(record_type, **values):
    return record_type(**{field.name: values.get(field.name) for field in fields(record_type)})


def _board(provider_outcomes, athlete_outcomes=(), event_outcomes=()):
    return DFSBoard(
        query=NBAMarketQuery(season="2025-26"),
        provider_outcomes=tuple(sorted(provider_outcomes, key=lambda item: item.provider)), disabled_providers=(),
        generated_at=NOW, mapping_outcomes=tuple(athlete_outcomes),
        event_mapping_outcomes=tuple(event_outcomes),
    )


@pytest.mark.parametrize(("provider_team", "canonical_team"), [("PHO", "PHX"), ("NO", "NOP")])
def test_canonical_join_normalizes_diacritics_suffix_order_team_dialect_and_season(
    provider_team, canonical_team
):
    resolved = AthleteResolver(RecordedAthleteCatalog(canonical_team)).resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id="pp-1",
            name="III, Luka Doncic",
            team=TeamEvidence(abbreviation=provider_team),
        ),
        "2025-26",
    )

    assert resolved.canonical_player_id == 101
    assert resolved.season == "2025-26"


def test_recorded_provider_labels_map_to_supported_market_categories():
    fixture = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/player_pool/provider_labels.json"
        ).read_text()
    )
    resolver = StatisticResolver(StatisticCatalog.load_default())

    resolved = {
        row["category"]: resolver.resolve(
            row["provider"],
            row["label"],
            scoring_period=ScoringPeriod.FULL_GAME,
            unit="count",
        ).canonical_id
        for row in fixture
    }

    assert resolved == {
        "PTS": "points",
        "PRA": "pra",
        "FGA": "field_goals_attempted",
        "FG3A": "three_pointers_attempted",
        "STKS": "stks",
        "FG2A": "two_pointers_attempted",
    }


def test_pool_unions_categories_and_provenance_by_canonical_player():
    board = _board(
        (
            _provider_outcome("prizepicks", [_market("prizepicks", "pp-1", "points")]),
            _provider_outcome("underdog", [_market("underdog", "ud-1", "assists")]),
            _provider_outcome(
                "dabble", [_market("dabble", "db-2", "field_goals_attempted")]
            ),
        ),
        athlete_outcomes=(
            _athlete_outcome("prizepicks", "pp-1", 101, 1, "Luka Dončić"),
            _athlete_outcome("underdog", "ud-1", 101, 1, "Luka Dončić"),
            _athlete_outcome("dabble", "db-2", 202, 2, "Gary Trent Jr."),
        ),
        event_outcomes=(
            _event_outcome("prizepicks", "game-1", "0022500001"),
            _event_outcome("underdog", "game-1", "0022500001"),
            _event_outcome("dabble", "game-1", "0022500001"),
        ),
    )
    service = PlayerPoolService(RecordedBoardService(board), CATALOG)

    pool = service.get_pool(season="2025-26", game_ids={"0022500001"})

    assert pool.team_counts == {1: 1, 2: 1}
    assert pool.players[0].canonical_player_id == 101
    assert pool.players[0].market_categories == ("AST", "PTS")
    assert pool.players[0].provenance == {
        "prizepicks": ("PTS",),
        "underdog": ("AST",),
    }
    assert pool.players[1].market_categories == ("FGA",)


def test_pool_excludes_nonqualifying_unknown_unjoined_and_other_slate_markets():
    markets = [
        _market("prizepicks", "joined", "points"),
        _market("prizepicks", "joined", None, unknown_label="Mystery Stat"),
        _market("prizepicks", "joined", None, unknown_label="New Stat"),
        _market("prizepicks", "joined", "points", status=MarketStatus.SUSPENDED),
        _market("prizepicks", "joined", "points", variant=MarketVariant.ALTERNATE),
        _market("prizepicks", "joined", "points", period=ScoringPeriod.FIRST_HALF),
        _market("prizepicks", "missing", "rebounds"),
        _market("prizepicks", "joined", "assists", event_id="other-game"),
    ]
    board = _board(
        (_provider_outcome("prizepicks", markets),),
        athlete_outcomes=(
            _athlete_outcome("prizepicks", "joined", 101, 1, "Luka Dončić"),
        ),
        event_outcomes=(
            _event_outcome("prizepicks", "game-1", "0022500001"),
            _event_outcome("prizepicks", "other-game", "0022500002"),
        ),
    )
    telemetry = RecordedTelemetry()

    pool = PlayerPoolService(
        RecordedBoardService(board), CATALOG, telemetry_recorder=telemetry
    ).get_pool(season="2025-26", game_ids={"0022500001"})

    assert pool.team_counts == {1: 1}
    assert pool.players[0].market_categories == ("PTS",)
    assert telemetry.events[-1].unknown_stat_label_count == 2
    assert telemetry.events[-1].unjoined_athlete_count == 1
    assert telemetry.events[-1].unjoined_event_count == 0
    assert telemetry.events[-1].team_mismatch_count == 0


def test_null_provider_identities_never_join_or_collide():
    markets = (
        _market("prizepicks", None, "points", event_id=None),
        _market("prizepicks", None, "assists", event_id=None),
    )
    board = _board(
        (_provider_outcome("prizepicks", markets),),
        athlete_outcomes=(
            _athlete_outcome("prizepicks", None, 101, 1, "First Player"),
            _athlete_outcome("prizepicks", None, 202, 2, "Second Player"),
        ),
        event_outcomes=(
            _event_outcome("prizepicks", None, "0022500001"),
            _event_outcome("prizepicks", None, "0022500002"),
        ),
    )
    telemetry = RecordedTelemetry()

    pool = PlayerPoolService(
        RecordedBoardService(board), CATALOG, telemetry_recorder=telemetry
    ).get_pool(season="2025-26", game_ids={"0022500001", "0022500002"})

    assert pool.players == ()
    assert telemetry.events[-1].unjoined_event_count == 2

    with pytest.raises(ValueError, match="non-empty identifier"):
        AthleteEvidence(provider_id="")
    with pytest.raises(ValueError, match="non-empty identifier"):
        EventEvidence(provider_id=" ")


def test_drop_telemetry_is_slate_scoped_and_distinguishes_unjoined_events():
    markets = (
        _market("prizepicks", "joined", None, event_id="current"),
        _market("prizepicks", "joined", None, event_id="other"),
        _market("prizepicks", "joined", None, event_id="missing"),
    )
    board = _board(
        (_provider_outcome("prizepicks", markets),),
        event_outcomes=(
            _event_outcome("prizepicks", "current", "0022500001"),
            _event_outcome("prizepicks", "other", "0022500002"),
        ),
    )
    telemetry = RecordedTelemetry()

    PlayerPoolService(
        RecordedBoardService(board), CATALOG, telemetry_recorder=telemetry
    ).get_pool(season="2025-26", game_ids={"0022500001"})

    assert telemetry.events[-1].unknown_stat_label_count == 1
    assert telemetry.events[-1].unjoined_event_count == 1


def test_joined_athlete_on_neither_governed_game_team_is_excluded_and_counted():
    market = _market("prizepicks", "traded", "points")
    board = _board(
        (_provider_outcome("prizepicks", (market,)),),
        athlete_outcomes=(
            _athlete_outcome("prizepicks", "traded", 303, 3, "Traded Player"),
        ),
        event_outcomes=(
            _event_outcome("prizepicks", "game-1", "0022500001", (1, 2)),
        ),
    )
    telemetry = RecordedTelemetry()

    pool = PlayerPoolService(
        RecordedBoardService(board), CATALOG, telemetry_recorder=telemetry
    ).get_pool(season="2025-26", game_ids={"0022500001"})

    assert pool.players == ()
    assert telemetry.events[-1].team_mismatch_count == 1


def test_mapped_noncomparable_market_category_still_qualifies():
    definition = {
        "schema_version": 1,
        "statistics": [{
            "id": "points", "label": "Points", "unit": "count",
            "scoring_periods": ["full_game"], "components": ["points"],
            "market_category": "PTS", "comparable": False,
            "provider_mappings": {"prizepicks": ["Points"]},
        }],
    }
    catalog = StatisticCatalog.from_mapping(definition)
    market = replace(
        _market("prizepicks", "pp-1", "points"),
        statistic_match=StatisticMatch(
            state=MatchState.CANONICAL,
            evidence=StatisticEvidence(label="Points"),
            provider="prizepicks",
            scoring_period=ScoringPeriod.FULL_GAME,
            canonical=catalog.by_id["points"],
        ),
    )
    board = _board(
        (_provider_outcome("prizepicks", (market,)),),
        athlete_outcomes=(
            _athlete_outcome("prizepicks", "pp-1", 101, 1, "Player"),
        ),
        event_outcomes=(
            _event_outcome("prizepicks", "game-1", "0022500001"),
        ),
    )

    pool = PlayerPoolService(RecordedBoardService(board), catalog).get_pool(
        season="2025-26", game_ids={"0022500001"}
    )

    assert pool.players[0].market_categories == ("PTS",)


def test_pool_freshness_is_truthful_for_empty_success_partial_failure_and_total_failure():
    all_fresh = _board(
        (_provider_outcome("prizepicks"), _provider_outcome("underdog"))
    )
    fresh = PlayerPoolService(RecordedBoardService(all_fresh), CATALOG).get_pool(
        season="2025-26", game_ids=set()
    )
    assert fresh.freshness["status"] == "fresh"

    empty = _board(
        (
            _provider_outcome("prizepicks"),
            _provider_outcome("underdog", failed=True),
        ),
    )
    pool = PlayerPoolService(RecordedBoardService(empty), CATALOG).get_pool(
        season="2025-26", game_ids=set()
    )

    assert pool.freshness == {
        "retrieved_at": NOW.isoformat(),
        "providers": {
            "prizepicks": {"status": "fresh", "retrieved_at": NOW.isoformat()},
            "underdog": {"status": "missing", "retrieved_at": None},
        },
    }

    failed = _board((_provider_outcome("prizepicks", failed=True),))
    unavailable = PlayerPoolService(RecordedBoardService(failed), CATALOG).get_pool(
        season="2025-26", game_ids=set()
    )
    assert unavailable.freshness["status"] == "unavailable"
    assert unavailable.freshness["retrieved_at"] is None


def test_player_pool_market_categories_are_derived_from_the_injected_catalog():
    service = PlayerPoolService(RecordedBoardService(_board(())), CATALOG)

    assert service.market_categories == {
        statistic.id: statistic.market_category
        for statistic in CATALOG.statistics
        if statistic.market_category is not None
    }


def test_authenticated_slate_route_serves_real_governed_player_and_event_joins(
    client, dependencies
):
    market = _market("prizepicks", "pp-1", "points")
    board = _board(
        (_provider_outcome("prizepicks", (market,)),),
        athlete_outcomes=(
            _athlete_outcome("prizepicks", "pp-1", 101, 1, "Luka Dončić"),
        ),
        event_outcomes=(
            _event_outcome("prizepicks", "game-1", "0022500001"),
        ),
    )

    class Catalog:
        def count_events(self, season):
            return 1

        def get_freshness(self, season, *, now):
            return {"last_success_at": NOW.isoformat()}

        def get_events_between(self, season, starts_at, ends_at):
            return [{
                "nba_game_id": "0022500001",
                "scheduled_at": "2026-01-03T00:00:00+00:00",
                "status_text": "7:00 pm ET",
                "status_code": 1,
                "is_postponed": False,
                "classification": "Regular Season",
                "away_team": {"id": 1, "name": "Phoenix Suns", "tricode": "PHX"},
                "home_team": {"id": 2, "name": "Home", "tricode": "HME"},
            }]

    dependencies.slate_service = SlateService(
        Catalog(),
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season="2025-26"),
        ),
        clock=lambda: NOW,
        player_pool=PlayerPoolService(RecordedBoardService(board), CATALOG),
    )

    response = client.get("/api/games/slate?date=2026-01-02")

    assert response.status_code == 200
    assert response.get_json()["games"][0]["away_team"]["targetable_player_count"] == 1
