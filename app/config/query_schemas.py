"""
Schema definitions for existing API endpoints

This file maps the existing API structure to enable natural language processing.
Based on analysis of the existing GameService.get_filtered_logs method.
"""

ENDPOINT_SCHEMAS = {
    "game_logs": {
        "service": "GameService",
        "method": "get_filtered_logs",
        "description": "Get filtered game logs for a player",
        "required_params": ["player_name"],
        "optional_params": {
            "minutes_filter": {
                "type": "tuple",
                "default": (0, 48),
                "description": "Min and max minutes played filter"
            },
            "game_filter": {
                "type": "int",
                "description": "Number of recent games to analyze",
                "example": 10
            },
            "location_filter": {
                "type": "str",
                "options": ["Home", "Away", "Both"],
                "default": "Both",
                "description": "Home/away game filter"
            },
            "teams_against": {
                "type": "list",
                "description": "List of team filter types (maps to filter_teams function)",
                "valid_filters": [
                    "OPP_PTS", "OPP_REB", "OPP_AST", "OPP_STOCKS", "OPP_FTA", "OPP_TOV", "OPP_BLK", "OPP_STL", "OPP_FG3M", "OPP_FG3A", "OPP_FTA",
                    "C&S 3s", "C&S PTS", "C&S 3A", "PU 2s", "PU 3s", "PU PTS",
                    "Transition", "Isolation", "PRBallHandler", "PRRollMan", "OffRebound",
                    "Spotup", "Cut", "Handoff", "OffScreen", "Misc", "Postup"
                ]
            },
            "rank_filter": {
                "type": "list",
                "description": "Ranking numbers corresponding to teams_against filters",
                "example": ["10", "-5"]
            },
            "date_filter": {
                "type": "str",
                "description": "Start date filter in YYYY-MM-DD format",
                "example": "2024-01-01"
            },
            "season_filter": {
                "type": "str",
                "default": "2025-26",
                "description": "NBA season in YYYY-YY format",
                "example": "2025-26"
            },
            "players_on": {
                "type": "list",
                "description": "Players that must be playing in filtered games"
            },
            "players_off": {
                "type": "list", 
                "description": "Players that must NOT be playing in filtered games"
            },
            "self_filters": {
                "type": "dict",
                "description": "Custom statistical filters on player's own stats, e.g. 'points > 10' or 'rebounds > 5'"
            }
        }
    },
    
    "player_profile": {
        "service": "PlayerService",
        "method": "get_player_profile",
        "description": "Get player profile data by category",
        "required_params": ["player_name"],
        "optional_params": {
            "category": {
                "type": "str",
                "options": ["Playtypes", "assists", "Archetype", "Shooting Type", "Zone Shooting"],
                "description": "Type of profile data to retrieve"
            },
            "opp_team": {
                "type": "str",
                "description": "Opponent team for archetype analysis"
            }
        }
    },
    
    "team_stats": {
        "service": "TeamService", 
        "method": "get_team_stats",
        "description": "Get team statistics by category",
        "required_params": ["team", "category"],
        "optional_params": {
            "date": {
                "type": "str",
                "description": "Date filter for team stats"
            }
        }
    }
}

# Intent classification patterns
INTENT_PATTERNS = {
    "game_logs": [
        r"(\w+(?:\s+\w+)?)\s+(?:last|past|recent)\s+\d+\s+games?",
        r"(\w+(?:\s+\w+)?)\s+(?:this\s+)?season",
        r"(\w+(?:\s+\w+)?)\s+against\s+",
        r"(\w+(?:\s+\w+)?)\s+(?:stats|performance|averages)",
        r"(\w+(?:\s+\w+)?)\s+(?:at\s+home|on\s+the\s+road|home|away)"
    ],
    "player_profile": [
        r"(\w+(?:\s+\w+)?)\s+(?:playstyle|archetype|playing\s+style)",
        r"(\w+(?:\s+\w+)?)\s+assists?\s+(?:profile|breakdown|location)",
        r"how\s+does\s+(\w+(?:\s+\w+)?)\s+(?:play|shoot)",
        r"(\w+(?:\s+\w+)?)\s+shooting\s+(?:zones|areas|type)"
    ],
    "team_stats": [
        r"(\w+(?:\s+\w+)?)\s+(?:team\s+)?(?:defense|defensive)",
        r"(\w+(?:\s+\w+)?)\s+(?:team\s+)?(?:offense|offensive)",
        r"(\w+(?:\s+\w+)?)\s+team\s+stats"
    ]
} 