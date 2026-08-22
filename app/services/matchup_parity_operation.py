"""Run the bounded, non-activating Matchups parity operation.

The compare command is deliberately one bounded operation.  It resolves one
exact manifest and immutable Event Catalog authority, reads the persisted
legacy diagnostics, validates inactive candidate Publications, records parity
artifacts, and writes a sanitized summary.  It never advances a Publication
pointer or enables a stream.

Example::

    ./scripts/matchup_parity.py compare \
        --database-url "$DATABASE_URL" \
        --season 2025-26 \
        --manifest-id "<manifest-id>" \
        --actor "operator@example.com" \
        --output parity-summary.json \
        --target candidate \
        --per36-capture-id "<capture id>"

The command composes all five inactive candidates in its transaction; callers
do not supply or select candidate publication IDs.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import inspect, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app.domain.publication_integrity import (  # noqa: E402
    publication_payload_matches_checksum,
)
from app.domain.utc import assume_utc  # noqa: E402
from app.errors import ProviderUnavailableError  # noqa: E402
from app.domain.slate_time import slate_date_for_instant  # noqa: E402
from app.migrations import (  # noqa: E402
    MIGRATIONS,
    MIGRATION_TABLE_NAME,
    run_migrations,
)
from app.models.collection_control import (  # noqa: E402
    ActiveSeason,
    AuditEvent,
    CatalogPublication,
    CollectionManifest,
    PublicationPointer,
    PublicationStream,
    PublicationVersion,
    CollectionObservation,
)
from app.models.canonical_game_ledger import (  # noqa: E402
    CanonicalGameLedgerGame,
    LedgerParityArtifact,
)
from app.services.canonical_game_ledger import (  # noqa: E402
    CanonicalGameLedgerRepository,
)
from app.services.database_first_activation import (  # noqa: E402
    decode_player_per36,
)
from app.services.ledger_derivations import (  # noqa: E402
    PlayerPer36Fact,
    derive_player_per36_facts,
    materialize_assist_location_window,
    materialize_team_window,
)
from app.services.ledger_parity import (  # noqa: E402
    LedgerParityArtifactRepository,
    LedgerParityReport,
    PER36_RAW_FIELDS,
    PER36_RATE_FIELDS,
    Per36DiagnosticCaptureRepository,
    SemanticDifference,
)
from app.services.ledger_lineage import LedgerLineage  # noqa: E402
from app.services.ledger_materialization import (  # noqa: E402
    LedgerMaterializationService,
)
from app.services.ledger_runtime import (  # noqa: E402
    ActiveManifestLedgerGovernanceReader,
)
from app.services.matchup_parity import (  # noqa: E402
    MatchupParityError,
    MatchupParityRunner,
    StoredLegacyMatchupSource,
    resolve_matchup_publication,
)
from app.services.nba_stats_adapter import NBAStatsAdapter  # noqa: E402
from app.services.per36_provider_evidence import (  # noqa: E402
    Per36ProviderEvidenceCollector,
)
from app.services.matchup_authority import (  # noqa: E402
    resolve_unique_matchup_authority,
)
from app.services.publication_authority import (  # noqa: E402
    verify_publication_authority,
)
from app.services.team_matchup_repository import (  # noqa: E402
    TeamMatchupRepository,
)
from app.services.team_matchup_publications import (  # noqa: E402
    PublicationGovernanceUnavailable,
)
from app.services.collection_control import PublicationService  # noqa: E402


EXIT_EXACT = 0
EXIT_PENDING_ADJUDICATION = 2
EXIT_INVALID_EVIDENCE = 3
MAX_CAPTURE_INPUT_BYTES = 5 * 1024 * 1024
ALL_REQUIRED_STREAMS = frozenset({
    "traditional_opponent_season",
    "traditional_opponent_l15",
    "assist_locations_season",
    "assist_locations_l15",
    "player_per36",
})


class InvalidEvidenceError(ValueError):
    """The bounded operation cannot make an activation-safe comparison."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _aware_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SystemExit("timestamp must be an aware value")
    return assume_utc(parsed)


def _load_publications(path: str) -> dict[str, str]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidEvidenceError("publications_manifest_invalid") from error
    if not isinstance(document, Mapping) or not document:
        raise InvalidEvidenceError("publications_manifest_invalid")
    result: dict[str, str] = {}
    for stream_key, publication_id in document.items():
        if not isinstance(stream_key, str) or not isinstance(publication_id, str):
            raise InvalidEvidenceError("publications_manifest_invalid")
        if not stream_key.strip() or not publication_id.strip():
            raise InvalidEvidenceError("publications_manifest_invalid")
        if stream_key in result:
            raise InvalidEvidenceError("publications_manifest_duplicate_stream")
        result[stream_key] = publication_id
    return result


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _database_fingerprint(engine) -> str:
    """Return a stable fingerprint without rendering credentials."""

    try:
        rendered = engine.url.render_as_string(hide_password=True)
    except AttributeError:
        rendered = str(engine.url).split("@", 1)[-1]
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _environment() -> str:
    return (
        os.environ.get("STATSPLUS_ENV")
        or os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("ENVIRONMENT")
        or "unknown"
    )[:64]


def _migration_version(engine) -> int:
    expected = max(migration.version for migration in MIGRATIONS)
    inspector = inspect(engine)
    if MIGRATION_TABLE_NAME not in inspector.get_table_names():
        raise InvalidEvidenceError("migrations_incomplete")
    from sqlalchemy import text

    with engine.connect() as connection:
        current = connection.execute(
            text(f"SELECT MAX(version) FROM {MIGRATION_TABLE_NAME}")
        ).scalar_one_or_none()
    if int(current or 0) != expected:
        raise InvalidEvidenceError("migrations_incomplete")
    return expected


def _control_state(
    engine, *, session: Session | None = None, lock: bool = False
) -> dict[str, Any]:
    """Capture all pointers and stream gates used to prove non-mutation."""

    owned = session is None
    session = session or Session(engine)
    try:
        pointer_query = select(PublicationPointer).order_by(
            PublicationPointer.stream_key
        )
        stream_query = select(PublicationStream).order_by(
            PublicationStream.stream_key
        )
        if lock:
            pointer_query = pointer_query.with_for_update()
            stream_query = stream_query.with_for_update()
        pointers = {
            str(row.stream_key): {
                "active": row.active_publication_id,
                "previous": row.previous_publication_id,
                "fence": int(row.fence or 0),
                "updated_at": (
                    assume_utc(row.updated_at).isoformat()
                    if row.updated_at is not None
                    else None
                ),
            }
            for row in session.scalars(
                pointer_query
            )
        }
        streams = {
            str(row.stream_key): bool(row.enabled)
            for row in session.scalars(
                stream_query
            )
        }
    finally:
        if owned:
            session.close()
    return {"pointers": pointers, "streams": streams}


def _control_state_checksum(state: Mapping[str, Any]) -> str:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _manifest_preflight(engine, *, season: str, manifest_id: str):
    if season != "2025-26":
        raise InvalidEvidenceError("unsupported_season")
    with Session(engine) as session:
        active = session.scalar(
            select(ActiveSeason).where(ActiveSeason.season == season)
        )
        if (
            active is None
            or active.status != "active"
            or active.phase != "Regular Season"
        ):
            raise InvalidEvidenceError("active_regular_season_required")
        manifest = session.get(CollectionManifest, manifest_id)
        if (
            manifest is None
            or manifest.season != season
            or manifest.status != "active"
        ):
            raise InvalidEvidenceError("manifest_invalid")
        try:
            scopes = set(json.loads(manifest.scopes))
            versions = set(json.loads(manifest.accepted_versions))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise InvalidEvidenceError("manifest_invalid") from error
        if "canonical_game_ledger" not in scopes or 1 not in versions:
            raise InvalidEvidenceError("manifest_invalid")
        catalog = session.get(
            CatalogPublication, manifest.event_catalog_publication_id
        )
        if (
            catalog is None
            or catalog.catalog_type != "event"
            or not catalog.complete
            or catalog.season != season
            or catalog.cutoff != manifest.cutoff
            or catalog.checksum != manifest.event_catalog_checksum
            or not publication_payload_matches_checksum(
                catalog.payload, catalog.checksum
            )
        ):
            raise InvalidEvidenceError("event_catalog_binding_invalid")
        cutoff = assume_utc(manifest.cutoff)
        if active.cutoff is not None and assume_utc(active.cutoff) != cutoff:
            raise InvalidEvidenceError("active_season_cutoff_mismatch")
        try:
            unique_authority = resolve_unique_matchup_authority(
                session, season=season, cutoff=cutoff, lock=False,
            )
        except ValueError as error:
            raise InvalidEvidenceError(str(error)) from error
        try:
            unique_authority.require_completed_regular_season()
        except ValueError as error:
            raise InvalidEvidenceError(str(error)) from error
        if (
            unique_authority.manifest_id != manifest_id
            or unique_authority.catalog_id != catalog.publication_id
        ):
            raise InvalidEvidenceError("manifest_authority_ambiguous")

    governance_reader = ActiveManifestLedgerGovernanceReader(engine)
    try:
        governance = governance_reader.read_for_composition(
            season, cutoff, manifest_id=manifest_id
        )
    except (PublicationGovernanceUnavailable, ValueError) as error:
        raise InvalidEvidenceError("manifest_governance_invalid") from error
    if governance.manifest_id != manifest_id or assume_utc(governance.cutoff) != cutoff:
        raise InvalidEvidenceError("manifest_governance_invalid")
    if governance.event_catalog_publication_id != catalog.publication_id or (
        governance.event_catalog_checksum != catalog.checksum
    ):
        raise InvalidEvidenceError("event_catalog_binding_invalid")
    if len(governance.team_ids) != 30:
        raise InvalidEvidenceError("governed_team_roster_incomplete")
    if not governance.expected_game_ids:
        raise InvalidEvidenceError("governed_game_set_empty")
    return governance, {
        "id": manifest.manifest_id,
        "checksum": manifest.checksum,
        "cutoff": cutoff.isoformat(),
        "event_catalog_publication_id": catalog.publication_id,
        "event_catalog_checksum": catalog.checksum,
    }, _migration_version(engine)


def _candidate_preflight(
    session: Session,
    *,
    publication_id: str,
    stream_key: str,
    season: str,
    cutoff: datetime,
    manifest_id: str,
    catalog_id: str,
    catalog_checksum: str,
):
    publication = session.scalar(
        select(PublicationVersion)
        .where(PublicationVersion.publication_id == publication_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        publication is None
        or publication.stream_key != stream_key
        or publication.season != season
        or assume_utc(publication.cutoff) != cutoff
        or publication.status != "candidate"
        or not publication_payload_matches_checksum(
            publication.payload, publication.checksum
        )
        or publication.manifest_id != manifest_id
        or publication.event_catalog_publication_id != catalog_id
        or publication.event_catalog_checksum != catalog_checksum
    ):
        raise InvalidEvidenceError("candidate_provenance_invalid")
    try:
        authority = verify_publication_authority(session, publication)
    except PublicationGovernanceUnavailable as error:
        raise InvalidEvidenceError("candidate_provenance_invalid") from error
    if (
        authority.manifest_id != manifest_id
        or authority.event_catalog_publication_id != catalog_id
        or authority.event_catalog_checksum != catalog_checksum
    ):
        raise InvalidEvidenceError("candidate_provenance_invalid")
    return publication


def _required_streams(window: str) -> frozenset[str]:
    if window not in {"season", "l15"}:
        raise InvalidEvidenceError("window_invalid")
    streams = {
        f"traditional_opponent_{window}",
        f"assist_locations_{window}",
    }
    if window == "season":
        streams.add("player_per36")
    return frozenset(streams)


class _UnavailableDiagnosticReader:
    def read(self, _stream_key, *, session=None):
        raise RuntimeError("bounded comparison records diagnostics separately")


def _compose_candidate_set(
    engine, *, session: Session, governance, season: str, cutoff: datetime,
) -> dict[str, PublicationVersion]:
    """Compose all five exact inactive candidates from the governed ledger."""

    repository = CanonicalGameLedgerRepository(engine)
    manifest = session.scalar(
        select(CollectionManifest)
        .where(CollectionManifest.manifest_id == governance.manifest_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    catalog = session.scalar(
        select(CatalogPublication)
        .where(
            CatalogPublication.publication_id
            == governance.event_catalog_publication_id
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        manifest is None
        or catalog is None
        or manifest.status != "active"
        or manifest.event_catalog_checksum != catalog.checksum
        or catalog.checksum != governance.event_catalog_checksum
    ):
        raise InvalidEvidenceError("manifest_governance_invalid")
    game_ids = tuple(sorted(map(str, governance.expected_game_ids)))
    locked_game_ids = tuple(session.scalars(
        select(CanonicalGameLedgerGame.game_id)
        .where(CanonicalGameLedgerGame.game_id.in_(game_ids))
        .order_by(CanonicalGameLedgerGame.game_id)
        .with_for_update()
    ))
    if locked_game_ids != game_ids:
        raise InvalidEvidenceError("ledger_game_set_incomplete")
    games = tuple(
        repository.get_game(game_id, connection=session.connection())
        for game_id in game_ids
    )
    if any(game is None for game in games):
        raise InvalidEvidenceError("ledger_game_set_incomplete")
    source_observation_ids = tuple(sorted({
        game.source_observation_id
        for game in games if game is not None
    }))
    locked_observation_ids = tuple(session.scalars(
        select(CollectionObservation.observation_id)
        .where(CollectionObservation.observation_id.in_(source_observation_ids))
        .order_by(CollectionObservation.observation_id)
        .with_for_update()
    ))
    if locked_observation_ids != source_observation_ids:
        raise InvalidEvidenceError("ledger_source_observation_incomplete")
    LedgerMaterializationService(
        repository,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=_UnavailableDiagnosticReader(),
        publication_service=PublicationService(engine),
    ).compose(
        tuple(game for game in games if game is not None),
        season=season,
        as_of=slate_date_for_instant(cutoff),
        cutoff=cutoff,
        expected_game_ids=frozenset(map(str, governance.expected_game_ids)),
        expected_l15_game_ids={
            int(team_id): frozenset(map(str, game_ids))
            for team_id, game_ids in governance.expected_l15_game_ids.items()
        },
        team_ids=frozenset(map(int, governance.team_ids)),
        require_assist_locations=False,
        activate=False,
        candidate_stream_keys=ALL_REQUIRED_STREAMS,
        session=session,
    )
    rows = {}
    for stream_key in sorted(ALL_REQUIRED_STREAMS):
        row = session.scalar(
            select(PublicationVersion)
            .where(
                PublicationVersion.stream_key == stream_key,
                PublicationVersion.season == season,
                PublicationVersion.cutoff == cutoff,
                PublicationVersion.status == "candidate",
            )
            .order_by(PublicationVersion.version.desc())
            .limit(1)
        )
        if row is None:
            raise InvalidEvidenceError("candidate_composition_incomplete")
        rows[stream_key] = _candidate_preflight(
            session,
            publication_id=row.publication_id,
            stream_key=stream_key,
            season=season,
            cutoff=cutoff,
            manifest_id=governance.manifest_id,
            catalog_id=governance.event_catalog_publication_id,
            catalog_checksum=governance.event_catalog_checksum,
        )
    return rows


def _compare_candidate_per36(
    engine,
    *,
    repository: CanonicalGameLedgerRepository | None = None,
    publication: PublicationVersion,
    governance,
    season: str,
    cutoff: datetime,
    provider_end_date: str,
    capture_id: str,
    session: Session,
) -> tuple[dict[str, Any], str | None]:
    """Compare a candidate against scoped immutable per-36 evidence."""

    try:
        candidate_payload = (
            json.loads(publication.payload)
            if isinstance(publication.payload, str)
            else publication.payload
        )
        candidate_rows = tuple(decode_player_per36(candidate_payload, season=season))
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise InvalidEvidenceError("player_per36_candidate_invalid") from error

    game_ids = frozenset(
        str(game_id)
        for game_ids_for_team in governance.expected_season_game_ids.values()
        for game_id in game_ids_for_team
    )
    repository = repository or CanonicalGameLedgerRepository(engine)
    games = tuple(
        repository.get_game(game_id, connection=session.connection())
        for game_id in sorted(game_ids)
    )
    if any(game is None for game in games) or len(games) != len(game_ids):
        raise InvalidEvidenceError("ledger_game_set_incomplete")
    canonical_games = tuple(game for game in games if game is not None)
    expected_game_ids = sorted(game_ids)
    try:
        capture = Per36DiagnosticCaptureRepository(engine).read(
            capture_id, session=session
        )
    except ValueError as error:
        raise InvalidEvidenceError("player_per36_diagnostic_capture_invalid") from error
    if (
        capture.publication_id != publication.publication_id
        or capture.payload_checksum != publication.checksum
        or capture.season != season
        or capture.cutoff != assume_utc(cutoff)
        or capture.manifest_id != governance.manifest_id
        or capture.event_catalog_publication_id != governance.event_catalog_publication_id
        or capture.event_catalog_checksum != governance.event_catalog_checksum
        or capture.game_set_checksum != LedgerLineage.for_game_ids(expected_game_ids)
        or capture.provider_window_identity.get("game_ids") != expected_game_ids
        or capture.provider_window_identity.get("request_checksum") != capture.request_checksum
        or capture.provider_window_identity.get("provider_end_date")
        != provider_end_date
        or capture.provider_window_identity.get("returned_game_count")
        != len(expected_game_ids)
        or set(
            capture.provider_window_identity.get(
                "event_catalog_mapping_trace", {}
            ).values()
        ) != set(expected_game_ids)
    ):
        raise InvalidEvidenceError("player_per36_diagnostic_capture_authority_invalid")

    expected_rows = derive_player_per36_facts(canonical_games, season=season)
    expected_by_id = {row.player_id: row for row in expected_rows}
    candidate_by_id = {row.player_id: row for row in candidate_rows}
    if set(expected_by_id) != set(candidate_by_id):
        raise InvalidEvidenceError("player_per36_candidate_identity_mismatch")
    fields = tuple(PlayerPer36Fact.__dataclass_fields__)
    for player_id, expected in expected_by_id.items():
        actual = candidate_by_id[player_id]
        for field in fields:
            left = getattr(expected, field)
            right = getattr(actual, field)
            if isinstance(left, float):
                matches = (
                    isinstance(right, (int, float))
                    and not isinstance(right, bool)
                    and math.isclose(
                        left, float(right), rel_tol=1e-9, abs_tol=1e-9
                    )
                )
            else:
                matches = left == right
            if not matches:
                raise InvalidEvidenceError("player_per36_candidate_mismatch")

    capture_by_id = {int(row["player_id"]): row for row in capture.rows}
    differences: list[SemanticDifference] = []
    for player_id in sorted(set(expected_by_id) | set(capture_by_id)):
        if player_id not in expected_by_id or player_id not in capture_by_id:
            differences.append(SemanticDifference(
                identity=f"per36:{player_id}", field="player_id",
                ledger_value=player_id if player_id in expected_by_id else None,
                legacy_value=player_id if player_id in capture_by_id else None,
                classification="identity_mismatch",
            ))
    raw_fields_by_player: dict[int, dict[str, int]] = {}
    for game in canonical_games:
        for fact in game.player_facts:
            totals = raw_fields_by_player.setdefault(
                fact.player_id, {field: 0 for field in PER36_RAW_FIELDS}
            )
            for field in PER36_RAW_FIELDS:
                totals[field] += int(getattr(fact, field))
    for player_id in sorted(set(expected_by_id) & set(capture_by_id)):
        expected = expected_by_id[player_id]
        actual = capture_by_id[player_id]
        expected_raw = raw_fields_by_player[player_id]
        for field, value in expected_raw.items():
            if actual[field] != value:
                differences.append(SemanticDifference(
                    identity=f"per36:{player_id}", field=field,
                    ledger_value=value, legacy_value=actual[field],
                    classification="raw_count_difference",
                ))
        if actual["game_count"] != expected.game_count:
            differences.append(SemanticDifference(
                identity=f"per36:{player_id}", field="game_count",
                ledger_value=expected.game_count, legacy_value=actual["game_count"],
                classification="game_count_difference",
            ))
        if actual["team_ids_at_game"] != list(expected.team_ids_at_game):
            differences.append(SemanticDifference(
                identity=f"per36:{player_id}", field="team_ids_at_game",
                ledger_value=list(expected.team_ids_at_game),
                legacy_value=actual["team_ids_at_game"],
                classification="team_identity_difference",
            ))
        if not math.isclose(
            float(actual["minutes"]), expected.minutes, rel_tol=1e-9, abs_tol=1e-9
        ):
            differences.append(SemanticDifference(
                identity=f"per36:{player_id}", field="minutes",
                ledger_value=expected.minutes, legacy_value=actual["minutes"],
                classification="minutes_difference",
            ))
    provider_rate_difference_count = sum(
        1
        for player_id in sorted(set(expected_by_id) & set(capture_by_id))
        for expected in (expected_by_id[player_id],)
        for field in PER36_RATE_FIELDS
        if not math.isclose(
            float(capture_by_id[player_id][field]),
            float(getattr(expected, field)),
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
    )

    artifact = LedgerParityArtifactRepository(engine).record(
        "player_per36",
        cutoff=cutoff,
        report=LedgerParityReport(
            season=season,
            game_count=len(canonical_games),
            compared_count=len(expected_rows),
            differences=tuple(differences),
            adjudication_required=bool(differences),
        ),
        publication_id=publication.publication_id,
        payload_checksum=publication.checksum,
        session=session,
        lineage={
            "capture_id": capture.capture_id,
            "capture_checksum": capture.capture_checksum,
            "request_checksum": capture.request_checksum,
            "source_observation_id": capture.source_observation_id,
        },
    )
    return {
        "stream": "player_per36",
        "status": "pending_adjudication" if differences else "exact",
        "game_count": len(canonical_games),
        "compared_count": len(expected_rows),
        "difference_count": len(differences),
        "difference_classifications": dict(sorted(Counter(
            difference.classification for difference in differences
        ).items())),
        "publication_id": publication.publication_id,
        "payload_checksum": publication.checksum,
        "provider_rate_difference_count": provider_rate_difference_count,
    }, artifact.artifact_id


def _validate_matchup_candidate_composition(
    engine,
    *,
    session: Session,
    publications: Mapping[str, PublicationVersion],
    governance,
    season: str,
    cutoff: datetime,
) -> None:
    """Prove each inactive candidate is composed from the governed ledger.

    Candidate IDs and checksums establish identity, but do not prove that a
    payload was produced from the current canonical game set.  Recompose all
    four matchup windows through the pure governed derivation seam and compare
    every candidate row before parity artifacts can be recorded.
    """

    game_ids = frozenset(str(game_id) for game_id in governance.expected_game_ids)
    repository = CanonicalGameLedgerRepository(engine)
    games = tuple(
        repository.get_game(game_id, connection=session.connection())
        for game_id in sorted(game_ids)
    )
    if any(game is None for game in games) or len(games) != len(game_ids):
        raise InvalidEvidenceError("ledger_game_set_incomplete")
    canonical_games = tuple(game for game in games if game is not None)
    for window in ("season", "l15"):
        expected_game_ids_by_team = (
            governance.expected_l15_game_ids
            if window == "l15"
            else governance.expected_season_game_ids
        )
        window_game_ids = frozenset(
            str(game_id)
            for game_ids_for_team in expected_game_ids_by_team.values()
            for game_id in game_ids_for_team
        )
        window_games = tuple(
            game for game in canonical_games if game.game_id in window_game_ids
        )
        for surface in ("traditional", "assist_locations"):
            stream_key = f"{surface if surface == 'assist_locations' else 'traditional_opponent'}_{window}"
            publication = resolve_matchup_publication(
                session,
                publication_id=publications[stream_key].publication_id,
                stream_key=stream_key,
                season=season,
                cutoff=cutoff,
                manifest_id=governance.manifest_id,
                event_catalog_publication_id=governance.event_catalog_publication_id,
                event_catalog_checksum=governance.event_catalog_checksum,
            )
            if surface == "traditional":
                materialization = materialize_team_window(
                    window_games,
                    season=season,
                    as_of=cutoff.date(),
                    window_games=15 if window == "l15" else None,
                    expected_game_ids=window_game_ids,
                    expected_team_game_ids=(
                        {int(team_id): frozenset(map(str, ids)) for team_id, ids in expected_game_ids_by_team.items()}
                        if window == "l15" else None
                    ),
                    team_ids=frozenset(int(team_id) for team_id in governance.team_ids),
                )
            else:
                try:
                    materialization = materialize_assist_location_window(
                        window_games,
                        season=season,
                        as_of=cutoff.date(),
                        window_games=15 if window == "l15" else None,
                        expected_game_ids=window_game_ids,
                        expected_team_game_ids=(
                            {int(team_id): frozenset(map(str, ids)) for team_id, ids in expected_game_ids_by_team.items()}
                            if window == "l15" else None
                        ),
                        team_ids=frozenset(int(team_id) for team_id in governance.team_ids),
                    )
                except ValueError:
                    if publication.rows:
                        raise InvalidEvidenceError(
                            "candidate_composition_assist_evidence_invalid"
                        )
                    # A bound empty candidate is the precise dependent
                    # unavailable result for missing optional assist
                    # primitives. Healthy traditional/per-36 candidates in
                    # this bounded transaction remain independently valid.
                    continue
            if not materialization.complete:
                if surface == "assist_locations" and not publication.rows:
                    continue
                raise InvalidEvidenceError("ledger_matchup_composition_incomplete")
            expected_by_id = {row.team_id: row for row in materialization.teams}
            actual_by_id = {row.team_id: row for row in publication.rows}
            if set(expected_by_id) != set(actual_by_id):
                raise InvalidEvidenceError("candidate_composition_team_identity_invalid")
            for team_id, expected in expected_by_id.items():
                actual = actual_by_id[team_id]
                if (
                    actual.team_tricode != expected.team_tricode
                    or actual.game_ids != tuple(expected.game_ids)
                    or actual.counts.keys() != expected.counts.keys()
                ):
                    raise InvalidEvidenceError("candidate_composition_identity_invalid")
                for metric in expected.counts:
                    if actual.counts[metric] != expected.counts[metric]:
                        raise InvalidEvidenceError("candidate_composition_count_mismatch")
                    if not math.isclose(
                        actual.per48[metric], expected.per48[metric],
                        rel_tol=1e-9, abs_tol=1e-9,
                    ):
                        raise InvalidEvidenceError("candidate_composition_rate_mismatch")
                    if actual.competition_rank[metric] != expected.competition_rank[metric]:
                        raise InvalidEvidenceError("candidate_composition_rank_mismatch")
                if not math.isclose(
                    actual.team_minutes, expected.team_minutes,
                    rel_tol=1e-9, abs_tol=1e-9,
                ):
                    raise InvalidEvidenceError("candidate_composition_minutes_mismatch")


def _sanitize_matchup_report(report) -> dict[str, Any]:
    classifications = Counter(
        difference.classification for difference in report.differences
    )
    return {
        "surface": report.surface,
        "window": report.window,
        "status": report.status,
        "compared_count": report.compared_count,
        "difference_count": len(report.differences),
        "difference_classifications": dict(sorted(classifications.items())),
        "expected_team_ids": sorted(report.expected_team_ids),
        "legacy_game_counts_by_team": {
            str(team_id): len(game_ids)
            for team_id, game_ids in sorted(report.legacy_game_ids_by_team.items())
        },
        "ledger_game_counts_by_team": {
            str(team_id): len(game_ids)
            for team_id, game_ids in sorted(report.ledger_game_ids_by_team.items())
        },
        "legacy_game_set_checksum": report.legacy_game_set_checksum,
        "ledger_game_set_checksum": report.ledger_game_set_checksum,
        "ledger_publication_id": report.ledger_publication_id,
        "ledger_payload_checksum": report.ledger_payload_checksum,
        "semantic_rule": report.semantic_rule,
    }


def _stage_summary(path: str, summary: Mapping[str, Any]) -> Path:
    destination = Path(path)
    if destination.exists() and destination.is_dir():
        raise OSError("summary output must be a file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(summary, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return temporary


def _publish_summary(path: str, temporary: Path) -> None:
    destination = Path(path)
    temporary.replace(destination)


def _write_summary(path: str, summary: Mapping[str, Any]) -> None:
    _publish_summary(path, _stage_summary(path, summary))


def _commit_and_publish_summary(
    transaction, session: Session, args, staged_summary: Path
) -> None:
    """Publish final evidence only after the artifact transaction commits."""

    transaction.commit()
    session.close()
    args._artifact_session = None
    args._database_transaction_committed = True
    _publish_summary(args.output, staged_summary)
    args._staged_summary = None


def _print_table(summary: Mapping[str, Any]) -> None:
    manifest = summary.get("manifest", {})
    if manifest:
        print(
            f"manifest: {manifest.get('id')} "
            f"checksum={manifest.get('checksum')} "
            f"cutoff={manifest.get('cutoff')}"
        )
        print(
            f"event catalog: {manifest.get('event_catalog_publication_id')} "
            f"checksum={manifest.get('event_catalog_checksum')}"
        )
    if summary.get("backend_git_sha"):
        print(
            f"backend git SHA: {summary['backend_git_sha']} "
            f"migration={summary.get('migration_version')} "
            f"environment={summary.get('environment')}"
        )
    print("surface                 status                compared  differences")
    print("----------------------  --------------------  --------  -----------")
    for report in summary.get("reports", ()):
        print(
            f"{report['stream']:<22}  {report['status']:<20}  "
            f"{report.get('compared_count', 0):>8}  "
            f"{report.get('difference_count', 0):>11}"
        )
    print(f"overall status: {summary.get('status', 'invalid_evidence')}")


def _print_protected_game_ids(governance) -> None:
    print("PROTECTED OPERATOR OUTPUT — exact governed game IDs; do not paste into trackers")
    for window, game_ids_by_team in (
        ("Season", governance.expected_season_game_ids),
        ("L15", governance.expected_l15_game_ids),
    ):
        print(f"{window} exact game IDs by team:")
        for team_id, game_ids in sorted(game_ids_by_team.items()):
            print(f"  {team_id}: {','.join(sorted(map(str, game_ids)))}")


def _overall_status(reports: list[Mapping[str, Any]]) -> str:
    if any(report.get("status") == "failed" for report in reports):
        return "pending_adjudication"
    if any(report.get("status") in {
        "adjudication_required", "pending_adjudication",
    } for report in reports):
        return "pending_adjudication"
    if reports and all(report.get("status") == "exact" for report in reports):
        return "exact"
    return "invalid_evidence"


def _record_compare_audit(
    engine, *, actor: str, manifest_id: str, status: str,
    reports: list[Mapping[str, Any]], session: Session | None = None,
) -> str:
    event_id = str(uuid4())
    details = {
        "status": status,
        "report_count": len(reports),
        "streams": [report.get("stream") for report in reports],
    }
    event = AuditEvent(
            event_id=event_id,
            actor=actor[:128],
            action="ledger.matchup_parity_compare",
            resource=manifest_id[:128],
            reason="bounded parity comparison",
            details=json.dumps(details, sort_keys=True),
            created_at=datetime.now(timezone.utc),
        )
    if session is not None:
        session.add(event)
    else:
        with Session(engine) as owned, owned.begin():
            owned.add(event)
    return event_id


def _bounded_compare(args, engine, *, before: Mapping[str, Any]) -> int:
    if (
        not isinstance(args.actor, str)
        or not args.actor.strip()
        or len(args.actor) > 128
    ):
        raise InvalidEvidenceError("actor_required")
    governance, manifest, migration_version = _manifest_preflight(
        engine, season=args.season, manifest_id=args.manifest_id
    )
    print(f"resolved manifest: {manifest['id']}")
    cutoff = _aware_utc(manifest["cutoff"])
    required_streams = ALL_REQUIRED_STREAMS
    if any(stream_key not in before["streams"] for stream_key in required_streams):
        raise InvalidEvidenceError("ledger_stream_unregistered")
    runner = MatchupParityRunner(
        engine,
        governance=ActiveManifestLedgerGovernanceReader(engine),
        legacy_source=StoredLegacyMatchupSource(TeamMatchupRepository(engine)),
    )
    ledger_repository = CanonicalGameLedgerRepository(engine)
    reports = []
    session = Session(engine)
    transaction = session.begin()
    args._artifact_session = session
    with session.no_autoflush:
        try:
            transaction_authority = resolve_unique_matchup_authority(
                session, season=args.season, cutoff=cutoff, lock=True,
            )
            transaction_authority.require_completed_regular_season()
        except ValueError as error:
            raise InvalidEvidenceError(str(error)) from error
        if (
            transaction_authority.manifest_id != args.manifest_id
            or transaction_authority.catalog_id
            != governance.event_catalog_publication_id
            or transaction_authority.catalog_checksum
            != governance.event_catalog_checksum
        ):
            raise InvalidEvidenceError("manifest_authority_ambiguous")
        candidate_rows = _compose_candidate_set(
            engine,
            session=session,
            governance=governance,
            season=args.season,
            cutoff=cutoff,
        )
        publications = {
            stream_key: row.publication_id
            for stream_key, row in candidate_rows.items()
        }
        _validate_matchup_candidate_composition(
            engine,
            session=session,
            publications={
                key: candidate_rows[key]
                for key in required_streams
                if key != "player_per36"
            },
            governance=governance,
            season=args.season,
            cutoff=cutoff,
        )
        for window in ("season", "l15"):
            matchup_publications = {
                key: publications[key]
                for key in required_streams
                if key != "player_per36" and key.endswith(f"_{window}")
            }
            window_reports = runner.run(
                args.season,
                window,
                cutoff=cutoff,
                publications=matchup_publications,
                session=session,
            )
            if {report.surface for report in window_reports} != {
                "traditional", "assist_locations"
            }:
                raise InvalidEvidenceError("matchup_reports_incomplete")
            reports.extend(window_reports)
        per36_report, per36_artifact_id = _compare_candidate_per36(
            engine,
            repository=ledger_repository,
            publication=candidate_rows["player_per36"],
            governance=governance,
            season=args.season,
            cutoff=cutoff,
            provider_end_date=transaction_authority.provider_end_date,
            capture_id=args.per36_capture_id,
            session=session,
        )
        sanitized_reports = [
            {
                "stream": f"{report.surface}_{report.window}",
                **_sanitize_matchup_report(report),
            }
            for report in reports
        ]
        sanitized_reports.append(per36_report)
        artifact_ids: dict[str, str] = {}
        for stream_key in required_streams:
            if stream_key == "player_per36":
                artifact_ids[stream_key] = per36_artifact_id
                continue
            row = session.scalar(
                select(LedgerParityArtifact)
                .where(
                    LedgerParityArtifact.stream_key == stream_key,
                    LedgerParityArtifact.publication_id == publications[stream_key],
                )
                .order_by(LedgerParityArtifact.created_at.desc())
                .limit(1)
            )
            if row is None:
                raise InvalidEvidenceError("parity_artifact_missing")
            artifact_ids[stream_key] = row.artifact_id

    after = _control_state(engine, session=session, lock=True)
    if before != after:
        raise InvalidEvidenceError("pointer_or_stream_state_changed")

    status = _overall_status(sanitized_reports)

    audit_id = _record_compare_audit(
        engine,
        actor=args.actor,
        manifest_id=args.manifest_id,
        status=status,
        reports=sanitized_reports,
        session=session,
    )
    summary = {
        "status": status,
        "target": args.target,
        "actor_fingerprint": hashlib.sha256(
            args.actor.strip().encode("utf-8")
        ).hexdigest(),
        "backend_git_sha": _git_sha(),
        "environment": _environment(),
        "database_fingerprint": _database_fingerprint(engine),
        "migration_version": migration_version,
        "season": args.season,
        "phase": "Regular Season",
        "manifest": manifest,
        "ledger_game_count": len(governance.expected_game_ids),
        "team_ids": sorted(int(team_id) for team_id in governance.team_ids),
        "season_game_counts_by_team": {
            str(team_id): len(game_ids)
            for team_id, game_ids in sorted(
                governance.expected_season_game_ids.items()
            )
        },
        "l15_game_counts_by_team": {
            str(team_id): len(game_ids)
            for team_id, game_ids in sorted(
                governance.expected_l15_game_ids.items()
            )
        },
        "reports": sanitized_reports,
        "artifact_ids": artifact_ids,
        "audit_id": audit_id,
        "pointer_nonmutation": {
            "unchanged": True,
            "before_checksum": _control_state_checksum(before),
            "after_checksum": _control_state_checksum(after),
        },
        "stream_nonmutation": {
            "unchanged": True,
            "before_checksum": _control_state_checksum(before),
            "after_checksum": _control_state_checksum(after),
        },
    }
    session.flush()
    staged_summary = _stage_summary(args.output, summary)
    args._staged_summary = staged_summary
    _commit_and_publish_summary(transaction, session, args, staged_summary)
    _print_table(summary)
    _print_protected_game_ids(governance)
    return {
        "exact": EXIT_EXACT,
        "pending_adjudication": EXIT_PENDING_ADJUDICATION,
        "invalid_evidence": EXIT_INVALID_EVIDENCE,
    }[status]


def _invalid_summary(args, code: str) -> dict[str, Any]:
    summary = {
        "status": "invalid_evidence",
        "error_code": code,
        "target": getattr(args, "target", None),
        "season": getattr(args, "season", None),
        "backend_git_sha": _git_sha(),
        "environment": _environment(),
    }
    before = getattr(args, "_control_state_before", None)
    after = getattr(args, "_control_state_after", None)
    if before is not None and after is not None:
        before_checksum = _control_state_checksum(before)
        after_checksum = _control_state_checksum(after)
        unchanged = before == after
        summary.update({
            "pointer_nonmutation": {
                "unchanged": unchanged,
                "before_checksum": before_checksum,
                "after_checksum": after_checksum,
            },
            "stream_nonmutation": {
                "unchanged": unchanged,
                "before_checksum": before_checksum,
                "after_checksum": after_checksum,
            },
        })
        if getattr(args, "_artifact_transaction_rolled_back", False):
            summary["artifact_writes_rolled_back"] = True
        if getattr(args, "_database_transaction_committed", False):
            summary["artifact_transaction_committed"] = True
    else:
        summary["nonmutation_proof"] = {"available": False}
    return summary


def _compare(args, engine) -> int:
    """Run the new bounded command, retaining the old test seam temporarily."""

    if not hasattr(args, "target"):
        # Existing unit callers exercised the pure matchup runner directly.
        # Keep that narrow seam while the executable command uses the full
        # safety contract above.
        run_migrations(engine)
        cutoff = _aware_utc(args.cutoff)
        runner = MatchupParityRunner(
            engine,
            governance=ActiveManifestLedgerGovernanceReader(engine),
            legacy_source=StoredLegacyMatchupSource(TeamMatchupRepository(engine)),
        )
        reports = runner.run(
            args.season,
            args.window,
            cutoff=cutoff,
            publications=_load_publications(args.publications_json),
        )
        print(json.dumps([report.to_dict() for report in reports], sort_keys=True, indent=2))
        return EXIT_EXACT if reports and all(report.exact for report in reports) else EXIT_PENDING_ADJUDICATION

    try:
        args._control_state_before = _control_state(engine)
        return _bounded_compare(args, engine, before=args._control_state_before)
    except (
        InvalidEvidenceError, MatchupParityError,
        PublicationGovernanceUnavailable, SQLAlchemyError, OSError, ValueError,
    ) as error:
        artifact_session = getattr(args, "_artifact_session", None)
        if artifact_session is not None:
            artifact_session.rollback()
            artifact_session.close()
            args._artifact_session = None
            args._artifact_transaction_rolled_back = True
        staged_summary = getattr(args, "_staged_summary", None)
        if staged_summary is not None:
            staged_summary.unlink(missing_ok=True)
            args._staged_summary = None
        code = error.code if isinstance(error, InvalidEvidenceError) else "comparison_invalid"
        try:
            args._control_state_after = _control_state(engine)
        except (OSError, ValueError):
            args._control_state_after = None
        summary = _invalid_summary(args, code)
        try:
            _write_summary(args.output, summary)
        except OSError:
            pass
        _print_table(summary)
        return EXIT_INVALID_EVIDENCE


def _adjudicate(args, engine) -> int:
    repository = LedgerParityArtifactRepository(engine)
    artifact = repository.adjudicate(
        args.artifact_id,
        decision=args.decision,
        actor=args.actor,
        reason=args.reason,
    )
    print(f"{artifact.artifact_id}: {artifact.decision}")
    return 0


def _capture_per36(args, engine) -> int:
    if not args.actor.strip() or len(args.actor) > 128:
        raise InvalidEvidenceError("actor_required")
    source = Path(args.input)
    if (
        not source.is_file()
        or source.stat().st_size > MAX_CAPTURE_INPUT_BYTES
    ):
        raise InvalidEvidenceError("per36_capture_input_invalid")
    try:
        with source.open("rb") as handle:
            raw = handle.read(MAX_CAPTURE_INPUT_BYTES + 1)
        if len(raw) > MAX_CAPTURE_INPUT_BYTES:
            raise InvalidEvidenceError("per36_capture_input_invalid")
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidEvidenceError("per36_capture_input_invalid") from error
    if not isinstance(document, Mapping) or set(document) != {
        "game_set_checksum", "provider_window_identity", "request_checksum",
        "rows",
    }:
        raise InvalidEvidenceError("per36_capture_input_invalid")
    _, manifest, _ = _manifest_preflight(
        engine, season=args.season, manifest_id=args.manifest_id
    )
    cutoff = _aware_utc(manifest["cutoff"])
    capture = Per36DiagnosticCaptureRepository(engine).record_operator_evidence(
        publication_id=args.publication_id,
        season=args.season,
        cutoff=cutoff,
        manifest_id=args.manifest_id,
        event_catalog_publication_id=manifest["event_catalog_publication_id"],
        event_catalog_checksum=manifest["event_catalog_checksum"],
        game_set_checksum=document["game_set_checksum"],
        request_checksum=document["request_checksum"],
        provider_window_identity=document["provider_window_identity"],
        rows=document["rows"],
        actor=args.actor,
    )
    _write_summary(args.output, {
        "status": "captured",
        "capture_id": capture.capture_id,
        "capture_checksum": capture.capture_checksum,
        "source_observation_id": capture.source_observation_id,
        "publication_id": capture.publication_id,
        "payload_checksum": capture.payload_checksum,
        "request_checksum": capture.request_checksum,
        "actor_fingerprint": hashlib.sha256(
            args.actor.strip().encode("utf-8")
        ).hexdigest(),
    })
    return 0


def _capture_per36_command(args, engine) -> int:
    try:
        return _capture_per36(args, engine)
    except (
        InvalidEvidenceError, OSError, SQLAlchemyError,
        ProviderUnavailableError, ValueError,
    ) as error:
        code = (
            error.code
            if isinstance(error, InvalidEvidenceError)
            else "per36_capture_invalid"
        )
        try:
            _write_summary(args.output, {
                "status": "invalid_evidence",
                "error_code": code,
                "season": args.season,
                "manifest_id": args.manifest_id,
            })
        except OSError:
            pass
        return EXIT_INVALID_EVIDENCE


def _collect_per36(args, engine) -> int:
    """Collect independent NBA rows; the app module owns evidence shaping."""

    try:
        _, manifest, _ = _manifest_preflight(
            engine, season=args.season, manifest_id=args.manifest_id,
        )
        cutoff = _aware_utc(manifest["cutoff"])
        with Session(engine) as session:
            authority = resolve_unique_matchup_authority(
                session, season=args.season, cutoff=cutoff, lock=False,
            )
        evidence = Per36ProviderEvidenceCollector(NBAStatsAdapter()).collect(
            season=args.season, authority=authority,
        )
        _write_summary(args.output, evidence)
        return EXIT_EXACT
    except (
        InvalidEvidenceError, OSError, SQLAlchemyError,
        ProviderUnavailableError, ValueError,
    ) as error:
        code = error.code if isinstance(error, InvalidEvidenceError) else "per36_collection_invalid"
        try:
            _write_summary(args.output, {
                "status": "invalid_evidence", "error_code": code,
                "season": args.season, "manifest_id": args.manifest_id,
            })
        except OSError:
            pass
        return EXIT_INVALID_EVIDENCE


def _prepare(args, engine) -> int:
    """Compose the five inactive candidates needed by the operator flow."""

    before = _control_state(engine)
    try:
        governance, manifest, _ = _manifest_preflight(
            engine, season=args.season, manifest_id=args.manifest_id,
        )
        cutoff = _aware_utc(manifest["cutoff"])
        with Session(engine) as session, session.begin():
            authority = resolve_unique_matchup_authority(
                session, season=args.season, cutoff=cutoff, lock=True,
            )
            authority.require_completed_regular_season()
            candidates = _compose_candidate_set(
                engine, session=session, governance=governance,
                season=args.season, cutoff=cutoff,
            )
            _validate_matchup_candidate_composition(
                engine, session=session,
                publications={
                    key: value for key, value in candidates.items()
                    if key != "player_per36"
                },
                governance=governance, season=args.season, cutoff=cutoff,
            )
            receipt = {
                "status": "prepared", "season": args.season,
                "manifest": manifest,
                "candidates": {
                    key: {
                        "publication_id": value.publication_id,
                        "payload_checksum": value.checksum,
                    }
                    for key, value in sorted(candidates.items())
                },
            }
        after = _control_state(engine)
        if before != after:
            raise InvalidEvidenceError("pointer_or_stream_state_changed")
        receipt["control_state_checksum"] = _control_state_checksum(after)
        _write_summary(args.output, receipt)
        return EXIT_EXACT
    except (
        InvalidEvidenceError, PublicationGovernanceUnavailable,
        SQLAlchemyError, OSError, ValueError,
    ):
        try:
            _write_summary(args.output, {
                "status": "invalid_evidence", "error_code": "prepare_invalid",
                "season": args.season, "manifest_id": args.manifest_id,
            })
        except OSError:
            pass
        return EXIT_INVALID_EVIDENCE


class MatchupParityOperation:
    """Deep application interface for the complete bounded operator workflow.

    Candidate composition/verification, immutable captures, comparison,
    transaction outcomes, and sanitized output all stay behind this seam. The
    executable adapter only parses argv and selects one method.
    """

    def __init__(self, engine) -> None:
        self._engine = engine

    def prepare(self, request) -> int:
        return _prepare(request, self._engine)

    def collect_per36(self, request) -> int:
        return _collect_per36(request, self._engine)

    def capture_per36(self, request) -> int:
        return _capture_per36_command(request, self._engine)

    def compare(self, request) -> int:
        return _compare(request, self._engine)

    def adjudicate(self, request) -> int:
        return _adjudicate(request, self._engine)


__all__ = [
    "ALL_REQUIRED_STREAMS",
    "InvalidEvidenceError",
    "MatchupParityOperation",
]
