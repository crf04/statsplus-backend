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


# --- A generation advancing mid-request -------------------------------------


#: The Publication a second capture inside the same request would see: the
#: player's logs re-published with a different game, opponent and score.
ADVANCED_ROW = {
    **m._log_row(game_id="0022500600", game_date="2026-01-15", points=40, minutes=36.0),
    "opponent_team_id": 1610612748,
    "opponent_team_tricode": "MIA",
}


class _AdvancingReader:
    """The world's reader, whose player logs advance after the first capture.

    The request reads its facts from the generation it captured first. Any
    second capture -- a provenance block recaptured instead of reported from
    that one snapshot -- sees the re-published logs and names another
    Publication, so its block no longer matches the facts.
    """

    def __init__(self, world):
        self._world = world
        self._reader = world.reader
        self.captures = 0

    def snapshot(self, stream_keys, **kwargs):
        captured = self._reader.snapshot(stream_keys, **kwargs)
        self.captures += 1
        if self.captures == 1:
            PublicationService(
                self._world.engine, clock=lambda: NOW - timedelta(minutes=1)
            ).compose(
                "player_game_logs",
                season=SEASON,
                cutoff=NOW - timedelta(minutes=1),
                payload={"rows": [ADVANCED_ROW]},
                expected_fence=1,
            )
        return captured

    def __getattr__(self, name):
        return getattr(self._reader, name)


# --- Game logs -------------------------------------------------------------


class _AthleteCatalog:
    def get_catalog(self, season, *, active_only):
        return [
            {"player_id": 2544, "display_name": "LeBron James", "is_active_for_season": True}
        ]


def test_game_logs_return_the_generation_their_logs_were_read_from(
    client, dependencies, authenticate, world, monkeypatch
):
    from app.services import game_service as game_service_module
    from app.services.game_logs_source import StoredGameLogsSource

    monkeypatch.setattr(game_service_module, "get_redis_client", lambda *a, **k: None)
    reader = _AdvancingReader(world)
    dependencies.game_service = game_service_module.GameService(
        world.engine,
        settings=RuntimeSettings(
            environment="testing", nba=m.NBASeasonSettings(current_season=SEASON)
        ),
        game_logs_source=StoredGameLogsSource(world.matchups.player_logs),
        athlete_catalog=_AthleteCatalog(),
        publication_reader=reader,
    )

    response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James&season_filter=2025-26",
        headers=authenticate(),
    )

    assert response.status_code == 200
    body = response.get_json()
    # The facts are the first generation's one game, not the re-published one.
    assert [(row["GAME_DATE"], row["PTS"]) for row in body["game_logs"]] == [
        ("2026-01-14", 31)
    ]
    assert body["season_game_count"] == 1
    assert body["provenance"] == {
        "generation": [world.expected[key] for key in GAME_LOG_STREAMS],
        "sources": {},
    }
    assert sorted(body) == [
        "averages", "game_logs", "next_game", "provenance", "season_averages",
        "season_game_count",
    ]
    assert reader.captures == 1


# --- Slate -----------------------------------------------------------------


UNAVAILABLE = {"status": "unavailable", "retrieved_at": None}


class _Catalog:
    """An Event Catalog collected 30 hours before the Slate is read."""

    def __init__(self, events=()):
        self.events = list(events)

    def count_events(self, season):
        return 1

    def get_freshness(self, season, *, now):
        return {"last_success_at": "2026-01-14T06:00:00+00:00", "event_count": 1}

    def get_events_between(self, season, starts_at, ends_at):
        return list(self.events)


def _slate_service(catalog, *, player_pool=None, injuries=None):
    return SlateService(
        catalog,
        settings=RuntimeSettings(
            environment="testing", nba=m.NBASeasonSettings(current_season=SEASON)
        ),
        clock=lambda: datetime(2026, 1, 15, 12, tzinfo=timezone.utc),
        schedule_max_age=timedelta(hours=24),
        player_pool=player_pool,
        injuries=injuries,
    )


def test_the_slate_reads_no_publication_and_states_its_sources(
    client, dependencies, authenticate
):
    dependencies.slate_service = _slate_service(_Catalog())

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
                "pool": UNAVAILABLE,
                # No injury read was made, so none is claimed.
                "injuries": UNAVAILABLE,
            },
        },
    }


def _event(game_id, *, away, home, scheduled_at):
    return {
        "nba_game_id": game_id,
        "scheduled_at": scheduled_at,
        "status_text": "7:30 pm ET",
        "status_code": 1,
        "is_postponed": False,
        "classification": "Regular Season",
        "away_team": {"id": away[0], "name": away[2], "tricode": away[1]},
        "home_team": {"id": home[0], "name": home[2], "tricode": home[1]},
    }


class _SlatePool:
    def __init__(self, pool):
        self.pool = pool

    def get_pool(self, *, season, game_ids):
        return self.pool


def _injury_result(status, retrieved_at, *, out=()):
    from app.services.matchup_injuries import MatchupInjuryResult

    return MatchupInjuryResult(
        block={"status": status, "retrieved_at": retrieved_at, "teams": []},
        out_player_ids=frozenset(out),
        badge_refs={},
    )


class _StoredInjuries:
    """Each game's stored injury override, by game id."""

    def __init__(self, results):
        self.results = results

    def get_stored_injuries_many(self, *, events, season, pool_players_by_game):
        return {
            str(event["nba_game_id"]): self.results[str(event["nba_game_id"])]
            for event in events
        }


def test_the_slate_states_the_least_fresh_injury_read_behind_its_counts(
    client, dependencies, authenticate
):
    from app.services.player_pool import PlayerPool, PoolPlayer

    pool = PlayerPool(
        players=(
            PoolPlayer(2544, "LeBron James", m.LAL, ("PTS",), {"prizepicks": ("PTS",)}),
            PoolPlayer(1628983, "Jimmy Butler", 1610612748, ("PTS",), {"prizepicks": ("PTS",)}),
        ),
        team_counts={m.LAL: 1, 1610612748: 1},
        freshness={
            "status": "fresh",
            "retrieved_at": "2026-01-15T10:00:00+00:00",
            "providers": {},
        },
    )
    dependencies.slate_service = _slate_service(
        _Catalog(
            [
                _event(
                    GAME_ID,
                    away=(m.LAL, "LAL", "Los Angeles Lakers"),
                    home=(m.BOS, "BOS", "Boston Celtics"),
                    scheduled_at="2026-01-16T00:30:00+00:00",
                ),
                _event(
                    "0022500585",
                    away=(1610612748, "MIA", "Miami Heat"),
                    home=(1610612743, "DEN", "Denver Nuggets"),
                    scheduled_at="2026-01-16T02:00:00+00:00",
                ),
            ]
        ),
        player_pool=_SlatePool(pool),
        injuries=_StoredInjuries(
            {
                # A 9-hour-old override marks LeBron out of tonight's count.
                GAME_ID: _injury_result(
                    "stale", "2026-01-15T03:00:00+00:00", out={2544}
                ),
                "0022500585": _injury_result("fresh", "2026-01-15T11:30:00+00:00"),
            }
        ),
    )

    response = client.get(f"/api/games/slate?date={SLATE_DATE}", headers=authenticate())

    assert response.status_code == 200
    body = response.get_json()
    assert [
        (game["away_team"]["tricode"], game["away_team"]["targetable_player_count"])
        for game in body["games"]
    ] == [("LAL", 0), ("MIA", 1)]
    assert body["provenance"]["sources"] == {
        "schedule": {"status": "stale", "retrieved_at": "2026-01-14T06:00:00+00:00"},
        "pool": {"status": "fresh", "retrieved_at": "2026-01-15T10:00:00+00:00"},
        "injuries": {"status": "stale", "retrieved_at": "2026-01-15T03:00:00+00:00"},
    }
    # The Slate's own freshness surface is unchanged.
    assert sorted(body["freshness"]) == ["pool", "schedule"]


# --- Targets ---------------------------------------------------------------


#: What the Slate double read for itself: its own pool and injury reads,
#: distinct from the ones each composed Matchup makes.
SLATE_SOURCES = {
    "schedule": {"status": "stale", "retrieved_at": "2026-01-14T06:00:00+00:00"},
    "pool": {"status": "fresh", "retrieved_at": "2026-01-15T11:30:00+00:00"},
    "injuries": {"status": "fresh", "retrieved_at": "2026-01-15T11:45:00+00:00"},
}
#: What the composed Matchup read: a pool served stale from 09:00 and a
#: 9-hour-old injury override.
MATCHUP_POOL = {
    "status": "stale-served",
    "retrieved_at": "2026-01-15T09:00:00+00:00",
}
MATCHUP_INJURIES = {"status": "stale", "retrieved_at": "2026-01-15T03:00:00+00:00"}
#: LeBron's seeded Transition share in each read is above this.
TRANSITION_15 = {
    "base": "play_types",
    "slice_key": "Transition",
    "comparator": "at_or_above",
    "threshold": 0.15,
}


class _Slate:
    """Tonight's Slate: LAL @ BOS, the game the seeded Matchup composes."""

    def __init__(self, *, games=True):
        self.games = games

    def get_slate(self, requested_date=None):
        return {
            "slate_date": requested_date or SLATE_DATE,
            "freshness": {
                "schedule": dict(SLATE_SOURCES["schedule"]),
                "pool": {**SLATE_SOURCES["pool"], "providers": {}},
            },
            "games": (
                [
                    res._game(
                        game_id=GAME_ID,
                        away=(m.LAL, "LAL", "Los Angeles Lakers"),
                        home=(m.BOS, "BOS", "Boston Celtics"),
                    )
                ]
                if self.games
                else []
            ),
            "provenance": {"generation": [], "sources": dict(SLATE_SOURCES)},
        }


class _MatchupInjuries:
    """The stored-only injury read a Matchup composes with: stale, no outs."""

    def get_injuries(self, *, event, season, pool_players):
        return _injury_result(
            MATCHUP_INJURIES["status"], MATCHUP_INJURIES["retrieved_at"]
        )


def _matchup_reads(world):
    """Give the world's Matchup its own pool read, distinct from the Slate's."""

    from app.services.player_pool import PlayerPool, PoolPlayer

    world.matchups.player_pool = m.RecordedPool(
        PlayerPool(
            players=(
                PoolPlayer(
                    2544, "LeBron James", m.LAL, ("PTS", "FGA"),
                    {"prizepicks": ("PTS", "FGA")},
                ),
            ),
            team_counts={m.LAL: 1},
            freshness={**MATCHUP_POOL, "providers": {}},
        )
    )
    return _MatchupInjuries()


def _target(opponent, qualifier=TRANSITION_15):
    return {
        "id": f"target-{opponent}",
        "opponent": opponent,
        "title": f"{opponent} vs Transition",
        "note": None,
        "qualifiers": [qualifier],
        "conditions": None,
        "stat_preferences": None,
    }


def _resolution(world, *targets, injuries):
    return TargetResolutionService(
        targets=SimpleNamespace(list_targets=lambda uid: list(targets)),
        slates=_Slate(),
        matchups=world.matchups,
        publication_reader=world.reader,
        injuries=injuries,
        settings=res._resolution_settings(),
    )


def test_resolve_returns_the_generation_and_reads_its_fits_came_from(
    client, dependencies, authenticate, world
):
    injuries = _matchup_reads(world)
    dependencies.target_resolution_service = _resolution(
        world, _target("BOS"), _target("MIA"), injuries=injuries
    )

    response = client.get(
        f"/api/user/targets/resolve?date={SLATE_DATE}", headers=authenticate()
    )

    assert response.status_code == 200
    body = response.get_json()
    assert [
        [player["name"] for player in target["players"]] for target in body["targets"]
    ] == [["LeBron James"], []]
    assert body["provenance"] == {
        "generation": [world.expected[key] for key in MATCHUP_STREAMS],
        # The schedule chose the games; the Matchup's own pool and injury
        # reads named the Fit.
        "sources": {
            "schedule": SLATE_SOURCES["schedule"],
            "pool": MATCHUP_POOL,
            "injuries": MATCHUP_INJURIES,
        },
    }
    assert sorted(body) == ["provenance", "slate_date", "success", "targets"]
    assert [target["game"] is None for target in body["targets"]] == [False, True]


def test_resolve_with_only_idle_targets_read_no_generation(
    client, dependencies, authenticate, world
):
    injuries = _matchup_reads(world)
    dependencies.target_resolution_service = _resolution(
        world, _target("MIA"), injuries=injuries
    )

    response = client.get(
        f"/api/user/targets/resolve?date={SLATE_DATE}", headers=authenticate()
    )

    assert response.status_code == 200
    assert response.get_json()["provenance"] == {
        "generation": [],
        "sources": SLATE_SOURCES,
    }


def _preview(world, dependencies, *, slate, injuries, reader):
    from app.services.player_diet import PlayerDietRepository

    settings = res._resolution_settings()
    dependencies.user_service.validate_target_draft = Mock(
        side_effect=UserService(
            db_engine=Mock(), settings=RuntimeSettings()
        ).validate_target_draft
    )
    dependencies.target_preview_service = TargetPreviewService(
        backtests=TargetBacktestService(
            targets=object(),
            player_logs=world.matchups.player_logs,
            player_diets=PlayerDietRepository(world.engine, publication_reader=world.reader),
            statistic_catalog=StatisticCatalog.load_default(),
            settings=settings,
            publication_reader=reader,
        ),
        resolutions=TargetResolutionService(
            targets=object(), slates=slate, matchups=world.matchups
        ),
        matchups=world.matchups,
        injuries=injuries,
        settings=settings,
        publication_reader=reader,
    )


#: The Backtest's own streams: the player's logs and the four Diets.
BACKTEST_STREAMS = [
    "exact_shot_zones",
    "grouped_shot_types",
    "player_assist_locations",
    "player_game_logs",
    "synergy_play_types",
]


def _backtested(body):
    """The games against BOS the Backtest found: the first generation's one."""

    return body["season"], body["games_considered"]


def test_the_preview_returns_the_one_generation_its_backtest_and_matchup_read(
    client, dependencies, authenticate, world
):
    reader = _AdvancingReader(world)
    _preview(
        world, dependencies, slate=_Slate(), injuries=_matchup_reads(world), reader=reader
    )

    response = client.post(
        "/api/user/targets/preview",
        headers=authenticate(),
        json={"opponent": "BOS", "qualifiers": [TRANSITION_15]},
    )

    assert response.status_code == 200
    body = response.get_json()
    # The first generation's one game against BOS, and tonight's one Fit.
    assert _backtested(body) == ("2025-26", {"played": 1, "kept": 1})
    assert body["today"]["fit_count"] == 1
    assert body["provenance"] == {
        "generation": [world.expected[key] for key in MATCHUP_STREAMS],
        "sources": {
            "schedule": SLATE_SOURCES["schedule"],
            "pool": MATCHUP_POOL,
            "injuries": MATCHUP_INJURIES,
        },
    }
    assert reader.captures == 1


def test_an_idle_preview_lists_only_the_streams_its_backtest_read(
    client, dependencies, authenticate, world
):
    reader = _AdvancingReader(world)
    _preview(
        world,
        dependencies,
        slate=_Slate(games=False),
        injuries=_matchup_reads(world),
        reader=reader,
    )

    response = client.post(
        "/api/user/targets/preview",
        headers=authenticate(),
        json={"opponent": "BOS", "qualifiers": [TRANSITION_15]},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert _backtested(body) == ("2025-26", {"played": 1, "kept": 1})
    assert body["today"] is None
    assert body["provenance"] == {
        "generation": [world.expected[key] for key in BACKTEST_STREAMS],
        "sources": SLATE_SOURCES,
    }
    assert reader.captures == 1
