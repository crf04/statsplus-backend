"""The shared provenance block every MCP-read route returns (crf04/statsplus#107).

Each test serves one route through the Flask test client from the route's real
service over one real database of seeded Publications, and asserts the
``provenance`` block's literal values:

* ``player_game_logs`` is composed two hours before the reader's clock under
  the one-hour ``cutoff_current`` rule, so it is ``stale``;
* ``synergy_play_types`` was published ten minutes earlier, so it is ``fresh``;
* ``synergy_play_types_opponent_season`` is a fresh row with no Event Catalog
  authority: the reader refuses it but keeps its age-based ``fresh`` label,
  and the block must report it ``unavailable``;
* ``player_per36`` is registered with nothing published (``missing``), and
  every other stream is not registered at all: both are ``unavailable``;
* ``synergy:l15`` is a permanently unsupported window and is not listed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config.settings import RuntimeSettings
from app.domain.publication_integrity import (
    canonical_publication_json,
    publication_payload_checksum,
)
from app.models.collection_control import PublicationPointer, PublicationVersion
from app.services.collection_control import PublicationService
from app.services.slate_service import SlateService
from app.services.statistic_catalog import StatisticCatalog
from app.services.target_backtest import TargetBacktestService
from app.services.target_preview import TargetPreviewService
from app.services.target_resolution import TargetResolutionService
from app.services.user_service import UserService
from tests import test_target_backtest as bt
from tests import test_target_resolution as res
from tests.services import test_matchup_service as m


NOW = m.NOW  # 2026-01-15T12:00:00Z, the reader's clock
SEASON = m.SEASON
GAME_ID = m.GAME_ID
SLATE_DATE = "2026-01-15"

#: Every stream a Matchup reads, as the block lists them: sorted, without the
#: permanently unsupported ``synergy:l15``.
MATCHUP_STREAMS = [
    "assist_locations_l15",
    "assist_locations_season",
    "exact_shot_zones",
    "exact_shot_zones_opponent_l15",
    "exact_shot_zones_opponent_season",
    "grouped_shot_types",
    "grouped_shot_types_opponent_l15",
    "grouped_shot_types_opponent_season",
    "player_assist_locations",
    "player_game_logs",
    "player_per36",
    "synergy_play_types",
    "synergy_play_types_opponent_l15",
    "synergy_play_types_opponent_season",
    "traditional_opponent_l15",
    "traditional_opponent_season",
]
#: Game logs: the player's logs, the four Diets, and the Season Defense Sheet
#: every row's PLAYTYPE_RTG and every Team Filter rank from.
GAME_LOG_STREAMS = [
    "assist_locations_season",
    "exact_shot_zones",
    "exact_shot_zones_opponent_season",
    "grouped_shot_types",
    "grouped_shot_types_opponent_season",
    "player_assist_locations",
    "player_game_logs",
    "synergy_play_types",
    "synergy_play_types_opponent_season",
    "traditional_opponent_season",
]


def _hand_seed(engine, stream_key, payload, *, publication_id, created_at, cutoff):
    """Register one stream and insert one active publication and pointer.

    The rows are exactly what an activation writes; governed composition is
    bypassed, so a stream the reader verifies against Event Catalog authority
    is refused as ``publication_authority_invalid``.
    """

    encoded = canonical_publication_json(payload)
    checksum = publication_payload_checksum(encoded)
    PublicationService(engine, clock=lambda: created_at).register_stream(
        stream_key,
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
        freshness_rule="cutoff_current",
    )
    with engine.begin() as connection:
        connection.execute(
            PublicationVersion.__table__.insert().values(
                publication_id=publication_id,
                stream_key=stream_key,
                season=SEASON,
                cutoff=cutoff,
                version=1,
                status="active",
                checksum=checksum,
                payload=encoded,
                created_at=created_at,
                fence=0,
            )
        )
        connection.execute(
            PublicationPointer.__table__.insert().values(
                stream_key=stream_key,
                active_publication_id=publication_id,
                previous_publication_id=None,
                fence=0,
                updated_at=created_at,
            )
        )
    return checksum


@pytest.fixture
def world(tmp_path):
    engine, matchups, logs = m._published_matchup_service(
        tmp_path,
        [m._log_row(game_id=GAME_ID, game_date="2026-01-14", points=31, minutes=34.0)],
    )
    play_types = _hand_seed(
        engine,
        "synergy_play_types",
        {
            "rows": [
                {
                    "player_id": 2544,
                    "slice_key": "Transition",
                    "share": 0.32,
                    "volume": 480.0,
                    "games_played": 60,
                    "volume_unit": "possessions",
                    "provider": "nba_synergy",
                }
            ]
        },
        publication_id="pub-play-types",
        created_at=NOW - timedelta(minutes=10),
        cutoff=datetime(2026, 1, 15, 8, tzinfo=timezone.utc),
    )
    defense = _hand_seed(
        engine,
        "synergy_play_types_opponent_season",
        {"rows": []},
        publication_id="pub-play-type-defense",
        created_at=NOW - timedelta(minutes=5),
        cutoff=datetime(2026, 1, 15, 9, tzinfo=timezone.utc),
    )
    PublicationService(engine, clock=lambda: NOW).register_stream(
        "player_per36",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
        freshness_rule="daily_recheck",
    )
    return SimpleNamespace(
        engine=engine,
        matchups=matchups,
        reader=matchups.publication_reader,
        expected=_expected(logs, play_types=play_types, defense=defense),
    )


def _unregistered(stream_key):
    return {
        "stream_key": stream_key,
        "publication_id": None,
        "season": None,
        "coverage_cutoff": None,
        "version": None,
        "status": "unavailable",
        "freshness": "unavailable",
        "age_seconds": None,
        "source": "database",
        "legacy_fallback_allowed": False,
        "payload_checksum": None,
        "retrieved_at": None,
        "fence": None,
        "unavailable_reason": "stream_not_registered",
        "manifest_id": None,
        "event_catalog_publication_id": None,
        "event_catalog_checksum": None,
    }


def _expected(logs, *, play_types, defense):
    """Each stream's generation entry, from the values seeded above."""

    expected = {key: _unregistered(key) for key in MATCHUP_STREAMS}
    expected["player_game_logs"] = {
        **_unregistered("player_game_logs"),
        "publication_id": logs.publication_id,
        "season": "2025-26",
        "coverage_cutoff": "2026-01-15T10:00:00+00:00",
        "version": 1,
        "status": "active",
        "freshness": "stale",
        "age_seconds": 7200,
        "payload_checksum": logs.checksum,
        "retrieved_at": "2026-01-15T10:00:00+00:00",
        "fence": 1,
        "unavailable_reason": None,
    }
    expected["synergy_play_types"] = {
        **_unregistered("synergy_play_types"),
        "publication_id": "pub-play-types",
        "season": "2025-26",
        "coverage_cutoff": "2026-01-15T08:00:00+00:00",
        "version": 1,
        "status": "active",
        "freshness": "fresh",
        "age_seconds": 600,
        "payload_checksum": play_types,
        "retrieved_at": "2026-01-15T11:50:00+00:00",
        "fence": 0,
        "unavailable_reason": None,
    }
    expected["synergy_play_types_opponent_season"] = {
        **_unregistered("synergy_play_types_opponent_season"),
        "publication_id": "pub-play-type-defense",
        "season": "2025-26",
        "coverage_cutoff": "2026-01-15T09:00:00+00:00",
        "version": 1,
        "status": "unavailable",
        # The reader labels this refused row ``fresh`` by age; the block
        # reports what the read could serve.
        "freshness": "unavailable",
        "age_seconds": 300,
        "payload_checksum": defense,
        "fence": 0,
        "unavailable_reason": "publication_authority_invalid",
    }
    expected["player_per36"] = {
        **_unregistered("player_per36"),
        "status": "missing",
        "unavailable_reason": None,
    }
    return expected


# --- Matchup ---------------------------------------------------------------


def test_the_matchup_returns_the_generation_it_read_and_its_sources(
    client, dependencies, authenticate, world
):
    dependencies.matchup_service = world.matchups

    response = client.get(f"/api/games/matchup?game_id={GAME_ID}", headers=authenticate())

    assert response.status_code == 200
    body = response.get_json()
    provenance = body["provenance"]
    assert provenance["generation"] == [world.expected[key] for key in MATCHUP_STREAMS]
    assert provenance["sources"] == {
        "schedule": {"status": "fresh", "retrieved_at": "2026-01-15T10:00:00+00:00"},
        "pool": {"status": "fresh", "retrieved_at": "2026-01-15T10:00:00+00:00"},
        "injuries": {"status": "unavailable", "retrieved_at": None},
    }
    # The existing stream-keyed map, coverage and freshness are unchanged:
    # the refused defense row keeps the reader's own age label there.
    assert sorted(body) == [
        "coverage", "experience", "freshness", "game", "injuries", "league",
        "players", "provenance", "teams",
    ]
    assert sorted(provenance) == sorted(
        [*MATCHUP_STREAMS, "synergy:l15", "generation", "sources"]
    )
    assert provenance["player_game_logs"] == world.expected["player_game_logs"]
    assert provenance["synergy_play_types_opponent_season"]["freshness"] == "fresh"
    assert provenance["synergy:l15"]["unavailable_reason"] == "provider_window_unsupported"
    assert body["coverage"] == {
        "mixed_cutoff": True,
        "mixed_freshness": True,
        "coverage_cutoffs": [
            "2026-01-15T08:00:00+00:00",
            "2026-01-15T09:00:00+00:00",
            "2026-01-15T10:00:00+00:00",
        ],
        "source": "database",
    }


def test_the_unscheduled_matchup_returns_its_generation_and_no_sources(
    client, dependencies, authenticate, world
):
    from tests.routes.test_unscheduled_matchup import (
        CATALOG_ROWS,
        _RecordedAthleteCatalog,
    )

    world.matchups.athlete_catalog = _RecordedAthleteCatalog(CATALOG_ROWS)
    world.matchups.team_matchups.publication_season_is_complete = (
        lambda season, **_kwargs: False
    )
    dependencies.matchup_service = world.matchups

    response = client.get(
        "/api/matchups/unscheduled?player_id=2544&opponent=BOS", headers=authenticate()
    )

    assert response.status_code == 200
    provenance = response.get_json()["provenance"]
    assert provenance["generation"] == [world.expected[key] for key in MATCHUP_STREAMS]
    assert provenance["sources"] == {}
    assert provenance["player_game_logs"] == world.expected["player_game_logs"]


# --- Game logs -------------------------------------------------------------


def test_game_logs_return_the_generation_they_read(
    client, dependencies, authenticate, world, monkeypatch
):
    from app.services import game_service as game_service_module
    from tests.services.test_game_log_playtype_rating import _game_logs

    monkeypatch.setattr(game_service_module, "get_redis_client", lambda *a, **k: None)
    service = game_service_module.GameService(
        world.engine,
        settings=RuntimeSettings(
            environment="testing", nba=m.NBASeasonSettings(current_season=SEASON)
        ),
        publication_reader=world.reader,
    )
    monkeypatch.setattr(service, "_get_game_logs", lambda name, season: (_game_logs(), None))
    dependencies.game_service = service

    response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James&season_filter=2025-26",
        headers=authenticate(),
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["provenance"] == {
        "generation": [world.expected[key] for key in GAME_LOG_STREAMS],
        "sources": {},
    }
    assert sorted(body) == [
        "averages", "game_logs", "next_game", "provenance", "season_averages",
        "season_game_count",
    ]
    assert body["season_game_count"] == 3


# --- Slate -----------------------------------------------------------------


class _Catalog:
    """An Event Catalog collected 30 hours before the Slate is read."""

    def count_events(self, season):
        return 1

    def get_freshness(self, season, *, now):
        return {"last_success_at": "2026-01-14T06:00:00+00:00", "event_count": 1}

    def get_events_between(self, season, starts_at, ends_at):
        return []


def test_the_slate_reads_no_publication_and_states_its_sources(
    client, dependencies, authenticate
):
    dependencies.slate_service = SlateService(
        _Catalog(),
        settings=RuntimeSettings(
            environment="testing", nba=m.NBASeasonSettings(current_season=SEASON)
        ),
        clock=lambda: datetime(2026, 1, 15, 12, tzinfo=timezone.utc),
        schedule_max_age=timedelta(hours=24),
    )

    response = client.get(f"/api/games/slate?date={SLATE_DATE}", headers=authenticate())

    assert response.status_code == 200
    assert response.get_json() == {
        "slate_date": SLATE_DATE,
        "freshness": {
            "schedule": {"status": "stale", "retrieved_at": "2026-01-14T06:00:00+00:00"},
            "pool": {"status": "unavailable", "retrieved_at": None, "providers": {}},
        },
        "games": [],
        "provenance": {
            "generation": [],
            "sources": {
                "schedule": {
                    "status": "stale",
                    "retrieved_at": "2026-01-14T06:00:00+00:00",
                },
                "pool": {"status": "unavailable", "retrieved_at": None},
            },
        },
    }


# --- Targets ---------------------------------------------------------------


SLATE_SOURCES = {
    "schedule": {"status": "stale", "retrieved_at": "2026-01-14T06:00:00+00:00"},
    "pool": {"status": "fresh", "retrieved_at": "2026-01-15T10:00:00+00:00"},
}


class _Slate:
    """Tonight's Slate: LAL @ BOS, the game the seeded Matchup composes."""

    def get_slate(self, requested_date=None):
        return {
            "slate_date": requested_date or SLATE_DATE,
            "freshness": {
                "schedule": dict(SLATE_SOURCES["schedule"]),
                "pool": {**SLATE_SOURCES["pool"], "providers": {}},
            },
            "games": [
                res._game(
                    game_id=GAME_ID,
                    away=(m.LAL, "LAL", "Los Angeles Lakers"),
                    home=(m.BOS, "BOS", "Boston Celtics"),
                )
            ],
        }


def _target(opponent):
    return {
        "id": f"target-{opponent}",
        "opponent": opponent,
        "title": f"{opponent} vs Corner 3",
        "note": None,
        "qualifiers": [res.CORNER_THREE],
        "conditions": None,
        "stat_preferences": None,
    }


def _resolution(world, *targets):
    return TargetResolutionService(
        targets=SimpleNamespace(list_targets=lambda uid: list(targets)),
        slates=_Slate(),
        matchups=world.matchups,
        publication_reader=world.reader,
        injuries=res._StoredNoInjuries(),
        settings=res._resolution_settings(),
    )


def test_resolve_returns_the_one_generation_its_matchups_read(
    client, dependencies, authenticate, world
):
    dependencies.target_resolution_service = _resolution(
        world, _target("BOS"), _target("MIA")
    )

    response = client.get(
        f"/api/user/targets/resolve?date={SLATE_DATE}", headers=authenticate()
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["provenance"] == {
        "generation": [world.expected[key] for key in MATCHUP_STREAMS],
        "sources": SLATE_SOURCES,
    }
    assert sorted(body) == ["provenance", "slate_date", "success", "targets"]
    assert [target["game"] is None for target in body["targets"]] == [False, True]


def test_resolve_with_only_idle_targets_read_no_generation(
    client, dependencies, authenticate, world
):
    dependencies.target_resolution_service = _resolution(world, _target("MIA"))

    response = client.get(
        f"/api/user/targets/resolve?date={SLATE_DATE}", headers=authenticate()
    )

    assert response.status_code == 200
    assert response.get_json()["provenance"] == {
        "generation": [],
        "sources": SLATE_SOURCES,
    }


def test_the_preview_returns_its_one_union_generation(
    client, dependencies, authenticate, world
):
    seams = bt._two_games()
    settings = res._resolution_settings()
    dependencies.user_service.validate_target_draft = Mock(
        side_effect=UserService(
            db_engine=Mock(), settings=RuntimeSettings()
        ).validate_target_draft
    )
    dependencies.target_preview_service = TargetPreviewService(
        backtests=TargetBacktestService(
            targets=object(),
            player_logs=seams["logs"],
            player_diets=seams["diets"],
            statistic_catalog=StatisticCatalog.load_default(),
            settings=settings,
            publication_reader=world.reader,
        ),
        resolutions=TargetResolutionService(
            targets=object(), slates=_Slate(), matchups=world.matchups
        ),
        matchups=world.matchups,
        injuries=res._StoredNoInjuries(),
        settings=settings,
        publication_reader=world.reader,
    )

    response = client.post(
        "/api/user/targets/preview",
        headers=authenticate(),
        json={"opponent": "MIA", "qualifiers": [res.CORNER_THREE]},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["provenance"] == {
        "generation": [world.expected[key] for key in MATCHUP_STREAMS],
        "sources": SLATE_SOURCES,
    }
    assert body["today"] is None
    assert body["summary"]["games"] == 2
