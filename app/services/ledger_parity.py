"""Semantic parity evidence between PBP-derived and legacy provider facts."""

from __future__ import annotations

import math
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.canonical_game_ledger import LedgerParityArtifact
from app.models.collection_control import AuditEvent

from app.services.canonical_game_ledger import CanonicalGame, PlayerGameFact, validate_canonical_season
from app.services.ledger_derivations import (
    PlayerPer36Fact,
    TraditionalOpponentFact,
    derive_player_per36_facts,
    derive_traditional_opponent_facts,
)


@dataclass(frozen=True, slots=True)
class SemanticDifference:
    identity: str
    field: str
    pbp_value: object
    legacy_value: object
    classification: str


@dataclass(frozen=True, slots=True)
class LedgerParityReport:
    season: str
    game_count: int
    compared_count: int
    differences: tuple[SemanticDifference, ...]
    adjudication_required: bool

    @property
    def exact(self) -> bool:
        """Whether at least one identity was compared with no differences."""

        return self.compared_count > 0 and not self.differences

    @property
    def status(self) -> str:
        """Durable, human-readable adjudication status for artifact writers."""

        return "adjudication_required" if self.adjudication_required else "exact"


class LedgerParityArtifactRepository:
    """Required durable sink for activation-facing parity evidence."""

    def __init__(self, engine: Engine, *, clock=None) -> None:
        self.engine = engine
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def record(
        self,
        stream_key: str,
        *,
        cutoff: datetime,
        report: LedgerParityReport,
    ) -> LedgerParityArtifact:
        row = LedgerParityArtifact(
            artifact_id=str(uuid4()),
            stream_key=stream_key,
            season=report.season,
            cutoff=cutoff,
            status="pending_adjudication" if report.adjudication_required else "exact",
            report=json.dumps(
                {
                    "game_count": report.game_count,
                    "compared_count": report.compared_count,
                    "differences": [asdict(difference) for difference in report.differences],
                },
                sort_keys=True,
                default=str,
            ),
            created_at=self.clock(),
        )
        with self.engine.begin() as connection:
            connection.execute(LedgerParityArtifact.__table__.insert().values(
                **{column.name: getattr(row, column.name) for column in LedgerParityArtifact.__table__.columns}
            ))
        return row

    def latest(self, stream_key: str, season: str) -> LedgerParityArtifact | None:
        with Session(self.engine) as session:
            return session.scalar(
                select(LedgerParityArtifact).where(
                    LedgerParityArtifact.stream_key == stream_key,
                    LedgerParityArtifact.season == season,
                ).order_by(LedgerParityArtifact.created_at.desc()).limit(1)
            )

    def adjudicate(
        self,
        artifact_id: str,
        *,
        decision: str,
        reason: str,
        actor: str,
    ) -> LedgerParityArtifact:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        if len(reason.strip()) < 3 or not actor.strip():
            raise ValueError("actor and reason are required")
        now = self.clock()
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            row = session.get(LedgerParityArtifact, artifact_id)
            if row is None:
                raise ValueError("parity artifact not found")
            row.decision = decision
            row.adjudicated_by = actor
            row.adjudicated_at = now
            row.adjudication_reason = reason
            session.add(AuditEvent(
                event_id=str(uuid4()),
                actor=actor,
                action=f"ledger.parity_{decision}",
                resource=artifact_id,
                reason=reason,
                details=json.dumps({"stream_key": row.stream_key, "season": row.season}, sort_keys=True),
                created_at=now,
            ))
            return row


class LegacyParityDiagnosticReader:
    """Read existing NBA diagnostic tables through an injected DB boundary."""

    TABLES = {
        "traditional_opponent": "opponent_stats",
        "player_per36": "player_per36_stats",
    }

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def read(self, stream_key: str) -> tuple[Mapping[str, object], ...]:
        from sqlalchemy import inspect, text

        table = self.TABLES[stream_key]
        if table not in inspect(self.engine).get_table_names():
            return ()
        with self.engine.connect() as connection:
            return tuple(dict(row) for row in connection.execute(text(f'SELECT * FROM "{table}"')).mappings())

_PLAYER_FIELDS = {
    "points": ("points", "PTS"),
    "rebounds": ("rebounds", "REB"),
    "assists": ("assists", "AST"),
    "turnovers": ("turnovers", "TOV"),
    "steals": ("steals", "STL"),
    "blocks": ("blocks", "BLK"),
    "personal_fouls": ("personal_fouls", "PF"),
    "minutes": ("minutes", "MIN"),
}
_TRADITIONAL_FIELDS = {
    "opponent_points": ("opponent_points", "OPP_PTS", "PTS"),
    "opponent_rebounds": ("opponent_rebounds", "OPP_REB", "REB"),
    "opponent_assists": ("opponent_assists", "OPP_AST", "AST"),
    "opponent_field_goals_made": ("opponent_field_goals_made", "OPP_FGM", "FGM"),
    "opponent_field_goals_attempted": ("opponent_field_goals_attempted", "OPP_FGA", "FGA"),
    "opponent_three_pointers_made": ("opponent_three_pointers_made", "OPP_FG3M", "FG3M"),
    "opponent_three_pointers_attempted": (
        "opponent_three_pointers_attempted",
        "OPP_FG3A",
        "FG3A",
    ),
    "opponent_free_throws_made": ("opponent_free_throws_made", "OPP_FTM", "FTM"),
    "opponent_free_throws_attempted": ("opponent_free_throws_attempted", "OPP_FTA", "FTA"),
    "opponent_turnovers": ("opponent_turnovers", "OPP_TOV", "TOV"),
    "opponent_steals": ("opponent_steals", "OPP_STL", "STL"),
    "opponent_blocks": ("opponent_blocks", "OPP_BLK", "BLK"),
    "opponent_personal_fouls": ("opponent_personal_fouls", "OPP_PF", "PF"),
}
_PER36_FIELDS = {
    "points_per36": ("points_per36", "PTS_PER36", "PTS"),
    "rebounds_per36": ("rebounds_per36", "REB_PER36", "REB"),
    "assists_per36": ("assists_per36", "AST_PER36", "AST"),
    "field_goals_made_per36": ("field_goals_made_per36", "FGM_PER36", "FGM"),
    "field_goals_attempted_per36": ("field_goals_attempted_per36", "FGA_PER36", "FGA"),
    "three_pointers_made_per36": ("three_pointers_made_per36", "FG3M_PER36", "FG3M"),
    "three_pointers_attempted_per36": (
        "three_pointers_attempted_per36",
        "FG3A_PER36",
        "FG3A",
    ),
    "free_throws_made_per36": ("free_throws_made_per36", "FTM_PER36", "FTM"),
    "free_throws_attempted_per36": ("free_throws_attempted_per36", "FTA_PER36", "FTA"),
    "turnovers_per36": ("turnovers_per36", "TOV_PER36", "TOV"),
    "steals_per36": ("steals_per36", "STL_PER36", "STL"),
    "blocks_per36": ("blocks_per36", "BLK_PER36", "BLK"),
    "personal_fouls_per36": ("personal_fouls_per36", "PF_PER36", "PF"),
}


def _first(row: Mapping[str, object], names: Sequence[str]) -> object | None:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _integer_identity(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _index_legacy(
    rows: Iterable[Mapping[str, object]],
    *,
    semantic: str,
    identity_from_row: Callable[[Mapping[str, object]], tuple[object, ...] | None],
    identity_text: Callable[[tuple[object, ...]], str],
) -> tuple[dict[tuple[object, ...], Mapping[str, object]], list[SemanticDifference]]:
    indexed: dict[tuple[object, ...], Mapping[str, object]] = {}
    differences: list[SemanticDifference] = []
    for index, row in enumerate(rows):
        identity = identity_from_row(row)
        if identity is None:
            differences.append(
                SemanticDifference(
                    f"{semantic}:invalid:{index}",
                    "identity",
                    None,
                    dict(row),
                    "invalid_legacy_identity",
                )
            )
            continue
        if identity in indexed:
            differences.append(
                SemanticDifference(
                    identity_text(identity),
                    "identity",
                    "unique",
                    "duplicate",
                    "duplicate_legacy_identity",
                )
            )
            continue
        indexed[identity] = row
    return indexed, differences


def _values_equal(left: object, right: object, tolerance: float) -> bool:
    try:
        return math.isclose(float(left), float(right), rel_tol=tolerance, abs_tol=tolerance)
    except (TypeError, ValueError):
        return left == right


def _compare_semantic(
    ledger: Mapping[tuple[object, ...], object],
    legacy_rows: Iterable[Mapping[str, object]] | None,
    *,
    semantic: str,
    identity_from_row: Callable[[Mapping[str, object]], tuple[object, ...] | None],
    identity_text: Callable[[tuple[object, ...]], str],
    fields: Mapping[str, Sequence[str]],
    tolerance: float,
) -> tuple[int, list[SemanticDifference]]:
    legacy, differences = _index_legacy(
        legacy_rows,
        semantic=semantic,
        identity_from_row=identity_from_row,
        identity_text=identity_text,
    )
    ledger_ids = set(ledger)
    legacy_ids = set(legacy)
    for identity in sorted(ledger_ids - legacy_ids):
        differences.append(
            SemanticDifference(
                identity_text(identity),
                "identity",
                "present",
                None,
                "missing_legacy_identity",
            )
        )
    for identity in sorted(legacy_ids - ledger_ids):
        differences.append(
            SemanticDifference(
                identity_text(identity),
                "identity",
                None,
                "present",
                "missing_ledger_identity",
            )
        )
    shared = sorted(ledger_ids & legacy_ids)
    for identity in shared:
        current = ledger[identity]
        legacy_row = legacy[identity]
        for field_name, aliases in fields.items():
            legacy_value = _first(legacy_row, aliases)
            if legacy_value is None:
                continue
            current_value = getattr(current, field_name)
            if not _values_equal(current_value, legacy_value, tolerance):
                differences.append(
                    SemanticDifference(
                        identity_text(identity),
                        field_name,
                        current_value,
                        legacy_value,
                        "semantic_difference",
                    )
                )
    return len(shared), differences


def _player_identity(row: Mapping[str, object]) -> tuple[object, ...] | None:
    game_id = str(_first(row, ("game_id", "GAME_ID")) or "").strip()
    player_id = _integer_identity(_first(row, ("player_id", "PLAYER_ID")))
    return (game_id, player_id) if game_id and player_id is not None else None


def _traditional_identity(row: Mapping[str, object]) -> tuple[object, ...] | None:
    game_id = str(_first(row, ("game_id", "GAME_ID")) or "").strip()
    team_id = _integer_identity(_first(row, ("team_id", "TEAM_ID")))
    return (game_id, team_id) if game_id and team_id is not None else None


def _per36_identity(row: Mapping[str, object]) -> tuple[object, ...] | None:
    player_id = _integer_identity(_first(row, ("player_id", "PLAYER_ID")))
    return (player_id,) if player_id is not None else None


def compare_ledger_to_legacy(
    games: Iterable[CanonicalGame],
    legacy_rows: Iterable[Mapping[str, object]],
    *,
    season: str,
    tolerance: float = 1e-9,
    legacy_traditional_rows: Iterable[Mapping[str, object]] | None = None,
    legacy_per36_rows: Iterable[Mapping[str, object]] | None = None,
) -> LedgerParityReport:
    """Compare governed ledger semantics.

    ``legacy_rows`` compares player game primitives. The optional inputs add
    independent comparisons for traditional opponent and season per-36
    outputs. ``None`` means that semantic was not sampled; an explicitly empty
    iterable is a sampled empty set and therefore cannot claim parity against
    non-empty ledger output.

    Legacy percentages and provider-specific rates remain outside the player
    game comparison. A difference is adjudication evidence, never a reason to
    rewrite the retained PBP observation.
    """

    canonical_season = validate_canonical_season(season)
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be finite and non-negative")
    games_tuple = tuple(game for game in games if game.season == canonical_season)
    ledger_players: dict[tuple[object, ...], PlayerGameFact] = {
        (game.game_id, player.player_id): player
        for game in games_tuple
        for player in game.player_facts
    }
    compared = 0
    differences: list[SemanticDifference] = []
    if legacy_rows is not None:
        compared, differences = _compare_semantic(
            ledger_players,
            legacy_rows,
            semantic="player_game",
            identity_from_row=_player_identity,
            identity_text=lambda identity: f"{identity[0]}:{identity[1]}",
            fields=_PLAYER_FIELDS,
            tolerance=tolerance,
        )
    if legacy_traditional_rows is not None:
        traditional: dict[tuple[object, ...], TraditionalOpponentFact] = {
            (fact.game_id, fact.team_id): fact
            for fact in derive_traditional_opponent_facts(games_tuple)
        }
        count, found = _compare_semantic(
            traditional,
            legacy_traditional_rows,
            semantic="traditional_opponent",
            identity_from_row=_traditional_identity,
            identity_text=lambda identity: f"traditional:{identity[0]}:{identity[1]}",
            fields=_TRADITIONAL_FIELDS,
            tolerance=tolerance,
        )
        compared += count
        differences.extend(found)
    if legacy_per36_rows is not None:
        per36: dict[tuple[object, ...], PlayerPer36Fact] = {
            (fact.player_id,): fact
            for fact in derive_player_per36_facts(games_tuple, season=canonical_season)
        }
        count, found = _compare_semantic(
            per36,
            legacy_per36_rows,
            semantic="player_per36",
            identity_from_row=_per36_identity,
            identity_text=lambda identity: f"per36:{identity[0]}",
            fields=_PER36_FIELDS,
            tolerance=tolerance,
        )
        compared += count
        differences.extend(found)
    if compared == 0 and not differences:
        differences.append(
            SemanticDifference(
                "parity",
                "identity",
                None,
                None,
                "empty_comparison",
            )
        )
    report = LedgerParityReport(
        season=canonical_season,
        game_count=len(games_tuple),
        compared_count=compared,
        differences=tuple(differences),
        adjudication_required=bool(differences),
    )
    return report


def generate_semantic_difference_report(
    games: Iterable[CanonicalGame],
    legacy_rows: Iterable[Mapping[str, object]],
    *,
    season: str,
    tolerance: float = 1e-9,
    legacy_traditional_rows: Iterable[Mapping[str, object]] | None = None,
    legacy_per36_rows: Iterable[Mapping[str, object]] | None = None,
    artifact_repository: LedgerParityArtifactRepository,
    stream_key: str,
    cutoff: datetime,
) -> LedgerParityReport:
    """Generate and durably persist required activation evidence."""

    report = compare_ledger_to_legacy(
        games,
        legacy_rows,
        season=season,
        tolerance=tolerance,
        legacy_traditional_rows=legacy_traditional_rows,
        legacy_per36_rows=legacy_per36_rows,
    )
    artifact_repository.record(stream_key, cutoff=cutoff, report=report)
    return report


__all__ = [
    "LedgerParityArtifactRepository",
    "LegacyParityDiagnosticReader",
    "LedgerParityReport",
    "SemanticDifference",
    "compare_ledger_to_legacy",
    "generate_semantic_difference_report",
]
