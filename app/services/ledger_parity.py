"""Semantic parity evidence between PBP-derived and legacy provider facts."""

from __future__ import annotations

import math
import json
import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, make_dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.models.canonical_game_ledger import LedgerParityArtifact
from app.models.collection_control import (
    AuditEvent,
    CatalogPublication,
    CollectionManifest,
    PublicationPointer,
    PublicationVersion,
    CollectionObservation,
)

from app.domain.matchup_parity_contract import (
    HARD_CLASSIFICATIONS,
    MATCHUP_REQUIRED_STREAMS,
    SOFT_CLASSIFICATIONS,
    semantic_rule_is_approved,
)
from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
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
from app.services.ledger_lineage import LedgerLineage
from app.services.publication_authority import verify_publication_authority
from app.services.team_matchup_publications import PublicationGovernanceUnavailable


_MATCHUP_STREAMS = MATCHUP_REQUIRED_STREAMS
_MATCHUP_HARD_CLASSIFICATIONS = HARD_CLASSIFICATIONS
_MATCHUP_SOFT_CLASSIFICATIONS = SOFT_CLASSIFICATIONS

PER36_DIAGNOSTIC_CAPTURE_STREAM = "player_per36_diagnostic_capture"
LEGACY_MATCHUP_DIAGNOSTIC_CAPTURE_STREAM = "legacy_matchup_diagnostic_capture"
PER36_RAW_FIELDS = (
    "points",
    "rebounds",
    "assists",
    "field_goals_made",
    "field_goals_attempted",
    "three_pointers_made",
    "three_pointers_attempted",
    "free_throws_made",
    "free_throws_attempted",
    "turnovers",
    "steals",
    "blocks",
    "personal_fouls",
)
PER36_RATE_FIELDS = tuple(f"{field}_per36" for field in PER36_RAW_FIELDS)


@dataclass(frozen=True, slots=True)
class Per36DiagnosticCapture:
    """Immutable, authority-bound provider evidence for Season per-36."""

    capture_id: str
    publication_id: str
    payload_checksum: str
    season: str
    cutoff: datetime
    manifest_id: str
    event_catalog_publication_id: str
    event_catalog_checksum: str
    game_set_checksum: str
    request_checksum: str
    provider_window_identity: Mapping[str, object]
    rows: tuple[Mapping[str, object], ...]
    actor: str
    capture_checksum: str
    source_observation_id: str


@dataclass(frozen=True, slots=True)
class LegacyMatchupDiagnosticCapture:
    """Immutable copy of the protected legacy side used by one dual run."""

    capture_id: str
    capture_checksum: str
    season: str
    window: str
    cutoff: datetime
    manifest_id: str
    event_catalog_publication_id: str
    event_catalog_checksum: str
    provider_window_identity: str
    document: Mapping[str, object]


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _canonical_capture_payload(document: Mapping[str, object]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class Per36DiagnosticCaptureRepository:
    """Persist and read the exact scoped per-36 diagnostic capture.

    This deliberately uses the append-only parity-artifact table rather than
    the legacy ``player_per36_stats`` table.  The latter has no durable
    manifest, request, or game-window authority and can never authorize a
    comparison by itself.
    """

    def __init__(self, engine: Engine, *, clock=None) -> None:
        self.engine = engine
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _validate_rows(rows: Iterable[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
        validated: list[dict[str, object]] = []
        identities: set[int] = set()
        for source in rows:
            if not isinstance(source, Mapping):
                raise ValueError("per36 capture rows must be objects")
            row = dict(source)
            player_id = row.get("player_id")
            if isinstance(player_id, bool) or not isinstance(player_id, int) or player_id <= 0:
                raise ValueError("per36 capture player identity is invalid")
            if player_id in identities:
                raise ValueError("per36 capture contains duplicate players")
            identities.add(player_id)
            minutes = row.get("minutes")
            if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
                raise ValueError("per36 capture minutes are invalid")
            if not math.isfinite(float(minutes)) or float(minutes) <= 0:
                raise ValueError("per36 capture minutes are invalid")
            game_count = row.get("game_count")
            if isinstance(game_count, bool) or not isinstance(game_count, int) or game_count <= 0:
                raise ValueError("per36 capture game count is invalid")
            team_ids = row.get("team_ids_at_game")
            if not isinstance(team_ids, (list, tuple)) or not team_ids:
                raise ValueError("per36 capture team identity is invalid")
            if any(
                isinstance(team_id, bool) or not isinstance(team_id, int) or team_id <= 0
                for team_id in team_ids
            ) or tuple(sorted(set(team_ids))) != tuple(team_ids):
                raise ValueError("per36 capture team identity is invalid")
            for field in PER36_RAW_FIELDS:
                value = row.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"per36 capture raw field {field} is invalid")
            for field in PER36_RATE_FIELDS:
                value = row.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"per36 capture rate field {field} is invalid")
                if not math.isfinite(float(value)) or float(value) < 0:
                    raise ValueError(f"per36 capture rate field {field} is invalid")
            validated.append(row)
        if not validated:
            raise ValueError("per36 capture rows are empty")
        return tuple(validated)

    @classmethod
    def _validate_document(cls, document: Mapping[str, object]) -> Per36DiagnosticCapture:
        required = (
            "kind", "capture_id", "publication_id", "payload_checksum", "season", "cutoff",
            "manifest_id", "event_catalog_publication_id", "event_catalog_checksum",
            "game_set_checksum", "request_checksum", "provider_window_identity",
            "rows", "actor", "capture_checksum",
            "source_observation_id",
        )
        if any(key not in document for key in required):
            raise ValueError("per36 capture evidence is incomplete")
        if document["kind"] != PER36_DIAGNOSTIC_CAPTURE_STREAM:
            raise ValueError("per36 capture kind is invalid")
        if any(
            not isinstance(document[key], str) or not document[key].strip()
            for key in (
                "capture_id", "publication_id", "season", "manifest_id",
                "event_catalog_publication_id", "actor",
                "source_observation_id",
            )
        ):
            raise ValueError("per36 capture identity is invalid")
        if not all(
            _is_sha256(document[key])
            for key in (
                "payload_checksum", "event_catalog_checksum", "game_set_checksum",
                "request_checksum", "capture_checksum",
            )
        ):
            raise ValueError("per36 capture checksum is invalid")
        try:
            cutoff = assume_utc(datetime.fromisoformat(str(document["cutoff"])))
        except (TypeError, ValueError) as error:
            raise ValueError("per36 capture cutoff is invalid") from error
        identity = document["provider_window_identity"]
        if not isinstance(identity, Mapping):
            raise ValueError("per36 provider window identity is invalid")
        try:
            provider_start = datetime.fromisoformat(
                str(identity.get("provider_start_date"))
            ).date()
            provider_end = datetime.fromisoformat(
                str(identity.get("provider_end_date"))
            ).date()
        except ValueError as error:
            raise ValueError("per36 provider window identity is invalid") from error
        game_ids = identity.get("game_ids")
        mapping_trace = identity.get("event_catalog_mapping_trace")
        request_identity = {
            key: identity.get(key)
            for key in (
                "season", "window", "cutoff", "provider_start_date",
                "provider_end_date",
            )
        }
        expected_request_checksum = hashlib.sha256(
            _canonical_capture_payload(request_identity).encode("utf-8")
        ).hexdigest()
        if (
            identity.get("season") != document["season"]
            or identity.get("window") != "season"
            or identity.get("cutoff") != cutoff.isoformat()
            or not isinstance(game_ids, list)
            or not game_ids
            or any(not isinstance(game_id, str) or not game_id for game_id in game_ids)
            or game_ids != sorted(set(game_ids))
            or identity.get("request_checksum") != document["request_checksum"]
            or document["request_checksum"] != expected_request_checksum
            or not isinstance(identity.get("provider_start_date"), str)
            or not isinstance(identity.get("provider_end_date"), str)
            or provider_start > provider_end
            or identity.get("returned_row_count") != len(document["rows"])
            or identity.get("returned_game_count") != len(game_ids)
            or not isinstance(mapping_trace, Mapping)
            or set(mapping_trace) != set(game_ids)
            or any(not isinstance(value, str) or not value for value in mapping_trace.values())
        ):
            raise ValueError("per36 provider window identity is invalid")
        rows = cls._validate_rows(document["rows"])
        unsigned = dict(document)
        unsigned.pop("capture_checksum", None)
        expected_checksum = hashlib.sha256(
            _canonical_capture_payload(unsigned).encode("utf-8")
        ).hexdigest()
        if document["capture_checksum"] != expected_checksum:
            raise ValueError("per36 capture checksum does not match evidence")
        return Per36DiagnosticCapture(
            capture_id=str(document["capture_id"]),
            publication_id=str(document["publication_id"]),
            payload_checksum=str(document["payload_checksum"]),
            season=str(document["season"]),
            cutoff=cutoff,
            manifest_id=str(document["manifest_id"]),
            event_catalog_publication_id=str(document["event_catalog_publication_id"]),
            event_catalog_checksum=str(document["event_catalog_checksum"]),
            game_set_checksum=str(document["game_set_checksum"]),
            request_checksum=str(document["request_checksum"]),
            provider_window_identity=dict(identity),
            rows=rows,
            actor=str(document["actor"]),
            capture_checksum=str(document["capture_checksum"]),
            source_observation_id=str(document["source_observation_id"]),
        )

    def record(
        self,
        *,
        publication_id: str,
        payload_checksum: str,
        season: str,
        cutoff: datetime,
        manifest_id: str,
        event_catalog_publication_id: str,
        event_catalog_checksum: str,
        game_set_checksum: str,
        request_checksum: str,
        provider_window_identity: Mapping[str, object],
        rows: Iterable[Mapping[str, object]],
        actor: str,
        source_observation_id: str,
        capture_id: str | None = None,
        session: Session | None = None,
    ) -> Per36DiagnosticCapture:
        if cutoff is None or cutoff.tzinfo is None:
            raise ValueError("per36 capture cutoff must be aware")
        if not actor.strip() or len(actor) > 128:
            raise ValueError("per36 capture actor is invalid")
        capture_id = capture_id or str(uuid4())
        rows_tuple = self._validate_rows(rows)
        owned = session is None
        lookup_session = session or Session(self.engine)
        try:
            observation = lookup_session.get(CollectionObservation, source_observation_id)
            if (
                observation is None
                or observation.observation_type != "player_per36_diagnostic"
                or observation.season != season
                or assume_utc(observation.cutoff) != assume_utc(cutoff)
                or observation.manifest_id != manifest_id
                or not publication_payload_matches_checksum(
                    observation.payload, observation.checksum
                )
            ):
                raise ValueError("per36 capture source observation is invalid")
            observation_payload = json.loads(observation.payload)
            if (
                not isinstance(observation_payload, Mapping)
                or observation_payload.get("rows") != [dict(row) for row in rows_tuple]
                or observation_payload.get("provider_window_identity")
                != dict(provider_window_identity)
                or observation_payload.get("request_checksum") != request_checksum
            ):
                raise ValueError("per36 capture source observation does not match evidence")
        finally:
            if owned:
                lookup_session.close()
        document: dict[str, object] = {
            "kind": PER36_DIAGNOSTIC_CAPTURE_STREAM,
            "capture_id": capture_id,
            "publication_id": publication_id,
            "payload_checksum": payload_checksum,
            "season": season,
            "cutoff": assume_utc(cutoff).isoformat(),
            "manifest_id": manifest_id,
            "event_catalog_publication_id": event_catalog_publication_id,
            "event_catalog_checksum": event_catalog_checksum,
            "game_set_checksum": game_set_checksum,
            "request_checksum": request_checksum,
            "provider_window_identity": dict(provider_window_identity),
            "rows": [dict(row) for row in rows_tuple],
            "actor": actor,
            "source_observation_id": source_observation_id,
        }
        capture_checksum = hashlib.sha256(
            _canonical_capture_payload(document).encode("utf-8")
        ).hexdigest()
        document["capture_checksum"] = capture_checksum
        capture = self._validate_document(document)
        row = LedgerParityArtifact(
            artifact_id=capture.capture_id,
            publication_id=capture.publication_id,
            payload_checksum=capture.payload_checksum,
            stream_key=PER36_DIAGNOSTIC_CAPTURE_STREAM,
            season=capture.season,
            cutoff=capture.cutoff,
            status="exact",
            report=json.dumps(document, sort_keys=True, separators=(",", ":")),
            created_at=self.clock(),
        )
        values = {
            column.name: getattr(row, column.name)
            for column in LedgerParityArtifact.__table__.columns
        }
        if session is not None:
            session.execute(LedgerParityArtifact.__table__.insert().values(**values))
        else:
            with self.engine.begin() as owned:
                owned.execute(LedgerParityArtifact.__table__.insert().values(**values))
        return capture


    def record_operator_evidence(
        self,
        *,
        publication_id: str,
        season: str,
        cutoff: datetime,
        manifest_id: str,
        event_catalog_publication_id: str,
        event_catalog_checksum: str,
        game_set_checksum: str,
        request_checksum: str,
        provider_window_identity: Mapping[str, object],
        rows: Iterable[Mapping[str, object]],
        actor: str,
    ) -> Per36DiagnosticCapture:
        """Create one audited immutable capture from bounded operator evidence."""

        rows_tuple = self._validate_rows(rows)
        now = self.clock()
        from app.services.ledger_runtime import (
            ActiveManifestLedgerGovernanceReader,
        )

        governance = ActiveManifestLedgerGovernanceReader(
            self.engine
        ).read_for_composition(season, cutoff, manifest_id=manifest_id)
        expected_game_ids = sorted(map(str, governance.expected_game_ids))
        governed_game_set_checksum = LedgerLineage.for_game_ids(
            expected_game_ids
        )
        mapping_trace = provider_window_identity.get(
            "event_catalog_mapping_trace"
        )
        if (
            game_set_checksum != governed_game_set_checksum
            or not isinstance(mapping_trace, Mapping)
            or len(mapping_trace) != len(expected_game_ids)
            or set(map(str, mapping_trace.values())) != set(expected_game_ids)
            or provider_window_identity.get("game_ids") != expected_game_ids
        ):
            raise ValueError("per36 operator evidence game set is invalid")
        with Session(self.engine) as session, session.begin():
            publication = session.scalar(
                select(PublicationVersion)
                .where(PublicationVersion.publication_id == publication_id)
                .with_for_update()
            )
            manifest = session.scalar(
                select(CollectionManifest)
                .where(CollectionManifest.manifest_id == manifest_id)
                .with_for_update()
            )
            catalog = session.scalar(
                select(CatalogPublication)
                .where(
                    CatalogPublication.publication_id
                    == event_catalog_publication_id
                )
                .with_for_update()
            )
            if (
                publication is None
                or publication.stream_key != "player_per36"
                or publication.status != "candidate"
                or publication.season != season
                or assume_utc(publication.cutoff) != assume_utc(cutoff)
                or publication.manifest_id != manifest_id
                or publication.event_catalog_publication_id
                != event_catalog_publication_id
                or publication.event_catalog_checksum != event_catalog_checksum
                or not publication_payload_matches_checksum(
                    publication.payload, publication.checksum
                )
                or manifest is None
                or manifest.status != "active"
                or manifest.season != season
                or assume_utc(manifest.cutoff) != assume_utc(cutoff)
                or manifest.event_catalog_publication_id
                != event_catalog_publication_id
                or manifest.event_catalog_checksum != event_catalog_checksum
                or catalog is None
                or not catalog.complete
                or catalog.checksum != event_catalog_checksum
                or not publication_payload_matches_checksum(
                    catalog.payload, catalog.checksum
                )
            ):
                raise ValueError("per36 operator evidence authority is invalid")
            observation_id = str(uuid4())
            observation_document = {
                "rows": [dict(row) for row in rows_tuple],
                "provider_window_identity": dict(provider_window_identity),
                "request_checksum": request_checksum,
            }
            payload = _canonical_capture_payload(observation_document)
            session.add(CollectionObservation(
                observation_id=observation_id,
                client_observation_id=f"operator-per36-{observation_id}",
                collector_id="matchup-parity-operator",
                manifest_id=manifest_id,
                environment="operator",
                provider="nba_stats",
                observation_type="player_per36_diagnostic",
                scope=json.dumps({
                    "season": season,
                    "window": "season",
                    "manifest_id": manifest_id,
                }, sort_keys=True),
                season=season,
                cutoff=assume_utc(cutoff),
                schema_version=1,
                checksum=hashlib.sha256(payload.encode()).hexdigest(),
                payload=payload,
                payload_bytes=len(payload.encode()),
                retrieved_at=now,
                accepted_at=now,
            ))
            session.flush()
            capture = self.record(
                publication_id=publication_id,
                payload_checksum=publication.checksum,
                season=season,
                cutoff=cutoff,
                manifest_id=manifest_id,
                event_catalog_publication_id=event_catalog_publication_id,
                event_catalog_checksum=event_catalog_checksum,
                game_set_checksum=governed_game_set_checksum,
                request_checksum=request_checksum,
                provider_window_identity=provider_window_identity,
                rows=rows_tuple,
                actor=actor,
                source_observation_id=observation_id,
                session=session,
            )
            session.add(AuditEvent(
                event_id=str(uuid4()),
                actor=actor[:128],
                action="ledger.per36_capture_recorded",
                resource=capture.capture_id,
                reason="bounded operator evidence capture",
                details=json.dumps({
                    "manifest_id": manifest_id,
                    "publication_id": publication_id,
                    "source_observation_id": observation_id,
                    "capture_checksum": capture.capture_checksum,
                }, sort_keys=True),
                created_at=now,
            ))
            return capture

    def read(self, capture_id: str, *, session: Session | None = None) -> Per36DiagnosticCapture:
        if session is not None:
            row = session.get(LedgerParityArtifact, capture_id)
        else:
            with Session(self.engine) as owned:
                row = owned.get(LedgerParityArtifact, capture_id)
        if row is None or row.stream_key != PER36_DIAGNOSTIC_CAPTURE_STREAM:
            raise ValueError("per36 diagnostic capture not found")
        try:
            document = json.loads(row.report)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("per36 diagnostic capture is invalid") from error
        if not isinstance(document, Mapping):
            raise ValueError("per36 diagnostic capture is invalid")
        capture = self._validate_document(document)
        if (
            capture.capture_id != row.artifact_id
            or capture.publication_id != row.publication_id
            or capture.payload_checksum != row.payload_checksum
            or assume_utc(row.cutoff) != capture.cutoff
        ):
            raise ValueError("per36 diagnostic capture binding is invalid")
        return capture


class LegacyMatchupDiagnosticCaptureRepository:
    """Append an exact, reproducible legacy snapshot before it is compared."""

    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] | None = None):
        self.engine = engine
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def record(
        self, materialization, *, publication_id: str, session: Session
    ) -> LegacyMatchupDiagnosticCapture:
        if not materialization.provider_window_identity:
            raise ValueError("legacy diagnostic capture requires provider window identity")
        def normalize(value: object) -> Mapping[str, object]:
            row = asdict(value)
            # The identical bounded identity is captured once above, not
            # repeated in every canonical fact and observation row.
            row.pop("provider_window_identity", None)
            return json.loads(json.dumps(row, sort_keys=True, default=str))
        document: dict[str, object] = {
            "season": materialization.season,
            "window": materialization.window,
            "cutoff": assume_utc(materialization.cutoff).isoformat(),
            "manifest_id": materialization.manifest_id,
            "event_catalog_publication_id": materialization.event_catalog_publication_id,
            "event_catalog_checksum": materialization.event_catalog_checksum,
            "provider_window_identity": materialization.provider_window_identity,
            "game_ids_by_team": {
                str(team_id): sorted(str(game_id) for game_id in game_ids)
                for team_id, game_ids in sorted(materialization.game_ids_by_team.items())
            },
            "facts": sorted(
                (normalize(fact) for fact in materialization.facts),
                key=lambda row: json.dumps(row, sort_keys=True),
            ),
            "observations": sorted(
                (normalize(observation) for observation in materialization.observations),
                key=lambda row: json.dumps(row, sort_keys=True),
            ),
        }
        capture_checksum = hashlib.sha256(
            _canonical_capture_payload(document).encode("utf-8")
        ).hexdigest()
        capture_id = str(uuid4())
        report = {**document, "capture_id": capture_id, "capture_checksum": capture_checksum}
        session.add(LedgerParityArtifact(
            artifact_id=capture_id,
            publication_id=publication_id,
            payload_checksum=capture_checksum,
            stream_key=LEGACY_MATCHUP_DIAGNOSTIC_CAPTURE_STREAM,
            season=materialization.season,
            cutoff=assume_utc(materialization.cutoff),
            status="exact",
            report=json.dumps(report, sort_keys=True, separators=(",", ":")),
            created_at=self.clock(),
        ))
        session.flush()
        return LegacyMatchupDiagnosticCapture(
            capture_id=capture_id, capture_checksum=capture_checksum,
            season=materialization.season, window=materialization.window,
            cutoff=assume_utc(materialization.cutoff),
            manifest_id=str(materialization.manifest_id),
            event_catalog_publication_id=str(materialization.event_catalog_publication_id),
            event_catalog_checksum=str(materialization.event_catalog_checksum),
            provider_window_identity=materialization.provider_window_identity,
            document=report,
        )


def matchup_parity_artifact_is_activatable(
    artifact: LedgerParityArtifact,
    *,
    stream_key: str | None = None,
    session: Session | None = None,
) -> bool:
    """Return whether an artifact contains only exact/soft matchup evidence."""

    if stream_key is not None and artifact.stream_key != stream_key:
        return False
    try:
        document = json.loads(artifact.report)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(document, Mapping):
        return False
    if getattr(artifact, "decision", None) == "rejected":
        return False
    if artifact.stream_key not in _MATCHUP_STREAMS:
        differences = document.get("differences", ())
        if not isinstance(differences, list):
            return False
        if not differences:
            if document.get("status") != "exact":
                return False
            if artifact.stream_key != "player_per36":
                return True
            lineage = document.get("lineage")
            if not isinstance(lineage, Mapping) or session is None:
                return False
            required = (
                "capture_id", "capture_checksum", "request_checksum",
                "source_observation_id",
            )
            if any(not isinstance(lineage.get(key), str) for key in required):
                return False
            try:
                capture = Per36DiagnosticCaptureRepository(
                    session.get_bind()
                ).read(str(lineage["capture_id"]), session=session)
            except ValueError:
                return False
            return (
                capture.publication_id == artifact.publication_id
                and capture.payload_checksum == artifact.payload_checksum
                and capture.capture_checksum == lineage["capture_checksum"]
                and capture.request_checksum == lineage["request_checksum"]
                and capture.source_observation_id
                == lineage["source_observation_id"]
            )
        if any(
            not isinstance(difference, Mapping)
            or difference.get("classification") != "semantic_difference"
            for difference in differences
        ):
            return False
        return semantic_rule_is_approved(
            document.get("semantic_rule"),
            document.get("semantic_rule_reason"),
        )
    # Rejected evidence is permanently ineligible, even if an operator left
    # the original status as exact.  This keeps a historical decision from
    # silently becoming activation authority on a later rerun.
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
    def game_set_checksum(value) -> str | None:
        if not isinstance(value, Mapping):
            return None
        game_ids: list[str] = []
        for team_id, ids in value.items():
            if not isinstance(team_id, str) or not isinstance(ids, list):
                return None
            if any(not isinstance(game_id, str) or not game_id for game_id in ids):
                return None
            game_ids.extend(ids)
        return LedgerLineage.for_game_ids(game_ids)

    legacy_checksum = game_set_checksum(document["legacy_game_ids_by_team"])
    ledger_checksum = game_set_checksum(document["ledger_game_ids_by_team"])
    lineage = document.get("legacy_capture")
    expected_team_ids = {
        str(team_id) for team_id in NBA_TEAM_ID_TO_TRICODE
    }
    report_team_ids = document.get("expected_team_ids")
    legacy_team_ids = set(document["legacy_game_ids_by_team"])
    ledger_team_ids = set(document["ledger_game_ids_by_team"])
    complete_report = all(
        document.get(field) is True
        for field in (
            "league_complete",
            "team_identities_exact",
            "game_sets_exact",
            "cutoffs_aligned",
            "rankings_deterministic",
        )
    )
    if (
        document.get("ledger_publication_id") != artifact.publication_id
        or document.get("ledger_payload_checksum") != artifact.payload_checksum
        or not isinstance(artifact.payload_checksum, str)
        or len(artifact.payload_checksum) != 64
        or not all(
            isinstance(document.get(key), str) and document.get(key).strip()
            for key in (
                "legacy_game_set_checksum", "ledger_game_set_checksum",
                "legacy_manifest_id", "legacy_event_catalog_publication_id",
                "legacy_event_catalog_checksum", "ledger_manifest_id",
                "ledger_event_catalog_publication_id", "ledger_event_catalog_checksum",
            )
        )
        or not isinstance(lineage, Mapping)
        or not _is_sha256(lineage.get("capture_checksum"))
        or not isinstance(lineage.get("capture_id"), str)
        or document["legacy_game_ids_by_team"] != document["ledger_game_ids_by_team"]
        or legacy_checksum is None
        or ledger_checksum is None
        or document["legacy_game_set_checksum"] != legacy_checksum
        or document["ledger_game_set_checksum"] != ledger_checksum
        or not complete_report
        or not isinstance(report_team_ids, list)
        or len(report_team_ids) != 30
        or {str(team_id) for team_id in report_team_ids} != expected_team_ids
        or legacy_team_ids != expected_team_ids
        or ledger_team_ids != expected_team_ids
        or (
            document.get("window") == "l15"
            and any(
                len(document["legacy_game_ids_by_team"][team_id]) != 15
                or len(document["ledger_game_ids_by_team"][team_id]) != 15
                for team_id in expected_team_ids
            )
        )
    ):
        return False
    if session is not None:
        publication = session.get(PublicationVersion, artifact.publication_id)
        capture = session.get(LedgerParityArtifact, lineage["capture_id"])
        try:
            capture_document = json.loads(capture.report) if capture is not None else None
            capture_payload = dict(capture_document)
            embedded_capture_id = capture_payload.pop("capture_id")
            embedded_capture_checksum = capture_payload.pop("capture_checksum")
            recomputed_capture_checksum = hashlib.sha256(
                _canonical_capture_payload(capture_payload).encode("utf-8")
            ).hexdigest()
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return False
        if (
            publication is None
            or publication.checksum != artifact.payload_checksum
            or not publication_payload_matches_checksum(
                publication.payload, publication.checksum
            )
            or capture is None
            or capture.stream_key != LEGACY_MATCHUP_DIAGNOSTIC_CAPTURE_STREAM
            or capture.payload_checksum != lineage["capture_checksum"]
            or embedded_capture_id != lineage["capture_id"]
            or embedded_capture_checksum != lineage["capture_checksum"]
            or recomputed_capture_checksum != lineage["capture_checksum"]
            or capture.season != artifact.season
            or assume_utc(capture.cutoff) != assume_utc(artifact.cutoff)
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
    if not classifications or any(
        classification in _MATCHUP_HARD_CLASSIFICATIONS
        for classification in classifications
    ):
        return False
    return semantic_rule_is_approved(
        document.get("semantic_rule"),
        document.get("semantic_rule_reason"),
    )


def matchup_parity_cohort_is_activatable(
    session: Session,
    *,
    season: str,
    cutoff,
    candidate_publication_id: str,
    artifact_id: str,
) -> bool:
    """Require one exact Season+L15, traditional+assist evidence cohort.

    Artifact history is append-only: rejected, superseded, or failed reruns
    must not make an otherwise valid cohort unusable.  Select the newest
    *fully validated* artifact per stream by creation time and ID, then bind
    the activation request to that exact selected member.
    """

    def validated(row: LedgerParityArtifact, stream_key: str):
        if not matchup_parity_artifact_is_activatable(
            row, stream_key=stream_key, session=session
        ):
            return None
        if row.decision == "rejected" or (
            row.status != "exact" and row.decision != "approved"
        ):
            return None
        publication = session.get(PublicationVersion, row.publication_id)
        if (
            publication is None
            or publication.status not in {"candidate", "active"}
            or publication.season != season
            or assume_utc(publication.cutoff) != target_cutoff
            or row.payload_checksum != publication.checksum
            or not publication_payload_matches_checksum(
                publication.payload, publication.checksum
            )
        ):
            return None
        try:
            document = json.loads(row.report)
            authority = verify_publication_authority(session, publication)
            if (
                document.get("ledger_manifest_id") != authority.manifest_id
                or document.get("ledger_event_catalog_publication_id")
                != authority.event_catalog_publication_id
                or document.get("ledger_event_catalog_checksum")
                != authority.event_catalog_checksum
                or document.get("legacy_manifest_id") != authority.manifest_id
                or document.get("legacy_event_catalog_publication_id")
                != authority.event_catalog_publication_id
                or document.get("legacy_event_catalog_checksum")
                != authority.event_catalog_checksum
            ):
                return None
            if (
                not isinstance(document.get("cutoff"), str)
                or assume_utc(datetime.fromisoformat(document["cutoff"]))
                != assume_utc(row.cutoff)
            ):
                return None
            from app.services.matchup_parity import _decode_ledger_rows

            ledger_rows = _decode_ledger_rows(
                publication.payload, stream_key=stream_key
            )
            expected_ids = {
                str(ledger_row.team_id): sorted(ledger_row.game_ids)
                for ledger_row in ledger_rows
            }
            if document.get("ledger_game_ids_by_team") != expected_ids:
                return None
        except (
            PublicationGovernanceUnavailable,
            TypeError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ):
            return None
        return authority

    target_cutoff = assume_utc(cutoff)
    # Lock the complete candidate history before selecting.  The lock order is
    # deterministic so concurrent activation attempts cannot validate one
    # generation and mutate a different one between reads.
    all_rows = session.scalars(
        select(LedgerParityArtifact).where(
            LedgerParityArtifact.season == season,
            LedgerParityArtifact.stream_key.in_(_MATCHUP_STREAMS),
        ).order_by(
            LedgerParityArtifact.stream_key,
            LedgerParityArtifact.created_at,
            LedgerParityArtifact.artifact_id,
        ).with_for_update()
    ).all()
    history_publication_ids = {
        row.publication_id for row in all_rows if row.publication_id
    }
    # Lock every authority row referenced by the history before any artifact
    # is considered.  This closes the read/lock gap around a concurrent
    # supersession or payload mutation.
    history_publications = session.scalars(
        select(PublicationVersion).where(
            PublicationVersion.publication_id.in_(history_publication_ids)
        ).order_by(PublicationVersion.publication_id).with_for_update()
        .execution_options(populate_existing=True)
    ).all() if history_publication_ids else []
    history_manifest_ids = {
        publication.manifest_id
        for publication in history_publications
        if publication.manifest_id
    }
    history_catalog_ids = {
        publication.event_catalog_publication_id
        for publication in history_publications
        if publication.event_catalog_publication_id
    }
    if history_manifest_ids:
        session.scalars(
            select(CollectionManifest).where(
                CollectionManifest.manifest_id.in_(history_manifest_ids)
            ).order_by(CollectionManifest.manifest_id).with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    if history_catalog_ids:
        session.scalars(
            select(CatalogPublication).where(
                CatalogPublication.publication_id.in_(history_catalog_ids)
            ).order_by(CatalogPublication.publication_id).with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    pointer_streams = tuple(sorted((*_MATCHUP_STREAMS, "canonical_game_ledger")))
    session.scalars(
        select(PublicationPointer).where(
            PublicationPointer.stream_key.in_(pointer_streams)
        ).order_by(PublicationPointer.stream_key).with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    rows_by_stream: dict[str, list[LedgerParityArtifact]] = {
        stream_key: sorted(
            (
                row for row in all_rows
                if row.stream_key == stream_key
                and assume_utc(row.cutoff) == target_cutoff
            ),
            key=lambda row: (assume_utc(row.created_at), row.artifact_id),
            reverse=True,
        )
        for stream_key in _MATCHUP_STREAMS
    }
    selected: dict[str, tuple[LedgerParityArtifact, object]] = {}
    for stream_key, candidates in rows_by_stream.items():
        for row in candidates:
            authority = validated(row, stream_key)
            if authority is not None:
                selected[stream_key] = (row, authority)
                break
    if set(selected) != _MATCHUP_STREAMS:
        return False

    authorities = {
        (
            authority.manifest_id,
            authority.event_catalog_publication_id,
            authority.event_catalog_checksum,
        )
        for _, authority in selected.values()
    }
    if len(authorities) != 1:
        return False

    selected_publication_ids = {
        row.publication_id for row, _ in selected.values()
    }
    publications = session.scalars(
        select(PublicationVersion).where(
            PublicationVersion.publication_id.in_(selected_publication_ids)
        ).order_by(PublicationVersion.publication_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if {row.publication_id for row in publications} != selected_publication_ids:
        return False
    selected_authority = next(iter(authorities))
    manifest_id, catalog_id, catalog_checksum = selected_authority
    manifests = session.scalars(
        select(CollectionManifest).where(
            CollectionManifest.manifest_id == manifest_id
        ).with_for_update().execution_options(populate_existing=True)
    ).all()
    catalogs = session.scalars(
        select(CatalogPublication).where(
            CatalogPublication.publication_id == catalog_id
        ).with_for_update().execution_options(populate_existing=True)
    ).all()
    if (
        len(manifests) != 1
        or len(catalogs) != 1
        or manifests[0].status != "active"
        or manifests[0].checksum is None
        or manifests[0].event_catalog_publication_id != catalog_id
        or manifests[0].event_catalog_checksum != catalog_checksum
        or catalogs[0].checksum != catalog_checksum
        or not publication_payload_matches_checksum(
            catalogs[0].payload, catalogs[0].checksum
        )
    ):
        return False
    # Pointers were locked before validation and remain held until the caller
    # mutates the selected stream pointer in this activation transaction.

    supplied = session.get(LedgerParityArtifact, artifact_id)
    if supplied is None or supplied.stream_key not in _MATCHUP_STREAMS:
        return False
    selected_row, _ = selected[supplied.stream_key]
    return (
        selected_row.artifact_id == supplied.artifact_id
        and supplied.publication_id == candidate_publication_id
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
    semantic_rule: str | None = None
    semantic_rule_reason: str | None = None

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
        lineage: Mapping[str, object] | None = None,
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
                    "status": report.status,
                    "semantic_rule": report.semantic_rule,
                    "semantic_rule_reason": report.semantic_rule_reason,
                    "differences": [asdict(difference) for difference in report.differences],
                    **({"lineage": dict(lineage)} if lineage is not None else {}),
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
        legacy_capture: LegacyMatchupDiagnosticCapture,
    ) -> LedgerParityArtifact:
        """Persist one matchup materializer dual-run report as parity evidence.

        The artifact is bound to the exact report window, surface, and aware
        immutable cutoff: the ``stream_key`` must name the report's own surface
        and window, and ``cutoff`` must equal the report's exact aware cutoff,
        so an L15 artifact can never authorize a Season stream.
        """

        if not publication_id or len(payload_checksum) != 64:
            raise ValueError("candidate publication and payload checksum are required")
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
            status=(
                "pending_adjudication"
                if report.hard_failure or report.adjudication_required
                else "exact"
            ),
            report=json.dumps({
                **report.to_dict(),
                "legacy_capture": {
                    "capture_id": legacy_capture.capture_id,
                    "capture_checksum": legacy_capture.capture_checksum,
                },
            }, sort_keys=True, default=str),
            created_at=self.clock(),
            decision="rejected" if report.hard_failure else None,
            adjudication_reason=(
                "automatic hard parity failure" if report.hard_failure else None
            ),
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
        if (
            len(reason.strip()) < 3
            or len(reason) > 255
            or not actor.strip()
            or len(actor) > 128
        ):
            raise ValueError("actor and reason are required")
        now = self.clock()
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            row = session.scalar(
                select(LedgerParityArtifact)
                .where(LedgerParityArtifact.artifact_id == artifact_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None:
                raise ValueError("parity artifact not found")
            if row.decision is not None:
                raise ValueError("parity artifact decision is immutable")
            if decision == "approved" and (
                len(reason.strip()) < 20
                or not matchup_parity_artifact_is_activatable(row)
            ):
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
    # The denominator and participation count are part of the Season parity
    # contract; rates alone cannot prove that a provider used the governed
    # player window.
    "minutes": ("minutes", "MIN"),
    "game_count": ("game_count", "GP", "G"),
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
    exact_fields: frozenset[str] = frozenset(),
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
            equal = (
                current_value == legacy_value
                if field_name in exact_fields
                else _values_equal(current_value, legacy_value, tolerance)
            )
            if not equal:
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
    semantic_rule: str | None = None,
    semantic_rule_reason: str | None = None,
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
            exact_fields=frozenset({"game_count"}),
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
        semantic_rule=semantic_rule,
        semantic_rule_reason=semantic_rule_reason,
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
    semantic_rule: str | None = None,
    semantic_rule_reason: str | None = None,
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
        semantic_rule=semantic_rule,
        semantic_rule_reason=semantic_rule_reason,
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
