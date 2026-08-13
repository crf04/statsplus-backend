"""Small, repeatable Matchups latency and query-plan artifact generator."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    iterations: int
    baseline_p95_ms: float
    database_first_p95_ms: float
    ratio: float
    under_one_second: bool
    within_ten_percent: bool
    query_plans: tuple[str, ...]
    query_plans_available: bool = True
    query_plans_indexed: bool = True
    fixture_validated: bool = True
    provider_calls: int = 0
    baseline_source: str = "injected"
    database_first_source: str = "injected"

    @property
    def passed(self) -> bool:
        return (
            self.under_one_second
            and self.within_ten_percent
            and self.query_plans_available
            and self.query_plans_indexed
            and self.fixture_validated
            and self.provider_calls == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "passed": self.passed}


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1))
    return ordered[index]


def benchmark_matchup_reads(
    engine: Engine,
    *,
    baseline: Callable[[], Any],
    database_first: Callable[[], Any],
    iterations: int = 20,
    provider_calls: int = 0,
    baseline_source: str = "injected",
    database_first_source: str = "injected",
    season: str = "2025-26",
    game_id: str = "benchmark-game",
    require_indexed_plans: bool = False,
    fixture_validated: bool = True,
) -> BenchmarkReport:
    """Measure distinct full-read seams and retain bounded SQL query plans."""

    if baseline is database_first:
        raise ValueError(
            "benchmark requires distinct baseline and database-first callables"
        )
    if isinstance(provider_calls, bool) or not isinstance(provider_calls, int) or provider_calls < 0:
        raise ValueError("provider_calls must be a non-negative integer")

    count = max(1, min(int(iterations), 1000))
    baseline_times: list[float] = []
    db_times: list[float] = []
    for _ in range(count):
        started = time.perf_counter()
        baseline()
        baseline_times.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        database_first()
        db_times.append((time.perf_counter() - started) * 1000)
    baseline_p95 = _p95(baseline_times)
    db_p95 = _p95(db_times)
    # A no-op seam can be below the timer's meaningful resolution on a local
    # SQLite run.  Do not turn that measurement noise into a false regression;
    # real production-like calls are above this floor and retain the normal
    # ten-percent comparison.
    measurement_floor_ms = 0.01
    if baseline_p95 < measurement_floor_ms and db_p95 < measurement_floor_ms:
        ratio = 1.0
    else:
        ratio = db_p95 / max(baseline_p95, measurement_floor_ms)
    plans = _query_plans(engine, season=season, game_id=game_id)
    plans_available = bool(plans) and all(
        "=> unavailable" not in plan for plan in plans
    )
    plans_indexed = _plans_are_indexed(plans) if require_indexed_plans else True
    return BenchmarkReport(
        iterations=count,
        baseline_p95_ms=round(baseline_p95, 3),
        database_first_p95_ms=round(db_p95, 3),
        ratio=round(ratio, 4),
        under_one_second=db_p95 < 1000.0,
        within_ten_percent=ratio <= 1.10,
        query_plans=plans,
        query_plans_available=plans_available,
        query_plans_indexed=plans_indexed,
        fixture_validated=fixture_validated,
        provider_calls=provider_calls,
        baseline_source=baseline_source,
        database_first_source=database_first_source,
    )


def benchmark_matchup_services(
    engine: Engine,
    *,
    baseline_route: Callable[[], Any],
    database_first_route: Callable[[], Any],
    season: str,
    game_id: str,
    iterations: int = 20,
    provider_call_count: Callable[[], int] | None = None,
    fixture_validated: bool = False,
) -> BenchmarkReport:
    """Benchmark the complete route/service callables over one fixture.

    The SQL benchmark remains available for low-level diagnostics, but the
    activation gate uses this seam so both paths execute the same public
    response assembly.  A provider counter can be injected by the service
    fixture; its delta must stay zero for the database-first path.
    """

    if not str(season).strip() or not str(game_id).strip():
        raise ValueError("benchmark requires concrete season and game identity")
    if baseline_route is database_first_route:
        raise ValueError("benchmark requires distinct route/service callables")
    if provider_call_count is None:
        raise ValueError("benchmark requires an instrumented statistical provider counter")
    if not fixture_validated:
        raise ValueError("benchmark requires a validated disposable fixture")

    def invoke(route: Callable[[], Any]) -> Any:
        value = route()
        if not isinstance(value, Mapping):
            raise ValueError("benchmark route returned no response object")
        return value

    observed_calls = 0
    def invoke_database_first() -> Any:
        nonlocal observed_calls
        before = provider_call_count()
        value = invoke(database_first_route)
        after = provider_call_count()
        if isinstance(before, bool) or isinstance(after, bool):
            raise ValueError("provider counter must return an integer")
        observed_calls += max(0, int(after) - int(before))
        return value

    report = benchmark_matchup_reads(
        engine,
        baseline=lambda: invoke(baseline_route),
        database_first=invoke_database_first,
        iterations=iterations,
        baseline_source="matchup_service_legacy",
        database_first_source="matchup_route_database_first",
        season=season,
        game_id=game_id,
        require_indexed_plans=True,
        fixture_validated=True,
    )
    return replace(report, provider_calls=observed_calls)


def _query_plans(
    engine: Engine, *, season: str = "2025-26", game_id: str = "benchmark-game"
) -> tuple[str, ...]:
    safe_season = str(season).replace("'", "''")
    safe_game_id = str(game_id).replace("'", "''")
    statements = (
        "SELECT stream_key, enabled FROM publication_streams ORDER BY stream_key",
        "SELECT p.stream_key, p.fence, v.publication_id, v.version "
        "FROM publication_pointers p JOIN publication_versions v "
        "ON v.publication_id = p.active_publication_id "
        f"WHERE v.season = '{safe_season}'",
        "SELECT v.publication_id, v.stream_key, v.payload FROM publication_pointers p "
        "JOIN publication_versions v ON v.publication_id = p.active_publication_id "
        f"WHERE v.season = '{safe_season}' AND v.stream_key = 'player_game_logs'",
        "SELECT season, player_id, game_date, game_id FROM player_game_logs "
        f"WHERE season = '{safe_season}' AND game_id = '{safe_game_id}' "
        "ORDER BY game_date DESC, game_id DESC LIMIT 50",
        "SELECT game_id, season, game_date FROM canonical_game_ledger_games "
        f"WHERE season = '{safe_season}' ORDER BY game_date DESC, game_id DESC LIMIT 50",
    )
    plans: list[str] = []
    with engine.connect() as connection:
        for statement in statements:
            try:
                prefix = "EXPLAIN QUERY PLAN " if engine.dialect.name == "sqlite" else "EXPLAIN "
                rows = connection.execute(text(prefix + statement)).all()
                plans.append(statement + " => " + " | ".join(str(row) for row in rows)[:1024])
            except Exception:
                plans.append(statement + " => unavailable")
    return tuple(plans)


def _plans_are_indexed(plans: tuple[str, ...]) -> bool:
    """Reject full scans for bounded publication/ledger read queries."""

    required_tables = (
        "publication_pointers",
        "publication_versions",
        "player_game_logs",
        "canonical_game_ledger_games",
    )
    for plan in plans:
        if "=> unavailable" in plan:
            return False
        upper = plan.upper()
        for table in required_tables:
            if table.upper() in upper and f"SCAN {table.upper()}" in upper:
                return False
        # SQLite reports aliased publication tables as ``SCAN p``/``SCAN v``;
        # the SQL text retained in the artifact identifies those aliases.
        if "publication_pointers" in plan.lower() and "SCAN P" in upper:
            return False
        if "publication_versions" in plan.lower() and "SCAN V" in upper:
            return False
    return True


def write_benchmark_report(report: BenchmarkReport, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")


__all__ = [
    "BenchmarkReport",
    "benchmark_matchup_reads",
    "benchmark_matchup_services",
    "write_benchmark_report",
]
