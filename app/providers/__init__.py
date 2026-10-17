"""External provider adapters used by the application."""

from .dabble import DabbleAdapter
from .kalshi import KalshiAdapter
from .nba_injury_report import NBAInjuryReportProvider
from .nba_stats import (
    NBAStatsAdapter,
    NBAStatsProvider,
    normalize_archetype_game_logs,
    normalize_player_game_logs,
    normalize_player_roster,
)
from .novig import NovigAdapter
from .pbp_stats import PBPStatsAdapter, PBPStatsProvider
from .prizepicks import PrizePicksAdapter
from .underdog import UnderdogAdapter

__all__ = [
    "DabbleAdapter",
    "KalshiAdapter",
    "NBAInjuryReportProvider",
    "NBAStatsAdapter",
    "NBAStatsProvider",
    "NovigAdapter",
    "PBPStatsAdapter",
    "PBPStatsProvider",
    "PrizePicksAdapter",
    "UnderdogAdapter",
    "normalize_archetype_game_logs",
    "normalize_player_game_logs",
    "normalize_player_roster",
]
