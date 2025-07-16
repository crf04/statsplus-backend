from app.services.nl_query.parser import BaseQueryParser
from app.services.nl_query.executor import QueryExecutor

class NLService:
    def __init__(self, engine):
        self.engine = engine
        self.nl_parser = None
        self.query_executor = None
        self.initialize_nl_system()
    
    def initialize_nl_system(self):
        """Initialize the natural language query system"""
        try:
            self.nl_parser = BaseQueryParser(self.engine)
            self.query_executor = QueryExecutor(self.engine)
            print("✅ Natural Language Query System initialized successfully")
        except Exception as e:
            print(f"❌ Failed to initialize NL Query System: {e}")
    
    def process_query(self, query):
        """Process natural language query and return structured results"""
        if not query or not query.strip():
            raise ValueError("Empty query provided")
        
        # Check if NL system is initialized
        if not self.nl_parser or not self.query_executor:
            raise RuntimeError("Natural language system not initialized")
        
        # Parse the query
        parsed_components = self.nl_parser.parse(query.strip()) 
        
        # Convert opponent_filters to frontend-compatible format
        teams_against = []
        rank_filter = []
        
        if parsed_components.opponent_filters:
            for stat, rank in parsed_components.opponent_filters:
                teams_against.append(stat)
                rank_filter.append(str(rank))
        
        # Convert to frontend-compatible format
        result = {
            'player_name': parsed_components.player_name,
            'team_name': parsed_components.team_name,
            'game_count': parsed_components.game_count,
            'location': parsed_components.location,
            'players_on': parsed_components.players_on,
            'players_off': parsed_components.players_off,
            'teams_against': teams_against,  
            'minutes_filter': parsed_components.minutes_filter,
            'date_range': parsed_components.date_range,
            'self_filters': parsed_components.self_filters,
            'rank_filter': rank_filter,      
            'season': getattr(parsed_components, 'season', '2024-25'),
            'confidence': parsed_components.confidence,
            'intent': parsed_components.intent,
            'time_period': parsed_components.time_period,
            'original_query': query.strip()
        }
        
        return result 