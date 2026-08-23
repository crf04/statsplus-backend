"""Boot-time schema-drift guard.

Production application code must never serve traffic against a database whose
schema is behind the migration head the code expects.  A silent drift already
shipped once (code expecting migration head 046 booted against a schema still
at 45), so this guard is the cause-agnostic backstop: on boot it compares the
live schema head to the code's expected head and fails closed when the database
is behind, so the Gunicorn worker cannot start, the Railway healthcheck fails,
and the bad release is not promoted.

The guard is deliberately narrow.  It engages only for a real deployment
database: it is inert for the local/testing environments, the read-only demo
fixture, and any database that has never recorded a migration (the app-factory
build path used by the offline test suite).  A database at or ahead of the
expected head is accepted -- only a database strictly behind the head is fatal,
so a mid-rollout worker running older code against a newer schema still boots.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping

from sqlalchemy.engine import Engine

from app.config.settings import LOCAL_ENVIRONMENTS, RuntimeSettings
from app.migrations import current_schema_version, expected_schema_version
from app.utils.db import is_demo_database_url

logger = logging.getLogger(__name__)

ALLOW_SCHEMA_DRIFT_ENV = "ALLOW_SCHEMA_DRIFT"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class SchemaBehindError(RuntimeError):
    """The live database schema is behind the migration head the code expects."""


def _override_enabled(env: Mapping[str, str]) -> bool:
    return str(env.get(ALLOW_SCHEMA_DRIFT_ENV, "")).strip().lower() in _TRUE_VALUES


def verify_schema_is_current(
    engine: Engine,
    settings: RuntimeSettings,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Fail closed when a real deployment database is behind the code's schema.

    ``env`` defaults to ``os.environ`` and is injectable for tests.  The
    conservative ``ALLOW_SCHEMA_DRIFT`` override downgrades the fatal error to a
    loud warning for emergencies; it defaults to enforcing.
    """

    env = os.environ if env is None else env

    if settings.environment in LOCAL_ENVIRONMENTS:
        return
    if is_demo_database_url(settings.database.url):
        return

    current = current_schema_version(engine)
    if current is None:
        # No recorded migration head (missing bookkeeping table or an engine
        # that cannot be inspected) is not the drift this guard exists to catch,
        # and a real un-migrated database surfaces elsewhere at query time.
        return

    expected = expected_schema_version()
    if current >= expected:
        return

    message = (
        f"Database schema is behind the application code: the live schema head "
        f"is {current} but this code expects migration head {expected}. Serving "
        f"traffic would run code against an un-migrated schema. Apply the pending "
        f"migrations with `python scripts/migrate.py` (with DATABASE_URL set to "
        f"this database) and redeploy."
    )
    if _override_enabled(env):
        logger.warning(
            "%s Continuing because %s is set; remove it once the schema is "
            "migrated.",
            message,
            ALLOW_SCHEMA_DRIFT_ENV,
        )
        return
    raise SchemaBehindError(message)
