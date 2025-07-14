"""
Comprehensive kitchen sink test suite for NBA query parser.

This module contains extensive stress tests with complex multi-filter combinations,
edge cases, real-world scenarios, and potential failure patterns to thoroughly
validate the parser's robustness and accuracy.
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
    print(f"\n{'='*80}")
    print(f"QUERY: '{query}'")
    print(f"{'='*80}")
    
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
    
    print(f"{'='*80}")


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
                    ('Kawhi Leonard',), ('Jimmy Butler',), ('Jaylen Brown',),
                    ('Klay Thompson',), ('Draymond Green',), ('Russell Westbrook',),
                    ('Chris Paul',), ('Kevin Durant',), ('Kyrie Irving',),
                    ('Victor Wembanyama',), ('Anthony Edwards',), ('Paolo Banchero',),
                    ('Scottie Barnes',), ('Franz Wagner',), ('Jalen Green',),
                    ('Alperen Sengun',), ('Josh Giddey',), ('Cade Cunningham',),
                    ('Evan Mobley',), ('Jarrett Allen',), ('Donovan Mitchell',)
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


class TestMultiFilterStressCombinations:
    """Test complex combinations of multiple filter types."""
    
    def test_maximum_complexity_queries(self, mock_parser: BaseQueryParser) -> None:
        """Test queries with maximum complexity using all filter types."""
        max_complexity_queries = [
            # Ultimate complexity: all filter types combined
            "LeBron last 15 home games with AD and Russ but without Dwight Powell where he scores 30+ points and 8+ rebounds and 6+ assists and plays 35+ minutes against top 5 defenses",
            
            # Fantasy basketball scenario
            "Curry past 20 away games with Klay and Draymond where he makes 8+ threes and 25+ points and less than 4 turnovers and plays between 32 and 40 minutes against worst 10 three point defenses",
            
            # Statistical analysis query
            "Giannis recent 12 road games without Dame but with Brook Lopez where he shoots 15+ field goals and makes 10+ free throws and gets double-digit rebounds and 5+ assists with 30+ minutes",
            
            # Complex relationship query
            "Luka last 10 games at home with Kyrie and Dwight Powell but without Tim Hardaway where he scores between 25 and 40 points and attempts 20+ shots and makes 4+ threes with exactly 35 minutes",
            
            # Multi-stat efficiency query
            "Tatum past 8 away games with Brown but without Smart where he scores 25+ points and shoots 12+ field goals and makes 3+ threes and gets 8+ rebounds against top 15 defenses with 32+ minutes"
        ]
        
        for query in max_complexity_queries:
            components = mock_parser.parse(query)
            debug_parsing_results(query, components, show_confidence=False)
            
            # Should extract main player
            assert components.player_name is not None, f"No main player found for: {query}"
            
            # Should have time filter
            assert components.time_period == "recent", f"Expected recent time period for: {query}"
            assert components.game_count is not None, f"No game count found for: {query}"
            
            # Should have location
            assert components.location is not None, f"No location found for: {query}"
            
            # Should have player relationships
            relationship_found = len(components.players_on) > 0 or len(components.players_off) > 0
            assert relationship_found, f"No player relationships found for: {query}"
            
            # Should have multiple self-filters
            assert len(components.self_filters) >= 2, f"Expected multiple self-filters for: {query}"
            
            # Should have high confidence for well-structured queries
            assert components.confidence > 0.7, f"Low confidence ({components.confidence:.3f}) for: {query}"
    
    def test_nested_player_relationships(self, mock_parser: BaseQueryParser) -> None:
        """Test complex nested player relationship queries."""
        nested_queries = [
            # Multiple with/without combinations
            "LeBron with AD and Russ but without Dwight and THJ last 10 games",
            "Curry with Klay, Draymond, and Wiggins but without Poole and Wiseman this season",
            "Giannis with Dame and Brook but without Khris and Bobby at home",
            
            # Complex lists with commas and conjunctions
            "Luka with Kyrie, Christian Wood, and Josh Green but without Tim Hardaway Jr and Maxi Kleber on the road",
            "Tatum with Brown, Smart, Horford, and Williams but without Grant Williams recent 15 games",
            
            # Mixed relationship complexity
            "KD with Kyrie and Nic Claxton but without Ben Simmons and Joe Harris where he scores 30+ points"
        ]
        
        for query in nested_queries:
            components = mock_parser.parse(query)
            debug_parsing_results(query, components, show_confidence=False)
            
            # Should have main player
            assert components.player_name is not None, f"No main player found for: {query}"
            
            # Should have both ON and OFF players for "but without" queries
            if "but without" in query:
                assert len(components.players_on) > 0, f"No players_on found for 'with...but without' query: {query}"
                assert len(components.players_off) > 0, f"No players_off found for 'with...but without' query: {query}"
            
            # Should have multiple players total
            total_players = len(components.players_on) + len(components.players_off)
            assert total_players >= 2, f"Expected multiple player relationships for: {query}"
    
    def test_multi_stat_combinations(self, mock_parser: BaseQueryParser) -> None:
        """Test queries with multiple statistical filters."""
        multi_stat_queries = [
            # Triple-double scenarios
            "LeBron games where he scores 25+ points and gets 10+ rebounds and 8+ assists",
            "Westbrook games where he gets double-digit points and double-digit rebounds and double-digit assists",
            
            # Shooting efficiency combinations
            "Curry games where he attempts 15+ field goals and makes 8+ threes and scores 30+ points",
            "Dame games where he takes 20+ shots and makes 12+ field goals and attempts 10+ threes",
            
            # Defensive combinations
            "Giannis games where he gets 12+ rebounds and 2+ blocks and 1+ steals and scores 25+ points",
            "AD games where he blocks 3+ shots and gets 10+ defensive rebounds and scores 20+ points",
            
            # Minutes and performance combinations
            "Jokic games where he plays 35+ minutes and scores 25+ points and gets 12+ rebounds and 8+ assists",
            "Embiid games where he plays between 30 and 38 minutes and scores 28+ points and blocks 2+ shots"
        ]
        
        for query in multi_stat_queries:
            components = mock_parser.parse(query)
            debug_parsing_results(query, components, show_confidence=False)
            
            # Should have multiple self-filters
            assert len(components.self_filters) >= 2, f"Expected multiple self-filters for: {query}"
            
            # Check for variety in stat types
            stat_types = {f.stat_column for f in components.self_filters}
            assert len(stat_types) >= 2, f"Expected diverse stat types for: {query}"


class TestEdgeCasesAndAmbiguity:
    """Test edge cases and potentially ambiguous queries."""
    
    def test_number_confusion_scenarios(self, mock_parser: BaseQueryParser) -> None:
        """Test queries where numbers could be confused (jersey numbers vs stats)."""
        number_confusion_queries = [
            # Jersey numbers vs stats
            "LeBron games where he wears number 6 and scores 30+ points",  # Should only extract scoring
            "Curry games with number 30 where he makes 8+ threes",  # Should only extract threes
            
            # Similar numbers in different contexts
            "Giannis last 10 games where he scores exactly 34 points",  # 10 = games, 34 = points
            "Dame recent 15 games where he plays 35+ minutes",  # 15 = games, 35 = minutes
            
            # Multiple numbers that could be confused
            "Luka last 20 games where he scores between 25 and 35 points and plays 32+ minutes"
        ]
        
        for query in number_confusion_queries:
            components = mock_parser.parse(query)
            debug_parsing_results(query, components, show_confidence=False)
            
            # Should correctly identify time vs stats
            if "last" in query and "games" in query:
                assert components.game_count is not None, f"Should extract game count from: {query}"
            
            # Should extract statistical filters
            assert len(components.self_filters) >= 1, f"Should extract self-filters from: {query}"
    
    def test_similar_player_names(self, mock_parser: BaseQueryParser) -> None:
        """Test queries with similar player names that could cause confusion."""
        similar_name_queries = [
            # Different players with similar names
            "Kevin Durant with Kyrie Irving last 10 games",
            "Anthony Davis with Anthony Edwards recent 5 games",
            
            # Full names vs nicknames
            "LeBron James with King James last 15 games",  # Should not duplicate
            "Stephen Curry with Steph Curry this season",  # Should not duplicate
            
            # Last name ambiguity
            "Brown with Tatum last 10 games",  # Jaylen Brown
            "Green with Curry at home",  # Draymond Green
        ]
        
        for query in similar_name_queries:
            components = mock_parser.parse(query)
            debug_parsing_results(query, components, show_confidence=False)
            
            # Should not have duplicate players
            all_players = [components.player_name] + components.players_on + components.players_off
            all_players = [p for p in all_players if p is not None]
            unique_players = set(all_players)
            
            assert len(all_players) == len(unique_players), f"Duplicate players detected in: {query}"
    
    def test_statistical_term_confusion(self, mock_parser: BaseQueryParser) -> None:
        """Test queries with statistical terms that could be confused with player names."""
        stat_confusion_queries = [
            # Statistical terms that should NOT be players
            "LeBron games with exactly 28 points and approximately 10 rebounds",
            "Curry games with around 25 points and nearly 8 threes",
            "Giannis games with over 30 points and under 5 turnovers",
            "Dame games with between 20 and 30 points and more than 8 assists",
            
            # Terms that could trigger fuzzy matching
            "Luka games with precisely 35 points and roughly 12 rebounds",
            "Tatum games with about 25 points and close to 8 rebounds"
        ]
        
        for query in stat_confusion_queries:
            components = mock_parser.parse(query)
            debug_parsing_results(query, components, show_confidence=False)
            
            # Should not extract statistical terms as players
            all_players = [components.player_name] + components.players_on + components.players_off
            stat_terms = {'exactly', 'approximately', 'around', 'nearly', 'over', 'under', 
                         'between', 'more', 'less', 'precisely', 'roughly', 'about', 'close'}
            
            for player in all_players:
                if player:
                    player_words = set(player.lower().split())
                    overlap = player_words & stat_terms
                    assert len(overlap) == 0, f"Statistical term '{overlap}' incorrectly extracted as player in: {query}"


class TestRealWorldScenarios:
    """Test realistic queries that users might actually make."""
    
    def test_fantasy_basketball_queries(self, mock_parser: BaseQueryParser) -> None:
        """Test queries typical of fantasy basketball analysis."""
        fantasy_queries = [
            # Daily fantasy analysis
            "LeBron last 5 games at home with AD where he scores 25+ points and gets 8+ rebounds and 6+ assists with 35+ minutes",
            
            # Matchup analysis
            "Curry away games against top 10 defenses where he makes 6+ threes and scores 28+ points",
            
            # Injury impact analysis
            "Giannis games without Dame where he scores 30+ points and gets 12+ rebounds with 36+ minutes",
            
            # Value plays
            "Alperen Sengun recent 10 games where he scores 15+ points and gets 8+ rebounds and 5+ assists with less than 32 minutes",
            
            # Ceiling/floor analysis
            "Luka home games with Kyrie where he scores between 35 and 50 points and gets 10+ rebounds"
        ]
        
        for query in fantasy_queries:
            components = mock_parser.parse(query)
            debug_parsing_results(query, components, show_confidence=False)
            
            # Fantasy queries should have high confidence
            assert components.confidence > 0.75, f"Low confidence for fantasy query: {query}"
            
            # Should have statistical filters (key for fantasy)
            assert len(components.self_filters) >= 1, f"No self-filters for fantasy query: {query}"
    
    def test_analyst_research_queries(self, mock_parser: BaseQueryParser) -> None:
        """Test queries typical of basketball analysts and researchers."""
        analyst_queries = [
            # Clutch performance analysis
            "KD games where he scores 30+ points and plays 35+ minutes against top 5 defenses on the road",
            
            # Team chemistry analysis
            "Tatum with Brown and Smart but without Williams where he scores 25+ points and gets 8+ rebounds",
            
            # Matchup-specific analysis
            "Dame against worst 8 three point defenses where he attempts 12+ threes and makes 6+ threes",
            
            # Load management impact
            "Kawhi games where he plays between 25 and 35 minutes and scores 22+ points with 6+ rebounds",
            
            # Rookie development tracking
            "Victor Wembanyama recent 15 games where he blocks 2+ shots and scores 18+ points and gets 10+ rebounds"
        ]
        
        for query in analyst_queries:
            components = mock_parser.parse(query)
            debug_parsing_results(query, components, show_confidence=False)
            
            # Analyst queries should have good structure
            assert components.player_name is not None, f"No main player for analyst query: {query}"
            
            # Should capture analytical filters
            has_filters = (len(components.self_filters) > 0 or 
                          len(components.opponent_filters) > 0 or 
                          components.minutes_filter is not None)
            assert has_filters, f"No analytical filters found for: {query}"
    
    def test_casual_fan_queries(self, mock_parser: BaseQueryParser) -> None:
        """Test queries typical of casual NBA fans."""
        casual_queries = [
            # Simple performance questions
            "LeBron last 10 games with 30+ points",
            "Curry this season with 8+ threes",
            "Giannis home games with double-digit rebounds",
            
            # Basic comparisons
            "Luka recent games where he scores more than 25 points",
            "Dame games where he makes over 6 threes",
            
            # Team context
            "Tatum with Brown last 15 games where he scores 20+ points"
        ]
        
        for query in casual_queries:
            components = mock_parser.parse(query)
            debug_parsing_results(query, components, show_confidence=False)
            
            # Casual queries should be parsed successfully
            assert components.player_name is not None, f"Failed to parse casual query: {query}"
            
            # Should have reasonable confidence
            assert components.confidence > 0.7, f"Low confidence for casual query: {query}"


class TestStressAndPerformance:
    """Test parser performance under stress conditions."""
    
    def test_very_long_queries(self, mock_parser: BaseQueryParser) -> None:
        """Test extremely long and complex queries."""
        long_queries = [
            # Extremely detailed fantasy analysis
            "LeBron James last twenty games at home during this season with Anthony Davis and Russell Westbrook but without Dwight Howard and Kendrick Nunn where he scores at least thirty points and gets a minimum of eight rebounds and distributes six or more assists and plays between thirty-five and forty-two minutes and attempts fifteen or more field goals and makes at least twelve field goals and shoots over eighty percent from the free throw line against top ten defensive teams",
            
            # Complex multi-player scenario
            "Stephen Curry past fifteen away games this season with Klay Thompson, Draymond Green, and Andrew Wiggins but without Jordan Poole and James Wiseman where he makes eight or more three pointers and scores twenty-five or more points and attempts at least fifteen three point shots and plays more than thirty minutes and has fewer than four turnovers against bottom five three point defensive teams",
            
            # Ultimate complexity query
            "Giannis Antetokounmpo recent twelve road games without Damian Lillard but with Brook Lopez and Khris Middleton where he scores between twenty-eight and forty-five points and gets double-digit rebounds and records five or more assists and blocks two or more shots and plays approximately thirty-six minutes and attempts eighteen or more field goals against teams ranked in the top fifteen for defensive efficiency"
        ]
        
        for query in long_queries:
            components = mock_parser.parse(query)
            debug_parsing_results(query, components, show_confidence=False)
            
            # Should still parse successfully despite length
            assert components.player_name is not None, f"Failed to parse long query"
            
            # Should extract multiple components
            total_filters = (len(components.self_filters) + 
                           len(components.opponent_filters) + 
                           (1 if components.minutes_filter else 0) +
                           (1 if components.location else 0) +
                           (1 if components.time_period else 0))
            
            assert total_filters >= 3, f"Expected multiple filters for complex query, got {total_filters}"
    
    def test_many_player_relationships(self, mock_parser: BaseQueryParser) -> None:
        """Test queries with many player relationships."""
        many_player_queries = [
            # Large team scenarios
            "LeBron with AD, Russ, Dwight, Austin Reaves, and Malik Monk but without THT and Kendrick Nunn last 10 games",
            
            # Full rotation analysis
            "Curry with Klay, Draymond, Wiggins, Looney, Poole, and Kuminga but without Wiseman and Moody this season",
            
            # Complex exclusions
            "Giannis without Dame, Khris, Brook, Bobby, and Pat but with Malik and AJ Green recent 5 games"
        ]
        
        for query in many_player_queries:
            components = mock_parser.parse(query)
            debug_parsing_results(query, components, show_confidence=False)
            
            # Should handle many players
            total_relationship_players = len(components.players_on) + len(components.players_off)
            assert total_relationship_players >= 3, f"Expected many player relationships for: {query}"
            
            # Should maintain reasonable confidence
            assert components.confidence > 0.6, f"Very low confidence for many-player query: {query}"


if __name__ == "__main__":
    # Run this test suite directly
    pytest.main([__file__, "-v", "-s"]) 