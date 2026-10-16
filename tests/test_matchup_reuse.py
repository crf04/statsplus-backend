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
from app.services.matchup_reuse import ComposedMatchupCache, PublicationSnapshotReuse
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

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, ttl, payload):
        self.values[key] = payload
        self.ttls[key] = ttl


class Composer:
    def __init__(self, document=DOCUMENT):
        self.document = document
        self.calls = []

    def get_matchup_from_snapshot(
        self, *, game_id, publication_snapshot, injuries, compose_cache=None, season=None
    ):
        self.calls.append(game_id)
        return self.document


def _snapshot(generation=GENERATION, retained=()):
    return SimpleNamespace(generation=generation, retained_history=retained)


def _read(cache, composer, *, snapshot=None, season="2025-26", game=GAME, settings=None):
    """One request's read of one game, as a fresh ``SnapshotMatchups``."""

    reader = SnapshotMatchups(
        composer,
        snapshot or _snapshot(),
        object(),
        season=season,
        cache=cache,
        settings=settings or _settings(),
    )
    return reader.get_matchup(game_id=game["game_id"], game=game)


def test_a_cached_matchup_is_byte_identical_to_the_composed_one():
    redis_client, composer = DictRedis(), Composer()
    cache = ComposedMatchupCache(redis_client)

    miss = _read(cache, composer)
    hit = _read(cache, composer)

    assert composer.calls == ["G1"]
    assert json.dumps(hit) == json.dumps(miss) == (
        '{"players": [{"canonical_id": 1, "scores": [0.25, null]}], "teams": []}'
    )
    assert list(redis_client.ttls.values()) == [300]
    assert next(iter(redis_client.values)).startswith("matchup:composed:v1:")


@pytest.mark.parametrize(
    "change",
    [
        {"snapshot": _snapshot((("player_game_logs", "pub-2", 1, 8),))},
        {"snapshot": _snapshot(retained=(("player_game_logs", "row-9"),))},
        {"season": "2024-25"},
        {"game": {**GAME, "game_id": "G2"}},
        {"game": {**GAME, "status": {"state": "final", "label": "Final"}}},
        {"game": {**GAME, "scheduled_at": "2026-04-11T01:00:00+00:00"}},
        {
            "settings": RuntimeSettings(
                environment="testing",
                nba={"current_season": "2025-26"},
                matchup_scores={"min_games": 6},
            )
        },
        {
            "settings": RuntimeSettings(
                environment="testing",
                nba={"current_season": "2025-26"},
                player_diet_baseline={"play_types_min_volume_per_game": 60.0},
            )
        },
    ],
    ids=[
        "generation", "retained-history", "season", "game-id", "game-state",
        "tip-time", "score-settings", "diet-baseline-settings",
    ],
)
def test_a_changed_input_misses_the_cache(change):
    redis_client, composer = DictRedis(), Composer()
    cache = ComposedMatchupCache(redis_client)
    _read(cache, composer)

    _read(cache, composer, **change)

    assert composer.calls == ["G1", change.get("game", GAME)["game_id"]]
    assert len(redis_client.values) == 2


def test_a_game_without_its_slate_entry_is_never_cached():
    redis_client, composer = DictRedis(), Composer()
    reader = SnapshotMatchups(
        composer, _snapshot(), object(), season="2025-26",
        cache=ComposedMatchupCache(redis_client), settings=_settings(),
    )

    reader.get_matchup(game_id="G1")
    reader.get_matchup(game_id="G1")

    assert composer.calls == ["G1", "G1"]
    assert redis_client.values == {}


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

    assert first == second == json.loads(json.dumps(DOCUMENT))
    assert composer.calls == ["G1", "G1"]


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
        assert _read(ComposedMatchupCache(client), composer)["teams"] == []
        assert composer.calls == ["G1"]


def test_a_disabled_flag_or_missing_client_never_caches():
    redis_client, composer = DictRedis(), Composer()

    off = _settings(matchup_enabled=False)
    _read(ComposedMatchupCache(redis_client), composer, settings=off)
    _read(ComposedMatchupCache(redis_client), composer, settings=off)
    _read(ComposedMatchupCache(None), composer)
    _read(ComposedMatchupCache(None), composer)

    assert composer.calls == ["G1"] * 4
    assert redis_client.values == {}


def test_a_document_json_cannot_hold_is_returned_uncached():
    redis_client = DictRedis()
    composer = Composer({"when": object()})

    document = _read(ComposedMatchupCache(redis_client), composer)

    assert list(document) == ["when"]
    assert redis_client.values == {}


def test_a_snapshot_without_a_generation_is_never_cached():
    redis_client, composer = DictRedis(), Composer()
    cache = ComposedMatchupCache(redis_client)

    _read(cache, composer, snapshot=_snapshot(generation=()))
    _read(cache, composer, snapshot=_snapshot(generation=()))

    assert composer.calls == ["G1", "G1"]
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

    assert first == second
    assert (reader.captures, composer.calls) == (1, ["G1"])
