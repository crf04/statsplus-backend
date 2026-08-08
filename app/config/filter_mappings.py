"""
Mappings between natural language terms and API filter parameters

This file maps natural language descriptions to the existing filter_teams 
function parameters in GameService.
"""

from .settings import current_nba_season

FILTER_MAPPINGS = {
    "defense": {
        "api_filters": ["OPP_PTS"],
        "keywords": ["defense", "defensive", "defenses", "defend", "defensive teams"],
        "ranking_direction": "ascending",
        "description": "Teams that allow fewer points (better defense)"
    },
    "three_point_defense": {
        "api_filters": ["C&S 3s", "C&S 3A", "PU 3s"],
        "keywords": ["three point", "3pt", "perimeter", "catch and shoot", "three point defense"],
        "ranking_direction": "ascending",
        "description": "Teams that allow fewer three-point shots"
    },
    "rebounding": {
        "api_filters": ["OPP_REB"],
        "keywords": ["rebound", "rebounding", "boards", "rebounding teams"],
        "ranking_direction": "ascending",
        "description": "Teams that allow fewer rebounds"
    },
    "turnovers": {
        "api_filters": ["OPP_TOV"],
        "keywords": ["turnover", "turnovers", "ball security"],
        "ranking_direction": "descending", 
        "description": "Teams that force more turnovers"
    },
    "free_throws": {
        "api_filters": ["OPP_FTA"],
        "keywords": ["free throw", "free throws", "foul", "fouls"],
        "ranking_direction": "descending",
        "description": "Teams that allow more free throw attempts"
    }
}

LOCATION_SYNONYMS = {
    "Home": ["home", "at home", "home games"],
    "Away": ["away", "road", "on the road", "away games", "road games"],
    "Both": ["both", "all", "anywhere", "all games"]
}

TIME_PERIOD_SYNONYMS = {
    "recent": ["last", "past", "recent", "latest", "previous"],
    "season": ["this season", "this year", "current season", "season"],
    "month": ["this month", "past month", "last 30 days", "recent month"],
    "week": ["this week", "past week", "last week", "recent week"]
}

# Season format mappings for specific NBA seasons.  Generic "current season"
# phrases are attached to the startup-derived season so this parser cannot
# drift away from the route/service default.

CURRENT_SEASON = current_nba_season()
CURRENT_SEASON_FULL = f"{int(CURRENT_SEASON[:4])}-{int(CURRENT_SEASON[:4]) + 1}"

SEASON_MAPPINGS = {
    CURRENT_SEASON: [
        CURRENT_SEASON,
        CURRENT_SEASON_FULL,
        "current season",
        "this season",
        "this year",
    ],
    "2024-25": ["2024-25", "2024-2025"],
    "2023-24": ["2023-24", "2023-2024", "last season", "previous season"],
    "2022-23": ["2022-23", "2022-2023"],
    "2021-22": ["2021-22", "2021-2022"],
    "2020-21": ["2020-21", "2020-2021"]
}

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "twenty-five": 25, "thirty": 30, "forty": 40, "fifty": 50
}

# Ranking direction mappings
RANKING_TERMS = {
    "positive": ["top", "best", "elite", "good", "strong", "leading"],
    "negative": ["worst", "bad", "bottom", "weak", "poor", "struggling"]
}
