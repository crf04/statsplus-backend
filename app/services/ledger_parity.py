"""Semantic parity evidence between PBP-derived and legacy provider facts."""

from __future__ import annotations

import math
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, make_dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.models.canonical_game_ledger import LedgerParityArtifact
from app.models.collection_control import AuditEvent, PublicationVersion

from app.domain.team_matchup_taxonomy import matchup_stream_key
from app.domain.publication_integrity import publication_payload_matches_checksum
from app.domain.utc import assume_utc
from app.services.canonical_game_ledger import CanonicalGame, PlayerGameFact, validate_canonical_season
from app.services.ledger_derivations import (
    PlayerPer36Fact,
    TraditionalOpponentFact,
    derive_player_per36_facts,
    derive_traditional_opponent_facts,
)
from app.services.publication_authority import verify_publication_authority
from app.services.team_matchup_publications import PublicationGovernanceUnavailable


_MATCHUP_STREAMS = frozenset({
    "traditional_opponent_season",
    "traditional_opponent_l15",
    "assist_locations_season",
    "assist_locations_l15",
})
_MATCHUP_HARD_CLASSIFICATIONS = frozenset({
    "league_incomplete",
    "missing_legacy_team",
    "missing_ledger_team",
    "extra_team",
    "game_set_mismatch",
    "integer_count_difference",
    "non_integer_count",
    "availability_difference",
    "cutoff_mismatch",
    "scope_mismatch",
    "missing_surface",
    "missing_metric",
    "extra_metric",
    "duplicate_metric",
    "l15_game_count_mismatch",
    "authority_mismatch",
    "invalid_denominator",
    "ranking_difference",
})
_MATCHUP_SOFT_CLASSIFICATIONS = frozenset({
    "denominator_tolerance_exceeded",
    "derived_rate_difference",
})


def matchup_parity_artifact_is_activatable(
    artifact: LedgerParityArtifact,
    *,
    stream_key: str | None = None,
) -> bool:
    """Return whether an artifact contains only exact/soft matchup evidence."""

    if artifact.stream_key not in _MATCHUP_STREAMS:
        return True
    if stream_key is not None and artifact.stream_key != stream_key:
        return False
    try:
        document = json.loads(artifact.report)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(document, Mapping):
        return False
    if document.get("status") not in {"exact", "adjudication_required"}:
        return False
    required_evidence = (
        "legacy_game_ids_by_team",
        "ledger_game_ids_by_team",
        "legacy_game_set_checksum",
        "ledger_game_set_checksum",
        "legacy_manifest_id",
        "legacy_event_catalog_publication_id",
        "legacy_event_catalog_checksum",
        "ledger_manifest_id",
        "ledger_event_catalog_publication_id",
        "ledger_event_catalog_checksum",
    )
    if any(key not in document for key in required_evidence):
        return False
    if (
        document.get("ledger_publication_id") != artifact.publication_id
        or document.get("ledger_payload_checksum") != artifact.payload_checksum
        or not all(
            isinstance(document.get(key), str) and document.get(key).strip()
            for key in (
                "legacy_game_set_checksum", "ledger_game_set_checksum",
                "legacy_manifest_id", "legacy_event_catalog_publication_id",
                "legacy_event_catalog_checksum", "ledger_manifest_id",
                "ledger_event_catalog_publication_id", "ledger_event_catalog_checksum",
            )
        )
        or document["legacy_game_ids_by_team"] != document["ledger_game_ids_by_team"]
    ):
        return False
    differences = document.get("differences", ())
    if not isinstance(differences, list):
        return False
    classifications = []
    for difference in differences:
        if not isinstance(difference, Mapping):
            return False
        classification = difference.get("classification")
        if classification not in (
            _MATCHUP_HARD_CLASSIFICATIONS | _MATCHUP_SOFT_CLASSIFICATIONS
        ):
            return False
        classifications.append(classification)
    if document["status"] == "exact":
        return not classifications
    return bool(classifications) and not any(
        classification in _MATCHUP_HARD_CLASSIFICATIONS
        for classification in classifications
    )


def matchup_parity_cohort_is_activatable(
    session: Session,
    *,
    season: str,
    cutoff,
    candidate_publication_id: str,
    artifact_id: str,
) -> bool:
    """Require one exact Season+L15, traditional+assist evidence cohort."""
    required = _MATCHUP_STREAMS
    rows = session.scalars(select(LedgerParityArtifact).where(
        LedgerParityArtifact.season == season,
        LedgerParityArtifact.cutoff == assume_utc(cutoff),
        LedgerParityArtifact.stream_key.in_(required),
    )).all()
    by_stream = {row.stream_key: row for row in rows}
    if len(rows) != len(required) or set(by_stream) != required:
        return False
    for stream_key in required:
        artifact = by_stream[stream_key]
        if not matchup_parity_artifact_is_activatable(artifact, stream_key=stream_key):
            return False
        if artifact.status != "exact" and artifact.decision != "approved":
            return False
        publication = session.get(PublicationVersion, artifact.publication_id)
        if publication is None or publication.status not in {"candidate", "active"}:
            return False
        if artifact.payload_checksum != publication.checksum:
            return False
        try:
            document = json.loads(artifact.report)
            authority = verify_publication_authority(session, publication)
            if (
                document.get("ledger_manifest_id") != authority.manifest_id
                or document.get("ledger_event_catalog_publication_id")
                != authority.event_catalog_publication_id
                or document.get("ledger_event_catalog_checksum")
                != authority.event_catalog_checksum
            ):
                return False
            from app.services.matchup_parity import _decode_ledger_rows

            rows = _decode_ledger_rows(publication.payload, stream_key=stream_key)
            expected_ids = {
                str(row.team_id): sorted(row.game_ids) for row in rows
            }
            if document.get("ledger_game_ids_by_team") != expected_ids:
                return False
        except (PublicationGovernanceUnavailable, TypeError, ValueError, KeyError,
                json.JSONDecodeError):
            return False
        if stream_key == artifact.stream_key and artifact.artifact_id == artifact_id:
            if artifact.publication_id != candidate_publication_id:
                return False
    return True


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
        publication_id: str,
        payload_checksum: str,
        session: Session | None = None,
        connection: Connection | None = None,
    ) -> LedgerParityArtifact:
        if not publication_id or len(payload_checksum) != 64:
            raise ValueError("candidate publication and payload checksum are required")
        row = LedgerParityArtifact(
            artifact_id=str(uuid4()),
            publication_id=publication_id,
            payload_checksum=payload_checksum,
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
        self._insert_artifact(row, session=session, connection=connection)
        return row

    def record_matchup_parity(
        self,
        stream_key: str,
        *,
        cutoff: datetime,
        report,
        publication_id: str,
        payload_checksum: str,
        session: Session | None = None,
        connection: Connection | None = None,
    ) -> LedgerParityArtifact:
        """Persist one matchup materializer dual-run report as parity evidence.

        The artifact is bound to the exact report window, surface, and aware
        immutable cutoff: the ``stream_key`` must name the report's own surface
        and window, and ``cutoff`` must equal the report's exact aware cutoff,
        so an L15 artifact can never authorize a Season stream.
        """

        if not publication_id or len(payload_checksum) != 64:
            raise ValueError("candidate publication and payload checksum are required")
        if getattr(report, "hard_failure", False):
            raise ValueError("matchup parity report contains non-adjudicable hard failures")
        known_classifications = (
            _MATCHUP_SOFT_CLASSIFICATIONS | _MATCHUP_HARD_CLASSIFICATIONS
        )
        if any(
            getattr(difference, "classification", None) not in known_classifications
            for difference in getattr(report, "differences", ())
        ):
            raise ValueError("matchup parity report contains an unknown classification")
        expected_stream_key = matchup_stream_key(report.surface, report.window)
        if stream_key != expected_stream_key:
            raise ValueError(
                f"stream_key {stream_key} does not match report surface/window "
                f"{expected_stream_key}"
            )
        if cutoff is None or cutoff.tzinfo is None:
            raise ValueError("matchup parity requires an aware immutable cutoff")
        if (
            report.cutoff is None
            or report.cutoff.tzinfo is None
            or assume_utc(report.cutoff) != assume_utc(cutoff)
        ):
            raise ValueError("matchup parity cutoff does not match the report cutoff")
        if getattr(report, "ledger_publication_id", None) not in {None, publication_id}:
            raise ValueError("matchup parity publication does not match the report publication")
        if getattr(report, "ledger_payload_checksum", None) not in {None, payload_checksum}:
            raise ValueError("matchup parity checksum does not match the report publication")
        self._verify_matchup_candidate(
            stream_key,
            report=report,
            publication_id=publication_id,
            payload_checksum=payload_checksum,
            session=session,
            connection=connection,
        )
        row = LedgerParityArtifact(
            artifact_id=str(uuid4()),
            publication_id=publication_id,
            payload_checksum=payload_checksum,
            stream_key=stream_key,
            season=report.season,
            cutoff=cutoff,
            status="pending_adjudication" if report.adjudication_required else "exact",
            report=json.dumps(report.to_dict(), sort_keys=True, default=str),
            created_at=self.clock(),
        )
        self._insert_artifact(row, session=session, connection=connection)
        return row

    def _verify_matchup_candidate(
        self,
        stream_key: str,
        *,
        report,
        publication_id: str,
        payload_checksum: str,
        session: Session | None,
        connection: Connection | None,
    ) -> None:
        """Bind matchup evidence to the actual immutable candidate row."""

        if session is not None and connection is not None:
            raise ValueError("session and connection are mutually exclusive")
        if session is not None:
            publication = session.get(PublicationVersion, publication_id)
            self._check_matchup_candidate(
                publication, stream_key, report, payload_checksum
            )
            self._verify_matchup_report_authority(session, publication, report)
            return
        if connection is not None:
            with Session(bind=connection) as authority_session:
                publication = authority_session.get(PublicationVersion, publication_id)
                self._check_matchup_candidate(
                    publication, stream_key, report, payload_checksum
                )
                self._verify_matchup_report_authority(
                    authority_session, publication, report
                )
            return
        with Session(self.engine) as owned_session:
            publication = owned_session.get(PublicationVersion, publication_id)
            self._check_matchup_candidate(
                publication, stream_key, report, payload_checksum
            )
            self._verify_matchup_report_authority(owned_session, publication, report)

    @staticmethod
    def _verify_matchup_authority(session, publication):
        try:
            return verify_publication_authority(session, publication)
        except PublicationGovernanceUnavailable as error:
            raise ValueError("matchup parity candidate authority is not exact") from error

    @classmethod
    def _verify_matchup_report_authority(cls, session, publication, report) -> None:
        authority = cls._verify_matchup_authority(session, publication)
        expected = (
            authority.manifest_id,
            authority.event_catalog_publication_id,
            authority.event_catalog_checksum,
        )
        actual = (
            getattr(report, "ledger_manifest_id", None),
            getattr(report, "ledger_event_catalog_publication_id", None),
            getattr(report, "ledger_event_catalog_checksum", None),
        )
        if actual != expected:
            raise ValueError("matchup parity report authority is not exact")

    @staticmethod
    def _check_matchup_candidate(
        publication,
        stream_key: str,
        report,
        payload_checksum: str,
    ) -> None:
        if publication is None:
            raise ValueError("matchup parity candidate publication not found")
        def get_value(name):
            return (
                getattr(publication, name)
                if hasattr(publication, name)
                else publication.get(name)
            )
        if (
            get_value("stream_key") != stream_key
            or get_value("season") != report.season
            or get_value("status") != "candidate"
            or assume_utc(get_value("cutoff")) != assume_utc(report.cutoff)
            or get_value("checksum") != payload_checksum
            or not publication_payload_matches_checksum(
                get_value("payload"), get_value("checksum")
            )
        ):
            raise ValueError("matchup parity candidate publication is not exact")

    def _insert_artifact(
        self,
        row: LedgerParityArtifact,
        *,
        session: Session | None,
        connection: Connection | None,
    ) -> None:
        if session is not None and connection is not None:
            raise ValueError("session and connection are mutually exclusive")
        values = {
            column.name: getattr(row, column.name)
            for column in LedgerParityArtifact.__table__.columns
        }
        if session is not None:
            session.execute(LedgerParityArtifact.__table__.insert().values(**values))
        elif connection is not None:
            connection.execute(LedgerParityArtifact.__table__.insert().values(**values))
        else:
            with self.engine.begin() as connection:
                connection.execute(LedgerParityArtifact.__table__.insert().values(**values))

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
            if decision == "approved" and not matchup_parity_artifact_is_activatable(row):
                raise ValueError("hard matchup parity failures cannot be approved")
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
        "player_game_logs": "player_game_logs",
        "traditional_opponent": "general_opponent_stats",
        "player_per36": "player_per36_stats",
    }

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def read(
        self,
        stream_key: str,
        *,
        session: Session | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        from sqlalchemy import inspect, text

        table = self.TABLES[stream_key]
        inspector = inspect(session.connection() if session is not None else self.engine)
        if table not in inspector.get_table_names():
            raise ValueError(f"required legacy parity table {table} is unavailable")
        if session is not None:
            rows = tuple(dict(row) for row in session.execute(text(f'SELECT * FROM "{table}"')).mappings())
        else:
            with self.engine.connect() as connection:
                rows = tuple(dict(row) for row in connection.execute(text(f'SELECT * FROM "{table}"')).mappings())
        if not rows:
            raise ValueError(f"required legacy parity table {table} is empty")
        return rows

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
    team_id = _integer_identity(_first(row, ("team_id", "TEAM_ID")))
    return (team_id,) if team_id is not None else None


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
        traditional = _season_traditional_opponent(games_tuple)
        count, found = _compare_semantic(
            traditional,
            legacy_traditional_rows,
            semantic="traditional_opponent",
            identity_from_row=_traditional_identity,
            identity_text=lambda identity: f"traditional:{identity[0]}",
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
    publication_id: str,
    payload_checksum: str,
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
    artifact_repository.record(
        stream_key,
        cutoff=cutoff,
        report=report,
        publication_id=publication_id,
        payload_checksum=payload_checksum,
    )
    return report


_TraditionalSeasonMetric = make_dataclass(
    "_TraditionalSeasonMetric",
    [(field, float) for field in _TRADITIONAL_FIELDS],
    frozen=True,
    slots=True,
)


def _season_traditional_opponent(
    games: Iterable[CanonicalGame],
) -> dict[tuple[object, ...], object]:
    """Aggregate ledger opponent counts to legacy ``Per48`` team semantics."""

    games_tuple = tuple(games)
    facts = derive_traditional_opponent_facts(games_tuple)
    games_by_id = {game.game_id: game for game in games_tuple}
    grouped: dict[int, list[TraditionalOpponentFact]] = {}
    for fact in facts:
        grouped.setdefault(fact.team_id, []).append(fact)
    result: dict[tuple[object, ...], object] = {}
    for team_id, team_facts in grouped.items():
        team_minutes = sum(
            next(
                row.team_minutes
                for row in games_by_id[fact.game_id].team_facts
                if row.team_id == team_id
            )
            for fact in team_facts
        )
        scale = 48.0 / team_minutes if team_minutes > 0 else 1.0 / len(team_facts)
        result[(team_id,)] = _TraditionalSeasonMetric(**{
            field: sum(float(getattr(fact, field)) for fact in team_facts) * scale
            for field in _TRADITIONAL_FIELDS
        })
    return result


__all__ = [
    "LedgerParityArtifactRepository",
    "matchup_parity_artifact_is_activatable",
    "matchup_parity_cohort_is_activatable",
    "LegacyParityDiagnosticReader",
    "LedgerParityReport",
    "SemanticDifference",
    "compare_ledger_to_legacy",
    "generate_semantic_difference_report",
]
