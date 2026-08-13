#!/usr/bin/env python3
"""Record baseline/database-first p95 latency and retained query plans."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import Engine

from app.config.settings import RuntimeSettings
from app.dependencies import build_dependencies
from app.migrations import run_migrations
from app.services.database_first_activation import DatabaseOnlyProviderGuard
from app.services.database_first_benchmark import benchmark_matchup_services
from app.models import Base
from app.models.collection_control import (
    PublicationPointer,
    PublicationStream,
    PublicationVersion,
)
from app.models.event_catalog import EventCatalogEntry, EventCatalogRefresh
from app.models.player_diet import PlayerDietFactRow, PlayerDietSurfaceObservationRow
from app.models.player_game_log import PlayerGameLog, PlayerGameLogRefresh
from app.models.player_pool_snapshot import PlayerPoolSnapshot
from app.models.team_matchup import TeamMatchupFactRow, TeamMatchupSurfaceObservationRow

UTC = timezone.utc

# The benchmark is intentionally representative rather than a toy smoke test.
# These lower bounds keep a tiny fixture from producing a misleadingly cheap
# p95 or a query plan that does not exercise the governed indexes.
MIN_FIXTURE_TEAMS = 30
MIN_FIXTURE_GAMES = 10
MIN_FIXTURE_PLAYERS = 100
MIN_FIXTURE_LOG_ROWS = 500


def _rows(value, *, label: str) -> list[dict]:
    if isinstance(value, Mapping):
        value = value.get("rows", value.get("records", value.get("data")))
    if not isinstance(value, list) or not value or not all(
        isinstance(row, Mapping) for row in value
    ):
        raise SystemExit(f"benchmark fixture section {label} must contain non-empty row objects")
    return [dict(row) for row in value]


def _datetime(value, *, default: datetime) -> datetime:
    if value is None:
        return default
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _date(value, *, default: date) -> date:
    if value is None:
        return default
    return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value))


def _json_value(value):
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))


def _filtered_values(table, row: Mapping, **defaults):
    """Keep fixture loading strict to the target model's declared columns."""

    values = {key: value for key, value in row.items() if key in table.c}
    values.update({key: value for key, value in defaults.items() if key not in values})
    return values


def _validate_database_scope(database_url: str, *, isolated: bool, production_url: str | None) -> None:
    if not isolated:
        raise SystemExit("benchmark requires --isolated and a disposable database")
    normalized = str(database_url).rstrip("/")
    if production_url and normalized == str(production_url).rstrip("/"):
        raise SystemExit("benchmark database must be separate from the production/control database")
    if any(marker in normalized.lower() for marker in ("prod", "production", "railway")):
        raise SystemExit("benchmark database URL must identify a disposable environment")


def _require_disposable_database(engine: Engine) -> None:
    """Reject a benchmark target that already contains application facts."""

    inspectable = inspect(engine)
    for table_name in (
        "event_catalog",
        "event_catalog_refreshes",
        "player_game_logs",
        "player_game_log_refreshes",
        "player_pool_snapshots",
        "publication_streams",
        "publication_versions",
        "publication_pointers",
        "publication_observations",
        "publication_activations",
        "canonical_game_ledger_games",
    ):
        if table_name in inspectable.get_table_names():
            with engine.connect() as connection:
                if connection.execute(
                    select(1).select_from(Base.metadata.tables[table_name]).limit(1)
                ).first() is not None:
                    raise SystemExit(
                        "benchmark database must be disposable and empty before fixture load"
                    )


def _validate_fixture_scale(
    *,
    event_rows: list[dict],
    player_rows: list[dict],
    pool_rows: list[dict],
    team_rows: list[dict],
    game_id: str,
) -> dict[str, object]:
    """Require enough teams/games/players to represent a production read."""

    game_ids = {
        str(row.get("nba_game_id") or row.get("game_id") or game_id)
        for row in event_rows
    }
    team_ids = {
        int(value)
        for row in event_rows
        for value in (row.get("home_team_id"), row.get("away_team_id"))
        if value is not None
    }
    team_ids.update(
        int(row["team_id"])
        for row in team_rows
        if row.get("team_id") is not None
    )
    player_ids = {int(row["player_id"]) for row in player_rows if row.get("player_id") is not None}
    pool_player_ids = {
        int(player.get("canonical_player_id") or player.get("player_id"))
        for row in pool_rows
        for player in (row.get("players") if isinstance(row.get("players"), list) else [row])
        if isinstance(player, Mapping)
        and (player.get("canonical_player_id") or player.get("player_id")) is not None
    }
    player_ids.update(pool_player_ids)
    profile = {
        "fixture_kind": "representative_fixture",
        "production_claim": False,
        "teams": len(team_ids),
        "games": len(game_ids),
        "players": len(player_ids),
        "player_game_log_rows": len(player_rows),
        "minimums": {
            "teams": MIN_FIXTURE_TEAMS,
            "games": MIN_FIXTURE_GAMES,
            "players": MIN_FIXTURE_PLAYERS,
            "player_game_log_rows": MIN_FIXTURE_LOG_ROWS,
        },
    }
    failures = [
        key for key, minimum in (
            ("teams", MIN_FIXTURE_TEAMS),
            ("games", MIN_FIXTURE_GAMES),
            ("players", MIN_FIXTURE_PLAYERS),
            ("player_game_log_rows", MIN_FIXTURE_LOG_ROWS),
        ) if int(profile[key]) < minimum
    ]
    if failures:
        raise SystemExit(
            "benchmark fixture is below representative minimums: "
            + ", ".join(failures)
        )
    return profile


def _load_fixture(engine: Engine, seeded: Mapping, *, season: str, game_id: str) -> dict[str, object]:
    """Load the JSON fixture into the temporary application schema.

    The benchmark deliberately owns fixture loading so timing never measures
    an unconnected manifest.  Sections use the corresponding application
    model column names; publications may be supplied as ``versions``,
    ``streams``, and ``pointers`` under one object.
    """

    _require_disposable_database(engine)
    now = datetime.now(UTC)
    event_rows = _rows(seeded["event_catalog"], label="event_catalog")
    player_rows = _rows(seeded["player_game_logs"], label="player_game_logs")
    diet_value = seeded["player_diets"]
    if isinstance(diet_value, Mapping) and not any(
        key in diet_value for key in ("rows", "records", "data")
    ):
        diet_rows = [row for value in diet_value.values() for row in _rows(value, label="player_diets")]
    else:
        diet_rows = _rows(diet_value, label="player_diets")
    team_value = seeded["team_matchups"]
    team_facts = _rows(
        team_value.get("facts", team_value) if isinstance(team_value, Mapping) else team_value,
        label="team_matchups",
    )
    pool_rows = _rows(seeded["player_pool"], label="player_pool")
    publication_value = seeded["publications"]
    if not isinstance(publication_value, Mapping):
        raise SystemExit("benchmark publications must contain streams, versions, and pointers")
    publication_versions = _rows(
        publication_value.get("versions"), label="publications.versions"
    )
    publication_streams = publication_value.get("streams")
    publication_pointers = publication_value.get("pointers")
    if publication_streams is None or publication_pointers is None:
        raise SystemExit("benchmark publications require streams, versions, and pointers")
    stream_rows = _rows(publication_streams, label="publications.streams")
    pointer_rows = _rows(publication_pointers, label="publications.pointers")
    fixture_profile = _validate_fixture_scale(
        event_rows=event_rows,
        player_rows=player_rows,
        pool_rows=pool_rows,
        team_rows=team_facts,
        game_id=game_id,
    )

    with engine.begin() as connection:
        event_table = EventCatalogEntry.__table__
        for row in event_rows:
            event_date = _datetime(row.get("scheduled_at"), default=now)
            connection.execute(event_table.insert().values(
                nba_game_id=str(row.get("nba_game_id") or game_id),
                season=str(row.get("season") or season),
                home_team_id=int(row["home_team_id"]),
                home_team_name=str(row.get("home_team_name") or "Home"),
                home_team_tricode=str(row.get("home_team_tricode") or "HOM"),
                away_team_id=int(row["away_team_id"]),
                away_team_name=str(row.get("away_team_name") or "Away"),
                away_team_tricode=str(row.get("away_team_tricode") or "AWY"),
                scheduled_at=event_date,
                status_text=str(row.get("status_text") or "Final"),
                status_code=row.get("status_code"),
                postponed_status=row.get("postponed_status"),
                postponement_evidence=row.get("postponement_evidence"),
                classification=str(row.get("classification") or "Regular Season"),
                first_seen_at=_datetime(row.get("first_seen_at"), default=now),
                last_seen_at=_datetime(row.get("last_seen_at"), default=now),
            ))
        connection.execute(EventCatalogRefresh.__table__.insert().values(
            season=season,
            last_attempt_at=now,
            last_success_at=now,
            last_failure_at=None,
            failure_summary=None,
            event_count=len(event_rows),
        ))
        game_table = PlayerGameLog.__table__
        for row in player_rows:
            values = _filtered_values(game_table, row)
            values.setdefault("season", season)
            values.setdefault("season_type", "Regular Season")
            values.setdefault("game_id", game_id)
            values["game_date"] = _date(values.get("game_date"), default=now.date())
            for key in PlayerGameLog.__table__.columns.keys():
                if key not in values and key not in {"season", "player_id", "game_id"}:
                    values[key] = 0 if key not in {"player_name", "team_tricode", "opponent_team_tricode"} else ""
            connection.execute(game_table.insert().values(values))
        connection.execute(
            PlayerGameLogRefresh.__table__.insert().values(
                season=season,
                source_provider="benchmark_fixture",
                retrieved_at=now,
                row_count=len(player_rows),
                source_row_count=len(player_rows),
                identity_source_row_count=len(player_rows),
                publication_status="complete",
            )
        )
        diet_table = PlayerDietFactRow.__table__
        for row in diet_rows:
            values = _filtered_values(diet_table, row)
            values.setdefault("season", season)
            values["retrieved_at"] = _datetime(values.get("retrieved_at"), default=now)
            connection.execute(diet_table.insert().values(values))
        diet_bases = {str(row.get("base")) for row in diet_rows}
        connection.execute(
            PlayerDietSurfaceObservationRow.__table__.insert(),
            [
                {
                    "season": season,
                    "base": base,
                    "status": "available" if base in diet_bases else "missing",
                    "unavailable_reason": None if base in diet_bases else "fixture_missing",
                    "retrieved_at": now,
                }
                for base in ("assist_locations", "play_types", "shot_types", "shot_zones")
            ],
        )
        pool_table = PlayerPoolSnapshot.__table__
        for row in pool_rows:
            values = _filtered_values(pool_table, row)
            values.setdefault("season", season)
            values["game_ids"] = _json_value(values.get("game_ids", [game_id]))
            values["payload"] = _json_value(values.get("payload", {"players": []}))
            values["retrieved_at"] = _datetime(values.get("retrieved_at"), default=now)
            values["updated_at"] = _datetime(values.get("updated_at"), default=now)
            values.setdefault("refresh_version", 1)
            values.setdefault("refresh_outcome", "complete")
            connection.execute(pool_table.insert().values(values))
        team_table = TeamMatchupFactRow.__table__
        for row in team_facts:
            values = _filtered_values(team_table, row)
            values.setdefault("season", season)
            values.setdefault("as_of_date", now.date())
            values.setdefault("window_kind", "season")
            values.setdefault("window_games", 0)
            values.setdefault("window_end_date", now.date())
            values["as_of_date"] = _date(values["as_of_date"], default=now.date())
            values["window_end_date"] = _date(values["window_end_date"], default=now.date())
            values["retrieved_at"] = _datetime(values.get("retrieved_at"), default=now)
            connection.execute(team_table.insert().values(values))
        team_observation_table = TeamMatchupSurfaceObservationRow.__table__
        scopes = {
            (
                str(row.get("season") or season),
                _date(row.get("as_of_date"), default=now.date()),
                str(row.get("window_kind") or "season"),
                int(row.get("window_games") or 0),
            )
            for row in team_facts
        }
        for fact_season, as_of_date, window_kind, window_games in sorted(scopes):
            surfaces = {
                str(row.get("base"))
                for row in team_facts
                if str(row.get("season") or season) == fact_season
                and _date(row.get("as_of_date"), default=now.date()) == as_of_date
                and str(row.get("window_kind") or "season") == window_kind
                and int(row.get("window_games") or 0) == window_games
            }
            connection.execute(
                team_observation_table.insert(),
                [
                    {
                        "season": fact_season,
                        "as_of_date": as_of_date,
                        "window_kind": window_kind,
                        "window_games": window_games,
                        "surface": surface,
                        "status": "available",
                        "unavailable_reason": None,
                        "retrieved_at": now,
                    }
                    for surface in sorted(surfaces)
                ],
            )
        stream_table = PublicationStream.__table__
        for row in stream_rows:
            connection.execute(stream_table.insert().values(
                stream_key=str(row["stream_key"]),
                provider=str(row.get("provider") or "ledger"),
                owner=str(row.get("owner") or "benchmark"),
                required_observations=_json_value(row.get("required_observations", [])),
                publication_strategy=str(row.get("publication_strategy") or "replace"),
                supported_windows=_json_value(row.get("supported_windows", ["season"])),
                schema_versions=_json_value(row.get("schema_versions", [1])),
                completeness_rule=str(row.get("completeness_rule") or "replace"),
                freshness_rule=str(row.get("freshness_rule") or "cutoff_current"),
                enabled=bool(row.get("enabled", True)),
                created_at=_datetime(row.get("created_at"), default=now),
            ))
        version_table = PublicationVersion.__table__
        for row in publication_versions:
            values = _filtered_values(version_table, row)
            values.setdefault("season", season)
            values["cutoff"] = _datetime(values.get("cutoff"), default=now)
            values["created_at"] = _datetime(values.get("created_at"), default=now)
            values["payload"] = _json_value(values.get("payload", {"rows": []}))
            values.setdefault("status", "active")
            values.setdefault("version", 1)
            values.setdefault("fence", 1)
            values.setdefault("reason", "benchmark fixture")
            expected_checksum = hashlib.sha256(values["payload"].encode("utf-8")).hexdigest()
            if "checksum" in values and values["checksum"] != expected_checksum:
                raise SystemExit(
                    f"benchmark publication {values.get('publication_id')} checksum does not match payload"
                )
            values.setdefault("checksum", expected_checksum)
            connection.execute(version_table.insert().values(values))
        pointer_table = PublicationPointer.__table__
        for row in pointer_rows:
            values = _filtered_values(pointer_table, row)
            values.setdefault("fence", 1)
            values["updated_at"] = _datetime(values.get("updated_at"), default=now)
            connection.execute(pointer_table.insert().values(values))
    return fixture_profile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument(
        "--production-database-url",
        help="production/control URL used only to reject accidental reuse",
    )
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="explicitly acknowledge that --database-url is a disposable benchmark database",
    )
    parser.add_argument(
        "--fixture",
        required=True,
        help="JSON production-like fixture containing season and game_id",
    )
    parser.add_argument("--game-id", required=True)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--report", required=True)
    parser.add_argument(
        "--artifact",
        default=str(Path("analysis") / "database_first_benchmark_artifact.json"),
        help="sanitized deterministic evidence artifact regenerated with the report",
    )
    args = parser.parse_args()
    _validate_database_scope(
        args.database_url,
        isolated=args.isolated,
        production_url=args.production_database_url,
    )
    engine = create_engine(args.database_url)
    run_migrations(engine)
    with open(args.fixture, encoding="utf-8") as handle:
        fixture = json.load(handle)
    if not isinstance(fixture, dict):
        raise SystemExit("benchmark fixture must be a JSON object")
    season = str(fixture.get("season") or "")
    game_id = str(fixture.get("game_id") or args.game_id)
    if not season or not game_id:
        raise SystemExit("benchmark fixture and --game-id require concrete season/game identity")
    seeded = fixture.get("seeded_fixture")
    required_sections = {
        "event_catalog",
        "player_pool",
        "player_game_logs",
        "player_diets",
        "team_matchups",
        "publications",
    }
    if (
        not isinstance(seeded, dict)
        or not required_sections <= set(seeded)
        or any(
            not isinstance(seeded[section], (dict, list, tuple))
            or not seeded[section]
            for section in required_sections
        )
    ):
        raise SystemExit(
            "benchmark fixture must contain non-empty event_catalog, player_pool, "
            "player_game_logs, player_diets, team_matchups, and publications sections"
        )

    fixture_profile = _load_fixture(engine, seeded, season=season, game_id=game_id)

    settings = RuntimeSettings(
        environment="testing",
        database={"url": args.database_url},
        auth={"firebase_admin_disabled": True},
        cache={"enabled": False},
        features={"injury_report_enabled": False},
        nba={"current_season": season},
    )
    dependencies = build_dependencies(settings)
    service = dependencies.matchup_service
    provider_counter = [0]
    nba_guard = DatabaseOnlyProviderGuard("benchmark-nba", counter=provider_counter)
    pbp_guard = DatabaseOnlyProviderGuard("benchmark-pbp", counter=provider_counter)
    # Keep every NBA/PBP dependency behind the same measured fail-closed
    # guard.  A benchmark that succeeds only because one provider reference
    # was left uninstrumented is not usable evidence.
    for target, attribute in (
        (dependencies.event_catalog_service, "provider"),
        (dependencies.player_diet_service, "nba_stats"),
        (dependencies.player_service, "nba_stats"),
        (dependencies.team_service, "nba_stats"),
        (dependencies.game_service, "nba_stats"),
        (dependencies.data_service, "nba_stats"),
        (dependencies.provider_health_service, "nba_stats"),
    ):
        if target is not None and hasattr(target, attribute):
            setattr(target, attribute, nba_guard)
    for target, attribute in (
        (dependencies.player_diet_service, "pbp_stats"),
        (dependencies.data_service, "pbp_provider"),
        (dependencies.data_service, "pbp"),
        (dependencies.provider_health_service, "pbp_stats"),
    ):
        if target is not None and hasattr(target, attribute):
            setattr(target, attribute, pbp_guard)
    if dependencies.game_logs_source is not None:
        live_source = getattr(dependencies.game_logs_source, "live_source", None)
        if live_source is not None and hasattr(live_source, "pbp_provider"):
            live_source.pbp_provider = pbp_guard
    # Injury Reports retain their existing service contract, but the benchmark
    # measures the statistical Matchups path and must not let a live injury
    # provider dominate its route timing.
    service.injuries = None
    if dependencies.event_catalog_service is not None:
        # The complete route must use the durable catalog read.  Any attempt
        # to refresh schedule/provider data turns the benchmark into a failed
        # evidence run instead of silently measuring a fallback.
        dependencies.event_catalog_service.provider = nba_guard

    def baseline_read() -> dict:
        """Run the complete MatchupService against legacy fact repositories."""

        targets = [
            (service, "publication_reader"),
            (service.player_logs, "_publication_reader"),
            (getattr(service.player_diets, "repository", None), "_publication_reader"),
            (service.team_matchups, "_publication_reader"),
        ]
        previous = [(target, attribute, getattr(target, attribute, None)) for target, attribute in targets if target is not None]
        try:
            for target, attribute, _ in previous:
                setattr(target, attribute, None)
            return service.get_matchup(game_id=game_id)
        finally:
            for target, attribute, value in previous:
                setattr(target, attribute, value)

    def database_first_read() -> dict:
        """Run the complete activated database-first MatchupService route."""

        return service.get_matchup(game_id=game_id)

    report = benchmark_matchup_services(
        engine,
        baseline_route=baseline_read,
        database_first_route=database_first_read,
        season=season,
        game_id=game_id,
        iterations=args.iterations,
        provider_call_count=lambda: provider_counter[0],
        fixture_validated=True,
        fixture_profile=fixture_profile,
    )
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    with open(args.artifact, "w", encoding="utf-8") as handle:
        json.dump(report.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
