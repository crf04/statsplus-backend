"""Inactive materialization services for ledger-derived publication streams."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.models.collection_control import CollectionManifest, CompositionJob
from app.services.collection_control import LedgerPublicationComposition

from app.services.canonical_game_ledger import (
    CanonicalGame,
    CanonicalGameLedgerRepository,
    LedgerPublicationRecord,
    validate_canonical_season,
)
from app.services.ledger_derivations import (
    AssistLocationFact,
    AssistLocationWindowMaterialization,
    PlayerPer36Fact,
    TeamWindowMaterialization,
    TraditionalOpponentFact,
    derive_assist_location_facts,
    derive_player_per36_facts,
    derive_traditional_opponent_facts,
    materialize_team_window,
    materialize_assist_location_window,
)
from app.services.ledger_parity import (
    LedgerParityArtifactRepository,
    LedgerParityReport,
    SemanticDifference,
    compare_ledger_to_legacy,
)
from app.services.ledger_lineage import LedgerLineage


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
    assist_location_season: AssistLocationWindowMaterialization | None
    assist_location_l15: AssistLocationWindowMaterialization | None


class LedgerMaterializationService:
    """Compose ledger-derived streams and record inactive publication metadata."""

    def __init__(
        self,
        repository: CanonicalGameLedgerRepository,
        *,
        parity_repository: LedgerParityArtifactRepository,
        parity_reader,
        publication_service=None,
        clock=None,
    ) -> None:
        self.repository = repository
        self.parity_repository = parity_repository
        self.parity_reader = parity_reader
        self.publication_service = publication_service
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def compose(
        self,
        games: Iterable[CanonicalGame],
        *,
        season: str,
        as_of: date,
        cutoff: datetime | None = None,
        expected_game_ids: frozenset[str] | None = None,
        expected_l15_game_ids: Mapping[int, frozenset[str]] | None = None,
        team_ids: frozenset[int] | None = None,
        require_assist_locations: bool = False,
        activate: bool = False,
        recomposition_reason: str | None = None,
        session: Session | None = None,
    ) -> LedgerMaterialization:
        canonical_season = validate_canonical_season(season)
        if expected_game_ids is None or set(
            self.repository.game_checksums(canonical_season, through=as_of)
        ) != set(expected_game_ids):
            raise LedgerMaterializationUnavailable(
                "stored eligible game IDs must exactly equal governed game IDs"
            )
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
            expected_game_ids=expected_game_ids,
            expected_team_game_ids=expected_l15_game_ids,
            team_ids=team_ids,
        )
        season_status = "complete" if season_window.complete else "unavailable"
        l15_status = "complete" if l15_window.complete else "unavailable"
        season_reason = (
            None if season_window.complete
            else season_window.reason or "Season ledger is incomplete"
        )
        l15_reason = (
            None if l15_window.complete
            else l15_window.reason or "L15 ledger is incomplete"
        )
        traditional = derive_traditional_opponent_facts(eligible)
        assist_status = "complete"
        try:
            assists = derive_assist_location_facts(eligible)
        except ValueError as error:
            if require_assist_locations:
                raise LedgerMaterializationUnavailable(str(error)) from error
            assists = ()
            assist_status = "unavailable"
        per36 = derive_player_per36_facts(eligible, season=canonical_season, cutoff=as_of)
        if team_ids is None or expected_game_ids is None:
            raise LedgerMaterializationUnavailable(
                "assist-location windows require exact governed teams and game IDs"
            )
        assist_season = assist_l15 = None
        if assist_status == "complete":
            try:
                assist_season = materialize_assist_location_window(
                    eligible,
                    season=canonical_season,
                    as_of=as_of,
                    expected_game_ids=expected_game_ids,
                    team_ids=team_ids,
                )
                assist_l15 = materialize_assist_location_window(
                    eligible,
                    season=canonical_season,
                    as_of=as_of,
                    window_games=15,
                    expected_game_ids=expected_game_ids,
                    expected_team_game_ids=expected_l15_game_ids,
                    team_ids=team_ids,
                )
            except ValueError as error:
                if require_assist_locations:
                    raise LedgerMaterializationUnavailable(str(error)) from error
                assist_status = "unavailable"
        result = LedgerMaterialization(
            season=canonical_season,
            as_of=as_of,
            traditional_opponent=traditional,
            assist_locations=assists,
            player_per36=per36,
            season_window=season_window,
            l15_window=l15_window,
            assist_location_season=assist_season,
            assist_location_l15=assist_l15,
        )
        retrieved_at = self.clock()
        player_game_logs = tuple(
            {
                "season": game.season,
                "season_type": game.season_type,
                "game_id": game.game_id,
                "game_date": game.game_date,
                "opponent_team_id": (
                    game.away_team_id
                    if player.team_id == game.home_team_id
                    else game.home_team_id
                ),
                "opponent_team_tricode": (
                    game.away_team_tricode
                    if player.team_id == game.home_team_id
                    else game.home_team_tricode
                ),
                "is_home": player.team_id == game.home_team_id,
                **_json_default(player),
                # PlayerGameFact keeps two-/three-point primitives while the
                # public log contract exposes aggregate field-goal columns.
                "field_goals_made": player.field_goals_made,
                "field_goals_attempted": player.field_goals_attempted,
            }
            for game in eligible
            for player in game.player_facts
        )
        publication_specs = [
            ("player_game_logs", player_game_logs, "season", 0, season_status, season_reason),
            ("traditional_opponent_season", season_window.teams, "season", 0, season_status, season_reason),
            ("traditional_opponent_l15", l15_window.teams, "rolling_games", 15, l15_status, l15_reason),
            ("player_per36", per36, "season", 0, season_status, season_reason),
            ("team_matchups_season", season_window.teams, "season", 0, season_status, season_reason),
            ("team_matchups_l15", l15_window.teams, "rolling_games", 15, l15_status, l15_reason),
        ]
        if assist_status == "complete" and assist_season is not None and assist_l15 is not None:
            publication_specs.extend((
                ("assist_locations_season", assist_season.teams, "season", 0, season_status, season_reason),
                ("assist_locations_l15", assist_l15.teams, "rolling_games", 15, l15_status, l15_reason),
            ))
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
                payload=_payload_json(payload),
            )
            for stream_key, payload, window_kind, window_games, status, reason in publication_specs
        )
        self.repository.publish_metadata_batch(
            publications,
            connection=session.connection() if session is not None else None,
        )
        publication_cutoff = cutoff or datetime.combine(
            as_of, datetime.min.time(), timezone.utc
        )
        if publication_cutoff.tzinfo is None or publication_cutoff.date() != as_of:
            raise LedgerMaterializationUnavailable(
                "publication cutoff must be aware and match the materialization date"
            )
        if self.publication_service is not None:
            candidates = []
            if season_window.complete:
                candidates.extend((
                    ("player_game_logs", player_game_logs),
                    ("traditional_opponent_season", season_window.teams),
                    ("player_per36", per36),
                ))
            if l15_window.complete:
                candidates.append(("traditional_opponent_l15", l15_window.teams))
            if assist_status == "complete" and assist_season is not None and assist_l15 is not None:
                if season_window.complete:
                    candidates.append(("assist_locations_season", assist_season.teams))
                if l15_window.complete:
                    candidates.append(("assist_locations_l15", assist_l15.teams))
            candidate_versions = {}
            batch = []
            for stream_key, payload in candidates:
                encoded_payload = json.loads(_payload_json(payload))
                selected_game_ids = (
                    l15_window.governed_game_ids
                    if stream_key.endswith("_l15")
                    else season_window.governed_game_ids
                )
                provenance = {
                    game.source_observation_id: game.game_id
                    for game in eligible
                    if game.game_id in selected_game_ids
                }
                publication_reason = recomposition_reason or "historical ledger rehearsal"
                if activate:
                    composition = LedgerPublicationComposition(
                        stream_key=stream_key, season=canonical_season,
                        cutoff=publication_cutoff, payload=encoded_payload,
                        provenance=provenance, reason=publication_reason,
                    )
                    if self.publication_service.stream_enabled(
                        stream_key,
                        session=session,
                    ):
                        batch.append(composition)
                    else:
                        candidate_versions[stream_key] = self.publication_service.compose_inactive_ledger(
                            stream_key, season=canonical_season, cutoff=publication_cutoff,
                            payload=encoded_payload, provenance=provenance,
                            reason=publication_reason,
                            session=session,
                        )
                else:
                    candidate_versions[stream_key] = self.publication_service.compose_inactive_ledger(
                        stream_key,
                        season=canonical_season,
                        cutoff=publication_cutoff,
                        payload=encoded_payload,
                        provenance=provenance,
                        reason=publication_reason,
                        session=session,
                    )
            if activate and batch:
                candidate_versions.update({
                    publication.stream_key: publication
                    for publication in self.publication_service.recompose_ledger_batch(
                        batch,
                        session=session,
                    )
                })
            parity_specs = (
                (
                    "player_game_logs",
                    "player_game_logs",
                    "legacy_rows",
                ),
                (
                    "traditional_opponent_season",
                    "traditional_opponent",
                    "legacy_traditional_rows",
                ),
                (
                    "traditional_opponent_l15",
                    None,
                    None,
                ),
                ("player_per36", "player_per36", "legacy_per36_rows"),
            )
            for candidate_key, diagnostic_key, comparison_key in parity_specs:
                candidate = candidate_versions.get(candidate_key)
                if candidate is None:
                    continue
                if diagnostic_key is None or comparison_key is None:
                    report = _unavailable_parity_report(
                        canonical_season,
                        len(eligible),
                        candidate_key,
                        ValueError("legacy diagnostic has no equivalent L15 window"),
                    )
                else:
                    try:
                        try:
                            diagnostic_rows = self.parity_reader.read(
                                diagnostic_key,
                                session=session,
                            )
                        except TypeError as error:
                            # Small injected diagnostic readers from older
                            # callers only accept the stream key.  Preserve
                            # that seam while the production reader uses the
                            # enclosing transaction's connection.
                            if "session" not in str(error):
                                raise
                            diagnostic_rows = self.parity_reader.read(diagnostic_key)
                        if comparison_key == "legacy_rows":
                            report = compare_ledger_to_legacy(
                                eligible,
                                diagnostic_rows,
                                season=canonical_season,
                            )
                        else:
                            report = compare_ledger_to_legacy(
                                eligible,
                                None,
                                season=canonical_season,
                                **{comparison_key: diagnostic_rows},
                            )
                    except (KeyError, RuntimeError, ValueError) as error:
                        report = _unavailable_parity_report(
                            canonical_season,
                            len(eligible),
                            diagnostic_key,
                            error,
                        )
                record_kwargs = {
                    "cutoff": publication_cutoff,
                    "report": report,
                    "publication_id": candidate.publication_id,
                    "payload_checksum": candidate.checksum,
                }
                if session is not None:
                    record_kwargs["session"] = session
                self.parity_repository.record(candidate_key, **record_kwargs)
        return result


class LedgerCorrectionQueue:
    """Atomically enqueue every derived slice invalidated by a correction."""

    STREAMS = (
        "player_game_logs",
        "traditional_opponent_season",
        "traditional_opponent_l15",
        "assist_locations_season",
        "assist_locations_l15",
        "player_per36",
    )

    def __init__(self, *, clock=None, require_governance: bool = False) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.require_governance = require_governance

    def __call__(self, connection: Connection, game: CanonicalGame) -> None:
        table = CompositionJob.__table__
        now = self.clock()
        is_correction = bool(
            connection.info.get("canonical_game_ledger_replacement", False)
        )
        affected_team_ids = sorted({fact.team_id for fact in game.team_facts})
        recomposition_reason = "correction" if is_correction else "initial_acceptance"
        manifest = connection.execute(select(CollectionManifest).where(
            CollectionManifest.season == game.season,
            CollectionManifest.status == "active",
            CollectionManifest.collect_before > now,
        ).order_by(CollectionManifest.cutoff.desc()).limit(1)).mappings().one_or_none()
        valid_manifest = manifest is not None and (
            "canonical_game_ledger" in set(json.loads(manifest["scopes"]))
            and 1 in set(json.loads(manifest["accepted_versions"]))
        )
        if not valid_manifest:
            if self.require_governance:
                raise LedgerMaterializationUnavailable("active manifest cutoff is required")
            cutoff = datetime.combine(now.date(), datetime.min.time(), timezone.utc)
        else:
            cutoff = manifest["cutoff"]
        for stream_key in self.STREAMS:
            existing_rows = connection.execute(select(
                table.c.job_id,
                table.c.status,
                table.c.generation,
                table.c.cutoff,
                table.c.affected_team_ids,
                table.c.source_observation_ids,
                table.c.trigger_game_id,
                table.c.trigger_game_ids,
                table.c.ledger_checksum,
                table.c.ledger_evidence,
                table.c.recomposition_reason,
            ).where(
                table.c.stream_key == stream_key,
                table.c.season == game.season,
            )).mappings().all()
            existing = next(
                (
                    row for row in existing_rows
                    if _cutoffs_equal(row["cutoff"], cutoff)
                ),
                None,
            )
            if existing is not None:
                previously_succeeded = existing["status"] == "succeeded"
                previous_game_ids = _stored_trigger_game_ids(
                    existing["trigger_game_ids"], existing["trigger_game_id"]
                )
                previous_source_ids = _json_values(existing["source_observation_ids"])
                previous_evidence = _stored_evidence(
                    existing["ledger_evidence"],
                    game_ids=previous_game_ids,
                    fallback_checksum=existing["ledger_checksum"],
                )
                previous_lineage = LedgerLineage(
                    {
                        game_id: {
                            "checksum": checksum,
                            "source_observation_id": (
                                previous_source_ids[index]
                                if index < len(previous_source_ids)
                                else ""
                            ),
                        }
                        for index, (game_id, checksum) in enumerate(
                            sorted(previous_evidence.items())
                        )
                    },
                    cutoff=cutoff,
                    recomposition_reason=str(
                        existing["recomposition_reason"] or "initial_acceptance"
                    ),
                )
                lineage = previous_lineage.merge(LedgerLineage.single(
                    game_id=game.game_id,
                    source_observation_id=str(game.source_observation_id),
                    ledger_checksum=game.checksum,
                    cutoff=cutoff,
                    reason=recomposition_reason,
                ))
                evidence = {
                    item.game_id: item.ledger_checksum for item in lineage.evidence
                }
                previous_team_ids = {
                    int(team_id)
                    for team_id in _json_values(existing["affected_team_ids"])
                    if str(team_id).isdigit()
                }
                merged_team_ids = sorted({
                    *previous_team_ids,
                    *affected_team_ids,
                })
                trigger_game_ids = lineage.game_ids
                trigger_game_id = (
                    lineage.game_ids[0] if len(lineage.game_ids) == 1 else None
                )
                if previously_succeeded and recomposition_reason == "correction":
                    # A completed job's lineage describes the publication
                    # that already ran.  A later correction must invalidate
                    # only its new game/team target; queued coalescing still
                    # unions all work that has not been composed yet.
                    trigger_game_ids = (game.game_id,)
                    trigger_game_id = game.game_id
                    merged_team_ids = affected_team_ids
                connection.execute(
                    update(table).where(table.c.job_id == existing["job_id"]).values(
                        status="queued",
                        attempts=0,
                        manifest_id=manifest["manifest_id"] if manifest is not None else None,
                        updated_at=now,
                        last_error=None,
                        trigger_game_ids=json.dumps(
                            trigger_game_ids, separators=(",", ":")
                        ),
                        trigger_game_id=trigger_game_id,
                        affected_team_ids=json.dumps(merged_team_ids, separators=(",", ":")),
                        source_observation_ids=json.dumps(lineage.source_observation_ids, separators=(",", ":")),
                        recomposition_reason=lineage.recomposition_reason,
                        ledger_checksum=(next(iter(evidence.values())) if len(evidence) == 1
                                         else LedgerLineage.evidence_checksum(dict(sorted(evidence.items())))),
                        game_set_checksum=lineage.game_set_checksum,
                        ledger_evidence=json.dumps(
                            dict(sorted(evidence.items())),
                            sort_keys=True, separators=(",", ":"),
                        ),
                        generation=(table.c.generation + 1),
                        claimed_generation=None,
                    )
                )
            else:
                connection.execute(insert(table).values(
                    job_id=str(uuid4()),
                    stream_key=stream_key,
                    manifest_id=manifest["manifest_id"] if manifest is not None else None,
                    season=game.season,
                    cutoff=cutoff,
                    status="queued",
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                    last_error=None,
                    trigger_game_ids=json.dumps([game.game_id], separators=(",", ":")),
                    trigger_game_id=game.game_id,
                    affected_team_ids=json.dumps(affected_team_ids, separators=(",", ":")),
                    source_observation_ids=json.dumps([str(game.source_observation_id)], separators=(",", ":")),
                    recomposition_reason=recomposition_reason,
                    ledger_checksum=game.checksum,
                    game_set_checksum=LedgerLineage.single(
                        game_id=game.game_id,
                        source_observation_id=str(game.source_observation_id),
                        ledger_checksum=game.checksum,
                        cutoff=cutoff,
                        reason=recomposition_reason,
                    ).game_set_checksum,
                    ledger_evidence=json.dumps({game.game_id: game.checksum}, separators=(",", ":")),
                    generation=1,
                    claimed_generation=None,
                ))


def _payload_checksum(payload: object) -> str:
    encoded = _payload_json(payload).encode()
    return hashlib.sha256(encoded).hexdigest()


def _payload_json(payload: object) -> str:
    return json.dumps(
        payload,
        default=_json_default,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _game_set_checksum(game_ids: Iterable[str]) -> str:
    """Hash a deterministic exact game-id set for correction diagnostics."""

    return hashlib.sha256(
        json.dumps(sorted(set(str(game_id) for game_id in game_ids)), separators=(",", ":")).encode()
    ).hexdigest()


def _json_values(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return ()
    if isinstance(parsed, list):
        return tuple(
            str(item) for item in parsed
            if isinstance(item, (str, int))
            and not isinstance(item, bool)
            and str(item)
        )
    if isinstance(parsed, (str, int)) and not isinstance(parsed, bool):
        return (str(parsed),)
    return ()


def _stored_trigger_game_ids(
    plural_value: str | None,
    singular_value: str | None,
) -> tuple[str, ...]:
    """Read plural lineage while safely falling back to legacy singular data."""

    plural = _json_values(plural_value)
    if plural:
        return tuple(sorted(set(plural)))
    singular = str(singular_value) if singular_value else ""
    return (singular,) if singular else ()


def _stored_evidence(
    value: str | None,
    *,
    game_ids: tuple[str, ...],
    fallback_checksum: str | None,
) -> dict[str, str]:
    """Decode keyed evidence and recover one old scalar checksum safely."""

    try:
        parsed = json.loads(value) if value else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = {}
    if isinstance(parsed, Mapping):
        evidence = {
            str(game_id): str(checksum)
            for game_id, checksum in parsed.items()
            if isinstance(game_id, (str, int))
            and isinstance(checksum, (str, int))
            and str(game_id)
            and str(checksum)
        }
        if evidence:
            return dict(sorted(evidence.items()))
    if len(game_ids) == 1 and fallback_checksum:
        return {game_ids[0]: str(fallback_checksum)}
    return {}


def _cutoffs_equal(left, right: datetime) -> bool:
    """Compare typed and legacy SQLite timestamp encodings equivalently."""

    def normalize(value):
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return value
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        return value

    return normalize(left) == normalize(right)


def _json_default(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _unavailable_parity_report(
    season: str,
    game_count: int,
    diagnostic_key: str,
    error: Exception,
) -> LedgerParityReport:
    return LedgerParityReport(
        season=season,
        game_count=game_count,
        compared_count=0,
        differences=(SemanticDifference(
            identity=diagnostic_key,
            field="diagnostic",
            pbp_value="candidate_persisted",
            legacy_value=None,
            classification=f"diagnostic_unavailable:{type(error).__name__}",
        ),),
        adjudication_required=True,
    )


# Friendly aliases for callers that describe this seam as composition rather
# than materialization.
LedgerCompositionService = LedgerMaterializationService


__all__ = [
    "LedgerCompositionService",
    "LedgerMaterialization",
    "LedgerMaterializationService",
    "LedgerMaterializationUnavailable",
    "LedgerCorrectionQueue",
]
