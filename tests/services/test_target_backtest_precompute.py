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
from sqlalchemy import create_engine

from app.config.settings import CacheSettings
from app.models.user import User
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
    first = world.saved_target()
    second = world.users.create_target(
        OWNER, opponent="BOS", qualifiers=[seasons.CORNER_THREE]
    )
    return first, second


def _stale_target(world):
    """A Target whose owner last logged in 15 days before ``NOW``.

    Its opponent differs from every recent Target's: keys exclude the owner,
    so an identical definition would share a recent Target's entry.
    """

    with world.engine.begin() as connection:
        connection.execute(
            User.__table__.insert(),
            {
                "firebase_uid": STALE_OWNER,
                "email": "stale@example.com",
                "display_name": STALE_OWNER,
                "photo_url": None,
                "created_at": NOW - timedelta(days=30),
                "last_login": NOW - timedelta(days=15),
                "is_active": True,
            },
        )
    return world.users.create_target(
        STALE_OWNER, opponent="LAL", qualifiers=[seasons.CORNER_THREE]
    )


def _bump_generation(world):
    world.compose(
        "player_game_logs", LAST, {"rows": seasons._season_logs(LAST, okc_games=1)}
    )


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


def test_a_second_run_with_nothing_changed_computes_nothing(world):
    _two_targets(world)
    redis = CountingRedis()
    service = world.backtests(redis_client=redis)
    service.precompute(now=NOW)
    writes = len(redis.sets)

    result = service.precompute(now=NOW)

    assert _without_duration(result) == _counts(already_cached=2)
    assert len(redis.sets) == writes


def test_a_spent_budget_stops_after_the_current_target_and_the_next_run_resumes(
    world,
):
    _two_targets(world)
    _with_budget(world, 0)
    service = world.backtests(redis_client=CountingRedis())

    stopped = service.precompute(now=NOW)
    resumed = service.precompute(now=NOW)

    assert _without_duration(stopped) == _counts(computed=1, remaining=1)
    assert _without_duration(resumed) == _counts(already_cached=1, computed=1)
    assert _statuses(service) == ["ok", "ok"]


def test_the_published_season_is_filled_first_and_each_season_has_an_entry(world):
    first, _second = _two_targets(world)
    seasons._activate_new_season(world)
    service = world.backtests(redis_client=CountingRedis())

    result = service.precompute(now=NOW)

    assert _without_duration(result) == _counts(
        seasons=[NEW, LAST], eligible=4, computed=4
    )
    assert service.backtest(OWNER, first["id"], season=NEW)[1] == "hit"
    assert service.backtest(OWNER, first["id"], season=LAST)[1] == "hit"


def test_an_unavailable_season_is_skipped(world):
    """Only 2025-26 was ever published, so 2026-27 is season_unavailable."""

    _two_targets(world)
    service = world.backtests(redis_client=CountingRedis())

    result = service.precompute(now=NOW)

    assert result["seasons"] == [LAST]


def test_one_targets_failure_is_logged_and_the_run_continues(
    world, monkeypatch, caplog
):
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
    assert (
        f"Target backtest precompute failed target_id={first['id']} season={LAST}"
        in caplog.messages
    )
    assert service.backtest(OWNER, second["id"])[1] == "hit"


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
    monkeypatch.setattr(
        service, "_backtest_stateful", lambda *a, **k: pytest.fail("computed")
    )

    with caplog.at_level(logging.INFO, logger="app.services.target_backtest"):
        assert service.precompute(now=NOW) is None

    assert caplog.messages == [
        f"target_backtest_precompute skipped reason=cache_disabled cause={cause}"
    ]
    if redis_client is not None:
        assert (redis_client.gets, redis_client.sets, redis_client.exists_calls) == (
            [], [], []
        )


# --- the cron command ---------------------------------------------------------


@pytest.fixture
def cron(world, monkeypatch):
    """The cron command over the world's database, with an in-memory Redis."""

    monkeypatch.setenv("DATABASE_URL", str(world.engine.url))
    monkeypatch.delenv("NBA_CURRENT_SEASON", raising=False)
    redis = CountingRedis()
    monkeypatch.setattr(
        "app.utils.cache_config.get_redis_client", lambda settings=None: redis
    )
    return redis


def _tick_line(capsys):
    return capsys.readouterr().out.splitlines()[0]


def test_the_cron_command_fills_the_cache_after_an_idle_tick(
    world, cron, monkeypatch, capsys
):
    first, second = _two_targets(world)
    monkeypatch.setattr(
        player_shooting_refresh, "datetime",
        type("Frozen", (), {"now": staticmethod(lambda tz=None: NOW)}),
    )

    assert player_shooting_refresh.main([]) == 0

    assert _tick_line(capsys) == '{"composed_jobs": 0, "state": "no_active_manifest"}'
    service = world.backtests(redis_client=cron)
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
        assert calls == ['{"composed_jobs": 0, "state": "no_active_manifest"}\n']
        assert "target_backtest_precompute failed" in caplog.messages
    else:
        monkeypatch.setattr(
            player_shooting_refresh, "active_manifest_season",
            lambda engine: (_ for _ in ()).throw(RuntimeError("tick failed")),
        )
        monkeypatch.setattr(
            player_shooting_refresh, "_precompute_target_backtests",
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

    assert _tick_line(capsys) == '{"composed_jobs": 0, "state": "no_active_manifest"}'
    assert (
        f"target_backtest_precompute skipped reason=cache_disabled cause={cause}"
        in caplog.messages
    )
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
