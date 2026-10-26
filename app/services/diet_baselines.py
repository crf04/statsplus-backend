"""League shares from the same stored Diet generation the Matchup reads."""

from app.domain.player_diet_taxonomy import PLAYER_DIET_QUALIFIER_SLICES
from app.services.player_diet import PLAYER_DIET_PUBLICATION_STREAM_KEYS, PLAYER_DIET_PUBLICATION_STREAMS
from app.services.publication_snapshot_calls import call_with_read_scope
from app.services.research_season import published_capture, season_context
from app.services.serving_season import readiness_rule, snapshot_trailing


class DietBaselinesService:
    def __init__(self, *, player_diets, settings, publication_reader=None):
        self.player_diets = player_diets
        self.settings = settings
        self.publication_reader = publication_reader

    def get(self):
        season, snapshot = published_capture(
            self.settings,
            self.publication_reader,
            lambda season: self.publication_reader.snapshot(
                tuple(sorted(PLAYER_DIET_PUBLICATION_STREAM_KEYS)),
                season=season,
                # Baselines need only decoded facts; a decode-cache hit never
                # selects or parses the league-wide diet payloads.
                decoded_only_keys=PLAYER_DIET_PUBLICATION_STREAM_KEYS,
            ),
        )
        result = (
            call_with_read_scope(
                self.player_diets.get_for_players,
                season,
                (),
                publication_snapshot=snapshot,
            )
            if self.player_diets is not None
            else None
        )
        shares = {}
        for base, slices in PLAYER_DIET_QUALIFIER_SLICES.items():
            shares[base] = {}
            for slice_key in slices:
                baseline = result.baselines.get((base, slice_key)) if result else None
                value = baseline.league_average_share if baseline else None
                shares[base][slice_key] = round(value, 6) if value is not None else None
        observed = [item.retrieved_at for item in result.observations] if result else []
        context = season_context(self.publication_reader, snapshot, lambda: season)
        if context is None:
            return {
                "season": season,
                "captured_at": max(observed).isoformat() if observed else None,
                "shares": shares,
            }
        # A selection names the season each base actually serves, and which trail.
        bases = {}
        for base, stream_key in sorted(PLAYER_DIET_PUBLICATION_STREAMS.items()):
            read = snapshot.read(stream_key)
            bases[base] = {
                "family": readiness_rule(stream_key).family,
                "season": read.season if read.available else None,
            }
        served = [entry["season"] for entry in bases.values() if entry["season"] is not None]
        return {
            "season_context": context,
            "season": max(served, default=season),
            "captured_at": max(observed).isoformat() if observed else None,
            "shares": shares,
            "bases": bases,
            "trailing": snapshot_trailing(snapshot),
        }
