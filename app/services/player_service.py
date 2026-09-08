import numpy as np
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
from app.domain.nba_events import REGULAR_SEASON_TYPE
from app.domain.play_type_matchup import complete_play_type_shares
from app.domain.team_matchup_taxonomy import SHOT_TYPE_STORED_TO_DISPLAY
from app.models.catalogs import PLAY_TYPES, SHOOTING_TYPES
from app.services.athlete_resolver import CanonicalAthlete, normalize_athlete_name
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
_DURABLE_PROFILE_CATEGORIES = frozenset(("Playtypes", "assists", "Shooting Type"))

#: How each stored shot-type slice reads in the Shooting Type profile.  The
#: keys are the slice vocabulary the Diet publishes; the values are the labels
#: the tab has always shown and the client still keys its columns on.
_SHOT_TYPE_PROFILE_LABELS = {
    "Catch and Shoot": "C&S",
    "Pullups": "Pullup",
    "Less Than 10 ft": "<10 Ft",
}

#: The per-36 columns the Archetype tab compares against a player's season.
_ARCHETYPE_RATE_COLUMNS = (
    "FGM", "FGA", "FG3M", "FG3A", "FTM", "FTA", "PTS", "TOV",
)


def _per_game(total: float, games_played: int) -> float:
    """One season total on the tab's per-game scale."""

    return round(float(total) / games_played, 1)


def _fraction(value: float) -> float:
    """One provider frequency, kept on the tab's fraction scale."""

    return round(float(value), 3)


def _made_rate(makes: float, attempts: float) -> float:
    """A shooting percentage, which is zero when nothing was attempted."""

    return round(float(makes) / attempts, 3) if attempts else 0.0


class PlayerService:
    def __init__(
        self,
        db_engine,
        profile_reader: PlayerProfileReader,
        settings: RuntimeSettings | None = None,
        publication_reader=None,
        game_logs=None,
    ):
        if profile_reader is None:
            raise TypeError("player profile reader is required")
        self.engine = db_engine
        self.settings = settings or get_runtime_settings()
        self.publication_reader = publication_reader
        self.profile_reader = profile_reader
        # The stored game-log repository, which owns no provider client.  The
        # profile read path therefore cannot reach NBA Stats at all.
        self.game_logs = game_logs

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

        if category in _DURABLE_PROFILE_CATEGORIES:
            canonical = self._resolve_profile_player(player_name)
            if canonical is None:
                raise ResourceNotFoundError("The requested player was not found.")
            player_id = canonical.player_id
            canonical_name = canonical.display_name
            team_abbreviation = canonical.team_abbreviation
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
                "Shooting Type": lambda: self._get_shooting_type(player_id),
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
            CanonicalAthlete.from_row(row)
            for row in rows
            if normalize_athlete_name(row.get("display_name")) == target
        ]
        if not matches:
            return None
        return min(
            matches,
            key=lambda athlete: (
                not athlete.is_active_for_season,
                athlete.player_id,
            ),
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
        shares = complete_play_type_shares(
            (fact.slice_key, fact.share)
            for fact in facts
            if fact.base == "play_types"
        )
        if shares is None:
            raise ResourceNotFoundError("The requested player profile was not found.")
        return {
            "PLAYER_NAME": canonical_name,
            "TEAM_ABBREVIATION": team_abbreviation,
            **{
                f"{play_type}%": float(shares[play_type] * 100)
                if play_type in shares
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
            for slice_key in _ASSIST_LOCATION_SLICES
            if slice_key in facts_by_slice
        }
        baselines = result.baselines

        def baseline_share(slice_keys):
            values = []
            for slice_key in slice_keys:
                baseline = baselines.get(("assist_locations", slice_key))
                if baseline is None or baseline.league_average_share is None:
                    return None
                values.append(baseline.league_average_share)
            total = sum(values)
            return total if total > 0 else None

        derived = {}
        if all(key in shares for key in _TWO_POINT_ASSIST_SLICES):
            derived["TwoPtAssists"] = sum(
                shares[key] for key in _TWO_POINT_ASSIST_SLICES
            )
        if all(key in shares for key in _THREE_POINT_ASSIST_SLICES):
            derived["ThreePtAssists"] = sum(
                shares[key] for key in _THREE_POINT_ASSIST_SLICES
            )
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
            share = all_shares.get(key)
            value = None if share is None else share * 100
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
            output[f"{key}+"] = (
                value / (denominator * 100)
                if value is not None and denominator is not None and denominator > 0
                else None
            )
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
        """Get player zone shooting data.

        Publication-first, following the ``_per36_frame`` precedent: the
        immutable ``exact_shot_zones`` publication is the source once the
        stream is activated, and the legacy ``player_shooting_zones`` table is
        read only while it is not.  Either way the frame is the same 43-column
        profile, rendered once in
        :func:`app.services.player_zone_profile.transform_player_zone_profile`,
        and the row is still located by the fuzzy-matched player name the
        caller resolved.
        """

        df = self._zone_shooting_frame()
        return df[df['PLAYER_NAME'] == player_name].to_dict(orient='records')[0]

    def _zone_shooting_frame(self):
        """Read the Zone Shooting profile from the publication, else legacy."""

        if self.publication_reader is None:
            return self._fetch_data_from_table('player_shooting_zones')
        from app.services.database_first_activation import (
            PublicationPayloadError,
            decode_player_shot_zones,
        )
        from app.services.player_zone_profile import transform_player_zone_profile

        season = self.settings.nba.current_season
        read = self.publication_reader.read("exact_shot_zones", season=season)
        if read.legacy_fallback_allowed:
            return self._fetch_data_from_table('player_shooting_zones')
        if not read.available:
            return pd.DataFrame(columns=['PLAYER_NAME'])
        try:
            rows = decode_player_shot_zones(read.payload)
        except PublicationPayloadError:
            return pd.DataFrame(columns=['PLAYER_NAME'])
        frame = pd.DataFrame([
            {
                'PLAYER_ID': row.player_id,
                'PLAYER_NAME': row.player_name,
                'TEAM_ID': row.team_id,
                'TEAM_ABBREVIATION': row.team_abbreviation,
                'AGE': row.age,
                'NICKNAME': row.nickname,
                **{
                    f'{category}_{metric}': value
                    for category, values in row.categories.items()
                    for metric, value in values.items()
                },
            }
            for row in rows
        ])
        # An entirely unreported category would otherwise arrive as an object
        # column of ``None``, which the profile arithmetic cannot sum.  A
        # reported one is already float and is unchanged.
        for column in frame.columns:
            if column not in ('PLAYER_NAME', 'TEAM_ABBREVIATION', 'NICKNAME'):
                frame[column] = pd.to_numeric(frame[column])
        # The whole published population is transformed, not just the matched
        # player: the profile's ``PTS%+`` columns are ratios against a league
        # mean taken over every source row.
        return transform_player_zone_profile(frame)

    def _get_shooting_type(self, player_id: int):
        """Render the Shooting Type tab from stored player Diet facts.

        The stored facts carry season Totals, while the tab has always shown
        per-game counts, so each count is divided by the fact's own games
        played.  A slice whose made/attempted split was never observed is
        omitted rather than shown with invented two- and three-point values.
        """

        _, facts = self._durable_profile_result(player_id)
        facts_by_slice = {
            SHOT_TYPE_STORED_TO_DISPLAY.get(fact.slice_key, fact.slice_key): fact
            for fact in facts
            if fact.base == "shot_types"
        }
        rows = []
        for shooting_type in SHOOTING_TYPES:
            fact = facts_by_slice.get(shooting_type)
            if fact is None or fact.shooting is None:
                continue
            shooting = fact.shooting
            games = fact.games_played
            rows.append({
                "SHOT_TYPE": _SHOT_TYPE_PROFILE_LABELS[shooting_type],
                "FGA_FREQUENCY": _fraction(fact.share),
                "FGM": _per_game(shooting.makes, games),
                "FGA": _per_game(fact.volume, games),
                "FG_PCT": _made_rate(shooting.makes, fact.volume),
                "FG2A_FREQUENCY": _fraction(shooting.two_point_share),
                "FG2M": _per_game(shooting.two_point_makes, games),
                "FG2A": _per_game(shooting.two_point_attempts, games),
                "FG2_PCT": _made_rate(
                    shooting.two_point_makes, shooting.two_point_attempts
                ),
                "FG3A_FREQUENCY": _fraction(shooting.three_point_share),
                "FG3M": _per_game(shooting.three_point_makes, games),
                "FG3A": _per_game(shooting.three_point_attempts, games),
                "FG3_PCT": _made_rate(
                    shooting.three_point_makes, shooting.three_point_attempts
                ),
            })
        return rows

    def _get_archetype_gamelogs(self, player_name, opp_team):
        """Get archetype gamelogs for player against specific team"""
        try:
            # Get player's cluster members
            player_ids = self._get_archetype_players_from_player(player_name)

            # Get team ID
            team_dict = pd.DataFrame(self._get_teams())
            team_id = team_dict.loc[team_dict['full_name'] == opp_team, 'id'].values[0]

            gl = self._archetype_frame(player_ids, int(team_id))
            if gl.empty:
                return []

            per36_df = self._per36_frame()

            # Calculate per 36minute stats
            for col in _ARCHETYPE_RATE_COLUMNS:
                gl[f'{col}/36MIN'] = (gl[col] / gl['MIN']) * 36

            logger.debug("Archetype game logs rows: %s", len(gl))
            merged_df = gl.merge(per36_df, left_on="PLAYER_ID", right_on="PLAYER_ID", suffixes=('', '_season'))
            # Every column on this tab is a percentage difference against a
            # season baseline, so a row whose baseline is zero or missing has
            # no comparison to make.  It is omitted rather than published with
            # an unreportable cell: the client renders every returned row as a
            # number, so a null would read as "no change" and an infinity is
            # not JSON.  A player with no usable baseline yields no rows.
            usable = merged_df[
                [f'{col}_season' for col in _ARCHETYPE_RATE_COLUMNS]
            ].apply(pd.to_numeric, errors='coerce')
            merged_df = merged_df[
                ((usable > 0) & np.isfinite(usable)).all(axis=1)
            ].copy()
            if merged_df.empty:
                return []
            #get percentage diff between game and season
            for col in _ARCHETYPE_RATE_COLUMNS:
                season = merged_df[f'{col}_season']
                merged_df[f'{col}/36MIN_DIFF'] = (
                    merged_df[f'{col}/36MIN'] - season
                ) / season

            merged_df = merged_df[['PLAYER_NAME', 'GAME_DATE', 'MIN',
                    'FGM', 'FGA', 'FG3M', 'FG3A', 'FTM', 'FTA', 'PTS', 'TOV','FGM/36MIN', 'FGA/36MIN', 'FG3M/36MIN', 'FG3A/36MIN',
                               'FTM/36MIN', 'FTA/36MIN', 'PTS/36MIN',
                               'FGM/36MIN_DIFF', 'FGA/36MIN_DIFF', 'FG3M/36MIN_DIFF', 'FG3A/36MIN_DIFF',
                               'FTM/36MIN_DIFF', 'FTA/36MIN_DIFF', 'PTS/36MIN_DIFF', 'TOV/36MIN_DIFF']]

            return merged_df.to_dict(orient='records')
        except Exception as e:
            logger.error("Error getting archetype gamelogs: %s", e)
            return []

    def _archetype_frame(self, player_ids, opponent_team_id: int):
        """Read the cluster's stored regular-season rows against one opponent.

        The rows come from the governed player game-log publication through
        the same indexed opponent projection the matchup cards read, so the
        tab observes exactly the facts the rest of the app does.
        """

        columns = ['PLAYER_NAME', 'PLAYER_ID', 'GAME_DATE', 'MIN', *_ARCHETYPE_RATE_COLUMNS]
        if not player_ids or self.game_logs is None:
            return pd.DataFrame(columns=columns)
        season = self.settings.nba.current_season
        records = self.game_logs.list_archetype_rows(
            season,
            player_ids,
            opponent_team_id,
            publication_snapshot=self.game_logs.read_publication_snapshot(season),
        )
        return pd.DataFrame(
            [
                {
                    'PLAYER_NAME': record.player_name,
                    'PLAYER_ID': record.player_id,
                    # The tab has always shown a date string and whole
                    # minutes; the stored facts are typed, so both are
                    # rendered back to that contract here.
                    'GAME_DATE': record.game_date.isoformat(),
                    'MIN': int(round(record.minutes)),
                    'FGM': record.field_goals_made,
                    'FGA': record.field_goals_attempted,
                    'FG3M': record.three_pointers_made,
                    'FG3A': record.three_pointers_attempted,
                    'FTM': record.free_throws_made,
                    'FTA': record.free_throws_attempted,
                    'PTS': record.points,
                    'TOV': record.turnovers,
                }
                for record in records
                if record.season_type == REGULAR_SEASON_TYPE
                and round(record.minutes) > 0
            ],
            columns=columns,
        )

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
