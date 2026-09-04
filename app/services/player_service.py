import pandas as pd
import logging
import requests
from collections.abc import Callable
from nba_api.stats.static import players
from rapidfuzz import process, fuzz
from typing import Optional
from ..errors import (
    AppError,
    InvalidInputError,
    ProviderUnavailableError,
    ResourceNotFoundError,
)
from app.config.settings import RuntimeSettings, get_runtime_settings
from app.models.catalogs import PLAY_TYPES
from app.providers.nba_stats import NBAStatsAdapter, NBAStatsProvider
from app.services.athlete_resolver import normalize_athlete_name
from app.services.player_diet import PlayerDietResult
from app.services.progress import RefreshProgress
from app.services.table_publisher import PublicationFence

logger = logging.getLogger(__name__)


class PlayerProfileReader:
    """Read-only catalog and Player Diet capability for player profiles.

    ``PlayerDietService`` owns the provider-backed refresh adapters, so the
    request service receives its repository directly through this narrow
    wrapper.  The catalog reader follows the same engine-only pattern.  This
    keeps the profile/list read path unable to reach NBA Stats or PBP Stats.
    """

    __slots__ = ("_catalog", "_diets")

    def __init__(self, catalog_reader, diet_reader) -> None:
        if not callable(getattr(catalog_reader, "get_catalog", None)):
            raise TypeError("player profile catalog reader must expose get_catalog")
        if not callable(getattr(diet_reader, "get_for_players", None)):
            raise TypeError("player profile diet reader must expose get_for_players")
        self._catalog = catalog_reader
        self._diets = diet_reader

    @classmethod
    def unavailable(cls) -> "PlayerProfileReader":
        """Build the database-only empty reader used by the demo fixture."""

        class UnavailableCatalogReader:
            @staticmethod
            def get_catalog(season: str, *, active_only: bool = False):
                del season, active_only
                return ()

        class UnavailableDietReader:
            @staticmethod
            def get_for_players(season: str, player_ids):
                del player_ids
                return PlayerDietResult(
                    season=season,
                    players={},
                    observations=(),
                )

        return cls(UnavailableCatalogReader(), UnavailableDietReader())

    def get_catalog(self, season: str, *, active_only: bool = False):
        return self._catalog.get_catalog(season, active_only=active_only)

    def get_for_players(self, season: str, player_ids):
        return self._diets.get_for_players(season, player_ids)


_ASSIST_LOCATION_SLICES = (
    "Arc3Assists",
    "Corner3Assists",
    "AtRimAssists",
    "ShortMidRangeAssists",
    "LongMidRangeAssists",
)
_TWO_POINT_ASSIST_SLICES = (
    "AtRimAssists",
    "ShortMidRangeAssists",
    "LongMidRangeAssists",
)
_THREE_POINT_ASSIST_SLICES = ("Arc3Assists", "Corner3Assists")
_DURABLE_PROFILE_CATEGORIES = frozenset(("Playtypes", "assists"))


class PlayerService:
    def __init__(
        self,
        db_engine,
        settings: RuntimeSettings | None = None,
        nba_stats_provider: NBAStatsProvider | None = None,
        publication_reader=None,
        profile_reader: PlayerProfileReader | None = None,
    ):
        self.engine = db_engine
        self.settings = settings or get_runtime_settings()
        self.nba_stats = nba_stats_provider or NBAStatsAdapter(settings=self.settings)
        self.publication_reader = publication_reader
        self.profile_reader = (
            profile_reader
            if profile_reader is not None
            else PlayerProfileReader.unavailable()
        )

    def get_all_players(self):
        """Fetch list of all players from database"""
        season = self.settings.nba.current_season
        catalog = self.profile_reader.get_catalog(season, active_only=False)
        catalog_by_id = {int(row["player_id"]): row for row in catalog}
        if not catalog_by_id:
            return []
        result = self.profile_reader.get_for_players(
            season, tuple(sorted(catalog_by_id))
        )
        return [
            catalog_by_id[player_id]["display_name"]
            for player_id in sorted(catalog_by_id)
            if any(
                fact.base == "play_types"
                for fact in result.players.get(player_id, ())
            )
        ]

    def get_player_profile(self, player_name, category, opp_team=None):
        """
        Get player profile data based on category.
        Categories: Playtypes, assists, Archetype
        """
        
        if not player_name or not category:
            raise InvalidInputError("player_name and category are required.")

        canonical = self._resolve_profile_player(player_name)
        if category in _DURABLE_PROFILE_CATEGORIES:
            if canonical is None:
                raise ResourceNotFoundError("The requested player was not found.")
            player_id, canonical_name, team_abbreviation = canonical
        elif canonical is not None:
            _, player_name, _ = canonical
        else:
            player_name = self._fuzzy_match_player_name(player_name)
            if player_name is None:
                raise ResourceNotFoundError("The requested player was not found.")

        try:
            handlers = {
                "Playtypes": lambda: self._get_durable_player_playtypes(
                    player_id, canonical_name, team_abbreviation
                ),
                "assists": lambda: self._get_durable_player_assists(
                    player_id, canonical_name
                ),
                "Archetype": lambda: self._get_archetype_gamelogs(
                    player_name, opp_team
                ),
                "Shooting Type": lambda: self._get_shooting_type(player_name),
                "Zone Shooting": lambda: self._get_player_zone_shooting(player_name),
            }
            handler = handlers.get(category)
            if handler is None:
                raise InvalidInputError("The requested profile category is invalid.")
            return handler()
        except AppError:
            raise
        except requests.exceptions.RequestException as error:
            raise ProviderUnavailableError(detail=error) from error
        except (IndexError, KeyError) as error:
            raise ResourceNotFoundError(
                "The requested player profile was not found.", detail=error
            ) from error
        except Exception:
            logger.exception("Error getting player profile")
            raise

    def _resolve_profile_player(self, player_name):
        """Resolve one input to the current-season canonical catalog row."""

        season = self.settings.nba.current_season
        rows = self.profile_reader.get_catalog(season, active_only=False)
        target = normalize_athlete_name(player_name)
        if not target:
            return None
        matches = [
            row
            for row in rows
            if normalize_athlete_name(row.get("display_name")) == target
        ]
        if not matches:
            return None
        row = min(
            matches,
            key=lambda item: (
                not bool(
                    item.get("is_active_for_season", item.get("is_active", False))
                ),
                int(item["player_id"]),
            ),
        )
        return (
            int(row["player_id"]),
            str(row.get("display_name") or ""),
            row.get("team_abbreviation"),
        )

    def _durable_profile_result(self, player_id):
        result = self.profile_reader.get_for_players(
            self.settings.nba.current_season, [player_id]
        )
        return result, tuple(result.players.get(player_id, ()))

    def _get_durable_player_playtypes(
        self, player_id: int, canonical_name: str, team_abbreviation: str | None
    ):
        _, facts = self._durable_profile_result(player_id)
        facts_by_slice = {
            fact.slice_key: fact
            for fact in facts
            if fact.base == "play_types"
        }
        if not facts_by_slice:
            raise ResourceNotFoundError("The requested player profile was not found.")
        return {
            "PLAYER_NAME": canonical_name,
            "TEAM_ABBREVIATION": team_abbreviation,
            **{
                f"{play_type}%": float(
                    facts_by_slice[play_type].share * 100
                )
                if play_type in facts_by_slice
                else 0
                for play_type in PLAY_TYPES
            },
        }

    def _get_durable_player_assists(self, player_id: int, canonical_name: str):
        result, facts = self._durable_profile_result(player_id)
        facts_by_slice = {
            fact.slice_key: fact
            for fact in facts
            if fact.base == "assist_locations"
        }
        if not facts_by_slice:
            return []

        shares = {
            slice_key: float(facts_by_slice[slice_key].share)
            if slice_key in facts_by_slice
            else 0.0
            for slice_key in _ASSIST_LOCATION_SLICES
        }
        baselines = result.baselines

        def baseline_share(slice_keys):
            values = [
                baselines.get(("assist_locations", slice_key)).league_average_share
                for slice_key in slice_keys
                if baselines.get(("assist_locations", slice_key)) is not None
                and baselines.get(("assist_locations", slice_key)).league_average_share
                is not None
            ]
            total = sum(values)
            return total if total > 0 else None

        derived = {
            "TwoPtAssists": sum(shares[key] for key in _TWO_POINT_ASSIST_SLICES),
            "ThreePtAssists": sum(shares[key] for key in _THREE_POINT_ASSIST_SLICES),
        }
        all_shares = {**shares, **derived}
        all_baseline_slices = {
            **{
                key: ("assist_locations", key)
                for key in _ASSIST_LOCATION_SLICES
            },
            "TwoPtAssists": ("assist_locations", _TWO_POINT_ASSIST_SLICES),
            "ThreePtAssists": ("assist_locations", _THREE_POINT_ASSIST_SLICES),
        }
        output = {"Name": canonical_name}
        for key in (
            "TwoPtAssists",
            "ThreePtAssists",
            *_ASSIST_LOCATION_SLICES,
        ):
            value = all_shares[key] * 100
            output[key] = value
            baseline_key = all_baseline_slices[key]
            if isinstance(baseline_key[1], tuple):
                denominator = baseline_share(baseline_key[1])
            else:
                baseline = baselines.get(baseline_key)
                denominator = (
                    None
                    if baseline is None
                    else baseline.league_average_share
                )
            output[f"{key}+"] = value / (denominator * 100) if denominator else 0
        return [output]
        
    def _fuzzy_match_player_name(self, player_name: str) -> Optional[str]:
        """
        Fuzzy match player name against the player database.
        
        Args:
            player_name (str): The input player name to match
            
        Returns:
            Optional[str]: The best matching player name from database, or None if no good match found
        """
        try:
            # Get all player names from database
            df = self._fetch_data_from_table('player_information')
            
            if df.empty:
                return None
                
            all_player_names = df['full_name'].tolist()
            
            # First try exact match (case insensitive)
            player_name_lower = player_name.lower().strip()
            for name in all_player_names:
                if name.lower().strip() == player_name_lower:
                    return name
            
            # If no exact match, use fuzzy matching
            match = process.extractOne(
                player_name,
                all_player_names,
                scorer=fuzz.token_sort_ratio,
                score_cutoff=85  # Require 85% similarity
            )
            
            if match:
                return match[0]  # Return the matched name
                
            return None
            
        except Exception:
            logger.exception("Error fuzzy matching player name %r", player_name)
            raise

    def _get_player_zone_shooting(self, player_name):
        """Get player zone shooting data"""
        df = self._fetch_data_from_table('player_shooting_zones')
        return df[df['PLAYER_NAME'] == player_name].to_dict(orient='records')[0]

    def _get_shooting_type(self, player_name):
        """Get player shooting type data"""
        player_team = self._fetch_data_from_table('player_team_table')
        team_id = player_team[player_team['Player'] == player_name]['Team_ID'].values[0]
        df = self.nba_stats.fetch_player_shot_chart(
            self.get_player_id(player_name),
            int(team_id),
        )
        df['SHOT_TYPE'].replace({'Less than 10 ft': '<10 Ft'}, inplace=True)
        df['SHOT_TYPE'].replace({'Pull Ups': 'Pullup'}, inplace=True)
        df['SHOT_TYPE'].replace({'Catch and Shoot': 'C&S'}, inplace=True)
        df.fillna(0, inplace=True)
        return df.to_dict(orient='records')
    
    def _get_archetype_gamelogs(self, player_name, opp_team):
        """Get archetype gamelogs for player against specific team"""
        try:
            # Get player's cluster members
            player_ids = self._get_archetype_players_from_player(player_name)
            
            
            # Get team ID
            team_dict = pd.DataFrame(self._get_teams())
            team_id = team_dict.loc[team_dict['full_name'] == opp_team, 'id'].values[0]
            
            # Get normalized cluster game logs through the app-owned provider.
            gl = self.nba_stats.get_archetype_game_logs(
                player_ids=player_ids,
                opponent_team_id=int(team_id),
                season=self.settings.nba.current_season,
            )

            # The provider applies the cluster-member filter at its seam.
            gl = gl[['PLAYER_NAME', 'PLAYER_ID', 'GAME_DATE', 'MIN', 
                    'FGM', 'FGA', 'FG3M', 'FG3A', 'FTM', 'FTA', 'PTS', 'TOV']]
            
            per36_df = self._per36_frame()
            
            # Calculate per 36minute stats
            for col in ['FGM', 'FGA', 'FG3M', 'FG3A', 'FTM', 'FTA', 'PTS', 'TOV']:
                gl[f'{col}/36MIN'] = (gl[col] / gl['MIN']) * 36
            
            logger.debug("Archetype game logs rows: %s", len(gl))
            merged_df = gl.merge(per36_df, left_on="PLAYER_ID", right_on="PLAYER_ID", suffixes=('', '_season'))
            #get percentage diff between game and season
            for col in ['FGM', 'FGA', 'FG3M', 'FG3A', 'FTM', 'FTA', 'PTS', 'TOV']:
                merged_df[f'{col}/36MIN_DIFF'] = (merged_df[f'{col}/36MIN'] - merged_df[f'{col}_season']) / merged_df[f'{col}_season']
            
            merged_df = merged_df[['PLAYER_NAME', 'GAME_DATE', 'MIN', 
                    'FGM', 'FGA', 'FG3M', 'FG3A', 'FTM', 'FTA', 'PTS', 'TOV','FGM/36MIN', 'FGA/36MIN', 'FG3M/36MIN', 'FG3A/36MIN', 
                               'FTM/36MIN', 'FTA/36MIN', 'PTS/36MIN', 
                               'FGM/36MIN_DIFF', 'FGA/36MIN_DIFF', 'FG3M/36MIN_DIFF', 'FG3A/36MIN_DIFF', 
                               'FTM/36MIN_DIFF', 'FTA/36MIN_DIFF', 'PTS/36MIN_DIFF', 'TOV/36MIN_DIFF']]

            return merged_df.to_dict(orient='records')
        except Exception as e:
            logger.error("Error getting archetype gamelogs: %s", e)
            return []

    def _per36_frame(self):
        """Read active per-36 facts from the immutable publication first."""

        if self.publication_reader is None:
            return self._fetch_data_from_table("player_per36_stats")
        from app.services.database_first_activation import (
            PublicationPayloadError,
            decode_player_per36,
        )

        season = self.settings.nba.current_season
        read = self.publication_reader.read("player_per36", season=season)
        if read.legacy_fallback_allowed:
            return self._fetch_data_from_table("player_per36_stats")
        if not read.available:
            return pd.DataFrame(
                columns=[
                    "PLAYER_ID", "FGM", "FGA", "FG3M", "FG3A", "FTM", "FTA",
                    "PTS", "TOV",
                ]
            )
        try:
            facts = decode_player_per36(read.payload, season=season)
        except PublicationPayloadError:
            return pd.DataFrame()
        return pd.DataFrame(
            [
                {
                    "PLAYER_ID": fact.player_id,
                    "FGM": fact.field_goals_made_per36,
                    "FGA": fact.field_goals_attempted_per36,
                    "FG3M": fact.three_pointers_made_per36,
                    "FG3A": fact.three_pointers_attempted_per36,
                    "FTM": fact.free_throws_made_per36,
                    "FTA": fact.free_throws_attempted_per36,
                    "PTS": fact.points_per36,
                    "TOV": fact.turnovers_per36,
                }
                for fact in facts
            ]
        )

    def _get_archetype_players_from_player(self, player_name):
        """Get list of player IDs in the same cluster as the given player"""
        try:
            players_df = self._fetch_data_from_table('player_clusters')
            cluster_id = players_df[players_df['PlayerName'] == player_name]['ClusterID'].values[0]
            result = players_df[players_df['ClusterID'] == cluster_id]['PlayerID']
            return result.tolist()
        except Exception as e:
            logger.error("Error getting archetype players: %s", e)
            return []

    def store_player_information(
        self,
        *,
        progress_callback: Callable | None = None,
        publication_fence: PublicationFence | None = None,
    ):
        """Store basic player information in database.

        Fetching completes before the table is replaced, so a provider failure
        leaves the existing player list untouched.
        """
        progress = RefreshProgress(progress_callback)
        progress.fetch("Fetching player information")
        player_dict = players.get_players()
        player_df = pd.DataFrame.from_dict(player_dict)
        progress.transform("Transforming player information")
        from app.services.table_publisher import AtomicTablePublisher

        progress.publish("Publishing player information")
        AtomicTablePublisher(self.engine).publish(
            {"player_information": player_df},
            publication_fence=publication_fence,
        )
        progress.complete()
        return True

    def get_player_id(self, player_name):
        """Get player ID from database"""
        try:
            df = self._fetch_data_from_table('player_information')
            player = df[df['full_name'] == player_name]
            return player['id'].values[0]
        except Exception as e:
            logger.error("Error getting player ID: %s", e)
            return None

    def _fetch_data_from_table(self, table_name):
        """Helper method to fetch data from database table"""
        from ..utils.tables import normalize_table_name
        table_name = normalize_table_name(table_name)
        query = f"SELECT * FROM {table_name}"
        with self.engine.connect() as conn:
            return pd.read_sql(query, conn)

    @staticmethod
    def _get_teams():
        """Helper method to get NBA teams"""
        from nba_api.stats.static import teams
        return teams.get_teams()
