"""Atomic multi-table publication for data-refresh operations.

Publishing each ``DataFrame`` to its live table with ``if_exists="replace"``
leaves a window where related tables hold mixed versions and a mid-sequence
failure silently keeps old data.  This module instead stages every frame in a
unique table first and then swaps all names inside one transaction, so readers
observe either the complete old set or the complete new set.

Table names are validated against a strict identifier grammar and always pass
through the active dialect's identifier preparer before appearing in SQL text.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Mapping

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

_STAGE_PREFIX = "__staging_"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class TablePublicationError(ValueError):
    """A table name or publication payload is invalid."""


# The callback deliberately receives the transaction's live connection.  A
# job lease renewal performed through a separate Session would commit before
# the table swap and leave a window in which a competing worker could reclaim
# the job.  Keeping this callback typed at the publisher boundary makes the
# transaction/fencing contract explicit to every caller.
PublicationFence = Callable[[Connection], None]


class AtomicTablePublisher:
    """Write frames to unique staging tables and swap them in one transaction.

    Publisher lifetimes mirror the engine they are built from: one publisher is
    long-lived per application service and reused across job runs.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def publish(
        self,
        frames: Mapping[str, pd.DataFrame],
        *,
        publication_fence: PublicationFence | None = None,
    ) -> None:
        """Publish ``frames`` (named by target table) as one atomic set.

        A failure at any point leaves the previously named tables untouched:
        staging writes and the name swaps share a single transaction.

        Raises:
            TablePublicationError: if any target name is not a safe identifier.
        """

        if not frames:
            return

        targets = {
            self._validate_identifier(table_name): frame
            for table_name, frame in frames.items()
        }
        staging_names = {
            table: self._staging_name(table) for table in targets
        }
        try:
            with self.engine.begin() as connection:
                for table in targets:
                    self._stage_frame(
                        connection, staging_names[table], targets[table]
                    )
                # Stage every frame before touching a live table.  The fence
                # is deliberately the final action before the first swap and
                # runs on this same transaction connection, so the lease
                # renewal and every table rename commit (or roll back) as one
                # unit.
                if publication_fence is not None:
                    publication_fence(connection)
                for table in targets:
                    self._swap(connection, staging_names[table], table)
        except BaseException:
            self._cleanup_staging(staging_names)
            raise

    @staticmethod
    def _validate_identifier(table_name: str) -> str:
        """Return ``table_name`` when it is safe to quote as one identifier."""

        if not isinstance(table_name, str) or not _SAFE_IDENTIFIER.fullmatch(table_name):
            raise TablePublicationError(
                f"Invalid table name: {table_name!r}"
            )
        return table_name

    @staticmethod
    def _staging_name(table_name: str) -> str:
        """Build a unique, safe staging name for one target table."""

        return f"{_STAGE_PREFIX}{table_name}_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _stage_frame(
        connection: Connection, table_name: str, frame: pd.DataFrame
    ) -> None:
        """Create and populate the staging table for one ``frame``."""

        frame.to_sql(table_name, connection, if_exists="replace", index=False)

    @staticmethod
    def _swap(connection: Connection, staging_name: str, target_name: str) -> None:
        """Atomically replace ``target_name`` with the staged data."""

        preparer = connection.dialect.identifier_preparer
        connection.execute(
            text(f"DROP TABLE IF EXISTS {preparer.quote(target_name)}")
        )
        connection.execute(
            text(f"ALTER TABLE {preparer.quote(staging_name)} "
                 f"RENAME TO {preparer.quote(target_name)}")
        )

    def _cleanup_staging(self, staging_names: Mapping[str, str]) -> None:
        """Best-effort removal of stray staging tables after a failed swap."""

        if not staging_names:
            return
        try:
            with self.engine.connect() as connection:
                preparer = connection.dialect.identifier_preparer
                for staging_name in staging_names.values():
                    connection.execute(
                        text(f"DROP TABLE IF EXISTS {preparer.quote(staging_name)}")
                    )
        except Exception as error:
            logger.warning(
                "Failed to drop one or more staging tables: %s", error
            )
