"""PrizePicks adapter for the shared NBA DFS snapshot contract."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from app.errors import ProviderUnavailableError
from app.providers.dfs import (
    AthleteEvidence,
    CompetitionEvidence,
    CoverageEvidence,
    DeadlineExceededError,
    EventEvidence,
    LeagueEvidence,
    MarketStatus,
    MarketThreshold,
    MalformedProviderResponseError,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    RetrievalContext,
    ScoringPeriod,
    SnapshotStatus,
    SportEvidence,
    StatisticEvidence,
    TeamEvidence,
    normalize_market_variant,
    normalize_timestamp,
)
from app.utils.telemetry import (
    CACHE_DISABLED,
    PROVIDER_PRIZEPICKS,
    ProviderResponseError,
    provider_call,
)

logger = logging.getLogger(__name__)

_NON_PLAYER_MARKET_KINDS = {
    "team",
    "teams",
    "match",
    "matches",
    "game",
    "games",
    "entry",
    "entry-placement",
    "placement",
}
_FUTURES_MARKET_KINDS = {"future", "futures"}


class _MalformedPage(MalformedProviderResponseError):
    """The whole PrizePicks page cannot be interpreted safely."""


class _MalformedRecord(MalformedProviderResponseError):
    """One PrizePicks record cannot be represented safely."""


class _ExcludedRecord(Exception):
    """One valid upstream record is outside the requested board scope."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class _PageResult:
    markets: tuple[PlayerProjectionMarket, ...]
    fetched_count: int
    eligible_count: int
    normalized_count: int
    skipped_count: int
    warning_codes: tuple[str, ...]
    skipped_reasons: tuple[str, ...]
    malformed_count: int
    current_page: int
    total_pages: int
    expected_total: int | None


class PrizePicksAdapter:
    """Retrieve and normalize PrizePicks' JSON:API projection board."""

    name = "prizepicks"
    PROVIDER_NAME = name
    BASE_URL = "https://api.prod01.universe.prizepicks.com/projections"
    DEFAULT_TIMEOUT = (10.0, 30.0)
    _LEAGUE_IDS = {"NBA": 7}

    def __init__(
        self,
        *,
        session: requests.Session | Any | None = None,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Origin": "https://app.prizepicks.com",
                "Referer": "https://app.prizepicks.com/",
                "User-Agent": "StatsPlus/1.0",
            }
        )

    def get_snapshot(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext,
    ) -> ProviderSnapshot:
        """Fetch every current eligible PrizePicks page for ``query``."""

        league_id = self._LEAGUE_IDS[query.league]
        retrieved_at = self._now_utc()
        page = 1
        total_pages = 1
        expected_total: int | None = None
        markets: list[PlayerProjectionMarket] = []
        seen: dict[tuple[str, str], PlayerProjectionMarket] = {}
        conflicting: set[tuple[str, str]] = set()
        fetched_count = 0
        eligible_count = 0
        normalized_count = 0
        skipped_count = 0
        malformed_count = 0
        warning_codes: list[str] = []
        skipped_reasons: list[str] = []
        pagination_complete = True
        fanout_complete = True
        expected_total_seen = False

        while page <= total_pages:
            try:
                result = self._request_page(
                    context=context,
                    league_id=league_id,
                    page=page,
                    expected_sport=query.league,
                    allowed_statuses=query.market_statuses,
                )
            except ProviderUnavailableError as error:
                if not markets:
                    raise
                pagination_complete = False
                fanout_complete = False
                warning_codes.append("page_fetch_failed")
                logger.warning("PrizePicks page %s failed after usable data: %s", page, error)
                break
            except _MalformedPage as error:
                if not markets:
                    raise self._invalid_response(error) from error
                pagination_complete = False
                fanout_complete = False
                warning_codes.append("page_malformed")
                if "meta." in str(error):
                    warning_codes.append("page_metadata_mismatch")
                    skipped_reasons.append("pagination_metadata_malformed")
                logger.warning("PrizePicks page %s was malformed after usable data", page)
                break

            if result.current_page != page:
                if not markets:
                    raise self._invalid_response(
                        f"requested page {page}, received page {result.current_page}"
                    )
                pagination_complete = False
                fanout_complete = False
                warning_codes.append("page_metadata_mismatch")
                skipped_reasons.append("pagination_page_mismatch")
                break
            if result.total_pages < total_pages:
                if not markets:
                    raise self._invalid_response("pagination total_pages shrank")
                pagination_complete = False
                fanout_complete = False
                warning_codes.append("page_metadata_mismatch")
                skipped_reasons.append("pagination_total_pages_changed")
                break
            total_pages = max(total_pages, result.total_pages)
            if not expected_total_seen:
                expected_total = result.expected_total
                expected_total_seen = True
            elif result.expected_total != expected_total:
                if not markets:
                    raise self._invalid_response("pagination expected total changed")
                pagination_complete = False
                fanout_complete = False
                warning_codes.append("page_metadata_mismatch")
                skipped_reasons.append("pagination_expected_total_changed")
                break
            fetched_count += result.fetched_count
            eligible_count += result.eligible_count
            normalized_count += result.normalized_count
            skipped_count += result.skipped_count
            malformed_count += result.malformed_count
            warning_codes.extend(result.warning_codes)
            skipped_reasons.extend(result.skipped_reasons)
            if result.malformed_count:
                fanout_complete = False

            for market in result.markets:
                identity = market.source_identity
                if identity is None:
                    markets.append(market)
                    continue
                previous = seen.get(identity)
                if previous is None:
                    seen[identity] = market
                    markets.append(market)
                elif previous == market:
                    warning_codes.append("duplicate_source_identity")
                else:
                    conflicting.add(identity)
                    skipped_count += 1
                    malformed_count += 1
                    fanout_complete = False
                    warning_codes.append("conflicting_source_identity")
                    skipped_reasons.append("conflicting_source_identity")

            page += 1

        if conflicting:
            markets = [market for market in markets if market.source_identity not in conflicting]
        if malformed_count:
            fanout_complete = False

        coverage = CoverageEvidence(
            fetched_count=fetched_count,
            eligible_count=eligible_count,
            normalized_count=normalized_count,
            skipped_count=skipped_count,
            pagination_complete=pagination_complete,
            fanout_complete=fanout_complete,
            expected_total=expected_total,
            warning_codes=tuple(dict.fromkeys(warning_codes)),
            skipped_reasons=tuple(dict.fromkeys(skipped_reasons)),
        )
        if malformed_count and not markets:
            raise self._invalid_response("no usable PrizePicks projection records")
        status = SnapshotStatus.PARTIAL if not coverage.is_complete else SnapshotStatus.COMPLETE
        return ProviderSnapshot(
            provider=self.name,
            status=status,
            markets=tuple(markets),
            coverage=coverage,
            retrieved_at=retrieved_at,
        )

    def _request_page(
        self,
        *,
        context: RetrievalContext,
        league_id: int,
        page: int,
        expected_sport: str,
        allowed_statuses: tuple[MarketStatus | str, ...],
    ) -> _PageResult:
        try:
            self._ensure_active(context)
            timeout = self._bounded_timeout(context)
            with provider_call(
                PROVIDER_PRIZEPICKS,
                "get_snapshot",
                cache_status=CACHE_DISABLED,
                request_id=context.request_id,
            ) as tracker:
                response = self.session.get(
                    self.BASE_URL,
                    params={
                        "league_id": league_id,
                        "page": page,
                        "per_page": 250,
                        "single_stat": "true",
                    },
                    timeout=timeout,
                )
                self._ensure_active(context)
                tracker.status_code = getattr(response, "status_code", None)
                response.raise_for_status()
                self._ensure_active(context)
                try:
                    payload = response.json()
                except (TypeError, ValueError) as error:
                    raise ProviderResponseError(
                        "PrizePicks returned invalid JSON"
                    ) from error
                self._ensure_active(context)
                try:
                    result = self._parse_page(
                        payload,
                        expected_sport=expected_sport,
                        allowed_statuses=allowed_statuses,
                    )
                except _MalformedPage as error:
                    raise ProviderResponseError(str(error)) from error
                self._ensure_active(context)
                return result
        except DeadlineExceededError as error:
            raise ProviderUnavailableError(
                "PrizePicks retrieval deadline exceeded.", detail=error
            ) from error
        except requests.exceptions.Timeout as error:
            raise ProviderUnavailableError(
                "PrizePicks timed out while fetching lines.", detail=error
            ) from error
        except requests.exceptions.RequestException as error:
            raise ProviderUnavailableError(
                "PrizePicks could not be reached.", detail=error
            ) from error
        except ProviderResponseError as error:
            raise _MalformedPage(str(error)) from error

    def _bounded_timeout(self, context: RetrievalContext) -> tuple[float, float]:
        remaining = context.remaining_seconds(now=self._now_utc())
        if remaining <= 0:
            raise DeadlineExceededError("PrizePicks retrieval deadline exceeded")
        connect, read = self.timeout
        return min(float(connect), remaining), min(float(read), remaining)

    def _ensure_active(self, context: RetrievalContext) -> None:
        context.ensure_active(now=self._now_utc())

    def _now_utc(self) -> datetime:
        value = self.now()
        if not isinstance(value, datetime):
            raise ValueError("PrizePicks clock must return an aware datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("PrizePicks clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    @classmethod
    def _parse_page(
        cls,
        payload: Any,
        *,
        expected_sport: str,
        allowed_statuses: tuple[MarketStatus | str, ...],
    ) -> _PageResult:
        if not isinstance(payload, Mapping):
            raise _MalformedPage("payload must be an object")
        rows = payload.get("data")
        included = payload.get("included", [])
        meta = payload.get("meta", {})
        if not isinstance(rows, list) or not isinstance(included, list):
            raise _MalformedPage("data and included must be lists")
        if not isinstance(meta, Mapping):
            raise _MalformedPage("meta must be an object")
        resources = cls._index_resources(included)
        current_page = cls._positive_int(meta.get("current_page"), "meta.current_page")
        total_pages = cls._positive_int(meta.get("total_pages", 1), "meta.total_pages")
        if current_page > total_pages:
            raise _MalformedPage("meta.current_page must not exceed meta.total_pages")
        expected_total = cls._optional_nonnegative_int(
            meta.get("total_count", meta.get("total_projections")),
            "meta.total_count",
        )

        markets: list[PlayerProjectionMarket] = []
        warning_codes: list[str] = []
        skipped_reasons: list[str] = []
        skipped_count = 0
        malformed_count = 0
        eligible_count = 0
        normalized_count = 0
        for row in rows:
            try:
                market = cls._normalize_projection(
                    row,
                    resources=resources,
                    expected_sport=expected_sport,
                    allowed_statuses=allowed_statuses,
                )
            except _ExcludedRecord as error:
                skipped_count += 1
                skipped_reasons.append(error.reason)
            except _MalformedRecord as error:
                skipped_count += 1
                malformed_count += 1
                warning_codes.append("malformed_record")
                skipped_reasons.append(str(error) or "malformed_record")
            else:
                eligible_count += 1
                normalized_count += 1
                markets.append(market)

        return _PageResult(
            markets=tuple(markets),
            fetched_count=len(rows),
            eligible_count=eligible_count,
            normalized_count=normalized_count,
            skipped_count=skipped_count,
            warning_codes=tuple(dict.fromkeys(warning_codes)),
            skipped_reasons=tuple(dict.fromkeys(skipped_reasons)),
            malformed_count=malformed_count,
            current_page=current_page,
            total_pages=total_pages,
            expected_total=expected_total,
        )

    @classmethod
    def _normalize_projection(
        cls,
        row: Any,
        *,
        resources: Mapping[tuple[str, str], Mapping[str, Any]],
        expected_sport: str,
        allowed_statuses: tuple[MarketStatus | str, ...],
    ) -> PlayerProjectionMarket:
        if not isinstance(row, Mapping):
            raise _MalformedRecord("projection must be an object")
        if row.get("type") != "projection":
            raise _ExcludedRecord("non_projection_market")
        attributes = row.get("attributes")
        relationships = row.get("relationships")
        if not isinstance(attributes, Mapping) or not isinstance(relationships, Mapping):
            raise _MalformedRecord("projection attributes and relationships must be objects")
        market_kind = cls._market_kind(attributes)
        if market_kind == "non_player":
            raise _ExcludedRecord("non_player_market")
        is_future = market_kind == "future"
        projection_id = cls._required_identifier(row, "id")
        player_relation = cls._optional_relationship(relationships, "new_player")
        if player_relation is None and is_future:
            raise _ExcludedRecord("non_player_market")
        player_id = cls._relationship_id(relationships, "new_player")
        league_id = cls._relationship_id(relationships, "league")
        player = resources.get(("new_player", player_id))
        league = resources.get(("league", league_id))
        if player is None or league is None:
            raise _MalformedRecord("projection relationships could not be resolved")
        player_attributes = player.get("attributes")
        league_attributes = league.get("attributes")
        if not isinstance(player_attributes, Mapping) or not isinstance(league_attributes, Mapping):
            raise _MalformedRecord("related resource attributes must be objects")

        league_name = cls._required_text(league_attributes, "name")
        if league_name.casefold() != expected_sport.casefold():
            raise _ExcludedRecord("non_nba_market")
        raw_status = cls._required_text(attributes, "status")
        status = cls._normalize_status(raw_status)
        if status is None:
            raise _ExcludedRecord("ineligible_status")
        if status not in allowed_statuses:
            raise _ExcludedRecord("status_filter")

        player_name = cls._required_text(player_attributes, "name")
        stat_label = cls._required_text(attributes, "stat_type")
        line_score = cls._required_number(attributes, "line_score")
        start_value = attributes.get("start_time")
        updated_value = attributes.get("updated_at")
        cls._validate_timestamp(start_value, "start_time")
        cls._validate_timestamp(updated_value, "updated_at")

        team = cls._team_from_mapping(player_attributes)
        sport = SportEvidence(label=league_name)
        competition = CompetitionEvidence(
            provider_id=league_id,
            label=league_name,
            sport=sport,
        )
        event_relation = cls._event_relationship(relationships)
        if is_future and event_relation is None:
            raise _ExcludedRecord("missing_event_relationship")
        event = cls._event_from_projection(attributes, relationships, resources)
        if is_future and event is None:
            raise _ExcludedRecord("missing_event_relationship")
        variant_label = cls._optional_text(attributes.get("odds_type"))
        variant = normalize_market_variant(variant_label)
        period_label = cls._optional_text(
            attributes.get("scoring_period", attributes.get("period"))
        )
        scoring_period = (
            period_label if period_label is not None else ScoringPeriod.UNKNOWN
        )

        try:
            return PlayerProjectionMarket(
                provider=cls.name,
                market_id=projection_id,
                athlete=AthleteEvidence(
                    provider_id=player_id,
                    name=player_name,
                    team=team,
                ),
                event=event,
                team=team,
                league=LeagueEvidence(provider_id=league_id, label=league_name),
                competition=competition,
                sport=sport,
                statistic=StatisticEvidence(label=stat_label),
                threshold=MarketThreshold(
                    line_score,
                    unit="count",
                    original_value=cls._display_number(attributes["line_score"]),
                ),
                status=status,
                status_label=raw_status,
                variant=variant.value,
                variant_label=variant_label,
                scoring_period=scoring_period,
                scoring_period_label=period_label,
                starts_at=start_value,
                updated_at=updated_value,
                selections=(),
            )
        except ValueError as error:
            raise _MalformedRecord(str(error)) from error

    @classmethod
    def _event_from_projection(
        cls,
        attributes: Mapping[str, Any],
        relationships: Mapping[str, Any],
        resources: Mapping[tuple[str, str], Mapping[str, Any]],
    ) -> EventEvidence | None:
        relation = cls._event_relationship(relationships)
        resource: Mapping[str, Any] | None = None
        provider_id: str | None = None
        if relation is not None:
            relation_type, provider_id = relation
            resource = resources.get((relation_type, provider_id))
        event_attributes = resource.get("attributes", {}) if resource else {}
        if not isinstance(event_attributes, Mapping):
            raise _MalformedRecord("event resource attributes must be an object")
        label = cls._optional_text(
            event_attributes.get("name", event_attributes.get("description"))
        ) or cls._optional_text(attributes.get("description"))
        starts_at = event_attributes.get("start_time", attributes.get("start_time"))
        updated_at = event_attributes.get("updated_at", attributes.get("updated_at"))
        status_label = cls._optional_text(event_attributes.get("status")) or cls._optional_text(
            event_attributes.get("status_label")
        )
        if status_label is not None and cls._is_ineligible_event_status(status_label):
            raise _ExcludedRecord("ineligible_event_status")
        cls._validate_timestamp(starts_at, "event start_time")
        cls._validate_timestamp(updated_at, "event updated_at")
        if provider_id is None and label is None and starts_at is None and updated_at is None:
            return None
        try:
            return EventEvidence(
                provider_id=provider_id,
                label=label,
                status_label=status_label,
                starts_at=starts_at,
                updated_at=updated_at,
            )
        except ValueError as error:
            raise _MalformedRecord(str(error)) from error

    @classmethod
    def _event_relationship(
        cls,
        relationships: Mapping[str, Any],
    ) -> tuple[str, str] | None:
        for name in ("event", "game", "fixture", "match"):
            if name in relationships:
                return cls._optional_relationship(relationships, name)
        return None

    @staticmethod
    def _market_kind(attributes: Mapping[str, Any]) -> str | None:
        """Classify provider market labels without treating them as player stats."""

        for key in (
            "projection_type",
            "projectionType",
            "market_type",
            "marketType",
            "market_kind",
            "marketKind",
            "type",
            "stat_type",
            "statType",
        ):
            value = attributes.get(key)
            if not isinstance(value, str):
                continue
            normalized = value.strip().casefold().replace("_", "-").replace(" ", "-")
            if not normalized:
                continue
            if normalized in _FUTURES_MARKET_KINDS or "future" in normalized:
                return "future"
            if normalized in _NON_PLAYER_MARKET_KINDS or any(
                normalized.startswith(f"{prefix}-")
                for prefix in _NON_PLAYER_MARKET_KINDS
            ):
                return "non_player"
        return None

    @classmethod
    def _index_resources(
        cls,
        included: list[Any],
    ) -> dict[tuple[str, str], Mapping[str, Any]]:
        resources: dict[tuple[str, str], Mapping[str, Any]] = {}
        for resource in included:
            if not isinstance(resource, Mapping):
                raise _MalformedPage("included resources must be objects")
            try:
                resource_type = cls._required_text(resource, "type")
                resource_id = cls._required_identifier(resource, "id")
            except _MalformedRecord as error:
                raise _MalformedPage(str(error)) from error
            key = (resource_type, resource_id)
            previous = resources.get(key)
            if previous is not None and previous != resource:
                raise _MalformedPage("included resource identity has conflicting content")
            resources[key] = resource
        return resources

    @classmethod
    def _relationship_id(cls, relationships: Mapping[str, Any], name: str) -> str:
        relation = cls._optional_relationship(relationships, name)
        if relation is None:
            raise _MalformedRecord(f"missing {name} relationship")
        expected_type, identifier = relation
        if name == "new_player" and expected_type != "new_player":
            raise _MalformedRecord(f"{name} relationship has an unexpected type")
        if name == "league" and expected_type != "league":
            raise _MalformedRecord(f"{name} relationship has an unexpected type")
        return identifier

    @classmethod
    def _optional_relationship(
        cls,
        relationships: Mapping[str, Any],
        name: str,
    ) -> tuple[str, str] | None:
        relationship = relationships.get(name)
        if relationship is None:
            return None
        if not isinstance(relationship, Mapping):
            raise _MalformedRecord(f"{name} relationship must be an object")
        data = relationship.get("data")
        if data is None:
            return None
        if not isinstance(data, Mapping):
            raise _MalformedRecord(f"{name} relationship data must be an object")
        return cls._required_text(data, "type"), cls._required_identifier(data, "id")

    @staticmethod
    def _normalize_status(label: str) -> MarketStatus | None:
        normalized = label.strip().casefold().replace("-", "_")
        if normalized in {"pre_game", "pregame", "active", "open", "available"}:
            return MarketStatus.AVAILABLE
        if normalized in {"suspended", "paused"}:
            return MarketStatus.SUSPENDED
        return None

    @staticmethod
    def _is_ineligible_event_status(label: str) -> bool:
        normalized = label.strip().casefold().replace("-", "_").replace(" ", "_")
        return normalized in {"live", "closed", "settled", "final", "in_play", "inplay"}

    @staticmethod
    def _required_identifier(value: Mapping[str, Any], key: str) -> str:
        identifier = value.get(key)
        if identifier is None or isinstance(identifier, bool) or not str(identifier).strip():
            raise _MalformedRecord(f"{key} must be present")
        return str(identifier)

    @classmethod
    def _required_text(cls, value: Mapping[str, Any], key: str) -> str:
        text = cls._optional_text(value.get(key))
        if text is None:
            raise _MalformedRecord(f"{key} must be a non-empty string")
        return text

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        return text or None

    @classmethod
    def _required_number(cls, value: Mapping[str, Any], key: str) -> str | int | float:
        raw = value.get(key)
        if isinstance(raw, bool) or raw is None or not isinstance(raw, (str, int, float)):
            raise _MalformedRecord(f"{key} must be numeric")
        try:
            decimal = MarketThreshold(raw, unit="count").value
        except ValueError as error:
            raise _MalformedRecord(f"{key} must be numeric") from error
        return raw if decimal.is_finite() else cls._raise_number(key)

    @staticmethod
    def _raise_number(key: str) -> str:
        raise _MalformedRecord(f"{key} must be finite")

    @staticmethod
    def _display_number(value: Any) -> str:
        if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float)):
            raise _MalformedRecord("line_score must have a displayable numeric value")
        return str(value)

    @staticmethod
    def _validate_timestamp(value: Any, field: str) -> None:
        if value is None:
            return
        if not isinstance(value, str):
            raise _MalformedRecord(f"{field} must be an ISO-8601 string")
        try:
            normalize_timestamp(value)
        except ValueError as error:
            raise _MalformedRecord(str(error)) from error

    @classmethod
    def _team_from_mapping(cls, attributes: Mapping[str, Any]) -> TeamEvidence | None:
        raw_team = attributes.get("team")
        team_id = attributes.get("team_id", attributes.get("teamId"))
        team_name = attributes.get("team_name", attributes.get("teamName"))
        abbreviation = attributes.get("team_abbreviation", attributes.get("teamAbbreviation"))
        if isinstance(raw_team, Mapping):
            team_id = raw_team.get("id", team_id)
            team_name = raw_team.get("name", team_name)
            abbreviation = raw_team.get("abbreviation", abbreviation)
        elif raw_team is not None:
            team_name = raw_team
        if team_id is None and team_name is None and abbreviation is None:
            return None
        if team_id is not None and (isinstance(team_id, bool) or not str(team_id).strip()):
            raise _MalformedRecord("team_id must be a non-empty identifier")
        if team_name is not None and not isinstance(team_name, str):
            raise _MalformedRecord("team name must be a string")
        if abbreviation is not None and not isinstance(abbreviation, str):
            raise _MalformedRecord("team abbreviation must be a string")
        try:
            return TeamEvidence(
                provider_id=str(team_id) if team_id is not None else None,
                name=team_name,
                abbreviation=abbreviation,
            )
        except ValueError as error:
            raise _MalformedRecord(str(error)) from error

    @classmethod
    def _positive_int(cls, value: Any, field: str) -> int:
        if isinstance(value, bool):
            raise _MalformedPage(f"{field} must be a positive integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as error:
            raise _MalformedPage(f"{field} must be a positive integer") from error
        if parsed < 1 or (isinstance(value, float) and value != parsed):
            raise _MalformedPage(f"{field} must be a positive integer")
        return parsed

    @classmethod
    def _optional_nonnegative_int(cls, value: Any, field: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise _MalformedPage(f"{field} must be a non-negative integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as error:
            raise _MalformedPage(f"{field} must be a non-negative integer") from error
        if parsed < 0 or (isinstance(value, float) and value != parsed):
            raise _MalformedPage(f"{field} must be a non-negative integer")
        return parsed

    @staticmethod
    def _invalid_response(detail: Any) -> ProviderUnavailableError:
        return ProviderUnavailableError(
            "PrizePicks returned an invalid response.", detail=detail
        )


__all__ = ["PrizePicksAdapter"]
