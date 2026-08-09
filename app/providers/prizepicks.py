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
    CoverageCode,
    CoverageRecordExcluded,
    CoverageRecordMalformed,
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
    _SnapshotMarketCollector,
    _RecordCoverageAccumulator,
    SportEvidence,
    StatisticEvidence,
    TeamEvidence,
    _NormalizedBatch,
    _build_snapshot,
    normalize_market_variant,
    normalize_market_status,
)
from app.providers.dfs_transport import request_json
from app.providers.dfs_normalization import (
    display_number,
    is_ineligible_event_status,
    optional_text,
    required_identifier,
    required_number,
    required_text,
    validate_timestamp,
)
from app.utils.telemetry import (
    PROVIDER_PRIZEPICKS,
    ProviderResponseError,
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


@dataclass(frozen=True, slots=True)
class _PageResult:
    batch: _NormalizedBatch
    current_page: int
    total_pages: int
    expected_total: int | None


class PrizePicksAdapter:
    """Retrieve and normalize PrizePicks' JSON:API projection board."""

    name = "prizepicks"
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
        collection = _SnapshotMarketCollector()
        coverage = _RecordCoverageAccumulator()
        warning_codes: list[CoverageCode] = []
        skipped_reasons: list[CoverageCode] = []
        diagnostic_details: list[str] = []
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
                if self._is_deadline_error(error):
                    raise
                if not collection.markets:
                    raise
                pagination_complete = False
                fanout_complete = False
                warning_codes.append(CoverageCode.PAGE_FETCH_FAILED)
                logger.warning("PrizePicks page %s failed after usable data: %s", page, error)
                break
            except _MalformedPage as error:
                if not collection.markets:
                    raise self._invalid_response(error) from error
                pagination_complete = False
                fanout_complete = False
                warning_codes.append(CoverageCode.PAGE_MALFORMED)
                if "meta." in str(error):
                    warning_codes.append(CoverageCode.PAGE_METADATA_MISMATCH)
                    skipped_reasons.append(CoverageCode.PAGINATION_METADATA_MALFORMED)
                    diagnostic_details.append(str(error))
                logger.warning("PrizePicks page %s was malformed after usable data", page)
                break

            if result.current_page != page:
                if not collection.markets:
                    raise self._invalid_response(
                        f"requested page {page}, received page {result.current_page}"
                    )
                pagination_complete = False
                fanout_complete = False
                warning_codes.append(CoverageCode.PAGE_METADATA_MISMATCH)
                skipped_reasons.append(CoverageCode.PAGINATION_PAGE_MISMATCH)
                break
            if result.total_pages < total_pages:
                if not collection.markets:
                    raise self._invalid_response("pagination total_pages shrank")
                pagination_complete = False
                fanout_complete = False
                warning_codes.append(CoverageCode.PAGE_METADATA_MISMATCH)
                skipped_reasons.append(CoverageCode.PAGINATION_TOTAL_PAGES_CHANGED)
                break
            total_pages = max(total_pages, result.total_pages)
            if not expected_total_seen:
                expected_total = result.expected_total
                expected_total_seen = True
            elif result.expected_total != expected_total:
                if not collection.markets:
                    raise self._invalid_response("pagination expected total changed")
                pagination_complete = False
                fanout_complete = False
                warning_codes.append(CoverageCode.PAGE_METADATA_MISMATCH)
                skipped_reasons.append(CoverageCode.PAGINATION_EXPECTED_TOTAL_CHANGED)
                break
            batch = result.batch
            coverage.absorb(batch, on_success=collection.add)
            if coverage.malformed_count:
                fanout_complete = False

            page += 1

        markets = collection.markets
        malformed_count = coverage.malformed_count
        warning_codes.extend(coverage.warning_values())
        skipped_reasons.extend(coverage.skipped_values())
        diagnostic_details.extend(coverage.diagnostic_values())
        warning_codes.extend(collection.warning_codes)
        if malformed_count:
            fanout_complete = False

        if malformed_count and not markets:
            raise self._invalid_response("no usable PrizePicks projection records")
        if (
            not markets
            and expected_total is not None
            and coverage.fetched_count < expected_total
        ):
            raise self._invalid_response(
                "pagination ended before the expected PrizePicks records were fetched"
            )
        snapshot = _build_snapshot(
            provider=self.name,
            markets=markets,
            retrieved_at=retrieved_at,
            fetched_count=coverage.fetched_count,
            eligible_count=coverage.eligible_count,
            normalized_count=coverage.normalized_count,
            skipped_count=coverage.skipped_count,
            pagination_complete=pagination_complete,
            fanout_complete=fanout_complete,
            expected_total=expected_total,
            warning_codes=warning_codes,
            skipped_reasons=skipped_reasons,
            diagnostic_details=diagnostic_details,
        )
        return snapshot

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
            return request_json(
                context=context,
                session=self.session,
                url=self.BASE_URL,
                params={
                    "league_id": league_id,
                    "page": page,
                    "per_page": 250,
                    "single_stat": "true",
                },
                timeout=self.timeout,
                now=self._now_utc,
                provider=PROVIDER_PRIZEPICKS,
                operation="get_snapshot",
                parse=lambda payload: self._parse_page(
                    payload,
                    expected_sport=expected_sport,
                    allowed_statuses=allowed_statuses,
                ),
                deadline_message="PrizePicks retrieval deadline exceeded.",
                timeout_message="PrizePicks timed out while fetching lines.",
                unavailable_message="PrizePicks could not be reached.",
                invalid_json_message="PrizePicks returned invalid JSON",
            )
        except ProviderResponseError as error:
            raise _MalformedPage(str(error)) from error

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

        records = _RecordCoverageAccumulator()
        records.extend(
            rows,
            lambda value: cls._normalize_projection(
                value,
                resources=resources,
                expected_sport=expected_sport,
                allowed_statuses=allowed_statuses,
            ),
        )

        return _PageResult(
            batch=_NormalizedBatch.from_accumulator(records),
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
            raise CoverageRecordMalformed("projection must be an object")
        if row.get("type") != "projection":
            raise CoverageRecordExcluded(CoverageCode.NON_PROJECTION_MARKET)
        attributes = row.get("attributes")
        relationships = row.get("relationships")
        if not isinstance(attributes, Mapping) or not isinstance(relationships, Mapping):
            raise CoverageRecordMalformed("projection attributes and relationships must be objects")
        market_kind = cls._market_kind(attributes)
        future_relation = cls._future_relationship(relationships)
        if market_kind in {"non_player", "future"} or future_relation is not None:
            raise CoverageRecordExcluded(CoverageCode.NON_PLAYER_MARKET)
        projection_id = required_identifier(row, "id")
        player_id = cls._relationship_id(relationships, "new_player")
        league_id = cls._relationship_id(relationships, "league")
        player = resources.get(("new_player", player_id))
        league = resources.get(("league", league_id))
        if player is None or league is None:
            raise CoverageRecordMalformed("projection relationships could not be resolved")
        player_attributes = player.get("attributes")
        league_attributes = league.get("attributes")
        if not isinstance(player_attributes, Mapping) or not isinstance(league_attributes, Mapping):
            raise CoverageRecordMalformed("related resource attributes must be objects")

        league_name = required_text(league_attributes, "name")
        if league_name.casefold() != expected_sport.casefold():
            raise CoverageRecordExcluded(CoverageCode.NON_NBA_MARKET)
        raw_status = required_text(attributes, "status")
        try:
            status = normalize_market_status(raw_status).value
        except ValueError as error:
            raise CoverageRecordExcluded(CoverageCode.INELIGIBLE_STATUS) from error
        if status not in allowed_statuses:
            raise CoverageRecordExcluded(CoverageCode.STATUS_FILTER)

        player_name = required_text(player_attributes, "name")
        stat_label = required_text(attributes, "stat_type")
        line_score = required_number(attributes, "line_score")
        start_value = attributes.get("start_time")
        updated_value = attributes.get("updated_at")
        validate_timestamp(start_value, "start_time")
        validate_timestamp(updated_value, "updated_at")

        team = cls._team_from_mapping(player_attributes)
        sport = SportEvidence(label=league_name)
        competition = CompetitionEvidence(
            provider_id=league_id,
            label=league_name,
            sport=sport,
        )
        event = cls._event_from_projection(attributes, relationships, resources)
        variant_label = optional_text(attributes.get("odds_type"))
        variant = normalize_market_variant(variant_label)
        period_label = optional_text(
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
                    original_value=display_number(attributes["line_score"], field="line_score"),
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
            raise CoverageRecordMalformed(str(error)) from error

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
            raise CoverageRecordMalformed("event resource attributes must be an object")
        label = optional_text(
            event_attributes.get("name", event_attributes.get("description"))
        ) or optional_text(attributes.get("description"))
        starts_at = event_attributes.get("start_time", attributes.get("start_time"))
        updated_at = event_attributes.get("updated_at", attributes.get("updated_at"))
        status_label = optional_text(event_attributes.get("status")) or optional_text(
            event_attributes.get("status_label")
        )
        if status_label is not None and is_ineligible_event_status(status_label):
            raise CoverageRecordExcluded(CoverageCode.INELIGIBLE_EVENT_STATUS)
        validate_timestamp(starts_at, "event start_time")
        validate_timestamp(updated_at, "event updated_at")
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
            raise CoverageRecordMalformed(str(error)) from error

    @classmethod
    def _event_relationship(
        cls,
        relationships: Mapping[str, Any],
    ) -> tuple[str, str] | None:
        for name in ("event", "game", "fixture", "match"):
            if name in relationships:
                return cls._optional_relationship(relationships, name)
        return None

    @classmethod
    def _future_relationship(
        cls,
        relationships: Mapping[str, Any],
    ) -> tuple[str, str] | None:
        """Recognize a linked future before resolving player/event resources."""

        relation_value: tuple[str, str] | None = None
        for name in ("future", "futures"):
            if name not in relationships:
                continue
            relation = cls._optional_relationship(relationships, name)
            if relation is None:
                continue
            relation_type, _ = relation
            if relation_type.casefold() not in _FUTURES_MARKET_KINDS:
                raise CoverageRecordMalformed(
                    f"{name} relationship has an unexpected type"
                )
            if relation_value is None:
                relation_value = relation
        return relation_value

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
                resource_type = required_text(resource, "type")
                resource_id = required_identifier(resource, "id")
            except ValueError as error:
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
            raise CoverageRecordMalformed(f"missing {name} relationship")
        expected_type, identifier = relation
        if name == "new_player" and expected_type != "new_player":
            raise CoverageRecordMalformed(f"{name} relationship has an unexpected type")
        if name == "league" and expected_type != "league":
            raise CoverageRecordMalformed(f"{name} relationship has an unexpected type")
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
            raise CoverageRecordMalformed(f"{name} relationship must be an object")
        data = relationship.get("data")
        if data is None:
            return None
        if not isinstance(data, Mapping):
            raise CoverageRecordMalformed(f"{name} relationship data must be an object")
        return required_text(data, "type"), required_identifier(data, "id")

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
            raise CoverageRecordMalformed("team_id must be a non-empty identifier")
        if team_name is not None and not isinstance(team_name, str):
            raise CoverageRecordMalformed("team name must be a string")
        if abbreviation is not None and not isinstance(abbreviation, str):
            raise CoverageRecordMalformed("team abbreviation must be a string")
        try:
            return TeamEvidence(
                provider_id=str(team_id) if team_id is not None else None,
                name=team_name,
                abbreviation=abbreviation,
            )
        except ValueError as error:
            raise CoverageRecordMalformed(str(error)) from error

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

    @staticmethod
    def _is_deadline_error(error: ProviderUnavailableError) -> bool:
        cause = error.__cause__
        while cause is not None:
            if isinstance(cause, DeadlineExceededError):
                return True
            cause = cause.__cause__
        return False


__all__ = ["PrizePicksAdapter"]
