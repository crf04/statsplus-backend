from app.services.nl_query.parser import BaseQueryParser
from app.services.nl_query.executor import QueryExecutor
from app.services.llm_service import LLMService
import logging

logger = logging.getLogger(__name__)

class NLService:
    def __init__(self, engine):
        self.engine = engine
        self.nl_parser = None
        self.query_executor = None
        self.llm_service = None
        self.initialize_nl_system()
    
    def initialize_nl_system(self):
        """Initialize the natural language query system with LLM fallback"""
        try:
            self.nl_parser = BaseQueryParser(self.engine)
            self.query_executor = QueryExecutor(self.engine)
            
            # Initialize LLM service for fallback
            try:
                self.llm_service = LLMService()
                logger.info("✅ LLM Service initialized for fallback routing")
            except Exception as llm_error:
                logger.warning(f"⚠️  LLM Service initialization failed: {llm_error}")
                logger.warning("   Will continue with NLP-only mode")
                self.llm_service = None
            
            print("✅ Natural Language Query System initialized successfully")
        except Exception as e:
            print(f"❌ Failed to initialize NL Query System: {e}")
    
    def process_query(self, query):
        """Process natural language query with hybrid NLP+LLM routing"""
        if not query or not query.strip():
            raise ValueError("Empty query provided")
        
        # Check if NL system is initialized
        if not self.nl_parser or not self.query_executor:
            raise RuntimeError("Natural language system not initialized")
        
        query_text = query.strip()
        
        # Step 1: Parse with NLP first
        parsed_components = self.nl_parser.parse(query_text)
        
        # Step 2: Check if LLM fallback is needed
        should_use_llm = (
            parsed_components.confidence_breakdown and 
            parsed_components.confidence_breakdown.should_use_llm
        )
        
        if should_use_llm and self.llm_service:
            logger.info(f"🧠 Routing to LLM (confidence: {parsed_components.confidence:.3f}): {query_text[:50]}...")
            try:
                # Extract player context from NLP result
                player_context = self._extract_player_context(parsed_components)
                
                # Route to LLM with player context for hybrid processing
                llm_result = self._process_with_llm(query_text, parsed_components, player_context)
                if llm_result:
                    return llm_result
                else:
                    logger.warning("⚠️  LLM processing failed, falling back to NLP result")
            except Exception as e:
                logger.error(f"❌ LLM processing error: {e}")
                logger.info("   Falling back to NLP result")
        else:
            logger.info(f"⚡ Using NLP (confidence: {parsed_components.confidence:.3f}): {query_text[:50]}...")
        
        # Step 3: Use NLP result (fast path or fallback)
        return self._format_nlp_result(parsed_components, query_text)
    
    def _process_with_llm(self, query_text: str, nlp_fallback, player_context=None):
        """Process query with LLM using player context for hybrid processing"""
        try:
            # Use enhanced prompt with player context for hybrid processing
            llm_response = self.llm_service.test_prompt_with_context(
                "prompts/system_prompt_optimized.txt", 
                query_text,
                player_context or {}
            )
            
            if not llm_response.get("success", False):
                logger.warning(f"LLM parsing failed: {llm_response.get('error', 'Unknown error')}")
                return None
            
            llm_content = llm_response.get("content", {})
            
            # Create hybrid result by merging LLM output with NLP player context
            return self._create_hybrid_result(llm_content, query_text, nlp_fallback, player_context)
            
        except Exception as e:
            logger.error(f"LLM processing exception: {e}")
            return None
    
    def _format_llm_result(self, llm_content: dict, query_text: str, nlp_fallback):
        """Format LLM response to match frontend expectations"""
        try:
            # Convert opponent_filters from LLM format [[stat, rank], ...] to frontend format
            teams_against = []
            rank_filter = []
            
            opponent_filters = llm_content.get("opponent_filters", [])
            if opponent_filters:
                for filter_item in opponent_filters:
                    if isinstance(filter_item, list) and len(filter_item) >= 2:
                        stat, rank = filter_item[0], filter_item[1]
                        teams_against.append(stat)
                        rank_filter.append(str(rank))
            
            # Convert self_filters from LLM format to frontend format
            self_filters = llm_content.get("self_filters", [])
            
            # Extract minutes filter if present
            minutes_filter = None
            for sf in self_filters:
                if isinstance(sf, dict) and sf.get("stat_column") in ["MIN", "minutes"]:
                    if sf.get("operator") == "between":
                        minutes_filter = [sf.get("value", 0), sf.get("value2", 48)]
                        # Remove from self_filters since it's handled separately
                        self_filters = [f for f in self_filters if f != sf]
                    break
            
            result = {
                'player_name': llm_content.get('player_name'),
                'team_name': llm_content.get('team_name'),
                'game_count': llm_content.get('game_count'),
                'location': llm_content.get('location'),
                'players_on': llm_content.get('players_on', []),
                'players_off': llm_content.get('players_off', []),
                'teams_against': teams_against,
                'minutes_filter': minutes_filter,
                'date_filter': llm_content.get('date_range'),
                'self_filters': self_filters,
                'rank_filter': rank_filter,
                'season': llm_content.get('season', '2024-25'),
                'confidence': llm_content.get('confidence', 0.9),  # LLM typically has high confidence
                'intent': llm_content.get('intent', 'game_logs'),
                'time_period': llm_content.get('time_period'),
                'original_query': query_text,
                'parsed_by': 'llm'  # Flag to indicate LLM was used
            }
            
            logger.info(f"✅ LLM successfully parsed query with {result['confidence']:.3f} confidence")
            return result
            
        except Exception as e:
            logger.error(f"LLM result formatting error: {e}")
            return None
    
    def _format_nlp_result(self, parsed_components, query_text: str):
        """Format NLP parser result to frontend expectations"""
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
            'date_filter': parsed_components.date_range,
            'self_filters': parsed_components.self_filters,
            'rank_filter': rank_filter,      
            'season': getattr(parsed_components, 'season', '2024-25'),
            'confidence': parsed_components.confidence,
            'intent': parsed_components.intent,
            'time_period': parsed_components.time_period,
            'original_query': query_text,
            'parsed_by': 'nlp'  # Flag to indicate NLP was used
        }
        
        return result
    
    def _extract_player_context(self, parsed_components):
        """Extract player context from NLP result to pass to LLM"""
        player_context = {}
        
        # Extract main player if found with reasonable confidence
        if parsed_components.player_name:
            player_context['player_name'] = parsed_components.player_name
        
        # Extract players on court if found
        if parsed_components.players_on:
            player_context['players_on'] = parsed_components.players_on
            
        # Extract players off court if found  
        if parsed_components.players_off:
            player_context['players_off'] = parsed_components.players_off
            
        logger.info(f"📋 Extracted player context: {player_context}")
        return player_context
    
    def _create_hybrid_result(self, llm_content: dict, query_text: str, nlp_fallback, player_context: dict):
        """Create hybrid result by intelligently merging LLM output with NLP player context"""
        try:
            # Start with LLM result as base
            hybrid_result = self._format_llm_result(llm_content, query_text, nlp_fallback)
            
            if not hybrid_result:
                return None
                
            # Apply selective override logic based on confidence thresholds
            if player_context:
                hybrid_result = self._apply_selective_overrides(hybrid_result, llm_content, player_context)
            
            # Mark as hybrid processing
            hybrid_result['parsed_by'] = 'hybrid'
            hybrid_result['player_context_used'] = player_context
            
            logger.info(f"🔄 Created hybrid result with confidence: {hybrid_result.get('confidence', 0):.3f}")
            return hybrid_result
            
        except Exception as e:
            logger.error(f"Hybrid result creation error: {e}")
            return None
    
    def _apply_selective_overrides(self, llm_result: dict, llm_content: dict, player_context: dict):
        """Apply selective override logic with confidence thresholds"""
        
        # Never override player_name if NLP found it (preserve nickname resolution)
        if player_context.get('player_name'):
            llm_result['player_name'] = player_context['player_name']
            logger.info(f"🔒 Preserved NLP player name: {player_context['player_name']}")
        
        # Check if LLM tried to override player info and validate confidence
        llm_confidence = llm_content.get('confidence', 0)
        
        # For other components, only override if LLM confidence >= 0.75
        override_threshold = 0.75
        player_override_threshold = 0.95
        
        # Handle players_on and players_off with high threshold for overrides
        for field in ['players_on', 'players_off']:
            if player_context.get(field) and llm_result.get(field):
                if llm_confidence >= player_override_threshold:
                    logger.info(f"⚠️  LLM overriding {field} (confidence: {llm_confidence:.3f})")
                else:
                    llm_result[field] = player_context[field]
                    logger.info(f"🔒 Preserved NLP {field}: {player_context[field]}")
        
        return llm_result 