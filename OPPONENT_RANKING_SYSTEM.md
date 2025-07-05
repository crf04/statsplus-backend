# NBA Backend - Opponent Team Ranking Filter System

## Overview

Your NBA backend already has a sophisticated opponent team ranking filter system that allows filtering game logs based on opponent team rankings across multiple statistical categories. This system uses the `teams_against` and `rank_filter` parameters to provide powerful analytical capabilities.

## Current Implementation

### Schema Structure

```python
# From app/config/query_schemas.py
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
}
```

### Filter Categories

#### 1. **Overall Opponent Stats**
- `OPP_PTS` - Points allowed per game
- `OPP_REB` - Rebounds allowed per game
- `OPP_AST` - Assists allowed per game
- `OPP_STOCKS` - Steals + Blocks allowed per game
- `OPP_FTA` - Free throw attempts allowed per game
- `OPP_TOV` - Turnovers forced per game
- `OPP_BLK` - Blocks per game
- `OPP_STL` - Steals per game
- `OPP_FG3M` - 3-pointers allowed per game
- `OPP_FG3A` - 3-point attempts allowed per game

#### 2. **Catch & Shoot Opponent Stats**
- `C&S 3s` - Catch & shoot 3-pointers allowed
- `C&S PTS` - Catch & shoot points allowed
- `C&S 3A` - Catch & shoot 3-point attempts allowed

#### 3. **Pullup Opponent Stats**
- `PU 2s` - Pullup 2-pointers allowed
- `PU 3s` - Pullup 3-pointers allowed
- `PU PTS` - Pullup points allowed

#### 4. **Playtype Opponent Stats**
- `Transition` - Transition plays allowed
- `Isolation` - Isolation plays allowed
- `PRBallHandler` - Pick & roll ball handler plays allowed
- `PRRollMan` - Pick & roll roll man plays allowed
- `OffRebound` - Offensive rebound plays allowed
- `Spotup` - Spot up plays allowed
- `Cut` - Cutting plays allowed
- `Handoff` - Handoff plays allowed
- `OffScreen` - Off screen plays allowed
- `Misc` - Miscellaneous plays allowed
- `Postup` - Post up plays allowed

### Ranking System

The `rank_filter` parameter uses a positive/negative system:

- **Positive values** (e.g., `10`) = **Top N teams** (best performance)
- **Negative values** (e.g., `-5`) = **Bottom N teams** (worst performance)

### Natural Language Mapping

```python
# From app/config/filter_mappings.py
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
    "turnovers": {
        "api_filters": ["OPP_TOV"],
        "keywords": ["turnover", "turnovers", "ball security"],
        "ranking_direction": "descending",
        "description": "Teams that force more turnovers"
    }
}
```

## How It Works

### 1. **Query Processing Flow**

```python
# Natural language query: "LeBron against top 10 defenses"
# Gets parsed to:
{
    "player_name": "LeBron James",
    "opponent_filters": [("OPP_PTS", 10)]
}

# Maps to API parameters:
{
    "teams_against": ["OPP_PTS"],
    "rank_filter": ["10"]
}
```

### 2. **Filter Teams Function**

```python
def filter_teams(self, filter, rank_filter, date_filter = None):
    # Categorizes filter type and fetches appropriate data
    if filter in overall_opp_types:
        df = self.general_opp_filtering(filter, date_filter)
    elif filter in Catch_Shoot_types:
        df = self.catch_shoot_filtering(filter, date_filter)
    elif filter in Pullup_types:
        df = self.pullup_filtering(filter,date_filter)
    elif filter in playtypes:
        df = self.playtype_filtering(filter)
    
    # Apply ranking filter
    if rank_filter >= 0:
        return df.head(rank_filter)['team'].tolist()  # Top N teams
    else:
        return df.tail(-rank_filter)['team'].tolist()  # Bottom N teams
```

### 3. **Multiple Filter Intersection**

```python
# Handles multiple opponent filters with intersection logic
teams_against = None
for index, ele in enumerate(filter_params['teams_against']):
    filtered_teams = set(self.filter_teams(ele, int(filter_params['rank_filter'][index]), filter_params['date_filter']))
    
    if teams_against is None:
        teams_against = filtered_teams
    else:
        teams_against = teams_against.intersection(filtered_teams)
```

## Usage Examples

### 1. **Basic Opponent Ranking**

```python
# Query: "LeBron against top 10 defenses"
{
    "player_name": "LeBron James",
    "teams_against": ["OPP_PTS"],
    "rank_filter": ["10"]
}
```

### 2. **Multiple Criteria**

```python
# Query: "Steph against top 5 three point defenses and bottom 10 rebounding teams"
{
    "player_name": "Stephen Curry",
    "teams_against": ["C&S 3s", "OPP_REB"],
    "rank_filter": ["5", "-10"]
}
```

### 3. **Playtype Filtering**

```python
# Query: "Luka against teams that allow most isolation plays"
{
    "player_name": "Luka Doncic",
    "teams_against": ["Isolation"],
    "rank_filter": ["10"]
}
```

## Current Capabilities

✅ **What Works Well:**
- Comprehensive opponent statistical categories
- Positive/negative ranking system
- Multiple filter intersection
- Natural language mapping
- Date-based filtering support
- Integration with existing game log filters

## Potential Enhancements

### 1. **Enhanced Natural Language Patterns**

```python
# Current: Limited pattern recognition
# Suggested: Add more patterns to parser.py

OPPONENT_PATTERNS = {
    r"against\s+top\s+(\d+)\s+(defense|defensive|defenses)": ("OPP_PTS", "positive"),
    r"against\s+worst\s+(\d+)\s+(defense|defensive|defenses)": ("OPP_PTS", "negative"),
    r"against\s+top\s+(\d+)\s+three\s+point\s+defenses": ("C&S 3s", "positive"),
    r"against\s+teams\s+that\s+allow\s+most\s+(\w+)": ("mapping_required", "negative"),
    r"vs\s+elite\s+(defense|rebounding|assists)": ("mapping_required", "positive"),
    r"against\s+bad\s+(defense|rebounding|assists)": ("mapping_required", "negative")
}
```

### 2. **Percentile-Based Filtering**

```python
# Current: Fixed rankings (top 10, bottom 5)
# Suggested: Add percentile support

"rank_filter_type": {
    "type": "str",
    "options": ["count", "percentile"],
    "description": "Whether rank_filter is count-based or percentile-based"
}

# Example: "against top 25% of defenses" -> percentile mode
```

### 3. **Advanced Stat Combinations**

```python
# Current: Individual stat filtering
# Suggested: Add composite stats

COMPOSITE_FILTERS = {
    "elite_defense": ["OPP_PTS", "OPP_FG_PCT", "OPP_EFG_PCT"],
    "pace_teams": ["Transition", "PACE"],
    "three_point_heavy": ["C&S 3A", "PU 3A", "OPP_FG3A"]
}
```

### 4. **Dynamic Ranking Context**

```python
# Current: Season-long rankings
# Suggested: Add contextual rankings

"ranking_context": {
    "type": "str",
    "options": ["season", "last_10_games", "last_month", "since_date"],
    "description": "Time period for ranking calculation"
}
```

### 5. **Range-Based Filtering**

```python
# Current: Top/bottom N teams
# Suggested: Add range support

"rank_range": {
    "type": "tuple",
    "description": "Range of rankings (e.g., teams ranked 5-15)",
    "example": [5, 15]
}
```

## Integration with Natural Language Parser

### Current Integration Points

1. **Parser Extraction** - `_extract_opponent_filters()` method
2. **Parameter Mapping** - `_map_params_to_schema()` method
3. **API Execution** - `GameService.get_filtered_logs()` method

### Suggested Parser Enhancements

```python
def _extract_opponent_filters(self, query: str) -> List[Tuple[str, int]]:
    """Enhanced opponent filter extraction with better pattern matching"""
    filters = []
    query_lower = query.lower()
    
    # Enhanced patterns for better recognition
    patterns = [
        r'against\s+(top|best|elite)\s+(\d+)\s+(defense|defensive|defenses)',
        r'against\s+(worst|bad|bottom)\s+(\d+)\s+(defense|defensive|defenses)',
        r'vs\s+(top|best)\s+(\d+)\s+three\s+point\s+defenses',
        r'against\s+teams\s+that\s+allow\s+(most|least)\s+(\w+)',
        r'vs\s+(elite|good|bad|poor)\s+(\w+)\s+teams'
    ]
    
    for pattern in patterns:
        matches = re.finditer(pattern, query_lower)
        for match in matches:
            # Enhanced logic to map natural language to filters
            # ... implementation details
    
    return filters
```

## Performance Considerations

- **Database Queries**: Each filter type queries different tables/endpoints
- **Caching**: Consider caching team rankings for performance
- **Intersection Logic**: Multiple filters use set intersection (efficient)
- **Memory Usage**: Rankings are calculated in-memory after data fetch

## Testing Recommendations

```python
# Test cases for opponent ranking system
test_cases = [
    "LeBron against top 10 defenses",
    "Steph vs worst 5 three point defenses", 
    "Luka against teams that allow most isolation plays",
    "Giannis vs elite defenses last 10 games",
    "KD against bottom 5 rebounding teams this month"
]
```

## Conclusion

Your opponent team ranking system is already quite sophisticated and well-designed. The main opportunities for improvement are:

1. **Enhanced natural language parsing** for more intuitive queries
2. **Percentile-based filtering** for more flexible ranking options
3. **Composite stat filtering** for advanced analytics
4. **Dynamic ranking contexts** for recency-based analysis

The current system provides a solid foundation that can handle complex opponent-based filtering with good performance and flexibility. 