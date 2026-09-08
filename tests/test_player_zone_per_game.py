"""Player shooting zones carry the provider's per-game makes and attempts.

The tab shows per-game values and `LeagueDashPlayerShotLocations` returns no
games-played column in any per-mode, so the provider's own `PerGame` rate is
published as it stands rather than derived here from a games count that need
not agree with the provider's.

The consequence is deliberate and worth stating: each zone is rounded to one
decimal independently, so the exact `Corner 3 == Left + Right` identity the
opponent surface asserts cannot hold here, and this surface therefore has no
cheap check against a per-mode fault of the kind repaired in
crf04/statsplus#51.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.collector.contracts import ProviderContractError
from app.collector.normalizers import normalize_zone_response

NOW = datetime(2026, 8, 13, 8, tzinfo=timezone.utc)


def _player(player_id: int, **overrides):
    row = {
        "player_id": player_id,
        "Restricted Area_FGM": 2.4, "Restricted Area_FGA": 4.1,
        "In The Paint (Non-RA)_FGM": 0.8, "In The Paint (Non-RA)_FGA": 2.2,
        "Mid-Range_FGM": 1.1, "Mid-Range_FGA": 2.9,
        "Above the Break 3_FGM": 1.4, "Above the Break 3_FGA": 3.8,
        "Left Corner 3_FGM": 0.4, "Left Corner 3_FGA": 0.9,
        "Right Corner 3_FGM": 0.3, "Right Corner 3_FGA": 0.7,
        # Independently rounded, so deliberately not the sum of the sides.
        "Corner 3_FGM": 0.6, "Corner 3_FGA": 1.5,
    }
    row.update(overrides)
    return row


def _normalize(rows):
    return normalize_zone_response(rows, season="2025-26", cutoff=NOW)


def test_the_scope_declares_per_game_so_evidence_states_its_own_scale():
    observation = _normalize([_player(1)])

    assert observation.scope["value_mode"] == "per_game"


def test_makes_and_attempts_are_both_carried_per_zone():
    """Attempts alone cannot reproduce the shooting tab this replaces."""

    observation = _normalize([_player(1)])
    by_zone = {
        record["category"]: record for record in observation.payload["records"]
    }

    assert by_zone["Restricted Area"]["FGM"] == 2.4
    assert by_zone["Restricted Area"]["FGA"] == 4.1
    assert by_zone["Corner 3"]["FGM"] == 0.6
    assert by_zone["Corner 3"]["FGA"] == 1.5
    assert set(by_zone) == {
        "Restricted Area", "In The Paint (Non-RA)", "Mid-Range",
        "Corner 3", "Above the Break 3",
    }


def test_a_corner_that_is_not_its_two_sides_is_published_anyway():
    """Rounding, not a defect: about two in five real players look like this.

    The fixture's combined corner (0.6 / 1.5) is already not its sides
    (0.7 / 1.6); publishing it is the whole point of taking the provider's
    rate as authoritative.
    """

    observation = _normalize([_player(1)])
    by_zone = {
        record["category"]: record for record in observation.payload["records"]
    }

    assert by_zone["Corner 3"]["FGA"] == 1.5
    assert "withheld_players" not in observation.payload


def test_makes_exceeding_attempts_withholds_only_that_player():
    """Rounding cannot produce this: zero of 2620 real pairs do.

    One response carries the whole league, so an impossible row withholds its
    own player rather than discarding everyone else's sound rows.
    """

    observation = _normalize([
        _player(1),
        _player(2, **{"Mid-Range_FGM": 9.9}),
        _player(3),
    ])

    published = {record["player_id"] for record in observation.payload["records"]}
    assert published == {1, 3}
    assert "mid-range_fgm_exceeds_fga" in (
        observation.payload["withheld_players"]["2"]
    )


def test_a_missing_zone_column_still_fails_the_source_as_a_whole():
    """A withheld player is inconsistent evidence; this is a broken contract."""

    row = _player(1)
    del row["Mid-Range_FGA"]

    with pytest.raises(ProviderContractError):
        _normalize([row])


def test_the_zone_request_asks_the_provider_for_per_game(monkeypatch):
    """Pin the per-mode itself, not just what the normalizer does with it.

    Every other test here passes against either per-mode, so without this the
    choice would be unasserted.
    """

    from nba_api.stats import endpoints

    from app.collector.provider import _StandaloneNBAProvider

    captured: dict[str, object] = {}

    class _Recorder:
        def __init__(self, **parameters):
            captured.update(parameters)

    monkeypatch.setattr(endpoints, "LeagueDashPlayerShotLocations", _Recorder)
    provider = _StandaloneNBAProvider.__new__(_StandaloneNBAProvider)
    provider.timeout = 1.0
    provider._frame = lambda value: [_player(1)]

    provider.fetch_player_shooting_zone(
        None, season="2025-26", season_type="Regular Season"
    )

    assert captured["per_mode_detailed"] == "PerGame"
    assert captured["distance_range"] == "By Zone"


def test_the_grouped_provider_header_keeps_makes_and_attempts_apart():
    """Exercise the real wire shape, not an already-flattened stand-in.

    The provider returns one grouped header spanning FGM, FGA and FG_PCT per
    category.  A flattener that kept only attempts would satisfy every other
    test here, because they all start from flattened keys.
    """

    zones = [
        "Restricted Area", "In The Paint (Non-RA)", "Mid-Range",
        "Left Corner 3", "Right Corner 3", "Above the Break 3",
        "Backcourt", "Corner 3",
    ]
    triples = {
        "Restricted Area": (2.4, 4.1), "In The Paint (Non-RA)": (0.8, 2.2),
        "Mid-Range": (1.1, 2.9), "Left Corner 3": (0.4, 0.9),
        "Right Corner 3": (0.3, 0.7), "Above the Break 3": (1.4, 3.8),
        "Backcourt": (0.0, 0.1), "Corner 3": (0.6, 1.5),
    }
    row = [1, "Fixture Player", 1610612737, "ATL", 25.0, "Fixture"]
    for zone in zones:
        makes, attempts = triples[zone]
        row.extend([makes, attempts, round(makes / attempts, 3) if attempts else 0.0])

    observation = _normalize({
        "resultSets": {
            "name": "ShotLocations",
            "headers": [
                {"name": "SHOT_CATEGORY", "columnsToSkip": 6,
                 "columnSpan": 3, "columnNames": zones},
                {"name": "columns", "columnSpan": 1, "columnNames": []},
            ],
            "rowSet": [row],
        }
    })

    by_zone = {
        record["category"]: record for record in observation.payload["records"]
    }
    assert by_zone["Restricted Area"]["FGM"] == 2.4
    assert by_zone["Restricted Area"]["FGA"] == 4.1
    assert by_zone["Restricted Area"]["FGM"] != by_zone["Restricted Area"]["FGA"]
    assert by_zone["Corner 3"]["FGM"] == 0.6
    assert by_zone["Corner 3"]["FGA"] == 1.5
