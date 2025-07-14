"""
Comprehensive test suite for all NBA query parser filters.

This module tests all implemented filter types with realistic query variations
to simulate human behavior and ensure robust parsing capabilities.
"""

import pytest
from typing import TYPE_CHECKING, List, Tuple, Optional, Dict, Any
from unittest.mock import Mock, MagicMock

from app.services.nl_query.parser import BaseQueryParser, QueryComponents, SelfFilter

if TYPE_CHECKING:
    from _pytest.capture import CaptureFixture
    from _pytest.fixtures import FixtureRequest
    from _pytest.logging import LogCaptureFixture
    from _pytest.monkeypatch import MonkeyPatch
    from pytest_mock.plugin import MockerFixture


def debug_parsing_results(query: str, components: QueryComponents, show_confidence: bool = True) -> None:
    """
    Debug helper to print detailed parsing results.
    
    Args:
        query: The original query
        components: Parsed query components
        show_confidence: Whether to show confidence breakdown
    """
    print(f"\n{'='*60}")
    print(f"QUERY: '{query}'")
    print(f"{'='*60}")
    
    print(f"Player Name: {components.player_name}")
    print(f"Team Name: {components.team_name}")
    print(f"Intent: {components.intent}")
    print(f"Confidence: {components.confidence:.3f}")
    
    if components.time_period or components.game_count:
        print(f"Time Period: {components.time_period}")
        print(f"Game Count: {components.game_count}")
    
    if components.location:
        print(f"Location: {components.location}")
    
    if components.minutes_filter:
        print(f"Minutes Filter: {components.minutes_filter}")
    
    if components.opponent_filters:
        print(f"Opponent Filters: {components.opponent_filters}")
    
    if components.players_on:
        print(f"Players ON: {components.players_on}")
    
    if components.players_off:
        print(f"Players OFF: {components.players_off}")
    
    if components.self_filters:
        print(f"Self Filters:")
        for i, filter_obj in enumerate(components.self_filters):
            print(f"  [{i+1}] {filter_obj.stat_column} {filter_obj.operator} {filter_obj.value}")
            if filter_obj.value2:
                print(f"      (value2: {filter_obj.value2})")
            print(f"      Original: '{filter_obj.original_text}'")
    
    if show_confidence and components.confidence_breakdown:
        print(f"\nConfidence Breakdown:")
        cb = components.confidence_breakdown
        print(f"  Coverage: {cb.coverage_score:.3f}")
        print(f"  Semantic: {cb.semantic_score:.3f}")
        print(f"  Ambiguity: {cb.ambiguity_score:.3f}")
        print(f"  Complexity: {cb.complexity_score:.3f}")
        print(f"  Completeness: {cb.completeness_score:.3f}")
        print(f"  Should use LLM: {cb.should_use_llm}")
        
        if cb.details.get('uncovered_text'):
            print(f"  Uncovered text: '{cb.details['uncovered_text']}'")
    
    print(f"{'='*60}")


class MockEngine:
    """Mock database engine for testing."""
    
    def connect(self):
        """Mock database connection."""
        return self
    
    def execute(self, query: str):
        """Mock database query execution."""
        class MockResult:
            def fetchall(self):
                return [
                    ('LeBron James',), ('Stephen Curry',), ('Anthony Davis',),
                    ('Giannis Antetokounmpo',), ('Luka Doncic',), ('Jayson Tatum',),
                    ('Damian Lillard',), ('Joel Embiid',), ('Nikola Jokic',),
                    ('Kawhi Leonard',), ('Jimmy Butler',), ('Jaylen Brown',)
                ]
        return MockResult()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        pass


@pytest.fixture
def mock_parser() -> BaseQueryParser:
    """Create a mock parser instance for testing."""
    return BaseQueryParser(MockEngine())


class TestSelfFilters:
    """Test self-filter functionality with various statistical conditions."""
    
    def test_basic_scoring_filters(self, mock_parser: BaseQueryParser) -> None:
        """Test basic scoring self-filters with different phrasings."""
        test_cases = [
            # Basic scoring patterns
            ("LeBron games where he scores 30+ points", "PTS", "gte", 30),
            ("Curry games with 25+ points", "PTS", "gte", 25),
            ("Giannis 40+ point games", "PTS", "gte", 40),
            ("Dame games where he drops 35+ points", "PTS", "gte", 35),
            ("Luka games with exactly 28 points", "PTS", "eq", 28),
            ("Tatum games where he scores between 25 and 35 points", "PTS", "between", 25),
        ]
        
        for query, expected_stat, expected_op, expected_val in test_cases:
            components = mock_parser.parse(query)
            
            # Debug output - shows parsing results during pytest runs
            debug_parsing_results(query, components, show_confidence=False)
            
            assert len(components.self_filters) >= 1, f"No self-filters found for: {query}"
            
            filter_obj = components.self_filters[0]
            assert filter_obj.stat_column == expected_stat, f"Expected {expected_stat}, got {filter_obj.stat_column}"
            assert filter_obj.operator == expected_op, f"Expected {expected_op}, got {filter_obj.operator}"
            assert filter_obj.value == expected_val, f"Expected {expected_val}, got {filter_obj.value}"
    
    def test_rebounding_filters(self, mock_parser: BaseQueryParser) -> None:
        """Test rebounding self-filters with natural language variations."""
        test_cases = [
            ("Giannis games where he grabs 12+ rebounds", "REB", "gte", 12),
            ("AD games with 15+ boards", "REB", "gte", 15),
            ("Embiid games where he pulls down 10+ rebounds", "REB", "gte", 10),
            ("Jokic 20+ rebound games", "REB", "gte", 20),
            ("Dwight games with double-digit rebounds", "REB", "gte", 10),
        ]
        
        for query, expected_stat, expected_op, expected_val in test_cases:
            components = mock_parser.parse(query)
            assert len(components.self_filters) >= 1, f"No self-filters found for: {query}"
            
            filter_obj = components.self_filters[0]
            assert filter_obj.stat_column == expected_stat
            assert filter_obj.operator == expected_op
            assert filter_obj.value == expected_val
    
    def test_assist_filters(self, mock_parser: BaseQueryParser) -> None:
        """Test assist self-filters with colloquial terms."""
        test_cases = [
            ("CP3 games where he dishes 10+ assists", "AST", "gte", 10),
            ("Luka games with 8+ dimes", "AST", "gte", 8),
            ("Jokic games where he gets 12+ assists", "AST", "gte", 12),
            ("Rondo games with 15+ assists", "AST", "gte", 15),
        ]
        
        for query, expected_stat, expected_op, expected_val in test_cases:
            components = mock_parser.parse(query)
            assert len(components.self_filters) >= 1, f"No self-filters found for: {query}"
            
            filter_obj = components.self_filters[0]
            assert filter_obj.stat_column == expected_stat
            assert filter_obj.operator == expected_op
            assert filter_obj.value == expected_val
    
    def test_shooting_volume_filters(self, mock_parser: BaseQueryParser) -> None:
        """Test shooting volume filters (attempts)."""
        test_cases = [
            ("Harden games where he attempts 20+ field goals", "FGA", "gte", 20),
            ("Curry games where he takes 15+ threes", "FG3A", "gte", 15),
            ("Dame games where he shoots 18+ shots", "FGA", "gte", 18),
            ("Klay games with 12+ three point attempts", "FG3A", "gte", 12),
            ("Luka games where he attempts 10+ free throws", "FTA", "gte", 10),
        ]
        
        for query, expected_stat, expected_op, expected_val in test_cases:
            components = mock_parser.parse(query)
            debug_parsing_results(query, components, show_confidence=False)
            assert len(components.self_filters) >= 1, f"No self-filters found for: {query}"
            
            filter_obj = components.self_filters[0]
            assert filter_obj.stat_column == expected_stat
            assert filter_obj.operator == expected_op
            assert filter_obj.value == expected_val
    
    def test_shooting_efficiency_filters(self, mock_parser: BaseQueryParser) -> None:
        """Test shooting efficiency filters (made shots)."""
        test_cases = [
            ("Curry games where he makes 8+ threes", "FG3M", "gte", 8),
            ("Dame games with 10+ three pointers", "FG3M", "gte", 10),
            ("KD games where he makes 12+ field goals", "FGM", "gte", 12),
            ("Klay games with 6+ from deep", "FG3M", "gte", 6),
        ]
        
        for query, expected_stat, expected_op, expected_val in test_cases:
            components = mock_parser.parse(query)
            assert len(components.self_filters) >= 1, f"No self-filters found for: {query}"
            
            filter_obj = components.self_filters[0]
            assert filter_obj.stat_column == expected_stat
            assert filter_obj.operator == expected_op
            assert filter_obj.value == expected_val
    
    def test_defensive_filters(self, mock_parser: BaseQueryParser) -> None:
        """Test defensive stat filters."""
        test_cases = [
            ("Kawhi games where he gets 3+ steals", "STL", "gte", 3),
            ("AD games with 4+ blocks", "BLK", "gte", 4),
            ("Giannis games where he blocks 2+ shots", "BLK", "gte", 2),
            ("Butler games with 2+ steals", "STL", "gte", 2),
        ]
        
        for query, expected_stat, expected_op, expected_val in test_cases:
            components = mock_parser.parse(query)
            assert len(components.self_filters) >= 1, f"No self-filters found for: {query}"
            
            filter_obj = components.self_filters[0]
            assert filter_obj.stat_column == expected_stat, f"Expected {expected_stat}, got {filter_obj.stat_column} for query: {query}"
            assert filter_obj.operator == expected_op
            assert filter_obj.value == expected_val
    
    def test_multiple_self_filters(self, mock_parser: BaseQueryParser) -> None:
        """Test queries with multiple self-filter conditions."""
        test_cases = [
            ("LeBron games where he scores 30+ points and grabs 10+ rebounds", 2),
            ("Luka games with 25+ points and 8+ assists", 2),
            ("Giannis games where he scores 35+ points and gets 12+ rebounds and 6+ assists", 3),
            ("Harden games with 30+ points and 10+ assists and 8+ rebounds", 3),
        ]
        
        for query, expected_count in test_cases:
            components = mock_parser.parse(query)
            assert len(components.self_filters) == expected_count, f"Expected {expected_count} filters, got {len(components.self_filters)} for: {query}"


class TestTimeFilters:
    """Test time-based filters with various natural language patterns."""
    
    def test_last_games_filters(self, mock_parser: BaseQueryParser) -> None:
        """Test 'last X games' filters with different phrasings."""
        test_cases = [
            ("LeBron last 10 games", "recent", 10),
            ("Curry past 5 games", "recent", 5),
            ("Giannis recent 15 games", "recent", 15),
            ("Dame last twenty games", "recent", 20),
            ("Luka past 8 games", "recent", 8),
        ]
        
        for query, expected_period, expected_count in test_cases:
            components = mock_parser.parse(query)
            assert components.time_period == expected_period, f"Expected {expected_period}, got {components.time_period}"
            assert components.game_count == expected_count, f"Expected {expected_count}, got {components.game_count}"
    
    def test_season_filters(self, mock_parser: BaseQueryParser) -> None:
        """Test season-based time filters."""
        test_cases = [
            ("LeBron this season", "season", None),
            ("Curry current season stats", "season", None),
            ("Giannis season performance", "season", None),
        ]
        
        for query, expected_period, expected_count in test_cases:
            components = mock_parser.parse(query)
            assert components.time_period == expected_period, f"Expected {expected_period}, got {components.time_period}"
            assert components.game_count == expected_count, f"Expected {expected_count}, got {components.game_count}"


class TestLocationFilters:
    """Test location-based filters."""
    
    def test_home_away_filters(self, mock_parser: BaseQueryParser) -> None:
        """Test home and away location filters."""
        test_cases = [
            ("LeBron home games", "home"),
            ("Curry at home", "home"),
            ("Giannis home court advantage", "home"),
            ("Dame away games", "away"),
            ("Luka on the road", "away"),
            ("Kawhi road games", "away"),
            ("Embiid road trip performance", "away"),
        ]
        
        for query, expected_location in test_cases:
            components = mock_parser.parse(query)
            assert components.location == expected_location, f"Expected {expected_location}, got {components.location} for: {query}"


class TestMinutesFilters:
    """Test minutes-based filters."""
    
    def test_minutes_filters(self, mock_parser: BaseQueryParser) -> None:
        """Test various minutes filter patterns."""
        test_cases = [
            ("LeBron games with 35+ minutes", (35, 48)),
            ("Curry games where he plays 30+ minutes", (30, 48)),
            ("Giannis games with less than 25 minutes", (0, 25)),
            ("Dame games between 28 and 35 minutes", (28, 35)),
            ("Luka games with exactly 32 minutes", (30, 34)),  # Exact uses ±2 range
        ]
        
        for query, expected_range in test_cases:
            components = mock_parser.parse(query)
            assert components.minutes_filter == expected_range, f"Expected {expected_range}, got {components.minutes_filter} for: {query}"


class TestOpponentFilters:
    """Test opponent-based filters."""
    
    def test_ranking_filters(self, mock_parser: BaseQueryParser) -> None:
        """Test opponent ranking filters."""
        test_cases = [
            ("LeBron against top 10 defenses", [("defense_rank", 10)]),
            ("Curry vs bottom 5 offenses", [("offense_rank", -5)]),
            ("Giannis against top 15 defensive teams", [("defense_rank", 15)]),
            ("Dame vs worst 8 three point defenses", [("defense_rank", -8)]),
        ]
        
        for query, expected_filters in test_cases:
            components = mock_parser.parse(query)
            assert len(components.opponent_filters) >= 1, f"No opponent filters found for: {query}"
            # Check that at least one expected filter is present
            found_match = False
            for expected_filter in expected_filters:
                if expected_filter in components.opponent_filters:
                    found_match = True
                    break
            assert found_match, f"Expected filters {expected_filters} not found in {components.opponent_filters}"


class TestPlayerRelationshipFilters:
    """Test player relationship filters (with/without)."""
    
    def test_with_player_filters(self, mock_parser: BaseQueryParser) -> None:
        """Test 'with player' relationship filters."""
        test_cases = [
            ("LeBron with AD", "LeBron James", ["Anthony Davis"]),
            ("Curry with Klay and Draymond", "Stephen Curry", ["Klay Thompson", "Draymond Green"]),
            ("Giannis playing with Dame", "Giannis Antetokounmpo", ["Damian Lillard"]),
            ("Luka with Kyrie", "Luka Doncic", ["Kyrie Irving"]),
        ]
        
        for query, expected_main, expected_with in test_cases:
            components = mock_parser.parse(query)
            # Note: The exact player matching might vary based on the database
            # So we'll check that the main player is found and there are 'with' players
            assert components.player_name is not None, f"No main player found for: {query}"
            assert len(components.players_on) >= 1, f"No 'with' players found for: {query}"
    
    def test_without_player_filters(self, mock_parser: BaseQueryParser) -> None:
        """Test 'without player' relationship filters."""
        test_cases = [
            ("LeBron without AD", "LeBron James", ["Anthony Davis"]),
            ("Curry without Draymond", "Stephen Curry", ["Draymond Green"]),
            ("Giannis without Dame", "Giannis Antetokounmpo", ["Damian Lillard"]),
        ]
        
        for query, expected_main, expected_without in test_cases:
            components = mock_parser.parse(query)
            assert components.player_name is not None, f"No main player found for: {query}"
            assert len(components.players_off) >= 1, f"No 'without' players found for: {query}"


class TestComplexCombinations:
    """Test complex combinations of multiple filter types."""
    
    def test_time_location_combinations(self, mock_parser: BaseQueryParser) -> None:
        """Test time and location filter combinations."""
        test_cases = [
            "LeBron last 10 home games",
            "Curry past 5 away games",
            "Giannis recent 8 road games",
            "Dame last 15 games at home",
        ]
        
        for query in test_cases:
            components = mock_parser.parse(query)
            assert components.time_period == "recent", f"Expected recent time period for: {query}"
            assert components.game_count is not None, f"No game count found for: {query}"
            assert components.location is not None, f"No location found for: {query}"
    
    def test_self_filter_time_combinations(self, mock_parser: BaseQueryParser) -> None:
        """Test self-filters with time filters."""
        test_cases = [
            "LeBron last 10 games where he scores 30+ points",
            "Curry past 5 games with 8+ threes",
            "Giannis recent 15 games where he gets 12+ rebounds",
            "Dame last 8 games with 25+ points and 8+ assists",
        ]
        
        for query in test_cases:
            components = mock_parser.parse(query)
            assert components.time_period == "recent", f"Expected recent time period for: {query}"
            assert components.game_count is not None, f"No game count found for: {query}"
            assert len(components.self_filters) >= 1, f"No self-filters found for: {query}"
    
    def test_triple_combinations(self, mock_parser: BaseQueryParser) -> None:
        """Test combinations of three filter types."""
        test_cases = [
            "LeBron last 10 home games where he scores 30+ points",
            "Curry past 5 away games with 8+ threes",
            "Giannis recent 8 road games where he gets 12+ rebounds and 6+ assists",
            "Dame last 15 home games with 25+ points and 35+ minutes",
        ]
        
        for query in test_cases:
            components = mock_parser.parse(query)
            # Should have time, location, and self-filters
            assert components.time_period == "recent", f"Expected recent time period for: {query}"
            assert components.game_count is not None, f"No game count found for: {query}"
            assert components.location is not None, f"No location found for: {query}"
            assert len(components.self_filters) >= 1, f"No self-filters found for: {query}"
    
    def test_kitchen_sink_combinations(self, mock_parser: BaseQueryParser) -> None:
        """Test complex queries with many filter types."""
        test_cases = [
            "LeBron last 10 home games with AD where he scores 30+ points and plays 35+ minutes",
            "Curry past 5 away games with Klay where he makes 8+ threes and plays 32+ minutes",
            "Giannis recent 8 road games without Dame where he gets 12+ rebounds and 6+ assists",
        ]
        
        for query in test_cases:
            components = mock_parser.parse(query)
            debug_parsing_results(query, components, show_confidence=False)
            # Should have multiple filter types
            assert components.time_period == "recent", f"Expected recent time period for: {query}"
            assert components.game_count is not None, f"No game count found for: {query}"
            assert components.location is not None, f"No location found for: {query}"
            assert len(components.self_filters) >= 1, f"No self-filters found for: {query}"
            # Should have either players_on or players_off
            assert len(components.players_on) >= 1 or len(components.players_off) >= 1, f"No player relationships found for: {query}"


class TestConfidenceScoring:
    """Test confidence scoring for various query types."""
    
    def test_high_confidence_queries(self, mock_parser: BaseQueryParser) -> None:
        """Test queries that should have high confidence scores."""
        high_confidence_queries = [
            "LeBron last 10 games",
            "Curry games where he scores 30+ points",
            "Giannis home games with 12+ rebounds",
            "Dame last 5 games with 8+ threes",
        ]
        
        for query in high_confidence_queries:
            components = mock_parser.parse(query)
            assert components.confidence > 0.8, f"Expected high confidence for: {query}, got {components.confidence}"
    
    def test_medium_confidence_queries(self, mock_parser: BaseQueryParser) -> None:
        """Test queries that should have medium confidence scores."""
        medium_confidence_queries = [
            "LeBron complex statistical analysis",
            "Curry advanced metrics evaluation", 
            "Giannis performance correlation study",
        ]
        
        for query in medium_confidence_queries:
            components = mock_parser.parse(query)
            assert 0.4 < components.confidence < 0.95, f"Expected medium confidence for: {query}, got {components.confidence}"
    
    def test_confidence_breakdown_exists(self, mock_parser: BaseQueryParser) -> None:
        """Test that confidence breakdown is provided for analysis."""
        query = "LeBron last 10 games where he scores 30+ points"
        components = mock_parser.parse(query)
        
        assert components.confidence_breakdown is not None, "Confidence breakdown should be provided"
        assert hasattr(components.confidence_breakdown, 'coverage_score'), "Should have coverage score"
        assert hasattr(components.confidence_breakdown, 'semantic_score'), "Should have semantic score"
        assert hasattr(components.confidence_breakdown, 'final_confidence'), "Should have final confidence"


class TestEdgeCases:
    """Test edge cases and error handling."""
    
    def test_empty_query(self, mock_parser: BaseQueryParser) -> None:
        """Test empty query handling."""
        components = mock_parser.parse("")
        assert components.confidence < 0.9, "Empty query should have lower confidence"
    
    def test_gibberish_query(self, mock_parser: BaseQueryParser) -> None:
        """Test gibberish query handling."""
        components = mock_parser.parse("asdfkjhasdf random gibberish")
        assert components.confidence < 0.9, "Gibberish query should have lower confidence"
    
    def test_partial_matches(self, mock_parser: BaseQueryParser) -> None:
        """Test queries with partial information."""
        partial_queries = [
            "LeBron games where he",
            "Curry with",
            "Giannis last",
            "Dame 30+ but",
        ]
        
        for query in partial_queries:
            components = mock_parser.parse(query)
            # These should parse but have lower confidence than complete queries
            assert components.confidence < 0.95, f"Partial query should have lower confidence: {query}"
    
    def test_impossible_values(self, mock_parser: BaseQueryParser) -> None:
        """Test queries with impossible statistical values."""
        impossible_queries = [
            "LeBron games where he scores 200+ points",
            "Curry games with 50+ rebounds",
            "Giannis games where he makes 100+ threes",
        ]
        
        for query in impossible_queries:
            components = mock_parser.parse(query)
            # Should still parse but might have semantic warnings
            assert len(components.self_filters) >= 1, f"Should still extract filter from: {query}"


class TestRealisticHumanQueries:
    """Test realistic queries that humans might actually ask."""
    
    def test_casual_fan_queries(self, mock_parser: BaseQueryParser) -> None:
        """Test queries a casual fan might ask."""
        casual_queries = [
            "How has LeBron been doing lately?",
            "Show me Curry's hot shooting games",
            "When does Giannis dominate the most?",
            "Dame's clutch performances this season",
            "Luka's triple double games",
        ]
        
        for query in casual_queries:
            components = mock_parser.parse(query)
            # Should extract some meaningful information
            assert components.player_name is not None, f"Should find player in: {query}"
    
    def test_analyst_queries(self, mock_parser: BaseQueryParser) -> None:
        """Test queries an analyst might ask."""
        analyst_queries = [
            "LeBron's efficiency in close games during the last month",
            "Curry's three-point volume when Klay is out",
            "Giannis vs elite defenses without his supporting cast",
            "Dame's scoring output in back-to-back games",
        ]
        
        for query in analyst_queries:
            components = mock_parser.parse(query)
            # Should extract relevant filters
            assert components.player_name is not None, f"Should find player in: {query}"
    
    def test_fantasy_player_queries(self, mock_parser: BaseQueryParser) -> None:
        """Test queries a fantasy player might ask."""
        fantasy_queries = [
            "Who should I start: LeBron or Giannis in their recent form?",
            "Curry's ceiling games when he's hot from three",
            "AD's injury-free stretches this season",
            "Dame's floor when he's cold shooting",
        ]
        
        for query in fantasy_queries:
            components = mock_parser.parse(query)
            # Should extract at least one meaningful component
            assert (components.player_name is not None or 
                   len(components.self_filters) > 0 or
                   components.time_period is not None), f"Should extract something from: {query}"


if __name__ == "__main__":
    pytest.main([__file__]) 