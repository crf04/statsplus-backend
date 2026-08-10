"""Behavioral tests for the live Player Pool assembled from DFS boards."""

from datetime import datetime, timezone
import json
from pathlib import Path
import pytest
from types import SimpleNamespace

from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.providers.dfs import (
    AthleteEvidence,
    EventEvidence,
    MarketStatus,
    MarketVariant,
    PlayerProjectionMarket,
    StatisticEvidence,
    TeamEvidence,
)
from app.services.player_pool import PlayerPoolService
from app.services.athlete_resolver import AthleteResolver
from app.services.statistic_catalog import StatisticCatalog, StatisticResolver


NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)


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
):
    match = None
    if category is not None:
        canonical = SimpleNamespace(
            id=category,
            label=category,
            unit=SimpleNamespace(value="count"),
            scoring_periods=(ScoringPeriod.FULL_GAME,),
            components=(category,),
            comparable=True,
        )
        match = StatisticMatch(
            state=MatchState.CANONICAL,
            evidence=StatisticEvidence(label=category),
            provider=provider,
            scoring_period=ScoringPeriod.FULL_GAME,
            canonical=canonical,
        )
    return PlayerProjectionMarket(
        provider=provider,
        athlete=AthleteEvidence(provider_id=athlete_id, name="Board Name"),
        event=EventEvidence(provider_id=event_id),
        statistic=StatisticEvidence(label=category or "Mystery Stat"),
        statistic_match=match,
        status=status,
        variant=variant,
        scoring_period=period,
    )


def _athlete_outcome(provider, provider_id, player_id, team_id, name):
    canonical = SimpleNamespace(player_id=player_id, team_id=team_id, display_name=name)
    return SimpleNamespace(
        provider=provider,
        provider_athlete_id=provider_id,
        canonical_player_id=player_id,
        resolution=SimpleNamespace(canonical_athlete=canonical),
    )


def _event_outcome(provider, provider_id, game_id):
    return SimpleNamespace(
        provider=provider,
        provider_event_id=provider_id,
        canonical_event_id=game_id,
        resolution=SimpleNamespace(provider_event_id=provider_id),
    )


def _provider_outcome(provider, markets=None, *, failed=False):
    snapshot = None
    if not failed:
        snapshot = SimpleNamespace(
            markets=tuple(markets or ()), retrieved_at=NOW, provider=provider
        )
    return SimpleNamespace(provider=provider, usable=not failed, snapshot=snapshot)


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
    board = SimpleNamespace(
        provider_outcomes=(
            _provider_outcome("prizepicks", [_market("prizepicks", "pp-1", "points")]),
            _provider_outcome("underdog", [_market("underdog", "ud-1", "assists")]),
            _provider_outcome(
                "dabble", [_market("dabble", "db-2", "field_goals_attempted")]
            ),
        ),
        mapping_outcomes=(
            _athlete_outcome("prizepicks", "pp-1", 101, 1, "Luka Dončić"),
            _athlete_outcome("underdog", "ud-1", 101, 1, "Luka Dončić"),
            _athlete_outcome("dabble", "db-2", 202, 2, "Gary Trent Jr."),
        ),
        event_mapping_outcomes=(
            _event_outcome("prizepicks", "game-1", "0022500001"),
            _event_outcome("underdog", "game-1", "0022500001"),
            _event_outcome("dabble", "game-1", "0022500001"),
        ),
    )
    service = PlayerPoolService(RecordedBoardService(board))

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
        _market("prizepicks", "joined", None),
        _market("prizepicks", "joined", "points", status=MarketStatus.SUSPENDED),
        _market("prizepicks", "joined", "points", variant=MarketVariant.ALTERNATE),
        _market("prizepicks", "joined", "points", period=ScoringPeriod.FIRST_HALF),
        _market("prizepicks", "missing", "rebounds"),
        _market("prizepicks", "joined", "assists", event_id="other-game"),
    ]
    board = SimpleNamespace(
        provider_outcomes=(_provider_outcome("prizepicks", markets),),
        mapping_outcomes=(
            _athlete_outcome("prizepicks", "joined", 101, 1, "Luka Dončić"),
        ),
        event_mapping_outcomes=(
            _event_outcome("prizepicks", "game-1", "0022500001"),
            _event_outcome("prizepicks", "other-game", "0022500002"),
        ),
    )
    telemetry = RecordedTelemetry()

    pool = PlayerPoolService(
        RecordedBoardService(board), telemetry_recorder=telemetry
    ).get_pool(season="2025-26", game_ids={"0022500001"})

    assert pool.team_counts == {1: 1}
    assert pool.players[0].market_categories == ("PTS",)
    assert telemetry.events[-1].unknown_stat_label_count == 1
    assert telemetry.events[-1].unjoined_athlete_count == 1


def test_pool_freshness_is_truthful_for_empty_success_partial_failure_and_total_failure():
    empty = SimpleNamespace(
        provider_outcomes=(
            _provider_outcome("prizepicks"),
            _provider_outcome("underdog", failed=True),
        ),
        mapping_outcomes=(),
        event_mapping_outcomes=(),
    )
    pool = PlayerPoolService(RecordedBoardService(empty)).get_pool(
        season="2025-26", game_ids=set()
    )

    assert pool.freshness == {
        "status": "fresh",
        "retrieved_at": NOW.isoformat(),
        "providers": {
            "prizepicks": {"status": "fresh", "retrieved_at": NOW.isoformat()},
            "underdog": {"status": "missing", "retrieved_at": None},
        },
    }

    failed = SimpleNamespace(
        provider_outcomes=(_provider_outcome("prizepicks", failed=True),),
        mapping_outcomes=(),
        event_mapping_outcomes=(),
    )
    unavailable = PlayerPoolService(RecordedBoardService(failed)).get_pool(
        season="2025-26", game_ids=set()
    )
    assert unavailable.freshness["status"] == "unavailable"
    assert unavailable.freshness["retrieved_at"] is None
