"""PBPStats ``OffPoss`` in the Canonical Game Ledger (crf04/statsplus#433).

Raw player rows become ``offensive_possessions`` on the player facts, a team's
is their sum over five, the typed checksum ignores both so the backfill
invalidates no lineage, and the backfill re-derives stored games from their own
archive.  Every expected value is a literal.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select, update

from app.migrations import run_migrations
from app.models.canonical_game_ledger import (
    CanonicalGameLedgerPlayerFact,
    CanonicalGameLedgerTeamFact,
)
from app.services.canonical_game_ledger import (
    CanonicalGameLedgerRepository,
    LedgerValidationError,
    canonical_game_from_pbp,
    validate_complete_game,
)
from tests.services.test_canonical_game_ledger import _event
from tests.services.test_rebound_types_ledger import (
    _as_livedata_fallback,
    _player,
    _team_row,
    _wire,
)

LAL = 1610612747
SAS = 1610612759


def _home():
    # 4 x 102 + 100 + 0 (omitted, an observed zero) + 2 = 510 player-possessions.
    return [
        _team_row(),
        *(_player(101 + index, f"Home {index}", OffPoss=102) for index in range(4)),
        _player(105, "Home 5", OffPoss=100),
        _player(106, "Home 6"),
        _player(107, "Home 7", OffPoss=2),
    ]


def _away():
    # 5 x 101 = 505 player-possessions.
    return [_team_row(), *(_player(201 + index, f"Away {index}", OffPoss=101) for index in range(5))]


def _game(home=None, away=None):
    return canonical_game_from_pbp(
        _wire(home or _home(), away or _away()),
        event=_event(),
        retrieved_at=datetime(2024, 11, 16, tzinfo=UTC),
    )


def _fallback_game():
    return _game(
        home=_as_livedata_fallback([_team_row(), _player(101, "Home One")]),
        away=_as_livedata_fallback([_team_row(), _player(201, "Away One")]),
    )


def _team_possessions(game):
    return {fact.team_id: fact.offensive_possessions for fact in game.team_facts}


def _repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'possessions.sqlite3'}")
    run_migrations(engine)
    return engine, CanonicalGameLedgerRepository(engine)


def _stored(engine):
    players = CanonicalGameLedgerPlayerFact.__table__
    teams = CanonicalGameLedgerTeamFact.__table__
    with engine.connect() as connection:
        return (
            dict(connection.execute(select(teams.c.team_id, teams.c.offensive_possessions)).all()),
            dict(connection.execute(select(players.c.player_id, players.c.offensive_possessions)).all()),
        )


def _null_possessions(engine):
    with engine.begin() as connection:
        for model in (CanonicalGameLedgerPlayerFact, CanonicalGameLedgerTeamFact):
            connection.execute(update(model.__table__).values(offensive_possessions=None))


FILLED = (
    {LAL: 102.0, SAS: 101.0},
    {
        101: 102.0,
        102: 102.0,
        103: 102.0,
        104: 102.0,
        105: 100.0,
        106: 0.0,
        107: 2.0,
        201: 101.0,
        202: 101.0,
        203: 101.0,
        204: 101.0,
        205: 101.0,
    },
)


# --- extraction ---------------------------------------------------------------


def test_team_possessions_are_the_player_offposs_sum_over_five():
    game = _game()

    assert _team_possessions(game) == {LAL: 102.0, SAS: 101.0}
    assert {fact.player_id: fact.offensive_possessions for fact in game.player_facts} == FILLED[1]


def test_a_game_whose_rows_never_carry_offposs_derives_none_never_zero():
    game = _fallback_game()

    assert _team_possessions(game) == {LAL: None, SAS: None}
    assert {fact.offensive_possessions for fact in game.player_facts} == {None}
    assert validate_complete_game(game).game_id == game.game_id


def test_the_typed_checksum_ignores_possessions_so_the_backfill_never_invalidates_lineage():
    game = _game()
    without = replace(
        game,
        player_facts=tuple(replace(p, offensive_possessions=None) for p in game.player_facts),
        team_facts=tuple(replace(t, offensive_possessions=None) for t in game.team_facts),
    )

    assert without.with_checksum().checksum == game.checksum


def test_a_team_fact_that_is_not_its_players_sum_over_five_is_rejected():
    game = _game()
    wrong = replace(
        game,
        team_facts=tuple(
            replace(team, offensive_possessions=103.0) if team.team_id == LAL else team
            for team in game.team_facts
        ),
    )

    with pytest.raises(LedgerValidationError):
        validate_complete_game(wrong)


def test_a_game_derives_possessions_for_every_player_or_for_none():
    game = _game()
    mixed = replace(
        game,
        player_facts=(replace(game.player_facts[0], offensive_possessions=None), *game.player_facts[1:]),
    )

    with pytest.raises(LedgerValidationError, match="offensive_possessions for every player or for none"):
        validate_complete_game(mixed)


# --- storage and the backfill -------------------------------------------------


def test_possessions_round_trip_through_storage(tmp_path):
    engine, repository = _repository(tmp_path)
    repository.replace_game(_game())

    assert _stored(engine) == FILLED


def test_backfill_fills_possessions_changes_no_checksum_and_a_rerun_changes_nothing(tmp_path):
    engine, repository = _repository(tmp_path)
    game = _game()
    repository.replace_game(game)
    _null_possessions(engine)

    first = repository.backfill_offensive_possessions(season="2024-25")
    filled = _stored(engine)
    second = repository.backfill_offensive_possessions(season="2024-25")

    assert (first.examined, first.updated, first.rejected, first.superseded) == (1, 1, (), 0)
    assert filled == FILLED
    assert (second.examined, second.updated, second.unchanged) == (1, 0, 1)
    assert _stored(engine) == FILLED
    stored = repository.get_game(game.game_id)
    assert stored.checksum == game.checksum
    assert stored.with_checksum().checksum == game.checksum


def test_a_dry_run_reports_the_change_and_writes_nothing(tmp_path):
    engine, repository = _repository(tmp_path)
    repository.replace_game(_game())
    _null_possessions(engine)

    result = repository.backfill_offensive_possessions(season="2024-25", dry_run=True)

    assert (result.updated, result.dry_run) == (1, True)
    assert _stored(engine) == ({LAL: None, SAS: None}, dict.fromkeys(FILLED[1]))


def test_backfill_leaves_fallback_games_null(tmp_path):
    engine, repository = _repository(tmp_path)
    repository.replace_game(_fallback_game())

    result = repository.backfill_offensive_possessions(season="2024-25")

    assert (result.examined, result.updated, result.not_observed) == (1, 0, 1)
    assert _stored(engine) == ({LAL: None, SAS: None}, {101: None, 201: None})


@pytest.mark.parametrize("possessions_first", [True, False], ids=("possessions-first", "usage-first"))
def test_pending_usage_and_possessions_backfills_succeed_in_either_order(tmp_path, possessions_first):
    engine, repository = _repository(tmp_path)
    repository.replace_game(
        _game(home=[_team_row(), _player(101, "Home One", OffPoss=480, Usage=20.0), _player(102, "Home Two")])
    )
    _null_possessions(engine)
    with engine.begin() as connection:
        connection.execute(update(CanonicalGameLedgerPlayerFact.__table__).values(usage_pct=None))

    steps = [repository.backfill_offensive_possessions, repository.backfill_usage]
    if not possessions_first:
        steps.reverse()
    results = [step(season="2024-25") for step in steps]

    assert [(r.updated, r.rejected) for r in results] == [(1, ())] * 2
    assert _stored(engine)[0] == {LAL: 96.0, SAS: 101.0}
