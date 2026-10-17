"""Backfill a season's closing points lines from The Odds API (#433).

For each game of the season the backfill asks the historical event-odds
endpoint for ``player_points`` at the last 5-minute mark before tip, keeps one
line per player (DraftKings first, see ``closing_lines.BOOK_ORDER``) and stores
it with its source book.  Event ids come from the historical events endpoint,
one call per game day, mapped to our games by the same event mapper the live
odds collector uses.

Spend is guarded and resumable:

* every 200 response is written to ``closing_line_purchases`` with its credits
  *before* anything is parsed, so a crash or a rerun never buys a recorded
  game again; the quotes it carried are kept there too, so a game is finished
  from the ledger alone;
* ``max_credits`` caps what one run spends and ``keep_remaining`` stops it
  before a call that would take the account's ``x-requests-remaining`` below
  that floor, leaving the live odds collector its credits.

A resolver fix reaches the games already bought without buying them again:
``ClosingLineReResolve`` re-reads the quotes kept on the ledger and rewrites each
game's lines.  It holds no client, so it can make no call and spend no credit.

Nothing here formats a URL or reads the API key; the client owns both.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app.domain.nba_events import is_completed_non_postponed_event, player_game_log_season_type
from app.domain.utc import assume_utc, utc_now
from app.models.player_game_log import PlayerGameLog
from app.providers.odds_api import (
    SPORTSBOOK_REGION,
    OddsApiError,
    parse_historical_event_odds,
)
from app.services.athlete_resolver import CLAIMING_MAPPING_STATES
from app.services.closing_lines import (
    BOOK_ORDER,
    COVERAGE_MINUTES,
    ODDS_MARKET,
    ClosingLineStore,
    CoverageReport,
    StoredPurchase,
    choose_closing_lines,
)
from app.services.event_resolver import stored_timestamp
from app.services.odds_identity import RosterNameResolver

logger = logging.getLogger(__name__)

SEASON = "2025-26"
#: ``10 x unique markets returned x regions`` for one market and one region.
GAME_COST = 10
#: A historical events list costs one credit, nothing when it is empty.
EVENTS_COST = 1
SNAPSHOT_STEP_MINUTES = 5
#: Slates are days in the US; a late tip stays on the day it belongs to.
SLATE_ZONE = ZoneInfo("America/New_York")
#: The events list is asked for at the snapshot this long before a day's first
#: tip, so every game of the day is still upcoming (listed) in it.
EVENTS_LEAD = timedelta(hours=1)
EVENTS_WINDOW_MARGIN = timedelta(hours=3)
#: Consecutive failed game calls that end a run (a dead network, not a game).
MAX_FAILURE_STREAK = 5
#: Missing players printed by the coverage report.
MISSING_SHOWN = 12
#: Still-unresolved names printed by a re-resolve.
NAMES_SHOWN = 12


def closing_snapshot_date(tip: datetime) -> datetime:
    """The last 5-minute mark strictly before ``tip``.

    The API returns the closest snapshot at or before ``date``, so a tip that
    falls exactly on a mark asks for the previous one: the mark at tip is
    already the start of the game.
    """

    earlier = assume_utc(tip) - timedelta(microseconds=1)
    return earlier.replace(
        minute=earlier.minute - earlier.minute % SNAPSHOT_STEP_MINUTES,
        second=0,
        microsecond=0,
    )


@dataclass(frozen=True, slots=True)
class TargetGame:
    game_id: str
    tip: datetime
    home_team_id: int
    away_team_id: int

    @property
    def day(self) -> date:
        return self.tip.astimezone(SLATE_ZONE).date()


def target_games(events: Iterable[Mapping[str, Any]]) -> list[TargetGame]:
    """The season's completed regular-season and playoff games, in tip order."""

    games = [
        TargetGame(
            game_id=str(event["nba_game_id"]),
            tip=stored_timestamp(event["scheduled_at"]),
            home_team_id=int(event["home_team_id"]),
            away_team_id=int(event["away_team_id"]),
        )
        for event in events
        if player_game_log_season_type(event) is not None and is_completed_non_postponed_event(event)
    ]
    return sorted(games, key=lambda game: (game.tip, game.game_id))


def spread(games: Sequence[TargetGame], count: int) -> list[TargetGame]:
    """``count`` games spread evenly over ``games`` (the middle one for one)."""

    if count >= len(games):
        return list(games)
    if count <= 1:
        return [games[len(games) // 2]] if count == 1 else []
    positions = {i * (len(games) - 1) // (count - 1) for i in range(count)}
    return [games[position] for position in sorted(positions)]


class SettledSeasonCatalog:
    """The Event Catalog of a finished season, read straight from the database.

    The event resolver refuses a catalog that was not refreshed lately.  A
    finished season's schedule cannot change, so for it the age of the last
    refresh is moot; only a catalog that was never published stays unusable.
    Reading the repository directly also keeps the provider-backed refresh
    service out of the script.
    """

    #: Longer than any refresh gap: freshness only reports "published once".
    _FOREVER = timedelta(days=36500)

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def get_events(self, season: str, **kwargs: Any) -> Any:
        return self._repository.list_events(season, **kwargs)

    def get_freshness(self, season: str, **kwargs: Any) -> dict[str, Any]:
        return self._repository.freshness(season, utc_now(), self._FOREVER, **kwargs)


class _Rows:
    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = list(rows)

    def get_catalog(self, season: str, *, active_only: bool = False):
        return list(self._rows)


class BoxScoreNames:
    """Resolve posted names within a game's own rosters, as played.

    The athlete catalog stores one team per player per season, so a player
    traded mid-season would not resolve in his earlier games.  The box score
    says whom each player played for in that game; it overrides the catalog's
    team for the players who appear in it, and a player who appears in it was
    on the roster and active that night, whatever the catalog now lists (a
    player waived since is inactive there).  Identity still goes through
    ``RosterNameResolver``: the governed mapping tables, exact normalized
    names, the suffix and nickname rules it documents, nothing fuzzy.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        catalog: Any,
        mapping_repository: Any | None,
        season: str = SEASON,
    ) -> None:
        self._engine = engine
        self._repository = mapping_repository
        self._season = season
        self._catalog_rows = [dict(row) for row in catalog.get_catalog(season, active_only=False)]

    def _box_score(self, game_id: str) -> dict[int, tuple[int, str]]:
        table = PlayerGameLog.__table__
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(table.c.player_id, table.c.team_id, table.c.player_name).where(
                    table.c.season == self._season, table.c.game_id == game_id
                )
            )
            return {int(pid): (int(team), str(name)) for pid, team, name in rows}

    def resolve(self, game_id: str, names: Iterable[str], team_ids: Collection[int]) -> dict[str, int]:
        played = self._box_score(game_id)
        rows = [
            {
                **row,
                "team_id": played[int(row["player_id"])][0],
                "roster_status": "active",
                "is_active": True,
                "is_active_for_season": True,
            }
            if int(row["player_id"]) in played
            else row
            for row in self._catalog_rows
        ]
        known = {int(row["player_id"]) for row in rows}
        rows += [
            {
                "season": self._season,
                "player_id": player_id,
                "display_name": name,
                "roster_status": "active",
                "is_active": True,
                "is_active_for_season": True,
                "team_id": team_id,
            }
            for player_id, (team_id, name) in played.items()
            if player_id not in known
        ]
        return RosterNameResolver(
            catalog=_Rows(rows), mapping_repository=self._repository, season=self._season
        ).resolve(names, team_ids)


class _Unrecorded:
    """What ``ReadOnlyMappings.record_resolution`` hands back: the claim, unwritten."""

    def __init__(self, resolution: Any) -> None:
        self._resolution = resolution

    def board_outcome(self, resolution: Any) -> "_Unrecorded":
        return self

    @property
    def canonical_player_id(self) -> int | None:
        if self._resolution.state not in CLAIMING_MAPPING_STATES:
            return None
        return self._resolution.canonical_player_id


class ReadOnlyMappings:
    """The athlete mapping repository as a re-resolve sees it: read, never written.

    The governed mappings (approvals, rejections, conflicts) are read as stored,
    so a name resolves the way the live collector would resolve it; only
    recording the resolution is skipped.
    """

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def get_mapping(self, *args: Any, **kwargs: Any) -> Any:
        return self._repository.get_mapping(*args, **kwargs)

    def get_mappings(self, *args: Any, **kwargs: Any) -> Any:
        return self._repository.get_mappings(*args, **kwargs)

    def get_rejection(self, *args: Any, **kwargs: Any) -> Any:
        return self._repository.get_rejection(*args, **kwargs)

    def get_rejections(self, *args: Any, **kwargs: Any) -> Any:
        return self._repository.get_rejections(*args, **kwargs)

    def record_resolution(self, resolution: Any, **kwargs: Any) -> _Unrecorded:
        return _Unrecorded(resolution)


class CreditGuard:
    """Refuses a call that would break the spend cap or the remaining floor.

    The cap counts the whole campaign: ``already_spent`` is what the ledger
    holds from earlier runs, ``spent`` what this run has spent.
    """

    def __init__(
        self,
        *,
        max_credits: int,
        keep_remaining: int,
        remaining: int | None,
        already_spent: int = 0,
    ) -> None:
        self.max_credits = max_credits
        self.keep_remaining = keep_remaining
        self.remaining = remaining
        self.already_spent = already_spent
        self.spent = 0

    @property
    def campaign_spent(self) -> int:
        return self.already_spent + self.spent

    def refusal(self, cost: int) -> str | None:
        """Why a call expected to cost ``cost`` must not be made, if so."""

        if self.campaign_spent + cost > self.max_credits:
            return "max_credits"
        if self.remaining is None:
            return "remaining_unknown"
        if self.remaining - cost < self.keep_remaining:
            return "keep_remaining"
        return None

    def spend(self, cost: int, remaining: int | None) -> None:
        self.spent += cost
        if remaining is not None:
            self.remaining = remaining
        elif self.remaining is not None:
            self.remaining -= cost

    def assume(self, cost: int) -> None:
        """Count a call whose outcome is unknown (it may have been charged).

        Charged to the cap and to the known balance alike; the next response's
        own header puts the balance right again.
        """

        self.spend(cost, None)


@dataclass(frozen=True, slots=True)
class Plan:
    games_total: int
    already_stored: int
    #: Recorded purchases whose body was never parsed (a fault between the
    #: receipt and the quotes); they are not bought again.
    unparsed: int
    pending: tuple[TargetGame, ...]
    days: int
    #: Credits the ledger and the events-list log already hold.
    credits_recorded: int = 0

    @property
    def estimated_credits(self) -> int:
        return len(self.pending) * GAME_COST + self.days * EVENTS_COST


@dataclass(slots=True)
class RunSummary:
    games_total: int = 0
    already_stored: int = 0
    planned: int = 0
    resumed: int = 0
    bought: int = 0
    empty: int = 0
    failed: int = 0
    unmatched: int = 0
    unparsed: int = 0
    #: Game days whose event list could not be read; their games stay for a rerun.
    days_failed: int = 0
    event_calls: int = 0
    odds_calls: int = 0
    credits: int = 0
    #: What the whole backfill has cost, earlier runs included (the cap's measure).
    campaign_credits: int = 0
    remaining: int | None = None
    stop: str | None = None


class ClosingLineBackfill:
    def __init__(
        self,
        *,
        store: ClosingLineStore,
        client: Any,
        event_mapper: Any,
        names: Any,
        events: Sequence[Mapping[str, Any]],
        max_credits: int,
        keep_remaining: int,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if getattr(client, "regions", SPORTSBOOK_REGION) != SPORTSBOOK_REGION:
            raise ValueError("the backfill buys the sportsbook region 'us' only")
        self._store = store
        self._client = client
        self._mapper = event_mapper
        self._names = names
        self._games = target_games(events)
        self._by_id = {game.game_id: game for game in self._games}
        self._max_credits = max_credits
        self._keep_remaining = keep_remaining
        self._clock = clock
        self._failure_streak = 0

    # -- plan ---------------------------------------------------------------

    def plan(self, spot_check: int | None = None) -> Plan:
        ledger = self._store.ledger()
        pending = [game for game in self._games if ledger.get(game.game_id, "failed") == "failed"]
        if spot_check is not None:
            pending = spread(pending, spot_check)
        return Plan(
            games_total=len(self._games),
            already_stored=sum(1 for game in self._games if ledger.get(game.game_id, "failed") != "failed"),
            unparsed=sum(1 for game in self._games if ledger.get(game.game_id) == "received"),
            pending=tuple(pending),
            days=len({game.day for game in pending}),
            credits_recorded=self._store.credits_recorded(),
        )

    # -- run ----------------------------------------------------------------

    def run(self, *, spot_check: int | None = None) -> RunSummary:
        summary = RunSummary()
        with self._store.exclusive_run() as acquired:
            if not acquired:
                summary.stop = "another_run_active"
                return summary
            self._resume(summary)
            plan = self.plan(spot_check)
            summary.games_total = plan.games_total
            summary.already_stored = plan.already_stored
            summary.unparsed = plan.unparsed
            summary.planned = len(plan.pending)
            summary.campaign_credits = plan.credits_recorded
            if not plan.pending:
                return summary
            try:
                quota = self._client.quota()
            except OddsApiError as error:
                logger.error("could not read the account quota: %s", error.reason)
                summary.stop = f"quota_{error.reason}"
                return summary
            guard = CreditGuard(
                max_credits=self._max_credits,
                keep_remaining=self._keep_remaining,
                remaining=quota.remaining,
                already_spent=plan.credits_recorded,
            )
            days: dict[date, list[TargetGame]] = {}
            for game in plan.pending:
                days.setdefault(game.day, []).append(game)
            for day, games in days.items():
                stop = self._run_day(day, games, guard, summary)
                if stop is not None:
                    summary.stop = stop
                    break
            summary.credits = guard.spent
            summary.campaign_credits = guard.campaign_spent
            summary.remaining = guard.remaining
        return summary

    def _resume(self, summary: RunSummary) -> None:
        """Finish games whose quotes are on the ledger; this buys nothing."""

        for purchase in self._store.purchased():
            game = self._by_id.get(purchase.game_id)
            if game is None:
                continue
            self._complete(game, purchase)
            summary.resumed += 1

    def _run_day(
        self,
        day: date,
        games: list[TargetGame],
        guard: CreditGuard,
        summary: RunSummary,
    ) -> str | None:
        refusal = guard.refusal(EVENTS_COST)
        if refusal is not None:
            return refusal
        first, last = games[0].tip, games[-1].tip
        try:
            listed = self._client.historical_events(
                first - EVENTS_LEAD,
                first - EVENTS_WINDOW_MARGIN,
                last + EVENTS_WINDOW_MARGIN,
            )
        except OddsApiError as error:
            summary.days_failed += 1
            self._failure_streak += 1
            if error.reason == "transport":
                # Sent and perhaps charged before the connection failed.
                self._lose(guard, "lost_call", str(day), EVENTS_COST)
            logger.error("historical events %s: %s", day, error.reason)
            if self._failure_streak >= MAX_FAILURE_STREAK:
                return "repeated_failures"
            return self._stop_for(error)
        # A cost header is always sent; if an unreadable answer lacks it,
        # assume the documented price rather than nothing.
        cost = listed.cost if listed.cost is not None else EVENTS_COST
        summary.event_calls += 1
        guard.spend(cost, listed.quota.remaining)
        self._store.record_spend(
            kind="events_list",
            subject=str(day),
            credits=cost,
            remaining=listed.quota.remaining,
            spent_at=self._clock(),
            events_listed=len(listed.events) if listed.readable else None,
        )
        logger.info(
            "historical events day=%s listed=%d cost=%d total=%d campaign=%d remaining=%s",
            day,
            len(listed.events),
            cost,
            guard.spent,
            guard.campaign_spent,
            guard.remaining,
        )
        if not listed.readable:
            summary.days_failed += 1
            self._failure_streak += 1
            logger.error(
                "historical events %s: the answer could not be read; its %d credits " "are recorded",
                day,
                cost,
            )
            if self._failure_streak >= MAX_FAILURE_STREAK:
                return "repeated_failures"
            return None
        self._failure_streak = 0
        mapped, _ = self._mapper.map(listed.events)
        by_game = {item.game_id: item for item in mapped}
        for game in games:
            item = by_game.get(game.game_id)
            if item is None:
                summary.unmatched += 1
                logger.warning("no Odds API event for game %s on %s", game.game_id, day)
                continue
            stop = self._buy(game, item.event, guard, summary)
            if stop is not None:
                return stop
        return None

    def _buy(self, game: TargetGame, event: Any, guard: CreditGuard, summary: RunSummary) -> str | None:
        refusal = guard.refusal(GAME_COST)
        if refusal is not None:
            return refusal
        requested_for = closing_snapshot_date(event.commence_time)
        try:
            response = self._client.historical_event_odds(
                event.event_id, requested_for, markets=(ODDS_MARKET,)
            )
        except OddsApiError as error:
            summary.failed += 1
            self._failure_streak += 1
            self._store.record_failure(
                game_id=game.game_id,
                odds_event_id=event.event_id,
                requested_for=requested_for,
                http_status=error.status,
                reason=error.reason,
                bought_at=self._clock(),
            )
            if error.reason == "transport":
                # The request may have been served and charged before the
                # connection failed: count it against the cap, durably.
                self._lose(guard, "lost_call", game.game_id, GAME_COST)
            logger.error("historical odds game=%s: %s", game.game_id, error.reason)
            if self._failure_streak >= MAX_FAILURE_STREAK:
                return "repeated_failures"
            return self._stop_for(error)
        self._failure_streak = 0
        # A cost header is always sent; if it is somehow missing, assume the
        # documented price rather than nothing.
        cost = response.cost if response.cost is not None else GAME_COST
        bought_at = self._clock()
        self._store.record_receipt(
            game_id=game.game_id,
            odds_event_id=event.event_id,
            requested_for=requested_for,
            credits=cost,
            remaining=response.quota.remaining,
            bought_at=bought_at,
        )
        guard.spend(cost, response.quota.remaining)
        summary.odds_calls += 1
        logger.info(
            "historical odds game=%s date=%s cost=%d total=%d campaign=%d remaining=%s",
            game.game_id,
            requested_for.strftime("%Y-%m-%dT%H:%MZ"),
            cost,
            guard.spent,
            guard.campaign_spent,
            guard.remaining,
        )
        try:
            snapshot_at, parsed = parse_historical_event_odds(
                response.body, event_id=event.event_id, not_after=requested_for
            )
        except OddsApiError:
            summary.unparsed += 1
            logger.error(
                "game %s: the response could not be read; its %d credits are "
                "recorded and it will not be bought again",
                game.game_id,
                cost,
            )
            return self._unexpected_cost(cost)
        lines = self._store.store_quotes(
            game.game_id, snapshot_at=snapshot_at or requested_for, parsed=parsed
        )
        if parsed.bookmaker_count:
            summary.bought += 1
            self._complete(
                game,
                StoredPurchase(
                    game_id=game.game_id,
                    odds_event_id=event.event_id,
                    snapshot_at=snapshot_at or requested_for,
                    bought_at=bought_at,
                    posted=lines,
                ),
            )
        else:
            summary.empty += 1
        return self._unexpected_cost(cost)

    @staticmethod
    def _unexpected_cost(cost: int) -> str | None:
        """A call dearer than the documented price means the plan is wrong."""

        return "unexpected_cost" if cost > GAME_COST else None

    def _lose(self, guard: CreditGuard, kind: str, subject: str, cost: int) -> None:
        """Charge a call that may have cost ``cost`` and logged nothing else."""

        guard.assume(cost)
        self._store.record_spend(
            kind=kind,
            subject=subject,
            credits=cost,
            remaining=None,
            spent_at=self._clock(),
        )

    @staticmethod
    def _stop_for(error: OddsApiError) -> str | None:
        if error.halts:
            return error.reason
        if error.reason == "rate_limited":
            return "rate_limited"
        return None

    def _complete(self, game: TargetGame, purchase: StoredPurchase) -> None:
        posted_names = {line.player_name for line in purchase.posted}
        players = self._names.resolve(game.game_id, posted_names, (game.home_team_id, game.away_team_id))
        chosen = choose_closing_lines(purchase.posted, players)
        unresolved = len(posted_names - players.keys())
        self._store.complete(purchase, chosen=chosen, unresolved=unresolved)
        logger.info(
            "game %s lines=%d books=%s unresolved_names=%d",
            game.game_id,
            len(chosen),
            ",".join(
                f"{book}:{count}"
                for book, count in sorted(
                    Counter(book for book, _ in chosen.values()).items(),
                    key=lambda item: BOOK_ORDER.index(item[0]),
                )
            ),
            unresolved,
        )


@dataclass(slots=True)
class ReResolveSummary:
    #: Games whose stored quotes were read.
    games: int = 0
    #: Recorded games the Event Catalog no longer holds (nothing to scope names to).
    skipped: int = 0
    #: Games whose lines or unresolved count differ from what the ledger holds.
    changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    unresolved_before: int = 0
    unresolved_after: int = 0
    #: Posted names still unresolved, by the number of games they are unresolved in.
    still_unresolved: Counter[str] = field(default_factory=Counter)
    #: Every game's lines as resolved now (``{game id: {player id: (book, line)}}``),
    #: for the coverage a dry run projects.
    projected: dict[str, dict[int, tuple[str, float]]] = field(default_factory=dict)
    stop: str | None = None


class ClosingLineReResolve:
    """Rewrite each recorded game's lines from the quotes kept on the ledger.

    The result is what the backfill would have stored had the resolver always
    been what it is now: the same name resolution, the same book order, one
    transaction per game.  It takes no client, so a run makes no Odds API call
    and spends no credit.  It reads the governed athlete mappings and records
    none: a posted name is one provider identity for the whole season, so
    recording it game by game would let the first player a name fits decide the
    next game's, while each game is resolved on its own two rosters.  Nothing
    but the closing-line tables is written, so a dry run (which writes nothing)
    reports exactly what a real run will do, a storage fault cannot turn a name
    into a miss, and a game whose lines and unresolved count already match is
    left alone, so a second run changes nothing.
    """

    def __init__(
        self,
        *,
        store: ClosingLineStore,
        events: Sequence[Mapping[str, Any]],
        engine: Engine,
        catalog: Any,
        mappings: Any,
    ) -> None:
        self._store = store
        self._names = BoxScoreNames(
            engine=engine, catalog=catalog, mapping_repository=ReadOnlyMappings(mappings)
        )
        self._by_id = {game.game_id: game for game in target_games(events)}

    def run(self, *, dry_run: bool) -> ReResolveSummary:
        summary = ReResolveSummary()
        if dry_run:
            self._pass(summary, write=False)
            return summary
        with self._store.exclusive_run() as acquired:
            if not acquired:
                summary.stop = "another_run_active"
                return summary
            self._pass(summary, write=True)
        return summary

    def _pass(self, summary: ReResolveSummary, *, write: bool) -> None:
        for recorded in self._store.recorded_games():
            purchase = recorded.purchase
            game = self._by_id.get(purchase.game_id)
            if game is None:
                summary.skipped += 1
                continue
            summary.games += 1
            posted_names = {line.player_name for line in purchase.posted}
            players = self._names.resolve(game.game_id, posted_names, (game.home_team_id, game.away_team_id))
            chosen = choose_closing_lines(purchase.posted, players)
            unresolved = posted_names - players.keys()
            summary.projected[game.game_id] = chosen
            summary.unresolved_before += recorded.unresolved
            summary.unresolved_after += len(unresolved)
            summary.still_unresolved.update(unresolved)
            lines = frozenset((player, book, line) for player, (book, line) in chosen.items())
            if (
                recorded.outcome == "complete"
                and lines == recorded.lines
                and len(unresolved) == recorded.unresolved
            ):
                continue
            summary.changed += 1
            summary.lines_added += len(lines - recorded.lines)
            summary.lines_removed += len(recorded.lines - lines)
            if write:
                self._store.complete(purchase, chosen=chosen, unresolved=len(unresolved))
            logger.info(
                "re-resolved game %s lines=%d unresolved_names=%d (was %d)",
                game.game_id,
                len(chosen),
                len(unresolved),
                recorded.unresolved,
            )


# -- reporting ----------------------------------------------------------------


def format_plan(plan: Plan, *, dry_run: bool, max_credits: int | None = None) -> list[str]:
    lines = [
        f"games in {SEASON} (regular season and playoffs): {plan.games_total}",
        f"already recorded: {plan.already_stored}",
        f"to buy: {len(plan.pending)} games over {plan.days} game days",
        f"estimated credits: {plan.estimated_credits} "
        f"({len(plan.pending)} x {GAME_COST} for the games + {plan.days} x {EVENTS_COST} "
        "for the event lists, an upper bound)",
    ]
    if max_credits is not None:
        lines.append(
            f"credits already spent by earlier runs: {plan.credits_recorded}; "
            f"cap for the whole backfill (--max-credits): {max_credits}"
        )
    if plan.unparsed:
        lines.append(f"recorded without readable quotes (not bought again): {plan.unparsed}")
    if dry_run:
        lines.append("dry run: no call was made")
    return lines


def format_summary(summary: RunSummary) -> list[str]:
    lines = [
        f"games: planned={summary.planned} bought={summary.bought} empty={summary.empty} "
        f"failed={summary.failed} no_event={summary.unmatched} "
        f"finished_from_ledger={summary.resumed}",
        f"calls: event_lists={summary.event_calls} game_odds={summary.odds_calls}",
        f"credits spent this run: {summary.credits}; for the whole backfill: "
        f"{summary.campaign_credits}"
        + ("" if summary.remaining is None else f" (remaining at the vendor: {summary.remaining})"),
    ]
    if summary.days_failed:
        lines.append(f"game days skipped (event list failed; a rerun retries): {summary.days_failed}")
    if summary.stop:
        lines.append(f"stopped early: {summary.stop}")
    return lines


def format_re_resolve(summary: ReResolveSummary, *, dry_run: bool) -> list[str]:
    changed = "would be rewritten" if dry_run else "rewritten"
    lines = [
        "re-resolve from the stored quotes: no Odds API call, no credits",
        f"games with stored quotes: {summary.games} "
        f"(unchanged: {summary.games - summary.changed}, {changed}: {summary.changed})",
        f"lines: {summary.lines_added} added, {summary.lines_removed} removed",
        f"posted names unresolved: {summary.unresolved_before} -> {summary.unresolved_after}",
    ]
    if summary.skipped:
        lines.append(f"recorded games not in the Event Catalog (left as they are): {summary.skipped}")
    if summary.still_unresolved:
        lines.append(f"still unresolved (most games first, first {NAMES_SHOWN}):")
        for name, games in summary.still_unresolved.most_common(NAMES_SHOWN):
            lines.append(f"  {name}: {games}")
    if summary.stop:
        lines.append(f"stopped early: {summary.stop}")
    if dry_run:
        lines.append("dry run: nothing was written")
    return lines


def format_coverage(report: CoverageReport, ledger_credits: int) -> list[str]:
    outcomes = (
        ", ".join(f"{name}={count}" for name, count in sorted(report.games_by_outcome.items())) or "none"
    )
    lines = [
        f"coverage over recorded games ({outcomes}); credits recorded (games and event lists): "
        f"{ledger_credits}",
        f"players with {COVERAGE_MINUTES}+ minutes: {report.players}; "
        f"with a closing line: {report.with_line} ({report.share:.1%})",
    ]
    for book in BOOK_ORDER:
        count = report.by_book.get(book, 0)
        if count:
            share = count / report.players if report.players else 0.0
            lines.append(f"  {book}: {count} ({share:.1%})")
    if report.missing:
        lines.append(f"without a line: {len(report.missing)} (first {MISSING_SHOWN})")
        for game_id, name, minutes in report.missing[:MISSING_SHOWN]:
            lines.append(f"  {game_id} {name} {minutes:.0f} min")
    return lines


__all__ = [
    "BoxScoreNames",
    "ClosingLineBackfill",
    "ClosingLineReResolve",
    "CreditGuard",
    "EVENTS_COST",
    "GAME_COST",
    "Plan",
    "ReResolveSummary",
    "ReadOnlyMappings",
    "RunSummary",
    "SEASON",
    "SettledSeasonCatalog",
    "TargetGame",
    "closing_snapshot_date",
    "format_coverage",
    "format_plan",
    "format_re_resolve",
    "format_summary",
    "spread",
    "target_games",
]
