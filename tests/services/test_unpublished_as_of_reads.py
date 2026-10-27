"""A past 2026-27 game reads 2026-27 evidence as of that game (crf04/statsplus-backend#626).

2025-26 is published and 2026-27 held.  The opening week's game logs and team
defense were composed on 2026-10-21; once every team had played ten games a
newer composition (cut 2026-10-24 04:00 UTC, the end of the 2026-10-23 slate
day) superseded each.  A 2026-27 Matchup of a game that tipped on 2026-10-23
must not read the newer ones, which count games played after it, and reads the
opening week's instead; a game before any 2026-27 composition trails on
2025-26 where its family trails.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.config.settings import CacheSettings
from app.models.player_game_log import PlayerGameLogSync
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.matchup_reuse import ComposedMatchupCache
from app.services.matchup_snapshot import capture_matchup_snapshot, request_matchups
from app.services.research_season import season_scoped
from tests.services.test_serving_season_matchup import (
    ACTOR,
    GAME_ID,
    TRAILING_TEAM_DEFENSE,
    _defense_payload,
    _game_event,
    _log_payload,
)
from tests.support.season_cutover import (
    NEW,
    NEW_CUTOFF,
    NEW_GAMES,
    NOW,
    OLD,
    OLD_CUTOFF,
    OLD_PROVENANCE,
    PLAYER_LOGS,
    SEASON_STREAM,
    THIN_CUTOFF,
    THIN_GAMES,
    THIN_PROVENANCE,
    TRICODES,
    _authority,
    accept_ledger,
    payload_for,
    provenance,
)
from tests.support.selected_season import PLAYER_ID, SelectedWorld, _StoredInjuries, settings
from tests.test_game_log_preview import FakeRedis

#: A final 2026-27 game on the 2026-10-23 slate, after the opening week's composition.
AFTER_THE_OPENING_WEEK = "2026-10-23T23:30:00+00:00"
#: A final 2026-27 game on the 2026-10-19 slate, before any 2026-27 composition.
BEFORE_ANY_COMPOSITION = "2026-10-19T23:30:00+00:00"
#: A game later on the reader's own slate day (NOW is 2026-10-25 12:00 UTC).
TODAY = "2026-10-25T23:30:00+00:00"
#: A final 2026-27 game on the 2026-10-24 slate, and a composition cut fifteen
#: minutes after it tipped, before that slate day ended.
TIPPED = "2026-10-24T23:30:00+00:00"
AFTER_THE_TIP = datetime(2026, 10, 24, 23, 45, tzinfo=UTC)
BOTH = {**provenance(THIN_GAMES, "thin"), **provenance(NEW_GAMES[len(THIN_GAMES) :])}


def season_in_progress(monkeypatch):
    """2026-27 has games to play: the fixture's Event Catalogs list only final ones."""

    complete = ActiveManifestLedgerGovernanceReader.resolve_season_is_complete
    monkeypatch.setattr(
        ActiveManifestLedgerGovernanceReader,
        "resolve_season_is_complete",
        lambda self, season, *args, **kwargs: season != NEW and complete(self, season, *args, **kwargs),
    )


@pytest.fixture
def world(tmp_path, monkeypatch):
    season_in_progress(monkeypatch)
    built = SelectedWorld(tmp_path)
    yield built
    built.engine.dispose()


def _compose(world, stream_key, payload, *, cutoff):
    return world.publications.recompose_ledger(
        stream_key,
        season=NEW,
        cutoff=cutoff,
        payload=payload,
        provenance=BOTH,
        reason="2026-27 ledger composition",
    )


def _ten_game_defense():
    """Thirty governed rows: BOS allows 67 steals a game, every other team 7."""

    rows = _defense_payload(NEW, NEW_GAMES)
    for row in rows:
        row["team_tricode"] = TRICODES[row["team_id"]]
    return rows


def _ten_game_logs():
    """The ten-game logs: a stat correction gives Player 0 20 points, 5 steals and 1 block."""

    payload = _log_payload(NEW, NEW_GAMES)
    for row in payload["rows"]:
        if row["player_id"] == PLAYER_ID:
            row.update(points=20, steals=5, blocks=1)
    return payload


def _hold_a_week_then_ten_games(world, *, cutoff=NEW_CUTOFF):
    """``(opening_week, ten_games)`` team defense; the newer supersedes the older.

    Game logs move the same way: ``world.opening_week_logs`` is the older,
    ``world.ten_games_logs`` the newer.  Player 0 scores 12 points with 3
    steals and 1 block in the opening week's logs, 20 with 5 and 1 in the
    newer.  The ten-game defense has BOS allow 67 steals a game; every
    opening-week team allows 7.  The opening week's games
    keep their ledger source, so the older compositions still prove the
    ledger's accepted evidence.
    """

    world.opening_week_logs, opening_week = world.hold_opening_week()
    accept_ledger(world.engine, NEW_GAMES[len(THIN_GAMES) :], "pbp")
    world.ten_games_logs = _compose(world, PLAYER_LOGS, _ten_game_logs(), cutoff=cutoff)
    ten_games = _compose(world, SEASON_STREAM, _ten_game_defense(), cutoff=cutoff)
    return opening_week, ten_games


def _status(world, publication_id):
    with world.engine.connect() as connection:
        return connection.scalar(
            text("SELECT status FROM publication_versions WHERE publication_id = :p"), {"p": publication_id}
        )


def _game_at(world, scheduled_at, game_id=GAME_ID):
    event = {
        **_game_event(),
        "nba_game_id": game_id,
        "scheduled_at": scheduled_at,
        "status_text": "Final",
        "status_code": 3,
    }
    world.service.event_catalog = world.selections.event_catalog = SimpleNamespace(
        get_event=lambda season, requested: event if (season, requested) == (NEW, game_id) else None,
        get_freshness=lambda season, *, now: {"last_success_at": NOW.isoformat(), "fresh": True},
    )
    return event


def _matchup(world):
    return season_scoped(world.service, NEW).get_matchup(game_id=GAME_ID)


def _served(matchup, *stream_keys):
    """``{stream_key: (status, season, publication_id, coverage_cutoff)}`` the provenance lists."""

    return {
        entry["stream_key"]: (
            entry["status"],
            entry["season"],
            entry["publication_id"],
            entry["coverage_cutoff"],
        )
        for entry in matchup["provenance"]["generation"]
        if entry["stream_key"] in stream_keys
    }


def _bos_steals(matchup):
    """BOS's Season OPP_STL cell and the league mean, as the Matchup serves them."""

    [bos] = [team for team in matchup["teams"] if team["tricode"] == "BOS"]
    [column] = [row for row in bos["defense_sheet"]["traditional"] if row["key"] == "OPP_STL"]
    [league] = [row for row in matchup["league"]["defense_sheet"]["traditional"] if row["key"] == "OPP_STL"]
    return (
        column["season"]["allowed_per_48"],
        column["season"]["rank"],
        column["season"]["percent_vs_league_average"],
        league["season"]["average_allowed_per_48"],
    )


def _season_defense(matchup):
    section = matchup["experience"]["sections"]["season_defense"]
    return section["status"], section["season"]


def test_a_past_game_reads_the_newest_2026_27_evidence_composed_before_it(world):
    opening_week, _ten_games = _hold_a_week_then_ten_games(world)
    _game_at(world, AFTER_THE_OPENING_WEEK)

    matchup = _matchup(world)

    assert _status(world, opening_week.publication_id) == "superseded"
    assert _season_defense(matchup) == ("available", NEW)
    assert _served(matchup, SEASON_STREAM, PLAYER_LOGS) == {
        SEASON_STREAM: ("candidate", NEW, opening_week.publication_id, THIN_CUTOFF.isoformat()),
        PLAYER_LOGS: ("candidate", NEW, world.opening_week_logs.publication_id, THIN_CUTOFF.isoformat()),
    }
    # The opening week's defense (every team allows 7 steals) and logs.
    assert _bos_steals(matchup) == (7.0, 1, 0.0, 7.0)
    assert matchup["players"][0]["season_scoring"] == 12.0
    # Diets and Last 15 have no 2026-27 composition: they trail on 2025-26.
    assert [entry["family"] for entry in matchup["trailing"]] == ["player_diets", "last_15"]
    assert matchup["coverage"]["coverage_cutoffs"] == [OLD_CUTOFF.isoformat(), THIN_CUTOFF.isoformat()]


def test_a_past_game_never_reads_a_composition_cut_after_it_tipped_that_day(world):
    with world.engine.begin() as connection:
        _authority(
            connection,
            season=NEW,
            manifest_id="manifest-late",
            cutoff=AFTER_THE_TIP,
            games=(),
            catalog_games=NEW_GAMES,
        )
    opening_week, _after_the_tip = _hold_a_week_then_ten_games(world, cutoff=AFTER_THE_TIP)
    _game_at(world, TIPPED)

    matchup = _matchup(world)

    assert _served(matchup, SEASON_STREAM, PLAYER_LOGS) == {
        SEASON_STREAM: ("candidate", NEW, opening_week.publication_id, THIN_CUTOFF.isoformat()),
        PLAYER_LOGS: ("candidate", NEW, world.opening_week_logs.publication_id, THIN_CUTOFF.isoformat()),
    }
    assert _bos_steals(matchup) == (7.0, 1, 0.0, 7.0)


def test_todays_game_still_reads_the_newest_2026_27_evidence(world):
    _opening_week, ten_games = _hold_a_week_then_ten_games(world)
    _game_at(world, TODAY)

    matchup = _matchup(world)

    assert _served(matchup, SEASON_STREAM, PLAYER_LOGS) == {
        SEASON_STREAM: ("candidate", NEW, ten_games.publication_id, NEW_CUTOFF.isoformat()),
        PLAYER_LOGS: ("candidate", NEW, world.ten_games_logs.publication_id, NEW_CUTOFF.isoformat()),
    }
    # The ten-game defense (BOS allows 67 steals against a league mean of 9) and logs.
    assert _bos_steals(matchup) == (67.0, 30, 644.444444, 9.0)
    assert matchup["players"][0]["season_scoring"] == 20.0


def _selected_steals_and_blocks(world):
    """Player 0's STKS in each head-to-head game the selection lists."""

    selection = season_scoped(world.selections, NEW).get_selection(game_id=GAME_ID, player_id=PLAYER_ID)
    return [row["stats"]["STKS"] for row in selection["h2h"]["rows"] if row["row_type"] == "game"]


def test_a_past_games_selection_reads_the_game_logs_composed_before_it(world):
    _hold_a_week_then_ten_games(world)
    _game_at(world, AFTER_THE_OPENING_WEEK)

    assert _selected_steals_and_blocks(world) == [4.0]

    _game_at(world, TODAY)
    assert _selected_steals_and_blocks(world) == [6.0]


def _corrupt(world, publication):
    with world.engine.begin() as connection:
        connection.execute(
            text("UPDATE publication_versions SET checksum = :c WHERE publication_id = :p"),
            {"c": "0" * 64, "p": publication.publication_id},
        )


def test_a_past_game_skips_a_refused_version_for_the_next_older_one(world):
    opening_week, ten_games = _hold_a_week_then_ten_games(world)
    _corrupt(world, ten_games)
    # Tipped after the ten-game composition was cut.
    _game_at(world, TIPPED)

    assert _served(_matchup(world), SEASON_STREAM)[SEASON_STREAM][2] == opening_week.publication_id


def test_todays_game_never_falls_back_to_an_older_version_when_the_newest_is_refused(world):
    _opening_week, ten_games = _hold_a_week_then_ten_games(world)
    _corrupt(world, ten_games)
    _game_at(world, TODAY)

    matchup = _matchup(world)

    assert _served(matchup, SEASON_STREAM) == {
        SEASON_STREAM: ("prior_season", OLD, world.live(SEASON_STREAM), OLD_CUTOFF.isoformat())
    }


def test_a_game_before_every_2026_27_composition_trails_on_2025_26(world):
    _hold_a_week_then_ten_games(world)
    _game_at(world, BEFORE_ANY_COMPOSITION)

    matchup = _matchup(world)

    assert _season_defense(matchup) == ("available", OLD)
    assert _served(matchup, SEASON_STREAM) == {
        SEASON_STREAM: ("prior_season", OLD, world.live(SEASON_STREAM), OLD_CUTOFF.isoformat())
    }
    # Every family that trails names 2025-26 and its sample, team defense included.
    assert [entry["family"] for entry in matchup["trailing"]] == ["player_diets", "team_defense", "last_15"]
    assert matchup["trailing"][1] == {**TRAILING_TEAM_DEFENSE, "team_games": {"min": 1, "max": 1}}
    # Game logs never trail: with no 2026-27 composition before the game they
    # read as after a cutover with none, and serve nothing.
    [logs] = [entry for entry in matchup["provenance"]["generation"] if entry["stream_key"] == PLAYER_LOGS]
    assert (logs["status"], logs["unavailable_reason"], logs["freshness"]) == (
        "missing",
        "window_not_ready",
        "unavailable",
    )
    assert matchup["players"][0]["season_scoring"] is None
    assert matchup["coverage"]["coverage_cutoffs"] == [OLD_CUTOFF.isoformat()]


def test_a_cached_past_game_matchup_reports_what_it_served(world):
    opening_week, _ten_games = _hold_a_week_then_ten_games(world)
    game = _game_at(world, AFTER_THE_OPENING_WEEK)
    cached = settings().model_copy(update={"cache": CacheSettings(enabled=True, matchup_enabled=True)})
    cache = ComposedMatchupCache(FakeRedis())
    reader = world.reader.for_season(NEW)
    composer = season_scoped(world.service, NEW)
    composed = []
    compose = composer.get_matchup_from_snapshot

    def counted(**kwargs):
        composed.append(kwargs["game_id"])
        return compose(**kwargs)

    composer.get_matchup_from_snapshot = counted

    def read():
        matchups = request_matchups(
            composer=composer,
            publication_reader=reader,
            injuries=_StoredInjuries(),
            settings=cached,
            cache=cache,
        )
        return matchups.get_matchup(game_id=GAME_ID, game={**game, "game_id": GAME_ID})

    miss, hit = read(), read()

    assert composed == [GAME_ID]

    for matchup in (miss, hit):
        assert _served(matchup, SEASON_STREAM, PLAYER_LOGS) == {
            SEASON_STREAM: ("candidate", NEW, opening_week.publication_id, THIN_CUTOFF.isoformat()),
            PLAYER_LOGS: ("candidate", NEW, world.opening_week_logs.publication_id, THIN_CUTOFF.isoformat()),
        }
        assert [entry["family"] for entry in matchup["trailing"]] == ["player_diets", "last_15"]
        assert _bos_steals(matchup) == (7.0, 1, 0.0, 7.0)
    # The capture the cache shares still holds the newest candidates.
    _season, captured = capture_matchup_snapshot(cached, reader)
    assert captured.read(SEASON_STREAM).cutoff == NEW_CUTOFF.isoformat()


def test_a_cached_past_game_matchup_recomposes_when_what_it_read_goes_stale(world):
    opening_week, _ten_games = _hold_a_week_then_ten_games(world)
    # Every other version was written ninety minutes later, so two hours on
    # only the opening week's reads, the ones this game reads, are stale.
    with world.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE publication_versions SET created_at = :later WHERE publication_id NOT IN (:logs, :defense)"
            ),
            {
                "later": NOW + timedelta(minutes=90),
                "logs": world.opening_week_logs.publication_id,
                "defense": opening_week.publication_id,
            },
        )
    game = _game_at(world, AFTER_THE_OPENING_WEEK)
    cached = settings().model_copy(update={"cache": CacheSettings(enabled=True, matchup_enabled=True)})
    cache = ComposedMatchupCache(FakeRedis())

    def read():
        matchups = request_matchups(
            composer=season_scoped(world.service, NEW),
            publication_reader=world.reader.for_season(NEW),
            injuries=_StoredInjuries(),
            settings=cached,
            cache=cache,
        )
        return matchups.get_matchup(game_id=GAME_ID, game={**game, "game_id": GAME_ID})

    assert read()["freshness"]["player_game_logs"]["status"] == "fresh"
    world.reader.clock = lambda: NOW + timedelta(hours=2)
    assert read()["freshness"]["player_game_logs"]["status"] == "stale"


def test_a_past_game_with_no_player_pool_lists_who_played_from_the_newest_game_logs(world):
    _hold_a_week_then_ten_games(world)
    # Round two's first game: only the ten-game logs hold its box score.
    played = NEW_GAMES[len(THIN_GAMES)][0]
    game = {**_game_at(world, AFTER_THE_OPENING_WEEK, played), "game_id": played}
    with world.engine.begin() as connection:
        connection.execute(
            PlayerGameLogSync.__table__.insert().values(
                season=NEW,
                game_id=played,
                season_type="Regular Season",
                status="complete",
                checksum="0" * 64,
                row_count=2,
                source_provider="nba_live_data",
                retrieved_at=NEW_CUTOFF,
            )
        )
    world.service.player_pool = SimpleNamespace(get_pool_for_game=lambda **_kwargs: None)
    composer = season_scoped(world.service, NEW)
    cached = settings().model_copy(update={"cache": CacheSettings(enabled=True, matchup_enabled=True)})
    cache = ComposedMatchupCache(FakeRedis())

    def read():
        matchups = request_matchups(
            composer=composer,
            publication_reader=world.reader.for_season(NEW),
            injuries=_StoredInjuries(),
            settings=cached,
            cache=cache,
        )
        return matchups.get_matchup(game_id=played, game=game)

    for matchup in (composer.get_matchup(game_id=played), read(), read()):
        assert matchup["experience"]["mode"] == "historical"
        assert [player["name"] for player in matchup["players"]] == ["Player 30", "Player 31"]
        # The evidence is the opening week's; the box score is the newest logs'.
        assert sorted(
            (entry["publication_id"], entry["coverage_cutoff"])
            for entry in matchup["provenance"]["generation"]
            if entry["stream_key"] == PLAYER_LOGS
        ) == sorted(
            [
                (world.opening_week_logs.publication_id, THIN_CUTOFF.isoformat()),
                (world.ten_games_logs.publication_id, NEW_CUTOFF.isoformat()),
            ]
        )


def _cut_over_and_revert_the_opening_week(world):
    """Cut over to the opening week's 2026-27 and revert: its defense is withdrawn."""

    _logs, opening_week = world.hold_opening_week()
    cutover = world.operations.cutover_season(NEW, actor=ACTOR, reason="open 2026-27")
    world.operations.revert_season_cutover(
        NEW, cutover.job_id, actor=ACTOR, reason="2026-27 opened too early"
    )
    # Only the game logs compose again, from the same week.
    logs = world.publications.recompose_ledger(
        PLAYER_LOGS,
        season=NEW,
        cutoff=THIN_CUTOFF,
        payload=payload_for(PLAYER_LOGS, NEW, games=THIN_GAMES, points=11),
        provenance=THIN_PROVENANCE,
        reason="2026-27 ledger composition",
    )
    return opening_week, logs


def test_a_past_game_never_reads_a_defense_a_reverted_cutover_withdrew(world):
    withdrawn, logs = _cut_over_and_revert_the_opening_week(world)
    _game_at(world, AFTER_THE_OPENING_WEEK)

    matchup = _matchup(world)

    assert _status(world, withdrawn.publication_id) == "superseded"
    assert _season_defense(matchup) == ("available", OLD)
    assert _served(matchup, SEASON_STREAM, PLAYER_LOGS) == {
        SEASON_STREAM: ("prior_season", OLD, world.live(SEASON_STREAM), OLD_CUTOFF.isoformat()),
        PLAYER_LOGS: ("candidate", NEW, logs.publication_id, THIN_CUTOFF.isoformat()),
    }


def test_prune_deletes_a_defense_a_reverted_cutover_withdrew(world):
    withdrawn, logs = _cut_over_and_revert_the_opening_week(world)

    world.publications.prune_history(season=NEW)
    world.publications.prune_history(season=NEW)

    assert _status(world, withdrawn.publication_id) is None
    assert _status(world, logs.publication_id) == "candidate"


def test_prune_keeps_the_superseded_2026_27_versions_a_past_game_reads(world):
    opening_week, _ten_games = _hold_a_week_then_ten_games(world)

    world.publications.prune_history(season=NEW)
    world.publications.prune_history(season=NEW)

    assert _status(world, opening_week.publication_id) == "superseded"
    assert _status(world, world.opening_week_logs.publication_id) == "superseded"
    _game_at(world, AFTER_THE_OPENING_WEEK)
    assert _served(_matchup(world), SEASON_STREAM, PLAYER_LOGS) == {
        SEASON_STREAM: ("candidate", NEW, opening_week.publication_id, THIN_CUTOFF.isoformat()),
        PLAYER_LOGS: ("candidate", NEW, world.opening_week_logs.publication_id, THIN_CUTOFF.isoformat()),
    }


def test_prune_still_deletes_a_superseded_defense_of_the_published_season(world):
    # The world composed 2025-26 team defense twice; a third leaves the first
    # neither active nor the rollback target.
    with world.engine.connect() as connection:
        [first] = connection.execute(
            text(
                "SELECT publication_id FROM publication_versions WHERE stream_key = :k AND status = 'superseded'"
            ),
            {"k": SEASON_STREAM},
        ).scalars()
    world.publications.recompose_ledger(
        SEASON_STREAM,
        season=OLD,
        cutoff=OLD_CUTOFF,
        payload=payload_for(SEASON_STREAM, OLD, points=11),
        provenance=OLD_PROVENANCE,
        reason="2025-26 ledger recomposition",
    )

    world.publications.prune_history(stream_key=SEASON_STREAM)

    assert _status(world, first) is None
