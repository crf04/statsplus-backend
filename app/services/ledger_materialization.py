"""Inactive materialization services for ledger-derived publication streams."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone

from app.services.canonical_game_ledger import (
    CanonicalGame,
    CanonicalGameLedgerRepository,
    LedgerPublicationRecord,
    validate_canonical_season,
)
from app.services.ledger_derivations import (
    AssistLocationFact,
    PlayerPer36Fact,
    TeamWindowMaterialization,
    TraditionalOpponentFact,
    derive_assist_location_facts,
    derive_player_per36_facts,
    derive_traditional_opponent_facts,
    materialize_team_window,
)


class LedgerMaterializationUnavailable(ValueError):
    """A derived stream cannot be published from complete governed facts."""


@dataclass(frozen=True, slots=True)
class LedgerMaterialization:
    season: str
    as_of: date
    traditional_opponent: tuple[TraditionalOpponentFact, ...]
    assist_locations: tuple[AssistLocationFact, ...]
    player_per36: tuple[PlayerPer36Fact, ...]
    season_window: TeamWindowMaterialization
    l15_window: TeamWindowMaterialization


class LedgerMaterializationService:
    """Compose ledger-derived streams and record inactive publication metadata."""

    def __init__(self, repository: CanonicalGameLedgerRepository, *, clock=None) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def compose(
        self,
        games: Iterable[CanonicalGame],
        *,
        season: str,
        as_of: date,
        expected_game_ids: frozenset[str] | None = None,
        team_ids: frozenset[int] | None = None,
        require_assist_locations: bool = False,
    ) -> LedgerMaterialization:
        canonical_season = validate_canonical_season(season)
        supplied = tuple(games)
        eligible = tuple(
            game for game in supplied
            if game.season == canonical_season and game.game_date <= as_of
        )
        season_window = materialize_team_window(
            supplied,
            season=canonical_season,
            as_of=as_of,
            expected_game_ids=expected_game_ids,
            team_ids=team_ids,
        )
        l15_window = materialize_team_window(
            supplied,
            season=canonical_season,
            as_of=as_of,
            window_games=15,
            team_ids=team_ids,
        )
        if not season_window.complete:
            raise LedgerMaterializationUnavailable(season_window.reason or "Season ledger is incomplete")
        if not l15_window.complete:
            raise LedgerMaterializationUnavailable(l15_window.reason or "L15 ledger is incomplete")
        traditional = derive_traditional_opponent_facts(eligible)
        assist_status = "complete"
        assist_reason = None
        try:
            assists = derive_assist_location_facts(eligible)
        except ValueError as error:
            if require_assist_locations:
                raise LedgerMaterializationUnavailable(str(error)) from error
            assists = ()
            assist_status = "unavailable"
            assist_reason = "assist-location evidence is incomplete"
        per36 = derive_player_per36_facts(eligible, season=canonical_season, cutoff=as_of)
        result = LedgerMaterialization(
            season=canonical_season,
            as_of=as_of,
            traditional_opponent=traditional,
            assist_locations=assists,
            player_per36=per36,
            season_window=season_window,
            l15_window=l15_window,
        )
        retrieved_at = self.clock()
        publications = tuple(
            LedgerPublicationRecord(
                stream_key=stream_key,
                season=canonical_season,
                window_kind=window_kind,
                window_games=window_games,
                as_of=as_of,
                status=status,
                checksum=_payload_checksum(payload),
                game_count=len(season_window.governed_game_ids),
                team_count=len(season_window.teams),
                retrieved_at=retrieved_at,
                reason=reason,
            )
            for stream_key, payload, window_kind, window_games, status, reason in (
                ("traditional_opponent", traditional, "season", 0, "complete", None),
                ("assist_locations", assists, "season", 0, assist_status, assist_reason),
                ("player_per36", per36, "season", 0, "complete", None),
                ("team_matchups_season", season_window.teams, "season", 0, "complete", None),
                ("team_matchups_l15", l15_window.teams, "rolling_games", 15, "complete", None),
            )
        )
        self.repository.publish_metadata_batch(publications)
        return result


def _payload_checksum(payload: object) -> str:
    encoded = json.dumps(payload, default=_json_default, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


# Friendly aliases for callers that describe this seam as composition rather
# than materialization.
LedgerCompositionService = LedgerMaterializationService


__all__ = [
    "LedgerCompositionService",
    "LedgerMaterialization",
    "LedgerMaterializationService",
    "LedgerMaterializationUnavailable",
]
