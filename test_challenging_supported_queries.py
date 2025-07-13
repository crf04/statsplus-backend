#!/usr/bin/env python3
"""
Test suite for challenging supported queries that might give the parser trouble.

This test focuses on complex but realistic queries that use only supported features
but push the boundaries of parsing complexity, ambiguity, and edge cases.
"""

from typing import List, Dict, Any, Optional
from unittest.mock import Mock, MagicMock
import sys
import os

# Add the project root to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.nl_query.parser import BaseQueryParser, QueryComponents


class MockEngine:
    """Mock database engine for testing."""
    
    def connect(self):
        """Mock database connection."""
        return self
    
    def execute(self, query: str):
        """Mock query execution."""
        class MockResult:
            def fetchall(self):
                return [
                    ("LeBron James",), ("Anthony Davis",), ("Anthony Edwards",), 
                    ("Stephen Curry",), ("Austin Reaves",), ("Luka Doncic",), 
                    ("Kyrie Irving",), ("Jayson Tatum",), ("Jaylen Brown",),
                    ("Giannis Antetokounmpo",), ("Damian Lillard",), ("Kevin Durant",),
                    ("James Harden",), ("Russell Westbrook",), ("Nikola Jokic",),
                    ("Jimmy Butler",), ("Bam Adebayo",), ("Tyler Herro",),
                    ("Shai Gilgeous-Alexander",), ("Josh Giddey",), ("Chet Holmgren",),
                    ("Paolo Banchero",), ("Franz Wagner",), ("Jalen Suggs",),
                    ("Victor Wembanyama",), ("Devin Vassell",), ("Keldon Johnson",),
                    ("Klay Thompson",), ("Jamal Murray",), ("Draymond Green",),
                    ("Andrew Wiggins",), ("Marcus Smart",), ("Robert Williams",),
                    ("Tim Hardaway Jr.",), ("Dwight Powell",), ("Deandre Ayton",),
                    ("Devin Booker",), ("Chris Paul",), ("Kawhi Leonard",),
                    ("Paul George",), ("Russell Westbrook",), ("Zion Williamson",),
                    ("Brandon Ingram",), ("CJ McCollum",), ("Jonas Valanciunas",),
                    ("Alperen Sengun",), ("Jalen Green",), ("Fred VanVleet",),
                    ("Scottie Barnes",), ("Pascal Siakam",), ("OG Anunoby",),
                    ("Mikal Bridges",), ("Cam Johnson",), ("Deandre Ayton",),
                    ("Anfernee Simons",), ("Jerami Grant",), ("Jusuf Nurkic",),
                    ("De'Aaron Fox",), ("Domantas Sabonis",), ("Kevin Huerter",),
                    ("Lauri Markkanen",), ("Walker Kessler",), ("Collin Sexton",),
                    ("Evan Mobley",), ("Donovan Mitchell",), ("Jarrett Allen",),
                    ("Trae Young",), ("Dejounte Murray",), ("Clint Capela",),
                    ("LaMelo Ball",), ("Miles Bridges",), ("Terry Rozier",),
                    ("Karl-Anthony Towns",), ("Anthony Edwards",), ("Jaden McDaniels",),
                    ("Zach LaVine",), ("DeMar DeRozan",), ("Nikola Vucevic",),
                    ("Ja Morant",), ("Jaren Jackson Jr.",), ("Desmond Bane",),
                    ("Brandon Clarke",), ("Steven Adams",), ("Tyus Jones",)
                ]
        return MockResult()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        pass


class TestChallengingSupportedQueries:
    """Test suite for challenging supported queries that might give the parser trouble."""
    
    def __init__(self, parser: BaseQueryParser):
        """Initialize test suite with parser."""
        self.parser = parser
        self.passed = 0
        self.failed = 0
        self.warnings = 0
        
    def assert_condition(self, condition: bool, message: str, warning: bool = False) -> None:
        """Assert a condition and track results."""
        if condition:
            self.passed += 1
        else:
            if warning:
                self.warnings += 1
                print(f"  ⚠️  WARNING: {message}")
            else:
                self.failed += 1
                print(f"  ❌ FAILED: {message}")
                raise AssertionError(message)
    
    def test_complex_player_relationships(self) -> None:
        """Test complex player relationship queries that might cause parsing issues."""
        queries = [
            # Multiple WITH players
            "LeBron James with Anthony Davis, Austin Reaves and Rui Hachimura",
            "Stephen Curry with Klay Thompson, Draymond Green and Andrew Wiggins",
            "Luka Doncic with Kyrie Irving, Tim Hardaway Jr. and Dwight Powell",
            
            # Multiple WITHOUT players
            "Giannis without Damian Lillard, Khris Middleton and Brook Lopez",
            "Jayson Tatum without Jaylen Brown, Marcus Smart and Robert Williams",
            
            # Mixed WITH and WITHOUT
            "Anthony Davis with LeBron James and Austin Reaves but without Russell Westbrook and Patrick Beverley",
            "Jimmy Butler with Bam Adebayo but without Tyler Herro and Duncan Robinson",
            
            # Complex punctuation
            "Shai Gilgeous-Alexander with Josh Giddey, Chet Holmgren, and Jalen Williams",
            "Paolo Banchero with Franz Wagner, Wendell Carter Jr., and Markelle Fultz",
            
            # Ambiguous relationship parsing
            "Nikola Jokic with Jamal Murray and Aaron Gordon without Michael Porter Jr.",
        ]
        
        print("\n=== COMPLEX PLAYER RELATIONSHIPS ===")
        for query in queries:
            components = self.parser.parse(query)
            confidence = components.confidence
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Players On: {components.players_on}")
            print(f"  Players Off: {components.players_off}")
            print(f"  Confidence: {confidence:.3f}")
            print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
            
            # Check parsing accuracy
            self.assert_condition(components.player_name is not None, f"No main player extracted from '{query}'")
            
            # Check for reasonable confidence (may be lower due to complexity)
            self.assert_condition(confidence >= 0.60, f"Very low confidence: {confidence:.3f}", warning=True)
            
            print("  ✅ PASSED\n")
    
    def test_ambiguous_syntax_patterns(self) -> None:
        """Test queries with ambiguous syntax that might confuse the parser."""
        queries = [
            # Ambiguous "with" usage
            "LeBron James with Anthony Davis at home with 30+ minutes",
            "Stephen Curry with Klay Thompson last 10 games with minimum 25 minutes",
            
            # Ambiguous time references
            "Luka Doncic recent games this season at home",
            "Giannis last 15 games this month on the road",
            
            # Multiple location references
            "Anthony Davis home games away from Crypto.com Arena",
            "Jimmy Butler road games at home court advantage",
            
            # Complex numeric expressions
            "Shai Gilgeous-Alexander 25-30 minutes last 5-10 games",
            "Paolo Banchero 30+ minutes recent 15 games this season",
            
            # Nested conditions
            "Nikola Jokic with Jamal Murray without Michael Porter Jr. at home last 10 games",
            "Jayson Tatum without Marcus Smart but with Jaylen Brown 35+ minutes away games",
        ]
        
        print("\n=== AMBIGUOUS SYNTAX PATTERNS ===")
        for query in queries:
            components = self.parser.parse(query)
            confidence = components.confidence
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Players On: {components.players_on}")
            print(f"  Players Off: {components.players_off}")
            print(f"  Location: {components.location}")
            print(f"  Time: {components.time_period} / {components.game_count}")
            print(f"  Minutes: {components.minutes_filter}")
            print(f"  Confidence: {confidence:.3f}")
            print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
            
            # These queries are tricky, so we expect some to have lower confidence
            self.assert_condition(components.player_name is not None, f"No main player extracted from '{query}'")
            self.assert_condition(confidence >= 0.50, f"Extremely low confidence: {confidence:.3f}", warning=True)
            
            print("  ✅ PASSED\n")
    
    def test_edge_case_values(self) -> None:
        """Test queries with edge case values that might cause issues."""
        queries = [
            # Edge case minutes
            "LeBron James 0-5 minutes recent games",
            "Stephen Curry 45+ minutes last 3 games",
            "Luka Doncic exactly 48 minutes this season",
            
            # Edge case game counts
            "Giannis last 1 game at home",
            "Anthony Davis recent 50 games this season",
            "Jimmy Butler last 100 games",
            
            # Unusual time expressions
            "Shai Gilgeous-Alexander recent 7 games this month",
            "Paolo Banchero last 13 games on the road",
            "Nikola Jokic recent 21 games with Jamal Murray",
            
            # Complex minutes ranges
            "Jayson Tatum between 20 and 25 minutes last 8 games",
            "Luka Doncic minimum 40 minutes maximum 48 minutes",
            "Giannis 35-45 minutes away games this season",
            
            # Mixed complex filters
            "Anthony Davis 30+ minutes without LeBron James recent 12 games at home",
            "Stephen Curry less than 30 minutes with Klay Thompson last 6 games away",
        ]
        
        print("\n=== EDGE CASE VALUES ===")
        for query in queries:
            components = self.parser.parse(query)
            confidence = components.confidence
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Minutes: {components.minutes_filter}")
            print(f"  Time: {components.time_period} / {components.game_count}")
            print(f"  Location: {components.location}")
            print(f"  Confidence: {confidence:.3f}")
            print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
            
            # Check that edge cases are handled reasonably
            self.assert_condition(components.player_name is not None, f"No main player extracted from '{query}'")
            
            # Edge cases might have lower confidence
            self.assert_condition(confidence >= 0.55, f"Very low confidence: {confidence:.3f}", warning=True)
            
            print("  ✅ PASSED\n")
    
    def test_long_complex_queries(self) -> None:
        """Test very long and complex queries that might overwhelm the parser."""
        queries = [
            # Super long multi-component queries
            "LeBron James with Anthony Davis and Austin Reaves but without Russell Westbrook at home last 15 games with 35+ minutes this season",
            "Stephen Curry with Klay Thompson and Draymond Green without Andrew Wiggins on the road recent 10 games between 30 and 40 minutes",
            "Luka Doncic with Kyrie Irving and Christian Wood but without Tim Hardaway Jr. and Dwight Powell at home last 12 games minimum 32 minutes",
            
            # Complex relationship chains
            "Giannis Antetokounmpo with Damian Lillard and Khris Middleton but without Brook Lopez and Bobby Portis away games recent 8 games",
            "Jayson Tatum with Jaylen Brown and Kristaps Porzingis without Marcus Smart and Robert Williams home games 30+ minutes this season",
            "Nikola Jokic with Jamal Murray and Aaron Gordon and Michael Porter Jr. but without Bruce Brown recent 20 games at home",
            
            # Nested time and location references
            "Anthony Davis with LeBron James at home during recent games this season with minimum 25 minutes last 10 games",
            "Jimmy Butler without Tyler Herro and Duncan Robinson on the road last 15 games this season with 35+ minutes recent games",
        ]
        
        print("\n=== LONG COMPLEX QUERIES ===")
        for query in queries:
            components = self.parser.parse(query)
            confidence = components.confidence
            breakdown = components.confidence_breakdown
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Players On: {components.players_on}")
            print(f"  Players Off: {components.players_off}")
            print(f"  Location: {components.location}")
            print(f"  Time: {components.time_period} / {components.game_count}")
            print(f"  Minutes: {components.minutes_filter}")
            print(f"  Confidence: {confidence:.3f}")
            print(f"  Should use LLM: {breakdown.should_use_llm}")
            print(f"  Coverage: {breakdown.coverage_score:.3f}")
            print(f"  Ambiguity: {breakdown.ambiguity_score:.3f}")
            print(f"  Complexity: {breakdown.complexity_score:.3f}")
            
            # Long queries are challenging, expect lower confidence
            self.assert_condition(components.player_name is not None, f"No main player extracted from '{query}'")
            self.assert_condition(confidence >= 0.40, f"Extremely low confidence: {confidence:.3f}", warning=True)
            
            print("  ✅ PASSED\n")
    
    def test_similar_player_names(self) -> None:
        """Test queries with similar player names that might cause confusion."""
        queries = [
            # Similar last names
            "Marcus Smart with Marcus Morris recent games",
            "Robert Williams with Robert Covington last 10 games",
            "Chris Paul with Chris Boucher at home",
            
            # Similar first names
            "Anthony Davis with Anthony Edwards last 5 games",
            "Michael Porter Jr. with Michael Carter-Williams",
            "Kevin Durant with Kevin Huerter away games",
            
            # Partial name matches
            "Brown with Green last 10 games",  # Jaylen Brown with Draymond Green?
            "Davis without Edwards recent games",  # Anthony Davis without Anthony Edwards?
            "Williams with Thompson at home",  # Robert Williams with Klay Thompson?
            
            # Complex name patterns
            "Jaren Jackson Jr. with Michael Porter Jr. last 15 games",
            "Tim Hardaway Jr. with Gary Payton II recent games",
            "Otto Porter Jr. with Wendell Carter Jr. at home",
        ]
        
        print("\n=== SIMILAR PLAYER NAMES ===")
        for query in queries:
            components = self.parser.parse(query)
            confidence = components.confidence
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Players On: {components.players_on}")
            print(f"  Players Off: {components.players_off}")
            print(f"  Confidence: {confidence:.3f}")
            print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
            
            # Similar names might cause ambiguity
            self.assert_condition(components.player_name is not None, f"No main player extracted from '{query}'")
            
            # These might have lower confidence due to ambiguity
            self.assert_condition(confidence >= 0.45, f"Very low confidence: {confidence:.3f}", warning=True)
            
            print("  ✅ PASSED\n")
    
    def test_boundary_parsing_cases(self) -> None:
        """Test boundary cases that might break parsing logic."""
        queries = [
            # Boundary punctuation
            "LeBron James, with Anthony Davis, at home, last 10 games",
            "Stephen Curry; with Klay Thompson; away games; 30+ minutes",
            
            # Repeated keywords
            "Luka Doncic with with Kyrie Irving last last 5 games",
            "Giannis at at home recent recent games",
            
            # Mixed case sensitivity
            "ANTHONY DAVIS with lebron james AT HOME",
            "stephen curry WITH klay thompson LAST 10 GAMES",
            
            # Special characters in context
            "Jimmy Butler @ home with Bam Adebayo",
            "Shai Gilgeous-Alexander vs. top teams last 15 games",
            
            # Number edge cases
            "Paolo Banchero 00+ minutes recent games",
            "Nikola Jokic last 0 games this season",
            "Jayson Tatum 99+ minutes away games",
            
            # Complex negation patterns
            "Anthony Davis not without LeBron James at home",
            "Stephen Curry with not Klay Thompson last 10 games",
        ]
        
        print("\n=== BOUNDARY PARSING CASES ===")
        for query in queries:
            components = self.parser.parse(query)
            confidence = components.confidence
            
            print(f"Query: '{query}'")
            print(f"  Player: {components.player_name}")
            print(f"  Players On: {components.players_on}")
            print(f"  Players Off: {components.players_off}")
            print(f"  Location: {components.location}")
            print(f"  Time: {components.time_period} / {components.game_count}")
            print(f"  Minutes: {components.minutes_filter}")
            print(f"  Confidence: {confidence:.3f}")
            print(f"  Should use LLM: {components.confidence_breakdown.should_use_llm}")
            
            # Boundary cases might have very low confidence
            self.assert_condition(components.player_name is not None, f"No main player extracted from '{query}'")
            self.assert_condition(confidence >= 0.30, f"Extremely low confidence: {confidence:.3f}", warning=True)
            
            print("  ✅ PASSED\n")
    
    def run_all_tests(self) -> None:
        """Run all tests and report results."""
        try:
            print("Testing challenging supported queries that might give the parser trouble...")
            print("These queries use only supported features but test edge cases and parsing complexity.")
            print()
            
            self.test_complex_player_relationships()
            self.test_ambiguous_syntax_patterns()
            self.test_edge_case_values()
            self.test_long_complex_queries()
            self.test_similar_player_names()
            self.test_boundary_parsing_cases()
            
            print("=" * 80)
            print(f"📊 CHALLENGING QUERIES TEST RESULTS:")
            print(f"  ✅ Passed: {self.passed}")
            print(f"  ⚠️  Warnings: {self.warnings}")
            print(f"  ❌ Failed: {self.failed}")
            print(f"  📈 Success Rate: {self.passed/(self.passed+self.failed)*100:.1f}%")
            
            if self.warnings > 0:
                print(f"\n⚠️  {self.warnings} queries had warning-level issues (lower confidence, parsing concerns)")
            
            print("=" * 80)
            
        except AssertionError as e:
            print("=" * 80)
            print(f"❌ CHALLENGING QUERIES TEST FAILED!")
            print(f"  ✅ Passed: {self.passed}")
            print(f"  ⚠️  Warnings: {self.warnings}")
            print(f"  ❌ Failed: {self.failed}")
            print(f"  Last error: {e}")
            print("=" * 80)
            return False
        
        return True


def run_challenging_supported_queries_test() -> None:
    """Run the challenging supported queries test."""
    print("=" * 80)
    print("CHALLENGING SUPPORTED QUERIES TEST")
    print("=" * 80)
    
    try:
        # Create mock parser
        mock_engine = MockEngine()
        parser = BaseQueryParser(mock_engine)
        
        # Create test instance
        test_instance = TestChallengingSupportedQueries(parser)
        
        # Run all tests
        success = test_instance.run_all_tests()
        
        if success:
            print("\n🎉 CHALLENGING QUERIES TEST COMPLETED!")
            print("The parser handled challenging scenarios reasonably well.")
        else:
            print("\n💥 CHALLENGING QUERIES TEST FAILED!")
            print("The parser struggled with some challenging scenarios.")
            
    except Exception as e:
        print(f"\n💥 ERROR RUNNING TESTS: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    run_challenging_supported_queries_test() 