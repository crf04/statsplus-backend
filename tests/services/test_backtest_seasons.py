"""Target Backtests read the previous season from retained Publications (#104).

These tests drive the real season seams end to end: Publications composed
through ``PublicationService`` into a migrated SQLite database, the real
``DatabaseFirstPublicationReader``, game-log repository, and Diet repository,
and the Backtest, preview, and validator services over them.  The 2025-26
season is published first, then 2026-27 activates, so every past-season read
must come from the pointer history rather than the live pointer.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config.settings import (
    MatchupScoreSettings,
    NBASeasonSettings,
    RuntimeSettings,
)
from app.domain.publication_integrity import canonical_publication_json
from app.errors import InvalidInputError, SeasonUnavailableError
from app.migrations import run_migrations
from app.models.user import User
from app.services.collection_control import PublicationService
from app.services.database_first_activation import DatabaseFirstPublicationReader
from app.services.player_diet import PlayerDietRepository
from app.services.player_game_log_repository import PlayerGameLogRepository
from app.services.statistic_catalog import StatisticCatalog
from app.services.target_backtest import (
    BACKTEST_PUBLICATION_STREAM_KEYS,
    TargetBacktestService,
)
from app.services.target_preview import TargetPreviewService
from app.services.user_service import UserService
from tests import test_target_backtest as bt

OWNER = "test-uid"
LAST = "2025-26"
NEW = "2026-27"
NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
CORNER_THREE = bt.CORNER_THREE
DIET_STREAMS = {
    "shot_zones": ("exact_shot_zones", "field_goal_attempts", "nba_stats", bt.SHOT_ZONES),
    "play_types": ("synergy_play_types", "possessions", "nba_synergy", ("Transition",)),
    "shot_types": ("grouped_shot_types", "field_goal_attempts", "nba_stats", ("catch_and_shoot",)),
    "assist_locations": ("player_assist_locations", "assists", "pbp_stats", ("Corner3Assists",)),
}


def _unpinned():
    """Settings with no ``NBA_CURRENT_SEASON`` pin: the pointer decides."""

    return RuntimeSettings(
        environment="testing",
        nba=NBASeasonSettings.model_construct(_fields_set=set(), current_season=NEW),
        matchup_scores=MatchupScoreSettings(),
    )


def _log(
    player_id, name, season, game_id, game_date, opponent_id, opponent,
    season_type="Regular Season",
):
    record = bt._row(
        player_id,
        name=name,
        season_type=season_type,
        game_id=game_id,
        game_date=game_date,
        opponent_team_id=opponent_id,
        opponent_team_tricode=opponent,
    )
    row = asdict(record)
    row["season"] = season
    row["game_date"] = game_date.isoformat()
    return row


def _season_logs(season, *, okc_games, season_type="Regular Season"):
    """LeBron's five-game season, ``okc_games`` of them against OKC.

    In 2025-26 Embiid plays for OKC in those games, so he is a defender the
    opponent fielded last season and never this one.
    """

    prefix = ("002" if season_type == "Regular Season" else "004") + (
        "25" if season == LAST else "26"
    )
    start = date(2026, 1, 1) if season == LAST else date(2026, 10, 22)
    rows = []
    for index in range(5):
        game_id, game_date = f"{prefix}{index:05d}", start + timedelta(days=index)
        against_okc = index < okc_games
        rows.append(_log(
            bt.LEBRON, "LeBron James", season, game_id, game_date,
            *((bt.OKC, "OKC") if against_okc else (bt.BOS, "BOS")),
            season_type=season_type,
        ))
        if against_okc and season == LAST:
            rows.append({
                **_log(bt.EMBIID, "Joel Embiid", season, game_id, game_date, bt.LAL, "LAL"),
                "team_id": bt.OKC,
                "team_tricode": "OKC",
                "is_home": False,
            })
    return rows


def _diet_rows(base, corner_three):
    stream, unit, provider, slices = DIET_STREAMS[base]
    shares = (
        bt._zone_diet(corner_three, 0.2)
        if base == "shot_zones"
        else {slice_key: 0.5 for slice_key in slices}
    )
    return {
        "base": base,
        "rows": [
            {
                "player_id": player_id,
                "slice_key": slice_key,
                "share": share,
                "volume": 60.0,
                "games_played": 10,
                "volume_unit": unit,
                "provider": provider,
            }
            for player_id in (bt.LEBRON, bt.TATUM)
            for slice_key, share in shares.items()
        ],
    }


class World:
    """One database whose Publications move through a season rollover."""

    def __init__(self, engine):
        self.engine = engine
        run_migrations(self.engine)
        with self.engine.begin() as connection:
            connection.execute(
                User.__table__.insert(),
                {
                    "firebase_uid": OWNER,
                    "email": "owner@example.com",
                    "display_name": OWNER,
                    "photo_url": None,
                    "created_at": NOW,
                    "last_login": NOW,
                    "is_active": True,
                },
            )
        self.publications = PublicationService(self.engine, clock=lambda: NOW)
        for stream in BACKTEST_PUBLICATION_STREAM_KEYS:
            self.publications.register_stream(
                stream,
                provider="ledger",
                owner="railway",
                required_observations=(),
                publication_strategy="replace",
                enabled=True,
                freshness_rule="cutoff_current",
            )
        self.reader = DatabaseFirstPublicationReader(self.engine, clock=lambda: NOW)
        self.settings = _unpinned()
        self.logs = PlayerGameLogRepository(
            self.engine,
            statistic_catalog=StatisticCatalog.load_default(),
            stats_surface_season=LAST,
            clock=lambda: NOW,
            stats_surface_max_age=timedelta(hours=30),
            publication_reader=self.reader,
        )
        self.users = UserService(
            self.engine,
            settings=self.settings,
            player_logs=self.logs,
            publication_reader=self.reader,
        )

    def compose(self, stream, season, payload):
        """Activate one Publication through the pointer-advancing write path.

        A Diet stream's public compose also demands the collection manifest
        and observations that authorized it; those are not what these tests
        are about, so every prepared payload takes the pointer advance (and
        its pointer-history row) the public compose ends in directly.
        """

        with Session(self.engine) as session, session.begin():
            self.publications._compose_active_in_session(
                session,
                stream_key=stream,
                season=season,
                cutoff=NOW,
                encoded=canonical_publication_json(payload),
                payload=payload,
                expected_fence=None,
                reason=None,
                provenance_ids=set(),
                now=NOW,
                derive_expected_fence_from_lock=True,
            )

    def publish_season(
        self, season, *, okc_games, corner_three, season_type="Regular Season"
    ):
        """Activate one season's five Backtest streams."""

        self.compose(
            "player_game_logs",
            season,
            {"rows": _season_logs(season, okc_games=okc_games, season_type=season_type)},
        )
        for base, (stream, *_rest) in DIET_STREAMS.items():
            self.compose(stream, season, _diet_rows(base, corner_three))

    def backtests(self, *, redis_client=None):
        return TargetBacktestService(
            targets=self.users,
            player_logs=self.logs,
            player_diets=PlayerDietRepository(
                self.engine, publication_reader=self.reader
            ),
            statistic_catalog=StatisticCatalog.load_default(),
            settings=self.settings,
            publication_reader=self.reader,
            engine=self.engine,
            redis_client=redis_client,
        )

    def saved_target(self, **conditions):
        return self.users.create_target(
            OWNER, opponent="OKC", qualifiers=[CORNER_THREE], **conditions
        )

    def revoke(self, stream, season):
        """Withdraw the season's latest history row of one stream."""

        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE publication_pointer_history SET revoked_at = :stamp "
                    "WHERE history_id = (SELECT history_id FROM "
                    "publication_pointer_history WHERE stream_key = :stream "
                    "AND season = :season AND revoked_at IS NULL "
                    "ORDER BY fence DESC LIMIT 1)"
                ),
                {"stamp": "2026-10-03 12:00:00.000000", "stream": stream, "season": season},
            )


@pytest.fixture
def world(tmp_path):
    built = World(create_engine(f"sqlite:///{tmp_path / 'seasons.sqlite3'}"))
    built.publish_season(LAST, okc_games=2, corner_three=0.42)
    yield built
    built.engine.dispose()


def _fitting(body):
    return [player["canonical_id"] for player in body["players"]]


def _activate_new_season(world):
    """Activate 2026-27: one OKC game, and a Diet LeBron does not fit."""

    world.publish_season(NEW, okc_games=1, corner_three=0.1)


def _pin(world, season):
    world.settings = RuntimeSettings(
        environment="testing",
        nba=NBASeasonSettings(current_season=season),
        matchup_scores=MatchupScoreSettings(),
    )
    world.users = UserService(
        world.engine,
        settings=world.settings,
        player_logs=world.logs,
        publication_reader=world.reader,
    )


def _season(body):
    return body["season"], body["season_reason"]


# --- the default and its fallback -------------------------------------------


def test_before_the_new_season_activates_the_default_reads_the_published_season(world):
    target = world.saved_target()

    body, _ = world.backtests().backtest(OWNER, target["id"])

    assert _season(body) == (LAST, "published")
    assert _fitting(body) == [bt.LEBRON]
    assert body["games_considered"] == {"played": 2, "kept": 2}


def test_with_zero_games_league_wide_the_default_falls_back_to_last_season(world):
    """Pinned to 2026-27 before its first game: nothing is published for it."""

    target = world.saved_target()
    _pin(world, NEW)

    body, _ = world.backtests().backtest(OWNER, target["id"])

    assert _season(body) == (LAST, "fallback_no_games")
    assert _fitting(body) == [bt.LEBRON]
    assert body["games_considered"] == {"played": 2, "kept": 2}


def test_an_explicit_season_overrides_the_fallback(world):
    target = world.saved_target()
    _pin(world, NEW)
    service = world.backtests()

    body, _ = service.backtest(OWNER, target["id"], season=LAST)

    assert _season(body) == (LAST, "requested")
    # 2026-27 is requestable, and with nothing published it is unavailable,
    # never an empty Backtest.
    with pytest.raises(SeasonUnavailableError, match="2026-27.*player_game_logs"):
        service.backtest(OWNER, target["id"], season=NEW)


def test_once_games_exist_the_published_season_is_used_however_thin(world):
    target = world.saved_target()
    _activate_new_season(world)

    body, _ = world.backtests().backtest(OWNER, target["id"])

    assert _season(body) == (NEW, "published")
    assert body["games_considered"] == {"played": 1, "kept": 1}
    assert _fitting(body) == []


def test_last_season_reads_its_own_retained_generation_after_the_new_one_activates(
    world,
):
    target = world.saved_target()
    _activate_new_season(world)

    body, _ = world.backtests().backtest(OWNER, target["id"], season=LAST)

    # Last season's shares, games, and averages throughout: LeBron fitted
    # Corner 3 in 2025-26 and played OKC twice, while 2026-27's Diet does not
    # fit him at all and he played OKC once.
    assert _season(body) == (LAST, "requested")
    assert _fitting(body) == [bt.LEBRON]
    assert body["games_considered"] == {"played": 2, "kept": 2}
    assert body["players"][0]["shares"][0]["share"] == 0.42
    assert len(body["players"][0]["games"]) == 2


def test_a_pin_behind_the_live_pointer_reads_its_published_season_retained(world):
    _activate_new_season(world)
    _pin(world, LAST)
    target = world.saved_target()
    service = world.backtests()

    body, _ = service.backtest(OWNER, target["id"])

    assert _season(body) == (LAST, "published")
    assert _fitting(body) == [bt.LEBRON]
    # The pin decides the published season; nothing reaches past it.
    with pytest.raises(InvalidInputError, match="season must be 2025-26 or 2024-25"):
        service.backtest(OWNER, target["id"], season=NEW)


@pytest.mark.parametrize("season", ["2024-25", "2027-28", "2025-2026", "", 2025])
def test_a_season_outside_the_window_is_refused(world, season):
    target = world.saved_target()
    _activate_new_season(world)

    with pytest.raises(InvalidInputError, match="season must be 2026-27 or 2025-26"):
        world.backtests().backtest(OWNER, target["id"], season=season)


# --- unavailable is not empty -----------------------------------------------


@pytest.mark.parametrize("stream", BACKTEST_PUBLICATION_STREAM_KEYS)
def test_a_stream_the_season_does_not_retain_is_season_unavailable(world, stream):
    target = world.saved_target()
    _activate_new_season(world)
    world.revoke(stream, LAST)
    service = world.backtests()

    with pytest.raises(SeasonUnavailableError, match=f"2025-26.*{stream}") as refused:
        service.backtest(OWNER, target["id"], season=LAST)

    assert (refused.value.status_code, refused.value.code) == (503, "season_unavailable")
    # The published season reads its live pointer and is unaffected.
    assert _season(service.backtest(OWNER, target["id"])[0]) == (NEW, "published")


def test_a_revoked_latest_row_falls_back_to_the_seasons_earlier_retained_row(world):
    world.compose("player_game_logs", LAST, {"rows": _season_logs(LAST, okc_games=1)})
    target = world.saved_target()
    _activate_new_season(world)
    service = world.backtests()
    assert service.backtest(OWNER, target["id"], season=LAST)[0][
        "games_considered"
    ] == {"played": 1, "kept": 1}

    world.revoke("player_game_logs", LAST)

    assert service.backtest(OWNER, target["id"], season=LAST)[0][
        "games_considered"
    ] == {"played": 2, "kept": 2}


# --- the result cache -------------------------------------------------------


def test_the_retained_cache_key_ignores_new_live_publications_and_follows_revocation(
    world,
):
    world.compose("player_game_logs", LAST, {"rows": _season_logs(LAST, okc_games=1)})
    target = world.saved_target()
    _activate_new_season(world)
    redis = bt.FakeRedis()
    service = world.backtests(redis_client=redis)

    first, first_state = service.backtest(OWNER, target["id"], season=LAST)
    # A nightly 2026-27 publication moves the live pointer, not 2025-26.
    world.compose("player_game_logs", NEW, {"rows": _season_logs(NEW, okc_games=2)})
    again, again_state = service.backtest(OWNER, target["id"], season=LAST)
    # Revoking the 2025-26 row that served moves the season to its earlier
    # retained row: a different Generation, so a different key.
    world.revoke("player_game_logs", LAST)
    revoked, revoked_state = service.backtest(OWNER, target["id"], season=LAST)

    assert (first_state, again_state, revoked_state) == ("miss", "hit", "miss")
    assert again == first
    assert [key for key, _ttl in redis.sets][0] != [key for key, _ttl in redis.sets][1]
    assert len(redis.sets) == 2
    assert first["games_considered"] == {"played": 1, "kept": 1}
    assert revoked["games_considered"] == {"played": 2, "kept": 2}


def test_a_hit_reports_the_requests_own_season_reason(world):
    target = world.saved_target()
    _pin(world, NEW)
    redis = bt.FakeRedis()
    service = world.backtests(redis_client=redis)

    requested, requested_state = service.backtest(OWNER, target["id"], season=LAST)
    fallback, fallback_state = service.backtest(OWNER, target["id"])
    batch, batch_state = service.backtest_all(OWNER)

    assert (requested_state, fallback_state, batch_state) == ("miss", "hit", "hit")
    assert fallback == {**requested, "season_reason": "fallback_no_games"}
    assert _season(batch) == (LAST, "fallback_no_games")
    assert batch["backtests"] == [
        {"target_id": target["id"], "status": "ok", "backtest": fallback}
    ]


def test_the_batch_resolves_the_season_once_for_every_target(world):
    target = world.saved_target()
    _activate_new_season(world)
    redis = bt.FakeRedis()
    service = world.backtests(redis_client=redis)
    single, _ = service.backtest(OWNER, target["id"], season=LAST)

    body, state = service.backtest_all(OWNER, season=LAST)
    published, _ = service.backtest_all(OWNER)

    assert state == "hit"
    assert _season(body) == (LAST, "requested")
    assert body["backtests"] == [
        {"target_id": target["id"], "status": "ok", "backtest": single}
    ]
    assert _season(published) == (NEW, "published")
    assert published["backtests"] == [{"target_id": target["id"], "status": "uncached"}]


@pytest.mark.parametrize("redis_client", [None, bt.FakeRedis], ids=("no-cache", "cache"))
def test_the_batch_refuses_an_unretained_or_invalid_season(world, redis_client):
    world.saved_target()
    _activate_new_season(world)
    world.revoke("synergy_play_types", LAST)
    service = world.backtests(redis_client=redis_client and redis_client())

    with pytest.raises(SeasonUnavailableError, match="synergy_play_types"):
        service.backtest_all(OWNER, season=LAST)
    with pytest.raises(InvalidInputError):
        service.backtest_all(OWNER, season="2024-25")


# --- Defender Conditions ----------------------------------------------------


def _defender(player_id):
    return {
        "defender": {"player_id": player_id, "comparator": "at_least", "minutes": 20},
        "from": None,
        "to": None,
        "player_minutes": None,
    }


def _draft(world, season, conditions):
    return world.users.validate_target_draft(
        opponent="OKC", qualifiers=[CORNER_THREE], conditions=conditions, season=season
    )


def test_a_defender_is_validated_against_the_backtests_season(world):
    """Embiid played for OKC last season only; LeBron never did."""

    _activate_new_season(world)
    embiid = _defender(bt.EMBIID)

    assert _draft(world, LAST, embiid)["conditions"]["defender"]["player_id"] == bt.EMBIID
    for season in (None, NEW):
        with pytest.raises(InvalidInputError, match="defender must appear"):
            _draft(world, season, embiid)
    with pytest.raises(InvalidInputError, match="defender must appear"):
        _draft(world, LAST, _defender(bt.LEBRON))
    with pytest.raises(InvalidInputError, match="season must be"):
        _draft(world, "2024-25", embiid)


def test_a_saved_targets_defender_follows_the_fallback_season(world):
    _pin(world, NEW)

    saved = world.saved_target(conditions=_defender(bt.EMBIID))

    body, _ = world.backtests().backtest(OWNER, saved["id"])
    assert _season(body) == (LAST, "fallback_no_games")
    # Embiid logged 34 minutes in both OKC games LeBron played.
    assert body["games_considered"] == {"played": 2, "kept": 2}


# --- the Lab preview ----------------------------------------------------------


class NoTonight:
    """A past season says nothing about tonight, so nothing reads it."""

    def today(self, *_args, **_kwargs):
        raise AssertionError("a past-season preview has no today")


def _preview(world, season):
    draft = world.users.validate_target_draft(
        opponent="OKC", qualifiers=[CORNER_THREE], season=season
    )
    return TargetPreviewService(
        backtests=world.backtests(),
        resolutions=NoTonight(),
        matchups=object(),
        injuries=None,
        settings=world.settings,
        publication_reader=world.reader,
    ).preview(draft, season=season)


def test_a_past_season_preview_reads_that_season_and_has_no_today(world):
    _activate_new_season(world)

    body = _preview(world, LAST)

    assert _season(body) == (LAST, "requested")
    assert _fitting(body) == [bt.LEBRON]
    assert body["today"] is None


def test_a_fallback_preview_has_no_today(world):
    _pin(world, NEW)

    body = _preview(world, None)

    assert _season(body) == (LAST, "fallback_no_games")
    assert _fitting(body) == [bt.LEBRON]
    assert body["today"] is None


def test_a_preview_of_an_unretained_season_is_unavailable(world):
    _activate_new_season(world)
    world.revoke("exact_shot_zones", LAST)

    with pytest.raises(SeasonUnavailableError, match="exact_shot_zones"):
        _preview(world, LAST)


# --- the retained reader ----------------------------------------------------


def test_a_retained_capture_is_labelled_and_keyed_apart_from_the_live_one(world):
    _activate_new_season(world)
    keys = BACKTEST_PUBLICATION_STREAM_KEYS

    retained = world.reader.retained_snapshot(keys, season=LAST)
    live_mismatch = world.reader.snapshot(keys, season=LAST)

    assert {read.status for read in retained.reads.values()} == {"retained"}
    assert all(read.season == LAST for read in retained.reads.values())
    assert {read.unavailable_reason for read in live_mismatch.reads.values()} == {
        "publication_season_mismatch"
    }
    # A Diet baseline or season summary cached under the live capture's
    # generation can never be served to the retained one.
    assert retained.generation != live_mismatch.generation
    assert [key for key, _ in retained.retained_history] == sorted(keys)
    assert world.reader.retained_history(keys, season=LAST) == retained.retained_history


def test_a_season_with_no_history_reads_missing(world):
    retained = world.reader.retained_snapshot(("player_game_logs",), season=NEW)

    read = retained.read("player_game_logs")
    assert (read.status, read.unavailable_reason) == ("missing", "retained_publication_missing")
    assert retained.retained_history == (("player_game_logs", None),)


# --- HTTP -------------------------------------------------------------------


@pytest.fixture
def served(world, dependencies):
    _activate_new_season(world)
    world.revoke("grouped_shot_types", LAST)
    dependencies.user_service = world.users
    dependencies.target_backtest_service = world.backtests()
    return world


@pytest.mark.parametrize(
    "path",
    ["/api/user/targets/{id}/backtest", "/api/user/targets/backtests"],
)
def test_the_get_routes_take_season_as_a_query_parameter(client, authenticate, served, path):
    target = served.saved_target()
    url = path.format(id=target["id"])

    invalid = client.get(url, query_string={"season": "2024-25"}, headers=authenticate())
    unavailable = client.get(url, query_string={"season": LAST}, headers=authenticate())
    published = client.get(url, headers=authenticate())

    assert invalid.status_code == 400
    assert invalid.get_json()["error"]["code"] == "invalid_input"
    assert unavailable.status_code == 503
    assert unavailable.get_json() == {
        "error": {
            "code": "season_unavailable",
            "message": (
                "The 2025-26 season is unavailable: no retained grouped_shot_types "
                "Publication can be read."
            ),
        }
    }
    assert published.status_code == 200
    assert _season(published.get_json()) == (NEW, "published")


def test_the_preview_route_takes_season_in_its_body(client, authenticate, served, dependencies):
    dependencies.target_preview_service = TargetPreviewService(
        backtests=dependencies.target_backtest_service,
        resolutions=NoTonight(),
        matchups=object(),
        injuries=None,
        settings=served.settings,
        publication_reader=served.reader,
    )
    body = {"opponent": "OKC", "qualifiers": [CORNER_THREE]}

    invalid = client.post(
        "/api/user/targets/preview", json={**body, "season": "2027-28"}, headers=authenticate()
    )
    unavailable = client.post(
        "/api/user/targets/preview", json={**body, "season": LAST}, headers=authenticate()
    )

    assert invalid.status_code == 400
    assert invalid.get_json()["error"]["code"] == "invalid_input"
    assert unavailable.status_code == 503
    assert unavailable.get_json()["error"]["code"] == "season_unavailable"
