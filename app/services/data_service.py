"""Data refresh service that collects provider frames before publication.

Every mutating refresh follows two phases.  First all provider calls and
transformations run and produce ``DataFrames`` without touching any live
table, so a provider or transformation failure cannot corrupt the read path.
Then the collected frames are published together through
:class:`AtomicTablePublisher`, which swaps the whole set inside one
transaction and preserves the previous tables if anything fails.
"""

import logging
from collections.abc import Callable

import pandas as pd
from nba_api.stats.static import players, teams

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.errors import InvalidInputError
from app.models.catalogs import (
    PBPDataKind,
    PBP_DATA_KINDS,
    PLAY_TYPES,
    SHOOTING_TYPES,
)
from app.services.nba_stats_adapter import NBAStatsAdapter
from app.services.pbp_stats_adapter import PBPTotalsAdapter
from app.services.table_publisher import AtomicTablePublisher
from app.utils.performance_monitor import monitor_nba_api_calls

logger = logging.getLogger(__name__)


class DataService:
    def __init__(self, db_engine, settings: RuntimeSettings | None = None):
        self.engine = db_engine
        self.settings = settings or get_runtime_settings()
        self.publisher = AtomicTablePublisher(db_engine)
        self.pbp = PBPTotalsAdapter(settings=self.settings)
        self.nba_stats = NBAStatsAdapter(settings=self.settings)

    def update_all_data(self, progress_callback: Callable | None = None):
        """Fetch every provider frame, then publish the full set atomically.

        Provider and transformation failures happen before any table is
        touched, so an interrupted refresh leaves the previous tables intact.
        """
        self._report_progress(progress_callback, 0.05, "Fetching provider data")
        frames = self._collect_all_frames()
        self._report_progress(progress_callback, 0.75, "Transforming provider data")
        self._report_progress(progress_callback, 0.9, "Publishing refreshed tables")
        self.publisher.publish(frames)
        self._report_progress(progress_callback, 1.0, "Completed")
        return True

    @monitor_nba_api_calls
    def fetch_PBP_data(
        self,
        data_type: PBPDataKind = "player",
        progress_callback: Callable | None = None,
    ):
        """Fetch one PBP totals set and publish it under its live table name."""
        self._validate_pbp_data_type(data_type)
        table_name = f"pbp_{data_type}_stats"
        self._report_progress(progress_callback, 0.1, "Fetching PBP totals")
        frame = self._collect_pbp_frame(data_type)
        self._report_progress(progress_callback, 0.75, "Transforming PBP totals")
        self._report_progress(progress_callback, 0.9, "Publishing PBP totals")
        self.publisher.publish({table_name: frame})
        self._report_progress(progress_callback, 1.0, "Completed")
        return True

    def fetch_players_with_teams(self, progress_callback: Callable | None = None):
        """Fetch teams, map players to them, and publish both tables at once."""
        self._report_progress(progress_callback, 0.1, "Fetching players and teams")
        frames = self._collect_players_with_teams()
        self._report_progress(progress_callback, 0.75, "Transforming player-team mapping")
        records = frames["player_team_table"].to_dict(orient="records")
        self._report_progress(progress_callback, 0.9, "Publishing player-team mapping")
        self.publisher.publish(frames)
        self._report_progress(progress_callback, 1.0, "Completed")
        return records

    @staticmethod
    def _report_progress(
        progress_callback: Callable | None,
        progress: float,
        note: str,
    ) -> None:
        if progress_callback is not None:
            progress_callback(progress, note)

    def get_playtypes(self):
        combined_df = pd.DataFrame()

        for play_type in PLAY_TYPES:
            df = self.nba_stats.fetch_synergy_play_types(
                play_type,
                player_or_team_abbreviation="T",
                type_grouping="Defensive",
            )
            df["PLAY_TYPE"] = play_type
            df["PTS/G"] = df["PTS"] / df["GP"]
            combined_df = pd.concat([combined_df, df], ignore_index=True)

        pts_pivot = combined_df.pivot_table(
            index="TEAM_NAME",
            columns="PLAY_TYPE",
            values="PTS/G",
            aggfunc="first",
        ).reset_index()

        gp_per_team = combined_df.groupby("TEAM_NAME")["GP"].first().reset_index()

        teams_df = pts_pivot.merge(gp_per_team, on="TEAM_NAME")
        return teams_df.to_dict(orient="records")

    def _collect_all_frames(self):
        """Gather every refreshable table without touching the live tables."""
        frames = {
            "player_information": self._collect_player_information(),
            "general_opponent_stats": self._fetch_opponent_data(),
            "player_per36_stats": self._fetch_player_per36_stats(),
            "team_play_types": self._collect_team_play_types(),
        }
        frames.update(self._collect_opp_shooting())
        frames["opp_shooting_zone"] = self._collect_opp_shooting_zone()
        frames["player_play_types"] = self._collect_playtypes_frame()
        frames["player_shooting_zones"] = self._collect_player_zone()

        pbp_player = self._collect_pbp_frame("player")
        pbp_opponent = self._collect_pbp_frame("opponent")
        frames["pbp_player_stats"] = pbp_player
        frames["pbp_opponent_stats"] = pbp_opponent
        frames.update(self._collect_assist_frames(pbp_player, pbp_opponent))
        return frames

    def _collect_player_information(self):
        """Build the active-player frame without writing to any table."""
        player_dict = players.get_active_players()
        return pd.DataFrame.from_dict(player_dict)

    def _collect_opp_shooting(self):
        """Build the three opponent shooting-type frames."""
        types_table_names = [
            (shooting_type, shooting_type.replace(" ", "_").lower())
            for shooting_type in SHOOTING_TYPES
        ]
        frames = {}
        for play_type, table_name in types_table_names:
            df = self._fetch_opp_shooting_data(play_type)
            df["FG3M_RANK"] = df["FG3M"].rank(method="min", ascending=True)
            df["FG2M_RANK"] = df["FG2M"].rank(method="min", ascending=True)
            df["FG2A_RANK"] = df["FG2A"].rank(method="min", ascending=True)
            df["FG3A_RANK"] = df["FG3A"].rank(method="min", ascending=True)
            frames[table_name] = df
        return frames

    def _collect_opp_shooting_zone(self):
        """Build the opponent shooting-zone frame."""
        opp_zone_df = self._fetch_opp_shooting_zone_data()
        opp_zone_df.columns = [
            "_".join(filter(None, col)).strip() for col in opp_zone_df.columns
        ]

        columns = [
            column
            for column in opp_zone_df.columns
            if "OPP" in column and "PCT" not in column and "Backcourt" not in column
        ]
        for column in columns:
            opp_zone_df[f"{column}_RANK"] = opp_zone_df[column].rank(
                method="min", ascending=True
            )
        return opp_zone_df

    def _collect_team_play_types(self):
        """Build the team play-type frame."""
        playtypes = PLAY_TYPES
        team_dfs = []

        for play_type in playtypes:
            df = self._fetch_team_play_type_data(play_type)
            df["PTS/G"] = df["PTS"] / df["GP"]
            team_dfs.append(df)

        combined_team_df = pd.concat(team_dfs, ignore_index=True)

        mean_pts_per_game = {
            play_type: combined_team_df[
                combined_team_df["PLAY_TYPE"] == play_type
            ]["PTS/G"].mean()
            for play_type in playtypes
        }
        for play_type in playtypes:
            combined_team_df.loc[
                combined_team_df["PLAY_TYPE"] == play_type, "PTS/G+"
            ] = combined_team_df["PTS/G"] / mean_pts_per_game[play_type]

        teams_df = combined_team_df.pivot_table(
            index="TEAM_NAME",
            columns="PLAY_TYPE",
            values="PTS/G+",
            aggfunc="first",
        ).reset_index()

        nba_teams = teams.get_teams()
        teams_df.loc[
            teams_df["TEAM_NAME"] == "LA Clippers", "TEAM_NAME"
        ] = "Los Angeles Clippers"
        team_ids = {team["full_name"]: team["id"] for team in nba_teams}
        teams_df["Team_ID"] = teams_df["TEAM_NAME"].map(team_ids)
        teams_df["team"] = teams_df["TEAM_NAME"].apply(
            self._nba_team_to_abbreviation
        )

        new_order = [
            "TEAM_NAME",
            "Cut",
            "Isolation",
            "PRRollMan",
            "PRBallHandler",
            "OffRebound",
            "Spotup",
            "Handoff",
            "OffScreen",
            "Misc",
            "Postup",
            "Transition",
            "Team_ID",
            "team",
        ]
        return teams_df[new_order]

    def _collect_playtypes_frame(self):
        """Build the player play-type frame."""
        playtypes = PLAY_TYPES
        dfs = []

        for play_type in playtypes:
            df = self._fetch_play_type_data(play_type)
            dfs.append(df)

        combined_df = pd.concat(dfs, ignore_index=True)

        pivot_df = combined_df.pivot_table(
            index="PLAYER_NAME",
            columns="PLAY_TYPE",
            values="PTS",
            aggfunc="first",
        ).reset_index()

        player_team_df = (
            combined_df.groupby("PLAYER_NAME")["TEAM_ABBREVIATION"]
            .agg("first")
            .reset_index()
        )
        pivot_df = pd.merge(pivot_df, player_team_df, on="PLAYER_NAME", how="left")

        sums = pivot_df.drop(["PLAYER_NAME", "TEAM_ABBREVIATION"], axis=1).sum(axis=1)
        pivot_df["Sum"] = sums

        for play_type in playtypes:
            percentage_column_name = play_type + "%"
            pivot_df[percentage_column_name] = (
                pivot_df[play_type] / pivot_df["Sum"] * 100
            )

        pivot_df.drop(playtypes + ["Sum"], axis=1, inplace=True)
        pivot_df.fillna(0, inplace=True)
        return pivot_df

    def _collect_player_zone(self):
        """Build the player shooting-zone frame."""
        player_zones = self._fetch_player_zone_data()
        player_zones.columns = [
            "_".join(filter(None, column)).strip() for column in player_zones.columns
        ]

        player_zones_columns = [
            column
            for column in player_zones.columns
            if ("FGM" in column or "_NAME" in column) and "Back" not in column
        ]

        for column in player_zones_columns:
            if "NAME" not in column:
                player_zones[column.split("_")[0] + "_PTS"] = (
                    player_zones[column] * 2
                    if "3" not in column
                    else player_zones[column] * 3
                )

        sums = player_zones.drop(
            [
                "PLAYER_NAME",
                "PLAYER_ID",
                "TEAM_ID",
                "TEAM_ABBREVIATION",
                "AGE",
                "NICKNAME",
            ],
            axis=1,
        ).sum(axis=1)
        player_zones["Sum"] = sums

        for column in player_zones_columns:
            if "NAME" not in column:
                percentage_column_name = column.split("_")[0] + "_PTS%"
                player_zones[percentage_column_name] = (
                    player_zones[column.split("_")[0] + "_PTS"]
                    / player_zones["Sum"]
                    * 100
                )

        means = player_zones.drop(
            [
                "PLAYER_NAME",
                "PLAYER_ID",
                "TEAM_ID",
                "TEAM_ABBREVIATION",
                "AGE",
                "NICKNAME",
            ],
            axis=1,
        ).mean()
        cols = [column for column in player_zones.columns if "PTS%" in column]

        for column in cols:
            if means[column] != 0:
                player_zones[f"{column}+"] = player_zones[column] / means[column]
            else:
                player_zones[f"{column}+"] = 0

        player_zones.drop(
            [column for column in player_zones.columns if "Backcourt" in column],
            axis=1,
            inplace=True,
        )
        player_zones.drop(
            [
                "PLAYER_ID",
                "TEAM_ID",
                "TEAM_ABBREVIATION",
                "AGE",
                "NICKNAME",
                "Sum",
            ],
            axis=1,
            inplace=True,
        )
        player_zones.fillna(0, inplace=True)
        return player_zones

    def _collect_assist_frames(self, pbp_player_df, pbp_opponent_df):
        """Build the processed assist tables from in-memory PBP frames."""
        team_columns = [
            "Name",
            "Assists",
            "AssistPoints",
            "TwoPtAssists",
            "ThreePtAssists",
            "Arc3Assists",
            "Corner3Assists",
            "AtRimAssists",
            "ShortMidRangeAssists",
            "LongMidRangeAssists",
        ]
        teams_df = pbp_opponent_df[team_columns].copy()

        stat_columns = team_columns[1:]
        means = teams_df[stat_columns].mean()
        for column in stat_columns:
            teams_df[column] = teams_df[column] / means[column]
            teams_df[f"{column}_RANK"] = teams_df[column].rank(
                method="min", ascending=True
            )

        player_columns = [
            "Name",
            "TwoPtAssists",
            "ThreePtAssists",
            "Arc3Assists",
            "Corner3Assists",
            "AtRimAssists",
            "ShortMidRangeAssists",
            "LongMidRangeAssists",
        ]
        players_df = pbp_player_df[player_columns].copy()

        location_cats = [
            "Arc3Assists",
            "Corner3Assists",
            "AtRimAssists",
            "ShortMidRangeAssists",
            "LongMidRangeAssists",
        ]
        location_sum = players_df.drop(
            ["Name", "TwoPtAssists", "ThreePtAssists"], axis=1
        ).sum(axis=1)
        for category in location_cats:
            players_df[category] = players_df[category] / location_sum * 100

        type_sum = players_df.drop(["Name"] + location_cats, axis=1).sum(axis=1)
        players_df["TwoPtAssists"] = players_df["TwoPtAssists"] / type_sum * 100
        players_df["ThreePtAssists"] = players_df["ThreePtAssists"] / type_sum * 100

        averages = players_df.drop("Name", axis=1).mean()
        for column in players_df.columns:
            if column != "Name":
                players_df[f"{column}+"] = players_df[column] / averages[column]

        return {
            "processed_team_assists": teams_df,
            "processed_player_assists": players_df,
        }

    def _collect_pbp_frame(self, data_type: PBPDataKind = "player"):
        """Fetch one PBP frame without writing anything yet."""
        self._validate_pbp_data_type(data_type)
        return self.pbp.fetch_totals_frame(data_type)

    def _collect_players_with_teams(self):
        """Build the team_info and player_team_table frames together."""
        raw_teams = pd.DataFrame(teams.get_teams())
        mapped_teams = raw_teams.copy()
        mapped_teams["full_name"] = mapped_teams["full_name"].str.replace(
            "76ers", "Sixers"
        )

        df = self._fetch_data_from_table("player_team_table")
        df = df.iloc[:-1]
        df["Team_ID"] = df["Current Team"].map(
            mapped_teams.set_index("full_name")["id"]
        )
        return {"team_info": raw_teams, "player_team_table": df}

    # Helper methods
    def _validate_pbp_data_type(self, data_type):
        if data_type not in PBP_DATA_KINDS:
            raise InvalidInputError(
                f"Unsupported PBP data type {data_type!r}. "
                f"Expected one of {sorted(PBP_DATA_KINDS)}."
            )

    def _fetch_opponent_data(self, date_filter=None):
        return self.nba_stats.fetch_opponent_team_stats(
            date_filter,
            per_mode_detailed="Per48",
            league_id="00",
        )

    def _fetch_opp_shooting_data(self, play_type, date_filter=None):
        return self.nba_stats.fetch_opponent_shot_chart(
            play_type,
            date_filter,
            per_mode_simple="PerGame",
            league_id="00",
        )

    def _fetch_opp_shooting_zone_data(self, date_filter=None):
        return self.nba_stats.fetch_opponent_shooting_zone(
            date_filter,
            per_mode_detailed="PerGame",
            league_id="00",
        )

    def _fetch_player_zone_data(self, date_filter=None):
        return self.nba_stats.fetch_player_shooting_zone(
            date_filter,
            per_mode_detailed="PerGame",
        )

    def _fetch_team_play_type_data(self, play_type):
        return self.nba_stats.fetch_synergy_play_types(
            play_type,
            player_or_team_abbreviation="T",
            type_grouping="Defensive",
        )

    def _fetch_play_type_data(self, play_type):
        return self.nba_stats.fetch_synergy_play_types(
            play_type,
            player_or_team_abbreviation="P",
            type_grouping="Offensive",
        )

    def _fetch_player_per36_stats(self):
        return self.nba_stats.fetch_player_per36_stats()

    def _fetch_data_from_table(self, table_name):
        from ..utils.tables import normalize_table_name

        normalized = normalize_table_name(table_name)
        query = f"SELECT * FROM {normalized}"
        with self.engine.connect() as conn:
            return pd.read_sql(query, conn)

    @staticmethod
    def _nba_team_to_abbreviation(team_name):
        nba_teams = teams.get_teams()
        team_abbr_map = {
            team["full_name"]: team["abbreviation"] for team in nba_teams
        }
        return team_abbr_map.get(team_name, "Unknown")
