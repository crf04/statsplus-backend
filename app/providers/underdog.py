"""Underdog adapter for the shared NBA DFS snapshot contract."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from app.errors import ProviderUnavailableError
from app.providers.dfs import (
    AthleteEvidence,
    AppearanceEvidence,
    CoverageEvidence,
    DeadlineExceededError,
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
    SnapshotStatus,
    SportEvidence,
    StatisticEvidence,
    TeamEvidence,
    normalize_market_variant,
    normalize_timestamp,
)
from app.utils.telemetry import (
    CACHE_DISABLED,
    PROVIDER_UNDERDOG,
    ProviderResponseError,
    provider_call,
)

logger = logging.getLogger(__name__)


class _MalformedPayload(MalformedProviderResponseError):
    """The Underdog payload cannot be interpreted safely."""


class _MalformedRecord(MalformedProviderResponseError):
    """One Underdog record cannot be represented safely."""


class _ExcludedRecord(Exception):
    """One valid upstream record is outside the requested board scope."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class _PayloadResult:
    markets: tuple[PlayerProjectionMarket, ...]
    fetched_count: int
    eligible_count: int
    normalized_count: int
    skipped_count: int
    warning_codes: tuple[str, ...]
    skipped_reasons: tuple[str, ...]
    malformed_count: int


class UnderdogAdapter:
    """Retrieve and normalize Underdog's public over/under board."""

    name = "underdog"
    PROVIDER_NAME = name
    BASE_URL = "https://api.underdogfantasy.com/beta/v3/over_under_lines"
    DEFAULT_TIMEOUT = (10.0, 30.0)

    def __init__(
        self,
        *,
        session: requests.Session | Any | None = None,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
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

        retrieved_at = datetime.now(timezone.utc)
        try:
            payload = self._request_payload(context)
            result = self._normalize_payload(
                payload,
                expected_sport=query.sport,
                allowed_statuses=query.market_statuses,
            )
        except ProviderUnavailableError:
            raise
        except _MalformedPayload as error:
            raise self._invalid_response(error) from error

        markets: list[PlayerProjectionMarket] = []
        seen: dict[tuple[str, str], PlayerProjectionMarket] = {}
        conflicting: set[tuple[str, str]] = set()
        warning_codes = list(result.warning_codes)
        skipped_reasons = list(result.skipped_reasons)
        skipped_count = result.skipped_count
        malformed_count = result.malformed_count
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
                warning_codes.append("conflicting_source_identity")
                skipped_reasons.append("conflicting_source_identity")

        if conflicting:
            markets = [market for market in markets if market.source_identity not in conflicting]
        coverage = CoverageEvidence(
            fetched_count=result.fetched_count,
            eligible_count=result.eligible_count,
            normalized_count=result.normalized_count,
            skipped_count=skipped_count,
            pagination_complete=True,
            fanout_complete=not malformed_count,
            expected_total=None,
            warning_codes=tuple(dict.fromkeys(warning_codes)),
            skipped_reasons=tuple(dict.fromkeys(skipped_reasons)),
        )
        if malformed_count and not markets:
            raise self._invalid_response("no usable Underdog player markets")
        status = SnapshotStatus.PARTIAL if not coverage.is_complete else SnapshotStatus.COMPLETE
        return ProviderSnapshot(
            provider=self.name,
            status=status,
            markets=tuple(markets),
            coverage=coverage,
            retrieved_at=retrieved_at,
        )

    def _request_payload(self, context: RetrievalContext) -> Any:
        try:
            context.ensure_active()
            timeout = self._bounded_timeout(context)
            with provider_call(
                PROVIDER_UNDERDOG,
                "get_snapshot",
                cache_status=CACHE_DISABLED,
                request_id=context.request_id,
            ) as tracker:
                response = self.session.get(self.BASE_URL, timeout=timeout)
                tracker.status_code = getattr(response, "status_code", None)
                response.raise_for_status()
                try:
                    return response.json()
                except (TypeError, ValueError) as error:
                    raise ProviderResponseError(
                        "Underdog returned invalid JSON"
                    ) from error
        except DeadlineExceededError as error:
            raise ProviderUnavailableError(
                "Underdog retrieval deadline exceeded.", detail=error
            ) from error
        except requests.exceptions.Timeout as error:
            raise ProviderUnavailableError(
                "Underdog timed out while fetching lines.", detail=error
            ) from error
        except requests.exceptions.RequestException as error:
            raise ProviderUnavailableError(
                "Underdog could not be reached.", detail=error
            ) from error
        except ProviderResponseError as error:
            raise ProviderUnavailableError(
                "Underdog returned an invalid response.", detail=error
            ) from error

    def _bounded_timeout(self, context: RetrievalContext) -> tuple[float, float]:
        remaining = context.remaining_seconds()
        if remaining <= 0:
            raise DeadlineExceededError("Underdog retrieval deadline exceeded")
        connect, read = self.timeout
        return min(float(connect), remaining), min(float(read), remaining)

    @classmethod
    def _normalize_payload(
        cls,
        payload: Any,
        *,
        expected_sport: str,
        allowed_statuses: tuple[MarketStatus | str, ...],
    ) -> _PayloadResult:
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

        markets: list[PlayerProjectionMarket] = []
        warning_codes: list[str] = []
        skipped_reasons: list[str] = []
        skipped_count = 0
        malformed_count = 0
        eligible_count = 0
        normalized_count = 0
        for row in rows:
            try:
                market = cls._normalize_line(
                    row,
                    players=players,
                    appearances=appearances,
                    games=games,
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

        return _PayloadResult(
            markets=tuple(markets),
            fetched_count=len(rows),
            eligible_count=eligible_count,
            normalized_count=normalized_count,
            skipped_count=skipped_count,
            warning_codes=tuple(dict.fromkeys(warning_codes)),
            skipped_reasons=tuple(dict.fromkeys(skipped_reasons)),
            malformed_count=malformed_count,
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
            raise _MalformedRecord("line must be an object")
        line_id = cls._required_identifier(row, "id")
        over_under = row.get("over_under")
        if not isinstance(over_under, Mapping):
            raise _MalformedRecord("line.over_under must be an object")
        appearance_stat = over_under.get("appearance_stat")
        if not isinstance(appearance_stat, Mapping):
            raise _MalformedRecord("line appearance_stat must be an object")
        appearance_id = cls._required_identifier(appearance_stat, "appearance_id")
        appearance = appearances.get(appearance_id)
        if appearance is None:
            raise _MalformedRecord("line appearance could not be resolved")
        appearance_type = cls._optional_text(appearance.get("type"))
        if appearance_type is None:
            raise _MalformedRecord("appearance type must be present")
        if appearance_type.casefold() != "player":
            raise _ExcludedRecord("non_player_market")
        appearance_label = (
            cls._optional_text(appearance.get("label"))
            or cls._optional_text(appearance.get("display_name"))
            or cls._optional_text(appearance.get("name"))
        )
        appearance_evidence = AppearanceEvidence(
            provider_id=appearance_id,
            appearance_type=appearance_type,
            label=appearance_label,
        )

        player_id = cls._required_identifier(appearance, "player_id")
        player = players.get(player_id)
        if player is None:
            raise _MalformedRecord("line player could not be resolved")
        player_sport = cls._required_text(player, "sport_id")
        if player_sport.casefold() != expected_sport.casefold():
            raise _ExcludedRecord("non_nba_market")
        raw_status = cls._required_text(row, "status")
        status = cls._normalize_status(raw_status)
        if status is None:
            raise _ExcludedRecord("ineligible_status")
        if status not in allowed_statuses:
            raise _ExcludedRecord("status_filter")

        match_id = cls._optional_identifier(appearance.get("match_id"))
        match = games.get(match_id) if match_id is not None else None
        match_type = cls._optional_text(appearance.get("match_type"))
        if match_type is not None and match_type.casefold() not in {"game", "solo_game"}:
            raise _ExcludedRecord("non_game_market")
        if match is not None:
            match_sport = cls._optional_text(match.get("sport_id"))
            if match_sport is not None and match_sport.casefold() != expected_sport.casefold():
                raise _ExcludedRecord("non_nba_market")

        options_value = row.get("options", [])
        if not isinstance(options_value, list):
            raise _MalformedRecord("line.options must be a list")
        options = tuple(cls._normalize_option(option) for option in options_value)
        stat_value = cls._required_number(row, "stat_value")
        stat_label = cls._required_text(appearance_stat, "display_stat")
        player_name = cls._player_name(player)
        team = cls._team_from_mapping(player, fallback_id=appearance.get("team_id"))
        event = cls._event_from_match(match_id, match)
        variant_label = cls._optional_text(row.get("line_type"))
        variant = normalize_market_variant(variant_label)
        updated_at = row.get("updated_at")
        cls._validate_timestamp(updated_at, "updated_at")
        starts_at = event.starts_at if event is not None else None
        period_label = cls._optional_text(
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
                    original_value=cls._display_number(row["stat_value"]),
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
            raise _MalformedRecord(str(error)) from error

    @classmethod
    def _normalize_option(cls, option: Any) -> Selection:
        if not isinstance(option, Mapping):
            raise _MalformedRecord("line option must be an object")
        choice = cls._optional_text(option.get("choice"))
        selection_id = cls._optional_identifier(
            option.get("id", option.get("selection_id"))
        )
        modifiers: tuple[SelectionModifier, ...] = ()
        multiplier = option.get("payout_multiplier")
        if multiplier is not None and multiplier != "":
            modifier_kind = cls._optional_text(option.get("modifier_kind")) or "payout_multiplier"
            modifier_scope = cls._optional_text(option.get("modifier_scope")) or "selection"
            modifier_label = cls._optional_text(option.get("payout_multiplier_label")) or str(multiplier)
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
                raise _MalformedRecord(str(error)) from error
        try:
            return Selection(
                selection_id=selection_id,
                label=choice,
                direction=choice,
                direction_label=choice,
                status=cls._optional_text(option.get("status")),
                modifiers=modifiers,
                american_price=option.get("american_price"),
                decimal_price=option.get("decimal_price"),
            )
        except ValueError as error:
            raise _MalformedRecord(str(error)) from error

    @classmethod
    def _event_from_match(
        cls,
        match_id: str | None,
        match: Mapping[str, Any] | None,
    ) -> EventEvidence | None:
        if match_id is None and match is None:
            return None
        label = cls._optional_text(match.get("title")) if match else None
        if label is None and match is not None:
            label = cls._optional_text(match.get("name"))
        starts_at = match.get("scheduled_at") if match else None
        updated_at = match.get("updated_at") if match else None
        status_label = (
            cls._optional_text(match.get("status"))
            or cls._optional_text(match.get("status_label"))
            if match
            else None
        )
        if status_label is not None and cls._is_ineligible_event_status(status_label):
            raise _ExcludedRecord("ineligible_event_status")
        cls._validate_timestamp(starts_at, "scheduled_at")
        cls._validate_timestamp(updated_at, "match updated_at")
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
            raise _MalformedRecord(str(error)) from error

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
            raise _MalformedRecord("team id must be a non-empty identifier")
        if name is not None and not isinstance(name, str):
            raise _MalformedRecord("team name must be a string")
        if abbreviation is not None and not isinstance(abbreviation, str):
            raise _MalformedRecord("team abbreviation must be a string")
        try:
            return TeamEvidence(
                provider_id=str(team_id) if team_id is not None else None,
                name=name,
                abbreviation=abbreviation,
            )
        except ValueError as error:
            raise _MalformedRecord(str(error)) from error

    @classmethod
    def _player_name(cls, player: Mapping[str, Any]) -> str:
        first = cls._optional_text(player.get("first_name"))
        last = cls._optional_text(player.get("last_name"))
        name = " ".join(part for part in (first, last) if part)
        if not name:
            name = cls._optional_text(player.get("name")) or cls._optional_text(player.get("display_name"))
        if not name:
            raise _MalformedRecord("player name must be present")
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
                identifier = cls._required_identifier(resource, "id")
            except _MalformedRecord as error:
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

    @classmethod
    def _required_identifier(cls, value: Mapping[str, Any], key: str) -> str:
        identifier = value.get(key)
        if identifier is None or isinstance(identifier, bool) or not str(identifier).strip():
            raise _MalformedRecord(f"{key} must be present")
        return str(identifier)

    @staticmethod
    def _optional_identifier(value: Any) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise _MalformedRecord("identifier must be a string, integer, or None")
        identifier = str(value).strip()
        return identifier or None

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
        if not decimal.is_finite():
            raise _MalformedRecord(f"{key} must be finite")
        return raw

    @staticmethod
    def _display_number(value: Any) -> str:
        if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float)):
            raise _MalformedRecord("stat_value must have a displayable numeric value")
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

    @staticmethod
    def _normalize_status(label: str) -> MarketStatus | None:
        normalized = label.strip().casefold().replace("-", "_")
        if normalized in {"active", "open", "available", "pre_game", "pregame"}:
            return MarketStatus.AVAILABLE
        if normalized in {"suspended", "paused"}:
            return MarketStatus.SUSPENDED
        return None

    @staticmethod
    def _is_ineligible_event_status(label: str) -> bool:
        normalized = label.strip().casefold().replace("-", "_").replace(" ", "_")
        return normalized in {"live", "closed", "settled", "final", "in_play", "inplay"}

    @staticmethod
    def _invalid_response(detail: Any) -> ProviderUnavailableError:
        return ProviderUnavailableError(
            "Underdog returned an invalid response.", detail=detail
        )


__all__ = ["UnderdogAdapter"]
