"""Underdog adapter for the shared NBA DFS snapshot contract."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

import requests

from app.errors import ProviderUnavailableError
from app.providers.dfs import (
    AthleteEvidence,
    AppearanceEvidence,
    CoverageCode,
    CoverageRecordExcluded,
    CoverageRecordMalformed,
    EventEvidence,
    MarketStatus,
    MarketThreshold,
    MalformedProviderResponseError,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    RetrievalContext,
    ScoringPeriod,
    Selection,
    SelectionModifier,
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
    PROVIDER_UNDERDOG,
    ProviderResponseError,
)

logger = logging.getLogger(__name__)


class _MalformedPayload(MalformedProviderResponseError):
    """The Underdog payload cannot be interpreted safely."""


class UnderdogAdapter:
    """Retrieve and normalize Underdog's public over/under board."""

    name = "underdog"
    BASE_URL = "https://api.underdogfantasy.com/beta/v3/over_under_lines"
    DEFAULT_TIMEOUT = (10.0, 30.0)

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
                "User-Agent": "StatsPlus/1.0",
            }
        )

    def get_snapshot(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext,
    ) -> ProviderSnapshot:
        """Fetch one complete or partial Underdog observation."""

        retrieved_at = self._now_utc()
        try:
            result = self._request_payload(
                context,
                expected_sport=query.sport,
                allowed_statuses=query.market_statuses,
            )
        except ProviderUnavailableError:
            raise
        except _MalformedPayload as error:
            raise self._invalid_response(error) from error

        warning_codes = list(result.warning_codes)
        skipped_reasons = list(result.skipped_reasons)
        diagnostic_details = list(result.diagnostic_details)
        skipped_count = result.skipped_count
        malformed_count = result.malformed_count
        markets = result.markets
        if malformed_count and not markets:
            raise self._invalid_response("no usable Underdog player markets")
        snapshot = _build_snapshot(
            provider=self.name,
            markets=markets,
            retrieved_at=retrieved_at,
            fetched_count=result.fetched_count,
            eligible_count=result.eligible_count,
            normalized_count=result.normalized_count,
            skipped_count=skipped_count,
            pagination_complete=True,
            fanout_complete=not malformed_count,
            expected_total=None,
            warning_codes=warning_codes,
            skipped_reasons=skipped_reasons,
            diagnostic_details=diagnostic_details,
        )
        return snapshot

    def _request_payload(
        self,
        context: RetrievalContext,
        *,
        expected_sport: str,
        allowed_statuses: tuple[MarketStatus | str, ...],
    ) -> _NormalizedBatch:
        try:
            return request_json(
                context=context,
                session=self.session,
                url=self.BASE_URL,
                params=None,
                timeout=self.timeout,
                now=self._now_utc,
                provider=PROVIDER_UNDERDOG,
                operation="get_snapshot",
                parse=lambda payload: self._normalize_payload(
                    payload,
                    expected_sport=expected_sport,
                    allowed_statuses=allowed_statuses,
                ),
                deadline_message="Underdog retrieval deadline exceeded.",
                timeout_message="Underdog timed out while fetching lines.",
                unavailable_message="Underdog could not be reached.",
                invalid_json_message="Underdog returned invalid JSON",
            )
        except ProviderResponseError as error:
            raise _MalformedPayload(str(error)) from error

    def _now_utc(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Underdog clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    @classmethod
    def _normalize_payload(
        cls,
        payload: Any,
        *,
        expected_sport: str,
        allowed_statuses: tuple[MarketStatus | str, ...],
    ) -> _NormalizedBatch:
        if not isinstance(payload, Mapping):
            raise _MalformedPayload("payload must be an object")
        rows = cls._required_list(payload, "over_under_lines")
        players = cls._index_resources(cls._required_list(payload, "players"), "players")
        appearances = cls._index_resources(
            cls._required_list(payload, "appearances"), "appearances"
        )
        games = cls._index_resources(cls._required_list(payload, "games"), "games")
        solo_games = cls._index_resources(
            cls._required_list(payload, "solo_games"), "solo_games"
        )
        for match_id, match in solo_games.items():
            previous = games.get(match_id)
            if previous is not None and previous != match:
                raise _MalformedPayload("game identity has conflicting content")
            games.setdefault(match_id, match)

        collection = _SnapshotMarketCollector()
        records = _RecordCoverageAccumulator()
        records.extend(
            rows,
            lambda value: cls._normalize_line(
                value,
                players=players,
                appearances=appearances,
                games=games,
                expected_sport=expected_sport,
                allowed_statuses=allowed_statuses,
            ),
            on_success=collection.add,
        )

        return _NormalizedBatch.from_accumulator(
            records,
            markets=collection.markets,
            warning_codes=collection.warning_codes,
        )

    @classmethod
    def _normalize_line(
        cls,
        row: Any,
        *,
        players: Mapping[str, Mapping[str, Any]],
        appearances: Mapping[str, Mapping[str, Any]],
        games: Mapping[str, Mapping[str, Any]],
        expected_sport: str,
        allowed_statuses: tuple[MarketStatus | str, ...],
    ) -> PlayerProjectionMarket:
        if not isinstance(row, Mapping):
            raise CoverageRecordMalformed("line must be an object")
        line_id = required_identifier(row, "id")
        over_under = row.get("over_under")
        if not isinstance(over_under, Mapping):
            raise CoverageRecordMalformed("line.over_under must be an object")
        appearance_stat = over_under.get("appearance_stat")
        if not isinstance(appearance_stat, Mapping):
            raise CoverageRecordMalformed("line appearance_stat must be an object")
        appearance_id = required_identifier(appearance_stat, "appearance_id")
        appearance = appearances.get(appearance_id)
        if appearance is None:
            raise CoverageRecordMalformed("line appearance could not be resolved")
        appearance_type = optional_text(appearance.get("type"))
        if appearance_type is None:
            raise CoverageRecordMalformed("appearance type must be present")
        if appearance_type.casefold() != "player":
            raise CoverageRecordExcluded(CoverageCode.NON_PLAYER_MARKET)
        appearance_label = (
            optional_text(appearance.get("label"))
            or optional_text(appearance.get("display_name"))
            or optional_text(appearance.get("name"))
        )
        appearance_evidence = AppearanceEvidence(
            provider_id=appearance_id,
            appearance_type=appearance_type,
            label=appearance_label,
        )

        player_id = required_identifier(appearance, "player_id")
        player = players.get(player_id)
        if player is None:
            raise CoverageRecordMalformed("line player could not be resolved")
        player_sport = required_text(player, "sport_id")
        if player_sport.casefold() != expected_sport.casefold():
            raise CoverageRecordExcluded(CoverageCode.NON_NBA_MARKET)
        raw_status = required_text(row, "status")
        try:
            status = normalize_market_status(raw_status).value
        except ValueError as error:
            raise CoverageRecordExcluded(CoverageCode.INELIGIBLE_STATUS) from error
        if status not in allowed_statuses:
            raise CoverageRecordExcluded(CoverageCode.STATUS_FILTER)

        match_id = cls._optional_identifier(appearance.get("match_id"))
        match = games.get(match_id) if match_id is not None else None
        match_type = optional_text(appearance.get("match_type"))
        if match_type is None:
            raise CoverageRecordMalformed(
                "missing_match_type",
                code=CoverageCode.MISSING_MATCH_TYPE,
            )
        normalized_match_type = match_type.casefold().replace("-", "_").replace(" ", "_")
        if normalized_match_type not in {"game", "solo_game"}:
            raise CoverageRecordExcluded(CoverageCode.NON_GAME_MARKET)
        if match_id is None or match is None:
            raise CoverageRecordMalformed(
                "missing_match_relationship",
                code=CoverageCode.MISSING_MATCH_RELATIONSHIP,
            )
        match_sport = required_text(match, "sport_id")
        if match_sport.casefold() != expected_sport.casefold():
            raise CoverageRecordExcluded(CoverageCode.NON_NBA_MARKET)

        options_value = row.get("options", [])
        if not isinstance(options_value, list):
            raise CoverageRecordMalformed("line.options must be a list")
        options = tuple(cls._normalize_option(option) for option in options_value)
        stat_value = required_number(row, "stat_value")
        stat_label = required_text(appearance_stat, "display_stat")
        player_name = cls._player_name(player)
        team = cls._team_from_mapping(player, fallback_id=appearance.get("team_id"))
        event = cls._event_from_match(match_id, match)
        variant_label = optional_text(row.get("line_type"))
        variant = normalize_market_variant(variant_label)
        updated_at = row.get("updated_at")
        validate_timestamp(updated_at, "updated_at")
        starts_at = event.starts_at if event is not None else None
        period_label = optional_text(
            appearance_stat.get("scoring_period", appearance_stat.get("period"))
        )
        scoring_period = (
            period_label if period_label is not None else ScoringPeriod.UNKNOWN
        )

        try:
            return PlayerProjectionMarket(
                provider=cls.name,
                market_id=line_id,
                athlete=AthleteEvidence(
                    provider_id=player_id,
                    name=player_name,
                    team=team,
                ),
                appearance=appearance_evidence,
                event=event,
                team=team,
                opponent=cls._opponent_from_match(match, team),
                sport=SportEvidence(label=player_sport),
                statistic=StatisticEvidence(label=stat_label),
                threshold=MarketThreshold(
                    stat_value,
                    unit="count",
                    original_value=display_number(row["stat_value"], field="stat_value"),
                ),
                status=status,
                status_label=raw_status,
                variant=variant.value,
                variant_label=variant_label,
                scoring_period=scoring_period,
                scoring_period_label=period_label,
                starts_at=starts_at,
                updated_at=updated_at,
                selections=options,
            )
        except ValueError as error:
            raise CoverageRecordMalformed(str(error)) from error

    @classmethod
    def _normalize_option(cls, option: Any) -> Selection:
        if not isinstance(option, Mapping):
            raise CoverageRecordMalformed("line option must be an object")
        choice = optional_text(option.get("choice"))
        selection_id = cls._optional_identifier(
            option.get("id", option.get("selection_id"))
        )
        modifiers: tuple[SelectionModifier, ...] = ()
        multiplier = option.get("payout_multiplier")
        if multiplier is not None and multiplier != "":
            modifier_kind = optional_text(option.get("modifier_kind")) or "payout_multiplier"
            modifier_scope = optional_text(option.get("modifier_scope")) or "selection"
            modifier_label = optional_text(option.get("payout_multiplier_label"))
            try:
                modifiers = (
                    SelectionModifier(
                        value=multiplier,
                        kind=modifier_kind,
                        scope=modifier_scope,
                        label=modifier_label,
                    ),
                )
            except ValueError as error:
                raise CoverageRecordMalformed(str(error)) from error
        try:
            return Selection(
                selection_id=selection_id,
                label=choice,
                direction=choice,
                direction_label=choice,
                status=optional_text(option.get("status")),
                modifiers=modifiers,
                american_price=option.get("american_price"),
                decimal_price=option.get("decimal_price"),
            )
        except ValueError as error:
            raise CoverageRecordMalformed(str(error)) from error

    @classmethod
    def _event_from_match(
        cls,
        match_id: str | None,
        match: Mapping[str, Any] | None,
    ) -> EventEvidence | None:
        if match_id is None and match is None:
            return None
        label = optional_text(match.get("title")) if match else None
        if label is None and match is not None:
            label = optional_text(match.get("name"))
        starts_at = match.get("scheduled_at") if match else None
        updated_at = match.get("updated_at") if match else None
        status_label = (
            optional_text(match.get("status"))
            or optional_text(match.get("status_label"))
            if match
            else None
        )
        if status_label is not None and is_ineligible_event_status(status_label):
            raise CoverageRecordExcluded(CoverageCode.INELIGIBLE_EVENT_STATUS)
        validate_timestamp(starts_at, "scheduled_at")
        validate_timestamp(updated_at, "match updated_at")
        try:
            return EventEvidence(
                provider_id=match_id,
                label=label,
                status_label=status_label,
                starts_at=starts_at,
                updated_at=updated_at,
                home_team=cls._team_from_mapping(match, prefix="home_team") if match else None,
                away_team=cls._team_from_mapping(match, prefix="away_team") if match else None,
            )
        except ValueError as error:
            raise CoverageRecordMalformed(str(error)) from error

    @classmethod
    def _opponent_from_match(
        cls,
        match: Mapping[str, Any] | None,
        player_team: TeamEvidence | None,
    ) -> TeamEvidence | None:
        if match is None:
            return None
        home = cls._team_from_mapping(match, prefix="home_team")
        away = cls._team_from_mapping(match, prefix="away_team")
        if player_team is not None and player_team.provider_id is not None:
            if home is not None and home.provider_id == player_team.provider_id:
                return away
            if away is not None and away.provider_id == player_team.provider_id:
                return home
        return None

    @classmethod
    def _team_from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        fallback_id: Any = None,
        prefix: str | None = None,
    ) -> TeamEvidence | None:
        if value is None:
            return None
        if prefix is None:
            team_id = value.get("team_id", fallback_id)
            name = value.get("team_name", value.get("team"))
            abbreviation = value.get("team_abbreviation", value.get("teamAbbreviation"))
        else:
            team_id = value.get(f"{prefix}_id")
            name = value.get(f"{prefix}_name")
            abbreviation = value.get(f"{prefix}_abbreviation")
        if team_id is None and name is None and abbreviation is None:
            return None
        if team_id is not None and (isinstance(team_id, bool) or not str(team_id).strip()):
            raise CoverageRecordMalformed("team id must be a non-empty identifier")
        if name is not None and not isinstance(name, str):
            raise CoverageRecordMalformed("team name must be a string")
        if abbreviation is not None and not isinstance(abbreviation, str):
            raise CoverageRecordMalformed("team abbreviation must be a string")
        try:
            return TeamEvidence(
                provider_id=str(team_id) if team_id is not None else None,
                name=name,
                abbreviation=abbreviation,
            )
        except ValueError as error:
            raise CoverageRecordMalformed(str(error)) from error

    @classmethod
    def _player_name(cls, player: Mapping[str, Any]) -> str:
        first = optional_text(player.get("first_name"))
        last = optional_text(player.get("last_name"))
        name = " ".join(part for part in (first, last) if part)
        if not name:
            name = optional_text(player.get("name")) or optional_text(player.get("display_name"))
        if not name:
            raise CoverageRecordMalformed("player name must be present")
        return name

    @classmethod
    def _index_resources(
        cls,
        resources: list[Any],
        name: str,
    ) -> dict[str, Mapping[str, Any]]:
        indexed: dict[str, Mapping[str, Any]] = {}
        for resource in resources:
            if not isinstance(resource, Mapping):
                raise _MalformedPayload(f"{name} entries must be objects")
            try:
                identifier = required_identifier(resource, "id")
            except CoverageRecordMalformed as error:
                raise _MalformedPayload(str(error)) from error
            previous = indexed.get(identifier)
            if previous is not None and previous != resource:
                raise _MalformedPayload(f"{name} identity has conflicting content")
            indexed[identifier] = resource
        return indexed

    @classmethod
    def _required_list(cls, payload: Mapping[str, Any], key: str) -> list[Any]:
        value = payload.get(key)
        if not isinstance(value, list):
            raise _MalformedPayload(f"{key} must be a list")
        return value

    @staticmethod
    def _optional_identifier(value: Any) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise CoverageRecordMalformed("identifier must be a string, integer, or None")
        identifier = str(value).strip()
        return identifier or None

    @staticmethod
    def _invalid_response(detail: Any) -> ProviderUnavailableError:
        return ProviderUnavailableError(
            "Underdog returned an invalid response.", detail=detail
        )


__all__ = ["UnderdogAdapter"]
