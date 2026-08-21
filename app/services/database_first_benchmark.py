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
    unplanned_query_shapes: tuple[str, ...] = ()
    governed_query_tables: tuple[str, ...] = ()
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
            and not self.unplanned_query_shapes
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
    evidence = _query_plans(
        engine,
        season=season,
        game_id=game_id,
        measured_statements=unique_measured if capture_sql else None,
    )
    plans = tuple(item.render() for item in evidence)
    plans_available = bool(evidence) and all(item.available for item in evidence)
    plans_indexed = (
        _plans_are_indexed(evidence, measured_statements=unique_measured)
        if require_indexed_plans
        else True
    )
    unplanned = _unplanned_query_shapes(plans, unique_measured) if require_indexed_plans else ()
    governed_tables = _governed_tables(unique_measured)
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
        unplanned_query_shapes=unplanned,
        governed_query_tables=governed_tables,
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


@dataclass(frozen=True, slots=True)
class _PlanNode:
    """One access-path node, reduced to structure with no predicates.

    Vendor plan text echoes bound constants back in fields like ``Index Cond``,
    so retaining it would place real values in the artifact.  Keep only the
    node type, the relation it reads, and the index it reads through.
    """

    node_type: str
    relation: str | None = None
    index_name: str | None = None

    def render(self) -> str:
        parts = [self.node_type or "unknown node"]
        if self.index_name:
            parts.append(f"using {self.index_name}")
        if self.relation:
            parts.append(f"on {self.relation}")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class _PlanEvidence:
    """One statement's redacted access path."""

    statement: str
    parameters: Any
    nodes: tuple[_PlanNode, ...]
    available: bool

    def render(self) -> str:
        if not self.available:
            return f"{self.statement} => unavailable"
        note = "" if self.parameters is None else f" [params={self.parameters!r}]"
        body = " | ".join(node.render() for node in self.nodes) or "no access path"
        return f"{self.statement}{note} => {body}"


# A full read of one of these grows with the season, so it is the regression
# the gate exists to catch.  Index-backed and bitmap access are fine; only a
# whole-relation read is not.
_SEQUENTIAL_NODE_TYPES = frozenset({
    "seq scan", "sample scan", "foreign scan",
})

_SQLITE_DETAIL = re.compile(r"^\s*(SCAN|SEARCH)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_SQLITE_INDEX = re.compile(
    r"USING\s+(?:COVERING\s+)?(?:INDEX\s+(\S+)|(INTEGER PRIMARY KEY|PRIMARY KEY))",
    re.IGNORECASE,
)
_ALIAS_KEYWORDS = frozenset({
    "where", "join", "inner", "left", "right", "outer", "full", "cross", "on",
    "group", "order", "limit", "using", "as", "set", "values", "union",
    "having", "offset", "returning", "natural",
})


def _statement_aliases(statement: str) -> dict[str, str]:
    """Map ``FROM publication_pointers p`` back to the real relation.

    SQLite reports the alias in its plan, so without this a bounded registry
    reached through an aliased join is unrecognisable.
    """

    aliases: dict[str, str] = {}
    for table, alias in re.findall(
        r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)\s+(?:as\s+)?([a-z_][a-z0-9_]*)",
        statement,
        re.IGNORECASE,
    ):
        if alias.lower() in _ALIAS_KEYWORDS:
            continue
        aliases[alias.lower()] = table.lower()
    return aliases


def _postgres_plan_nodes(document: Any) -> tuple[_PlanNode, ...]:
    """Walk an ``EXPLAIN (FORMAT JSON)`` tree into whitelisted nodes."""

    nodes: list[_PlanNode] = []

    def walk(node: Any) -> None:
        if not isinstance(node, Mapping):
            return
        nodes.append(_PlanNode(
            node_type=str(node.get("Node Type", "")).strip(),
            relation=node.get("Relation Name"),
            index_name=node.get("Index Name"),
        ))
        for child in node.get("Plans") or ():
            walk(child)

    root = document[0] if isinstance(document, list) and document else document
    if isinstance(root, Mapping):
        walk(root.get("Plan"))
    return tuple(nodes)


def _sqlite_plan_nodes(rows: Any, aliases: Mapping[str, str]) -> tuple[_PlanNode, ...]:
    """Parse ``EXPLAIN QUERY PLAN`` detail rows into the same node shape."""

    nodes: list[_PlanNode] = []
    for row in rows:
        detail = str(row[-1])
        match = _SQLITE_DETAIL.match(detail)
        if match is None:
            continue
        name = match.group(2)
        index = _SQLITE_INDEX.search(detail)
        index_name = None
        if index is not None:
            index_name = index.group(1) or index.group(2)
        nodes.append(_PlanNode(
            node_type="Index Scan" if index is not None else "Seq Scan",
            relation=aliases.get(name.lower(), name),
            index_name=index_name,
        ))
    return tuple(nodes)


def _query_plans(
    engine: Engine, *, season: str = "2025-26", game_id: str = "benchmark-game",
    measured_statements: tuple[tuple[str, Any], ...] | None = None,
) -> tuple[_PlanEvidence, ...]:
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
    sqlite = engine.dialect.name == "sqlite"
    prefix = "EXPLAIN QUERY PLAN " if sqlite else "EXPLAIN (FORMAT JSON) "
    evidence: list[_PlanEvidence] = []
    with engine.connect() as connection:
        for statement, parameters in statements:
            try:
                if parameters is None:
                    rows = connection.execute(text(prefix + statement)).all()
                else:
                    bind = (
                        parameters.raw
                        if isinstance(parameters, _BoundParameters)
                        else parameters
                    )
                    rows = connection.exec_driver_sql(prefix + statement, bind).all()
                if sqlite:
                    nodes = _sqlite_plan_nodes(rows, _statement_aliases(statement))
                else:
                    document = rows[0][0] if rows else None
                    if isinstance(document, (str, bytes)):
                        document = json.loads(document)
                    nodes = _postgres_plan_nodes(document)
                evidence.append(_PlanEvidence(statement, parameters, nodes, True))
            except Exception:
                evidence.append(_PlanEvidence(statement, parameters, (), False))
    return tuple(evidence)


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
        self.capture.statements.append(
            (normalized, _BoundParameters(_raw_parameters(parameters), _safe_parameters(parameters)))
        )


class _BoundParameters:
    """Real bind values for EXPLAIN, sanitized values for the artifact.

    The captured parameters serve two purposes that conflict.  The retained
    evidence must not print raw values, but EXPLAIN has to receive values the
    database can actually bind.  Sanitizing a ``date`` to the string ``"date"``
    satisfies the first and makes the second impossible, so keep both and use
    each where it belongs.  ``__repr__`` returns the sanitized form, which is
    what every evidence and de-duplication path already reads.
    """

    __slots__ = ("raw", "safe")

    def __init__(self, raw: Any, safe: Any) -> None:
        self.raw = raw
        self.safe = safe

    def __repr__(self) -> str:
        return repr(self.safe)


def _raw_parameters(parameters: Any) -> Any:
    """Copy the driver's bind values so a reused buffer cannot mutate them."""

    if isinstance(parameters, Mapping):
        return {str(key): value for key, value in parameters.items()}
    if isinstance(parameters, (list, tuple)):
        return tuple(parameters)
    return parameters


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


_GOVERNED_TABLE_PATTERNS = (
    "publication_",
    "player_game_logs",
    "player_pool_snapshots",
    "player_diet",
    "team_matchup",
    "event_catalog",
    "canonical_game_ledger",
)


# Control-plane registries hold roughly one row per stream, so they never grow
# with data volume and a planner will correctly sequentially scan them at any
# scale.  Requiring an index scan here is unsatisfiable rather than strict, so
# a full scan of one of these is not evidence of an unbounded read.  Every
# other governed table is a fact table whose size tracks the season.
_BOUNDED_REGISTRY_TABLES = frozenset({
    "publication_streams",
    "publication_pointers",
})


def _governed_tables(statements: tuple[tuple[str, Any], ...]) -> tuple[str, ...]:
    names: set[str] = set()
    for statement, _parameters in statements:
        lowered = statement.lower()
        for match in re.finditer(r"\b(?:from|join)\s+([a-z][a-z0-9_]*)", lowered):
            table = match.group(1)
            if any(table.startswith(pattern) for pattern in _GOVERNED_TABLE_PATTERNS):
                names.add(table)
    return tuple(sorted(names))


def _unplanned_query_shapes(
    plans: tuple[str, ...],
    statements: tuple[tuple[str, Any], ...],
) -> tuple[str, ...]:
    planned = {
        plan.split(" => ", 1)[0].split(" [params=", 1)[0]
        for plan in plans
        if " => " in plan
    }
    return tuple(
        statement
        for statement, _parameters in statements
        if statement not in planned
    )


def _plans_are_indexed(
    evidence: tuple[_PlanEvidence, ...],
    *,
    measured_statements: tuple[tuple[str, Any], ...] = (),
) -> bool:
    """Reject unavailable, unplanned, or whole-relation governed reads.

    The rule is only that no governed relation whose size tracks the season is
    read in full.  A bounded registry holds about one row per stream, so a
    planner reads all of it by choice at any scale and that is not evidence of
    an unbounded read.  Index, index-only, and bitmap access all satisfy this.
    """

    if not evidence:
        return False
    if _unplanned_query_shapes(tuple(item.render() for item in evidence), measured_statements):
        return False
    for item in evidence:
        if not item.available:
            return False
        for node in item.nodes:
            if node.node_type.strip().lower() not in _SEQUENTIAL_NODE_TYPES:
                continue
            relation = (node.relation or "").lower()
            if not relation:
                continue
            if not any(relation.startswith(pattern) for pattern in _GOVERNED_TABLE_PATTERNS):
                continue
            if relation in _BOUNDED_REGISTRY_TABLES:
                continue
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
