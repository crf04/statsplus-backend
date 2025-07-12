# Minutes Filter Implementation

## Overview

The minutes filter allows users to filter NBA game logs based on the minutes played by a player in specific games. This feature has been implemented across the entire natural language query processing pipeline.

## Features

### Supported Query Patterns

1. **Minimum Minutes (30+ minutes)**
   - `"LeBron James games with 30+ minutes"`
   - `"Stephen Curry when he plays 35 or more minutes"`
   - `"Kevin Durant games with more than 25 minutes"`
   - `"Giannis games with at least 32 minutes"`
   - `"Anthony Davis with minimum 28 minutes"`

2. **Maximum Minutes (less than X minutes)**
   - `"LeBron games with less than 20 minutes"`
   - `"Curry when he plays under 30 minutes"`
   - `"KD games with below 25 minutes"`
   - `"Giannis with maximum 35 minutes"`

3. **Range Minutes (X-Y minutes)**
   - `"LeBron in games with 25-35 minutes"`
   - `"Curry games with 30 to 40 minutes"`
   - `"KD games between 28 and 36 minutes"`

4. **Exact Minutes (±2 minute range)**
   - `"LeBron games with exactly 32 minutes"`
   - `"Curry with 35 minutes"`

5. **Combined with Other Filters**
   - `"LeBron James last 10 games with 30+ minutes at home"`
   - `"Curry with KD when both play 35+ minutes"`

### Supported Keywords

- **Minutes**: `minutes`, `mins`, `min`
- **More than**: `+`, `or more`, `more than`, `over`, `above`, `at least`, `minimum`
- **Less than**: `less than`, `under`, `below`, `maximum`, `max`
- **Range**: `-`, `to`, `between...and`
- **Exact**: `exactly`, or just the number

## Implementation Details

### 1. Parser Changes (`app/services/nl_query/parser.py`)

#### QueryComponents Enhancement
```python
@dataclass
class QueryComponents:
    # ... existing fields ...
    minutes_filter: Optional[Tuple[int, int]] = None  # (min_minutes, max_minutes)
```

#### New Method: `_extract_minutes_filter`
```python
def _extract_minutes_filter(self, query: str) -> Optional[Tuple[int, int]]:
    """Extract minutes filter from natural language query"""
    # Handles patterns like "30+ minutes", "less than 25 minutes", "20-35 minutes"
    # Returns (min_minutes, max_minutes) tuple
```

#### Updated `parse` Method
```python
def parse(self, query: str) -> QueryComponents:
    # ... existing code ...
    components.minutes_filter = self._extract_minutes_filter(query)
    # ... rest of code ...
```

#### Updated Confidence Calculation
```python
def _calculate_confidence(self, components: QueryComponents) -> float:
    # ... existing code ...
    if components.minutes_filter:
        confidence += 0.05
    # ... rest of code ...
```

### 2. Parameter Mapper Changes (`app/services/nl_query/parameter_mapper.py`)

#### Parameter Generation
```python
def _generate_parameters(self, components, endpoint):
    # ... existing code ...
    # Map minutes filter
    if components.minutes_filter:
        params["minutes_filter"] = components.minutes_filter
    # ... rest of code ...
```

#### Schema Mapping
```python
def _map_params_to_schema(self, params, endpoint):
    # ... existing code ...
    # Map minutes filter
    if params.get("minutes_filter"):
        mapped_params["minutes_filter"] = params["minutes_filter"]
    # ... rest of code ...
```

#### Description Generation
```python
def _generate_description(self, components):
    # ... existing code ...
    if components.minutes_filter:
        min_minutes, max_minutes = components.minutes_filter
        if min_minutes > 0 and max_minutes < 48:
            description_parts.append(f"with {min_minutes}-{max_minutes} minutes played")
        elif min_minutes > 0:
            description_parts.append(f"with {min_minutes}+ minutes played")
        elif max_minutes < 48:
            description_parts.append(f"with less than {max_minutes} minutes played")
    # ... rest of code ...
```

### 3. Executor Changes (`app/services/nl_query/executor.py`)

#### Parameter Conversion
```python
def _convert_to_game_service_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
    filter_params = {
        'minutes_filter': (0, 48),  # Default
        # ... other default parameters ...
    }
    
    # ... existing mappings ...
    
    # Handle minutes filter override
    if params.get("minutes_filter"):
        filter_params["minutes_filter"] = params["minutes_filter"]
    
    return filter_params
```

### 4. Game Service Integration

The `GameService` already supported minutes filtering through the `apply_filters` method:

```python
def apply_filters(self, df, filter_params):
    # Apply minutes filter
    if 'minutes_filter' in filter_params:
        min_filter, max_filter = filter_params['minutes_filter']
        df = df[(df['MIN'] >= min_filter) & (df['MIN'] <= max_filter)]
    # ... rest of filters ...
```

## Usage Examples

### Basic Usage
```python
from app.services.nl_query.parser import BaseQueryParser
from app.services.nl_query.parameter_mapper import ParameterMapper
from app.services.nl_query.executor import QueryExecutor

# Initialize components
parser = BaseQueryParser(db_engine)
mapper = ParameterMapper()
executor = QueryExecutor(db_engine)

# Parse a query with minutes filter
query = "LeBron James last 10 games with 30+ minutes"
components = parser.parse(query)

# Map to API parameters
api_info = mapper.map_to_api_params(components)

# Execute the query
results = executor.execute_query(api_info)
```

### Query Examples and Results

1. **"LeBron James games with 30+ minutes"**
   - Parsed: `minutes_filter: (30, 48)`
   - Result: Games where LeBron played 30+ minutes

2. **"Curry with less than 25 minutes"**
   - Parsed: `minutes_filter: (0, 25)`
   - Result: Games where Curry played under 25 minutes

3. **"KD games with 28-35 minutes"**
   - Parsed: `minutes_filter: (28, 35)`
   - Result: Games where KD played between 28-35 minutes

4. **"Giannis with exactly 32 minutes"**
   - Parsed: `minutes_filter: (30, 34)`
   - Result: Games where Giannis played 30-34 minutes (±2 range)

## Technical Notes

### Range Handling
- NBA games have a maximum of 48 minutes (plus overtime)
- Minimum is 0 minutes (DNP - Did Not Play)
- Exact minutes use ±2 minute range for realistic matching
- Range validation ensures min_minutes ≤ max_minutes

### Integration Points
1. **Parser**: Extracts minutes filter from natural language
2. **Parameter Mapper**: Maps to API parameters and generates descriptions
3. **Executor**: Converts to GameService format
4. **GameService**: Applies the actual filter to pandas DataFrame

### Error Handling
- Invalid ranges are ignored
- Non-numeric values are skipped
- Default range (0, 48) is used when no filter is specified

## Testing

The implementation has been tested with:
- Various query patterns and synonyms
- Integration through the complete pipeline
- Edge cases and error conditions
- Combined filters (minutes + location + time period)

All tests pass successfully, confirming the minutes filter works correctly across the entire system. 