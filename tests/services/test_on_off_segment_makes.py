"""Made field goals, made twos and made free throws on segment-served on/off
splits (crf04/statsplus#500): the derivation, the split read that serves them
only when every row carries them, and the backfill that fills stored games."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.services import lineup_segments
from app.services.lineup_segment_repository import LineupSegmentRepository, write_game
from app.services.lineup_segments import COUNTS, GameSegments, Segment, derive_segments
from scripts import lineup_segment_backfill
from scripts.lineup_segment_backfill import fill_makes
from tests.services.test_on_off import FakeProvider
from tests.services.test_on_off_segments import (
    BOS,
    BROWN,
    DEN,
    TATUM,
    WRITTEN_AT,
    FakeLiveData,
    FlakyLiveData,
    _box,
    _collection,
    _dump,
    _engine,
    _game,
    _service,
)


def _forget_makes(engine, game_ids=None):
    """Null the makes, as on a row collected before migration 089."""

    where = "" if game_ids is None else " WHERE game_id IN (" + ", ".join(f"'{g}'" for g in game_ids) + ")"
    with engine.begin() as connection:
        connection.execute(text(f"UPDATE lineup_segment_players SET fgm = NULL, ftm = NULL{where}"))


# -- derivation ---------------------------------------------------------------------


def test_makes_count_made_twos_threes_and_free_throws_including_and_ones_and_technicals():
    game = _game(
        ("2pt", "PT11M00.00S", {"teamId": BOS, "personId": 1, "shotResult": "Made", "possession": BOS}),
        ("foul", "PT11M00.00S", {"teamId": DEN, "personId": 11, "subType": "personal", "possession": BOS}),
        (
            "freethrow",
            "PT11M00.00S",
            {"teamId": BOS, "personId": 1, "subType": "1 of 1", "shotResult": "Made"},
        ),
        ("3pt", "PT10M00.00S", {"teamId": BOS, "personId": 2, "shotResult": "Made", "possession": BOS}),
        ("3pt", "PT09M00.00S", {"teamId": BOS, "personId": 2, "shotResult": "Missed", "possession": BOS}),
        ("foul", "PT08M00.00S", {"teamId": DEN, "personId": 11, "subType": "technical", "possession": BOS}),
        (
            "freethrow",
            "PT08M00.00S",
            {"teamId": BOS, "personId": 3, "subType": "1 of 1", "shotResult": "Made"},
        ),
        ("foul", "PT07M00.00S", {"teamId": DEN, "personId": 12, "subType": "personal", "possession": BOS}),
        (
            "freethrow",
            "PT07M00.00S",
            {"teamId": BOS, "personId": 3, "subType": "1 of 2", "shotResult": "Missed"},
        ),
        (
            "freethrow",
            "PT07M00.00S",
            {"teamId": BOS, "personId": 3, "subType": "2 of 2", "shotResult": "Made"},
        ),
    )

    (segment,) = [s for s in derive_segments(_box(), game, "2025-26").segments if s.team_id == BOS]

    assert {
        player: {key: segment.counts[player][key] for key in ("pts", "fgm", "fg3m", "ftm", "fta")}
        for player in (1, 2, 3)
    } == {
        1: {"pts": 3, "fgm": 1, "fg3m": 0, "ftm": 1, "fta": 1},
        2: {"pts": 3, "fgm": 1, "fg3m": 1, "ftm": 0, "fta": 0},
        3: {"pts": 2, "fgm": 0, "fg3m": 0, "ftm": 2, "fta": 3},
    }


def test_every_row_of_82_recorded_games_satisfies_the_points_identity(bos_games):
    rows = checked = 0
    for game_id, documents in bos_games.items():
        segments = derive_segments(documents["box"], documents["pbp"], "2025-26")
        made = {}
        for action in documents["pbp"]["game"]["actions"]:
            if action.get("shotResult") == "Made" and action["actionType"] in ("2pt", "3pt", "freethrow"):
                key = (int(action["personId"]), action["actionType"] == "freethrow")
                made[key] = made.get(key, 0) + 1
        summed = {}
        for segment in segments.segments:
            for player, counts in segment.counts.items():
                rows += 1
                assert (
                    counts["pts"] == 2 * (counts["fgm"] - counts["fg3m"]) + 3 * counts["fg3m"] + counts["ftm"]
                ), (
                    game_id,
                    segment.seg,
                    player,
                )
                summed[(player, False)] = summed.get((player, False), 0) + counts["fgm"]
                summed[(player, True)] = summed.get((player, True), 0) + counts["ftm"]
        # Each make is counted once, for the player who made it.
        assert {key: value for key, value in summed.items() if value} == made, game_id
        checked += 1
    assert (checked, rows) == (82, 18_145)


# -- reads --------------------------------------------------------------------------


def _write(engine, game_id, lineup, counts):
    with engine.begin() as connection:
        write_game(
            connection,
            "2025-26",
            GameSegments(
                game_id,
                BOS,
                DEN,
                {p: f"P{p}" for p in lineup},
                (
                    Segment(
                        BOS,
                        0,
                        lineup,
                        12 * 6000,
                        {p: {**dict.fromkeys(COUNTS, 0), **counts.get(p, {})} for p in lineup},
                        0,
                        0,
                        0,
                        0,
                        0,
                    ),
                ),
            ),
            WRITTEN_AT,
        )


@pytest.fixture
def mixed(tmp_path):
    """One game re-collected (player 1 shot 3-for-4 with a three and 1-of-2 free
    throws), one collected before migration 089, in another lineup."""

    engine = _engine(tmp_path)
    shooter = {"pts": 8, "fgm": 3, "fg3m": 1, "fga": 4, "fg3a": 2, "ftm": 1, "fta": 2}
    _write(engine, "0022500001", (1, 2, 3, 4, 5), {1: shooter})
    _write(engine, "0022500002", (6, 7, 8, 9, 10), {6: shooter})
    _forget_makes(engine, ["0022500002"])
    LineupSegmentRepository(engine).record_coverage("2025-26", WRITTEN_AT)
    return engine


def test_a_split_whose_segments_all_carry_makes_serves_them_beside_two_point_attempts(mixed):
    service = _service(FakeProvider(), mixed)

    for body in (service.get("BOS", "2025-26", on=[1]), service.get("BOS", "2025-26", off=[6])):
        shooter = next(player for player in body["players"] if player["player_id"] == 1)
        assert shooter["minutes"] == 12
        assert shooter["totals"] == {
            "pts": 8,
            "reb": 0,
            "ast": 0,
            "fg3m": 1,
            "fga": 4,
            "fg3a": 2,
            "tov": 0,
            "fta": 2,
            "fg2a": 2,
            "fgm": 3,
            "fg2m": 2,
            "ftm": 1,
        }
        assert shooter["per36"] == {
            "pts": 24.0,
            "reb": 0.0,
            "ast": 0.0,
            "fg3m": 3.0,
            "fga": 12.0,
            "fg3a": 6.0,
            "tov": 0.0,
            "fta": 6.0,
            "fg2a": 6.0,
            "fgm": 9.0,
            "fg2m": 6.0,
            "ftm": 3.0,
        }


def test_a_split_with_any_segment_lacking_makes_omits_them_for_every_player(mixed):
    service = _service(FakeProvider(), mixed)

    for body in (service.get("BOS", "2025-26"), service.get("BOS", "2025-26", on=[6])):
        assert body["players"], body
        for player in body["players"]:
            for side in ("totals", "per36"):
                assert list(player[side]) == [
                    "pts",
                    "reb",
                    "ast",
                    "fg3m",
                    "fga",
                    "fg3a",
                    "tov",
                    "fta",
                    "fg2a",
                ]
    full = service.get("BOS", "2025-26")
    assert next(p for p in full["players"] if p["player_id"] == 1)["totals"]["fg3m"] == 1


def test_a_lineup_seen_in_a_filled_and_an_unfilled_game_serves_no_makes_rather_than_a_part(mixed):
    shooter = {"pts": 8, "fgm": 3, "fg3m": 1, "fga": 4, "fg3a": 2, "ftm": 1, "fta": 2}
    _write(mixed, "0022500003", (1, 2, 3, 4, 5), {1: shooter})
    _forget_makes(mixed, ["0022500003"])

    body = _service(FakeProvider(), mixed).get("BOS", "2025-26", on=[1])

    shooter_row = next(player for player in body["players"] if player["player_id"] == 1)
    assert (shooter_row["minutes"], shooter_row["totals"]["pts"]) == (24, 16)
    assert list(shooter_row["totals"]) == ["pts", "reb", "ast", "fg3m", "fga", "fg3a", "tov", "fta", "fg2a"]
    assert list(shooter_row["per36"]) == list(shooter_row["totals"])


def test_the_rotation_job_stores_makes_so_brown_has_them_asked_and_for_the_season(tmp_path, bos_games):
    engine = _engine(tmp_path)
    stored = sorted(bos_games)[:2]
    _collection(engine, bos_games).collect("2025-26", game_ids=stored)
    LineupSegmentRepository(engine).record_coverage("2025-26", WRITTEN_AT)
    service = _service(FakeProvider(), engine)

    def brown(body):
        player = next(player for player in body["players"] if player["player_id"] == BROWN)
        keys = ("pts", "fg3m", "fgm", "fg2m", "ftm")
        return (
            player["minutes"],
            {k: player["totals"][k] for k in keys},
            {k: player["per36"][k] for k in keys},
        )

    made = {"fgm": 0, "ftm": 0}
    for game_id in stored:
        for action in bos_games[game_id]["pbp"]["game"]["actions"]:
            if action.get("personId") == BROWN and action.get("shotResult") == "Made":
                made["ftm" if action["actionType"] == "freethrow" else "fgm"] += 1

    assert brown(service.get("BOS", "2025-26", on=[1628401])) == (
        56,
        {"pts": 46, "fg3m": 3, "fgm": 17, "fg2m": 14, "ftm": 9},
        {"pts": 29.57, "fg3m": 1.93, "fgm": 10.93, "fg2m": 9.0, "ftm": 5.79},
    )
    season = brown(service.get("BOS", "2025-26"))
    assert season == (
        74,
        {"pts": 55, "fg3m": 5, "fgm": 20, "fg2m": 15, "ftm": 10},
        {"pts": 26.76, "fg3m": 2.43, "fgm": 9.73, "fg2m": 7.3, "ftm": 4.86},
    )
    assert made == {"fgm": season[1]["fgm"], "ftm": season[1]["ftm"]}


def test_a_pbp_stats_season_keeps_exactly_its_fields(tmp_path):
    body = _service(FakeProvider(), _engine(tmp_path)).get("BOS", "2024-25", off=[TATUM])

    assert (body["source"], len(body["players"])) == ("pbpstats", 4)
    for player in body["players"]:
        for side in ("totals", "per36"):
            assert list(player[side]) == ["pts", "reb", "ast", "fg3m", "fga", "fg3a", "tov", "fta"]


# -- filling stored games -------------------------------------------------------------


def _fill(engine, games, **kwargs):
    return fill_makes(
        FakeLiveData(games), LineupSegmentRepository(engine), "2025-26", throttle_seconds=0, **kwargs
    )


def test_the_fill_is_bounded_resumable_and_ends_with_the_rows_a_fresh_collection_writes(tmp_path, bos_games):
    engine, game_ids, fresh = _stored(tmp_path, bos_games)
    live = FakeLiveData(bos_games)
    segments = LineupSegmentRepository(engine)

    first = fill_makes(live, segments, "2025-26", max_games=2, throttle_seconds=0)
    second = fill_makes(live, segments, "2025-26", max_games=2, throttle_seconds=0)
    third = fill_makes(live, segments, "2025-26", throttle_seconds=0)

    assert [(r["attempted"], r["filled"], r["remaining"]) for r in (first, second, third)] == [
        (2, 2, 1),
        (1, 1, 0),
        (0, 0, 0),
    ]
    assert live.fetched == game_ids
    assert _dump(engine) == fresh


@pytest.mark.parametrize(
    "column",
    ["pts", "fga", "fg3m", "fta", "reb", "ast", "fg3a", "tov", "duration_cs", "team_fga", "off_poss"],
)
def test_a_game_whose_stored_counts_changed_is_reported_and_left_as_it_was(tmp_path, bos_games, column):
    engine, game_ids, fresh = _stored(tmp_path, bos_games)
    with engine.begin() as connection:
        # A scoring correction since the game was stored, say: one stored value is one higher.
        connection.execute(
            text(
                f"UPDATE lineup_segment_players SET {column} = {column} + 1"
                f" WHERE game_id = '{game_ids[1]}' AND player_id = {BROWN} AND seg = 0"
            )
        )
    before = [row for row in _dump(engine)["lineup_segment_players"] if row[0] == game_ids[1]]

    result = _fill(engine, bos_games)

    assert (result["attempted"], result["filled"], result["remaining"]) == (3, 2, 1)
    assert result["mismatched"] == [
        {
            "game_id": game_ids[1],
            "reason": f"stored counts differ for team {BOS} segment 0 player {BROWN}: {column}",
        }
    ]
    assert [row for row in _dump(engine)["lineup_segment_players"] if row[0] == game_ids[1]] == before
    assert [row for row in _dump(engine)["lineup_segment_players"] if row[0] != game_ids[1]] == [
        row for row in fresh["lineup_segment_players"] if row[0] != game_ids[1]
    ]


@pytest.mark.parametrize("change", ["extra", "missing"])
def test_a_game_whose_stored_rows_differ_from_the_derivation_is_reported_and_left(
    tmp_path, bos_games, change
):
    engine, game_ids, _fresh = _stored(tmp_path, bos_games)
    game = f"game_id = '{game_ids[0]}'"
    with engine.begin() as connection:
        stored = connection.execute(
            text(f"SELECT COUNT(*) FROM lineup_segment_players WHERE {game}")
        ).scalar()
        if change == "extra":
            connection.execute(
                text(
                    "INSERT INTO lineup_segment_players SELECT game_id, team_id, seg, 1, season, written_at,"
                    " lineup_mask, duration_cs, pts, reb, ast, fg3m, fga, fg3a, tov, fta, team_fga, team_fta,"
                    " team_tov, team_heaves, off_poss, fgm, ftm FROM lineup_segment_players"
                    f" WHERE {game} AND player_id = {BROWN} AND seg = 0"
                )
            )
        else:
            connection.execute(
                text(f"DELETE FROM lineup_segment_players WHERE {game} AND player_id = {BROWN} AND seg = 0")
            )
    before = _dump(engine)
    now = stored + 1 if change == "extra" else stored - 1

    result = _fill(engine, bos_games, max_games=1)

    assert result["mismatched"] == [
        {
            "game_id": game_ids[0],
            "reason": f"stored segments differ: {now} player rows stored, {stored} derived",
        }
    ]
    assert _dump(engine) == before


def test_a_fill_that_fails_partway_through_a_game_writes_none_of_its_rows(tmp_path, bos_games):
    engine, game_ids, _fresh = _stored(tmp_path, bos_games)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER fail_after_five BEFORE UPDATE OF fgm ON lineup_segment_players"
                " WHEN (SELECT COUNT(*) FROM lineup_segment_players"
                " WHERE game_id = NEW.game_id AND fgm IS NOT NULL) >= 5"
                " BEGIN SELECT RAISE(ABORT, 'disk full'); END"
            )
        )
    documents = bos_games[game_ids[0]]

    with pytest.raises(IntegrityError, match="disk full"):
        LineupSegmentRepository(engine).fill_makes(
            derive_segments(documents["box"], documents["pbp"], "2025-26")
        )

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(fgm), COUNT(ftm) FROM lineup_segment_players")
        ).one() == (0, 0)


@pytest.mark.parametrize(
    ("failed", "mismatched", "code"),
    [
        ([], [], 0),
        ([{"game_id": "0022500001", "error": "timeout"}], [], 1),
        ([], [{"game_id": "0022500001"}], 1),
    ],
)
def test_the_makes_run_exits_1_when_a_game_failed_or_was_mismatched(
    tmp_path, monkeypatch, capsys, failed, mismatched, code
):
    result = {"season": "2025-26", "filled": 3, "failed": failed, "mismatched": mismatched, "remaining": 0}
    monkeypatch.setattr(lineup_segment_backfill, "fill_makes", lambda *args, **kwargs: result)

    exit_code = lineup_segment_backfill.main(
        ["2025-26", "--makes", "--database-url", f"sqlite:///{tmp_path / 'run.db'}"]
    )

    assert (exit_code, json.loads(capsys.readouterr().out.splitlines()[-1])) == (code, result)


def test_a_game_whose_new_counts_break_the_points_identity_is_reported_and_left(
    tmp_path, bos_games, monkeypatch
):
    engine, game_ids, _fresh = _stored(tmp_path, bos_games)
    real = lineup_segments.derive_segments

    def one_make_short(box, pbp, season):
        derived = real(box, pbp, season)
        if derived.game_id != game_ids[0]:
            return derived
        segment = next(s for s in derived.segments if any(c["fgm"] for c in s.counts.values()))
        player = next(p for p, c in segment.counts.items() if c["fgm"])
        counts = {
            **segment.counts,
            player: {**segment.counts[player], "fgm": segment.counts[player]["fgm"] - 1},
        }
        broken = Segment(**{**{f: getattr(segment, f) for f in Segment.__slots__}, "counts": counts})
        return GameSegments(
            derived.game_id,
            derived.home_team_id,
            derived.away_team_id,
            derived.names,
            tuple(broken if s is segment else s for s in derived.segments),
        )

    monkeypatch.setattr(lineup_segments, "derive_segments", one_make_short)
    before = _dump(engine)

    result = _fill(engine, bos_games, max_games=1)

    assert (result["attempted"], result["filled"], result["remaining"]) == (1, 0, 3)
    assert len(result["mismatched"]) == 1
    assert result["mismatched"][0]["reason"].startswith("points identity fails for team ")
    assert _dump(engine) == before


def test_a_game_that_could_not_be_fetched_stays_pending_for_the_next_run(tmp_path, bos_games):
    engine, game_ids, fresh = _stored(tmp_path, bos_games)
    segments = LineupSegmentRepository(engine)

    first = fill_makes(FlakyLiveData(bos_games, game_ids[0]), segments, "2025-26", throttle_seconds=0)
    second = fill_makes(FakeLiveData(bos_games), segments, "2025-26", throttle_seconds=0)

    assert (first["filled"], first["remaining"], [f["game_id"] for f in first["failed"]]) == (
        2,
        1,
        [game_ids[0]],
    )
    assert (second["attempted"], second["filled"], second["remaining"]) == (1, 1, 0)
    assert _dump(engine) == fresh


def test_the_fill_pauses_between_games_and_leaves_other_seasons_alone(tmp_path, bos_games):
    engine, game_ids, _fresh = _stored(tmp_path, bos_games)
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE lineup_segment_players SET season = '2026-27' WHERE game_id = '{game_ids[2]}'")
        )
    pauses = []

    result = fill_makes(
        FakeLiveData(bos_games),
        LineupSegmentRepository(engine),
        "2025-26",
        throttle_seconds=2,
        sleep=pauses.append,
    )

    assert (result["pending"], result["filled"], pauses) == (2, 2, [2])


def _stored(tmp_path, bos_games):
    """Three BOS games stored before migration 089, and the rows a fresh collection writes."""

    (tmp_path / "fresh").mkdir()
    game_ids = sorted(bos_games)[:3]
    fresh = _engine(tmp_path / "fresh")
    _collection(fresh, bos_games).collect("2025-26", game_ids=game_ids)
    engine = _engine(tmp_path)
    _collection(engine, bos_games).collect("2025-26", game_ids=game_ids)
    _forget_makes(engine)
    return engine, game_ids, _dump(fresh)


@pytest.fixture(scope="module")
def bos_games():
    from scripts.on_off_parity import load_games

    return load_games("bos_2025_26")
