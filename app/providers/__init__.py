"""External provider adapters used by the application."""

from .dabble import DabbleAdapter
from .nba_stats import (
    NBAStatsAdapter,
    NBAStatsProvider,
    normalize_archetype_game_logs,
    normalize_player_roster,
    normalize_player_game_logs,
)
from .pbp_stats import PBPStatsAdapter, PBPStatsProvider
from .prizepicks import PrizePicksAdapter
from .underdog import UnderdogAdapter
from .rotowire import RotoWireInjuryProvider

__all__ = [
    "DabbleAdapter",
    "NBAStatsAdapter",
    "NBAStatsProvider",
    "PBPStatsAdapter",
    "PBPStatsProvider",
    "PrizePicksAdapter",
    "UnderdogAdapter",
    "RotoWireInjuryProvider",
    "normalize_archetype_game_logs",
    "normalize_player_roster",
    "normalize_player_game_logs",
]
