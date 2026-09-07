"""Reject impossible stored playtype partitions without renormalizing evidence."""
from dataclasses import replace

import pandas as pd
import pytest

from app.errors import ResourceNotFoundError
from app.models.catalogs import PLAY_TYPES
from app.services.player_diet import PlayerDietResult
from app.services.player_service import PlayerService
from tests.services.test_player_service import (
    _DurableProfileReader, _catalog_row, _fact, _settings,
)
from tests.services.test_player_diets import _service


# Production 2025-26 player 1627824, read-only audit: category-specific
# team stints produced different denominators (36 versus 26 games).
MIXED_STINT_ROWS = (
    ("Cut", .101, 12, 36), ("Misc", .084, 10, 36),
    ("Postup", .054, 14, 26), ("PRRollMan", .267, 69, 26),
    ("Spotup", .487, 58, 36), ("Transition", .151, 18, 36),
)


def test_profile_rejects_recorded_impossible_traded_player_partition():
    facts = tuple(
        replace(_fact(1627824, "play_types", key, share, volume=volume),
                games_played=games)
        for key, share, volume, games in MIXED_STINT_ROWS
    )
    reader = _DurableProfileReader(
        [_catalog_row(1627824, "Recorded Player")],
        PlayerDietResult("2025-26", {1627824: facts}, ()),
    )
    service = PlayerService(object(), settings=_settings(), profile_reader=reader)
    with pytest.raises(ResourceNotFoundError):
        service.get_player_profile("Recorded Player", "Playtypes")
    assert sum(f.share for f in facts) == pytest.approx(1.144)


@pytest.mark.parametrize("shares", [(0.2, 0.05), (0.5, 0.505)])
def test_profile_preserves_sparse_and_provider_rounded_shares(shares):
    facts = tuple(_fact(1, "play_types", key, share)
                  for key, share in zip(("Cut", "Spotup"), shares))
    reader = _DurableProfileReader(
        [_catalog_row(1, "Valid Player")],
        PlayerDietResult("2025-26", {1: facts}, ()),
    )
    profile = PlayerService(object(), settings=_settings(), profile_reader=reader)
    result = profile.get_player_profile("Valid Player", "Playtypes")
    assert result["Cut%"] == pytest.approx(shares[0] * 100)
    assert result["Spotup%"] == pytest.approx(shares[1] * 100)
    assert result["Isolation%"] == 0


@pytest.mark.parametrize("share_scale, quarantined", [(1.0, True), (0.5, False)])
def test_refresh_omits_impossible_mixed_stints_but_preserves_sparse_player(
    tmp_path, share_scale, quarantined
):
    service, nba, _ = _service(tmp_path, player_ids=(2544, 1627824))
    recorded = {key: (share, volume, games)
                for key, share, volume, games in MIXED_STINT_ROWS}

    def fetch(play_type, **kwargs):
        rows = [{"PLAYER_ID": 2544, "PLAY_TYPE": play_type,
                 "TYPE_GROUPING": "Offensive", "GP": 40,
                 "POSS_PCT": .02, "POSS": 12}]
        if play_type in recorded:
            share, volume, games = recorded[play_type]
            rows.append({"PLAYER_ID": 1627824, "PLAY_TYPE": play_type,
                         "TYPE_GROUPING": "Offensive", "GP": games,
                         "POSS_PCT": share * share_scale, "POSS": volume})
        return pd.DataFrame(rows)

    nba.fetch_synergy_play_types = fetch
    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544, 1627824])
    affected = [f for f in result.players.get(1627824, ())
                if f.base == "play_types"]
    assert bool(affected) is not quarantined
    if not quarantined:
        assert {f.slice_key: f.share for f in affected} == {
            key: share * share_scale
            for key, share, _, _ in MIXED_STINT_ROWS
        }
    valid = [f for f in result.players[2544] if f.base == "play_types"]
    assert len(valid) == len(PLAY_TYPES)
    assert all(f.share == .02 for f in valid)
    assert any(o.base == "play_types" and o.status == "available"
               for o in result.observations)
