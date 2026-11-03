"""Choose the season a Target Backtest reads (crf04/statsplus#104).

A Backtest reads exactly one of the two supported seasons, 2025-26 or
2026-27, never both.  With no season requested it reads the Serving Season of
the player Diets (crf04/statsplus#304): the previous season while any Diet
stream still serves it, else the published season.  A requested supported
season overrides that, and any other season is refused.  The published
season -- the one ``research_season`` names: the ``NBA_CURRENT_SEASON`` pin
when set, else the active player-game-log publication's season -- is reported
as metadata and decides where a season's Publications are read from.

The live pointers name only one season's Publications, so each choice reads
in one ``BacktestMode``.  The published season reads each stream's live pointer
when it names that season, and its retained Publication otherwise (a pin
behind the pointer, or one stream activating a new season before the others).
Any other season reads every stream's retained Publication through the
pointer history, so its Generation is fixed until a row is revoked, whatever
the live pointers do, except a requested season after the published one
before its game logs activate: it reads as an Unpublished Season, each
stream's held candidate, as if the Season Cutover had run
(crf04/statsplus-backend#625).  A stream that does
not serve exactly the chosen season fails the request with
``season_unavailable`` naming it: unavailable is not empty, and a Backtest
never mixes two seasons.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from app.config.settings import MatchupScoreSettings
from app.errors import InvalidInputError, SeasonUnavailableError
from app.services.database_first_activation import ReadSeason, SeasonUnavailable
from app.services.player_diet import PLAYER_DIET_PUBLICATION_STREAM_KEYS
from app.services.publication_snapshot_calls import accepts_keyword
from app.services.research_season import published_capture
from app.services.serving_season import next_season, previous_season

SEASON_REASON_REQUESTED = "requested"
SEASON_REASON_DEFAULT = "default"
SEASON_REASON_PUBLISHED = "published"

#: The seasons a Backtest may read, in the order a client offers them.
BACKTEST_SEASONS = ("2025-26", "2026-27")
#: The season a Backtest reports when no Publication can be read to name the
#: Diets' Serving Season.
DEFAULT_BACKTEST_SEASON = "2025-26"

_GAME_LOGS = "player_game_logs"
_GAME_LOG_KEYS = frozenset({_GAME_LOGS})


class BacktestMode(StrEnum):
    """Where one Backtest season's Publications are read from."""

    PUBLISHED = "published"  # the published season at the live pointers
    # The published season, its game logs off the live pointer (a pin behind
    # it): every stream still reads live first, but the identity a cache keys
    # on and a defender check read the retained Publications.
    PUBLISHED_LOGS_RETAINED = "published_logs_retained"
    RETAINED = "retained"  # another season, from the pointer history
    UNPUBLISHED = "unpublished"  # the next season's held candidates


@dataclass(frozen=True, slots=True)
class BacktestSeason:
    """The season one Backtest reads, why, and where its Publications live."""

    season: str
    reason: str
    published: str
    mode: BacktestMode

    @property
    def past(self) -> bool:
        """Whether this season is not the published one, so says nothing of tonight."""

        return self.season != self.published

    def thinness(self, settings: MatchupScoreSettings) -> MatchupScoreSettings:
        """The Diet thinness floors this season's players are judged under.

        An Unpublished Season is a reader's explicit choice of the next season
        before its cutover, while most players have fewer games than the Diet
        thinness floor.  That reader judges the sample from each player's
        ``season_games`` (crf04/statsplus#386), so one game clears it there.
        """

        if self.mode is not BacktestMode.UNPUBLISHED:
            return settings
        return settings.model_copy(update={"min_games": 1})


class BacktestSeasons:
    """Resolve Backtest seasons and capture a season's retained Generation."""

    def __init__(self, settings, publication_reader: Any | None):
        self.settings = settings
        self.publication_reader = publication_reader

    def resolve(self, requested: Any, *, session: Any | None = None) -> BacktestSeason:
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
    def choose(published: str, live: Any, requested: Any = None, *, diets: Any = None) -> BacktestSeason:
        """Apply a request to a live capture taken in the published season.

        A requested season must be one of ``BACKTEST_SEASONS``; with none,
        the Diets' Serving Season is read, as ``diets`` (else ``live``)
        captured them.  ``published`` and ``live`` also say where the
        season's Publications are read from: only a requested season after
        the published one whose game logs have not activated is Unpublished.
        """

        if requested is None:
            season = _diet_serving_season(published, live if diets is None else diets)
            reason = SEASON_REASON_DEFAULT
        elif isinstance(requested, str) and requested in BACKTEST_SEASONS:
            season, reason = requested, SEASON_REASON_REQUESTED
        else:
            raise InvalidInputError(f"season must be {BACKTEST_SEASONS[0]} or {BACKTEST_SEASONS[1]}.")
        live_season = _live_season(live, published)
        if season == published:
            mode = BacktestMode.PUBLISHED if season == live_season else BacktestMode.PUBLISHED_LOGS_RETAINED
        elif season == next_season(published) != live_season:
            mode = BacktestMode.UNPUBLISHED
        else:
            mode = BacktestMode.RETAINED
        return BacktestSeason(season, reason, published, mode)

    def _game_log_capture(self, season: Any, session: Any | None) -> Any:
        """Capture the game logs and, pointer-only, the Diets' seasons."""

        capture = getattr(self.publication_reader, "snapshot", None)
        if not callable(capture):
            return None
        keys: tuple[str, ...] = (_GAME_LOGS,)
        keyword = {}
        if accepts_keyword(capture, "projection_only_keys"):
            keys = (_GAME_LOGS, *sorted(PLAYER_DIET_PUBLICATION_STREAM_KEYS))
            keyword["projection_only_keys"] = frozenset(keys)
        if session is not None and accepts_keyword(capture, "session"):
            keyword["session"] = session
        return capture(keys, season=season, **keyword)

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
            captured, live = published_capture(self.settings, self.publication_reader, live_capture)
            diets = self._game_log_capture(captured, session) if _lacks_diets(live) else None
            choice = self.choose(captured, live, diets=diets)
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

        ``live`` is a live capture in ``choice.season``, read only for the
        published season; the others capture their own.  A
        published-season stream the live pointer has no Publication for in
        that season (a ``missing`` read) is replaced by its retained read; the
        result's ``retained_history`` names the rows that were.  A live
        Publication that exists but cannot be read (corrupt, unauthorised,
        without its projection) is unavailable too, never an empty season;
        see ``_readable`` for the reads that still serve.
        """

        keys = tuple(stream_keys)
        shape = {
            "projection_only_keys": projection_only_keys,
            "decoded_only_keys": decoded_only_keys,
            "session": session,
        }
        if choice.mode is BacktestMode.RETAINED:
            return self.retained_snapshot(choice, keys, **shape)
        if choice.mode is BacktestMode.UNPUBLISHED:
            snapshot = self._unpublished_capture(choice, keys, **shape)
            _refuse_unserved(choice, snapshot, keys, frozenset(keys))
            return snapshot
        read_stream = getattr(live, "read", None)
        if not callable(read_stream):
            return live
        for key in keys:
            read = read_stream(key)
            if not _absent(read, choice.season) and not _readable(read):
                raise _unavailable(choice, key)
        absent = tuple(key for key in keys if _absent(read_stream(key), choice.season))
        if not absent:
            return live
        retained = self.retained_snapshot(choice, absent, **shape)
        reads = {**live.reads, **retained.reads}
        return replace(
            live,
            reads=reads,
            generation=tuple(
                (key, read.publication_id, read.fence, read.version) for key, read in sorted(reads.items())
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
        season, the held candidates' generation for an Unpublished one (a
        newer candidate is a new key, and one a ledger correction withdrew is
        refused here), else the live capture's generation.  ``None`` is a
        reader that captures no Generation.
        """

        keys = tuple(stream_keys)
        pointer_only = frozenset(keys)
        if choice.mode is BacktestMode.UNPUBLISHED:
            snapshot = self._unpublished_capture(
                choice, keys, projection_only_keys=pointer_only, session=session
            )
            _refuse_unserved(choice, snapshot, keys, projection_keys)
            return tuple(snapshot.generation)
        absent = keys
        identity = None
        if choice.mode is BacktestMode.PUBLISHED:
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
            absent = self._refuse_unreadable(choice, live, keys, projection_keys, missing_allowed=True)
            identity = tuple(live.generation)
        if absent:
            retained = self._retained_capture(
                choice.season, absent, projection_only_keys=pointer_only, session=session
            )
            if retained is None:
                raise _unavailable(choice, absent[0])
            self._refuse_unreadable(choice, retained, absent, projection_keys, missing_allowed=False)
            if choice.mode is not BacktestMode.PUBLISHED:
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
            if _absent(read, choice.season):
                if not missing_allowed:
                    raise _unavailable(choice, key)
                missing.append(key)
            elif not _readable(read) and not (
                read.unavailable_reason == "publication_projection_missing" and key not in projection_keys
            ):
                raise _unavailable(choice, key)
        return tuple(missing)

    def defender_game_logs(self, choice: BacktestSeason) -> Any | None:
        """The game logs a Backtest of ``choice`` checks a defender against.

        ``None`` is the live pointer's; otherwise the same Publications
        ``require_streams`` keys the Backtest on.
        """

        if choice.mode is BacktestMode.PUBLISHED:
            return None
        if choice.mode is BacktestMode.UNPUBLISHED:
            return self.evidence(choice, None, (_GAME_LOGS,), projection_only_keys=_GAME_LOG_KEYS)
        return self.retained_snapshot(choice, (_GAME_LOGS,), projection_only_keys=_GAME_LOG_KEYS)

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

    def _unpublished_capture(
        self,
        choice: BacktestSeason,
        keys: tuple[str, ...],
        *,
        projection_only_keys: frozenset[str] = frozenset(),
        decoded_only_keys: frozenset[str] = frozenset(),
        session: Any | None = None,
    ) -> Any:
        """Capture the season's held candidates through a reader scoped to it.

        The scoped reader judges each candidate as the cutover would, its
        ledger lineage on every capture; game logs that cannot move refuse
        the season naming them.
        """

        scoped = getattr(self.publication_reader, "for_season", None)
        if not callable(scoped):
            raise _unavailable(choice, _GAME_LOGS)
        try:
            return scoped(choice.season).snapshot(
                keys,
                season=ReadSeason(choice.published, requested=choice.season),
                projection_only_keys=projection_only_keys,
                decoded_only_keys=decoded_only_keys,
                session=session,
            )
        except SeasonUnavailable:
            raise _unavailable(choice, _GAME_LOGS) from None

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


def _refuse_unserved(
    choice: BacktestSeason, snapshot: Any, keys: tuple[str, ...], projection_keys: frozenset[str]
) -> None:
    """Raise naming the first stream that does not serve ``choice.season`` itself.

    A trailing family's previous-season read, an unopened window and an
    unreadable candidate all refuse; only a narrowed read's missing
    projection on a stream outside ``projection_keys`` does not.
    """

    for key in keys:
        read = snapshot.read(key)
        if read.season != choice.season or not (
            read.available
            or (read.unavailable_reason == "publication_projection_missing" and key not in projection_keys)
        ):
            raise _unavailable(choice, key)


def _diet_serving_season(published: str, live: Any) -> str:
    """The Serving Season of the player Diets ``live`` captured.

    Their previous season while any Diet stream still serves it, else the
    published season.  A capture without per-stream reads names no Diet.
    """

    reads = getattr(live, "reads", None)
    if not isinstance(reads, Mapping):
        return published
    prior = previous_season(published)
    if any(getattr(reads.get(key), "season", None) == prior for key in PLAYER_DIET_PUBLICATION_STREAM_KEYS):
        return prior
    return published


def _lacks_diets(capture: Any) -> bool:
    """Whether a capture with per-stream reads read no Diet stream."""

    reads = getattr(capture, "reads", None)
    return isinstance(reads, Mapping) and PLAYER_DIET_PUBLICATION_STREAM_KEYS.isdisjoint(reads)


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


def _absent(read: Any, season: str) -> bool:
    """Whether ``season`` has no Publication at this read.

    A trailing family's previous-season read is another season's facts, so a
    Backtest of the Published Season looks for that season's retained one.
    """

    return read.status == "missing" or (read.season is not None and read.season != season)


def _readable(read: Any) -> bool:
    """Whether a live read serves, or is the deployment's own non-Publication read.

    A disabled stream's legacy read and a stream this deployment never
    registered both read as they did before Publications; neither is an
    unreadable Publication.
    """

    return (
        read.available or read.legacy_fallback_allowed or read.unavailable_reason == "stream_not_registered"
    )


def _unavailable(choice: BacktestSeason, stream_key: str) -> SeasonUnavailableError:
    cause = (
        f"its held {stream_key} candidate does not serve it yet"
        if choice.mode is BacktestMode.UNPUBLISHED
        else f"no retained {stream_key} Publication can be read"
    )
    return SeasonUnavailableError(
        f"The {choice.season} season is unavailable: {cause}.",
        season=choice.season,
        published_season=choice.published,
        stream=stream_key,
    )


__all__ = [
    "BACKTEST_SEASONS",
    "DEFAULT_BACKTEST_SEASON",
    "SEASON_REASON_DEFAULT",
    "SEASON_REASON_PUBLISHED",
    "SEASON_REASON_REQUESTED",
    "BacktestSeason",
    "BacktestSeasons",
]
