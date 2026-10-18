"""The Rebound Types vocabulary and where it plugs into the Matchup (#446)."""

from __future__ import annotations

from app.domain.rebound_types import REBOUND_TYPE_FIELDS, REBOUND_TYPE_WIRE_NAMES
from app.domain.team_matchup_taxonomy import (
    DEFENSE_SHEET_BASES,
    LEDGER_DEFENSE_SHEET_METRICS,
    LEDGER_OWNED_MATCHUP_STREAM_KEYS,
    LEDGER_OWNED_MATCHUP_SURFACES,
    LEDGER_PARITY_MATCHUP_SURFACES,
    defense_sheet_identities,
    matchup_stream_key,
)
from app.services.matchup import slice_markets
from app.services.matchup_parity import LEDGER_OWNED_SURFACES


def test_the_six_slices_are_the_provider_field_names_in_publication_order():
    assert REBOUND_TYPE_WIRE_NAMES == (
        "DefTwoPtRebounds",
        "DefThreePtRebounds",
        "FTDefRebounds",
        "OffTwoPtRebounds",
        "OffThreePtRebounds",
        "FTOffRebounds",
    )
    assert [attribute for attribute, _wire in REBOUND_TYPE_FIELDS] == [
        "def_two_pt_rebounds",
        "def_three_pt_rebounds",
        "ft_def_rebounds",
        "off_two_pt_rebounds",
        "off_three_pt_rebounds",
        "ft_off_rebounds",
    ]


def test_the_base_sits_directly_after_traditional_and_after_the_original_five():
    assert DEFENSE_SHEET_BASES == (
        "play_types",
        "shot_zones",
        "shot_types",
        "assist_locations",
        "traditional",
        "rebound_types",
    )


def test_the_sheet_rows_are_the_six_slices_each_their_own_metric():
    assert LEDGER_DEFENSE_SHEET_METRICS["rebound_types"] == {name: name for name in REBOUND_TYPE_WIRE_NAMES}
    assert defense_sheet_identities("rebound_types") == tuple(
        (name, name, name) for name in REBOUND_TYPE_WIRE_NAMES
    )


def test_every_slice_feeds_exactly_the_four_rebound_markets():
    for name in REBOUND_TYPE_WIRE_NAMES:
        assert slice_markets("rebound_types", name, name) == ("REB", "PR", "RA", "PRA")
    # The row it sits beside is unchanged.
    assert slice_markets("traditional", "OPP_REB", "OPP_REB") == ("REB", "PR", "RA", "PRA")
    assert slice_markets("traditional", "OPP_TOV", "OPP_TOV") == ("TOV",)


def test_the_streams_are_ledger_owned_but_outside_the_legacy_parity_dual_run():
    assert matchup_stream_key("rebound_types", "season") == "rebound_types_season"
    assert matchup_stream_key("rebound_types", "l15") == "rebound_types_l15"
    assert "rebound_types" in LEDGER_OWNED_MATCHUP_SURFACES
    assert {"rebound_types_season", "rebound_types_l15"} <= LEDGER_OWNED_MATCHUP_STREAM_KEYS
    # There is no legacy source to compare against, so the dual-run never
    # waits on it.
    assert LEDGER_PARITY_MATCHUP_SURFACES == ("traditional", "assist_locations")
    assert LEDGER_OWNED_SURFACES == LEDGER_PARITY_MATCHUP_SURFACES


def test_rebound_types_is_an_optional_rebound_component_never_a_required_input():
    # REB blends rebound_types in when stored (crf04/statsplus#234), but it
    # is never required: its absence must not add `missing_inputs`.
    from app.services.matchup import _SCORE_INPUT_BASES

    for market in ("REB", "PR", "RA", "PRA", "PTS", "AST", "PA", "TOV", "STL", "BLK", "STKS"):
        assert "rebound_types" not in _SCORE_INPUT_BASES[market]
