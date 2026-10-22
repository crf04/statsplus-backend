#!/usr/bin/env python3
"""Time ``GET /api/games/game_logs`` in process against a read-only database.

Runs the real route handler (auth skipped) with the dependency graph the web
app builds, a clock pinned to a chosen instant, and every database session
forced read-only.  Database traffic goes through a local TCP relay that counts
round trips (client-to-server turns) and bytes, so the counts do not depend on
this machine's latency to the database.  Prints one JSON line per case with
the median and range of wall time, the per-request statement, checkout,
connect and round-trip counts, and the response body's sha256.

    GAME_LOGS_BENCH_DATABASE_URL=... python scripts/benchmark_game_logs.py \\
        --now 2026-10-21T02:30:00Z --player "Jalen Brunson" --runs 7

Run it with ``PYTHONPATH`` naming the checkout to measure; the script's own
checkout is only a fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import statistics
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

sys.path.append(str(Path(__file__).resolve().parents[1]))


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
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


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
    return " < ".join(reversed(frames[-int(os.environ.get("GAME_LOGS_BENCH_FRAMES", "4")):]))


class _Counters:
    def __init__(self, engine, relay: _Relay) -> None:
        from sqlalchemy import event

        self.trace: list[dict] | None = None
        self.statements = 0
        self.sql_seconds = 0.0
        self.checkouts = 0
        self.connects = 0
        self._started: list[float] = []

        @event.listens_for(engine, "before_cursor_execute")
        def _before(*_args):
            self._started.append((time.perf_counter(), relay.bytes_down, relay.turns))

        @event.listens_for(engine, "after_cursor_execute")
        def _after(_conn, cursor, statement, *_args):
            started, bytes_down, turns = self._started.pop()
            elapsed = time.perf_counter() - started
            self.statements += 1
            self.sql_seconds += elapsed
            if self.trace is not None:
                self.trace.append(
                    {
                        "ms": round(elapsed * 1000, 1),
                        "rows": cursor.rowcount,
                        "bytes_down": relay.bytes_down - bytes_down,
                        "round_trips": relay.turns - turns,
                        "sql": " ".join(statement.split())[:240],
                        "caller": _caller(),
                    }
                )

        @event.listens_for(engine, "checkout")
        def _checkout(*_args):
            self.checkouts += 1

        @event.listens_for(engine, "connect")
        def _connect(*_args):
            self.connects += 1

    def reset(self) -> dict[str, float]:
        counts = {
            "statements": self.statements,
            "sql_ms": round(self.sql_seconds * 1000, 1),
            "checkouts": self.checkouts,
            "connects": self.connects,
        }
        self.statements = self.checkouts = self.connects = 0
        self.sql_seconds = 0.0
        return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--now", required=True, help="pinned UTC instant, e.g. 2026-10-21T02:30:00Z")
    parser.add_argument("--player", action="append", required=True, help="player_name; repeatable")
    parser.add_argument("--param", action="append", default=[], help="extra query parameter key=value")
    parser.add_argument("--runs", type=int, default=7, help="measured runs per player after one warm-up")
    parser.add_argument("--rtt-ms", type=float, default=0.0, help="extra delay injected per round trip")
    parser.add_argument("--label", default="", help="label copied into each output line")
    parser.add_argument("--body-dir", type=Path, help="write each player's response body here")
    parser.add_argument("--trace", type=Path, help="write one extra run's per-statement trace here")
    parser.add_argument("--profile", type=Path, help="cProfile one extra run per player into this directory")
    args = parser.parse_args()

    database_url = os.environ.get("GAME_LOGS_BENCH_DATABASE_URL")
    if not database_url:
        parser.error("set GAME_LOGS_BENCH_DATABASE_URL to a read-only-safe database")
    from sqlalchemy.engine import make_url

    target = make_url(database_url)
    relay = _Relay(target.host, target.port or 5432, delay_seconds=args.rtt_ms / 1000)
    app, dependencies = _build(database_url, relay)
    from app.routes import game_routes
    from app.utils.db import get_engine

    now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
    counters = _Counters(get_engine(), relay)
    view = game_routes.get_game_logs.__wrapped__
    extra = dict(item.split("=", 1) for item in args.param)

    def call(player: str) -> bytes:
        # Again per call: a module first imported during a request binds the
        # real clock.
        _pin_clock(now)
        query = urlencode({"player_name": player, **extra})
        with app.test_request_context(f"/api/games/game_logs?{query}"):
            response = view()
            return response.get_data()

    for player in args.player:
        call(player)
        relay.reset()
        counters.reset()
        walls, digests, counts = [], set(), None
        for _ in range(args.runs):
            started = time.perf_counter()
            body = call(player)
            walls.append((time.perf_counter() - started) * 1000)
            digests.add(hashlib.sha256(body).hexdigest())
            run_counts = {**counters.reset(), **relay.reset()}
            if counts is None:
                counts = {**run_counts, "varied": []}
            counts["varied"] = sorted(
                set(counts["varied"])
                | {key for key, value in run_counts.items() if key != "sql_ms" and value != counts[key]}
            )
        if args.body_dir:
            args.body_dir.mkdir(parents=True, exist_ok=True)
            (args.body_dir / f"{player.replace(' ', '_')}.json").write_bytes(body)
        if args.trace:
            args.trace.mkdir(parents=True, exist_ok=True)
            counters.trace = []
            call(player)
            (args.trace / f"{player.replace(' ', '_')}.json").write_text(json.dumps(counters.trace, indent=1))
            counters.trace = None
            counters.reset()
            relay.reset()
        if args.profile:
            import cProfile

            args.profile.mkdir(parents=True, exist_ok=True)
            cProfile.runctx(
                "call(player)",
                globals(),
                {"call": call, "player": player},
                str(args.profile / f"{player.replace(' ', '_')}.prof"),
            )
        decoded = json.loads(body)
        print(
            json.dumps(
                {
                    "label": args.label,
                    "now": args.now,
                    "player": player,
                    "runs": args.runs,
                    "wall_ms_median": round(statistics.median(walls), 1),
                    "wall_ms_min": round(min(walls), 1),
                    "wall_ms_max": round(max(walls), 1),
                    **counts,
                    "games": len(decoded.get("game_logs") or []),
                    "posted_line_game": (decoded.get("posted_line") or {}).get("game_id"),
                    "bytes": len(body),
                    "sha256": sorted(digests),
                }
            ),
            flush=True,
        )
    del dependencies
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
