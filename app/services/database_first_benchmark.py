"""Small, repeatable Matchups latency and query-plan artifact generator."""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

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
    provider_calls: int = 0
    baseline_source: str = "injected"
    database_first_source: str = "injected"

    @property
    def passed(self) -> bool:
        return (
            self.under_one_second
            and self.within_ten_percent
            and self.query_plans_available
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
    plans = _query_plans(engine)
    plans_available = bool(plans) and all(
        "=> unavailable" not in plan for plan in plans
    )
    return BenchmarkReport(
        iterations=count,
        baseline_p95_ms=round(baseline_p95, 3),
        database_first_p95_ms=round(db_p95, 3),
        ratio=round(ratio, 4),
        under_one_second=db_p95 < 1000.0,
        within_ten_percent=ratio <= 1.10,
        query_plans=plans,
        query_plans_available=plans_available,
        provider_calls=provider_calls,
        baseline_source=baseline_source,
        database_first_source=database_first_source,
    )


def _query_plans(engine: Engine) -> tuple[str, ...]:
    statements = (
        "SELECT stream_key, enabled FROM publication_streams ORDER BY stream_key",
        "SELECT stream_key, active_publication_id FROM publication_pointers",
        "SELECT season, player_id, game_date, game_id FROM player_game_logs WHERE season = '2025-26' ORDER BY game_date DESC, game_id DESC LIMIT 50",
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


def write_benchmark_report(report: BenchmarkReport, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")


__all__ = ["BenchmarkReport", "benchmark_matchup_reads", "write_benchmark_report"]
