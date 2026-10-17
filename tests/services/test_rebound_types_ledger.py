"""Miss-type rebound counts in the Canonical Game Ledger (crf04/statsplus#234).

Ledger derivation seam: raw PBP FullGame rows become player and team facts,
the identity rejects counts that do not add up, a backfill re-derives stored
games from their archived rows without a provider, and the team windows and
the player Diet derived from those facts add up to the player-credited
``OPP_REB``.  Every expected value is a literal.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, select, update

import app.services.canonical_game_ledger as ledger_module
from app.migrations import run_migrations
from app.models.canonical_game_ledger import (
    CanonicalGameLedgerGame,
    CanonicalGameLedgerPlayerFact,
    CanonicalGameLedgerTeamFact,
    LedgerGameRowEvidence,
)
from app.models.collection_control import CompositionJob
from app.services.canonical_game_ledger import (
    CanonicalGameLedgerRepository,
    LedgerValidationError,
    canonical_game_from_pbp,
    rederive_rebound_types,
    validate_complete_game,
)
from app.services.ledger_derivations import (
    LedgerDerivationUnavailable,
    derive_player_rebound_type_diet_rows,
    materialize_rebound_type_window,
)
from tests.services.test_canonical_game_ledger import _event
from tests.services.test_ledger_derivations import _league_games

SIX = (
    "def_two_pt_rebounds",
    "def_three_pt_rebounds",
    "ft_def_rebounds",
    "off_two_pt_rebounds",
    "off_three_pt_rebounds",
    "ft_off_rebounds",
)
_DIAGNOSTIC = (
    "Points", "FG2M", "FG2A", "FG3M", "FG3A", "FtPoints", "FTA", "OffRebounds",
    "DefRebounds", "Rebounds", "Assists", "Turnovers", "Steals", "Blocks", "Fouls",
)


def _six(fact):
    return tuple(getattr(fact, name) for name in SIX)


def _team_row(**fields):
    return {"EntityId": "0", "Name": "Team", "Minutes": "00:00", **fields}


def _player(entity_id, name, **fields):
    return {"EntityId": str(entity_id), "Name": name, "Minutes": "20:00", **fields}


def _diagnostic(rows):
    totals = {name: 0 for name in _DIAGNOSTIC}
    for row in rows:
        for name in _DIAGNOSTIC:
            totals[name] += row.get(name, 0)
    totals["Rebounds"] = totals["OffRebounds"] + totals["DefRebounds"]
    return {"FullGame": totals}


def _wire(home_rows, away_rows):
    return {
        "stats": {"Home": {"FullGame": home_rows}, "Away": {"FullGame": away_rows}},
        "team_results": {
            "Home": _diagnostic(home_rows),
            "Away": _diagnostic(away_rows),
        },
        "home_team_abbreviation": "LAL",
        "away_team_abbreviation": "SAS",
        "date": "2024-11-15",
        "participant_ids_by_team": {
            "1610612747": [
                int(row["EntityId"]) for row in home_rows if row["EntityId"] != "0"
            ],
            "1610612759": [
                int(row["EntityId"]) for row in away_rows if row["EntityId"] != "0"
            ],
        },
    }


def _game(wire=None):
    wire = wire or _wire(
        [
            _team_row(),
            _player(
                101, "Home One", OffRebounds=1, DefRebounds=2,
                DefTwoPtRebounds=2, OffThreePtRebounds=1,
            ),
            _player(
                102, "Home Two", DefRebounds=3,
                DefThreePtRebounds=1, FTDefRebounds=2,
            ),
            _player(103, "Home Three"),
        ],
        [
            _team_row(),
            _player(
                201, "Away One", OffRebounds=2, DefRebounds=1,
                DefTwoPtRebounds=1, OffTwoPtRebounds=1, FTOffRebounds=1,
            ),
            _player(202, "Away Two"),
        ],
    )
    return canonical_game_from_pbp(
        wire,
        event=_event(),
        retrieved_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
    )


def _by_name(game):
    return {player.player_name: player for player in game.player_facts}


def _repository(tmp_path, name="rebound.sqlite3"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    run_migrations(engine)
    return engine, CanonicalGameLedgerRepository(engine)


# --- extraction ---------------------------------------------------------------


def test_a_field_missing_from_a_row_counts_as_zero():
    players = _by_name(_game())

    assert _six(players["Home One"]) == (2, 0, 0, 0, 1, 0)
    assert _six(players["Home Two"]) == (0, 1, 2, 0, 0, 0)
    assert _six(players["Away One"]) == (1, 0, 0, 1, 0, 1)
    # No rebounds and no fields at all is a derived zero row, not "not derived".
    assert _six(players["Home Three"]) == (0, 0, 0, 0, 0, 0)
    assert _six(players["Away Two"]) == (0, 0, 0, 0, 0, 0)


def test_team_facts_sum_the_player_rows_and_exclude_team_only_rebounds():
    home_team_row = _team_row(
        DefRebounds=4, DefThreePtRebounds=4, OffRebounds=2, OffTwoPtRebounds=2,
        Rebounds=6,
    )
    game = _game(_wire(
        [
            home_team_row,
            _player(
                101, "Home One", OffRebounds=1, DefRebounds=2,
                DefTwoPtRebounds=2, OffThreePtRebounds=1,
            ),
            _player(
                102, "Home Two", DefRebounds=3,
                DefThreePtRebounds=1, FTDefRebounds=2,
            ),
        ],
        [_team_row(), _player(201, "Away One", DefRebounds=1, DefTwoPtRebounds=1)],
    ))
    home = next(fact for fact in game.team_facts if fact.is_home)

    # The team fact keeps the team-only rebounds the traditional total counts:
    # 5 player + 4 team defensive, 1 player + 2 team offensive.
    assert (home.defensive_rebounds, home.offensive_rebounds) == (9, 3)
    # The six slices are the player rows alone: 5 defensive and 1 offensive.
    assert _six(home) == (2, 1, 2, 0, 1, 0)
    assert sum(_six(home)[:3]) == 5 and sum(_six(home)[3:]) == 1


def test_a_row_whose_parts_do_not_add_up_rejects_the_whole_game():
    short_defensive = _player(
        101, "Home One", OffRebounds=1, DefRebounds=2,
        DefTwoPtRebounds=1, OffThreePtRebounds=1,
    )
    with pytest.raises(LedgerValidationError, match="add up"):
        _game(_wire(
            [_team_row(), short_defensive],
            [_team_row(), _player(201, "Away One")],
        ))
    extra_offensive = _player(
        101, "Home One", OffRebounds=1, DefRebounds=2,
        DefTwoPtRebounds=2, OffThreePtRebounds=1, FTOffRebounds=1,
    )
    with pytest.raises(LedgerValidationError, match="add up"):
        _game(_wire(
            [_team_row(), extra_offensive],
            [_team_row(), _player(201, "Away One")],
        ))


def test_a_fact_that_fails_the_identity_is_rejected_at_the_repository_boundary(tmp_path):
    _engine, repository = _repository(tmp_path)
    game = _game()
    broken = replace(
        game.player_facts[0],
        def_two_pt_rebounds=game.player_facts[0].def_two_pt_rebounds + 1,
    )
    candidate = replace(game, player_facts=(broken, *game.player_facts[1:]))

    with pytest.raises(LedgerValidationError, match="add up"):
        validate_complete_game(candidate)
    with pytest.raises(LedgerValidationError):
        repository.replace_game(candidate)
    assert repository.get_game(game.game_id) is None


def _as_livedata_fallback(rows):
    """Rows whose team summary identifies them as the LiveData fallback."""

    return [
        {**row, "LedgerEvidenceProvider": "nba_live_data"}
        if row["EntityId"] == "0" else row
        for row in rows
    ]


def test_explicitly_identified_fallback_evidence_derives_nothing():
    game = _game(_wire(
        _as_livedata_fallback(
            [_team_row(), _player(101, "Home One", OffRebounds=1, DefRebounds=2)]
        ),
        _as_livedata_fallback([_team_row(), _player(201, "Away One", DefRebounds=1)]),
    ))

    assert all(_six(player) == (None,) * 6 for player in game.player_facts)
    assert all(_six(team) == (None,) * 6 for team in game.team_facts)
    # Absent is not zero: the game is still a complete, valid ledger game.
    assert validate_complete_game(game).game_id == game.game_id


def test_ordinary_pbp_rows_with_rebounds_and_no_split_at_all_are_rejected():
    """A provider regression must not replace complete evidence with NULLs."""

    with pytest.raises(LedgerValidationError, match="add up"):
        _game(_wire(
            [_team_row(), _player(101, "Home One", OffRebounds=1, DefRebounds=2)],
            [_team_row(), _player(201, "Away One", DefRebounds=1)],
        ))


def test_fallback_evidence_that_does_carry_the_split_is_still_held_to_it():
    with pytest.raises(LedgerValidationError, match="add up"):
        _game(_wire(
            _as_livedata_fallback([
                _team_row(),
                _player(101, "Home One", DefRebounds=2, DefTwoPtRebounds=1),
            ]),
            _as_livedata_fallback([_team_row(), _player(201, "Away One")]),
        ))


def test_a_pbp_game_with_no_rebounds_and_no_split_derives_zeros():
    game = _game(_wire(
        [_team_row(), _player(101, "Home One")],
        [_team_row(), _player(201, "Away One")],
    ))

    assert all(_six(player) == (0,) * 6 for player in game.player_facts)


def test_a_game_derives_the_split_for_every_player_or_for_none():
    game = _game()
    mixed = replace(
        game,
        player_facts=(
            replace(game.player_facts[0], **{name: None for name in SIX}),
            *game.player_facts[1:],
        ),
    )

    with pytest.raises(LedgerValidationError, match="every player or for none"):
        validate_complete_game(mixed)


def test_team_facts_must_equal_the_sum_over_their_player_rows():
    game = _game()
    home = next(fact for fact in game.team_facts if fact.is_home)
    tampered = replace(home, def_two_pt_rebounds=home.def_two_pt_rebounds + 1)
    candidate = replace(
        game,
        team_facts=tuple(
            tampered if fact is home else fact for fact in game.team_facts
        ),
    )

    with pytest.raises(LedgerValidationError):
        validate_complete_game(candidate)


def test_a_side_with_no_team_summary_row_still_proves_its_team_counts():
    """The sparse wire omits a team row when the side has no team-only residual."""

    game = _game(_wire(
        [
            _team_row(),
            _player(
                101, "Home One", OffRebounds=1, DefRebounds=2, Rebounds=3,
                DefTwoPtRebounds=2, OffThreePtRebounds=1,
            ),
        ],
        [_player(201, "Away One", DefRebounds=3, Rebounds=3, DefThreePtRebounds=3)],
    ))
    away = next(fact for fact in game.team_facts if not fact.is_home)
    assert _six(away) == (0, 3, 0, 0, 0, 0)
    assert validate_complete_game(game).game_id == game.game_id

    tampered = replace(away, def_three_pt_rebounds=2, def_two_pt_rebounds=1)
    candidate = replace(
        game,
        team_facts=tuple(tampered if fact is away else fact for fact in game.team_facts),
    )
    with pytest.raises(LedgerValidationError, match="sum over the side's player rows"):
        validate_complete_game(candidate)


def test_the_typed_checksum_ignores_the_six_so_backfilling_never_invalidates_lineage():
    game = _game()
    without = replace(
        game,
        player_facts=tuple(
            replace(player, **{name: None for name in SIX})
            for player in game.player_facts
        ),
        team_facts=tuple(
            replace(team, **{name: None for name in SIX})
            for team in game.team_facts
        ),
    )

    assert game.checksum == without.with_checksum().checksum
    # What the six are a function of is archived, and hashed, in the raw rows.
    assert any(
        "DefTwoPtRebounds" in row.payload
        for row in game.raw_rows
        if row.row_type == "player"
    )


# --- storage and the backfill -------------------------------------------------


def _null_the_six(engine):
    nulls = {name: None for name in SIX}
    with engine.begin() as connection:
        connection.execute(update(CanonicalGameLedgerPlayerFact.__table__).values(nulls))
        connection.execute(update(CanonicalGameLedgerTeamFact.__table__).values(nulls))


def _stored_six(engine):
    with engine.connect() as connection:
        players = connection.execute(
            select(
                CanonicalGameLedgerPlayerFact.__table__.c.player_id,
                *[getattr(CanonicalGameLedgerPlayerFact.__table__.c, n) for n in SIX],
            ).order_by(CanonicalGameLedgerPlayerFact.__table__.c.player_id)
        ).all()
        teams = connection.execute(
            select(
                CanonicalGameLedgerTeamFact.__table__.c.team_id,
                *[getattr(CanonicalGameLedgerTeamFact.__table__.c, n) for n in SIX],
            ).order_by(CanonicalGameLedgerTeamFact.__table__.c.team_id)
        ).all()
    return [tuple(row) for row in players], [tuple(row) for row in teams]


def test_the_counts_round_trip_through_storage(tmp_path):
    _engine_, repository = _repository(tmp_path)
    game = _game()
    repository.replace_game(game)

    stored = repository.get_game(game.game_id)

    assert [(p.player_id, *_six(p)) for p in stored.player_facts] == [
        (101, 2, 0, 0, 0, 1, 0),
        (102, 0, 1, 2, 0, 0, 0),
        (103, 0, 0, 0, 0, 0, 0),
        (201, 1, 0, 0, 1, 0, 1),
        (202, 0, 0, 0, 0, 0, 0),
    ]
    assert [(t.team_id, *_six(t)) for t in stored.team_facts] == [
        (1610612747, 2, 1, 2, 0, 1, 0),
        (1610612759, 1, 0, 0, 1, 0, 1),
    ]


def test_backfill_fills_null_counts_from_the_archived_rows_and_a_rerun_changes_nothing(
    tmp_path,
):
    engine, repository = _repository(tmp_path)
    game = _game()
    repository.replace_game(game)
    _null_the_six(engine)
    assert _stored_six(engine)[0][0] == (101, None, None, None, None, None, None)

    first = repository.backfill_rebound_types(season="2024-25")

    assert (first.examined, first.updated, first.unchanged, first.not_observed) == (1, 1, 0, 0)
    assert first.rejected == () and first.superseded == 0
    players, teams = _stored_six(engine)
    assert players == [
        (101, 2, 0, 0, 0, 1, 0),
        (102, 0, 1, 2, 0, 0, 0),
        (103, 0, 0, 0, 0, 0, 0),
        (201, 1, 0, 0, 1, 0, 1),
        (202, 0, 0, 0, 0, 0, 0),
    ]
    assert teams == [
        (1610612747, 2, 1, 2, 0, 1, 0),
        (1610612759, 1, 0, 0, 1, 0, 1),
    ]

    second = repository.backfill_rebound_types(season="2024-25")

    assert (second.examined, second.updated, second.unchanged) == (1, 0, 1)
    assert _stored_six(engine) == (players, teams)


def test_backfill_changes_no_checksum_and_queues_no_composition_work(tmp_path):
    engine, repository = _repository(tmp_path)
    game = _game()
    repository.replace_game(game)
    _null_the_six(engine)
    with engine.connect() as connection:
        before = connection.execute(
            select(
                CanonicalGameLedgerGame.__table__.c.checksum,
                CanonicalGameLedgerGame.__table__.c.raw_checksum,
                CanonicalGameLedgerGame.__table__.c.updated_at,
            )
        ).one()
        raw_before = connection.execute(
            select(LedgerGameRowEvidence.__table__.c.checksum)
            .order_by(
                LedgerGameRowEvidence.__table__.c.side,
                LedgerGameRowEvidence.__table__.c.row_index,
            )
        ).scalars().all()

    repository.backfill_rebound_types(season="2024-25")

    with engine.connect() as connection:
        after = connection.execute(
            select(
                CanonicalGameLedgerGame.__table__.c.checksum,
                CanonicalGameLedgerGame.__table__.c.raw_checksum,
                CanonicalGameLedgerGame.__table__.c.updated_at,
            )
        ).one()
        raw_after = connection.execute(
            select(LedgerGameRowEvidence.__table__.c.checksum)
            .order_by(
                LedgerGameRowEvidence.__table__.c.side,
                LedgerGameRowEvidence.__table__.c.row_index,
            )
        ).scalars().all()
        jobs = connection.execute(select(CompositionJob.__table__)).all()
    assert tuple(after) == tuple(before)
    assert raw_after == raw_before
    assert jobs == []


def test_a_dry_run_reports_the_change_and_writes_nothing(tmp_path):
    engine, repository = _repository(tmp_path)
    repository.replace_game(_game())
    _null_the_six(engine)
    nulls = _stored_six(engine)

    result = repository.backfill_rebound_types(season="2024-25", dry_run=True)

    assert (result.updated, result.dry_run) == (1, True)
    assert _stored_six(engine) == nulls


def test_backfill_leaves_fallback_evidence_null_and_rejects_unmarked_evidence(tmp_path):
    engine, repository = _repository(tmp_path)
    repository.replace_game(_game(_wire(
        _as_livedata_fallback(
            [_team_row(), _player(101, "Home One", OffRebounds=1, DefRebounds=2)]
        ),
        _as_livedata_fallback([_team_row(), _player(201, "Away One", DefRebounds=1)]),
    )))

    result = repository.backfill_rebound_types(season="2024-25")

    assert (result.examined, result.updated, result.not_observed) == (1, 0, 1)
    assert _stored_six(engine)[0] == [
        (101, None, None, None, None, None, None),
        (201, None, None, None, None, None, None),
    ]
    # The same archive without its fallback marker is ordinary PBP evidence
    # with rebounds and no split: it is rejected, not silently skipped.
    raw = LedgerGameRowEvidence.__table__
    with engine.begin() as connection:
        for row in connection.execute(select(raw.c.side, raw.c.row_index, raw.c.payload)
                                      .where(raw.c.row_type == "team")).all():
            payload = json.loads(row.payload)
            payload.pop("LedgerEvidenceProvider")
            connection.execute(update(raw).where(
                raw.c.side == row.side, raw.c.row_index == row.row_index
            ).values(payload=json.dumps(payload)))

    rejected = repository.backfill_rebound_types(season="2024-25")

    assert rejected.updated == 0 and rejected.not_observed == 0
    assert len(rejected.rejected) == 1 and "add up" in rejected.rejected[0][1]


def test_backfill_rejects_an_archive_whose_parts_do_not_add_up_and_writes_nothing(
    tmp_path,
):
    engine, repository = _repository(tmp_path)
    game = _game()
    repository.replace_game(game)
    _null_the_six(engine)
    raw = LedgerGameRowEvidence.__table__
    with engine.begin() as connection:
        payload = connection.execute(
            select(raw.c.payload).where(raw.c.entity_id == 101)
        ).scalar_one()
        broken = json.loads(payload)
        broken["DefTwoPtRebounds"] = 1
        connection.execute(
            update(raw).where(raw.c.entity_id == 101).values(payload=json.dumps(broken))
        )

    result = repository.backfill_rebound_types(season="2024-25")

    assert result.updated == 0
    assert [game_id for game_id, _reason in result.rejected] == [game.game_id]
    assert "add up" in result.rejected[0][1]
    assert _stored_six(engine)[0][0] == (101, None, None, None, None, None, None)


def test_backfill_never_overwrites_a_game_a_correction_replaced_meanwhile(
    tmp_path, monkeypatch
):
    engine, repository = _repository(tmp_path)
    game = _game()
    repository.replace_game(game)
    _null_the_six(engine)
    real = ledger_module.rederive_rebound_types

    def corrected_meanwhile(stored):
        derived = real(stored)
        with engine.begin() as connection:
            connection.execute(
                update(CanonicalGameLedgerGame.__table__).values(raw_checksum="f" * 64)
            )
        return derived

    monkeypatch.setattr(ledger_module, "rederive_rebound_types", corrected_meanwhile)

    result = repository.backfill_rebound_types(season="2024-25")

    assert (result.updated, result.superseded) == (0, 1)
    assert _stored_six(engine)[0][0] == (101, None, None, None, None, None, None)


def test_rederiving_a_stored_game_reproduces_what_extraction_derived():
    game = _game()
    underived = replace(
        game,
        player_facts=tuple(
            replace(player, **{name: None for name in SIX})
            for player in game.player_facts
        ),
        team_facts=tuple(
            replace(team, **{name: None for name in SIX}) for team in game.team_facts
        ),
    )

    assert rederive_rebound_types(underived) == game


# --- windows and the Diet -----------------------------------------------------


def _window(games, *, window_games=None):
    expected = frozenset(game.game_id for game in games)
    by_team = {
        team_id: frozenset(
            game.game_id
            for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }
    return materialize_rebound_type_window(
        games,
        season="2025-26",
        as_of=date(2025, 10, 15),
        window_games=window_games,
        expected_game_ids=expected,
        expected_team_game_ids=by_team if window_games else None,
        team_ids=frozenset(range(1, 31)),
    )


def test_the_season_window_counts_what_each_teams_opponents_collected():
    window = _window(_league_games())
    team = {row.team_id: row for row in window.teams}

    assert window.complete and len(window.teams) == 30
    assert dict(team[1].counts) == {
        "DefTwoPtRebounds": 30,
        "DefThreePtRebounds": 15,
        "FTDefRebounds": 15,
        "OffTwoPtRebounds": 7,
        "OffThreePtRebounds": 8,
        "FTOffRebounds": 0,
    }
    # 15 games of 48 minutes: per 48 is the count over 15.
    assert team[1].team_minutes == 720.0
    assert team[1].per48["DefTwoPtRebounds"] == pytest.approx(2.0)
    assert team[1].per48["OffTwoPtRebounds"] == pytest.approx(7 / 15)
    assert team[1].per48["OffThreePtRebounds"] == pytest.approx(8 / 15)
    assert team[1].league_average["DefTwoPtRebounds"] == pytest.approx(2.0)
    assert team[1].league_average["OffTwoPtRebounds"] == pytest.approx(0.5)
    assert team[1].population_sigma["OffTwoPtRebounds"] == pytest.approx(0.259772, abs=1e-6)
    assert team[1].population_sigma["FTDefRebounds"] == 0.0
    # Rank 1 allows the fewest, ties share the lower rank (the sheet convention).
    assert {key: team[1].competition_rank[key] for key in team[1].competition_rank} == {
        "DefTwoPtRebounds": 11,
        "DefThreePtRebounds": 11,
        "FTDefRebounds": 1,
        "OffTwoPtRebounds": 13,
        "OffThreePtRebounds": 16,
        "FTOffRebounds": 1,
    }
    assert team[2].competition_rank["OffTwoPtRebounds"] == 29
    assert team[3].competition_rank["OffTwoPtRebounds"] == 1


def test_every_teams_six_slices_add_up_to_its_player_credited_opp_reb():
    window = _window(_league_games())

    assert [sum(team.counts.values()) for team in window.teams] == [75] * 30
    for team in window.teams:
        assert sum(team.per48.values()) == pytest.approx(75 / 15)


def test_the_l15_window_is_independent_of_the_season_window():
    window = _window(_league_games(), window_games=15)

    assert window.complete
    assert [sum(team.counts.values()) for team in window.teams] == [75] * 30


def test_slices_that_do_not_add_up_to_opp_reb_fail_the_window():
    games = list(_league_games())
    first = games[0]
    home = next(fact for fact in first.team_facts if fact.is_home)
    # One opposing team fact carries a slice the player rows do not.
    inflated = replace(home, def_two_pt_rebounds=home.def_two_pt_rebounds + 1)
    games[0] = replace(
        first,
        team_facts=tuple(inflated if fact is home else fact for fact in first.team_facts),
    )

    with pytest.raises(LedgerDerivationUnavailable, match="OPP_REB"):
        _window(games)


def test_a_window_with_a_game_that_never_derived_the_split_is_unavailable():
    games = list(_league_games())
    first = games[0]
    games[0] = replace(
        first,
        team_facts=tuple(
            replace(fact, **{name: None for name in SIX}) for fact in first.team_facts
        ),
    )

    with pytest.raises(LedgerDerivationUnavailable, match="derived miss-type counts"):
        _window(games)


def test_the_player_diet_shares_partition_each_players_rebounds():
    rows = derive_player_rebound_type_diet_rows(_league_games()[:1])
    by_player = {}
    for row in rows:
        by_player.setdefault(row["player_id"], []).append(row)

    # Player 1001 (team 1) in the first game: 4 defensive (1 two, 0... see the
    # fixture: 3 - 1 = 2 two-point, 1 three-point, 1 free-throw) and 1
    # offensive (1 two-point, since team 1 is odd).
    shares = {
        row["slice_key"]: (row["share"], row["volume"])
        for row in by_player[1001]
    }
    assert shares == {
        "DefTwoPtRebounds": (0.4, 2.0),
        "DefThreePtRebounds": (0.2, 1.0),
        "FTDefRebounds": (0.2, 1.0),
        "OffTwoPtRebounds": (0.2, 1.0),
        "OffThreePtRebounds": (0.0, 0.0),
        "FTOffRebounds": (0.0, 0.0),
    }
    assert {row["volume_unit"] for row in rows} == {"rebounds"}
    assert {row["games_played"] for row in by_player[1001]} == {1}
    assert sum(share for share, _volume in shares.values()) == pytest.approx(1.0)


def test_a_player_with_no_rebounds_has_no_diet_and_an_unobserved_game_refuses_one():
    game = _game()
    rows = derive_player_rebound_type_diet_rows((game,))

    assert {row["player_id"] for row in rows} == {101, 102, 201}
    assert 103 not in {row["player_id"] for row in rows}
    assert [row["slice_key"] for row in rows if row["player_id"] == 101] == [
        "DefTwoPtRebounds", "DefThreePtRebounds", "FTDefRebounds",
        "OffTwoPtRebounds", "OffThreePtRebounds", "FTOffRebounds",
    ]
    underived = replace(
        game,
        player_facts=tuple(
            replace(player, **{name: None for name in SIX}) for player in game.player_facts
        ),
    )
    with pytest.raises(LedgerDerivationUnavailable):
        derive_player_rebound_type_diet_rows((underived,))
