"""On/off parity between stored lineup segments and PBP Stats WOWY (crf04/statsplus#410).

``record`` turns LiveData documents and PBP Stats WOWY responses into the
compact fixtures under ``tests/fixtures/on_off_segments``: every game's box
score and play-by-play trimmed to the fields the derivation reads (xz JSON),
and each split's PBP Stats rows trimmed to the fields ``/api/on_off`` returns.
``report`` stores the recorded games through the rotation job's write path in
a throwaway SQLite database, asks ``OnOffService`` for each split, and prints
per-field differences against PBP Stats; it exits 1 when a split misses the
parent issue's tolerances.  ``tests/test_on_off_parity.py`` runs the same check.

    python scripts/on_off_parity.py report
    python scripts/on_off_parity.py record --live DIR --wowy DIR --splits SPLITS.json --name bos_2025_26
"""

from __future__ import annotations

import argparse
import json
import lzma
import statistics
import sys
import tempfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "on_off_segments"
WRITTEN_AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
COUNT_FIELDS = ("pts", "reb", "ast", "fg3m", "fga", "fg3a", "tov", "fta")
#: The parent issue's tolerances.
MAX_COUNT_EVENTS = 1
MAX_MINUTES_PCT = 0.05
MAX_POSSESSIONS_PCT = 0.5
MAX_USAGE_PCT = 5.0
USAGE_MIN_MINUTES = 100

_ACTION_FIELDS = (
    "actionNumber",
    "actionType",
    "subType",
    "clock",
    "period",
    "personId",
    "teamId",
    "possession",
    "shotResult",
    "assistPersonId",
)
_WOWY_FIELDS = (
    "EntityId",
    "Name",
    "Minutes",
    "SecondsPlayed",
    "OffPoss",
    "Usage",
    "Points",
    "Rebounds",
    "Assists",
    "FG3M",
    "FG2A",
    "FG3A",
    "Turnovers",
    "FTA",
)


# -- record ----------------------------------------------------------------


def trim_box(box: Mapping[str, Any]) -> dict[str, Any]:
    game = box["game"]
    keep = ("gameId", "gameStatus", "period", "gameEt")
    trimmed: dict[str, Any] = {key: game[key] for key in keep}
    for side in ("homeTeam", "awayTeam"):
        team = game[side]
        trimmed[side] = {
            "teamId": team["teamId"],
            "teamTricode": team["teamTricode"],
            "players": [
                {
                    "personId": player["personId"],
                    "name": player["name"],
                    "starter": player.get("starter"),
                    "statistics": {"minutes": player["statistics"]["minutes"]},
                }
                for player in team["players"]
            ],
        }
    return {"game": trimmed}


def trim_play_by_play(play_by_play: Mapping[str, Any]) -> dict[str, Any]:
    actions = []
    for action in play_by_play["game"]["actions"]:
        trimmed = {key: action[key] for key in _ACTION_FIELDS if action.get(key) not in (None, "")}
        if action["actionType"] == "freethrow":
            trimmed["description"] = action.get("description") or ""
        elif action["actionType"] == "foul" and action.get("descriptor"):
            trimmed["descriptor"] = action["descriptor"]
        actions.append(trimmed)
    return {"game": {"gameId": play_by_play["game"]["gameId"], "actions": actions}}


def record(live: Path, wowy: Path, splits_path: Path, name: str) -> None:
    """Write ``<name>_games.json.xz`` and ``<name>_wowy.json`` from raw documents.

    ``splits_path`` holds ``{"team", "season", "games": [ids], "splits":
    {split: {"on": [...], "off": [...], "file": "<wowy file name>"}}}``.
    """

    spec = json.loads(splits_path.read_text())
    games = {
        game_id: {
            "box": trim_box(json.loads((live / f"boxscore_{game_id}.json").read_text())),
            "pbp": trim_play_by_play(json.loads((live / f"playbyplay_{game_id}.json").read_text())),
        }
        for game_id in spec["games"]
    }
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / f"{name}_games.json.xz").write_bytes(
        lzma.compress(json.dumps(games, separators=(",", ":"), sort_keys=True).encode(), preset=9)
    )
    splits = {}
    for split, side in spec["splits"].items():
        payload = json.loads((wowy / side["file"]).read_text())
        rows = payload.get("payload", payload)["multi_row_table_data"]
        splits[split] = {
            "on": side["on"],
            "off": side["off"],
            "multi_row_table_data": [{key: row.get(key) for key in _WOWY_FIELDS} for row in rows],
        }
    (FIXTURES / f"{name}_wowy.json").write_text(
        json.dumps(
            {"team": spec["team"], "season": spec["season"], "splits": splits}, indent=1, sort_keys=True
        )
        + "\n"
    )


# -- report ----------------------------------------------------------------


def load_games(name: str) -> dict[str, dict[str, Any]]:
    return json.loads(lzma.decompress((FIXTURES / f"{name}_games.json.xz").read_bytes()))


def load_wowy(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}_wowy.json").read_text())


def store_games(engine, season: str, games: Mapping[str, Mapping[str, Any]]) -> None:
    """Store every recorded game as the rotation job does, and cover the season."""

    from app.services.lineup_segment_repository import LineupSegmentRepository
    from app.services.lineup_segments import derive_segments
    from app.services.rotation_derivation import derive_rotation
    from app.services.rotation_repository import RotationRepository

    rotations = RotationRepository(engine)
    for documents in games.values():
        rotation = derive_rotation(documents["box"], documents["pbp"])
        segments = derive_segments(documents["box"], documents["pbp"], season)
        rotations.replace_game(season, rotation, WRITTEN_AT, segments)
    LineupSegmentRepository(engine).record_coverage(season, WRITTEN_AT)


def segment_service(engine):
    from app.config.settings import RuntimeSettings
    from app.services.lineup_segment_repository import LineupSegmentRepository
    from app.services.on_off import OnOffService

    return OnOffService(
        None, RuntimeSettings(environment="testing"), segments=LineupSegmentRepository(engine)
    )


def compare_split(
    ours: Iterable[Mapping[str, Any]], reference: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """One split's differences: count mismatches, total minutes and
    possessions, and usage per player with at least 100 PBP Stats minutes."""

    ours_by_id = {player["player_id"]: player for player in ours}
    ref_by_id = {player["player_id"]: player for player in reference}
    zero = {"minutes": 0, "possessions": 0, "usage": None, "totals": dict.fromkeys(COUNT_FIELDS, 0)}
    count_mismatches = []
    possession_mismatches = []
    usage_pct = []
    sums = {"minutes": [0, 0], "possessions": [0, 0]}
    for player_id in sorted(set(ours_by_id) | set(ref_by_id)):
        mine = ours_by_id.get(player_id, zero)
        theirs = ref_by_id.get(player_id, zero)
        for key in COUNT_FIELDS:
            if mine["totals"][key] != theirs["totals"][key]:
                count_mismatches.append((player_id, key, mine["totals"][key], theirs["totals"][key]))
        for key in sums:
            sums[key][0] += mine[key]
            sums[key][1] += theirs[key]
        if mine["possessions"] != theirs["possessions"]:
            possession_mismatches.append((player_id, mine["possessions"], theirs["possessions"]))
        if theirs["minutes"] >= USAGE_MIN_MINUTES and theirs["usage"]:
            usage_pct.append(abs(100 * ((mine["usage"] or 0) - theirs["usage"]) / theirs["usage"]))
    return {
        "count_events": sum(abs(mine - theirs) for _, _, mine, theirs in count_mismatches),
        "count_mismatches": count_mismatches,
        "possession_mismatches": possession_mismatches,
        "minutes_pct": _pct(*sums["minutes"]),
        "possessions_pct": _pct(*sums["possessions"]),
        "usage_pct_worst": max(usage_pct, default=0.0),
        "usage_pct_median": statistics.median(usage_pct) if usage_pct else 0.0,
        "count_pct": {key: _pct(*_field_sums(ours_by_id, ref_by_id, key)) for key in COUNT_FIELDS},
    }


def within_tolerance(result: Mapping[str, Any]) -> bool:
    return (
        result["count_events"] <= MAX_COUNT_EVENTS
        and result["minutes_pct"] <= MAX_MINUTES_PCT
        and result["possessions_pct"] <= MAX_POSSESSIONS_PCT
        and result["usage_pct_worst"] <= MAX_USAGE_PCT
    )


def parity(name: str, engine) -> dict[str, dict[str, Any]]:
    """Every recorded split of fixture ``name`` against its PBP Stats rows."""

    from app.services.on_off import normalize_rows
    from app.services.pbp_stats_adapter import PBPTotalsAdapter

    wowy = load_wowy(name)
    store_games(engine, wowy["season"], load_games(name))
    service = segment_service(engine)
    results = {}
    for split, side in wowy["splits"].items():
        response = service.get(wowy["team"], wowy["season"], on=side["on"], off=side["off"])
        assert response["source"] == "statsplus", response["source"]
        reference = normalize_rows(PBPTotalsAdapter.parse_wowy(side))
        results[split] = compare_split(response["players"], reference)
    return results


def _field_sums(ours, reference, key) -> tuple[int, int]:
    return (
        sum(player["totals"][key] for player in ours.values()),
        sum(player["totals"][key] for player in reference.values()),
    )


def _pct(mine: float, theirs: float) -> float:
    return abs(100 * (mine - theirs) / theirs) if theirs else float(mine != 0) * 100


def _report(names: Iterable[str]) -> int:
    from sqlalchemy import create_engine

    from app.migrations import run_migrations

    failed = False
    for name in names:
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(f"sqlite:///{Path(directory) / 'parity.db'}")
            run_migrations(engine)
            results = parity(name, engine)
            engine.dispose()
        print(f"\n{name}")
        print(f"{'split':28} {'events':>6} {'min %':>7} {'poss %':>7} {'usg med %':>9} {'usg worst %':>11}")
        for split, result in results.items():
            ok = within_tolerance(result)
            failed = failed or not ok
            print(
                f"{split:28} {result['count_events']:6d} {result['minutes_pct']:7.3f} "
                f"{result['possessions_pct']:7.3f} {result['usage_pct_median']:9.2f} "
                f"{result['usage_pct_worst']:11.2f}{'' if ok else '  OUT OF TOLERANCE'}"
            )
            for mismatch in result["count_mismatches"]:
                print(
                    f"{'':28} player {mismatch[0]} {mismatch[1]}: ours {mismatch[2]}, PBP Stats {mismatch[3]}"
                )
        print(f"\n{'field':12} {'median % across splits':>24} {'worst %':>9}")
        for label, values in (
            ("minutes", [r["minutes_pct"] for r in results.values()]),
            ("possessions", [r["possessions_pct"] for r in results.values()]),
            ("usage", [r["usage_pct_worst"] for r in results.values()]),
            *((key, [r["count_pct"][key] for r in results.values()]) for key in COUNT_FIELDS),
        ):
            print(f"{label:12} {statistics.median(values):24.3f} {max(values):9.3f}")
    return 1 if failed else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report", help="compare the recorded splits")
    report.add_argument("--fixture", action="append", help="fixture name (default: all recorded)")
    recorder = commands.add_parser("record", help="write fixtures from raw documents")
    recorder.add_argument(
        "--live", type=Path, required=True, help="directory of LiveData boxscore_/playbyplay_ files"
    )
    recorder.add_argument("--wowy", type=Path, required=True, help="directory of PBP Stats WOWY responses")
    recorder.add_argument("--splits", type=Path, required=True)
    recorder.add_argument("--name", required=True)
    args = parser.parse_args(argv)
    if args.command == "record":
        record(args.live, args.wowy, args.splits, args.name)
        return 0
    names = args.fixture or sorted(
        path.name.removesuffix("_wowy.json") for path in FIXTURES.glob("*_wowy.json")
    )
    return _report(names)


if __name__ == "__main__":
    raise SystemExit(main())
