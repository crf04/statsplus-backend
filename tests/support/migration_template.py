"""Serve freshly migrated SQLite databases from a per-process template.

``run_migrations`` is the most expensive call in the offline suite: well over a
thousand tests build a brand-new SQLite database and migrate it, and every one
of those runs produces the same schema.  ``install()`` (called from
``tests/conftest.py``) replaces ``app.migrations.run_migrations`` *in the test
process only* with a wrapper that migrates one template database per process
(per xdist worker), then copies it into later empty SQLite targets with
SQLite's online backup API.  The copy is page-for-page, so the target ends up
byte-equivalent to the template, and the wrapper returns the template's
``MigrationResult`` (every migration applied, current head).

The real function runs whenever the copy could differ from a real migration:

* the current test is marked ``@pytest.mark.real_migrations`` (tests of the
  migrations themselves and of the startup schema guard);
* the engine is not a SQLAlchemy ``Engine`` for the ``sqlite`` dialect, or
  targets the tracked demo database (the real function rejects it);
* any non-dunder global of ``app.migrations`` differs from the pristine
  module, i.e. a test patched ``MIGRATIONS``, an upgrade step, or a helper;
* the engine has connection-level event listeners (a test observing the
  statements migrations execute);
* the target already contains schema objects, is mid-transaction, is
  write-locked by another connection, or was given a page size, auto-vacuum
  mode, or text encoding different from the template's.

``tests/test_migration_template.py`` proves a restored database is
indistinguishable from a freshly migrated one.
"""

from __future__ import annotations

import atexit
import shutil
import sqlite3
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

import app.migrations as migrations
import app.utils.db as db_utils

MARKER = "real_migrations"
REAL_RUN_MIGRATIONS = migrations.run_migrations

# Connection-level events that would observe the statements migrations run.
_OBSERVING_EVENTS = (
    "before_execute",
    "after_execute",
    "before_cursor_execute",
    "after_cursor_execute",
    "handle_error",
    "begin",
    "commit",
    "rollback",
    "savepoint",
    "rollback_savepoint",
    "release_savepoint",
)
# Header properties that a pre-configured empty target could carry and that a
# backup would overwrite with the template's value.
_HEADER_PRAGMAS = ("page_size", "auto_vacuum", "encoding")

_lock = threading.RLock()
_state: dict = {
    "enabled": True,
    "pristine": None,
    "directory": None,
    "source": None,
    "result": None,
    "header": None,
}
calls = {"template": 0, "real": 0}


def _fingerprint() -> dict[str, int]:
    return {
        name: id(value)
        for name, value in vars(migrations).items()
        if not name.startswith("__")
    }


def _template():
    """Return (source connection, MigrationResult, header), building it once."""
    with _lock:
        if _state["source"] is None:
            directory = tempfile.mkdtemp(prefix="statsplus-migrated-template-")
            _state["directory"] = directory
            path = Path(directory) / "template.db"
            engine = create_engine(f"sqlite:///{path}")
            try:
                _state["result"] = REAL_RUN_MIGRATIONS(engine)
            finally:
                engine.dispose()
            source = sqlite3.connect(path, check_same_thread=False)
            _state["header"] = _header(source)
            _state["source"] = source
        return _state["source"], _state["result"], _state["header"]


def _header(connection: sqlite3.Connection) -> tuple:
    return tuple(
        connection.execute(f"PRAGMA {pragma}").fetchone()[0] for pragma in _HEADER_PRAGMAS
    )


def _observed(engine: Engine) -> bool:
    return any(getattr(engine.dispatch, name, None) for name in _OBSERVING_EVENTS)


def _eligible(engine) -> bool:
    return (
        _state["enabled"]
        and isinstance(engine, Engine)
        and engine.dialect.name == "sqlite"
        and not db_utils.is_demo_database_url(str(engine.url))
        and _fingerprint() == _state["pristine"]
        and not _observed(engine)
    )


def _restore_into(engine: Engine):
    """Copy the template into ``engine``; return its result, or None if ineligible."""
    raw = engine.raw_connection()
    try:
        target = raw.driver_connection
        if not isinstance(target, sqlite3.Connection) or target.in_transaction:
            return None
        (objects,) = target.execute("SELECT count(*) FROM sqlite_master").fetchone()
        if objects:
            return None
        source, result, header = _template()
        if _header(target) != header:
            return None
        # sqlite3's backup() retries a locked target forever, while the real
        # function honours the busy timeout.  Probe for the write lock first and
        # let the real function report contention exactly as it would.
        try:
            target.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return None
        target.rollback()
        with _lock:
            source.backup(target)
        return result
    finally:
        raw.close()


def run_migrations(engine):
    """Test-process stand-in for ``app.migrations.run_migrations``."""
    if _eligible(engine):
        result = _restore_into(engine)
        if result is not None:
            calls["template"] += 1
            return result
    calls["real"] += 1
    return REAL_RUN_MIGRATIONS(engine)


@contextmanager
def disabled():
    """Force the real ``run_migrations`` for the duration of the block."""
    previous = _state["enabled"]
    _state["enabled"] = False
    try:
        yield
    finally:
        _state["enabled"] = previous


def _cleanup() -> None:
    source = _state["source"]
    if source is not None:
        source.close()
    if _state["directory"] is not None:
        shutil.rmtree(_state["directory"], ignore_errors=True)


def install() -> None:
    """Route every ``run_migrations`` reference through the template wrapper."""
    if migrations.run_migrations is run_migrations:
        return
    migrations.run_migrations = run_migrations
    # Modules that ran ``from app.migrations import run_migrations`` before this
    # hook hold the real function by value; rebind them too.
    for module in list(sys.modules.values()):
        try:
            bound = getattr(module, "run_migrations", None)
        except Exception:  # a lazy module __getattr__ must not break collection
            continue
        if bound is REAL_RUN_MIGRATIONS:
            module.run_migrations = run_migrations
    _state["pristine"] = _fingerprint()
    atexit.register(_cleanup)
