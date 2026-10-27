"""A past 2026-27 game reads 2026-27 defense as of that game (crf04/statsplus-backend#626).

2025-26 is published and 2026-27 held.  The opening week's team defense was
composed on 2026-10-21; once every team had played ten games a newer
composition (cut 2026-10-24 04:00 UTC, the end of the 2026-10-23 slate day)
superseded it.  A 2026-27 Matchup of a game on 2026-10-23 must not read the
newer one, which counts games played after it, and reads the opening week's
instead; a game before any 2026-27 composition trails on 2025-26.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.services.database_first_activation import ReadSeason
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.research_season import season_scoped
from tests.services.test_serving_season_matchup import GAME_ID, _game_event, _log_payload
from tests.support.season_cutover import (
    NEW,
    NEW_CUTOFF,
    NEW_GAMES,
    NOW,
    OLD,
    OLD_CUTOFF,
    PLAYER_LOGS,
    SEASON_STREAM,
    THIN_CUTOFF,
    THIN_GAMES,
    accept_ledger,
    provenance,
)
from tests.support.selected_season import SelectedWorld

#: A final 2026-27 game on the 2026-10-23 slate, after the opening week's composition.
AFTER_THE_OPENING_WEEK = "2026-10-23T23:30:00+00:00"
#: A final 2026-27 game on the 2026-10-19 slate, before any 2026-27 composition.
BEFORE_ANY_COMPOSITION = "2026-10-19T23:30:00+00:00"


@pytest.fixture
def world(tmp_path, monkeypatch):
    # This fixture's 2026-27 Event Catalogs list only final games; the real
    # season has most of its games still to play.
    complete = ActiveManifestLedgerGovernanceReader.resolve_season_is_complete
    monkeypatch.setattr(
        ActiveManifestLedgerGovernanceReader,
        "resolve_season_is_complete",
        lambda self, season, *args, **kwargs: season != NEW and complete(self, season, *args, **kwargs),
    )
    built = SelectedWorld(tmp_path)
    yield built
    built.engine.dispose()


def _hold_a_week_then_ten_games(world):
    """``(opening_week, ten_games)`` team defense; the newer supersedes the older.

    The opening week's games keep their ledger source, so the older
    composition still proves the ledger's accepted evidence.
    """

    _logs, opening_week = world.hold_opening_week()
    accept_ledger(world.engine, NEW_GAMES[len(THIN_GAMES) :], "pbp")
    both = {**provenance(THIN_GAMES, "thin"), **provenance(NEW_GAMES[len(THIN_GAMES) :])}
    world.publications.recompose_ledger(
        PLAYER_LOGS,
        season=NEW,
        cutoff=NEW_CUTOFF,
        payload=_log_payload(NEW, NEW_GAMES),
        provenance=both,
        reason="2026-27 ledger composition",
    )
    ten_games = world.compose_new_defense(NEW_GAMES, cutoff=NEW_CUTOFF, provenance=both)
    return opening_week, ten_games


def _status(world, publication_id):
    with world.engine.connect() as connection:
        return connection.scalar(
            text("SELECT status FROM publication_versions WHERE publication_id = :p"), {"p": publication_id}
        )


def _final_game_at(world, scheduled_at):
    event = {**_game_event(), "scheduled_at": scheduled_at, "status_text": "Final", "status_code": 3}
    world.service.event_catalog = SimpleNamespace(
        get_event=lambda season, game_id: event if (season, game_id) == (NEW, GAME_ID) else None,
        get_freshness=lambda season, *, now: {"last_success_at": NOW.isoformat(), "fresh": True},
    )


def _season_defense(world):
    body = season_scoped(world.service, NEW).get_matchup(game_id=GAME_ID)
    section = body["experience"]["sections"]["season_defense"]
    return section["status"], section["season"]


def _traditional_lineage(world, slate_date):
    """``(publication_id, cutoff, season)`` the Season traditional surface of ``slate_date`` reads."""

    reader = world.reader.for_season(NEW)
    snapshot = reader.snapshot((SEASON_STREAM, PLAYER_LOGS), season=ReadSeason(OLD))
    window = world.service.team_matchups.get_latest_window(
        NEW, as_of=slate_date, publication_snapshot=snapshot, bases=("traditional",)
    )
    [observation] = [item for item in window.observations if item.surface == "traditional"]
    return observation.publication.publication_id, observation.publication.cutoff, window.scope.season


def test_a_past_game_reads_the_newest_2026_27_defense_composed_before_it(world):
    opening_week, ten_games = _hold_a_week_then_ten_games(world)
    _final_game_at(world, AFTER_THE_OPENING_WEEK)

    assert _status(world, opening_week.publication_id) == "superseded"
    assert _season_defense(world) == ("available", NEW)
    assert _traditional_lineage(world, date(2026, 10, 23)) == (
        opening_week.publication_id,
        THIN_CUTOFF.isoformat(),
        NEW,
    )
    # The ten-game composition is cut at the end of that slate day: never served for it.
    assert ten_games.publication_id != opening_week.publication_id


def test_todays_game_still_reads_the_newest_2026_27_defense(world):
    _opening_week, ten_games = _hold_a_week_then_ten_games(world)

    assert _traditional_lineage(world, date(2026, 10, 25))[:2] == (ten_games.publication_id, NEW_CUTOFF.isoformat())


def test_a_game_before_every_2026_27_composition_trails_on_2025_26(world):
    _hold_a_week_then_ten_games(world)
    _final_game_at(world, BEFORE_ANY_COMPOSITION)

    assert _season_defense(world) == ("available", OLD)
    assert _traditional_lineage(world, date(2026, 10, 19)) == (
        world.live(SEASON_STREAM),
        OLD_CUTOFF.isoformat(),
        OLD,
    )


def test_prune_keeps_the_superseded_2026_27_defense_a_past_game_reads(world):
    opening_week, _ten_games = _hold_a_week_then_ten_games(world)
    _final_game_at(world, AFTER_THE_OPENING_WEEK)

    world.publications.prune_history(season=NEW)

    assert _status(world, opening_week.publication_id) == "superseded"
    assert _traditional_lineage(world, date(2026, 10, 23))[0] == opening_week.publication_id
