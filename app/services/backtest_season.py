"""Choose the season a Target Backtest reads (crf04/statsplus#104).

A Backtest reads exactly one of the two supported seasons, 2025-26 or
2026-27, never both.  With no season requested it reads 2025-26, whatever
season is published and whether or not 2026-27 has games; a requested
supported season overrides that, and any other season is refused.  The
published season -- the one ``research_season`` names: the
``NBA_CURRENT_SEASON`` pin when set, else the active player-game-log
publication's season -- is reported as metadata and decides only where a
season's Publications are read from.

The live pointers name only one season's Publications.  A season other than
the published one reads every stream's retained Publication through the
pointer history, so its Generation is fixed until a row is revoked, whatever
the live pointers do.  The published season reads each stream's live pointer
when it names that season, and its retained Publication otherwise (a pin
behind the pointer, or one stream activating a new season before the others).
A stream the season has in neither place fails the request with
``season_unavailable`` naming it: unavailable is not empty.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from app.errors import InvalidInputError, SeasonUnavailableError
from app.services.publication_snapshot_calls import accepts_keyword
from app.services.research_season import published_capture


SEASON_REASON_REQUESTED = "requested"
SEASON_REASON_DEFAULT = "default"
SEASON_REASON_PUBLISHED = "published"

#: The seasons a Backtest may read, in the order a client offers them.
BACKTEST_SEASONS = ("2025-26", "2026-27")
#: The season a Backtest reads when none is requested.
DEFAULT_BACKTEST_SEASON = "2025-26"

_GAME_LOGS = "player_game_logs"
_GAME_LOG_KEYS = frozenset({_GAME_LOGS})


@dataclass(frozen=True, slots=True)
class BacktestSeason:
    """The season one Backtest reads, why, and where its game logs live.

    ``retained`` is true when the season's game logs are read from the
    pointer history: always for a season other than the published one, and
    for the published season when the live game-log pointer names another.
    """

    season: str
    reason: str
    published: str
    retained: bool = False

    @property
    def past(self) -> bool:
        """Whether this season is not the published one, so is read retained."""

        return self.season != self.published


class BacktestSeasons:
    """Resolve Backtest seasons and capture a season's retained Generation."""

    def __init__(self, settings, publication_reader: Any | None):
        self.settings = settings
        self.publication_reader = publication_reader

    def resolve(
        self, requested: Any, *, session: Any | None = None
    ) -> BacktestSeason:
        """The season a request reads, before any full capture.

        One pointer-and-projection capture of the game logs decides the
        published season exactly as ``published_capture`` does for a full
        read, then ``choose`` applies the request to it.
        """

        published, live = published_capture(
            self.settings,
            self.publication_reader,
            lambda season: self._game_log_capture(season, session),
        )
        return self.choose(published, live, requested)

    @staticmethod
    def choose(published: str, live: Any, requested: Any = None) -> BacktestSeason:
        """Apply a request to a live capture taken in the published season.

        A requested season must be one of ``BACKTEST_SEASONS``; with none,
        ``DEFAULT_BACKTEST_SEASON`` is read.  ``published`` and ``live`` say
        only where the season's Publications are read from.
        """

        if requested is None:
            season, reason = DEFAULT_BACKTEST_SEASON, SEASON_REASON_DEFAULT
        elif isinstance(requested, str) and requested in BACKTEST_SEASONS:
            season, reason = requested, SEASON_REASON_REQUESTED
        else:
            raise InvalidInputError(
                f"season must be {BACKTEST_SEASONS[0]} or {BACKTEST_SEASONS[1]}."
            )
        return BacktestSeason(
            season,
            reason,
            published,
            retained=season != published
            or season != _live_season(live, published),
        )

    def _game_log_capture(self, season: Any, session: Any | None) -> Any:
        capture = getattr(self.publication_reader, "snapshot", None)
        if not callable(capture):
            return None
        keyword = {}
        if accepts_keyword(capture, "projection_only_keys"):
            keyword["projection_only_keys"] = _GAME_LOG_KEYS
        if session is not None and accepts_keyword(capture, "session"):
            keyword["session"] = session
        return capture((_GAME_LOGS,), season=season, **keyword)

    def capture(
        self,
        requested: Any,
        live_capture,
        stream_keys: Iterable[str],
        *,
        choice: BacktestSeason | None = None,
        projection_only_keys: frozenset[str] = frozenset(),
        decoded_only_keys: frozenset[str] = frozenset(),
        session: Any | None = None,
    ) -> tuple[BacktestSeason, Any]:
        """Resolve the season and capture its one Generation of ``stream_keys``.

        ``live_capture(season)`` captures the live pointers in a season.  With
        no ``choice`` and nothing requested, one live capture decides the
        published season (as ``published_capture`` does) and the default
        season is read against it.  The returned capture is ``evidence`` for the season.
        """

        captured = live = None
        if choice is None and requested is not None:
            choice = self.resolve(requested, session=session)
        if choice is None:
            captured, live = published_capture(
                self.settings, self.publication_reader, live_capture
            )
            choice = self.choose(captured, live)
        if not choice.past and choice.season != captured:
            live = live_capture(choice.season)
        return choice, self.evidence(
            choice,
            live,
            stream_keys,
            projection_only_keys=projection_only_keys,
            decoded_only_keys=decoded_only_keys,
            session=session,
        )

    def evidence(
        self,
        choice: BacktestSeason,
        live: Any,
        stream_keys: Iterable[str],
        *,
        projection_only_keys: frozenset[str] = frozenset(),
        decoded_only_keys: frozenset[str] = frozenset(),
        session: Any | None = None,
    ) -> Any:
        """The one capture a Backtest of ``choice`` reads, or raise naming a stream.

        ``live`` is a live capture in ``choice.season`` (unused for a season
        other than the published one, whose every stream is retained).  A
        published-season stream the live pointer has no Publication for in
        that season (a ``missing`` read) is replaced by its retained read; the
        result's ``retained_history`` names the rows that were.  A live
        Publication that exists but cannot be read (corrupt, unauthorised,
        without its projection) is unavailable too, never an empty season;
        see ``_readable`` for the reads that still serve.
        """

        keys = tuple(stream_keys)
        if choice.past:
            return self.retained_snapshot(
                choice,
                keys,
                projection_only_keys=projection_only_keys,
                decoded_only_keys=decoded_only_keys,
                session=session,
            )
        read_stream = getattr(live, "read", None)
        if not callable(read_stream):
            return live
        for key in keys:
            read = read_stream(key)
            if read.status != "missing" and not _readable(read):
                raise _unavailable(choice, key)
        absent = tuple(key for key in keys if read_stream(key).status == "missing")
        if not absent:
            return live
        retained = self.retained_snapshot(
            choice,
            absent,
            projection_only_keys=projection_only_keys,
            decoded_only_keys=decoded_only_keys,
            session=session,
        )
        reads = {**live.reads, **retained.reads}
        return replace(
            live,
            reads=reads,
            generation=tuple(
                (key, read.publication_id, read.fence, read.version)
                for key, read in sorted(reads.items())
            ),
            retained_history=retained.retained_history,
        )

    def require_streams(
        self,
        choice: BacktestSeason,
        stream_keys: Iterable[str],
        *,
        projection_keys: frozenset[str] = frozenset(),
        session: Any | None = None,
    ) -> tuple[tuple, ...] | None:
        """Return the identity of what ``choice.season`` reads, or raise naming a stream.

        The pointer-only check ``evidence`` makes, for a read that computes
        nothing or serves a cached result: each stream is read where
        ``evidence`` would read it -- the live pointer when it names the
        season, else the season's retained Publication -- and refused when it
        is missing there or unreadable without opening a payload (authority,
        a projection stream's projection).  A corrupt payload is found only by
        a read of it.

        The identity is that of the Publications just checked, so a cache
        lookup keyed on it cannot reach one a concurrent revocation or pointer
        move put in their place: the retained history-row ids for a retained
        season, else the live capture's generation.  ``None`` is a reader that
        captures no Generation.
        """

        keys = tuple(stream_keys)
        pointer_only = frozenset(keys)
        absent = keys
        identity = None
        if not choice.retained:
            capture = getattr(self.publication_reader, "snapshot", None)
            if not callable(capture):
                return None
            live = capture(
                keys,
                season=choice.season,
                projection_only_keys=pointer_only,
                session=session,
            )
            if not callable(getattr(live, "read", None)):
                # Nothing per stream to check; the capture still names its
                # Generation.
                generation = getattr(live, "generation", None)
                return None if generation is None else tuple(generation)
            absent = self._refuse_unreadable(
                choice, live, keys, projection_keys, missing_allowed=True
            )
            identity = tuple(live.generation)
        if absent:
            retained = self._retained_capture(
                choice.season, absent, projection_only_keys=pointer_only, session=session
            )
            if retained is None:
                raise _unavailable(choice, absent[0])
            self._refuse_unreadable(
                choice, retained, absent, projection_keys, missing_allowed=False
            )
            if choice.retained:
                identity = tuple(retained.retained_history)
        return identity

    @staticmethod
    def _refuse_unreadable(
        choice: BacktestSeason,
        snapshot: Any,
        keys: tuple[str, ...],
        projection_keys: frozenset[str],
        *,
        missing_allowed: bool,
    ) -> tuple[str, ...]:
        """Raise for an unreadable pointer-only read; return the missing keys.

        Every key is narrowed to its projection here, so only a projection
        stream's missing projection is a real refusal.
        """

        missing = []
        for key in keys:
            read = snapshot.read(key)
            if read.status == "missing":
                if not missing_allowed:
                    raise _unavailable(choice, key)
                missing.append(key)
            elif not _readable(read) and not (
                read.unavailable_reason == "publication_projection_missing"
                and key not in projection_keys
            ):
                raise _unavailable(choice, key)
        return tuple(missing)

    def retained_snapshot(
        self,
        choice: BacktestSeason,
        stream_keys: Iterable[str],
        *,
        projection_only_keys: frozenset[str] = frozenset(),
        decoded_only_keys: frozenset[str] = frozenset(),
        session: Any | None = None,
    ) -> Any:
        """Capture the season's retained Generation, or raise naming a stream."""

        keys = tuple(stream_keys)
        snapshot = self._retained_capture(
            choice.season,
            keys,
            projection_only_keys=projection_only_keys,
            decoded_only_keys=decoded_only_keys,
            session=session,
        )
        for key in keys:
            if snapshot is None or not snapshot.read(key).available:
                raise _unavailable(choice, key)
        return snapshot

    def _retained_capture(
        self,
        season: str,
        keys: tuple[str, ...],
        *,
        projection_only_keys: frozenset[str] = frozenset(),
        decoded_only_keys: frozenset[str] = frozenset(),
        session: Any | None = None,
    ) -> Any:
        capture = getattr(self.publication_reader, "retained_snapshot", None)
        if not callable(capture):
            return None
        return capture(
            keys,
            season=season,
            projection_only_keys=projection_only_keys,
            decoded_only_keys=decoded_only_keys,
            session=session,
        )


def _live_season(live: Any, published: str) -> str | None:
    """The season the live game-log pointer names, as one capture saw it.

    A capture without per-stream reads (no publication reader, or an older
    one) has no pointer to move past a season, so it serves the published
    season as it always has.
    """

    read_stream = getattr(live, "read", None)
    if not callable(read_stream):
        return published
    read = read_stream(_GAME_LOGS)
    return read.season if read.publication_id is not None else None


def _readable(read: Any) -> bool:
    """Whether a live read serves, or is the deployment's own non-Publication read.

    A disabled stream's legacy read and a stream this deployment never
    registered both read as they did before Publications; neither is an
    unreadable Publication.
    """

    return (
        read.available
        or read.legacy_fallback_allowed
        or read.unavailable_reason == "stream_not_registered"
    )


def _unavailable(choice: BacktestSeason, stream_key: str) -> SeasonUnavailableError:
    return SeasonUnavailableError(
        f"The {choice.season} season is unavailable: no retained {stream_key} "
        "Publication can be read.",
        season=choice.season,
        published_season=choice.published,
        stream=stream_key,
    )


__all__ = [
    "BacktestSeason",
    "BacktestSeasons",
    "BACKTEST_SEASONS",
    "DEFAULT_BACKTEST_SEASON",
    "SEASON_REASON_DEFAULT",
    "SEASON_REASON_PUBLISHED",
    "SEASON_REASON_REQUESTED",
]
