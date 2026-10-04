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
from app.domain.team_matchup_taxonomy import PLAY_TYPES, SHOT_TYPE_SLICES
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
#: Each Base's stream, unit, provider, and full slice partition; the first
#: slice is the one ``EVERY_BASE`` qualifies on.
DIET_STREAMS = {
    "shot_zones": ("exact_shot_zones", "field_goal_attempts", "nba_stats", bt.SHOT_ZONES),
    "play_types": ("synergy_play_types", "possessions", "nba_synergy", PLAY_TYPES),
    "shot_types": (
        "grouped_shot_types", "field_goal_attempts", "nba_stats", SHOT_TYPE_SLICES
    ),
    "assist_locations": (
        "player_assist_locations",
        "assists",
        "pbp_stats",
        (
            "Corner3Assists", "Arc3Assists", "AtRimAssists",
            "ShortMidRangeAssists", "LongMidRangeAssists",
        ),
    ),
}


def _unpinned():
    """Settings with no ``NBA_CURRENT_SEASON`` pin: the pointer decides."""

    return RuntimeSettings(
        environment="testing",
        nba=NBASeasonSettings.model_construct(_fields_set=set(), current_season=NEW),
        matchup_scores=MatchupScoreSettings(),
    )


#: LeBron's points per game each season, so a season's averages are its own.
POINTS = {LAST: 30, NEW: 12}
#: Tatum's Corner 3 share each season: last season's league baseline is
#: (0.42 + 0.30) / 2 = 0.36, this season's (0.1 + 0.1) / 2 = 0.1.
TATUM_CORNER_THREE = {LAST: 0.30, NEW: 0.1}


def _log(player_id, name, season, game_id, game_date, opponent_id, opponent):
    record = bt._row(
        player_id,
        name=name,
        points=POINTS[season],
        game_id=game_id,
        game_date=game_date,
        opponent_team_id=opponent_id,
        opponent_team_tricode=opponent,
    )
    row = asdict(record)
    row["season"] = season
    row["game_date"] = game_date.isoformat()
    return row


def _season_logs(season, *, okc_games):
    """LeBron's five-game season, ``okc_games`` of them against OKC.

    In 2025-26 Embiid plays for OKC in those games, so he is a defender the
    opponent fielded last season and never this one.
    """

    prefix = "00225" if season == LAST else "00226"
    start = date(2026, 1, 1) if season == LAST else date(2026, 10, 22)
    rows = []
    for index in range(5):
        game_id, game_date = f"{prefix}{index:05d}", start + timedelta(days=index)
        against_okc = index < okc_games
        rows.append(_log(
            bt.LEBRON, "LeBron James", season, game_id, game_date,
            *((bt.OKC, "OKC") if against_okc else (bt.BOS, "BOS")),
        ))
        if against_okc and season == LAST:
            rows.append({
                **_log(bt.EMBIID, "Joel Embiid", season, game_id, game_date, bt.LAL, "LAL"),
                "team_id": bt.OKC,
                "team_tricode": "OKC",
                "is_home": False,
            })
    return rows


#: LeBron's share of each other Base's one slice: last season's clears every
#: Qualifier in ``EVERY_BASE``, this season's clears none.
OTHER_BASE_SHARE = {LAST: 0.5, NEW: 0.005}


def _diet_rows(base, season, corner_three):
    stream, unit, provider, slices = DIET_STREAMS[base]

    def shares(player_id):
        if base != "shot_zones":
            share = OTHER_BASE_SHARE[season] if player_id == bt.LEBRON else 0.3
            rest = round((1 - share) / (len(slices) - 1), 6)
            return {
                slice_key: share if index == 0 else rest
                for index, slice_key in enumerate(slices)
            }
        if player_id == bt.TATUM:
            return bt._zone_diet(TATUM_CORNER_THREE[season], 0.2)
        return bt._zone_diet(corner_three, 0.2)

    return {
        "base": base,
        "rows": [
            {
                "player_id": player_id,
                "slice_key": slice_key,
                "share": share,
                "volume": 100.0,
                "games_played": 10,
                "volume_unit": unit,
                "provider": provider,
            }
            for player_id in (bt.LEBRON, bt.TATUM)
            for slice_key, share in shares(player_id).items()
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

    def publish_season(self, season, *, okc_games, corner_three):
        """Activate one season's five Backtest streams."""

        self.compose(
            "player_game_logs",
            season,
            {"rows": _season_logs(season, okc_games=okc_games)},
        )
        for base, (stream, *_rest) in DIET_STREAMS.items():
            self.publish_diet(stream, season, corner_three=corner_three)

    def publish_diet(self, stream, season, *, corner_three):
        base = next(base for base, (key, *_) in DIET_STREAMS.items() if key == stream)
        self.compose(stream, season, _diet_rows(base, season, corner_three))

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

    def revoke(self, stream, season, *, column="revoked_at"):
        """Withdraw (or, with ``retired_at``, retire) the season's latest row."""

        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"UPDATE publication_pointer_history SET {column} = :stamp "
                    "WHERE history_id = (SELECT history_id FROM "
                    "publication_pointer_history WHERE stream_key = :stream "
                    "AND season = :season AND revoked_at IS NULL "
                    "AND retired_at IS NULL ORDER BY fence DESC LIMIT 1)"
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
    assert body["players"][0]["season_averages"]["PTS"] == 30.0
    assert body["players"][0]["shares"][0]["league_average_share"] == 0.36


def test_the_fallback_reads_last_seasons_retained_rows_not_the_live_pointer(world):
    """The live game logs still name 2025-26, but the fallback is retained."""

    target = world.saved_target()
    _pin(world, NEW)
    # One stream activating 2026-27 early does not change last season.
    world.publish_diet("exact_shot_zones", NEW, corner_three=0.1)
    service = world.backtests()

    body, _ = service.backtest(OWNER, target["id"])
    assert _season(body) == (LAST, "fallback_no_games")
    assert body["players"][0]["shares"][0]["share"] == 0.42

    world.revoke("player_game_logs", LAST)
    with pytest.raises(SeasonUnavailableError, match="player_game_logs"):
        service.backtest(OWNER, target["id"])


def test_the_fallbacks_cache_entry_survives_the_new_seasons_first_activation(world):
    target = world.saved_target()
    _pin(world, NEW)
    redis = bt.FakeRedis()
    service = world.backtests(redis_client=redis)

    fallback, fallback_state = service.backtest(OWNER, target["id"])
    _activate_new_season(world)
    requested, requested_state = service.backtest(OWNER, target["id"], season=LAST)

    assert (fallback_state, requested_state) == ("miss", "hit")
    assert requested == {**fallback, "season_reason": "requested"}


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
    lebron = body["players"][0]
    assert lebron["shares"][0]["share"] == 0.42
    assert lebron["shares"][0]["league_average_share"] == 0.36
    assert lebron["season_averages"]["PTS"] == 30.0
    assert lebron["season_games"] == 5
    assert [game["stats"]["PTS"] for game in lebron["games"]] == [30.0, 30.0]


#: One Qualifier on every Diet Base.
EVERY_BASE = [CORNER_THREE, bt.TRANSITION, bt.SHOT_TYPE, bt.ASSIST]


def test_every_diet_base_reads_last_seasons_retained_shares_and_baselines(world):
    target = world.users.create_target(OWNER, opponent="OKC", qualifiers=EVERY_BASE)
    _activate_new_season(world)

    body, _ = world.backtests().backtest(OWNER, target["id"], season=LAST)

    assert _fitting(body) == [bt.LEBRON]
    assert [
        (share["base"], share["share"], share["league_average_share"])
        for share in body["players"][0]["shares"]
    ] == [
        ("shot_zones", 0.42, 0.36),
        ("play_types", 0.5, 0.4),
        ("shot_types", 0.5, 0.4),
        ("assist_locations", 0.5, 0.4),
    ]


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


@pytest.mark.parametrize("column", ["revoked_at", "retired_at"])
def test_a_withdrawn_latest_row_falls_back_to_the_seasons_earlier_retained_row(
    world, column
):
    world.compose("player_game_logs", LAST, {"rows": _season_logs(LAST, okc_games=1)})
    target = world.saved_target()
    _activate_new_season(world)
    service = world.backtests()
    assert service.backtest(OWNER, target["id"], season=LAST)[0][
        "games_considered"
    ] == {"played": 1, "kept": 1}

    world.revoke("player_game_logs", LAST, column=column)

    assert service.backtest(OWNER, target["id"], season=LAST)[0][
        "games_considered"
    ] == {"played": 2, "kept": 2}


@pytest.mark.parametrize("redis_client", [None, bt.FakeRedis], ids=("no-cache", "cache"))
def test_a_published_season_stream_with_no_publication_is_season_unavailable(
    world, redis_client
):
    """2026-27 game logs activate before any 2026-27 Diet stream."""

    target = world.saved_target()
    world.compose("player_game_logs", NEW, {"rows": _season_logs(NEW, okc_games=1)})
    service = world.backtests(redis_client=redis_client and redis_client())

    for season in (None, NEW):
        with pytest.raises(SeasonUnavailableError, match="2026-27.*exact_shot_zones"):
            service.backtest(OWNER, target["id"], season=season)
        with pytest.raises(SeasonUnavailableError, match="2026-27.*exact_shot_zones"):
            service.backtest_all(OWNER, season=season)


#: A Corner 3 Qualifier both seasons' LeBron clears (0.42 and 0.1).
ANY_CORNER_THREE = {**CORNER_THREE, "threshold": 0.05}


def _moved_off_the_season(world):
    """exact_shot_zones moves back to 2025-26 after 2026-27 activated."""

    _activate_new_season(world)
    world.publish_diet("exact_shot_zones", LAST, corner_three=0.42)


def _assert_new_seasons_lebron(body):
    """2026-27's retained shares and baseline, and its live games."""

    assert _season(body) == (NEW, "published")
    assert _fitting(body) == [bt.LEBRON]
    lebron = body["players"][0]
    assert lebron["shares"][0]["share"] == 0.1
    assert lebron["shares"][0]["league_average_share"] == 0.1
    assert lebron["season_averages"]["PTS"] == 12.0
    assert [game["stats"]["PTS"] for game in lebron["games"]] == [12.0]


def test_a_published_stream_moved_off_the_season_reads_its_retained_row(world):
    _moved_off_the_season(world)
    target = world.users.create_target(
        OWNER, opponent="OKC", qualifiers=[ANY_CORNER_THREE]
    )
    redis = bt.FakeRedis()

    body, state = world.backtests(redis_client=redis).backtest(OWNER, target["id"])

    _assert_new_seasons_lebron(body)
    # Live and retained reads together have no single Generation to key on.
    assert (state, redis.sets) == ("bypass", [])


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


def test_the_batch_resolves_the_season_once_for_every_target(world, monkeypatch):
    target = world.saved_target()
    world.users.create_target(OWNER, opponent="BOS", qualifiers=[bt.LOW_RIM])
    _activate_new_season(world)
    redis = bt.FakeRedis()
    service = world.backtests(redis_client=redis)
    single, _ = service.backtest(OWNER, target["id"], season=LAST)
    resolutions = []
    resolve = service.seasons.resolve
    monkeypatch.setattr(
        service.seasons,
        "resolve",
        lambda *args, **kwargs: resolutions.append(args) or resolve(*args, **kwargs),
    )

    body, state = service.backtest_all(OWNER, season=LAST)
    published, _ = service.backtest_all(OWNER)

    assert resolutions == [(LAST,), (None,)]
    assert state == "miss"
    assert _season(body) == (LAST, "requested")
    assert body["backtests"][1] == {
        "target_id": target["id"], "status": "ok", "backtest": single
    }
    assert body["backtests"][0]["status"] == "uncached"
    assert _season(published) == (NEW, "published")
    assert [item["status"] for item in published["backtests"]] == ["uncached", "uncached"]


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


def test_a_preview_rechecks_its_defender_in_the_season_it_captured(world):
    """2026-27 activates between validating the draft and previewing it."""

    draft = world.users.validate_target_draft(
        opponent="OKC", qualifiers=[CORNER_THREE], conditions=_defender(bt.EMBIID)
    )
    _activate_new_season(world)

    with pytest.raises(InvalidInputError, match="defender must appear"):
        TargetPreviewService(
            backtests=world.backtests(),
            resolutions=NoTonight(),
            matchups=object(),
            injuries=None,
            settings=world.settings,
            publication_reader=world.reader,
        ).preview(draft)


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


def test_a_published_preview_reading_a_retained_stream_has_no_today(world):
    _moved_off_the_season(world)
    draft = world.users.validate_target_draft(
        opponent="OKC", qualifiers=[ANY_CORNER_THREE]
    )

    body = TargetPreviewService(
        backtests=world.backtests(),
        resolutions=NoTonight(),
        matchups=object(),
        injuries=None,
        settings=world.settings,
        publication_reader=world.reader,
    ).preview(draft)

    _assert_new_seasons_lebron(body)
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
            "details": {
                "season": LAST,
                "published_season": NEW,
                "stream": "grouped_shot_types",
            },
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


# --- season minutes (defender roster) ----------------------------------------


def _minutes(world, season=None):
    from app.services.target_season_minutes import TargetSeasonMinutesService

    return TargetSeasonMinutesService(
        player_logs=world.logs, settings=world.settings, publication_reader=world.reader
    ).get("okc", season=season)


def test_season_minutes_list_last_seasons_opponent_roster_from_retained_rows(world):
    """Embiid played for OKC only in 2025-26; 2026-27's OKC fielded nobody logged."""

    _activate_new_season(world)

    assert _minutes(world, LAST) == {
        "season": LAST,
        "season_reason": "requested",
        "published_season": NEW,
        "players": [
            {"player_id": bt.EMBIID, "name": "Joel Embiid", "games_played": 2,
             "average_minutes": 34.0},
        ],
    }
    assert _minutes(world) == {
        "season": NEW,
        "season_reason": "published",
        "published_season": NEW,
        "players": [],
    }


def test_season_minutes_follow_the_backtests_fallback(world):
    _pin(world, NEW)

    body = _minutes(world)

    assert (body["season"], body["season_reason"], body["published_season"]) == (
        LAST, "fallback_no_games", NEW
    )
    assert [player["player_id"] for player in body["players"]] == [bt.EMBIID]


def test_season_minutes_refuse_an_unretained_or_invalid_season(world):
    _activate_new_season(world)
    world.revoke("player_game_logs", LAST)

    with pytest.raises(SeasonUnavailableError, match="player_game_logs") as refused:
        _minutes(world, LAST)
    assert refused.value.public_details == {
        "season": LAST, "published_season": NEW, "stream": "player_game_logs"
    }
    with pytest.raises(InvalidInputError, match="season must be 2026-27 or 2025-26"):
        _minutes(world, "2024-25")


def test_the_season_minutes_route_takes_season_as_a_query_parameter(
    client, authenticate, world, dependencies
):
    from app.services.target_season_minutes import TargetSeasonMinutesService

    _activate_new_season(world)
    dependencies.target_season_minutes_service = TargetSeasonMinutesService(
        player_logs=world.logs, settings=world.settings, publication_reader=world.reader
    )
    url = "/api/teams/OKC/season-minutes"

    past = client.get(url, query_string={"season": LAST}, headers=authenticate())
    invalid = client.get(url, query_string={"season": "2027-28"}, headers=authenticate())

    assert past.status_code == 200
    assert (past.get_json()["season"], past.get_json()["season_reason"]) == (
        LAST, "requested"
    )
    assert [player["player_id"] for player in past.get_json()["players"]] == [bt.EMBIID]
    assert invalid.status_code == 400
    assert invalid.get_json()["error"]["code"] == "invalid_input"


# --- published_season and refusal metadata ----------------------------------


def test_every_backtest_body_names_the_published_season(world):
    target = world.saved_target()
    _activate_new_season(world)
    redis = bt.FakeRedis()
    service = world.backtests(redis_client=redis)

    miss, _ = service.backtest(OWNER, target["id"], season=LAST)
    hit, _ = service.backtest(OWNER, target["id"], season=LAST)
    batch, _ = service.backtest_all(OWNER, season=LAST)
    published, _ = service.backtest(OWNER, target["id"])

    assert [
        (body["season"], body["season_reason"], body["published_season"])
        for body in (miss, hit, batch, batch["backtests"][0]["backtest"], published)
    ] == [
        (LAST, "requested", NEW),
        (LAST, "requested", NEW),
        (LAST, "requested", NEW),
        (LAST, "requested", NEW),
        (NEW, "published", NEW),
    ]


def test_a_first_default_refusal_tells_the_client_the_published_season(world):
    """2026-27 game logs activate before its Diet streams."""

    target = world.saved_target()
    world.compose("player_game_logs", NEW, {"rows": _season_logs(NEW, okc_games=1)})

    with pytest.raises(SeasonUnavailableError) as refused:
        world.backtests().backtest(OWNER, target["id"])

    assert refused.value.public_details == {
        "season": NEW, "published_season": NEW, "stream": "exact_shot_zones"
    }


def _drop_live_game_log_projection(world):
    """The live 2026-27 game-log Publication exists but cannot be read."""

    with world.engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM publication_player_game_logs WHERE publication_id = "
            "(SELECT active_publication_id FROM publication_pointers "
            "WHERE stream_key = 'player_game_logs')"
        ))


@pytest.mark.parametrize("redis_client", [None, bt.FakeRedis], ids=("no-cache", "cache"))
def test_an_unreadable_live_stream_is_season_unavailable_not_empty(
    client, authenticate, world, dependencies, redis_client
):
    from app.services.target_season_minutes import TargetSeasonMinutesService

    target = world.saved_target()
    _activate_new_season(world)
    _drop_live_game_log_projection(world)
    dependencies.user_service = world.users
    dependencies.target_backtest_service = world.backtests(
        redis_client=redis_client and redis_client()
    )
    dependencies.target_season_minutes_service = TargetSeasonMinutesService(
        player_logs=world.logs, settings=world.settings, publication_reader=world.reader
    )
    expected = {
        "season": NEW, "published_season": NEW, "stream": "player_game_logs"
    }

    for url in (
        f"/api/user/targets/{target['id']}/backtest",
        "/api/user/targets/backtests",
        "/api/teams/OKC/season-minutes",
    ):
        for query in ({}, {"season": NEW}):
            response = client.get(url, query_string=query, headers=authenticate())
            assert response.status_code == 503, (url, query)
            assert response.get_json()["error"]["code"] == "season_unavailable"
            assert response.get_json()["error"]["details"] == expected
    # The previous season is read retained and unaffected.
    past = client.get(
        f"/api/user/targets/{target['id']}/backtest",
        query_string={"season": LAST},
        headers=authenticate(),
    )
    assert past.status_code == 200


def test_a_preview_names_its_season_reason_and_published_season(world):
    _pin(world, NEW)

    fallback = _preview(world, None)
    requested = _preview(world, LAST)

    assert [
        (body["season"], body["season_reason"], body["published_season"])
        for body in (fallback, requested)
    ] == [(LAST, "fallback_no_games", NEW), (LAST, "requested", NEW)]


@pytest.mark.parametrize("season", [None, 2025, ["2025-26"], {"season": "2025-26"}])
def test_an_explicit_preview_season_that_is_not_a_season_string_is_refused(
    client, authenticate, served, dependencies, season
):
    dependencies.target_preview_service = TargetPreviewService(
        backtests=dependencies.target_backtest_service,
        resolutions=NoTonight(),
        matchups=object(),
        injuries=None,
        settings=served.settings,
        publication_reader=served.reader,
    )
    body = {"opponent": "OKC", "qualifiers": [CORNER_THREE]}

    refused = client.post(
        "/api/user/targets/preview", json={**body, "season": season}, headers=authenticate()
    )

    assert refused.status_code == 400
    assert refused.get_json()["error"]["code"] == "invalid_input"


class IdleTonight:
    def today(self, *_args, **_kwargs):
        return None


def test_an_omitted_preview_season_alone_applies_the_default(
    client, authenticate, served, dependencies
):
    dependencies.target_preview_service = TargetPreviewService(
        backtests=dependencies.target_backtest_service,
        resolutions=IdleTonight(),
        matchups=object(),
        injuries=None,
        settings=served.settings,
        publication_reader=served.reader,
    )

    response = client.post(
        "/api/user/targets/preview",
        json={"opponent": "OKC", "qualifiers": [CORNER_THREE]},
        headers=authenticate(),
    )

    assert response.status_code == 200
    assert _season(response.get_json()) == (NEW, "published")
