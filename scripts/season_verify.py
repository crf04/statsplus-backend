"""Verify a season's held ledger candidates against NBA.com, read-only.

For each ledger-composed stream this finds the newest held candidate of the
season (the one ``cutover_season`` would move), bounds NBA.com's Regular
Season figures to the same last game date, and reports per-identity agreement
(statsplus#391).  Nothing is written: the database connection is read-only and
the report goes to stdout and an optional file.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nba_api.stats import endpoints
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.domain.slate_time import slate_date_for_instant
from app.domain.utc import assume_utc
from app.models.collection_control import PublicationVersion
from app.services.database_first_activation import (
    _decode_known_publication_payload,
    decode_player_game_logs,
    decode_player_per36,
)
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.publication_authority import verify_publication_authority
from app.services.team_matchup_publications import PublicationGovernanceUnavailable
from app.utils.db import _normalize_database_url

#: Streams with an NBA.com reference, then the ones without.
REFERENCE_STREAMS = (
    "player_game_logs",
    "player_per36",
    "traditional_opponent_season",
    "traditional_opponent_l15",
)
NO_REFERENCE_STREAMS = (
    "assist_locations_season",
    "assist_locations_l15",
    "player_assist_locations",
    "rebound_types_season",
    "rebound_types_l15",
    "player_rebound_types",
)
TOP_MISMATCHES = 10

#: Ledger field -> NBA.com column, per reference.  Counts compare exactly.
LOG_FIELDS = {
    "points": "PTS",
    "rebounds": "REB",
    "assists": "AST",
    "field_goals_made": "FGM",
    "field_goals_attempted": "FGA",
    "three_pointers_made": "FG3M",
    "three_pointers_attempted": "FG3A",
    "free_throws_made": "FTM",
    "free_throws_attempted": "FTA",
    "offensive_rebounds": "OREB",
    "defensive_rebounds": "DREB",
    "turnovers": "TOV",
    "steals": "STL",
    "blocks": "BLK",
    "personal_fouls": "PF",
    "minutes": "MIN",
}
PER36_FIELDS = {
    "points_per36": "PTS",
    "rebounds_per36": "REB",
    "assists_per36": "AST",
    "field_goals_made_per36": "FGM",
    "field_goals_attempted_per36": "FGA",
    "three_pointers_made_per36": "FG3M",
    "three_pointers_attempted_per36": "FG3A",
    "free_throws_made_per36": "FTM",
    "free_throws_attempted_per36": "FTA",
    "turnovers_per36": "TOV",
    "steals_per36": "STL",
    "blocks_per36": "BLK",
    "personal_fouls_per36": "PF",
}
#: Ledger ``counts`` key -> LeagueDashTeamStats Opponent Totals column.  The
#: four contracted keys are ``MATCHUP_TRADITIONAL_KEYS`` (2025-26 parity).
TEAM_FIELDS = {
    "points": "OPP_PTS",
    "rebounds": "OPP_REB",
    "assists": "OPP_AST",
    "field_goals_made": "OPP_FGM",
    "field_goals_attempted": "OPP_FGA",
    "three_pointers_made": "OPP_FG3M",
    "three_pointers_attempted": "OPP_FG3A",
    "free_throws_made": "OPP_FTM",
    "free_throws_attempted": "OPP_FTA",
    "turnovers": "OPP_TOV",
    "steals": "OPP_STL",
    "blocks": "OPP_BLK",
    "personal_fouls": "OPP_PF",
    "offensive_rebounds": "OPP_OREB",
    "defensive_rebounds": "OPP_DREB",
}
#: Per-field absolute tolerances; absent means exact.  Ledger minutes come
#: from play-by-play clocks and drift from box-score minutes by seconds;
#: NBA.com publishes Per36 rounded to one decimal (max error 0.05).
LOG_TOLERANCE = {"minutes": 0.1}
PER36_TOLERANCE = {field: 0.05 + 1e-6 for field in PER36_FIELDS}


class NbaReference(Protocol):
    def player_game_logs(self, season: str, date_to: date) -> list[Mapping[str, Any]]: ...
    def player_per36(self, season: str, date_to: date) -> list[Mapping[str, Any]]: ...
    def team_opponent_totals(
        self,
        season: str,
        date_to: date,
        last_n_games: int,
    ) -> list[Mapping[str, Any]]: ...


class NbaReferenceError(RuntimeError):
    """An NBA.com request failed; carries no upstream text (it can name the proxy)."""


class NbaStatsReference:
    """NBA.com reference reads through ``nba_api``, optionally via a proxy."""

    def __init__(self, proxy: str | None = None, timeout: float = 60.0) -> None:
        self._proxy = proxy or None
        self._timeout = timeout

    def _records(self, build: Callable[..., Any]) -> list[Mapping[str, Any]]:
        try:
            endpoint = build(proxy=self._proxy, timeout=self._timeout)
            return endpoint.get_data_frames()[0].to_dict("records")
        except Exception as error:
            raise NbaReferenceError(f"NBA.com request failed ({type(error).__name__})") from None

    def player_game_logs(self, season, date_to):
        return self._records(
            lambda **kw: endpoints.PlayerGameLogs(
                season_nullable=season,
                season_type_nullable="Regular Season",
                date_to_nullable=date_to.strftime("%m/%d/%Y"),
                **kw,
            )
        )

    def player_per36(self, season, date_to):
        return self._records(
            lambda **kw: endpoints.LeagueDashPlayerStats(
                season=season,
                season_type_all_star="Regular Season",
                measure_type_detailed_defense="Base",
                per_mode_detailed="Per36",
                date_to_nullable=date_to.strftime("%m/%d/%Y"),
                **kw,
            )
        )

    def team_opponent_totals(self, season, date_to, last_n_games):
        return self._records(
            lambda **kw: endpoints.LeagueDashTeamStats(
                season=season,
                season_type_all_star="Regular Season",
                measure_type_detailed_defense="Opponent",
                per_mode_detailed="Totals",
                last_n_games=last_n_games,
                date_to_nullable=date_to.strftime("%m/%d/%Y"),
                **kw,
            )
        )


def open_read_only_engine(database_url: str) -> Engine:
    """An engine whose connections reject every write."""

    url = _normalize_database_url(database_url)
    if url.startswith("sqlite"):
        engine = create_engine(url)

        @event.listens_for(engine, "connect")
        def _query_only(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA query_only = ON")

        return engine
    return create_engine(
        url,
        connect_args={"options": "-c default_transaction_read_only=on"},
    )


@dataclass(frozen=True, slots=True)
class Comparison:
    compared: int
    exact: int
    within_tolerance: int
    ledger_only: tuple[str, ...]
    nba_only: tuple[str, ...]
    mismatches: tuple[dict[str, Any], ...]  # worst first, all of them

    @property
    def matched(self) -> int:
        return self.exact + self.within_tolerance

    @property
    def match_rate(self) -> float:
        total = self.compared + len(self.ledger_only) + len(self.nba_only)
        return self.matched / total if total else 1.0


def compare_rows(
    ledger: Mapping[str, Mapping[str, float]],
    nba: Mapping[str, Mapping[str, float]],
    tolerance: Mapping[str, float],
) -> Comparison:
    """Compare identities present on both sides field by field."""

    exact = within = 0
    mismatches = []
    shared = sorted(set(ledger) & set(nba))
    for identity in shared:
        worst: dict[str, Any] | None = None
        all_exact = True
        for field, ledger_value in ledger[identity].items():
            nba_value = nba[identity][field]
            difference = abs(ledger_value - nba_value)
            if difference == 0:
                continue
            all_exact = False
            if difference > tolerance.get(field, 0.0) and (worst is None or difference > worst["difference"]):
                worst = {
                    "identity": identity,
                    "field": field,
                    "ledger": ledger_value,
                    "nba": nba_value,
                    "difference": difference,
                }
        if worst is not None:
            mismatches.append(worst)
        elif all_exact:
            exact += 1
        else:
            within += 1
    mismatches.sort(key=lambda item: (-item["difference"], item["identity"]))
    return Comparison(
        compared=len(shared),
        exact=exact,
        within_tolerance=within,
        ledger_only=tuple(sorted(set(ledger) - set(nba))),
        nba_only=tuple(sorted(set(nba) - set(ledger))),
        mismatches=tuple(mismatches),
    )


def newest_held_candidate(
    session: Session,
    stream_key: str,
    season: str,
) -> PublicationVersion | None:
    """The candidate ``cutover_season`` would move for this stream."""

    return session.scalars(
        select(PublicationVersion)
        .where(
            PublicationVersion.stream_key == stream_key,
            PublicationVersion.season == season,
            PublicationVersion.status == "candidate",
        )
        .order_by(PublicationVersion.cutoff.desc(), PublicationVersion.version.desc())
        .limit(1)
    ).first()


def _nba_rows(
    records: list[Mapping[str, Any]],
    identity: Callable[[Mapping[str, Any]], str],
    fields: Mapping[str, str],
    extra: Mapping[str, str] | None = None,
) -> dict[str, dict[str, float]]:
    columns = {**fields, **(extra or {})}
    rows = {}
    for record in records:
        missing = [column for column in columns.values() if column not in record]
        if missing:
            raise NbaReferenceError(f"NBA.com response lacks columns {missing}")
        values = {field: float(record[column]) for field, column in columns.items()}
        if not all(math.isfinite(value) for value in values.values()):
            # A null cell reads as NaN, which no tolerance check would catch.
            raise NbaReferenceError("NBA.com response has a non-numeric statistic")
        rows[identity(record)] = values
    return rows


def governance_for(session: Session, candidate: PublicationVersion):
    """The candidate's own governed games, from its bound manifest and Event
    Catalog (the resolution ``cutover_season`` applies), or ``None``."""

    try:
        verify_publication_authority(session, candidate)
        return ActiveManifestLedgerGovernanceReader(session.get_bind()).read_for_composition(
            candidate.season,
            assume_utc(candidate.cutoff),
            candidate.manifest_id,
        )
    except PublicationGovernanceUnavailable:
        return None


def verify_stream(
    session: Session,
    stream_key: str,
    season: str,
    nba: NbaReference,
) -> dict[str, Any]:
    candidate = newest_held_candidate(session, stream_key, season)
    if candidate is None:
        # Nothing to verify fails a reference stream; an assist stream has no
        # reference either way.
        return {
            "stream_key": stream_key,
            "status": "no_candidate",
            "passed": stream_key in NO_REFERENCE_STREAMS,
        }
    cutoff = assume_utc(candidate.cutoff)
    result: dict[str, Any] = {
        "stream_key": stream_key,
        "publication_id": candidate.publication_id,
        "version": candidate.version,
        "cutoff": cutoff.isoformat(),
    }
    payload = json.loads(candidate.payload)
    if stream_key in NO_REFERENCE_STREAMS:
        rows = _decode_known_publication_payload(
            stream_key,
            payload,
            season=season,
            retrieved_at=cutoff,
        )
        return {**result, "status": "no_reference", "rows": len(rows), "passed": True}

    # The reference window comes from the governed games, never from the
    # payload under audit: a candidate that omits a slate must not shrink it.
    governance = governance_for(session, candidate)
    if governance is None:
        return {**result, "status": "ungoverned", "passed": False}
    date_to = max(slate_date_for_instant(event["scheduled_at"]) for event in governance.events)
    # (team, game) or game identities the payload lacks / the governance lacks.
    outside_window = 0
    governed_counts: dict[str, int] | None = None
    if stream_key == "player_game_logs":
        logs = decode_player_game_logs(payload, season=season)
        covered = {log.game_id for log in logs}
        missing = governance.expected_game_ids - covered
        ungoverned = covered - governance.expected_game_ids
        ledger = {
            f"{log.player_id}:{log.game_id}": {field: float(getattr(log, field)) for field in LOG_FIELDS}
            for log in logs
        }
        # Date-bounded NBA.com can hold games the candidate does not govern
        # (a same-day game that finished later); only governed games compare.
        records = nba.player_game_logs(season, date_to)
        governed_records = [r for r in records if str(r["GAME_ID"]) in governance.expected_game_ids]
        outside_window = len(records) - len(governed_records)
        reference = _nba_rows(
            governed_records,
            lambda r: f"{int(r['PLAYER_ID'])}:{r['GAME_ID']}",
            LOG_FIELDS,
        )
        tolerance = LOG_TOLERANCE
    elif stream_key == "player_per36":
        # The payload names no games, so only the window is bound here.
        missing = ungoverned = set()
        facts = decode_player_per36(payload, season=season)
        ledger = {
            str(fact.player_id): {
                **{field: getattr(fact, field) for field in PER36_FIELDS},
                "game_count": float(fact.game_count),
            }
            for fact in facts
        }
        reference = _nba_rows(
            nba.player_per36(season, date_to),
            lambda r: str(int(r["PLAYER_ID"])),
            PER36_FIELDS,
            {"game_count": "GP"},
        )
        tolerance = PER36_TOLERANCE
        # The payload names no games: its own count is the governed count.
        governed_counts = {i: int(row["game_count"]) for i, row in ledger.items()}
    else:
        _decode_known_publication_payload(
            stream_key,
            payload,
            season=season,
            retrieved_at=cutoff,
        )
        team_rows = payload["rows"] if isinstance(payload, dict) else payload
        expected = (
            governance.expected_l15_game_ids
            if stream_key.endswith("_l15")
            else governance.expected_season_game_ids
        )
        held = {(int(row["team_id"]), str(g)) for row in team_rows for g in row["game_ids"]}
        wanted = {(team, g) for team, games in expected.items() for g in games}
        missing = {f"{team}:{g}" for team, g in wanted - held}
        ungoverned = {f"{team}:{g}" for team, g in held - wanted}
        ledger = {
            str(row["team_id"]): {
                **{field: float(row["counts"][field]) for field in TEAM_FIELDS if field in row["counts"]},
                "game_count": float(row["game_count"]),
            }
            for row in team_rows
        }
        # The v1 taxonomy has no rebound split; compare what the row carries.
        columns = {f: c for f, c in TEAM_FIELDS.items() if f in next(iter(ledger.values()))}
        reference = _nba_rows(
            nba.team_opponent_totals(
                season,
                date_to,
                15 if stream_key.endswith("_l15") else 0,
            ),
            lambda r: str(int(r["TEAM_ID"])),
            columns,
            {"game_count": "GP"},
        )
        tolerance = {}
        governed_counts = {str(team): len(games) for team, games in expected.items()}
    # NBA.com saw more games than the candidate governs: its window is not
    # the candidate's, so those identities cannot be compared stat by stat.
    incomparable = [
        {"identity": i, "nba_games": int(reference[i]["game_count"]), "governed_games": count}
        for i, count in sorted((governed_counts or {}).items())
        if i in reference and reference[i]["game_count"] > count
    ]
    for entry in incomparable:
        ledger.pop(entry["identity"], None)
        reference.pop(entry["identity"])
    comparison = compare_rows(ledger, reference, tolerance)
    return {
        **result,
        "status": "window_incomparable" if incomparable else "compared",
        "incomparable": incomparable,
        "reference_outside_window": outside_window,
        "date_to": date_to.isoformat(),
        "rows_compared": comparison.compared,
        "exact": comparison.exact,
        "within_tolerance": comparison.within_tolerance,
        "mismatch_count": len(comparison.mismatches),
        "ledger_only": list(comparison.ledger_only),
        "nba_only": list(comparison.nba_only),
        "missing_governed_games": sorted(missing),
        "ungoverned_games": sorted(ungoverned),
        "match_rate": comparison.match_rate,
        "top_mismatches": list(comparison.mismatches[:TOP_MISMATCHES]),
    }


def build_report(
    session: Session,
    season: str,
    nba: NbaReference,
    threshold: float,
) -> dict[str, Any]:
    streams = []
    for stream_key in REFERENCE_STREAMS + NO_REFERENCE_STREAMS:
        result = verify_stream(session, stream_key, season, nba)
        if result["status"] in {"compared", "window_incomparable"}:
            result["passed"] = (
                result["status"] == "compared"
                and result["match_rate"] >= threshold
                and not result["missing_governed_games"]
                and not result["ungoverned_games"]
            )
        streams.append(result)
    return {
        "season": season,
        "generated_at": datetime.now(UTC).isoformat(),
        "threshold": threshold,
        "passed": all(item["passed"] for item in streams),
        "streams": streams,
    }


def main(argv: list[str] | None = None, *, nba: NbaReference | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", required=True)
    parser.add_argument("--database-url")
    parser.add_argument("--proxy-url", help="HTTP(S) proxy for NBA.com calls only")
    parser.add_argument("--output", help="also write the JSON report here")
    parser.add_argument(
        "--threshold",
        type=float,
        default=1.0,
        help="minimum match rate per reference stream (default: every identity)",
    )
    args = parser.parse_args(argv)
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("a database target is required: pass --database-url or set DATABASE_URL")
    reference = nba or NbaStatsReference(proxy=args.proxy_url)
    engine = open_read_only_engine(database_url)
    try:
        with Session(engine) as session:
            try:
                report = build_report(session, args.season, reference, args.threshold)
            except NbaReferenceError as error:
                print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
                return 2
    finally:
        engine.dispose()
    report["proxy"] = "configured" if args.proxy_url else "none"
    text = json.dumps(report, sort_keys=True, indent=2)
    print(text)
    if args.output:
        Path(args.output).write_text(text + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
