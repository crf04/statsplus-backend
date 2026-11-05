#!/usr/bin/env python3
"""Time ``GET /api/games/*`` routes in process against a read-only database.

Generalizes ``scripts/benchmark_game_logs.py`` to the Matchup, Game Logs,
Slate, Slate Edges and seasons routes.  Runs the real route handler (auth
skipped) with the dependency graph the web app builds, a clock pinned to a
chosen instant, and every database session forced read-only.  Database traffic
goes through a local TCP relay that counts round trips and bytes, so the counts
do not depend on this machine's latency to the database.  Prints one JSON line
per case with the median and range of wall time, the per-request statement,
checkout, round-trip and byte counts, the response body's sha256, and
``model_ms``: thread CPU + round trips x 1 ms + MB down x 12.8 ms, an estimate
of the request on Railway's private network (8 ms/MB transfer at 125 MB/s plus
4.8 ms/MB detoast) that a slow or busy link to the database does not move.

    BENCH_DATABASE_URL=... python scripts/benchmark_routes.py \\
        --now 2026-11-02T01:00:00Z --out /tmp/bench \\
        --case "matchup=matchup,game_id=0022600157;season=2026-27"

``--case`` is ``name=route,query`` with ``;`` between query parameters.  Run it
with ``PYTHONPATH`` naming the checkout to measure; the script's own checkout is
only a fallback.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import socket
import statistics
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path


class _Relay:
    """A localhost TCP relay that counts client-to-server turns and bytes."""

    def __init__(self, host: str, port: int, *, delay_seconds: float = 0.0) -> None:
        self._target = (host, port)
        self._delay = delay_seconds
        self._lock = threading.Lock()
        self.turns = 0
        self.bytes_up = 0
        self.bytes_down = 0
        self.connections = 0
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def reset(self) -> dict[str, int]:
        with self._lock:
            counts = {
                "round_trips": self.turns,
                "bytes_up": self.bytes_up,
                "bytes_down": self.bytes_down,
                "tcp_connects": self.connections,
            }
            self.turns = self.bytes_up = self.bytes_down = self.connections = 0
        return counts

    def _accept(self) -> None:
        while True:
            client, _ = self._listener.accept()
            server = socket.create_connection(self._target)
            for sock in (client, server):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                self.connections += 1
            # ``True`` while the server spoke last, so the client's next send
            # opens a new turn.
            state = {"server_last": True}
            threading.Thread(target=self._pump, args=(client, server, state, True), daemon=True).start()
            threading.Thread(target=self._pump, args=(server, client, state, False), daemon=True).start()

    def _pump(self, source: socket.socket, sink: socket.socket, state: dict, upstream: bool) -> None:
        try:
            while data := source.recv(65536):
                with self._lock:
                    if upstream:
                        self.bytes_up += len(data)
                        new_turn = state["server_last"]
                        if new_turn:
                            self.turns += 1
                        state["server_last"] = False
                    else:
                        self.bytes_down += len(data)
                        state["server_last"] = True
                if upstream and new_turn and self._delay:
                    time.sleep(self._delay)
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (source, sink):
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)


def _pin_clock(now: datetime) -> None:
    """Make every ``datetime.now`` in ``app`` return ``now``.

    Modules bind ``datetime`` at import, so the class is swapped in each
    loaded ``app`` module; ``isinstance`` keeps accepting real datetimes.
    """

    real = datetime

    class _Meta(type(real)):
        def __instancecheck__(cls, instance):
            return isinstance(instance, real)

        def __subclasscheck__(cls, subclass):
            return issubclass(subclass, real)

    class PinnedDatetime(real, metaclass=_Meta):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return now.astimezone(UTC).replace(tzinfo=None)

    for name, module in list(sys.modules.items()):
        if (name == "app" or name.startswith("app.")) and getattr(module, "datetime", None) is real:
            module.datetime = PinnedDatetime


def _build(database_url: str, relay: _Relay):
    from sqlalchemy import event
    from sqlalchemy.engine import Engine, make_url

    from app.config.settings import load_settings, set_runtime_settings

    url = make_url(database_url).set(host="127.0.0.1", port=relay.port)

    @event.listens_for(Engine, "connect")
    def _read_only(dbapi_connection, _record):
        if dbapi_connection.__class__.__module__.startswith("psycopg2"):
            cursor = dbapi_connection.cursor()
            cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            cursor.close()
            dbapi_connection.commit()

    settings = load_settings(
        {
            **os.environ,
            "DATABASE_URL": url.render_as_string(hide_password=False),
            "ENABLE_CACHE": "false",
            "FLASK_ENV": "development",
            "PROJECTION_ARCHIVE_READ_ENABLED": "true",
            "ODDS_API_ENABLED": "true",
            "DFS_ENABLED_PROVIDERS": os.environ.get(
                "GAME_LOGS_BENCH_DFS_PROVIDERS", "prizepicks,underdog,dabble,kalshi,polymarket,novig"
            ),
        }
    )
    # The web app's statement limit, as ``create_app`` applies it.
    settings = settings.model_copy(
        update={
            "database": settings.database.model_copy(
                update={"statement_timeout_ms": settings.database.web_statement_timeout_ms}
            )
        }
    )
    set_runtime_settings(settings)

    from flask import Flask

    from app.dependencies import build_dependencies
    from app.services.collection_control import PublicationService

    # Boot reconciles the stream registry with ``SELECT ... FOR UPDATE``,
    # which a read-only session refuses; no request reads its result.
    PublicationService.register_default_streams = lambda self: ()
    dependencies = build_dependencies(settings)
    app = Flask("benchmark_game_logs")
    app.json.sort_keys = True
    app.json.compact = True
    app.extensions["runtime_settings"] = settings
    app.extensions["dependencies"] = dependencies
    return app, dependencies


def _caller() -> str:
    """The innermost ``app`` frames that issued a statement."""

    import traceback

    frames = [
        f"{Path(frame.filename).stem}:{frame.name}:{frame.lineno}"
        for frame in traceback.extract_stack()
        if "/app/" in frame.filename
    ]
    return " < ".join(reversed(frames[-int(os.environ.get("GAME_LOGS_BENCH_FRAMES", "4")) :]))


_PLACEHOLDER_RUN = re.compile(r"%\((\w+?)_\d+\)s(\s*,\s*%\(\1_\d+\)s)+")


def fingerprint(statement: str) -> str:
    """Normalize literals and expanded IN lists so one shape is one key."""

    text = " ".join(statement.split())
    text = _PLACEHOLDER_RUN.sub(lambda m: f"%({m.group(1)}_N)s", text)
    text = re.sub(r"%\((\w+?)_\d+\)s", r"%(\1_n)s", text)
    text = re.sub(r"'(?:[^']|'')*'", "?", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "N", text)
    text = re.sub(r"\(\s*(?:\?|N)(?:\s*,\s*(?:\?|N))+\s*\)", "(?…)", text)
    return text


class _Counters:
    def __init__(self, engine, relay: _Relay) -> None:
        from sqlalchemy import event

        self.relay = relay
        self.trace: list[dict] | None = None
        self.capture_sql = False
        self.statements = 0
        self.sql_seconds = 0.0
        self.stmt_trips = 0
        self.checkouts = 0
        self.connects = 0
        self.shapes: dict[str, int] = {}
        self.identical: dict[str, int] = {}
        self._started: list[tuple] = []

        @event.listens_for(engine, "before_cursor_execute")
        def _before(*_args):
            self._started.append((time.perf_counter(), relay.bytes_down, relay.turns))

        @event.listens_for(engine, "after_cursor_execute")
        def _after(_conn, cursor, statement, parameters, *_args):
            started, bytes_down, turns = self._started.pop()
            elapsed = time.perf_counter() - started
            trips = relay.turns - turns
            self.statements += 1
            self.sql_seconds += elapsed
            self.stmt_trips += trips
            shape = fingerprint(statement)
            self.shapes[shape] = self.shapes.get(shape, 0) + 1
            key = hashlib.sha1((statement + repr(parameters)).encode()).hexdigest()
            self.identical[key] = self.identical.get(key, 0) + 1
            if self.trace is not None:
                entry = {
                    "ms": round(elapsed * 1000, 2),
                    "rows": cursor.rowcount,
                    "bytes_down": relay.bytes_down - bytes_down,
                    "round_trips": trips,
                    "shape": shape[:400],
                    "sql": " ".join(statement.split())[:400],
                    "identical_key": key,
                    "caller": _caller(),
                }
                if self.capture_sql:
                    try:
                        entry["literal_sql"] = cursor.mogrify(statement, parameters).decode()
                    except Exception as error:
                        entry["literal_sql_error"] = repr(error)
                self.trace.append(entry)

        @event.listens_for(engine, "checkout")
        def _checkout(*_args):
            self.checkouts += 1

        @event.listens_for(engine, "connect")
        def _connect(*_args):
            self.connects += 1

    def reset(self) -> dict:
        counts = {
            "statements": self.statements,
            "sql_ms": round(self.sql_seconds * 1000, 1),
            "stmt_round_trips": self.stmt_trips,
            "checkouts": self.checkouts,
            "connects": self.connects,
            "shapes": self.shapes,
            "identical_repeats": sum(n - 1 for n in self.identical.values() if n > 1),
        }
        self.statements = self.checkouts = self.connects = self.stmt_trips = 0
        self.sql_seconds = 0.0
        self.shapes, self.identical = {}, {}
        return counts


def _rtt_ms(relay: _Relay, database_url: str, samples: int = 30) -> dict:
    import psycopg2
    from sqlalchemy.engine import make_url

    url = make_url(database_url).set(host="127.0.0.1", port=relay.port)
    conn = psycopg2.connect(
        host="127.0.0.1", port=relay.port, user=url.username, password=url.password, dbname=url.database
    )
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()
    times = []
    for _ in range(samples):
        t = time.perf_counter()
        cur.execute("SELECT 1")
        cur.fetchall()
        times.append((time.perf_counter() - t) * 1000)
    conn.close()
    return {
        "median": round(statistics.median(times), 2),
        "min": round(min(times), 2),
        "max": round(max(times), 2),
    }


ROUTES = {
    # name: (module, view attribute, path, unwrap auth decorator, signed-in caller)
    "slate": ("game_routes", "get_slate", "/api/games/slate", False, False),
    "slate_edges": ("game_routes", "get_slate_edges", "/api/games/slate/edges", False, False),
    "matchup": ("game_routes", "get_matchup", "/api/games/matchup", True, True),
    "game_logs": ("game_routes", "get_game_logs", "/api/games/game_logs", True, True),
    "seasons": ("game_routes", "get_game_log_seasons", "/api/games/seasons", True, True),
    "unscheduled": ("game_routes", "get_unscheduled_matchup", "/api/matchups/unscheduled", True, True),
    "targets_resolve": ("user_routes", "resolve_targets", "/api/user/targets/resolve", True, True),
}


def _target_owner_uid(rank: int) -> tuple[str, int]:
    """The Firebase UID owning the ``rank``-th most Targets, and its count.

    The UID stays in process: callers print only the count.
    """

    from sqlalchemy import text

    from app.utils.db import get_engine

    with get_engine().connect() as connection:
        rows = connection.execute(
            text(
                "SELECT firebase_uid, count(*) AS n FROM targets"
                " GROUP BY firebase_uid ORDER BY n DESC, firebase_uid LIMIT :limit"
            ),
            {"limit": rank},
        ).all()
    uid, count = rows[rank - 1]
    return uid, int(count)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--now", required=True)
    parser.add_argument(
        "--case",
        action="append",
        required=True,
        help="name=route,query  e.g. matchup=matchup,game_id=0022600157",
    )
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-profile", action="store_true")
    parser.add_argument(
        "--target-owner",
        type=int,
        default=1,
        help="targets_resolve signs in as the owner of the N-th most Targets",
    )
    args = parser.parse_args()

    database_url = os.environ.get("BENCH_DATABASE_URL")
    if not database_url:
        parser.error("set BENCH_DATABASE_URL")
    from sqlalchemy.engine import make_url

    target = make_url(database_url)
    relay = _Relay(target.host, target.port or 5432)
    app, dependencies = _build(database_url, relay)
    if os.environ.get("BENCH_PATCH_FILE"):
        # A prototype patch applied in process only, to measure a candidate fix.
        exec(
            compile(Path(os.environ["BENCH_PATCH_FILE"]).read_text(), os.environ["BENCH_PATCH_FILE"], "exec"),
            {},
        )
    from flask import g

    import app as app_package
    from app.routes import game_routes, user_routes
    from app.utils.db import get_engine

    args.out.mkdir(parents=True, exist_ok=True)
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
    counters = _Counters(get_engine(), relay)
    rtt = _rtt_ms(relay, database_url)
    relay.reset()
    meta = {"app_file": app_package.__file__, "now": args.now, "rtt_ms": rtt}
    owner_uid = None
    if any(spec.split("=", 1)[1].startswith("targets_resolve") for spec in args.case):
        owner_uid, meta["target_owner_targets"] = _target_owner_uid(args.target_owner)
        meta["target_owner_rank"] = args.target_owner
        relay.reset()
    print(json.dumps(meta), flush=True)

    def make_call(route: str, query: str):
        module, attr, path, unwrap, signed_in = ROUTES[route]
        view = getattr({"game_routes": game_routes, "user_routes": user_routes}[module], attr)
        uid = owner_uid if route == "targets_resolve" else "bench"
        if unwrap:
            view = view.__wrapped__

        def call() -> tuple[int | str, bytes]:
            _pin_clock(now)
            with app.test_request_context(f"{path}?{query}" if query else path):
                if signed_in:
                    # An admitted caller without the Firebase sync: ``is_dev_bypass``
                    # makes ``is_waitlisted`` answer False with no lookup.
                    g.current_user = {"uid": uid, "email": "bench@example.invalid", "is_dev_bypass": True}
                try:
                    response = view()
                except Exception as error:
                    return type(error).__name__, repr(error).encode()
                return response.status_code, response.get_data()

        return call

    results = {"meta": meta, "cases": {}}
    for spec in args.case:
        name, rest = spec.split("=", 1)
        route, _, query = rest.partition(",")
        query = query.replace(";", "&")
        call = make_call(route, query)
        for _ in range(args.warmups):
            status, body = call()
        relay.reset()
        counters.reset()
        runs = []
        bodies = set()
        for _ in range(args.runs):
            relay.reset()
            counters.reset()
            cpu0, proc0 = time.thread_time(), time.process_time()
            started = time.perf_counter()
            status, body = call()
            wall = (time.perf_counter() - started) * 1000
            cpu = (time.thread_time() - cpu0) * 1000
            proc = (time.process_time() - proc0) * 1000
            c = counters.reset()
            r = relay.reset()
            bodies.add(hashlib.sha256(body).hexdigest())
            runs.append(
                {
                    "status": status,
                    "wall_ms": round(wall, 1),
                    "thread_cpu_ms": round(cpu, 1),
                    "process_cpu_ms": round(proc, 1),
                    **{k: v for k, v in c.items() if k != "shapes"},
                    **r,
                    "response_bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "_shapes": c["shapes"],
                }
            )
        (args.out / f"{name}.body.json").write_bytes(body)
        # Traced run: per-statement timings with literal SQL for EXPLAIN.
        counters.trace = []
        counters.capture_sql = True
        call()
        (args.out / f"{name}.trace.json").write_text(json.dumps(counters.trace, indent=1, ensure_ascii=False))
        counters.trace = None
        counters.capture_sql = False
        counters.reset()
        relay.reset()
        if not args.no_profile:
            import cProfile

            # ``BENCH_PROFILE_TIMER=cpu`` attributes thread CPU instead of wall time.
            timer = time.thread_time if os.environ.get("BENCH_PROFILE_TIMER") == "cpu" else time.perf_counter
            profiler = cProfile.Profile(timer)
            profiler.runcall(call)
            profiler.dump_stats(str(args.out / f"{name}.pstats"))
            counters.reset()
            relay.reset()
        walls = [r["wall_ms"] for r in runs]
        shapes = runs[-1]["_shapes"]
        summary = {
            "route": route,
            "query": query,
            "statuses": [r["status"] for r in runs],
            "non_200": sum(1 for r in runs if r["status"] != 200),
            "sha256_distinct": sorted(bodies),
            "wall_ms_median": round(statistics.median(walls), 1),
            "wall_ms_min": min(walls),
            "wall_ms_max": max(walls),
            **{
                f"{k}_median": round(statistics.median(r[k] for r in runs), 1)
                for k in (
                    "thread_cpu_ms",
                    "process_cpu_ms",
                    "sql_ms",
                    "statements",
                    "stmt_round_trips",
                    "round_trips",
                    "checkouts",
                    "connects",
                    "bytes_down",
                    "bytes_up",
                    "identical_repeats",
                    "response_bytes",
                )
            },
            "model_ms_median": round(
                statistics.median(
                    r["thread_cpu_ms"] + r["round_trips"] * 1.0 + r["bytes_down"] / 1e6 * 12.8 for r in runs
                ),
                1,
            ),
            "runs": [{k: v for k, v in r.items() if k != "_shapes"} for r in runs],
            "shapes_last_run": dict(sorted(shapes.items(), key=lambda kv: -kv[1])),
        }
        results["cases"][name] = summary
        print(
            json.dumps({k: v for k, v in summary.items() if k not in ("runs", "shapes_last_run")}), flush=True
        )
        (args.out / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    del dependencies
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
