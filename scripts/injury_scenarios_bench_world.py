#!/usr/bin/env python3
"""Seed a throwaway Postgres with a production-sized Injury Scenarios world.

``GET /api/injury_scenarios`` reads game-log Publications (indexed projection
rows), stored rotations, lineup segments, the Event Catalog, the Athlete
Catalog and stored injury reports.  This writes all of them, through the same
repositories the jobs use, at production volume: 30 teams, a full 2025-26
(1230 games) and 2026-27 through 2026-11-23, with injury spells, a deadline
trade wave and offseason moves.  The Slate is 2026-11-24: eight games with a
stored report listing about five players per team.  Every value comes from a
seeded random generator, so two runs write the same world.

    python scripts/injury_scenarios_bench_world.py --database-url postgresql://...

Then time the route with ``scripts/benchmark_routes.py --case
"one_game=injury_scenarios,date=2026-11-24;game_id=0022600256"``.

Not seeded: Player Pool lines (rows read ``no_line``) and Redis (both seasons'
on/off splits come from segments, as in production since #410).  Never point
this at a database that matters: it writes migrations and rows.
"""

from __future__ import annotations

import argparse
import random
import time
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session

from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
from app.domain.publication_integrity import canonical_publication_json
from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog
from app.models.event_catalog import EventCatalogEntry
from app.services.collection_control import PublicationService
from app.services.injury_snapshot_repository import InjurySnapshotRepository, InjurySnapshotScope
from app.services.lineup_segment_repository import LineupSegmentRepository
from app.services.lineup_segments import COUNTS, GameSegments, Segment
from app.services.player_game_log_repository import PlayerGameLogRecord
from app.services.rotation_derivation import GameRotation, PlayerRotation, Stint
from app.services.rotation_repository import RotationRepository

PRIOR, CURRENT = "2025-26", "2026-27"
SLATE = date(2026, 11, 24)
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
TEAMS = sorted(NBA_TEAM_TRICODE_TO_ID.items(), key=lambda item: item[1])
ROSTER = 16
GAME_CS = 48 * 60 * 100


def _pid(team_index: int, slot: int) -> int:
    return 1_700_000 + team_index * 100 + slot


class _League:
    """Who is on which team on a given day, and who is hurt."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.names: dict[int, str] = {}
        # season -> player -> list of (from_day, team_index)
        self.stints: dict[str, dict[int, list[tuple[date, int]]]] = {PRIOR: {}, CURRENT: {}}
        for t in range(len(TEAMS)):
            for slot in range(ROSTER):
                pid = _pid(t, slot)
                self.names[pid] = f"Bench Player {pid}"
                self.stints[PRIOR][pid] = [(date(2025, 10, 1), t)]
        # Deadline wave: 12 players change teams on 2026-02-05.
        for _ in range(12):
            pid = _pid(rng.randrange(30), rng.randrange(2, ROSTER))
            self.stints[PRIOR][pid].append((date(2026, 2, 5), rng.randrange(30)))
        # Offseason: each 2025-26 player keeps his last team unless moved; 3 per team move.
        for pid, history in self.stints[PRIOR].items():
            self.stints[CURRENT][pid] = [(date(2026, 10, 1), history[-1][1])]
        for t in range(len(TEAMS)):
            for slot in rng.sample(range(ROSTER), 3):
                self.stints[CURRENT][_pid(t, slot)] = [(date(2026, 10, 1), rng.randrange(30))]
        # Rookies: two per team in 2026-27.
        for t in range(len(TEAMS)):
            for slot in (ROSTER, ROSTER + 1):
                pid = _pid(t, slot)
                self.names[pid] = f"Rookie Player {pid}"
                self.stints[CURRENT][pid] = [(date(2026, 10, 1), t)]
        self.role = {pid: rng.random() for pid in self.names}  # lower = bigger role
        for t in range(len(TEAMS)):
            for slot in range(5):
                self.role[_pid(t, slot)] = rng.random() * 0.25
        # Injury spells per player and season: (first_day, last_day).
        self.spells: dict[tuple[str, int], list[tuple[date, date]]] = {}
        for season, start in ((PRIOR, date(2025, 10, 21)), (CURRENT, date(2026, 10, 20))):
            for pid in self.names:
                spells = []
                for _ in range(rng.choice((0, 0, 1, 1, 2, 3))):
                    first = start + timedelta(days=rng.randrange(0, 170))
                    spells.append((first, first + timedelta(days=rng.choice((2, 4, 7, 12, 20, 35)))))
                self.spells[(season, pid)] = spells

    def team_of(self, season: str, pid: int, day: date) -> int | None:
        team = None
        for start, t in self.stints[season].get(pid, ()):
            if day >= start:
                team = t
        return team

    def roster(self, season: str, team: int, day: date) -> list[int]:
        return sorted(pid for pid in self.stints[season] if self.team_of(season, pid, day) == team)

    def hurt(self, season: str, pid: int, day: date) -> bool:
        return any(first <= day <= last for first, last in self.spells[(season, pid)])


def _schedule(rng: random.Random, prefix: str, first: date, rounds: int) -> list[tuple[str, date, int, int]]:
    games = []
    for r in range(rounds):
        order = list(range(30))
        rng.shuffle(order)
        for g in range(15):
            day = first + timedelta(days=2 * r + (g % 2))
            home, away = order[2 * g], order[2 * g + 1]
            games.append((f"{prefix}{len(games) + 1:05d}", day, home, away))
    return games


def _box(rng: random.Random, minutes: float) -> dict[str, int]:
    fga = max(0, round(minutes * rng.uniform(0.2, 0.55)))
    fgm = round(fga * rng.uniform(0.35, 0.6))
    fg3a = round(fga * rng.uniform(0.2, 0.5))
    fg3m = min(fgm, round(fg3a * rng.uniform(0.25, 0.45)))
    fta = round(minutes * rng.uniform(0.0, 0.2))
    ftm = round(fta * rng.uniform(0.6, 0.9))
    oreb, dreb = round(minutes * rng.uniform(0, 0.06)), round(minutes * rng.uniform(0.05, 0.25))
    return {
        "fga": fga,
        "fgm": fgm,
        "fg3a": fg3a,
        "fg3m": fg3m,
        "fta": fta,
        "ftm": ftm,
        "pts": 2 * fgm + fg3m + ftm,
        "oreb": oreb,
        "dreb": dreb,
        "ast": round(minutes * rng.uniform(0.02, 0.25)),
        "tov": round(minutes * rng.uniform(0, 0.08)),
    }


def _lineups(rng, players: dict[int, float], team_id: int, base_seg: int) -> list[Segment]:
    """About 30 five-man stretches whose on-floor time tracks each player's minutes."""

    ids = sorted(players)
    segments = []
    left = GAME_CS
    seg = base_seg
    while left > 0:
        duration = min(left, rng.randrange(3000, 18000))
        pool = sorted(ids, key=lambda p: -players[p] * rng.uniform(0.5, 1.5))
        five = tuple(sorted(pool[:5]))
        team_fga = round(duration / 6000 * 1.8)
        counts = {}
        for p in five:
            box = _box(rng, duration / 6000)
            counts[p] = {key: box.get(key, 0) for key in COUNTS} | {"reb": box["oreb"] + box["dreb"]}
        segments.append(
            Segment(
                team_id=team_id,
                seg=seg,
                players=five,
                duration_cs=duration,
                counts=counts,
                team_fga=team_fga,
                team_fta=round(team_fga * 0.25),
                team_tov=round(team_fga * 0.15),
                team_heaves=0,
                off_poss=round(team_fga * 1.1),
            )
        )
        seg += 1
        left -= duration
    return segments


def seed(database_url: str, seed_value: int = 410) -> dict[str, int]:
    rng = random.Random(seed_value)
    engine = create_engine(database_url)
    run_migrations(engine)
    league = _League(rng)
    schedules = {
        PRIOR: _schedule(rng, "00225", date(2025, 10, 21), 82),
        CURRENT: _schedule(rng, "00226", date(2026, 10, 20), 17),
    }
    rotations = RotationRepository(engine)
    counts = {"log_rows": 0, "games": 0, "segment_games": 0}
    events = []
    for season, games in schedules.items():
        rows = []
        for game_id, day, home, away in games:
            tip = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=24)
            events.append(_event(season, game_id, home, away, tip, final=True))
            lines = []
            team_minutes: dict[int, dict[int, float]] = {}
            for t in (home, away):
                tricode, team_id = TEAMS[t]
                opp_tricode, opp_id = TEAMS[away if t == home else home]
                playing = [
                    pid
                    for pid in league.roster(season, t, day)
                    if not league.hurt(season, pid, day) and (league.role[pid] < 0.7 or rng.random() < 0.5)
                ]
                ranked = sorted(playing, key=lambda pid: league.role[pid])
                minutes = {}
                for rank, pid in enumerate(ranked):
                    minutes[pid] = max(2.0, rng.gauss(34 - rank * 2.6, 3.0))
                scale = 240 / sum(minutes.values())
                minutes = {pid: round(m * scale, 1) for pid, m in minutes.items()}
                team_minutes[team_id] = minutes
                for rank, pid in enumerate(ranked):
                    box = _box(rng, minutes[pid])
                    record = PlayerGameLogRecord(
                        season=season,
                        season_type="Regular Season",
                        player_id=pid,
                        game_id=game_id,
                        player_name=league.names[pid],
                        game_date=day,
                        team_id=team_id,
                        team_tricode=tricode,
                        opponent_team_id=opp_id,
                        opponent_team_tricode=opp_tricode,
                        is_home=t == home,
                        minutes=minutes[pid],
                        points=box["pts"],
                        rebounds=box["oreb"] + box["dreb"],
                        assists=box["ast"],
                        field_goals_made=box["fgm"],
                        field_goals_attempted=box["fga"],
                        three_pointers_made=box["fg3m"],
                        three_pointers_attempted=box["fg3a"],
                        free_throws_made=box["ftm"],
                        free_throws_attempted=box["fta"],
                        offensive_rebounds=box["oreb"],
                        defensive_rebounds=box["dreb"],
                        turnovers=box["tov"],
                        steals=rng.randrange(3),
                        blocks=rng.randrange(3),
                        personal_fouls=rng.randrange(5),
                        usage_pct=round(rng.uniform(10, 32), 1),
                    )
                    rows.append({**asdict(record), "game_date": day.isoformat()})
                    cs = round(minutes[pid] * 6000)
                    half = cs // 2
                    lines.append(
                        PlayerRotation(
                            player_id=pid,
                            name=league.names[pid],
                            team_tricode=tricode,
                            started=rank < 5,
                            minutes_cs=cs,
                            closed=rank < 5,
                            period_cs={
                                "Q1": half // 2,
                                "Q2": half - half // 2,
                                "Q3": cs // 4,
                                "Q4": cs - half - cs // 4,
                                "OT": 0,
                            },
                            stints=(
                                Stint(1, 72000, 2, 36000, half),
                                Stint(3, 72000, 4, 0, cs - half),
                            ),
                        )
                    )
            home_id, away_id = TEAMS[home][1], TEAMS[away][1]
            segments = GameSegments(
                game_id=game_id,
                home_team_id=home_id,
                away_team_id=away_id,
                names={pid: league.names[pid] for minutes in team_minutes.values() for pid in minutes},
                segments=tuple(
                    _lineups(rng, team_minutes[home_id], home_id, 0)
                    + _lineups(rng, team_minutes[away_id], away_id, 1000)
                ),
            )
            rotations.replace_game(
                season,
                GameRotation(
                    game_id=game_id,
                    game_date=day.isoformat(),
                    home_tricode=TEAMS[home][0],
                    away_tricode=TEAMS[away][0],
                    periods=4,
                    players=tuple(lines),
                ),
                NOW,
                segments,
            )
            counts["games"] += 1
            counts["segment_games"] += 1
        _compose(engine, season, rows)
        LineupSegmentRepository(engine).record_coverage(season, NOW)
        counts["log_rows"] += len(rows)
    slate = _slate(rng)
    for game_id, home, away in slate:
        tip = datetime.combine(SLATE, datetime.min.time(), tzinfo=UTC) + timedelta(hours=24, minutes=30)
        events.append(_event(CURRENT, game_id, home, away, tip, final=False))
    with engine.begin() as connection:
        connection.execute(insert(EventCatalogEntry), events)
        connection.execute(insert(AthleteCatalog), _athletes(league))
    counts["injury_rows"] = _reports(engine, rng, league, slate)
    counts["past_report_rows"] = _past_reports(engine, rng, league, schedules)
    engine.dispose()
    return counts


def _event(season, game_id, home, away, tip, *, final):
    (home_tri, home_id), (away_tri, away_id) = TEAMS[home], TEAMS[away]
    return {
        "nba_game_id": game_id,
        "season": season,
        "home_team_id": home_id,
        "home_team_name": home_tri,
        "home_team_tricode": home_tri,
        "away_team_id": away_id,
        "away_team_name": away_tri,
        "away_team_tricode": away_tri,
        "scheduled_at": tip,
        "status_text": "Final" if final else "7:30 pm ET",
        "status_code": 3 if final else 1,
        "postponed_status": None,
        "postponement_evidence": None,
        "classification": "Regular Season",
        "first_seen_at": tip - timedelta(days=90),
        "last_seen_at": NOW,
    }


def _slate(rng) -> list[tuple[str, int, int]]:
    order = list(range(30))
    rng.shuffle(order)
    return [(f"00226{256 + g:05d}", order[2 * g], order[2 * g + 1]) for g in range(8)]


def _athletes(league: _League) -> list[dict]:
    rows = []
    for season, day in ((PRIOR, date(2026, 4, 12)), (CURRENT, SLATE)):
        for pid in league.stints[season]:
            team = league.team_of(season, pid, day)
            tri, team_id = TEAMS[team]
            rows.append(
                {
                    "season": season,
                    "player_id": pid,
                    "display_name": league.names[pid],
                    "roster_status": "active",
                    "is_active": True,
                    "is_active_for_season": True,
                    "team_id": team_id,
                    "team_name": tri,
                    "team_abbreviation": tri,
                    "published_at": NOW,
                }
            )
    return rows


def _reports(engine, rng, league: _League, slate) -> int:
    statuses = ("Out", "Out", "Questionable", "Doubtful", "Probable")
    games = []
    for game_id, home, away in slate:
        entries = []
        for t in (home, away):
            tricode = TEAMS[t][0]
            roster = league.roster(CURRENT, t, SLATE)
            sampled = rng.sample(roster, 6)
            for pid, status in zip(sampled, statuses, strict=False):
                entries.append(
                    {
                        "canonical_player_id": pid,
                        "tricode": tricode,
                        "canonical_status": status,
                        "raw_status": status,
                        "source_player_name": league.names[pid],
                        "game_date": SLATE.isoformat(),
                        "reason": "Injury/Illness - Left Ankle; Sprain",
                        "report_time": "2026-11-24T17:00:00-05:00",
                        "source_url": "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-24_05_00PM.pdf",
                    }
                )
            # One G League assignment per team: skipped unless named.
            pid = sampled[-1]
            entries.append(
                {
                    "canonical_player_id": pid,
                    "tricode": tricode,
                    "canonical_status": "Out",
                    "raw_status": "Out",
                    "source_player_name": league.names[pid],
                    "game_date": SLATE.isoformat(),
                    "reason": "G League - Two-Way",
                    "report_time": "2026-11-24T17:00:00-05:00",
                    "source_url": "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-24_05_00PM.pdf",
                }
            )
        games.append((InjurySnapshotScope(CURRENT, game_id), entries, 0))
    return InjurySnapshotRepository(engine).store_backfilled_report(
        source="nba_official", raw_payload=[], source_entries=[], retrieved_at=NOW, games=games
    )


def _past_reports(engine, rng, league: _League, schedules) -> int:
    """A stored report for every past game: its hurt players Out, one teammate Probable.

    Team windows read a season's reports (``get_season_mentions``), so the
    season holds as many snapshots as production does.
    """

    inserted = 0
    for season, games in schedules.items():
        stored = []
        for game_id, day, home, away in games:
            entries = []
            for t in (home, away):
                roster = league.roster(season, t, day)
                listed = [(pid, "Out") for pid in roster if league.hurt(season, pid, day)]
                listed.append((rng.choice(roster), "Probable"))
                entries += [
                    {
                        "canonical_player_id": pid,
                        "tricode": TEAMS[t][0],
                        "canonical_status": status,
                        "raw_status": status,
                        "source_player_name": league.names[pid],
                        "game_date": day.isoformat(),
                        "reason": "Injury/Illness - Left Ankle; Sprain",
                        "report_time": f"{day.isoformat()}T17:00:00-05:00",
                        "source_url": f"https://ak-static.cms.nba.com/referee/injury/Injury-Report_{day.isoformat()}_05_00PM.pdf",
                    }
                    for pid, status in listed
                ]
            stored.append((InjurySnapshotScope(season, game_id), entries, 0))
        inserted += InjurySnapshotRepository(engine).store_backfilled_report(
            source="nba_official", raw_payload=[], source_entries=[], retrieved_at=NOW, games=stored
        )
    return inserted


def _compose(engine, season: str, rows: list[dict]) -> None:
    """Activate one season's game-log Publication through the pointer-advancing write path.

    The same path the reader fixtures use (``tests/services/test_backtest_seasons.World``):
    it builds the indexed projection rows production reads.
    """

    from tests.support.season_rollover import cut_over_if_held

    publications = PublicationService(engine, clock=lambda: NOW)
    publications.register_stream(
        "player_game_logs",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
        freshness_rule="cutoff_current",
    )
    payload = {"rows": rows}
    with Session(engine) as session, session.begin():
        version = publications._compose_active_in_session(
            session,
            stream_key="player_game_logs",
            season=season,
            cutoff=NOW,
            encoded=canonical_publication_json(payload),
            payload=payload,
            expected_fence=None,
            reason=None,
            provenance_ids=set(),
            now=NOW,
            derive_expected_fence_from_lock=True,
        )
        cut_over_if_held(publications, version, session=session)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--seed", type=int, default=410)
    args = parser.parse_args()
    started = time.perf_counter()
    counts = seed(args.database_url, args.seed)
    print({**counts, "seconds": round(time.perf_counter() - started, 1)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
