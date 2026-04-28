"""
Parameter mapping implementation

This module maps parsed query components to NBA API parameters and generates
actual API calls that can be executed.
"""

from datetime import datetime, timedelta
from app.config.query_schemas import ENDPOINT_SCHEMAS
from app.config.filter_mappings import SEASON_MAPPINGS

class ParameterMapper:
    """Maps QueryComponents to API call parameters"""
    
    def __init__(self):
        """Initialize the parameter mapper"""
        self.endpoint_schemas = ENDPOINT_SCHEMAS
        self.season_mappings = SEASON_MAPPINGS
        
        # Season mappings
        self.current_season = "2025-26"  # Updated to match route default
        
        # Location mappings
        self.location_mapping = {
            "home": "Home",
            "away": "Away",
            "both": ""
        }
        
        # Opponent filter mappings
        self.opponent_stat_mapping = {
            "OPP_PTS": "opponent_points",
            "OPP_REB": "opponent_rebounds", 
            "OPP_AST": "opponent_assists",
            "OPP_FG_PCT": "opponent_fg_percentage",
            "OPP_3P_PCT": "opponent_three_point_percentage"
        }
    
    def map_to_api_params(self, components):
        """
        Map QueryComponents to API parameters
        
        Args:
            components: QueryComponents object
            
        Returns:
            dict: Complete API mapping with endpoint, parameters, and call info
        """
        # Determine the primary endpoint based on intent
        endpoint = self._determine_endpoint(components)
        
        # Generate parameters for the endpoint
        params = self._generate_parameters(components, endpoint)
        
        # Create the complete API call information
        api_call_info = {
            "endpoint": endpoint,
            "parameters": params,
            "api_calls": self._generate_api_calls(endpoint, params),
            "description": self._generate_description(components),
            "estimated_results": self._estimate_results(components)
        }
        
        return api_call_info
    
    def _determine_endpoint(self, components):
        """Determine the primary NBA API endpoint to use"""
        if components.intent == "player_profile":
            return "player_profile"
        elif components.intent == "team_stats":
            return "team_stats"
        else:
            # Default to game logs for most queries
            return "game_logs"
    
    def _generate_parameters(self, components, endpoint):
        """Generate parameters for the specific endpoint"""
        params = {}
        
        # Map player information
        if components.player_name:
            params["player_name"] = components.player_name
            # In real implementation, this would be converted to player_id
            params["player_id"] = f"PLAYER_ID_FOR_{components.player_name.replace(' ', '_').upper()}"
        
        # Map team information
        if components.team_name:
            params["team_name"] = components.team_name
            params["team_id"] = f"TEAM_ID_FOR_{components.team_name}"
        
        # Map time period
        if components.time_period:
            params.update(self._map_time_period(components.time_period, components.game_count))
        
        # Map location
        if components.location:
            params["location"] = self.location_mapping.get(components.location, "")
        
        # Map opponent filters (keep original format for easier processing)
        if components.opponent_filters:
            params["opponent_filters"] = components.opponent_filters
        
        # Map minutes filter
        if components.minutes_filter:
            params["minutes_filter"] = components.minutes_filter
        
        # Map players on/off court
        if components.players_on:
            params["players_on"] = components.players_on
            
        if components.players_off:
            params["players_off"] = components.players_off
        
        # Set season (detect from query or use default)
        params["season"] = self._detect_season(components.raw_query)
        
        return params
    
    def _map_time_period(self, time_period, game_count):
        """Map time period to API parameters"""
        time_params = {}
        
        if time_period == "recent" and game_count:
            time_params["last_n_games"] = game_count
        elif time_period == "season":
            time_params["season_type"] = "Regular Season"
        elif time_period == "month":
            # Last 30 days
            end_date = datetime.now()
            start_date = end_date - timedelta(days=30)
            time_params["date_from"] = start_date.strftime("%m/%d/%Y")
            time_params["date_to"] = end_date.strftime("%m/%d/%Y")
        
        return time_params
    
    def _map_opponent_filters(self, opponent_filters):
        """Map opponent filters to API parameters"""
        mapped_filters = []
        
        for stat, value in opponent_filters:
            if stat in self.opponent_stat_mapping:
                mapped_filters.append({
                    "stat": self.opponent_stat_mapping[stat],
                    "value": value,
                    "operator": "top" if value > 0 else "bottom"
                })
        
        return mapped_filters
    
    def _detect_season(self, query):
        """Detect specific NBA season from query text"""
        if not query:
            return self.current_season
        
        query_lower = query.lower()
        
        # Check for specific season formats
        for season, synonyms in self.season_mappings.items():
            if any(synonym.lower() in query_lower for synonym in synonyms):
                return season
        
        # Default to current season if no specific season detected
        return self.current_season
    
    def _generate_api_calls(self, endpoint, params):
        """Generate the actual API calls that would be made"""
        api_calls = []
        
        # Use schema-defined services instead of hardcoded NBA API calls
        if endpoint in self.endpoint_schemas:
            schema = self.endpoint_schemas[endpoint]
            service = schema["service"]
            method = schema["method"]
            description = schema["description"]
            
            # Create the primary API call using schema information
            service_module = service.lower().replace('service', '') + '_service'  # GameService -> game_service
            primary_call = {
                "service": f"app.services.{service_module}",
                "method": method,
                "parameters": self._map_params_to_schema(params, endpoint),
                "purpose": description
            }
            
            api_calls.append(primary_call)
        
        else:
            # Fallback for undefined endpoints (shouldn't happen with proper schema)
            fallback_call = {
                "service": "unknown_service",
                "method": "unknown_method", 
                "parameters": params,
                "purpose": f"Handle {endpoint} endpoint"
            }
            api_calls.append(fallback_call)
        
        return api_calls
    
    def _map_params_to_schema(self, params, endpoint):
        """Map parsed parameters to the schema-defined parameter structure"""
        if endpoint not in self.endpoint_schemas:
            return params
            
        mapped_params = {}
        
        # Handle game_logs endpoint mapping
        if endpoint == "game_logs":
            # Map to GameService.get_filtered_logs parameters
            if params.get("player_name"):
                mapped_params["player_name"] = params["player_name"]
            
            if params.get("last_n_games"):
                mapped_params["game_filter"] = params["last_n_games"]
            
            if params.get("location"):
                # Map to location_filter parameter (case-insensitive)
                location_map = {"home": "Home", "away": "Away", "both": "Both"}
                location_key = params["location"].lower()  # Convert to lowercase for lookup
                mapped_params["location_filter"] = location_map.get(location_key, "Both")
            
            if params.get("opponent_filters"):
                # Map to teams_against and rank_filter parameters
                teams_against = []
                rank_filter = []
                
                for stat, rank in params["opponent_filters"]:
                    teams_against.append(stat)
                    rank_filter.append(str(rank))
                
                if teams_against:
                    mapped_params["teams_against"] = teams_against
                    mapped_params["rank_filter"] = rank_filter
            
            # Map players on/off court filters
            if params.get("players_on"):
                mapped_params["players_on"] = params["players_on"]
                
            if params.get("players_off"):
                mapped_params["players_off"] = params["players_off"]
            
            # Map minutes filter
            if params.get("minutes_filter"):
                mapped_params["minutes_filter"] = params["minutes_filter"]
            
            # Map season parameter to season_filter
            if params.get("season"):
                mapped_params["season_filter"] = params["season"]
        
        elif endpoint == "player_profile":
            # Map to PlayerService.get_player_profile parameters
            if params.get("player_name"):
                mapped_params["player_name"] = params["player_name"]
            
            # Default category if not specified
            mapped_params["category"] = "Playtypes"  # Could be made dynamic later
        
        elif endpoint == "team_stats":
            # Map to TeamService.get_team_stats parameters  
            if params.get("team_name"):
                mapped_params["team"] = params["team_name"]
            mapped_params["category"] = "general"  # Default category
        
        return mapped_params
    
    def _generate_description(self, components):
        """Generate a human-readable description of what the query will do"""
        description_parts = []
        
        if components.player_name:
            description_parts.append(f"Get data for {components.player_name}")
        elif components.team_name:
            description_parts.append(f"Get data for {components.team_name}")
        
        if components.time_period == "recent" and components.game_count:
            description_parts.append(f"for the last {components.game_count} games")
        elif components.time_period == "season":
            description_parts.append("for this season")
        elif components.time_period == "month":
            description_parts.append("for the last month")
        
        if components.location:
            description_parts.append(f"in {components.location} games")
        
        if components.opponent_filters:
            filter_descriptions = []
            for stat, value in components.opponent_filters:
                operator = "top" if value > 0 else "worst"
                filter_descriptions.append(f"against {operator} {abs(value)} teams by {stat}")
            description_parts.append(", ".join(filter_descriptions))
        
        if components.minutes_filter:
            min_minutes, max_minutes = components.minutes_filter
            if min_minutes > 0 and max_minutes < 48:
                description_parts.append(f"with {min_minutes}-{max_minutes} minutes played")
            elif min_minutes > 0:
                description_parts.append(f"with {min_minutes}+ minutes played")
            elif max_minutes < 48:
                description_parts.append(f"with less than {max_minutes} minutes played")
        
        if components.players_on:
            description_parts.append(f"playing with {', '.join(components.players_on)}")
            
        if components.players_off:
            description_parts.append(f"playing without {', '.join(components.players_off)}")
        
        return " ".join(description_parts) if description_parts else "Get NBA data"
    
    def _estimate_results(self, components):
        """Estimate what kind of results this query would return"""
        estimates = {
            "data_type": "Unknown",
            "expected_records": "Unknown",
            "processing_time": "Unknown"
        }
        
        if components.intent == "game_logs":
            estimates["data_type"] = "Game-by-game statistics"
            if components.game_count:
                estimates["expected_records"] = f"~{components.game_count} games"
            else:
                estimates["expected_records"] = "~82 games (full season)"
            estimates["processing_time"] = "1-3 seconds"
        
        elif components.intent == "player_profile":
            estimates["data_type"] = "Career statistics and info"
            estimates["expected_records"] = "Career summary + season splits"
            estimates["processing_time"] = "2-4 seconds"
        
        elif components.intent == "team_stats":
            estimates["data_type"] = "Team performance metrics"
            estimates["expected_records"] = "Team splits and rankings"
            estimates["processing_time"] = "1-2 seconds"
        
        return estimates 
