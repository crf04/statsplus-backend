"""Snapshot reuse and the composed-Matchup cache (crf04/statsplus-backend#422).

Both are optimisations, so each test names what a request must observe: how
many captures or composes ran, and that a cached document equals the composed
one.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
import redis as redis_package

from app.config.settings import RuntimeSettings
from app.services.matchup_reuse import (
    SECTION_TREATMENT,
    ComposedMatchupCache,
    PublicationSnapshotReuse,
)
from app.services.database_first_activation import (
    PublicationRead,
    PublicationReadSnapshot,
)
from app.services.matchup import build_publication_metadata
from app.services.matchup_snapshot import SnapshotMatchups, request_matchups

GENERATION = (("player_game_logs", "pub-1", 1, 7),)
GAME = {
    "game_id": "G1",
    "status": {"state": "scheduled", "label": "7:00 pm ET"},
    "scheduled_at": "2026-04-10T23:00:00+00:00",
}
DOCUMENT = {"players": [{"canonical_id": 1, "scores": (0.25, None)}], "teams": []}


def _settings(**cache):
    return RuntimeSettings(
        environment="testing", nba={"current_season": "2025-26"}, cache=cache
    )


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Publications:
    """A reader reporting its generation cheaply and counting full captures."""

    captures_published_season = True

    def __init__(self, generation=GENERATION, delay=0.0):
        self.generation_value = generation
        self.delay = delay
        self.captures = 0
        self.checks = 0
        self._lock = threading.Lock()

    def generation(self, stream_keys, *, season=None):
        self.checks += 1
        return self.generation_value

    def snapshot(self, stream_keys, *, season, **_):
        with self._lock:
            self.captures += 1
        time.sleep(self.delay)
        return SimpleNamespace(
            generation=self.generation_value, retained_history=(), season="2025-26"
        )


def _capture(reader):
    return lambda season: reader.snapshot(("player_game_logs",), season=season)


def _reuse_capture(reuse, reader, settings=None):
    return reuse.capture(
        settings or _settings(), reader, ("player_game_logs",), _capture(reader)
    )


# --- snapshot reuse --------------------------------------------------------


def test_a_snapshot_is_captured_once_while_the_generation_holds():
    reader, reuse = Publications(), PublicationSnapshotReuse()

    first = _reuse_capture(reuse, reader)
    second = _reuse_capture(reuse, reader)

    assert reader.captures == 1
    assert reader.checks == 2
    assert second[1] is first[1]
    assert first[0] == "2025-26"


def test_a_new_generation_is_captured_again():
    reader, reuse = Publications(), PublicationSnapshotReuse()
    _reuse_capture(reuse, reader)

    reader.generation_value = (("player_game_logs", "pub-2", 1, 8),)
    _reuse_capture(reuse, reader)

    assert reader.captures == 2


def test_only_the_last_two_generations_are_kept():
    reader, reuse = Publications(), PublicationSnapshotReuse()
    generations = [(("player_game_logs", f"pub-{n}", 1, n),) for n in (1, 2, 3)]
    for generation in generations:
        reader.generation_value = generation
        _reuse_capture(reuse, reader)
    assert reader.captures == 3

    reader.generation_value = generations[1]
    _reuse_capture(reuse, reader)
    assert reader.captures == 3
    reader.generation_value = generations[0]
    _reuse_capture(reuse, reader)

    assert reader.captures == 4


def test_a_snapshot_older_than_the_reuse_age_is_captured_again():
    clock, reader = Clock(), Publications()
    reuse = PublicationSnapshotReuse(clock=clock)
    settings = _settings(snapshot_reuse_max_age_seconds=60)
    _reuse_capture(reuse, reader, settings)

    clock.now = 59
    _reuse_capture(reuse, reader, settings)
    assert reader.captures == 1
    clock.now = 60
    _reuse_capture(reuse, reader, settings)

    assert reader.captures == 2


def test_a_zero_reuse_age_captures_every_time():
    reader, reuse = Publications(), PublicationSnapshotReuse()
    settings = _settings(snapshot_reuse_max_age_seconds=0)

    _reuse_capture(reuse, reader, settings)
    _reuse_capture(reuse, reader, settings)

    assert (reader.captures, reader.checks) == (2, 0)


def test_a_reader_that_cannot_report_its_generation_is_captured_every_time():
    class NoGeneration:
        captures_published_season = True
        captures = 0

        def snapshot(self, stream_keys, *, season, **_):
            type(self).captures += 1
            return SimpleNamespace(generation=GENERATION, season="2025-26")

    reader, reuse = NoGeneration(), PublicationSnapshotReuse()

    _reuse_capture(reuse, reader)
    _reuse_capture(reuse, reader)

    assert NoGeneration.captures == 2


def test_a_failing_generation_check_captures_instead_of_failing():
    reader, reuse = Publications(), PublicationSnapshotReuse()
    reader.generation = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("x"))

    season, snapshot = _reuse_capture(reuse, reader)

    assert (reader.captures, season) == (1, "2025-26")
    assert snapshot.generation == GENERATION


def test_concurrent_requests_share_one_capture():
    reader, reuse = Publications(delay=0.05), PublicationSnapshotReuse()
    results = []

    def request():
        results.append(_reuse_capture(reuse, reader)[1])

    threads = [threading.Thread(target=request) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert reader.captures == 1
    assert len({id(snapshot) for snapshot in results}) == 1


# --- composed Matchup cache ------------------------------------------------


class DictRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}
        self.gets = 0

    def get(self, key):
        self.gets += 1
        return self.values.get(key)

    def setex(self, key, ttl, payload):
        self.values[key] = payload
        self.ttls[key] = ttl


class Composer:
    """Each compose returns its own literal document: ``{"compose": n, ...}``."""

    def __init__(self):
        self.calls = []
        self.retained = {}

    def get_matchup_from_snapshot(
        self, *, game_id, publication_snapshot, injuries, compose_cache=None, season=None
    ):
        self.calls.append(game_id)
        return {"compose": len(self.calls), "game": game_id, "scores": (0.25, None)}


def _snapshot(generation=GENERATION, retained=()):
    return SimpleNamespace(generation=generation, retained_history=retained)


def _read(cache, composer, *, snapshot=None, season="2025-26", game=GAME,
          settings=None, cache_reads=True):
    """One request's read of one game, as a fresh ``SnapshotMatchups``."""

    reader = SnapshotMatchups(
        composer,
        snapshot or _snapshot(),
        object(),
        season=season,
        cache=cache,
        settings=settings or _settings(),
        cache_reads=cache_reads,
    )
    return reader.get_matchup(game_id=game["game_id"], game=game)


def _scored(**overrides):
    return RuntimeSettings(
        environment="testing", nba={"current_season": "2025-26"}, **overrides
    )


def test_a_cached_matchup_is_byte_identical_to_the_composed_one():
    redis_client, composer = DictRedis(), Composer()
    cache = ComposedMatchupCache(redis_client)

    miss = _read(cache, composer)
    hit = _read(cache, composer)

    assert composer.calls == ["G1"]
    assert json.dumps(hit) == json.dumps(miss) == (
        '{"compose": 1, "game": "G1", "scores": [0.25, null]}'
    )
    assert list(redis_client.ttls.values()) == [300]
    assert next(iter(redis_client.values)).startswith("matchup:composed:v2:")


def _pool_game(**changes):
    return {
        **GAME,
        "projection_state": {"state": "live", "observed_at": "2026-04-10T10:00:00+00:00"},
        "away_team": {"targetable_player_count": 9},
        "home_team": {"targetable_player_count": 8},
        **changes,
    }


CHANGES = {
    "generation": {"snapshot": _snapshot((("player_game_logs", "pub-2", 1, 8),))},
    "retained-history": {"snapshot": _snapshot(retained=(("player_game_logs", "row-9"),))},
    "season": {"season": "2024-25"},
    "game-id": {"game": {**GAME, "game_id": "G2"}},
    "game-state": {"game": {**GAME, "status": {"state": "final", "label": "Final"}}},
    "tip-time": {"game": {**GAME, "scheduled_at": "2026-04-11T01:00:00+00:00"}},
    "score-settings": {"settings": _scored(matchup_scores={"min_games": 6})},
    "diet-baseline-settings": {
        "settings": _scored(player_diet_baseline={"play_types_min_volume_per_game": 60.0})
    },
    "schedule-age-setting": {"settings": _scored(catalog={"slate_schedule_max_age_hours": 99})},
    "pinned-season": {
        "settings": RuntimeSettings(environment="testing", nba={"current_season": "2026-27"})
    },
    "unpinned-season": {"settings": RuntimeSettings(environment="testing")},
}


@pytest.mark.parametrize("change", list(CHANGES), ids=list(CHANGES))
def test_a_changed_input_misses_and_returns_the_new_document(change):
    redis_client, composer = DictRedis(), Composer()
    cache = ComposedMatchupCache(redis_client)
    first = _read(cache, composer)
    arguments = CHANGES[change]

    second = _read(cache, composer, **arguments)
    again = _read(cache, composer, **arguments)

    game_id = arguments.get("game", GAME)["game_id"]
    assert first["compose"] == 1
    assert (second["compose"], second["game"]) == (2, game_id)
    assert again == second
    assert composer.calls == ["G1", game_id]


@pytest.mark.parametrize(
    "changed",
    [
        {"projection_state": {"state": "live", "observed_at": "2026-04-10T10:05:00+00:00"}},
        {"projection_state": None},
        {"away_team": {"targetable_player_count": 8}},
        {"home_team": {"targetable_player_count": 7}},
    ],
    ids=["new-poll", "projection-gone", "away-count", "home-count"],
)
def test_a_changed_player_pool_view_misses_and_returns_the_new_document(changed):
    composer = Composer()
    cache = ComposedMatchupCache(DictRedis())
    _read(cache, composer, game=_pool_game())

    second = _read(cache, composer, game=_pool_game(**changed))

    assert second["compose"] == 2
    assert _read(cache, composer, game=_pool_game())["compose"] == 1


def test_a_game_without_its_slate_entry_is_never_cached():
    redis_client, composer = DictRedis(), Composer()
    reader = SnapshotMatchups(
        composer, _snapshot(), object(), season="2025-26",
        cache=ComposedMatchupCache(redis_client), settings=_settings(),
    )

    first = reader.get_matchup(game_id="G1")
    second = reader.get_matchup(game_id="G1")

    assert (first["compose"], second["compose"]) == (1, 2)
    assert redis_client.values == {}


def test_a_reader_that_does_not_read_the_cache_composes_fresh_but_writes_it():
    redis_client, composer = DictRedis(), Composer()
    cache = ComposedMatchupCache(redis_client)

    first = _read(cache, composer)
    fresh = _read(cache, composer, cache_reads=False)
    served = _read(cache, composer)

    assert (first["compose"], fresh["compose"]) == (1, 2)
    assert served["compose"] == 2
    assert composer.calls == ["G1", "G1"]


def test_a_redis_failure_is_a_miss_not_an_error():
    class Down(DictRedis):
        def get(self, key):
            raise redis_package.exceptions.ConnectionError("down")

        def setex(self, key, ttl, payload):
            raise redis_package.exceptions.TimeoutError("down")

    composer = Composer()
    cache = ComposedMatchupCache(Down())

    first = _read(cache, composer)
    second = _read(cache, composer)

    assert (first["compose"], second["compose"]) == (1, 2)


def test_a_read_failure_opens_the_breaker_so_redis_is_not_asked_again():
    class FailingGet(DictRedis):
        def get(self, key):
            self.gets += 1
            raise redis_package.exceptions.ConnectionError("down")

    redis_client, composer = FailingGet(), Composer()
    cache = ComposedMatchupCache(redis_client)

    _read(cache, composer)
    _read(cache, composer)
    _read(cache, composer)

    assert redis_client.gets == 1


def test_nothing_is_written_while_the_breaker_is_open():
    class FailingGet(DictRedis):
        sets = 0

        def get(self, key):
            raise redis_package.exceptions.ConnectionError("down")

        def setex(self, key, ttl, payload):
            type(self).sets += 1

    redis_client, composer = FailingGet(), Composer()

    first = _read(ComposedMatchupCache(redis_client), composer)

    assert (first["compose"], FailingGet.sets) == (1, 0)


@pytest.mark.parametrize(
    "error",
    [redis_package.exceptions.ConnectionError("down"), redis_package.exceptions.TimeoutError("slow")],
)
def test_a_write_failure_opens_the_breaker_so_redis_is_not_asked_again(error):
    class FailingSet(DictRedis):
        def setex(self, key, ttl, payload):
            raise error

    redis_client, composer = FailingSet(), Composer()
    cache = ComposedMatchupCache(redis_client)

    first = _read(cache, composer)
    second = _read(cache, composer)

    assert (first["compose"], second["compose"]) == (1, 2)
    assert redis_client.gets == 1


def test_an_unexpected_redis_error_or_corrupt_entry_is_a_miss():
    class Broken(DictRedis):
        def get(self, key):
            raise RuntimeError("boom")

        def setex(self, key, ttl, payload):
            raise RuntimeError("boom")

    class Corrupt(DictRedis):
        def get(self, key):
            return b"not zlib"

    for client in (Broken(), Corrupt()):
        composer = Composer()
        assert _read(ComposedMatchupCache(client), composer)["compose"] == 1
        assert composer.calls == ["G1"]


def test_a_disabled_flag_or_missing_client_never_caches():
    redis_client, composer = DictRedis(), Composer()

    off = _settings(matchup_enabled=False)
    _read(ComposedMatchupCache(redis_client), composer, settings=off)
    _read(ComposedMatchupCache(redis_client), composer, settings=off)
    _read(ComposedMatchupCache(None), composer)
    last = _read(ComposedMatchupCache(None), composer)

    assert composer.calls == ["G1"] * 4
    assert last["compose"] == 4
    assert redis_client.values == {}


def test_a_document_json_cannot_hold_is_returned_uncached():
    class Odd(Composer):
        def get_matchup_from_snapshot(self, **kwargs):
            return {"when": object()}

    redis_client = DictRedis()

    document = _read(ComposedMatchupCache(redis_client), Odd())

    assert list(document) == ["when"]
    assert redis_client.values == {}


def test_a_snapshot_without_a_generation_is_never_cached():
    redis_client, composer = DictRedis(), Composer()
    cache = ComposedMatchupCache(redis_client)

    _read(cache, composer, snapshot=_snapshot(generation=()))
    second = _read(cache, composer, snapshot=_snapshot(generation=()))

    assert second["compose"] == 2
    assert redis_client.values == {}


# --- retained Publications and request-dependent provenance -------------------


def _read_of(stream, publication_id, *, age, freshness="fresh", season="2025-26"):
    return PublicationRead(
        stream_key=stream,
        publication_id=publication_id,
        season=season,
        cutoff="2026-04-09T00:00:00+00:00",
        version=7,
        status="active",
        freshness=freshness,
        age_seconds=age,
        payload={},
        fence=1,
    )


def _real_snapshot(age, freshness="fresh"):
    reads = {"player_game_logs": _read_of("player_game_logs", "pub-1", age=age, freshness=freshness)}
    return PublicationReadSnapshot(season="2025-26", reads=reads, generation=GENERATION)


class ProvenanceComposer(Composer):
    """Composes the way ``MatchupService`` does: data plus snapshot metadata."""

    def get_matchup_from_snapshot(
        self, *, game_id, publication_snapshot, injuries, compose_cache=None, season=None
    ):
        self.calls.append(game_id)
        sources = {"schedule": {"status": "fresh", "retrieved_at": "2026-04-10T09:00:00+00:00"}}
        retained = [self.retained["read"]] if "read" in self.retained else []
        return {
            "compose": len(self.calls),
            "game": game_id,
            **build_publication_metadata(
                publication_snapshot.metadata(), publication_snapshot, sources, retained
            ),
        }

    def retained_publication(self, season):
        return self.retained.get("found")


def test_a_hit_reports_the_current_snapshots_age_not_the_stored_one():
    composer = ProvenanceComposer()
    cache = ComposedMatchupCache(DictRedis())
    first_capture, second_capture = _real_snapshot(age=0), _real_snapshot(age=60)
    _read(cache, composer, snapshot=first_capture)

    hit = _read(cache, composer, snapshot=second_capture)
    uncached = ProvenanceComposer().get_matchup_from_snapshot(
        game_id="G1", publication_snapshot=second_capture, injuries=None
    )

    assert composer.calls == ["G1"]
    assert hit["provenance"]["generation"][0]["age_seconds"] == 60
    assert hit["provenance"]["generation"][0]["freshness"] == "fresh"
    assert hit["provenance"]["player_game_logs"]["age_seconds"] == 60
    assert hit["coverage"]["mixed_freshness"] is False
    assert json.dumps(hit) == json.dumps(uncached)


def test_a_retained_publication_is_part_of_the_key_and_its_read_is_current():
    composer = ProvenanceComposer()
    cache = ComposedMatchupCache(DictRedis())
    snapshot = _real_snapshot(age=0)
    # A 2024-25 game id: an earlier season than the 2025-26 evidence.
    game = {**GAME, "game_id": "0022400001"}
    read_v1 = _read_of("player_game_logs", "ret-1", age=1000, season="2024-25")
    composer.retained.update(found=("ret-1", 4, read_v1), read=read_v1)
    composer.old_game_id = game["game_id"]

    def read():
        return SnapshotMatchups(
            composer, snapshot, object(), season="2025-26",
            cache=cache, settings=RuntimeSettings(environment="testing"),
        ).get_matchup(game_id=game["game_id"], game=game)

    first, again = read(), read()

    assert first["compose"] == again["compose"] == 1
    assert [e["publication_id"] for e in again["provenance"]["generation"]] == [
        "ret-1", "pub-1",
    ]

    corrected = _read_of("player_game_logs", "ret-2", age=5, season="2024-25")
    composer.retained.update(found=("ret-2", 5, corrected), read=corrected)
    after = read()

    assert after["compose"] == 2
    assert [e["publication_id"] for e in after["provenance"]["generation"]] == [
        "ret-2", "pub-1",
    ]


def test_either_half_of_the_retained_identity_alone_misses():
    composer = ProvenanceComposer()
    cache = ComposedMatchupCache(DictRedis())
    snapshot = _real_snapshot(age=0)
    game = {**GAME, "game_id": "0022400001"}

    def read(publication_id, fence):
        composer.retained.update(
            found=(publication_id, fence, _read_of(
                "player_game_logs", publication_id, age=9, season="2024-25"
            ))
        )
        return SnapshotMatchups(
            composer, snapshot, object(), season="2025-26",
            cache=cache, settings=RuntimeSettings(environment="testing"),
        ).get_matchup(game_id=game["game_id"], game=game)["compose"]

    assert [
        read("ret-1", 4), read("ret-1", 4), read("ret-1", 5), read("ret-2", 5),
    ] == [1, 1, 2, 3]


def test_an_earlier_season_game_whose_retained_read_cannot_be_named_is_not_cached():
    redis_client, composer = DictRedis(), Composer()
    game = {**GAME, "game_id": "0022400001"}

    first = _read(ComposedMatchupCache(redis_client), composer, game=game,
                  settings=RuntimeSettings(environment="testing"))
    second = _read(ComposedMatchupCache(redis_client), composer, game=game,
                   settings=RuntimeSettings(environment="testing"))

    assert (first["compose"], second["compose"]) == (1, 2)
    assert redis_client.values == {}


def test_request_matchups_reuses_the_snapshot_and_the_cached_matchup():
    redis_client, composer, reader = DictRedis(), Composer(), Publications()
    cache, reuse = ComposedMatchupCache(redis_client), PublicationSnapshotReuse()

    def request():
        return request_matchups(
            composer=composer,
            publication_reader=reader,
            injuries=object(),
            settings=_settings(),
            snapshot_reuse=reuse,
            cache=cache,
        ).get_matchup(game_id="G1", game=GAME)

    first, second = request(), request()

    assert first == second == {"compose": 1, "game": "G1", "scores": [0.25, None]}
    assert (reader.captures, composer.calls) == (1, ["G1"])


# --- round 2: the real services and the reviewer's reproductions -------------


def _real_service_cache(cache):
    import tests.services.test_matchup_service as service_doubles
    from app.services.player_game_log_repository import PlayerGameLogRepository

    service = service_doubles._service()
    repo = SimpleNamespace(_publication_reader=object())
    # The real repository method: with a serving snapshot it needs no database.
    service.player_logs.get_read_freshness = (
        lambda season, publication_snapshot: PlayerGameLogRepository.get_read_freshness(
            repo, season, publication_snapshot=publication_snapshot
        )
    )
    game = service._game(service_doubles._event())

    def cached(snapshot):
        return SnapshotMatchups(
            service, snapshot, None, season="2025-26", cache=cache,
            settings=service.settings,
        ).get_matchup(game_id=service_doubles.GAME_ID, game=game)

    def uncached(snapshot):
        return service.get_matchup_from_snapshot(
            game_id=service_doubles.GAME_ID, publication_snapshot=snapshot,
            injuries=None, season="2025-26",
        )

    return service, cached, uncached


def test_a_fresh_then_stale_capture_of_one_generation_equals_an_uncached_compose():
    cache = ComposedMatchupCache(DictRedis())
    service, cached, uncached = _real_service_cache(cache)
    cached(_real_snapshot(age=0))
    stale = _real_snapshot(age=60, freshness="stale")

    hit = cached(stale)
    expected = uncached(stale)

    assert expected["freshness"]["player_game_logs"]["status"] == "stale"
    assert hit["freshness"]["player_game_logs"]["status"] == "stale"
    assert json.dumps(hit) == json.dumps(expected)


def test_an_older_capture_with_the_same_labels_is_a_hit_equal_to_an_uncached_compose():
    cache = ComposedMatchupCache(DictRedis())
    service, cached, uncached = _real_service_cache(cache)
    cached(_real_snapshot(age=0))
    composes = len(service.player_logs.summary_calls)
    older = _real_snapshot(age=60)

    hit = cached(older)

    assert len(service.player_logs.summary_calls) == composes
    assert hit["provenance"]["player_game_logs"]["age_seconds"] == 60
    assert json.dumps(hit) == json.dumps(uncached(older))


def test_a_retained_publication_activated_between_lookup_and_compose_is_not_cached():
    redis_client, composer = DictRedis(), ProvenanceComposer()
    snapshot = _real_snapshot(age=0)
    looked_up = _read_of("player_game_logs", "ret-A", age=1000, season="2024-25")
    composed = _read_of("player_game_logs", "ret-B", age=1000, season="2024-25")
    # The lookup names A; composition reads B, activated in between.
    composer.retained.update(found=("ret-A", 4, looked_up), read=composed)
    game = {**GAME, "game_id": "0022400001"}

    def read():
        return SnapshotMatchups(
            composer, snapshot, object(), season="2025-26",
            cache=ComposedMatchupCache(redis_client),
            settings=RuntimeSettings(environment="testing"),
        ).get_matchup(game_id=game["game_id"], game=game)

    first, second = read(), read()

    assert [e["publication_id"] for e in first["provenance"]["generation"]] == [
        "ret-B", "pub-1",
    ]
    assert (first["compose"], second["compose"]) == (1, 2)
    assert redis_client.values == {}


def test_one_providers_new_poll_misses_though_the_oldest_stamp_and_counts_hold():
    import tests.services.test_projection_archive as archive
    from datetime import timedelta
    from sqlalchemy import create_engine

    from app.migrations import run_migrations
    from app.providers.dfs import NBAMarketQuery
    from app.services.projection_archive import (
        LatestProjectionPlayerPoolReader,
        ProjectionArchive,
        ProjectionArchiveReadScope,
    )
    from app.services.statistic_catalog import StatisticCatalog
    import tempfile

    t0, season, game_id = archive.OBSERVED_AT, archive.SEASON, archive.GAME_ID
    with tempfile.TemporaryDirectory() as temp:
        engine = create_engine(f"sqlite:///{temp}/pool.sqlite3")
        run_migrations(engine)
        catalog = StatisticCatalog.load_default()
        projections = ProjectionArchive(engine, catalog)
        query = NBAMarketQuery(season=season)

        def put(provider, minutes, line):
            at = t0 + timedelta(minutes=minutes)
            projections.ingest_complete_snapshot(
                archive._line_snapshot(provider, [archive._line_market(
                    catalog, provider=provider, market_id="pts",
                    statistic_id="points", threshold=line,
                )], at),
                query=query, accepted_at=at,
            )

        put("dabble", 0, "20.5")
        put("underdog", 1, "21.5")
        reader = LatestProjectionPlayerPoolReader(
            engine,
            [ProjectionArchiveReadScope(provider=p, query=query) for p in ("dabble", "underdog")],
            clock=lambda: t0 + timedelta(minutes=3),
        )
        first = reader.get_pool_for_game(season=season, game_id=game_id)
        put("underdog", 2, "25.5")
        second = reader.get_pool_for_game(season=season, game_id=game_id)

        def game(pool):
            return {
                **GAME, "game_id": game_id,
                "projection_state": pool.game_states[game_id],
                "away_team": {"targetable_player_count": 0},
                "home_team": {"targetable_player_count": pool.team_counts[1610612747]},
            }

        class LineComposer(Composer):
            pool = first

            def get_matchup_from_snapshot(self, **kwargs):
                return {"lines": self.pool.players[0].lines}

        composer = LineComposer()
        cache = ComposedMatchupCache(DictRedis())

        def read(pool, *, cached=True):
            return SnapshotMatchups(
                composer, _snapshot(), None, season=season, settings=_settings(),
                cache=cache if cached else None,
            ).get_matchup(game_id=game_id, game=game(pool), pool=pool.freshness)

        old = read(first)
        composer.pool = second
        hit = read(second)

        assert game(first) == game(second)
        assert hit == read(second, cached=False)
        assert hit != old


def test_a_hit_reports_the_requests_own_slate_schedule_stamp_everywhere():
    import tests.services.test_matchup_service as service_doubles
    from datetime import timedelta

    cache = ComposedMatchupCache(DictRedis())
    service, _, uncached = _real_service_cache(cache)
    snapshot = _real_snapshot(age=0)
    game = service._game(service_doubles._event())

    def cached(schedule):
        return SnapshotMatchups(
            service, snapshot, None, season="2025-26", cache=cache,
            settings=service.settings,
        ).get_matchup(game_id=service_doubles.GAME_ID, game=game, schedule=schedule)

    def slate_schedule():
        return {
            "status": "fresh",
            "retrieved_at": service.event_catalog.retrieved_at.isoformat(),
        }

    cached(slate_schedule())
    composes = len(service.player_logs.summary_calls)
    # A schedule refresh moves the stamp, not the key.
    service.event_catalog.retrieved_at += timedelta(minutes=1)

    hit = cached(slate_schedule())
    expected = uncached(snapshot)

    assert len(service.player_logs.summary_calls) == composes + 1  # only `expected`
    stamp = service.event_catalog.retrieved_at.isoformat()
    assert hit["provenance"]["sources"]["schedule"]["retrieved_at"] == stamp
    assert hit["freshness"]["schedule"]["retrieved_at"] == stamp
    assert hit["experience"]["sections"]["schedule"]["collected_at"] == stamp
    assert json.dumps(hit) == json.dumps(expected)


# --- the whole class: every input the composer reads ------------------------


def _section_paths(document):
    """The sections the treatment table must name: top level, plus the nested
    sections of ``freshness`` and ``experience``."""

    paths = set()
    for name, value in document.items():
        if name == "freshness":
            paths |= {f"freshness.{key}" for key in value}
        elif name == "experience":
            paths |= {"experience.mode", "experience.player_source"}
            paths |= {f"experience.sections.{key}" for key in value["sections"]}
        else:
            paths.add(name)
    return paths


def test_the_treatment_table_names_exactly_the_sections_of_a_real_matchup():
    from app.services.matchup_reuse import (
        ACCEPTED_LAG,
        ACCEPTED_LAG_INPUTS,
        KEYED,
        REBUILT,
    )

    _, _, uncached = _real_service_cache(ComposedMatchupCache(DictRedis()))

    paths = _section_paths(uncached(_real_snapshot(age=0)))

    assert set(SECTION_TREATMENT) == paths
    assert {t for entry in SECTION_TREATMENT.values() for t in entry[0]} == {
        KEYED, REBUILT, ACCEPTED_LAG,
    }
    assert all(reason for _, reason, _ in SECTION_TREATMENT.values())
    # A section lists accepted-lag inputs exactly when it is classified so, and
    # every named input is described once and used.
    for path, (treatments, _, inputs) in SECTION_TREATMENT.items():
        assert (ACCEPTED_LAG in treatments) is bool(inputs), path
        assert set(inputs) <= set(ACCEPTED_LAG_INPUTS), path
    assert {
        name for _, _, inputs in SECTION_TREATMENT.values() for name in inputs
    } == set(ACCEPTED_LAG_INPUTS)
    assert all(ACCEPTED_LAG_INPUTS.values())


def _poison(entry_value, path):
    """``entry_value`` (a stored document) with the section at ``path`` replaced
    by a marker, keeping the shape the rebuild step reads."""

    *parents, leaf = path.split(".")
    node = entry_value
    for key in parents:
        node = node[key]
    if path == "provenance":
        node[leaf] = {**node[leaf], "POISON": True}
    elif path == "experience.sections.schedule":
        node[leaf] = {**node[leaf], "context": "POISON", "collected_at": "POISON"}
    else:
        node[leaf] = "POISON"


def _value_at(document, path):
    for key in path.split("."):
        document = document[key]
    return document


@pytest.mark.parametrize("path", sorted(SECTION_TREATMENT))
def test_rebuilt_sections_are_rebuilt_and_the_others_are_served_from_the_store(path):
    import zlib

    from app.services.matchup_reuse import REBUILT

    doubles, service, clock, cached, uncached = _matrix_world()
    snapshot = _real_snapshot(age=0)
    redis_client = cached.cache._redis
    cached(snapshot)
    ((key, payload),) = redis_client.values.items()
    entry = json.loads(zlib.decompress(payload))
    _poison(entry["matchup"], path)
    redis_client.values[key] = zlib.compress(json.dumps(entry).encode("utf-8"))
    composes = len(service.player_logs.summary_calls)

    hit = cached(snapshot)
    expected = uncached(snapshot)

    assert len(service.player_logs.summary_calls) == composes + 1  # a hit
    if REBUILT not in SECTION_TREATMENT[path][0]:
        assert _value_at(hit, path) == "POISON", f"{path} is not served from the store"
    elif path == "experience.sections.schedule":
        section = _value_at(hit, path)
        assert section["context"] == "POISON"
        assert section["collected_at"] == _value_at(expected, path)["collected_at"]
    else:
        assert _value_at(hit, path) == _value_at(expected, path), f"{path} is not rebuilt"
        assert "POISON" not in json.dumps(_value_at(hit, path))


def _matrix_world():
    import tests.services.test_matchup_service as doubles
    from app.domain.utc import assume_utc

    cache = ComposedMatchupCache(DictRedis())
    service, _, uncached = _real_service_cache(cache)
    clock = {"now": doubles.NOW}
    service._clock = lambda: clock["now"]
    catalog = service.event_catalog
    catalog.get_freshness = lambda season, *, now: {
        "last_success_at": catalog.retrieved_at.isoformat()
    }

    def cached(snapshot):
        slate_game = service._game(catalog.events[0])
        schedule = service._schedule_freshness(
            doubles.SEASON, observed_at=assume_utc(clock["now"])
        )
        return SnapshotMatchups(
            service, snapshot, None, season=doubles.SEASON, cache=cache,
            settings=service.settings,
        ).get_matchup(
            game_id=doubles.GAME_ID, game=slate_game,
            pool=service.player_pool.pool.freshness, schedule=schedule,
        )

    cached.cache = cache
    return doubles, service, clock, cached, uncached


def _vary_status(doubles, service, clock):
    service.event_catalog.events[0].update(status_text="Q2 10:00", status_code=2)


def _vary_tip_time(doubles, service, clock):
    service.event_catalog.events[0]["scheduled_at"] = "2026-01-16T01:30:00+00:00"


def _vary_tip_passes(doubles, service, clock):
    from datetime import datetime, timezone

    clock["now"] = datetime(2026, 1, 16, 1, tzinfo=timezone.utc)


def _vary_schedule_stamp(doubles, service, clock):
    from datetime import timedelta

    service.event_catalog.retrieved_at += timedelta(minutes=1)


def _vary_pool_provider_stamp(doubles, service, clock):
    from dataclasses import replace

    pool = service.player_pool.pool
    providers = {
        name: {**block, "retrieved_at": "2026-01-15T10:05:00+00:00"}
        for name, block in pool.freshness["providers"].items()
    }
    service.player_pool.pool = replace(
        pool, freshness={**pool.freshness, "providers": providers}
    )


def _vary_stats_completion(doubles, service, clock):
    from datetime import timedelta

    from app.services.stats_freshness_repository import StatsFreshness

    later = doubles.RETRIEVED_AT + timedelta(hours=1)
    service.stats_freshness = SimpleNamespace(get=lambda: StatsFreshness(later))


def _vary_other_game_final(doubles, service, clock):
    service.event_catalog.events.append({
        **doubles._event(), "nba_game_id": "0022500583",
        "scheduled_at": "2026-01-15T11:00:00+00:00",
        "status_text": "Final", "status_code": 3,
    })


# name -> (change, snapshot after the change, whether the read must still hit)
MATRIX = {
    "game status and clock": (_vary_status, None, False),
    "tip time moved": (_vary_tip_time, None, False),
    "tip time passes": (_vary_tip_passes, None, False),
    "schedule stamp": (_vary_schedule_stamp, None, True),
    "pool per-provider stamp": (_vary_pool_provider_stamp, None, False),
    "stream freshness labels": (
        lambda *a: None, lambda: _real_snapshot(age=60, freshness="stale"), False
    ),
    "snapshot age": (lambda *a: None, lambda: _real_snapshot(age=60), True),
    "stats completion": (_vary_stats_completion, None, True),
    "another game goes final": (_vary_other_game_final, None, True),
}


# This matrix covers the inputs the composer is known to read today; it cannot
# catch an arbitrary new input, which is what SECTION_TREATMENT is for.
@pytest.mark.parametrize("name", list(MATRIX), ids=list(MATRIX))
def test_a_cached_read_equals_an_uncached_compose_after_each_input_changes(name):
    change, snapshot_after, must_hit = MATRIX[name]
    doubles, service, clock, cached, uncached = _matrix_world()
    baseline = _real_snapshot(age=0)
    cached(baseline)
    composes = len(service.player_logs.summary_calls)

    change(doubles, service, clock)
    snapshot = baseline if snapshot_after is None else snapshot_after()
    hit = cached(snapshot)
    expected = uncached(snapshot)

    assert json.dumps(hit) == json.dumps(expected)
    # The cache still works for what is rebuilt: only the uncached compose ran.
    assert (len(service.player_logs.summary_calls) == composes + 1) is must_hit, name
