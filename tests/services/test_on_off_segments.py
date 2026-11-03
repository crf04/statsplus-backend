"""On/off splits from stored lineup segments (crf04/statsplus#410).

The derivation rules on small hand-built games, the rotation job's write path
and backfill on recorded BOS 2025-26 games, and ``OnOffService`` serving a
covered season while an uncovered one keeps the PBP Stats path unchanged.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import requests
from sqlalchemy import create_engine, insert, text

from app.config.settings import CacheSettings, RuntimeSettings
from app.migrations import run_migrations
from app.models.event_catalog import EventCatalogEntry
from app.services.injury_scenarios import _OnOffReads
from app.services.lineup_segment_repository import (
    LineupSegmentRepository,
    RosterSlotsExhausted,
    mask_of,
    write_game,
)
from app.services.lineup_segments import COUNTS, GameSegments, Segment, derive_segments
from app.services.on_off import OnOffService
from app.services.rotation_collection import RotationCollectionService
from app.services.rotation_derivation import RotationRejected
from app.services.rotation_repository import RotationRepository
from scripts.lineup_segment_backfill import backfill
from scripts.on_off_parity import load_games
from tests.services.test_on_off import NOW, Clock, FakeProvider, FakeRedis

BOS, DEN, NOP = 1610612738, 1610612743, 1610612740
TATUM, BROWN = 1628369, 1627759
WRITTEN_AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)

# -- the rules, on hand-built games -----------------------------------------------


def _box():
    def team(team_id, tricode, first):
        return {
            "teamId": team_id,
            "teamTricode": tricode,
            "players": [
                {"personId": first + n, "name": f"{tricode} {n}", "starter": "1" if n < 5 else "0"}
                for n in range(6)
            ],
        }

    return {
        "game": {
            "gameId": "0022500001",
            "period": 1,
            "homeTeam": team(BOS, "BOS", 1),
            "awayTeam": team(DEN, "DEN", 11),
        }
    }


def _game(*actions):
    """Play-by-play of one period: a start, ``actions`` (``(type, clock, fields)``), an end."""

    rows = [
        ("period", "PT12M00.00S", {"subType": "start"}),
        *actions,
        ("period", "PT00M00.00S", {"subType": "end"}),
    ]
    return {
        "game": {
            "gameId": "0022500001",
            "actions": [
                {"actionNumber": number, "actionType": kind, "period": 1, "clock": clock, **fields}
                for number, (kind, clock, fields) in enumerate(rows, start=1)
            ],
        }
    }


def _bos(segments, *, holding=None):
    """BOS segments, or the one whose lineup holds player ``holding``."""

    rows = [segment for segment in segments.segments if segment.team_id == BOS]
    return rows if holding is None else [segment for segment in rows if holding in segment.players]


def test_free_throws_after_a_substitution_count_for_the_lineup_at_the_foul():
    play_by_play = _game(
        ("foul", "PT05M00.00S", {"teamId": DEN, "personId": 11, "subType": "personal"}),
        ("substitution", "PT05M00.00S", {"teamId": BOS, "personId": 2, "subType": "out"}),
        ("substitution", "PT05M00.00S", {"teamId": BOS, "personId": 6, "subType": "in"}),
        (
            "freethrow",
            "PT05M00.00S",
            {"teamId": BOS, "personId": 1, "subType": "1 of 2", "shotResult": "Made"},
        ),
        (
            "freethrow",
            "PT05M00.00S",
            {"teamId": BOS, "personId": 1, "subType": "2 of 2", "shotResult": "Made"},
        ),
    )

    segments = derive_segments(_box(), play_by_play, "2025-26")

    [at_foul] = _bos(segments, holding=2)
    [after_sub] = _bos(segments, holding=6)
    assert (at_foul.duration_cs, after_sub.duration_cs) == (42000, 30000)
    assert (at_foul.counts[1]["fta"], at_foul.counts[1]["pts"], at_foul.team_fta) == (2, 2, 2)
    assert (after_sub.counts[1]["fta"], after_sub.counts[1]["pts"], after_sub.team_fta) == (0, 0, 0)
    # The possession the last free throw ends is the foul lineup's too.
    assert (at_foul.off_poss, after_sub.off_poss) == (1, 0)


def test_a_technical_during_a_trip_leaves_the_trips_free_throws_with_the_lineup_at_the_shooting_foul():
    play_by_play = _game(
        ("foul", "PT05M00.00S", {"teamId": DEN, "personId": 11, "subType": "personal"}),
        ("substitution", "PT05M00.00S", {"teamId": BOS, "personId": 2, "subType": "out"}),
        ("substitution", "PT05M00.00S", {"teamId": BOS, "personId": 6, "subType": "in"}),
        ("foul", "PT05M00.00S", {"teamId": DEN, "personId": 11, "subType": "technical"}),
        (
            "freethrow",
            "PT05M00.00S",
            {
                "teamId": BOS,
                "personId": 1,
                "subType": "1 of 1",
                "shotResult": "Made",
                "description": "BOS 1 technical Free Throw 1 of 1 (1 PTS)",
            },
        ),
        (
            "freethrow",
            "PT05M00.00S",
            {"teamId": BOS, "personId": 1, "subType": "1 of 2", "shotResult": "Made"},
        ),
        (
            "freethrow",
            "PT05M00.00S",
            {"teamId": BOS, "personId": 1, "subType": "2 of 2", "shotResult": "Made"},
        ),
    )

    segments = derive_segments(_box(), play_by_play, "2025-26")

    [at_foul] = _bos(segments, holding=2)
    [after_sub] = _bos(segments, holding=6)
    assert (at_foul.counts[1]["fta"], at_foul.team_fta, at_foul.off_poss) == (2, 2, 1)
    assert (after_sub.counts[1]["fta"], after_sub.team_fta, after_sub.off_poss) == (1, 1, 0)


def test_a_technical_free_throw_without_the_word_technical_counts_for_the_lineup_at_the_technical():
    """2026-27 descriptions no longer name the technical; its free throw still
    belongs to the technical foul, not to an earlier personal foul."""

    play_by_play = _game(
        ("foul", "PT09M00.00S", {"teamId": DEN, "personId": 11, "subType": "personal"}),
        ("substitution", "PT07M00.00S", {"teamId": BOS, "personId": 2, "subType": "out"}),
        ("substitution", "PT07M00.00S", {"teamId": BOS, "personId": 6, "subType": "in"}),
        ("foul", "PT06M00.00S", {"teamId": DEN, "personId": 11, "subType": "technical"}),
        (
            "freethrow",
            "PT06M00.00S",
            {
                "teamId": BOS,
                "personId": 1,
                "subType": "1 of 1",
                "shotResult": "Made",
                "description": "x 1 of 1",
            },
        ),
    )

    segments = derive_segments(_box(), play_by_play, "2026-27")

    [before_sub] = _bos(segments, holding=2)
    [after_sub] = _bos(segments, holding=6)
    assert (before_sub.team_fta, after_sub.team_fta, after_sub.counts[1]["pts"]) == (0, 1, 1)


def test_a_technical_free_throw_answers_the_other_teams_technical():
    play_by_play = _game(
        ("foul", "PT06M00.00S", {"teamId": BOS, "personId": 2, "subType": "technical"}),
        ("substitution", "PT06M00.00S", {"teamId": BOS, "personId": 2, "subType": "out"}),
        ("substitution", "PT06M00.00S", {"teamId": BOS, "personId": 6, "subType": "in"}),
        ("foul", "PT06M00.00S", {"teamId": DEN, "personId": 11, "subType": "technical"}),
        (
            "freethrow",
            "PT06M00.00S",
            {"teamId": BOS, "personId": 1, "subType": "1 of 1", "shotResult": "Made"},
        ),
    )

    segments = derive_segments(_box(), play_by_play, "2026-27")

    assert [(segment.players, segment.team_fta) for segment in _bos(segments)] == [
        ((1, 2, 3, 4, 5), 0),
        ((1, 3, 4, 5, 6), 1),
    ]


def test_a_technical_whose_free_throw_was_never_shot_claims_no_later_free_throw():
    """Technicals whose free throws never come claim no 1-of-1 at a later
    clock; it answers the personal foul before it."""

    play_by_play = _game(
        ("foul", "PT08M00.00S", {"teamId": DEN, "personId": 11, "subType": "technical"}),
        ("foul", "PT08M00.00S", {"teamId": BOS, "personId": 1, "subType": "technical"}),
        ("substitution", "PT07M00.00S", {"teamId": BOS, "personId": 2, "subType": "out"}),
        ("substitution", "PT07M00.00S", {"teamId": BOS, "personId": 6, "subType": "in"}),
        ("foul", "PT06M00.00S", {"teamId": DEN, "personId": 11, "subType": "personal"}),
        ("substitution", "PT06M00.00S", {"teamId": BOS, "personId": 3, "subType": "out"}),
        ("substitution", "PT06M00.00S", {"teamId": BOS, "personId": 2, "subType": "in"}),
        (
            "freethrow",
            "PT06M00.00S",
            {"teamId": BOS, "personId": 1, "subType": "1 of 1", "shotResult": "Made"},
        ),
    )

    segments = derive_segments(_box(), play_by_play, "2026-27")

    assert [(segment.players, segment.team_fta) for segment in _bos(segments)] == [
        ((1, 2, 3, 4, 5), 0),
        ((1, 3, 4, 5, 6), 1),
        ((1, 2, 4, 5, 6), 0),
    ]


def test_offsetting_double_technicals_leave_a_missed_and_one_and_its_putback_one_possession():
    """Recorded DEN game 0022600140, Q3 1:04: a NOP layup, a shooting foul, the
    and-one missed, an offensive rebound and a putback.  Double technicals at
    that clock offset and award no free throw, so the 1-of-1 is still the and-one."""

    original = load_games("den_2026_27")["0022600140"]
    game = copy.deepcopy(original)
    actions = game["pbp"]["game"]["actions"]
    at = next(index for index, action in enumerate(actions) if action["actionNumber"] == 492)
    actions[at:at] = [
        {
            "actionType": "foul",
            "subType": "technical",
            "descriptor": "double",
            "period": 3,
            "clock": "PT01M04.00S",
            "teamId": team_id,
            "personId": person_id,
            "possession": NOP,
        }
        for team_id, person_id in ((DEN, 203999), (NOP, 1642852))
    ]

    segments = derive_segments(game["box"], game["pbp"], "2026-27").segments

    assert sum(segment.off_poss for segment in segments if segment.team_id == NOP) == 100
    assert segments == derive_segments(original["box"], original["pbp"], "2026-27").segments


def test_an_and_one_keeps_the_possession_until_its_free_throw():
    play_by_play = _game(
        ("2pt", "PT06M00.00S", {"teamId": BOS, "personId": 1, "shotResult": "Made"}),
        ("foul", "PT06M00.00S", {"teamId": DEN, "personId": 11, "subType": "personal"}),
        (
            "freethrow",
            "PT06M00.00S",
            {"teamId": BOS, "personId": 1, "subType": "1 of 1", "shotResult": "Made"},
        ),
    )

    [segment] = _bos(derive_segments(_box(), play_by_play, "2025-26"))

    assert (segment.counts[1]["pts"], segment.off_poss) == (3, 1)


def test_a_technical_free_throw_does_not_end_the_possession():
    play_by_play = _game(
        ("foul", "PT06M00.00S", {"teamId": DEN, "personId": 11, "subType": "technical"}),
        (
            "freethrow",
            "PT06M00.00S",
            {
                "teamId": BOS,
                "personId": 1,
                "subType": "1 of 1",
                "shotResult": "Made",
                "description": "BOS 1 Free Throw Technical (1 PTS)",
            },
        ),
        ("2pt", "PT05M40.00S", {"teamId": BOS, "personId": 2, "shotResult": "Made"}),
    )

    [segment] = _bos(derive_segments(_box(), play_by_play, "2025-26"))

    assert (segment.counts[1]["fta"], segment.counts[1]["pts"], segment.off_poss) == (1, 1, 1)


@pytest.mark.parametrize(
    ("foul", "trip", "bos_possessions"),
    [
        ({"subType": "technical"}, 1, 1),
        ({"subType": "personal", "descriptor": "flagrant-type-1"}, 2, 1),
        ({"subType": "personal", "descriptor": "away-from-play"}, 1, 1),
        ({"subType": "personal", "descriptor": "shooting"}, 1, 2),
    ],
)
def test_2026_27_free_throws_keep_the_ball_by_their_foul_not_their_description(foul, trip, bos_possessions):
    """2026-27 descriptions read ``Free Throw 1 of 1`` whatever the foul, so the
    foul decides: technical, flagrant and away-from-play trips end nothing and
    the made shot after them ends BOS's possession; a shooting foul's trip ends it."""

    play_by_play = _game(
        ("foul", "PT06M00.00S", {"teamId": DEN, "personId": 11, **foul}),
        *(
            (
                "freethrow",
                "PT06M00.00S",
                {
                    "teamId": BOS,
                    "personId": 1,
                    "subType": f"{shot} of {trip}",
                    "shotResult": "Made",
                    "description": f"BOS 1 Free Throw {shot} of {trip} ({shot} PTS)",
                },
            )
            for shot in range(1, trip + 1)
        ),
        ("2pt", "PT05M40.00S", {"teamId": BOS, "personId": 2, "shotResult": "Made"}),
    )

    segments = derive_segments(_box(), play_by_play, "2026-27").segments

    # DEN's one possession runs from BOS's made shot to the end of the period.
    assert {segment.team_id: segment.off_poss for segment in segments} == {BOS: bos_possessions, DEN: 1}


@pytest.mark.parametrize(
    ("foul", "bos_possessions"),
    [
        ({"subType": "technical"}, 2),
        ({"subType": "personal", "descriptor": "flagrant-type-1"}, 1),
        ({"subType": "personal", "descriptor": "shooting"}, 2),
    ],
)
def test_a_1_of_1_at_a_made_shots_clock_is_an_and_one_by_its_foul(foul, bos_possessions):
    """A technical's 1-of-1 is no and-one, so the shot ends the possession; a
    flagrant's is, and keeps the ball; a shooting foul's is, and its make ends it."""

    play_by_play = _game(
        ("2pt", "PT06M00.00S", {"teamId": BOS, "personId": 1, "shotResult": "Made"}),
        ("foul", "PT06M00.00S", {"teamId": DEN, "personId": 11, **foul}),
        (
            "freethrow",
            "PT06M00.00S",
            {
                "teamId": BOS,
                "personId": 1,
                "subType": "1 of 1",
                "shotResult": "Made",
                "description": "BOS 1 Free Throw 1 of 1 (3 PTS)",
            },
        ),
        ("2pt", "PT05M40.00S", {"teamId": BOS, "personId": 2, "shotResult": "Made"}),
    )

    segments = derive_segments(_box(), play_by_play, "2026-27").segments

    assert {segment.team_id: segment.off_poss for segment in segments} == {BOS: bos_possessions, DEN: 1}


def test_the_dead_ball_rebound_after_a_missed_technical_free_throw_ends_no_possession():
    play_by_play = _game(
        ("foul", "PT06M00.00S", {"teamId": DEN, "personId": 11, "subType": "technical"}),
        (
            "freethrow",
            "PT06M00.00S",
            {
                "teamId": BOS,
                "personId": 1,
                "subType": "1 of 1",
                "shotResult": "Missed",
                "description": "MISS BOS 1 technical Free Throw 1 of 1",
            },
        ),
        ("rebound", "PT06M00.00S", {"teamId": DEN, "personId": 0, "subType": "defensive"}),
        ("2pt", "PT05M40.00S", {"teamId": BOS, "personId": 2, "shotResult": "Made"}),
    )

    segments = derive_segments(_box(), play_by_play, "2025-26").segments

    # BOS's one possession ends at the made shot; DEN's then runs to the end of the period.
    assert {segment.team_id: segment.off_poss for segment in segments} == {BOS: 1, DEN: 1}


@pytest.mark.parametrize(
    ("season", "shooter", "team_fga", "team_heaves"),
    [("2026-27", (1, 1), 1, 0), ("2025-26", (0, 0), 0, 1)],
)
def test_from_2026_27_a_heave_is_its_shooters_missed_three(season, shooter, team_fga, team_heaves):
    play_by_play = _game(("heave", "PT00M01.00S", {"teamId": BOS, "personId": 1}))

    [segment] = _bos(derive_segments(_box(), play_by_play, season))

    assert (segment.counts[1]["fga"], segment.counts[1]["fg3a"]) == shooter
    assert (segment.team_fga, segment.team_heaves) == (team_fga, team_heaves)


def test_a_heave_with_no_shooter_is_a_player_less_heave_in_every_season():
    play_by_play = _game(("heave", "PT00M01.00S", {"teamId": BOS}))

    [segment] = _bos(derive_segments(_box(), play_by_play, "2026-27"))

    assert (segment.team_fga, segment.team_heaves, segment.counts[1]["fga"]) == (0, 1, 0)


def _without_period_start(game, period):
    broken = copy.deepcopy(game)
    actions = broken["pbp"]["game"]["actions"]
    actions.remove(
        next(
            a
            for a in actions
            if a["actionType"] == "period" and a.get("subType") == "start" and a["period"] == period
        )
    )
    return broken


def test_a_missing_period_start_action_still_gives_each_team_the_whole_game(bos_games):
    game = _without_period_start(bos_games["0022500017"], 4)

    segments = derive_segments(game["box"], game["pbp"], "2025-26").segments

    for team_id in (BOS, 1610612765):
        durations = [segment.duration_cs for segment in segments if segment.team_id == team_id]
        assert (sum(durations), min(durations) >= 0) == (4 * 72000, True)
    original = bos_games["0022500017"]
    assert segments == derive_segments(original["box"], original["pbp"], "2025-26").segments


def test_a_period_without_its_start_action_begins_at_its_first_action():
    box = _box()
    box["game"]["period"] = 2
    play_by_play = _game(("2pt", "PT06M00.00S", {"teamId": BOS, "personId": 1, "shotResult": "Missed"}))
    actions = play_by_play["game"]["actions"]
    actions.append({**actions[1], "actionNumber": 4, "period": 2, "shotResult": "Made"})
    actions.append({**actions[2], "actionNumber": 5, "period": 2})

    segments = _bos(derive_segments(box, play_by_play, "2025-26"))

    assert [
        (segment.duration_cs, segment.counts[1]["fga"], segment.counts[1]["pts"]) for segment in segments
    ] == [
        (72000, 1, 0),
        (72000, 1, 2),
    ]


def test_a_substitution_listed_after_a_later_one_rejects_the_game_instead_of_a_negative_segment():
    play_by_play = _game(
        ("substitution", "PT05M00.00S", {"teamId": BOS, "personId": 2, "subType": "out"}),
        ("substitution", "PT05M00.00S", {"teamId": BOS, "personId": 6, "subType": "in"}),
        ("2pt", "PT05M00.00S", {"teamId": BOS, "personId": 1, "shotResult": "Missed"}),
        ("substitution", "PT06M00.00S", {"teamId": BOS, "personId": 6, "subType": "out"}),
        ("substitution", "PT06M00.00S", {"teamId": BOS, "personId": 2, "subType": "in"}),
        ("2pt", "PT04M00.00S", {"teamId": BOS, "personId": 1, "shotResult": "Made"}),
    )

    with pytest.raises(RotationRejected, match=f"team {BOS} segment from 42000 cs ends earlier, at 36000 cs"):
        derive_segments(_box(), play_by_play, "2025-26")


def test_a_lineup_short_a_player_for_any_time_rejects_the_game():
    play_by_play = _game(
        ("substitution", "PT06M00.00S", {"teamId": BOS, "personId": 2, "subType": "out"}),
        ("2pt", "PT05M00.00S", {"teamId": BOS, "personId": 1, "shotResult": "Missed"}),
        ("substitution", "PT05M00.00S", {"teamId": BOS, "personId": 6, "subType": "in"}),
    )

    with pytest.raises(RotationRejected, match=f"team {BOS} has 4 on court at 36000 cs"):
        derive_segments(_box(), play_by_play, "2025-26")


def test_an_action_past_the_box_scores_last_period_rejects_the_game(tmp_path, bos_games):
    game = copy.deepcopy(bos_games["0022500017"])
    actions = game["pbp"]["game"]["actions"]
    actions.append({**actions[-1], "actionNumber": actions[-1]["actionNumber"] + 1, "period": 5})
    engine = _engine(tmp_path)

    result = _collection(engine, {"0022500017": game}).collect("2025-26", game_ids=["0022500017"])

    assert result["rejected"] == [
        {"game_id": "0022500017", "reason": "action in period 5 of a 4-period game"}
    ]
    assert _dump(engine)["lineup_segment_players"] == []


def test_a_substitution_batch_across_a_period_boundary_is_stored_like_one_at_the_period_start(
    tmp_path, bos_games
):
    """Q1 0:00 and Q2 12:00 are the same moment, so a batch that starts at one
    and finishes at the other is one lineup change at the boundary."""

    game = copy.deepcopy(bos_games["0022500017"])
    out = next(action for action in game["pbp"]["game"]["actions"] if action["actionNumber"] == 200)
    assert (out["subType"], out["period"], out["clock"]) == ("out", 2, "PT12M00.00S")
    out["period"], out["clock"] = 1, "PT00M00.00S"
    (tmp_path / "moved").mkdir()
    (tmp_path / "recorded").mkdir()
    moved, recorded = _engine(tmp_path / "moved"), _engine(tmp_path / "recorded")

    result = _collection(moved, {"0022500017": game}).collect("2025-26", game_ids=["0022500017"])
    _collection(recorded, bos_games).collect("2025-26", game_ids=["0022500017"])

    assert (result["stored"], result["rejected"]) == (1, [])
    assert len(_dump(moved)["lineup_segment_players"]) == 5 * 49
    assert _dump(moved) == _dump(recorded)


# -- writes ---------------------------------------------------------------------


def _engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'segments.db'}")
    run_migrations(engine)
    return engine


def _dump(engine):
    with engine.connect() as connection:
        return {
            table: sorted(tuple(row) for row in connection.execute(text(f"SELECT * FROM {table}")))
            for table in ("lineup_roster_slots", "lineup_segment_games", "lineup_segment_players")
        }


@pytest.fixture(scope="module")
def bos_games():
    return load_games("bos_2025_26")


class FakeLiveData:
    def __init__(self, games):
        self.games = games
        self.fetched = []

    def fetch_game_stats(self, game_id, season):
        self.fetched.append(game_id)
        return copy.deepcopy(self.games[game_id]["box"])

    def fetch_play_by_play(self, game_id):
        return copy.deepcopy(self.games[game_id]["pbp"])


def _collection(engine, games, events=()):
    return RotationCollectionService(
        FakeLiveData(games), RotationRepository(engine), lambda season: list(events), clock=lambda: WRITTEN_AT
    )


def _rejected(games, game_id):
    """A copy of ``game_id`` whose first substitution is lost, so its lineups break."""

    broken = copy.deepcopy(games[game_id])
    actions = broken["pbp"]["game"]["actions"]
    actions.remove(next(action for action in actions if action["actionType"] == "substitution"))
    return broken


def test_a_rerun_of_the_rotation_job_leaves_identical_segment_rows(tmp_path, bos_games):
    engine = _engine(tmp_path)
    game_ids = sorted(bos_games)[:2]
    collection = _collection(engine, bos_games)

    collection.collect("2025-26", game_ids=game_ids)
    first = _dump(engine)
    collection.collect("2025-26", game_ids=game_ids)

    assert _dump(engine) == first
    assert len(first["lineup_segment_games"]) == 2
    assert len(first["lineup_segment_players"]) == 5 * sum(
        len(derive_segments(bos_games[g]["box"], bos_games[g]["pbp"], "2025-26").segments) for g in game_ids
    )


def test_a_rejected_game_gets_no_segments_and_no_rotation(tmp_path, bos_games):
    engine = _engine(tmp_path)
    game_id = sorted(bos_games)[0]

    result = _collection(engine, {game_id: _rejected(bos_games, game_id)}).collect(
        "2025-26", game_ids=[game_id]
    )

    assert [item["game_id"] for item in result["rejected"]] == [game_id]
    dump = _dump(engine)
    assert dump["lineup_segment_players"] == []
    assert [(row[0], row[5] is not None) for row in dump["lineup_segment_games"]] == [(game_id, True)]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM rotation_stints")).scalar() == 0


def test_segments_roll_back_with_the_stints_when_a_team_season_runs_out_of_slots(tmp_path, bos_games):
    engine = _engine(tmp_path)
    game_id = sorted(bos_games)[0]
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lineup_roster_slots (season, team_id, player_id, slot, player_name) "
                "VALUES " + ", ".join(f"('2025-26', {BOS}, {9000 + slot}, {slot}, 'x')" for slot in range(64))
            )
        )

    with pytest.raises(RosterSlotsExhausted):
        _collection(engine, bos_games).collect("2025-26", game_ids=[game_id])

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM rotation_stints")).scalar() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM lineup_segment_players")).scalar() == 0


def test_the_mask_is_a_signed_64_bit_integer_and_slot_63_still_selects(tmp_path):
    assert (mask_of([0, 2]), mask_of([63]), mask_of(range(64))) == (5, -(1 << 63), -1)
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lineup_roster_slots (season, team_id, player_id, slot, player_name) "
                "VALUES " + ", ".join(f"('2025-26', {BOS}, {9000 + slot}, {slot}, 'x')" for slot in range(59))
            )
        )
        lineup = (1, 2, 3, 4, 5)
        counts = {
            player: dict.fromkeys(("pts", "reb", "ast", "fg3m", "fga", "fg3a", "tov", "fta"), 0)
            for player in lineup
        }
        write_game(
            connection,
            "2025-26",
            GameSegments(
                game_id="0022500001",
                home_team_id=BOS,
                away_team_id=DEN,
                names={player: f"P{player}" for player in lineup},
                segments=(Segment(BOS, 0, lineup, 6000, counts, 0, 0, 0, 0, 0),),
            ),
            WRITTEN_AT,
        )
    repository = LineupSegmentRepository(engine)
    repository.record_coverage("2025-26", WRITTEN_AT)

    on_63 = repository.read_split("2025-26", BOS, on=[5], off=[])
    off_63 = repository.read_split("2025-26", BOS, on=[1], off=[5])

    assert sorted(row["player_id"] for row in on_63.rows) == [1, 2, 3, 4, 5]
    assert off_63.rows == ()


# -- backfill -----------------------------------------------------------------------


def _event(game_id, scheduled_at, home=("HOU", 1610612745), away=("DET", 1610612765)):
    return {
        "nba_game_id": game_id,
        "season": "2025-26",
        "home_team_id": home[1],
        "home_team_name": home[0],
        "home_team_tricode": home[0],
        "away_team_id": away[1],
        "away_team_name": away[0],
        "away_team_tricode": away[0],
        "scheduled_at": scheduled_at,
        "status_text": "Final",
        "status_code": 3,
        "postponed_status": None,
        "postponement_evidence": None,
        "classification": "Regular Season",
        "first_seen_at": scheduled_at,
        "first_observed_started_at": scheduled_at,
        "last_seen_at": scheduled_at,
    }


def test_the_backfill_resumes_skips_written_games_and_records_coverage_when_done(tmp_path, bos_games):
    engine = _engine(tmp_path)
    game_ids = sorted(bos_games)[:3]
    events = [
        _event(game_id, datetime(2025, 11, 1 + n, 23, 0, tzinfo=UTC)) for n, game_id in enumerate(game_ids)
    ]
    collection = _collection(engine, bos_games, events)
    segments = LineupSegmentRepository(engine)

    first = backfill(collection, segments, "2025-26", max_games=2, throttle_seconds=0)
    assert (first["attempted"], first["stored"], first["remaining"], first["covered"]) == (2, 2, 1, False)
    assert not segments.covered("2025-26")

    second = backfill(collection, segments, "2025-26", max_games=2, throttle_seconds=0)
    assert (second["attempted"], second["remaining"], second["covered"]) == (1, 0, True)
    assert collection.provider.fetched == game_ids

    third = backfill(collection, segments, "2025-26", throttle_seconds=0)
    assert (third["attempted"], third["covered"]) == (0, True)
    assert collection.provider.fetched == game_ids
    assert segments.covered("2025-26")


class FlakyLiveData(FakeLiveData):
    """Recorded games, except that ``unreachable`` cannot be fetched."""

    def __init__(self, games, unreachable):
        super().__init__(games)
        self.unreachable = unreachable

    def fetch_game_stats(self, game_id, season):
        if game_id == self.unreachable:
            raise requests.ConnectionError("LiveData unreachable")
        return super().fetch_game_stats(game_id, season)


def test_a_game_whose_fetch_failed_stays_pending_and_the_season_uncovered(tmp_path, bos_games):
    engine = _engine(tmp_path)
    game_ids = sorted(bos_games)[:2]
    events = [
        _event(game_id, datetime(2025, 11, 1 + n, 23, 0, tzinfo=UTC)) for n, game_id in enumerate(game_ids)
    ]
    segments = LineupSegmentRepository(engine)
    flaky = RotationCollectionService(
        FlakyLiveData(bos_games, game_ids[1]),
        RotationRepository(engine),
        lambda season: list(events),
        clock=lambda: WRITTEN_AT,
    )

    first = backfill(flaky, segments, "2025-26", throttle_seconds=0)

    assert [game["game_id"] for game in first["failed"]] == [game_ids[1]]
    assert (first["stored"], first["remaining"], first["covered"]) == (1, 1, False)
    assert not segments.covered("2025-26")
    second = backfill(_collection(engine, bos_games, events), segments, "2025-26", throttle_seconds=0)
    assert (second["attempted"], second["remaining"], second["covered"]) == (1, 0, True)


def test_the_backfill_refuses_seasons_before_2025_26(tmp_path):
    engine = _engine(tmp_path)
    with pytest.raises(ValueError, match="2024-25 keeps PBP Stats"):
        backfill(_collection(engine, {}), LineupSegmentRepository(engine), "2024-25")


# -- reads ----------------------------------------------------------------------


def _settings():
    return RuntimeSettings(environment="testing", cache=CacheSettings(on_off_min_interval_seconds=2))


@pytest.fixture
def covered(tmp_path, bos_games):
    """BOS 2025-26: two games stored, one rejected, the season covered."""

    engine = _engine(tmp_path)
    stored, rejected = sorted(bos_games)[:2], sorted(bos_games)[2]
    games = {**{game_id: bos_games[game_id] for game_id in stored}, rejected: _rejected(bos_games, rejected)}
    _collection(engine, games).collect("2025-26", game_ids=[*stored, rejected])
    with engine.begin() as connection:
        connection.execute(
            insert(EventCatalogEntry.__table__).values(
                # 00:30 UTC on 2025-11-15 is the evening of 2025-11-14 in New York.
                _event(
                    rejected,
                    datetime(2025, 11, 15, 0, 30, tzinfo=UTC),
                    home=("BOS", BOS),
                    away=("DET", 1610612765),
                )
            )
        )
    LineupSegmentRepository(engine).record_coverage("2025-26", WRITTEN_AT)
    return SimpleNamespace(engine=engine, stored=stored, rejected=rejected)


@pytest.fixture
def provider():
    return FakeProvider()


def _service(provider, engine=None, redis=None):
    redis = redis or FakeRedis()
    return OnOffService(
        provider,
        _settings(),
        redis_client=redis,
        segments=None if engine is None else LineupSegmentRepository(engine),
        clock=Clock(),
        sleep=redis.sleep,
    )


class RecordingRedis(FakeRedis):
    """FakeRedis that records the name of every command it receives."""

    COMMANDS = frozenset({"get", "set", "incr", "expire", "exists", "delete", "pttl"})

    def __init__(self):
        super().__init__()
        self.commands = []

    def __getattribute__(self, name):
        if name in RecordingRedis.COMMANDS:
            super().__getattribute__("commands").append(name)
        return super().__getattribute__(name)


def test_a_covered_season_is_served_from_segments_without_pbp_stats_or_redis(covered, provider):
    redis = RecordingRedis()
    service = _service(provider, covered.engine, redis)

    body = service.get("BOS", "2025-26", off=[TATUM], user_id="u1", deadline=0.0)
    cached = service.get_cached("BOS", "2025-26", off=[TATUM])

    assert (provider.calls, redis.commands) == ([], [])
    assert (body["status"], body["source"], body["stale"]) == ("ok", "statsplus", False)
    assert body["cached_at"] == "2026-11-03T12:00:00+00:00"
    assert list(body) == [
        "status",
        "source",
        "team",
        "season",
        "on",
        "off",
        "cached_at",
        "stale",
        "players",
        "games_missing",
    ]
    assert set(body["players"][0]) == {
        "player_id",
        "name",
        "minutes",
        "possessions",
        "usage",
        "totals",
        "per36",
    }
    assert cached == body
    # An uncovered season goes through Redis, so the recorder does see commands.
    service.get("BOS", "2024-25", off=[TATUM])
    assert "get" in redis.commands


def test_another_seasons_segments_for_the_team_stay_out_of_the_split(tmp_path, provider):
    engine = _engine(tmp_path)
    _store_lineup(engine, "0022500001", (1, 2, 3, 4, 5), datetime(2026, 11, 1, 9, 0, tzinfo=UTC))
    _store_lineup(engine, "0022600001", (1, 2, 3, 4, 5), datetime(2026, 11, 2, 9, 0, tzinfo=UTC), "2026-27")
    LineupSegmentRepository(engine).record_coverage("2025-26", WRITTEN_AT)

    body = _service(provider, engine).get("BOS", "2025-26", on=[1])

    assert [(player["player_id"], player["minutes"]) for player in body["players"]] == [
        (1, 1),
        (2, 1),
        (3, 1),
        (4, 1),
        (5, 1),
    ]
    assert body["cached_at"] == "2026-11-01T09:00:00+00:00"


def test_games_missing_names_each_rejected_game_by_its_eastern_date_and_teams(covered, provider):
    body = _service(provider, covered.engine).get("BOS", "2025-26", on=[TATUM])

    assert body["games_missing"] == [
        {"game_id": covered.rejected, "game_date": "2025-11-14", "matchup": "DET @ BOS"}
    ]


class FailingLiveData:
    """LiveData that cannot be reached."""

    def fetch_game_stats(self, game_id, season):
        raise requests.ConnectionError("LiveData unreachable")

    def fetch_play_by_play(self, game_id):
        raise requests.ConnectionError("LiveData unreachable")


def test_games_missing_lists_every_completed_regular_season_game_with_no_stored_segments(covered, provider):
    def at(day):
        return datetime(2025, 11, day, 23, 0, tzinfo=UTC)

    bos_home = {"home": ("BOS", BOS), "away": ("DET", 1610612765)}
    events = [
        _event(covered.stored[0], at(1), **bos_home),  # stored
        _event("0022500900", at(20), **bos_home),  # its fetch fails below
        _event("0022500901", at(18), home=("MIA", 1610612748), away=("BOS", BOS)),  # never collected
        {**_event("0022500902", at(28), **bos_home), "status_text": "7:30 pm ET", "status_code": 1},
        {**_event("0012500001", at(2), **bos_home), "classification": "Preseason"},
        {**_event("0022500904", at(21), **bos_home), "postponed_status": "Postponed"},
        _event("0022500903", at(19)),  # HOU and DET
    ]
    with covered.engine.begin() as connection:
        connection.execute(insert(EventCatalogEntry.__table__), events)
    failed = RotationCollectionService(
        FailingLiveData(), RotationRepository(covered.engine), lambda season: [], clock=lambda: WRITTEN_AT
    ).collect("2025-26", game_ids=["0022500900"])
    assert [game["game_id"] for game in failed["failed"]] == ["0022500900"]

    body = _service(provider, covered.engine).get("BOS", "2025-26", on=[TATUM])

    assert body["games_missing"] == [
        {"game_id": covered.rejected, "game_date": "2025-11-14", "matchup": "DET @ BOS"},
        {"game_id": "0022500901", "game_date": "2025-11-18", "matchup": "BOS @ MIA"},
        {"game_id": "0022500900", "game_date": "2025-11-20", "matchup": "DET @ BOS"},
    ]


def _store_lineup(engine, game_id, lineup, written_at, season="2025-26"):
    counts = {player: dict.fromkeys(COUNTS, 0) for player in lineup}
    with engine.begin() as connection:
        write_game(
            connection,
            season,
            GameSegments(
                game_id,
                BOS,
                DEN,
                {p: f"P{p}" for p in lineup},
                (Segment(BOS, 0, lineup, 6000, counts, 0, 0, 0, 0, 0),),
            ),
            written_at,
        )


def test_cached_at_is_the_newest_write_among_the_games_the_split_includes(tmp_path, provider):
    engine = _engine(tmp_path)
    _store_lineup(engine, "0022500001", (1, 2, 3, 4, 5), datetime(2026, 11, 1, 9, 0, tzinfo=UTC))
    _store_lineup(engine, "0022500002", (1, 2, 3, 4, 5), datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    _store_lineup(engine, "0022500003", (6, 7, 8, 9, 10), datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    LineupSegmentRepository(engine).record_coverage("2025-26", WRITTEN_AT)
    service = _service(provider, engine)

    first_two = service.get("BOS", "2025-26", on=[1])
    all_three = service.get("BOS", "2025-26", off=[203999])

    assert first_two["cached_at"] == "2026-11-02T09:00:00+00:00"
    assert all_three["cached_at"] == "2026-11-03T09:00:00+00:00"


def test_an_on_player_who_never_played_for_the_team_has_an_empty_split(covered, provider):
    body = _service(provider, covered.engine).get("BOS", "2025-26", on=[203999])

    assert (body["status"], body["source"], body["players"]) == ("ok", "statsplus", [])
    assert provider.calls == []


def test_an_uncovered_season_keeps_the_pbp_stats_path_byte_for_byte(tmp_path, bos_games):
    engine = _engine(tmp_path)
    _collection(engine, bos_games).collect("2025-26", game_ids=sorted(bos_games)[:1])
    with_segments, plain = FakeProvider(), FakeProvider()
    segment_service = _service(with_segments, engine)
    pbp_service = _service(plain)

    for service in (segment_service, pbp_service):
        service.get("DEN", "2025-26", off=[203999], user_id="u1")
    responses = [
        json.dumps(service.get("DEN", "2025-26", off=[203999]))
        + json.dumps(service.get_cached("DEN", "2025-26", off=[203999]))
        for service in (segment_service, pbp_service)
    ]

    assert responses[0] == responses[1]
    assert '"source": "pbpstats"' in responses[0] and "games_missing" not in responses[0]
    assert with_segments.calls == plain.calls == [(DEN, "2025-26", (), (203999,))]
    assert not segment_service.serves_from_segments("2025-26")


def test_injury_scenarios_read_a_covered_season_from_segments_on_both_paths(covered, provider):
    service = _service(provider, covered.engine)
    for fetch in (True, False):
        request = SimpleNamespace(
            fetch_on_off=fetch, user_id="u1", on_off_deadline=service.request_deadline() if fetch else None
        )
        reads = _OnOffReads(service, "BOS", TATUM, request)

        for side in ("on", "off"):
            assert reads.get("2025-26", side)["source"] == "statsplus"
    assert provider.calls == []


def test_the_warm_skips_a_covered_season(covered, provider):
    from scripts.on_off_warm import warm

    class Listing:
        def list_injuries(self, requested_date, team):
            return {"injuries": [{"player_id": TATUM, "team": "BOS", "status": "Out"}]}

    result = warm(Listing(), _service(provider, covered.engine), "2025-26", NOW.date())

    assert result == {"state": "served_from_segments", "requests": 0, "segment_seasons": ["2025-26"]}
    assert provider.calls == []


def test_the_route_serves_a_covered_season(app, dependencies, authenticate, covered, provider):
    dependencies.on_off_service = _service(provider, covered.engine)

    body = (
        app.test_client()
        .get(f"/api/on_off?team=BOS&season=2025-26&on[]={BROWN}", headers=authenticate())
        .get_json()
    )

    assert (body["source"], body["games_missing"][0]["matchup"]) == ("statsplus", "DET @ BOS")
    assert body["players"][0]["player_id"] == BROWN
    assert provider.calls == []


def test_segment_minutes_round_half_up_like_pbp_stats():
    from app.services.on_off import segment_row

    sums = {
        "player_id": 1,
        "name": "P",
        "off_poss": 10,
        **dict.fromkeys(("pts", "reb", "ast", "fg3m", "fga", "fg3a", "tov", "fta"), 0),
        **dict.fromkeys(("team_fga", "team_fta", "team_tov", "team_heaves"), 0),
    }
    minutes = [segment_row({**sums, "duration_cs": cs}).minutes for cs in (8010_00 - 1, 8010_00, 8040_00)]
    assert minutes == [133, 134, 134]
    usage = segment_row(
        {
            **sums,
            "duration_cs": 6000,
            "fga": 10,
            "fta": 5,
            "tov": 2,
            "team_fga": 40,
            "team_heaves": 1,
            "team_fta": 10,
            "team_tov": 6,
        }
    ).usage
    assert usage == pytest.approx(100 * (10 + 0.44 * 5 + 2) / (40 + 1 + 0.44 * 10 + 6))
