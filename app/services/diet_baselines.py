"""League shares from the same stored Diet generation the Matchup reads."""
from app.domain.player_diet_taxonomy import PLAYER_DIET_QUALIFIER_SLICES
from app.services.player_diet import PLAYER_DIET_PUBLICATION_STREAM_KEYS
from app.services.publication_snapshot_calls import call_with_read_scope


class DietBaselinesService:
    def __init__(self, *, player_diets, settings, publication_reader=None):
        self.player_diets = player_diets
        self.settings = settings
        self.publication_reader = publication_reader

    def get(self):
        season = self.settings.nba.current_season
        snapshot = (
            self.publication_reader.snapshot(
                tuple(sorted(PLAYER_DIET_PUBLICATION_STREAM_KEYS)), season=season
            ) if self.publication_reader is not None else None
        )
        result = (
            call_with_read_scope(
                self.player_diets.get_for_players, season, (),
                publication_snapshot=snapshot,
            ) if self.player_diets is not None else None
        )
        shares = {}
        for base, slices in PLAYER_DIET_QUALIFIER_SLICES.items():
            shares[base] = {}
            for slice_key in slices:
                baseline = result.baselines.get((base, slice_key)) if result else None
                value = baseline.league_average_share if baseline else None
                shares[base][slice_key] = round(value, 6) if value is not None else None
        observed = [item.retrieved_at for item in result.observations] if result else []
        return {
            'season': season,
            'captured_at': max(observed).isoformat() if observed else None,
            'shares': shares,
        }
