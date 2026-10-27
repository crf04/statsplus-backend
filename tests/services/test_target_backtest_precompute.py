"""The cron precompute fills the saved-Target Backtest cache (#363).

Driven over the real season seams of ``test_backtest_seasons``: Publications
composed into a migrated SQLite database, the real publication reader, game
log and Diet repositories, and the Backtest service over them, with an
in-memory Redis.  A generation bump is a new ``player_game_logs``
Publication, which changes every result-cache key.
"""

from __future__ import annotations

import ast
import logging
from datetime import timedelta
from pathlib import Path

import pytest
import redis
from sqlalchemy import create_engine, text

from app.config.settings import CacheSettings
from app.models.athlete_catalog import AthleteCatalog
from app.models.user import User
from app.services.athlete_catalog_service import AthleteCatalogReader
from scripts import player_shooting_refresh
from tests import test_target_backtest as bt
from tests.services import test_backtest_seasons as seasons
from tests.services.test_backtest_seasons import LAST, NEW, NOW, OWNER

STALE_OWNER = "stale-uid"


@pytest.fixture
def world(tmp_path):
    built = seasons.World(create_engine(f"sqlite:///{tmp_path / 'seasons.sqlite3'}"))
    built.publish_season(LAST, okc_games=2, corner_three=0.42)
    yield built
    built.engine.dispose()


class CountingRedis(bt.FakeRedis):
    """The in-memory Redis, also answering and recording ``EXISTS``."""

    def __init__(self):
        super().__init__()
        self.exists_calls = []

    def exists(self, key):
        self.exists_calls.append(key)
        return int(key in self.store)


def _two_targets(world):
    """Two of ``OWNER``'s Targets; ``first`` was updated later, so runs first."""

    first = world.saved_target()
    second = world.users.create_target(OWNER, opponent="BOS", qualifiers=[seasons.CORNER_THREE])
    _updated(world, first, NOW - timedelta(days=1))
    _updated(world, second, NOW - timedelta(days=2))
    return first, second


def _updated(world, target, stamp):
    with world.engine.begin() as connection:
        connection.execute(
            text("UPDATE targets SET updated_at = :stamp WHERE id = :id"),
            {"stamp": stamp, "id": target["id"]},
        )


def _add_owner(world, uid, last_login):
    with world.engine.begin() as connection:
        connection.execute(
            User.__table__.insert(),
            {
                "firebase_uid": uid,
                "email": f"{uid}@example.com",
                "display_name": uid,
                "photo_url": None,
                "created_at": NOW - timedelta(days=30),
                "last_login": last_login,
                "is_active": True,
            },
        )


def _computed_in_order(service, monkeypatch):
    """Record the (Target id, season) of every entry the run computes."""

    order = []
    compute = service._backtest_stateful

    def record(target, **kwargs):
        order.append((target["id"], kwargs["choice"].season))
        return compute(target, **kwargs)

    monkeypatch.setattr(service, "_backtest_stateful", record)
    return order


def _stale_target(world):
    """A Target whose owner last logged in 15 days before ``NOW``.

    Its opponent differs from every recent Target's: keys exclude the owner,
    so an identical definition would share a recent Target's entry.
    """

    _add_owner(world, STALE_OWNER, NOW - timedelta(days=15))
    return world.users.create_target(STALE_OWNER, opponent="LAL", qualifiers=[seasons.CORNER_THREE])


def _bump_generation(world):
    world.compose("player_game_logs", LAST, {"rows": seasons._season_logs(LAST, okc_games=1)})


def _with_budget(world, seconds):
    world.settings = world.settings.model_copy(
        update={"cache": CacheSettings(target_backtest_precompute_budget_seconds=seconds)}
    )


def _statuses(service, uid=OWNER):
    body, _ = service.backtest_all(uid)
    return [item["status"] for item in body["backtests"]]


def _counts(**values):
    return {
        "seasons": [LAST],
        "eligible": 2,
        "already_cached": 0,
        "computed": 0,
        "bypassed": 0,
        "failed": 0,
        "remaining": 0,
        **values,
    }


def _without_duration(result):
    assert isinstance(result.pop("duration_ms"), int)
    return result


def test_after_a_generation_bump_both_targets_are_hits_on_first_read(world):
    first, second = _two_targets(world)
    service = world.backtests(redis_client=CountingRedis())
    service.precompute(now=NOW)
    _bump_generation(world)
    assert _statuses(service) == ["uncached", "uncached"]

    result = service.precompute(now=NOW)

    assert _without_duration(result) == _counts(computed=2)
    assert _statuses(service) == ["ok", "ok"]
    assert service.backtest(OWNER, first["id"])[1] == "hit"
    assert service.backtest(OWNER, second["id"])[1] == "hit"


def test_a_target_whose_owner_has_not_logged_in_for_14_days_is_not_computed(world):
    world.saved_target()
    _stale_target(world)
    service = world.backtests(redis_client=CountingRedis())

    result = service.precompute(now=NOW)

    assert _without_duration(result) == _counts(eligible=1, computed=1)
    assert _statuses(service, STALE_OWNER) == ["uncached"]
    assert _statuses(service) == ["ok"]


def test_a_second_run_with_nothing_changed_computes_nothing(world, monkeypatch):
    _two_targets(world)
    redis = CountingRedis()
    service = world.backtests(redis_client=redis)
    service.precompute(now=NOW)
    writes = len(redis.sets)
    monkeypatch.setattr(service, "_backtest_stateful", lambda *a, **k: pytest.fail("computed"))

    result = service.precompute(now=NOW)

    assert _without_duration(result) == _counts(already_cached=2)
    assert len(redis.sets) == writes


def test_a_spent_budget_stops_after_the_current_target_and_the_next_run_resumes(world, monkeypatch):
    first, second = _two_targets(world)
    redis_client = CountingRedis()
    _with_budget(world, 0)
    stopped = world.backtests(redis_client=redis_client).precompute(now=NOW)
    _with_budget(world, 180)
    service = world.backtests(redis_client=redis_client)
    order = _computed_in_order(service, monkeypatch)

    resumed = service.precompute(now=NOW)

    assert _without_duration(stopped) == _counts(computed=1, remaining=1)
    assert _without_duration(resumed) == _counts(already_cached=1, computed=1)
    assert order == [(second["id"], LAST)]
    assert _statuses(service) == ["ok", "ok"]


def test_a_spent_budget_also_stops_a_run_of_cached_entries(world):
    """Each clock read is ten seconds later, so the five-second budget is
    spent by the first entry's check whether it computed or not."""

    _two_targets(world)
    redis_client = CountingRedis()
    world.backtests(redis_client=redis_client).precompute(now=NOW)
    _with_budget(world, 5)
    ticks = iter(range(0, 1000, 10))

    result = world.backtests(redis_client=redis_client).precompute(now=NOW, clock=lambda: next(ticks))

    assert result == {**_counts(already_cached=1, remaining=1), "duration_ms": 20000}


def test_the_published_season_is_filled_first_and_each_season_has_an_entry(world, monkeypatch):
    first, second = _two_targets(world)
    seasons._activate_new_season(world)
    redis_client = CountingRedis()
    service = world.backtests(redis_client=redis_client)
    order = _computed_in_order(service, monkeypatch)

    result = service.precompute(now=NOW)

    assert _without_duration(result) == _counts(seasons=[NEW, LAST], eligible=4, computed=4)
    assert order == [
        (first["id"], NEW),
        (second["id"], NEW),
        (first["id"], LAST),
        (second["id"], LAST),
    ]
    reader = world.backtests(redis_client=redis_client)
    assert reader.backtest(OWNER, first["id"], season=NEW)[1] == "hit"
    assert reader.backtest(OWNER, first["id"], season=LAST)[1] == "hit"


def test_targets_are_filled_by_owner_login_then_target_update_newest_first(world, monkeypatch):
    first, second = _two_targets(world)
    _add_owner(world, "earlier-uid", NOW - timedelta(days=1))
    earlier = world.users.create_target("earlier-uid", opponent="LAL", qualifiers=[seasons.CORNER_THREE])
    # Updated most recently of all, but its owner logged in less recently.
    _updated(world, earlier, NOW)
    service = world.backtests(redis_client=CountingRedis())
    order = _computed_in_order(service, monkeypatch)

    service.precompute(now=NOW)

    assert order == [(first["id"], LAST), (second["id"], LAST), (earlier["id"], LAST)]


def test_an_unavailable_season_is_skipped(world):
    """Only 2025-26 was ever published, so 2026-27 is season_unavailable."""

    _two_targets(world)
    service = world.backtests(redis_client=CountingRedis())

    result = service.precompute(now=NOW)

    assert result["seasons"] == [LAST]


def test_one_targets_failure_is_logged_and_the_run_continues(world, monkeypatch, caplog):
    first, second = _two_targets(world)
    service = world.backtests(redis_client=CountingRedis())
    compute = service._backtest_stateful

    def fail_the_first(target, **kwargs):
        if target["id"] == first["id"]:
            raise RuntimeError("boom")
        return compute(target, **kwargs)

    monkeypatch.setattr(service, "_backtest_stateful", fail_the_first)
    with caplog.at_level(logging.INFO, logger="app.services.target_backtest"):
        result = service.precompute(now=NOW)

    assert _without_duration(result) == _counts(computed=1, failed=1)
    assert f"Target backtest precompute failed target_id={first['id']} season={LAST}" in caplog.messages
    # ``first`` ran first; the Target after it was still filed.
    assert service.backtest(OWNER, second["id"])[1] == "hit"


class DeadRedis(CountingRedis):
    """A Redis whose ``EXISTS`` fails the way an outage does."""

    def __init__(self, error):
        super().__init__()
        self.error = error

    def exists(self, key):
        self.exists_calls.append(key)
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        redis.exceptions.ConnectionError("connection refused"),
        redis.exceptions.TimeoutError("timed out"),
    ],
)
def test_an_unreachable_redis_stops_the_run_and_opens_the_circuit(world, monkeypatch, caplog, error):
    _two_targets(world)
    dead = DeadRedis(error)
    service = world.backtests(redis_client=dead)
    monkeypatch.setattr(service, "_backtest_stateful", lambda *a, **k: pytest.fail("computed"))

    with caplog.at_level(logging.INFO, logger="app.services.target_backtest"):
        first = service.precompute(now=NOW)
        again = service.precompute(now=NOW)

    assert _without_duration(first) == _counts(bypassed=2)
    assert _without_duration(again) == _counts(bypassed=2)
    # The second run met the open circuit and called Redis not at all.
    assert len(dead.exists_calls) == 1
    assert "Target backtest precompute stopped: result cache unavailable" in caplog.messages


def test_the_run_logs_one_structured_line(world, caplog):
    _two_targets(world)
    service = world.backtests(redis_client=CountingRedis())

    with caplog.at_level(logging.INFO, logger="app.services.target_backtest"):
        result = service.precompute(now=NOW)

    lines = [m for m in caplog.messages if m.startswith("target_backtest_precompute")]
    assert lines == [
        "target_backtest_precompute seasons=2025-26 eligible=2 already_cached=0 "
        f"computed=2 bypassed=0 failed=0 remaining=0 duration_ms={result['duration_ms']}"
    ]


@pytest.mark.parametrize(
    ("redis_client", "enabled", "cause"),
    [
        (None, True, "no_redis_client"),
        (CountingRedis(), False, "TARGET_BACKTEST_CACHE_ENABLED=false"),
    ],
)
def test_with_the_cache_off_nothing_is_read_or_computed(
    world, monkeypatch, caplog, redis_client, enabled, cause
):
    _two_targets(world)
    world.settings = world.settings.model_copy(
        update={"cache": CacheSettings(target_backtest_enabled=enabled)}
    )
    service = world.backtests(redis_client=redis_client)
    monkeypatch.setattr(service, "_backtest_stateful", lambda *a, **k: pytest.fail("computed"))

    with caplog.at_level(logging.INFO, logger="app.services.target_backtest"):
        assert service.precompute(now=NOW) is None

    assert caplog.messages == [f"target_backtest_precompute skipped reason=cache_disabled cause={cause}"]
    if redis_client is not None:
        assert (redis_client.gets, redis_client.sets, redis_client.exists_calls) == ([], [], [])


# --- the cron command ---------------------------------------------------------


@pytest.fixture
def cron(world, monkeypatch):
    """The cron command over the world's database, with an in-memory Redis."""

    monkeypatch.setenv("DATABASE_URL", str(world.engine.url))
    monkeypatch.delenv("NBA_CURRENT_SEASON", raising=False)
    redis = CountingRedis()
    monkeypatch.setattr("app.utils.cache_config.get_redis_client", lambda settings=None: redis)
    return redis


def _tick_line(capsys):
    return capsys.readouterr().out.splitlines()[0]


def test_the_cron_command_fills_the_cache_after_an_idle_tick(world, cron, monkeypatch, capsys):
    first, second = _two_targets(world)
    monkeypatch.setattr(
        player_shooting_refresh,
        "datetime",
        type("Frozen", (), {"now": staticmethod(lambda tz=None: NOW)}),
    )

    assert player_shooting_refresh.main([]) == 0

    assert _tick_line(capsys) == '{"composed_jobs": 0, "state": "no_active_season"}'
    service = world.backtests(redis_client=cron, athlete_catalog=AthleteCatalogReader(world.engine))
    assert _statuses(service) == ["ok", "ok"]


@pytest.mark.parametrize("failure", ["build", "tick"])
def test_the_step_runs_after_the_tick_and_its_failure_changes_nothing(
    world, cron, monkeypatch, capsys, caplog, failure
):
    calls = []
    if failure == "build":

        def broken(*args, **kwargs):
            calls.append(capsys.readouterr().out)
            raise RuntimeError("no dependencies")

        monkeypatch.setattr("app.dependencies.build_dependencies", broken)
        assert player_shooting_refresh.main([]) == 0
        assert calls == ['{"composed_jobs": 0, "state": "no_active_season"}\n']
        assert "target_backtest_precompute failed" in caplog.messages
    else:
        monkeypatch.setattr(
            player_shooting_refresh,
            "active_season",
            lambda engine: (_ for _ in ()).throw(RuntimeError("tick failed")),
        )
        monkeypatch.setattr(
            player_shooting_refresh,
            "_precompute_target_backtests",
            lambda: calls.append("precompute"),
        )
        with pytest.raises(RuntimeError, match="tick failed"):
            player_shooting_refresh.main([])
        assert calls == ["precompute"]


@pytest.mark.parametrize(
    ("variable", "cause"),
    [
        ("ENABLE_CACHE", "ENABLE_CACHE=false"),
        ("TARGET_BACKTEST_CACHE_ENABLED", "TARGET_BACKTEST_CACHE_ENABLED=false"),
    ],
)
def test_with_the_cache_off_the_command_builds_nothing_and_exits_as_the_tick(
    world, cron, monkeypatch, capsys, caplog, variable, cause
):
    monkeypatch.setenv(variable, "false")
    monkeypatch.setattr(
        "app.dependencies.build_dependencies",
        lambda *a, **k: pytest.fail("built dependencies"),
    )

    assert player_shooting_refresh.main([]) == 0

    assert _tick_line(capsys) == '{"composed_jobs": 0, "state": "no_active_season"}'
    assert f"target_backtest_precompute skipped reason=cache_disabled cause={cause}" in caplog.messages
    assert (cron.gets, cron.sets, cron.exists_calls) == ([], [], [])


def test_the_precompute_is_reachable_only_from_the_cron_command():
    """No route, ``create_app`` path, or gunicorn hook calls it."""

    root = Path(__file__).resolve().parents[2]
    sources = [
        *sorted((root / "app").rglob("*.py")),
        *sorted((root / "scripts").rglob("*.py")),
        *sorted(root.glob("*.py")),
    ]
    callers = set()
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", None))
                if name in {"precompute", "_precompute_target_backtests"}:
                    callers.add(str(path.relative_to(root)))
    assert callers == {"scripts/player_shooting_refresh.py"}


def exercise_position_and_average_targets(world):
    """Position and Average Targets, over the real catalog table and log seams.

    LeBron plays five games averaging eight three-point attempts and is
    listed as a Wing.  Shared with the PostgreSQL run, where the migrated
    ``athlete_catalog.position`` and ``target_qualifiers`` columns are real.
    """

    with world.engine.begin() as connection:
        connection.execute(
            AthleteCatalog.__table__.insert().values(
                season=LAST,
                player_id=bt.LEBRON,
                display_name="LeBron James",
                roster_status="active",
                is_active=True,
                is_active_for_season=True,
                team_id=bt.LAL,
                team_name="Los Angeles Lakers",
                team_abbreviation="LAL",
                position="Wing",
                published_at=NOW,
            )
        )
    wing_shooter = world.users.create_target(
        OWNER,
        opponent="OKC",
        qualifiers=[bt.WING, bt._average("3PA", 7.5)],
    )
    big = world.users.create_target(OWNER, opponent="OKC", qualifiers=[bt.BIG])
    heavy_shooter = world.users.create_target(OWNER, opponent="OKC", qualifiers=[bt._average("3PA", 8.5)])
    service = world.backtests(
        redis_client=CountingRedis(), athlete_catalog=AthleteCatalogReader(world.engine)
    )

    result = service.precompute(now=NOW)

    assert _without_duration(result) == _counts(eligible=3, computed=3)
    results = {
        name: service.backtest(OWNER, target["id"])
        for name, target in (("wing", wing_shooter), ("big", big), ("heavy", heavy_shooter))
    }
    assert {name: state for name, (_body, state) in results.items()} == {
        "wing": "hit",
        "big": "hit",
        "heavy": "hit",
    }
    wing = results["wing"][0]
    assert [(p["name"], p["position"]) for p in wing["players"]] == [("LeBron James", "Wing")]
    assert wing["summary"]["players_without_position"] == 0
    assert results["big"][0]["players"] == []
    assert results["big"][0]["summary"]["players_without_position"] == 0
    assert results["heavy"][0]["players"] == []
    assert "players_without_position" not in results["heavy"][0]["summary"]


def test_position_and_average_targets_precompute_and_serve_from_the_cache(world):
    exercise_position_and_average_targets(world)


def test_a_catalog_relabel_makes_the_precompute_and_the_batch_read_recompute(world, monkeypatch):
    with world.engine.begin() as connection:
        connection.execute(
            AthleteCatalog.__table__.insert().values(
                season=LAST,
                player_id=bt.LEBRON,
                display_name="LeBron James",
                roster_status="active",
                is_active=True,
                is_active_for_season=True,
                team_id=bt.LAL,
                team_name="Los Angeles Lakers",
                team_abbreviation="LAL",
                position="Wing",
                published_at=NOW,
            )
        )
    diet_only = world.saved_target()
    service = world.backtests(
        redis_client=CountingRedis(), athlete_catalog=AthleteCatalogReader(world.engine)
    )
    assert _without_duration(service.precompute(now=NOW)) == _counts(eligible=1, computed=1)
    assert service.backtest(OWNER, diet_only["id"])[0]["players"][0]["position"] == "Wing"
    assert _statuses(service) == ["ok"]

    # A refresh republishes the season's rows with LeBron relabelled.
    with world.engine.begin() as connection:
        connection.execute(
            AthleteCatalog.__table__.update()
            .where(AthleteCatalog.player_id == bt.LEBRON)
            .values(position="Big", published_at=NOW + timedelta(days=1))
        )

    assert _statuses(service) == ["uncached"]
    assert _without_duration(service.precompute(now=NOW)) == _counts(eligible=1, computed=1)
    body, state = service.backtest(OWNER, diet_only["id"])
    assert (state, body["players"][0]["position"]) == ("hit", "Big")
    # With the relabel filed, a rerun finds it by its own key and computes nothing.
    monkeypatch.setattr(service, "_backtest_stateful", lambda *a, **k: pytest.fail("computed"))
    assert _without_duration(service.precompute(now=NOW)) == _counts(eligible=1, already_cached=1)


def test_a_collector_relabel_stamped_before_a_later_tombstone_still_invalidates_the_cache(world):
    """The reviewer's scenario, through the real collector writer.

    The roster is published, then a later snapshot cuts an athlete, then a
    relabel arrives stamped between the two.  The newest ``published_at`` does
    not move, so only the content of the catalog can tell the cache.
    """

    from datetime import UTC, datetime

    from app.services.collection_control import CollectionControlService
    from tests.services.test_collection_control import (
        _catalog_payload,
        _roster_payload,
        _seed_governed_catalog_evidence,
    )

    _seed_governed_catalog_evidence(world.engine)
    with world.engine.begin() as connection:
        connection.execute(AthleteCatalog.__table__.delete())
    start = datetime(2026, 8, 12, tzinfo=UTC)
    clock = {"now": start}
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    control = CollectionControlService(world.engine, clock=lambda: clock["now"])
    control.activate_season(LAST, actor="operator")
    event_request = control.create_bootstrap_request(LAST, "event", cutoff=cutoff)
    assert control.publish_catalog(
        event_request.request_id, _catalog_payload("event"), version="event"
    ).complete

    def publish(version, positions, *, at):
        clock["now"] = at
        roster = _roster_payload(list(positions))
        for identity in roster["identities"]:
            identity["position"] = positions[int(identity["player_id"])]
        request = control.create_bootstrap_request(LAST, "athlete", cutoff=cutoff)
        assert control.publish_catalog(request.request_id, roster, version=version).complete

    lebron = bt.LEBRON
    publish("first", {1: None, lebron: "Big", 11: None, 12: None}, at=start)
    publish("cut", {1: None, lebron: "Big", 11: None}, at=start + timedelta(seconds=2))
    target = world.users.create_target(OWNER, opponent="OKC", qualifiers=[bt.BIG])
    service = world.backtests(
        redis_client=CountingRedis(), athlete_catalog=AthleteCatalogReader(world.engine)
    )
    body, state = service.backtest(OWNER, target["id"])
    assert (state, [p["position"] for p in body["players"]]) == ("miss", ["Big"])
    assert service.backtest(OWNER, target["id"])[1] == "hit"

    publish("relabel", {1: None, lebron: "Wing", 11: None}, at=start + timedelta(seconds=1))

    with world.engine.connect() as connection:
        newest = connection.execute(text("SELECT MAX(published_at) FROM athlete_catalog")).scalar_one()
    assert str(newest).startswith("2026-08-12 00:00:02")
    body, state = service.backtest(OWNER, target["id"])
    assert (state, body["players"], body["summary"]["players_without_position"]) == ("miss", [], 0)


def test_a_refresh_landing_between_two_reads_cannot_file_a_result_under_the_wrong_positions(world):
    """Digest and Positions come from one read, so ABA cannot cache an empty Backtest.

    A Wing publication lands right after the first catalog read.  Were the
    Positions read again, the Big Target would compute against Wing and file an
    empty result under the Big digest, which the restored Big would then hit.
    """

    from datetime import UTC, datetime

    from app.services.collection_control import CollectionControlService
    from tests.services.test_collection_control import (
        _catalog_payload,
        _roster_payload,
        _seed_governed_catalog_evidence,
    )

    _seed_governed_catalog_evidence(world.engine)
    with world.engine.begin() as connection:
        connection.execute(AthleteCatalog.__table__.delete())
    start = datetime(2026, 8, 12, tzinfo=UTC)
    cutoff = datetime(2026, 8, 11, tzinfo=UTC)
    clock = {"now": start}
    control = CollectionControlService(world.engine, clock=lambda: clock["now"])
    control.activate_season(LAST, actor="operator")
    event_request = control.create_bootstrap_request(LAST, "event", cutoff=cutoff)
    assert control.publish_catalog(
        event_request.request_id, _catalog_payload("event"), version="event"
    ).complete

    def publish(version, position, tick):
        clock["now"] = start + timedelta(seconds=tick)
        roster = _roster_payload([1, bt.LEBRON, 11, 12])
        for identity in roster["identities"]:
            identity["position"] = position if int(identity["player_id"]) == bt.LEBRON else None
        request = control.create_bootstrap_request(LAST, "athlete", cutoff=cutoff)
        assert control.publish_catalog(request.request_id, roster, version=version).complete

    publish("big-initial", "Big", 0)
    target = world.users.create_target(OWNER, opponent="OKC", qualifiers=[bt.BIG])

    class InterleavedReader(AthleteCatalogReader):
        armed = True

        def get_positions(self, season):
            positions = super().get_positions(season)
            if self.armed:
                self.armed = False
                publish("wing-interleaved", "Wing", 1)
            return positions

    service = world.backtests(redis_client=CountingRedis(), athlete_catalog=InterleavedReader(world.engine))

    body, state = service.backtest(OWNER, target["id"])
    assert (state, [(p["name"], p["position"]) for p in body["players"]]) == (
        "miss",
        [("LeBron James", "Big")],
    )

    publish("big-restored", "Big", 2)
    body, state = service.backtest(OWNER, target["id"])
    assert (state, [(p["name"], p["position"]) for p in body["players"]]) == (
        "hit",
        [("LeBron James", "Big")],
    )


def test_a_serving_season_advancing_after_the_position_read_cannot_mix_seasons(world):
    """The season, its Positions and the digest filed under come from one snapshot.

    LeBron is Big in 2025-26 and Wing in 2026-27.  2026-27 is published right
    after the cache precheck read the 2025-26 Positions; the Big Target must
    not come back as a 2026-27 Backtest holding 2025-26's Positions.
    """

    target = world.users.create_target(OWNER, opponent="OKC", qualifiers=[bt.BIG])
    with world.engine.begin() as connection:
        connection.execute(
            AthleteCatalog.__table__.insert(),
            [
                {
                    "season": season,
                    "player_id": bt.LEBRON,
                    "display_name": "LeBron James",
                    "roster_status": "active",
                    "is_active": True,
                    "is_active_for_season": True,
                    "position": position,
                    "published_at": NOW,
                }
                for season, position in [(LAST, "Big"), (NEW, "Wing")]
            ],
        )

    class AdvancingCatalog(AthleteCatalogReader):
        armed = True

        def get_positions(self, season):
            positions = super().get_positions(season)
            if self.armed:
                self.armed = False
                world.publish_season(NEW, okc_games=1, corner_three=0.1)
            return positions

    catalog = AdvancingCatalog(world.engine)
    redis_client = CountingRedis()
    service = world.backtests(redis_client=redis_client, athlete_catalog=catalog)

    body, state = service.backtest(OWNER, target["id"])

    pairs = [(p["name"], p["position"]) for p in body["players"]]
    # The request answers for the season it started in, with that season's Positions.
    assert (body["season"], state, pairs) == (LAST, "miss", [("LeBron James", "Big")])

    later, later_state = service.backtest(OWNER, target["id"])

    # The next request serves 2026-27, where LeBron is a Wing and no Big fits.
    assert (later["season"], later_state, later["players"]) == (NEW, "miss", [])
