"""Small, repeatable Matchups latency and query-plan artifact generator."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any

from sqlalchemy import event, text
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
    query_count: int = 0
    query_count_ceiling: int = 0
    query_count_within_ceiling: bool = True
    measured_query_shapes: tuple[str, ...] = ()
    fixture_profile: Mapping[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return (
            self.under_one_second
            and self.within_ten_percent
            and self.query_plans_available
            and self.query_plans_indexed
            and self.fixture_validated
            and self.provider_calls == 0
            and self.query_count_within_ceiling
            and bool(self.measured_query_shapes or self.baseline_source == "injected")
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
    capture_sql: bool = False,
    query_count_ceiling: int = 128,
) -> BenchmarkReport:
    """Measure distinct full-read seams and retain bounded SQL query plans."""

    if baseline is database_first:
        raise ValueError(
            "benchmark requires distinct baseline and database-first callables"
        )
    if isinstance(provider_calls, bool) or not isinstance(provider_calls, int) or provider_calls < 0:
        raise ValueError("provider_calls must be a non-negative integer")

    count = max(1, min(int(iterations), 1000))
    ceiling = max(1, min(int(query_count_ceiling), 10_000))
    baseline_times: list[float] = []
    db_times: list[float] = []
    measured: list[tuple[str, Any]] = []
    observed_query_count = 0
    for _ in range(count):
        started = time.perf_counter()
        with _capture_sql(engine):
            baseline()
        baseline_times.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        with _capture_sql(engine) as database_capture:
            database_first()
        db_times.append((time.perf_counter() - started) * 1000)
        if capture_sql:
            observed_query_count = max(observed_query_count, len(database_capture.statements))
            measured.extend(database_capture.statements)
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
    unique_measured = _unique_statements(measured)
    plans = _query_plans(
        engine,
        season=season,
        game_id=game_id,
        measured_statements=unique_measured if capture_sql else None,
    )
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
        query_count=observed_query_count,
        query_count_ceiling=ceiling if capture_sql else 0,
        query_count_within_ceiling=(
            observed_query_count > 0 and observed_query_count <= ceiling
            if capture_sql else True
        ),
        measured_query_shapes=tuple(
            f"{statement} | params={parameters!r}"
            for statement, parameters in unique_measured
        ),
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
    fixture_profile: Mapping[str, Any] | None = None,
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
    if (
        not isinstance(fixture_profile, Mapping)
        or fixture_profile.get("fixture_kind") != "representative_fixture"
        or bool(fixture_profile.get("production_claim"))
    ):
        raise ValueError("benchmark requires a representative fixture profile")

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
        capture_sql=True,
    )
    return replace(report, provider_calls=observed_calls, fixture_profile=fixture_profile)


def _query_plans(
    engine: Engine, *, season: str = "2025-26", game_id: str = "benchmark-game",
    measured_statements: tuple[tuple[str, Any], ...] | None = None,
) -> tuple[str, ...]:
    safe_season = str(season).replace("'", "''")
    safe_game_id = str(game_id).replace("'", "''")
    fallback_statements = (
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
    statements = tuple(measured_statements or ((statement, None) for statement in fallback_statements))
    plans: list[str] = []
    with engine.connect() as connection:
        for statement, parameters in statements:
            try:
                prefix = "EXPLAIN QUERY PLAN " if engine.dialect.name == "sqlite" else "EXPLAIN "
                if parameters is None:
                    rows = connection.execute(text(prefix + statement)).all()
                else:
                    rows = connection.exec_driver_sql(prefix + statement, parameters).all()
                parameter_note = "" if parameters is None else f" [params={parameters!r}]"
                plans.append(statement + parameter_note + " => " + " | ".join(str(row) for row in rows)[:1024])
            except Exception:
                plans.append(statement + " => unavailable")
    return tuple(plans)


@dataclass(slots=True)
class _SQLCapture:
    statements: list[tuple[str, Any]]


class _capture_sql:
    """Capture the SQL emitted by a real service invocation.

    Only SELECT statements are retained as sanitized shape/parameter pairs;
    setup, transaction, and PRAGMA chatter cannot be mistaken for read-path
    evidence.  The listener is installed for the duration of one call and is
    always removed, so benchmark instrumentation cannot leak into the app.
    """

    def __init__(self, engine: Engine):
        self.engine = engine
        self.capture = _SQLCapture([])
        self._listener = self._record

    def __enter__(self) -> _SQLCapture:
        event.listen(self.engine, "before_cursor_execute", self._listener)
        return self.capture

    def __exit__(self, *_exc: Any) -> None:
        event.remove(self.engine, "before_cursor_execute", self._listener)

    def _record(self, _conn: Any, _cursor: Any, statement: str, parameters: Any, *_args: Any) -> None:
        normalized = re.sub(r"\s+", " ", str(statement)).strip()
        upper = normalized.upper()
        if not (upper.startswith("SELECT") or upper.startswith("WITH")):
            return
        self.capture.statements.append((normalized, _safe_parameters(parameters)))


def _safe_parameters(parameters: Any) -> Any:
    if isinstance(parameters, Mapping):
        return {str(key): _safe_parameters(value) for key, value in sorted(parameters.items(), key=lambda item: str(item[0]))}
    if isinstance(parameters, (list, tuple)):
        return tuple(_safe_parameters(value) for value in parameters)
    if parameters is None or isinstance(parameters, (str, int, float, bool)):
        return parameters
    return type(parameters).__name__


def _unique_statements(statements: list[tuple[str, Any]]) -> tuple[tuple[str, Any], ...]:
    seen: set[str] = set()
    result: list[tuple[str, Any]] = []
    for statement, parameters in statements:
        key = f"{statement}\x00{parameters!r}"
        if key in seen:
            continue
        seen.add(key)
        result.append((statement, parameters))
    return tuple(result)


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
