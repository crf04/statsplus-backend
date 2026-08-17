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
        --publications-json publications.json \
        --per36-capture-id "<capture id>"

``publications.json`` names all five inactive candidate publications:

    {
      "traditional_opponent_season": "<publication id>",
      "traditional_opponent_l15": "<publication id>",
      "assist_locations_season": "<publication id>",
      "assist_locations_l15": "<publication id>",
      "player_per36": "<publication id>"
    }
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping
from uuid import uuid4

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from app.domain.publication_integrity import (  # noqa: E402
    publication_payload_matches_checksum,
)
from app.domain.utc import assume_utc  # noqa: E402
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
)
from app.models.canonical_game_ledger import LedgerParityArtifact  # noqa: E402
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
    PER36_RAW_FIELDS,
    PER36_RATE_FIELDS,
    Per36DiagnosticCaptureRepository,
)
from app.services.ledger_lineage import LedgerLineage  # noqa: E402
from app.services.ledger_runtime import (  # noqa: E402
    ActiveManifestLedgerGovernanceReader,
)
from app.services.matchup_parity import (  # noqa: E402
    MatchupParityError,
    MatchupParityRunner,
    StoredLegacyMatchupSource,
    resolve_matchup_publication,
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


EXIT_EXACT = 0
EXIT_PENDING_ADJUDICATION = 2
EXIT_INVALID_EVIDENCE = 3
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
            cwd=Path(__file__).resolve().parents[1],
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


def _control_state(engine) -> dict[str, Any]:
    """Capture all pointers and stream gates used to prove non-mutation."""

    with Session(engine) as session:
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
                select(PublicationPointer).order_by(PublicationPointer.stream_key)
            )
        }
        streams = {
            str(row.stream_key): bool(row.enabled)
            for row in session.scalars(
                select(PublicationStream).order_by(PublicationStream.stream_key)
            )
        }
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
        if manifest is None or manifest.season != season:
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
        cutoff = _aware_utc(manifest.cutoff)
        if active.cutoff is not None and _aware_utc(active.cutoff) != cutoff:
            raise InvalidEvidenceError("active_season_cutoff_mismatch")

    governance_reader = ActiveManifestLedgerGovernanceReader(engine)
    try:
        governance = governance_reader.read_for_composition(
            season, cutoff, manifest_id=manifest_id
        )
    except (PublicationGovernanceUnavailable, ValueError) as error:
        raise InvalidEvidenceError("manifest_governance_invalid") from error
    if governance.manifest_id != manifest_id or _aware_utc(governance.cutoff) != cutoff:
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
    publication = session.get(PublicationVersion, publication_id)
    if (
        publication is None
        or publication.stream_key != stream_key
        or publication.season != season
        or _aware_utc(publication.cutoff) != cutoff
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


def _validate_candidate_set(
    engine,
    *,
    publications: Mapping[str, str],
    required_streams: frozenset[str],
    season: str,
    cutoff: datetime,
    manifest: Mapping[str, str],
) -> dict[str, PublicationVersion]:
    if set(publications) != set(required_streams):
        raise InvalidEvidenceError("candidate_publications_incomplete")
    with Session(engine) as session:
        rows = {
            stream_key: _candidate_preflight(
                session,
                publication_id=publications[stream_key],
                stream_key=stream_key,
                season=season,
                cutoff=cutoff,
                manifest_id=manifest["id"],
                catalog_id=manifest["event_catalog_publication_id"],
                catalog_checksum=manifest["event_catalog_checksum"],
            )
            for stream_key in sorted(required_streams)
        }
    return rows


def _compare_candidate_per36(
    engine,
    *,
    publication: PublicationVersion,
    governance,
    season: str,
    cutoff: datetime,
    capture_id: str,
    session: Session,
) -> tuple[dict[str, Any], str | None]:
    """Compare a candidate against scoped immutable per-36 evidence."""

    try:
        candidate_rows = tuple(decode_player_per36(publication.payload, season=season))
    except (TypeError, ValueError, KeyError) as error:
        raise InvalidEvidenceError("player_per36_candidate_invalid") from error

    game_ids = frozenset(
        str(game_id)
        for game_ids_for_team in governance.expected_season_game_ids.values()
        for game_id in game_ids_for_team
    )
    repository = CanonicalGameLedgerRepository(engine)
    games = tuple(repository.get_game(game_id) for game_id in sorted(game_ids))
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
    if set(capture_by_id) != set(expected_by_id):
        raise InvalidEvidenceError("player_per36_diagnostic_capture_identity_invalid")
    raw_fields_by_player: dict[int, dict[str, int]] = {}
    for game in canonical_games:
        for fact in game.player_facts:
            totals = raw_fields_by_player.setdefault(
                fact.player_id, {field: 0 for field in PER36_RAW_FIELDS}
            )
            for field in PER36_RAW_FIELDS:
                totals[field] += int(getattr(fact, field))
    for player_id, expected in expected_by_id.items():
        actual = capture_by_id[player_id]
        expected_raw = raw_fields_by_player[player_id]
        if any(actual[field] != value for field, value in expected_raw.items()):
            raise InvalidEvidenceError("player_per36_diagnostic_capture_raw_mismatch")
        if actual["game_count"] != expected.game_count:
            raise InvalidEvidenceError("player_per36_diagnostic_capture_game_count_mismatch")
        if actual["team_ids_at_game"] != list(expected.team_ids_at_game):
            raise InvalidEvidenceError("player_per36_diagnostic_capture_team_mismatch")
        if not math.isclose(
            float(actual["minutes"]), expected.minutes, rel_tol=1e-9, abs_tol=1e-9
        ):
            raise InvalidEvidenceError("player_per36_diagnostic_capture_minutes_mismatch")
        if any(
            not math.isclose(
                float(actual[field]), float(getattr(expected, field)),
                rel_tol=1e-9, abs_tol=1e-9,
            )
            for field in PER36_RATE_FIELDS
        ):
            raise InvalidEvidenceError("player_per36_diagnostic_capture_rate_mismatch")

    return {
        "stream": "player_per36",
        "status": "exact",
        "game_count": len(canonical_games),
        "compared_count": len(expected_rows),
        "difference_count": 0,
        "difference_classifications": {},
        "publication_id": publication.publication_id,
        "payload_checksum": publication.checksum,
    }, capture_id


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
    games = tuple(repository.get_game(game_id) for game_id in sorted(game_ids))
    if any(game is None for game in games) or len(games) != len(game_ids):
        raise InvalidEvidenceError("ledger_game_set_incomplete")
    canonical_games = tuple(game for game in games if game is not None)
    for window in ("season", "l15"):
        expected_game_ids_by_team = (
            governance.expected_l15_game_ids
            if window == "l15"
            else governance.expected_season_game_ids
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
                    canonical_games,
                    season=season,
                    as_of=cutoff.date(),
                    window_games=15 if window == "l15" else None,
                    expected_game_ids=frozenset(
                        str(game_id)
                        for game_ids_for_team in expected_game_ids_by_team.values()
                        for game_id in game_ids_for_team
                    ),
                    expected_team_game_ids=(
                        {int(team_id): frozenset(map(str, ids)) for team_id, ids in expected_game_ids_by_team.items()}
                        if window == "l15" else None
                    ),
                    team_ids=frozenset(int(team_id) for team_id in governance.team_ids),
                )
            else:
                materialization = materialize_assist_location_window(
                    canonical_games,
                    season=season,
                    as_of=cutoff.date(),
                    window_games=15 if window == "l15" else None,
                    expected_game_ids=frozenset(
                        str(game_id)
                        for game_ids_for_team in expected_game_ids_by_team.values()
                        for game_id in game_ids_for_team
                    ),
                    expected_team_game_ids=(
                        {int(team_id): frozenset(map(str, ids)) for team_id, ids in expected_game_ids_by_team.items()}
                        if window == "l15" else None
                    ),
                    team_ids=frozenset(int(team_id) for team_id in governance.team_ids),
                )
            if not materialization.complete:
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
        "legacy_game_ids_by_team": report.to_dict()["legacy_game_ids_by_team"],
        "ledger_game_ids_by_team": report.to_dict()["ledger_game_ids_by_team"],
        "legacy_game_set_checksum": report.legacy_game_set_checksum,
        "ledger_game_set_checksum": report.ledger_game_set_checksum,
        "ledger_publication_id": report.ledger_publication_id,
        "ledger_payload_checksum": report.ledger_payload_checksum,
        "semantic_rule": report.semantic_rule,
        "semantic_rule_reason": report.semantic_rule_reason,
    }


def _write_summary(path: str, summary: Mapping[str, Any]) -> None:
    destination = Path(path)
    if destination.exists() and destination.is_dir():
        raise OSError("summary output must be a file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(summary, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


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


def _record_compare_audit(engine, *, actor: str, manifest_id: str, status: str, reports: list[Mapping[str, Any]]) -> str:
    event_id = str(uuid4())
    details = {
        "status": status,
        "report_count": len(reports),
        "streams": [report.get("stream") for report in reports],
    }
    with Session(engine) as session, session.begin():
        session.add(AuditEvent(
            event_id=event_id,
            actor=actor[:128],
            action="ledger.matchup_parity_compare",
            resource=manifest_id[:128],
            reason="bounded parity comparison",
            details=json.dumps(details, sort_keys=True),
            created_at=datetime.now(timezone.utc),
        ))
    return event_id


def _bounded_compare(args, engine, *, before: Mapping[str, Any]) -> int:
    if not isinstance(args.actor, str) or not args.actor.strip():
        raise InvalidEvidenceError("actor_required")
    publications = _load_publications(args.publications_json)
    governance, manifest, migration_version = _manifest_preflight(
        engine, season=args.season, manifest_id=args.manifest_id
    )
    print(f"resolved manifest: {manifest['id']}")
    cutoff = _aware_utc(manifest["cutoff"])
    required_streams = ALL_REQUIRED_STREAMS
    semantic_rule = getattr(args, "semantic_rule", None)
    semantic_rule_reason = getattr(args, "semantic_rule_reason", None)
    if bool(semantic_rule) != bool(semantic_rule_reason):
        raise InvalidEvidenceError("semantic_rule_reason_required")
    if any(
        stream_key not in before["streams"] or before["streams"][stream_key]
        for stream_key in required_streams
    ):
        raise InvalidEvidenceError("ledger_stream_already_enabled")
    candidate_rows = _validate_candidate_set(
        engine,
        publications=publications,
        required_streams=required_streams,
        season=args.season,
        cutoff=cutoff,
        manifest=manifest,
    )

    runner = MatchupParityRunner(
        engine,
        governance=ActiveManifestLedgerGovernanceReader(engine),
        legacy_source=StoredLegacyMatchupSource(TeamMatchupRepository(engine)),
    )
    reports = []
    with Session(engine) as session, session.begin():
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
                semantic_rule=semantic_rule,
                semantic_rule_reason=semantic_rule_reason,
            )
            if {report.surface for report in window_reports} != {
                "traditional", "assist_locations"
            } or any(report.hard_failure for report in window_reports):
                raise InvalidEvidenceError("matchup_reports_incomplete")
            reports.extend(window_reports)
        per36_report, per36_artifact_id = _compare_candidate_per36(
            engine,
            publication=candidate_rows["player_per36"],
            governance=governance,
            season=args.season,
            cutoff=cutoff,
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

    after = _control_state(engine)
    if before != after:
        raise InvalidEvidenceError("pointer_or_stream_state_changed")

    if any(report.get("status") == "failed" for report in sanitized_reports):
        status = "invalid_evidence"
    elif any(
        report.get("status") == "adjudication_required"
        for report in sanitized_reports
    ):
        status = "pending_adjudication"
    elif all(report.get("status") == "exact" for report in sanitized_reports):
        status = "exact"
    else:
        status = "invalid_evidence"

    audit_id = _record_compare_audit(
        engine,
        actor=args.actor,
        manifest_id=args.manifest_id,
        status=status,
        reports=sanitized_reports,
    )
    summary = {
        "status": status,
        "target": args.target,
        "actor": args.actor,
        "backend_git_sha": _git_sha(),
        "environment": _environment(),
        "database_fingerprint": _database_fingerprint(engine),
        "migration_version": migration_version,
        "season": args.season,
        "phase": "Regular Season",
        "manifest": manifest,
        "ledger_game_count": len(governance.expected_game_ids),
        "team_ids": sorted(int(team_id) for team_id in governance.team_ids),
        "season_game_ids_by_team": {
            str(team_id): sorted(str(game_id) for game_id in game_ids)
            for team_id, game_ids in sorted(
                governance.expected_season_game_ids.items()
            )
        },
        "l15_game_ids_by_team": {
            str(team_id): sorted(str(game_id) for game_id in game_ids)
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
    _write_summary(args.output, summary)
    _print_table(summary)
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
            "artifact_writes_rolled_back": True,
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
    except (InvalidEvidenceError, MatchupParityError, PublicationGovernanceUnavailable, OSError, ValueError) as error:
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


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    compare = subparsers.add_parser("compare")
    compare.add_argument("--database-url", required=True)
    compare.add_argument("--season", required=True)
    compare.add_argument("--manifest-id", required=True)
    compare.add_argument("--actor", required=True)
    compare.add_argument("--output", required=True, help="sanitized JSON summary path")
    compare.add_argument("--target", choices=("isolated", "candidate"), required=True)
    compare.add_argument("--publications-json", required=True)
    compare.add_argument("--per36-capture-id", required=True)
    compare.add_argument(
        "--semantic-rule",
        choices=("provider_rounding", "parent_approved_semantic_difference"),
    )
    compare.add_argument("--semantic-rule-reason")

    adjudicate = subparsers.add_parser("adjudicate")
    adjudicate.add_argument("artifact_id")
    adjudicate.add_argument("decision", choices=("approved", "rejected"))
    adjudicate.add_argument("--database-url", required=True)
    adjudicate.add_argument("--actor", required=True)
    adjudicate.add_argument("--reason", required=True)

    args = parser.parse_args()
    if args.command == "adjudicate":
        return _adjudicate(args, create_engine(args.database_url))
    if args.command == "compare":
        return _compare(args, create_engine(args.database_url))
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
