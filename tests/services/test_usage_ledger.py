"""PBPStats ``Usage`` in the Canonical Game Ledger (crf04/statsplus-backend#450).

Raw PBP FullGame rows become ``usage_pct`` on the player facts (0-100, as the
provider sends it), the typed checksum ignores it so a backfill invalidates no
lineage, and the backfill re-derives stored games from their own archive.
Every expected value is a literal.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, select, update

from app.migrations import run_migrations
from app.models.canonical_game_ledger import CanonicalGameLedgerPlayerFact
from app.services.canonical_game_ledger import (
    CanonicalGameLedgerRepository,
    LedgerValidationError,
    canonical_game_from_pbp,
    rederive_usage,
    validate_complete_game,
)
from tests.services.test_canonical_game_ledger import _event
from tests.services.test_rebound_types_ledger import (
    _as_livedata_fallback,
    _player,
    _team_row,
    _wire,
)


def _game(*, home=None, away=None, team_usage=None):
    team_fields = {} if team_usage is None else {"Usage": team_usage}
    wire = _wire(
        [
            _team_row(**team_fields),
            _player(101, "Home One", Usage=28.5714),
            _player(102, "Home Two", Usage=1.41),
            _player(103, "Home Three"),
        ] if home is None else home,
        [
            _team_row(),
            _player(201, "Away One", Usage=100.0),
            _player(202, "Away Two"),
        ] if away is None else away,
    )
    return canonical_game_from_pbp(
        wire,
        event=_event(),
        retrieved_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
    )


def _usage(game):
    return {fact.player_id: fact.usage_pct for fact in game.player_facts}


def _repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'usage.sqlite3'}")
    run_migrations(engine)
    return engine, CanonicalGameLedgerRepository(engine)


def _stored(engine):
    table = CanonicalGameLedgerPlayerFact.__table__
    with engine.connect() as connection:
        return {
            row.player_id: row.usage_pct
            for row in connection.execute(select(table.c.player_id, table.c.usage_pct))
        }


def _null_usage(engine, *, except_player=None):
    table = CanonicalGameLedgerPlayerFact.__table__
    statement = update(table).values(usage_pct=None)
    if except_player is not None:
        statement = statement.where(table.c.player_id != except_player)
    with engine.begin() as connection:
        connection.execute(statement)


# --- extraction ---------------------------------------------------------------


def test_usage_is_the_providers_value_unrounded_and_a_missing_row_is_an_observed_zero():
    assert _usage(_game()) == {
        101: 28.5714,
        102: 1.41,
        103: 0.0,
        201: 100.0,
        202: 0.0,
    }


def test_a_game_whose_rows_never_carry_usage_derives_none_for_every_player():
    game = _game(
        home=_as_livedata_fallback([_team_row(), _player(101, "Home One")]),
        away=_as_livedata_fallback([_team_row(), _player(201, "Away One")]),
    )

    assert _usage(game) == {101: None, 201: None}
    # Absent is not zero, and the game is still a complete, valid ledger game.
    assert validate_complete_game(game).game_id == game.game_id


def test_a_team_summary_row_carrying_usage_does_not_make_the_game_usage_bearing():
    game = _game(
        home=[_team_row(Usage=55.0), _player(101, "Home One")],
        away=[_team_row(), _player(201, "Away One")],
    )

    assert _usage(game) == {101: None, 201: None}


def test_a_game_derives_usage_for_every_player_or_for_none():
    game = _game()
    mixed = replace(
        game,
        player_facts=(replace(game.player_facts[0], usage_pct=None), *game.player_facts[1:]),
    )

    with pytest.raises(LedgerValidationError, match="usage_pct for every player or for none"):
        validate_complete_game(mixed)


def test_usage_outside_zero_to_one_hundred_is_rejected():
    game = _game(home=[_team_row(), _player(101, "Home One", Usage=100.5)])

    with pytest.raises(LedgerValidationError, match="usage_pct must be null or between 0 and 100"):
        validate_complete_game(game)


def test_the_typed_checksum_ignores_usage_so_backfilling_never_invalidates_lineage():
    game = _game()
    without = replace(
        game,
        player_facts=tuple(replace(p, usage_pct=None) for p in game.player_facts),
    )

    assert game.checksum == without.with_checksum().checksum
    # What usage is a function of is archived, and hashed, in the raw rows.
    assert any(
        row.payload.get("Usage") == 28.5714
        for row in game.raw_rows
        if row.row_type == "player"
    )


# --- storage and the backfill -------------------------------------------------


def test_usage_round_trips_through_storage(tmp_path):
    engine, repository = _repository(tmp_path)
    game = _game()
    repository.replace_game(game)

    assert _usage(repository.get_game(game.game_id)) == _usage(game)
    assert _stored(engine) == {101: 28.5714, 102: 1.41, 103: 0.0, 201: 100.0, 202: 0.0}


def test_backfill_fills_null_usage_changes_no_checksum_and_a_rerun_changes_nothing(
    tmp_path,
):
    engine, repository = _repository(tmp_path)
    game = _game()
    repository.replace_game(game)
    _null_usage(engine)
    assert set(_stored(engine).values()) == {None}

    first = repository.backfill_usage(season="2024-25")

    assert (first.examined, first.updated, first.unchanged, first.not_observed) == (1, 1, 0, 0)
    assert first.rejected == () and first.superseded == 0
    filled = _stored(engine)
    assert filled == {101: 28.5714, 102: 1.41, 103: 0.0, 201: 100.0, 202: 0.0}
    assert repository.get_game(game.game_id).checksum == game.checksum

    second = repository.backfill_usage(season="2024-25")

    assert (second.examined, second.updated, second.unchanged) == (1, 0, 1)
    assert _stored(engine) == filled
    assert repository.get_game(game.game_id).checksum == game.checksum


def test_backfill_writes_only_the_players_whose_value_differs(tmp_path):
    engine, repository = _repository(tmp_path)
    repository.replace_game(_game())
    _null_usage(engine, except_player=101)
    updates = []

    @event.listens_for(engine, "before_cursor_execute")
    def record(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("UPDATE"):
            updates.append(parameters)

    result = repository.backfill_usage(season="2024-25")

    assert result.updated == 1
    assert len(updates) == 4
    assert all(28.5714 not in tuple(parameters) for parameters in updates)
    assert _stored(engine) == {101: 28.5714, 102: 1.41, 103: 0.0, 201: 100.0, 202: 0.0}


def test_a_dry_run_reports_the_change_and_writes_nothing(tmp_path):
    engine, repository = _repository(tmp_path)
    repository.replace_game(_game())
    _null_usage(engine)

    result = repository.backfill_usage(season="2024-25", dry_run=True)

    assert (result.updated, result.dry_run) == (1, True)
    assert set(_stored(engine).values()) == {None}


def test_backfill_leaves_fallback_games_null(tmp_path):
    engine, repository = _repository(tmp_path)
    repository.replace_game(_game(
        home=_as_livedata_fallback([_team_row(), _player(101, "Home One")]),
        away=_as_livedata_fallback([_team_row(), _player(201, "Away One")]),
    ))

    result = repository.backfill_usage(season="2024-25")

    assert (result.examined, result.updated, result.not_observed) == (1, 0, 1)
    assert _stored(engine) == {101: None, 201: None}


def test_rederiving_a_stored_game_reproduces_what_extraction_derived(tmp_path):
    _engine, repository = _repository(tmp_path)
    game = _game()
    repository.replace_game(game)

    assert rederive_usage(repository.get_game(game.game_id)) == repository.get_game(game.game_id)


# --- a correction racing the backfill ----------------------------------------


def test_backfill_never_overwrites_a_correction_that_lands_before_its_write(tmp_path):
    """SQLite ignores FOR UPDATE, so the write itself must be conditional."""

    engine, repository = _repository(tmp_path)
    original = _game()
    repository.replace_game(original)
    _null_usage(engine)
    corrected = _game(home=[
        _team_row(),
        _player(101, "Home One", Usage=35.0),
        _player(102, "Home Two", Usage=1.41),
        _player(103, "Home Three"),
    ])
    correction_engine = create_engine(engine.url)
    correction_repository = CanonicalGameLedgerRepository(correction_engine)
    landed = []

    @event.listens_for(engine, "before_cursor_execute")
    def correct_first(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE canonical_game_ledger_player_facts") and not landed:
            landed.append(True)
            correction_repository.replace_game(corrected)

    try:
        result = repository.backfill_usage(season="2024-25")
    finally:
        correction_engine.dispose()

    stored = repository.get_game(original.game_id)
    assert landed == [True]
    assert (result.examined, result.updated, result.superseded) == (1, 0, 1)
    assert stored.raw_checksum == corrected.raw_checksum
    # The corrected game's own extraction stands; nothing stale was written.
    assert _stored(engine) == {101: 35.0, 102: 1.41, 103: 0.0, 201: 100.0, 202: 0.0}
    assert validate_complete_game(stored).game_id == original.game_id


# --- the rebound and usage backfills are independent -------------------------


def _both_pending(tmp_path):
    from sqlalchemy import update as sa_update

    from app.models.canonical_game_ledger import CanonicalGameLedgerTeamFact
    from app.services.canonical_game_ledger import REBOUND_TYPE_ATTRIBUTES

    engine, repository = _repository(tmp_path)
    repository.replace_game(_game(home=[
        _team_row(),
        _player(101, "Home One", Usage=28.5714, DefRebounds=2, DefTwoPtRebounds=2),
        _player(102, "Home Two", Usage=1.41),
        _player(103, "Home Three"),
    ]))
    nulls = {name: None for name in REBOUND_TYPE_ATTRIBUTES}
    with engine.begin() as connection:
        for model in (CanonicalGameLedgerPlayerFact, CanonicalGameLedgerTeamFact):
            connection.execute(sa_update(model.__table__).values(nulls))
    _null_usage(engine)
    return engine, repository


@pytest.mark.parametrize("usage_first", [True, False], ids=("usage-first", "rebound-first"))
def test_pending_rebound_and_usage_backfills_succeed_in_either_order(tmp_path, usage_first):
    engine, repository = _both_pending(tmp_path)
    table = CanonicalGameLedgerPlayerFact.__table__

    def rebounds():
        with engine.connect() as connection:
            return connection.execute(
                select(table.c.player_id, table.c.def_two_pt_rebounds)
                .order_by(table.c.player_id)
            ).all()

    steps = [repository.backfill_usage, repository.backfill_rebound_types]
    if not usage_first:
        steps.reverse()
    results = [step(season="2024-25") for step in steps]

    assert [(r.examined, r.updated, r.rejected) for r in results] == [(1, 1, ())] * 2
    assert _stored(engine) == {101: 28.5714, 102: 1.41, 103: 0.0, 201: 100.0, 202: 0.0}
    assert [tuple(row) for row in rebounds()] == [
        (101, 2), (102, 0), (103, 0), (201, 0), (202, 0),
    ]
    assert [r.updated for r in (
        repository.backfill_usage(season="2024-25"),
        repository.backfill_rebound_types(season="2024-25"),
    )] == [0, 0]


# --- ordinary ingestion does not tolerate a pending backfill ------------------


def test_ordinary_validation_rejects_a_null_usage_the_archive_contradicts():
    game = _game()
    pending = replace(
        game,
        player_facts=tuple(replace(p, usage_pct=None) for p in game.player_facts),
    )

    with pytest.raises(LedgerValidationError, match="typed player facts must match raw"):
        validate_complete_game(pending)
    # Only a backfill validating its own result may tolerate the pending field.
    assert validate_complete_game(pending, tolerate_pending_backfills=True)


def test_ordinary_validation_rejects_null_rebound_counts_the_archive_contradicts():
    from app.services.canonical_game_ledger import REBOUND_TYPE_ATTRIBUTES

    game = _game(home=[
        _team_row(),
        _player(101, "Home One", Usage=28.5714, DefRebounds=2, DefTwoPtRebounds=2),
        _player(102, "Home Two", Usage=1.41),
        _player(103, "Home Three"),
    ])
    pending = replace(
        game,
        player_facts=tuple(
            replace(p, **{name: None for name in REBOUND_TYPE_ATTRIBUTES})
            for p in game.player_facts
        ),
        team_facts=tuple(
            replace(t, **{name: None for name in REBOUND_TYPE_ATTRIBUTES})
            for t in game.team_facts
        ),
    )

    with pytest.raises(LedgerValidationError, match="typed player facts must match raw"):
        validate_complete_game(pending)
    assert validate_complete_game(pending, tolerate_pending_backfills=True)
