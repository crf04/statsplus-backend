# NBA Query Parser Enhancement Plan
## Supporting All game_logs Endpoint Parameters

### Current Status Analysis

#### ✅ **Already Fully Supported**
- `player_name` - Complete with aliases, last names, relationships
- `minutes_filter` - Complex patterns (30+, 25-35, less than 30, etc.)
- `game_filter` - Supported as `game_count` ("last 10 games")
- `location_filter` - Home/Away/Both mapping
- `players_on` - Multi-player "with" relationships
- `players_off` - Multi-player "without" relationships
- `season_filter` - Basic season detection ("this season", "2024-25")

#### ⚠️ **Partially Supported (Needs Enhancement)**
- `teams_against` - Only basic team abbreviations + top/bottom rankings
- `rank_filter` - Limited ranking patterns

#### ❌ **Not Implemented (Critical Gaps)**
- `self_filters` - **HIGH PRIORITY** - Player stat thresholds
- `date_filter` - **MEDIUM PRIORITY** - Specific date ranges  
- `playstyle_range` - **LOW PRIORITY** - Player archetype filtering

---

## Implementation Plan

### Phase 1: Self Filters (Stat Categories) - **HIGH PRIORITY**

**Goal**: Extract player performance conditions like "when LeBron scores 30+ points"

#### 1.1 Add Self Filters to QueryComponents
```python
# In parser.py QueryComponents
self_filters: Dict[str, Tuple[str, float]] = field(default_factory=dict)
# Format: {"points": (">", 30), "rebounds": (">=", 10), "assists": ("<", 5)}
```

#### 1.2 Create Self Filter Extraction Method
```python
def _extract_self_filters(self, query: str) -> Dict[str, Tuple[str, float]]:
    """
    Extract player performance conditions from query.
    
    Patterns to detect:
    - "when he scores 30+ points" -> {"points": (">=", 30)}
    - "when LeBron gets 10+ rebounds" -> {"rebounds": (">=", 10)}
    - "when Curry makes 5+ threes" -> {"three_pointers_made": (">=", 5)}
    - "when he shoots over 50%" -> {"field_goal_percentage": (">", 0.5)}
    - "double-double games" -> {"points": (">=", 10), "rebounds": (">=", 10)}
    - "triple-double games" -> special case
    """
```

#### 1.3 Stat Category Mapping
```python
STAT_MAPPINGS = {
    # Basic stats
    "points": ["points", "pts", "scored", "scoring"],
    "rebounds": ["rebounds", "rebs", "boards"],
    "assists": ["assists", "asts", "dimes"],
    "steals": ["steals", "stls"],
    "blocks": ["blocks", "blks"],
    
    # Shooting stats  
    "field_goal_percentage": ["fg%", "field goal percentage", "shooting percentage"],
    "three_point_percentage": ["3pt%", "three point percentage", "from three"],
    "free_throw_percentage": ["ft%", "free throw percentage"],
    "three_pointers_made": ["threes", "3pm", "three pointers"],
    
    # Advanced patterns
    "double_double": ["double double", "double-double"],
    "triple_double": ["triple double", "triple-double"],
    "30_point_game": ["30+ points", "30 point game"],
}
```

#### 1.4 Pattern Recognition
```python
# Patterns to implement:
SELF_FILTER_PATTERNS = [
    # Threshold patterns
    (r'(?:when|with|in games where).*?(\d+)\+\s*(points|pts|rebounds|assists)', 'threshold_plus'),
    (r'(?:scores?|gets?|makes?)\s*(\d+)\+\s*(points|rebounds|threes)', 'performance_plus'),
    (r'(?:over|above|more than)\s*(\d+)\s*(points|rebounds|assists)', 'threshold_over'),
    (r'(?:under|below|less than)\s*(\d+)\s*(points|rebounds|assists)', 'threshold_under'),
    
    # Percentage patterns
    (r'(?:shoots?|shooting)\s*(?:over|above)\s*(\d+)%', 'percentage_over'),
    (r'(?:shoots?|shooting)\s*(?:under|below)\s*(\d+)%', 'percentage_under'),
    
    # Special achievements
    (r'(?:double.double|double double)', 'double_double'),
    (r'(?:triple.double|triple double)', 'triple_double'),
    (r'(\d+).(\d+).(\d+)', 'stat_line'),  # "20-10-5" format
]
```

### Phase 2: Enhanced Opponent Filtering - **MEDIUM PRIORITY**

**Goal**: Support full `teams_against` and `rank_filter` capabilities

#### 2.1 Expand Opponent Filter Types
```python
# Current: Only team abbreviations + basic rankings
# Add support for all API filters:
OPPONENT_FILTER_MAPPINGS = {
    # Defensive rankings
    "points allowed": "OPP_PTS",
    "rebounds allowed": "OPP_REB", 
    "assists allowed": "OPP_AST",
    "turnovers forced": "OPP_TOV",
    "steals": "OPP_STL",
    "blocks": "OPP_BLK",
    
    # Offensive rankings
    "three point defense": "OPP_FG3M",
    "free throw attempts allowed": "OPP_FTA",
    
    # Play type filters
    "transition defense": "Transition",
    "isolation defense": "Isolation", 
    "pick and roll defense": "PRBallHandler",
    "post defense": "Postup",
    "spot up defense": "Spotup",
    "cut defense": "Cut",
    "screen defense": "OffScreen",
}
```

#### 2.2 Enhanced Pattern Recognition
```python
# Add patterns like:
# "against top 10 defenses" -> teams_against=["OPP_PTS"], rank_filter=["10"]
# "against bottom 5 three point defenses" -> teams_against=["OPP_FG3M"], rank_filter=["-5"]  
# "against teams allowing 120+ points" -> teams_against=["OPP_PTS"], rank_filter=["120+"]
```

### Phase 3: Date Range Filtering - **MEDIUM PRIORITY**

**Goal**: Support specific date filtering

#### 3.1 Implement Date Range Extraction
```python
def _extract_date_range(self, query: str) -> Optional[str]:
    """
    Extract specific date filters.
    
    Patterns:
    - "since January 1" -> "2024-01-01"  
    - "between January 1 and February 15" -> start date handling
    - "in January" -> month-specific filtering
    - "after the All-Star break" -> event-based dates
    """
```

#### 3.2 Date Pattern Recognition
```python
DATE_PATTERNS = [
    (r'since\s+(\w+\s+\d+)', 'since_date'),
    (r'between\s+(\w+\s+\d+)\s+and\s+(\w+\s+\d+)', 'date_range'),
    (r'in\s+(\w+)', 'month_filter'),
    (r'after\s+all.star\s+break', 'post_allstar'),
    (r'before\s+playoffs', 'pre_playoffs'),
]
```

### Phase 4: Playstyle Range Filtering - **LOW PRIORITY**

**Goal**: Support player archetype/playstyle filtering

#### 4.1 Playstyle Pattern Recognition  
```python
def _extract_playstyle_range(self, query: str) -> Optional[List[int]]:
    """
    Extract playstyle-based filtering.
    
    Patterns:
    - "primary ball handlers" -> specific playstyle range
    - "catch and shoot players" -> specific range
    - "post players" -> specific range
    """
```

---

## Implementation Steps

### Step 1: Update QueryComponents (5 minutes)
```python
@dataclass 
class QueryComponents:
    # ... existing fields ...
    
    # New fields
    self_filters: Dict[str, Tuple[str, float]] = field(default_factory=dict)
    date_filter: Optional[str] = None  # Make this functional
    enhanced_opponent_filters: List[Dict[str, Any]] = field(default_factory=list)
    playstyle_range: Optional[List[int]] = None
```

### Step 2: Implement Self Filters (2-3 hours)
1. Create `_extract_self_filters()` method
2. Add stat category mappings
3. Implement threshold and percentage patterns  
4. Add double-double/triple-double detection
5. Integrate with main parsing pipeline

### Step 3: Enhanced Opponent Filters (1-2 hours)
1. Expand opponent filter mappings
2. Update `_extract_opponent_filters()` method
3. Add complex ranking pattern recognition
4. Map to API format

### Step 4: Date Range Implementation (1 hour)
1. Create `_extract_date_range()` method
2. Add date parsing utilities
3. Handle month/event-based patterns

### Step 5: Integration & Testing (1 hour)
1. Update confidence scoring to account for new parameters
2. Create comprehensive test cases
3. Validate against API parameter format

---

## Test Cases to Implement

### Self Filters
```python
test_cases = [
    "LeBron when he scores 30+ points last 10 games",
    "Curry triple double games this season",  
    "Giannis double-double games at home",
    "Durant shooting over 50% away games",
    "Harden with 10+ assists and 5+ rebounds",
]
```

### Enhanced Opponents
```python
test_cases = [
    "LeBron against top 5 defenses",
    "Curry against teams allowing 120+ points", 
    "Giannis against bottom 10 rebounding teams",
    "Durant against transition defenses",
]
```

### Date Ranges
```python
test_cases = [
    "LeBron since January 1",
    "Curry between January 1 and February 15", 
    "Giannis in January games",
    "Durant after All-Star break",
]
```

---

## Expected Outcomes

### Before Enhancement
- **Supported**: 7/11 endpoint parameters (64%)
- **Stat categories**: Not extractable
- **Complex opponent filters**: Very limited
- **Date ranges**: Not functional

### After Enhancement  
- **Supported**: 11/11 endpoint parameters (100%)
- **Stat categories**: Fully extractable with thresholds
- **Complex opponent filters**: Complete API coverage
- **Date ranges**: Full date filtering support

### Success Metrics
- **Parameter coverage**: 100% of game_logs endpoint
- **Test success rate**: 90%+ on enhanced features
- **Query complexity**: Support 6+ parameter combinations
- **Real-world applicability**: Handle all common basketball analytics queries

This plan transforms the parser from a basic entity extractor to a comprehensive NBA analytics query processor that fully leverages the API's capabilities. 