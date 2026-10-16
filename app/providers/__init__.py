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
from .novig import NovigAdapter
from .prizepicks import PrizePicksAdapter
from .underdog import UnderdogAdapter
from .kalshi import KalshiAdapter
from .nba_injury_report import NBAInjuryReportProvider

__all__ = [
    "DabbleAdapter",
    "NBAStatsAdapter",
    "NBAStatsProvider",
    "PBPStatsAdapter",
    "NovigAdapter",
    "PBPStatsProvider",
    "PrizePicksAdapter",
    "UnderdogAdapter",
    "KalshiAdapter",
    "NBAInjuryReportProvider",
    "normalize_archetype_game_logs",
    "normalize_player_roster",
    "normalize_player_game_logs",
]
