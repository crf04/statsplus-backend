#!/usr/bin/env python3
"""
Comprehensive test suite for confidence validation and triple filter combinations.

Tests both parsing accuracy and confidence scoring to ensure queries with parsing errors
are correctly identified and sent to LLM fallback.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.nl_query.parser import BaseQueryParser
from sqlalchemy import create_engine, text
from typing import Dict, List, Tuple, Any
import traceback

class MockEngine:
    """Mock database engine for testing"""
    
    def connect(self):
        return MockConnection()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        pass

class MockConnection:
    """Mock database connection"""
    
    def execute(self, query):
        return MockResult()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        pass

class MockResult:
    """Mock database result"""
    
    def fetchall(self):
        # Return common NBA players
        return [
            ("LeBron James",),
            ("Anthony Davis",),
            ("Stephen Curry",),
            ("Kevin Durant",),
            ("Giannis Antetokounmpo",),
            ("Luka Dončić",),
            ("Jayson Tatum",),
            ("Jaylen Brown",),
            ("Jimmy Butler",),
            ("Bam Adebayo",),
            ("Damian Lillard",),
            ("CJ McCollum",),
            ("Devin Booker",),
            ("Chris Paul",),
            ("Russell Westbrook",),
            ("Austin Reaves",),
            ("D'Angelo Russell",),
            ("Anthony Edwards",),
            ("Jalen Brunson",),
            ("Donovan Mitchell",),
            ("Shai Gilgeous-Alexander",),
            ("Josh Giddey",),
            ("Paolo Banchero",),
            ("Victor Wembanyama",),
            ("Scottie Barnes",),
            ("Franz Wagner",),
            ("Alperen Şengün",),
            ("Cade Cunningham",),
            ("Jalen Green",),
            ("Evan Mobley",),
        ]

def create_test_cases() -> List[Dict[str, Any]]:
    """Create 30 realistic NBA test cases with varying complexity"""
    
    test_cases = [
        # === SIMPLE TRIPLE FILTER COMBINATIONS (Should work well) ===
        {
            "query": "LeBron James with AD at home games with 35+ minutes",
            "expected_parsing": {
                "player_name": "LeBron James",
                "players_on": ["Anthony Davis"],
                "players_off": [],
                "location": "home",
                "minutes_filter": (35, 48),
                "should_parse_well": True
            },
            "expected_confidence": "high",
            "category": "simple_triple"
        },
        
        {
            "query": "Stephen Curry without Draymond on road games with 30+ minutes",
            "expected_parsing": {
                "player_name": "Stephen Curry",
                "players_on": [],
                "players_off": ["Draymond Green"],
                "location": "away",
                "minutes_filter": (30, 48),
                "should_parse_well": True
            },
            "expected_confidence": "high",
            "category": "simple_triple"
        },
        
        {
            "query": "Jayson Tatum with Jaylen Brown away games 25-35 minutes",
            "expected_parsing": {
                "player_name": "Jayson Tatum",
                "players_on": ["Jaylen Brown"],
                "players_off": [],
                "location": "away",
                "minutes_filter": (25, 35),
                "should_parse_well": True
            },
            "expected_confidence": "high",
            "category": "simple_triple"
        },
        
        {
            "query": "Giannis home games with 32+ minutes without Khris Middleton",
            "expected_parsing": {
                "player_name": "Giannis Antetokounmpo",
                "players_on": [],
                "players_off": ["Khris Middleton"],
                "location": "home",
                "minutes_filter": (32, 48),
                "should_parse_well": True
            },
            "expected_confidence": "high",
            "category": "simple_triple"
        },
        
        {
            "query": "Luka Dončić with Kyrie Irving at home under 40 minutes",
            "expected_parsing": {
                "player_name": "Luka Dončić",
                "players_on": ["Kyrie Irving"],
                "players_off": [],
                "location": "home",
                "minutes_filter": (0, 40),
                "should_parse_well": True
            },
            "expected_confidence": "high",
            "category": "simple_triple"
        },
        
        # === DOUBLE FILTER COMBINATIONS (Should work well) ===
        {
            "query": "Jimmy Butler home games with 30+ minutes",
            "expected_parsing": {
                "player_name": "Jimmy Butler",
                "players_on": [],
                "players_off": [],
                "location": "home",
                "minutes_filter": (30, 48),
                "should_parse_well": True
            },
            "expected_confidence": "high",
            "category": "double_filter"
        },
        
        {
            "query": "Devin Booker with Chris Paul on road games",
            "expected_parsing": {
                "player_name": "Devin Booker",
                "players_on": ["Chris Paul"],
                "players_off": [],
                "location": "away",
                "minutes_filter": None,
                "should_parse_well": True
            },
            "expected_confidence": "high",
            "category": "double_filter"
        },
        
        {
            "query": "Damian Lillard without CJ McCollum 28-38 minutes",
            "expected_parsing": {
                "player_name": "Damian Lillard",
                "players_on": [],
                "players_off": ["CJ McCollum"],
                "location": None,
                "minutes_filter": (28, 38),
                "should_parse_well": True
            },
            "expected_confidence": "high",
            "category": "double_filter"
        },
        
        # === NICKNAME/ALIAS USAGE (Medium complexity) ===
        {
            "query": "King James with The Brow at home 35+ minutes",
            "expected_parsing": {
                "player_name": "LeBron James",
                "players_on": ["Anthony Davis"],
                "players_off": [],
                "location": "home",
                "minutes_filter": (35, 48),
                "should_parse_well": True
            },
            "expected_confidence": "medium",
            "category": "nickname_usage"
        },
        
        {
            "query": "Greek Freak with Khris Middleton home games 30+ minutes",
            "expected_parsing": {
                "player_name": "Giannis Antetokounmpo",
                "players_on": ["Khris Middleton"],
                "players_off": [],
                "location": "home",
                "minutes_filter": (30, 48),
                "should_parse_well": True
            },
            "expected_confidence": "medium",
            "category": "nickname_usage"
        },
        
        {
            "query": "CP3 with Book on road games less than 35 minutes",
            "expected_parsing": {
                "player_name": "Chris Paul",
                "players_on": ["Devin Booker"],
                "players_off": [],
                "location": "away",
                "minutes_filter": (0, 35),
                "should_parse_well": True
            },
            "expected_confidence": "medium",
            "category": "nickname_usage"
        },
        
        # === MULTIPLE PLAYERS (Medium complexity) ===
        {
            "query": "Steph Curry with KD and Klay Thompson home games 32+ minutes",
            "expected_parsing": {
                "player_name": "Stephen Curry",
                "players_on": ["Kevin Durant", "Klay Thompson"],
                "players_off": [],
                "location": "home",
                "minutes_filter": (32, 48),
                "should_parse_well": False  # Complex multiple players
            },
            "expected_confidence": "medium",
            "category": "multiple_players"
        },
        
        {
            "query": "Jayson Tatum with Brown and Smart away games under 38 minutes",
            "expected_parsing": {
                "player_name": "Jayson Tatum",
                "players_on": ["Jaylen Brown", "Marcus Smart"],
                "players_off": [],
                "location": "away",
                "minutes_filter": (0, 38),
                "should_parse_well": False
            },
            "expected_confidence": "medium",
            "category": "multiple_players"
        },
        
        {
            "query": "LeBron without AD and Westbrook home games 30+ minutes",
            "expected_parsing": {
                "player_name": "LeBron James",
                "players_on": [],
                "players_off": ["Anthony Davis", "Russell Westbrook"],
                "location": "home",
                "minutes_filter": (30, 48),
                "should_parse_well": False
            },
            "expected_confidence": "medium",
            "category": "multiple_players"
        },
        
        # === COMPLEX SYNTAX (Medium complexity) ===
        {
            "query": "Paolo Banchero with Franz Wagner but without Wendell Carter home games",
            "expected_parsing": {
                "player_name": "Paolo Banchero",
                "players_on": ["Franz Wagner"],
                "players_off": ["Wendell Carter Jr."],
                "location": "home",
                "minutes_filter": None,
                "should_parse_well": False
            },
            "expected_confidence": "medium",
            "category": "complex_syntax"
        },
        
        {
            "query": "Shai Gilgeous-Alexander minus Josh Giddey on road games 30+ minutes",
            "expected_parsing": {
                "player_name": "Shai Gilgeous-Alexander",
                "players_on": [],
                "players_off": ["Josh Giddey"],
                "location": "away",
                "minutes_filter": (30, 48),
                "should_parse_well": True
            },
            "expected_confidence": "medium",
            "category": "complex_syntax"
        },
        
        {
            "query": "Zion Williamson alongside CJ McCollum at home over 25 minutes",
            "expected_parsing": {
                "player_name": "Zion Williamson",
                "players_on": ["CJ McCollum"],
                "players_off": [],
                "location": "home",
                "minutes_filter": (25, 48),
                "should_parse_well": True
            },
            "expected_confidence": "medium",
            "category": "complex_syntax"
        },
        
        # === AMBIGUOUS QUERIES (Should have lower confidence) ===
        {
            "query": "Tatum with Brown at home when they're both healthy",
            "expected_parsing": {
                "player_name": "Jayson Tatum",
                "players_on": ["Jaylen Brown"],
                "players_off": [],
                "location": "home",
                "minutes_filter": None,
                "should_parse_well": False
            },
            "expected_confidence": "low",
            "category": "ambiguous"
        },
        
        {
            "query": "LeBron with his teammates on road games recently",
            "expected_parsing": {
                "player_name": "LeBron James",
                "players_on": [],
                "players_off": [],
                "location": "away",
                "minutes_filter": None,
                "should_parse_well": False
            },
            "expected_confidence": "low",
            "category": "ambiguous"
        },
        
        {
            "query": "Show me Curry games with the core lineup at home",
            "expected_parsing": {
                "player_name": "Stephen Curry",
                "players_on": [],
                "players_off": [],
                "location": "home",
                "minutes_filter": None,
                "should_parse_well": False
            },
            "expected_confidence": "low",
            "category": "ambiguous"
        },
        
        {
            "query": "Giannis without his supporting cast road games",
            "expected_parsing": {
                "player_name": "Giannis Antetokounmpo",
                "players_on": [],
                "players_off": [],
                "location": "away",
                "minutes_filter": None,
                "should_parse_well": False
            },
            "expected_confidence": "low",
            "category": "ambiguous"
        },
        
        # === REALISTIC EDGE CASES ===
        {
            "query": "Victor Wembanyama with Devin Vassell home games exactly 30 minutes",
            "expected_parsing": {
                "player_name": "Victor Wembanyama",
                "players_on": ["Devin Vassell"],
                "players_off": [],
                "location": "home",
                "minutes_filter": (28, 32),
                "should_parse_well": True
            },
            "expected_confidence": "high",
            "category": "realistic_edge"
        },
        
        {
            "query": "Scottie Barnes without OG Anunoby on road games 20-30 minutes",
            "expected_parsing": {
                "player_name": "Scottie Barnes",
                "players_on": [],
                "players_off": ["OG Anunoby"],
                "location": "away",
                "minutes_filter": (20, 30),
                "should_parse_well": True
            },
            "expected_confidence": "high",
            "category": "realistic_edge"
        },
        
        {
            "query": "Alperen Şengün with Fred VanVleet home games minimum 28 minutes",
            "expected_parsing": {
                "player_name": "Alperen Şengün",
                "players_on": ["Fred VanVleet"],
                "players_off": [],
                "location": "home",
                "minutes_filter": (28, 48),
                "should_parse_well": True
            },
            "expected_confidence": "high",
            "category": "realistic_edge"
        },
        
        # === LAST NAME ONLY (Should work but medium confidence) ===
        {
            "query": "Tatum with Brown home games 30+ minutes",
            "expected_parsing": {
                "player_name": "Jayson Tatum",
                "players_on": ["Jaylen Brown"],
                "players_off": [],
                "location": "home",
                "minutes_filter": (30, 48),
                "should_parse_well": True
            },
            "expected_confidence": "medium",
            "category": "last_name_only"
        },
        
        {
            "query": "Durant with Curry away games less than 35 minutes",
            "expected_parsing": {
                "player_name": "Kevin Durant",
                "players_on": ["Stephen Curry"],
                "players_off": [],
                "location": "away",
                "minutes_filter": (0, 35),
                "should_parse_well": True
            },
            "expected_confidence": "medium",
            "category": "last_name_only"
        },
        
        {
            "query": "Lillard without McCollum road games 25-40 minutes",
            "expected_parsing": {
                "player_name": "Damian Lillard",
                "players_on": [],
                "players_off": ["CJ McCollum"],
                "location": "away",
                "minutes_filter": (25, 40),
                "should_parse_well": True
            },
            "expected_confidence": "medium",
            "category": "last_name_only"
        },
        
        # === ROOKIE/YOUNG PLAYERS (Should work well) ===
        {
            "query": "Cade Cunningham with Jalen Green home games 28+ minutes",
            "expected_parsing": {
                "player_name": "Cade Cunningham",
                "players_on": ["Jalen Green"],
                "players_off": [],
                "location": "home",
                "minutes_filter": (28, 48),
                "should_parse_well": True
            },
            "expected_confidence": "high",
            "category": "young_players"
        },
        
        {
            "query": "Evan Mobley without Donovan Mitchell away games under 32 minutes",
            "expected_parsing": {
                "player_name": "Evan Mobley",
                "players_on": [],
                "players_off": ["Donovan Mitchell"],
                "location": "away",
                "minutes_filter": (0, 32),
                "should_parse_well": True
            },
            "expected_confidence": "high",
            "category": "young_players"
        },
        
        {
            "query": "Anthony Edwards with Jalen Brunson road games 30-42 minutes",
            "expected_parsing": {
                "player_name": "Anthony Edwards",
                "players_on": ["Jalen Brunson"],
                "players_off": [],
                "location": "away",
                "minutes_filter": (30, 42),
                "should_parse_well": True
            },
            "expected_confidence": "high",
            "category": "young_players"
        },
        
        # === SINGLE FILTER QUERIES (Should work excellently) ===
        {
            "query": "Bam Adebayo home games only",
            "expected_parsing": {
                "player_name": "Bam Adebayo",
                "players_on": [],
                "players_off": [],
                "location": "home",
                "minutes_filter": None,
                "should_parse_well": True
            },
            "expected_confidence": "high",
            "category": "single_filter"
        }
    ]
    
    return test_cases

def evaluate_parsing_accuracy(parsed_components, expected_parsing) -> Tuple[bool, List[str]]:
    """Evaluate if parsing matches expected results"""
    issues = []
    
    # Check player name
    if parsed_components.player_name != expected_parsing.get("player_name"):
        issues.append(f"Player name: expected {expected_parsing.get('player_name')}, got {parsed_components.player_name}")
    
    # Check players on
    expected_on = set(expected_parsing.get("players_on", []))
    actual_on = set(parsed_components.players_on)
    if expected_on != actual_on:
        issues.append(f"Players ON: expected {expected_on}, got {actual_on}")
    
    # Check players off
    expected_off = set(expected_parsing.get("players_off", []))
    actual_off = set(parsed_components.players_off)
    if expected_off != actual_off:
        issues.append(f"Players OFF: expected {expected_off}, got {actual_off}")
    
    # Check location
    if parsed_components.location != expected_parsing.get("location"):
        issues.append(f"Location: expected {expected_parsing.get('location')}, got {parsed_components.location}")
    
    # Check minutes filter
    if parsed_components.minutes_filter != expected_parsing.get("minutes_filter"):
        issues.append(f"Minutes filter: expected {expected_parsing.get('minutes_filter')}, got {parsed_components.minutes_filter}")
    
    return len(issues) == 0, issues

def evaluate_confidence_appropriateness(confidence_breakdown, expected_confidence, parsing_success) -> Tuple[bool, str]:
    """Evaluate if confidence level is appropriate"""
    confidence = confidence_breakdown.final_confidence
    should_use_llm = confidence_breakdown.should_use_llm
    
    # Define confidence thresholds
    if expected_confidence == "high":
        # High confidence should be >= 0.75 and NOT use LLM
        if confidence >= 0.75 and not should_use_llm:
            return True, f"✅ High confidence: {confidence:.3f}"
        else:
            return False, f"❌ Expected high confidence (>=0.75, no LLM), got {confidence:.3f}, LLM={should_use_llm}"
    
    elif expected_confidence == "medium":
        # Medium confidence should be 0.5-0.74
        if 0.5 <= confidence < 0.75:
            return True, f"✅ Medium confidence: {confidence:.3f}"
        else:
            return False, f"❌ Expected medium confidence (0.5-0.74), got {confidence:.3f}"
    
    elif expected_confidence == "low":
        # Low confidence should be < 0.5 and use LLM
        if confidence < 0.5 and should_use_llm:
            return True, f"✅ Low confidence: {confidence:.3f} -> LLM"
        else:
            return False, f"❌ Expected low confidence (<0.5, use LLM), got {confidence:.3f}, LLM={should_use_llm}"
    
    return False, f"❌ Unknown expected confidence level: {expected_confidence}"

def run_confidence_validation_tests():
    """Run comprehensive confidence validation tests"""
    
    print("🧪 CONFIDENCE VALIDATION & TRIPLE FILTER TEST SUITE")
    print("=" * 80)
    
    # Initialize parser
    try:
        mock_engine = MockEngine()
        parser = BaseQueryParser(mock_engine)
        print(f"✅ Parser initialized successfully")
    except Exception as e:
        print(f"❌ Failed to initialize parser: {e}")
        return False
    
    test_cases = create_test_cases()
    
    # Results tracking
    results = {
        "simple_triple": [],
        "double_filter": [],
        "nickname_usage": [],
        "multiple_players": [],
        "complex_syntax": [],
        "ambiguous": [],
        "realistic_edge": [],
        "last_name_only": [],
        "young_players": [],
        "single_filter": []
    }
    
    total_tests = 0
    confidence_correct = 0
    parsing_correct = 0
    
    # Run tests
    for test_case in test_cases:
        total_tests += 1
        query = test_case["query"]
        expected_parsing = test_case["expected_parsing"]
        expected_confidence = test_case["expected_confidence"]
        category = test_case["category"]
        
        print(f"\n{'='*60}")
        print(f"TEST {total_tests}: {category.upper()}")
        print(f"Query: '{query}'")
        print(f"Expected confidence: {expected_confidence}")
        
        try:
            # Parse query
            components = parser.parse(query)
            
            # Evaluate parsing accuracy
            parsing_success, parsing_issues = evaluate_parsing_accuracy(components, expected_parsing)
            
            # Evaluate confidence appropriateness
            confidence_success, confidence_msg = evaluate_confidence_appropriateness(
                components.confidence_breakdown, expected_confidence, parsing_success
            )
            
            # Display results
            print(f"\n📊 PARSING RESULTS:")
            print(f"   Player: {components.player_name}")
            print(f"   Players ON: {components.players_on}")
            print(f"   Players OFF: {components.players_off}")
            print(f"   Location: {components.location}")
            print(f"   Minutes: {components.minutes_filter}")
            
            if parsing_success:
                print(f"   ✅ Parsing: SUCCESS")
                parsing_correct += 1
            else:
                print(f"   ❌ Parsing: FAILED")
                for issue in parsing_issues:
                    print(f"      - {issue}")
            
            print(f"\n🎯 CONFIDENCE ANALYSIS:")
            print(f"   {confidence_msg}")
            
            # Show confidence breakdown
            breakdown = components.confidence_breakdown
            print(f"   Coverage: {breakdown.coverage_score:.3f}")
            print(f"   Semantic: {breakdown.semantic_score:.3f}")
            print(f"   Ambiguity: {breakdown.ambiguity_score:.3f}")
            print(f"   Complexity: {breakdown.complexity_score:.3f}")
            print(f"   Completeness: {breakdown.completeness_score:.3f}")
            print(f"   → Final: {breakdown.final_confidence:.3f}")
            print(f"   → Use LLM: {breakdown.should_use_llm}")
            
            if confidence_success:
                confidence_correct += 1
            
            # Show issues/warnings
            if breakdown.details.get('semantic_warnings'):
                print(f"   ⚠️  Semantic warnings: {breakdown.details['semantic_warnings']}")
            if breakdown.details.get('ambiguities'):
                print(f"   ⚠️  Ambiguities: {len(breakdown.details['ambiguities'])}")
            if breakdown.details.get('uncovered_text'):
                print(f"   ⚠️  Uncovered text: '{breakdown.details['uncovered_text']}'")
            
            # Store result
            results[category].append({
                'query': query,
                'parsing_success': parsing_success,
                'confidence_success': confidence_success,
                'confidence_score': breakdown.final_confidence,
                'should_use_llm': breakdown.should_use_llm,
                'parsing_issues': parsing_issues
            })
            
        except Exception as e:
            print(f"❌ ERROR: {e}")
            traceback.print_exc()
            results[category].append({
                'query': query,
                'parsing_success': False,
                'confidence_success': False,
                'error': str(e)
            })
    
    # Summary report
    print(f"\n{'='*80}")
    print(f"📈 SUMMARY REPORT")
    print(f"{'='*80}")
    
    print(f"\n🎯 OVERALL STATISTICS:")
    print(f"   Total tests: {total_tests}")
    print(f"   Parsing accuracy: {parsing_correct}/{total_tests} ({parsing_correct/total_tests*100:.1f}%)")
    print(f"   Confidence accuracy: {confidence_correct}/{total_tests} ({confidence_correct/total_tests*100:.1f}%)")
    
    # Category breakdown
    print(f"\n📊 CATEGORY BREAKDOWN:")
    for category, tests in results.items():
        if tests:
            parsing_success_rate = sum(1 for t in tests if t.get('parsing_success', False)) / len(tests)
            confidence_success_rate = sum(1 for t in tests if t.get('confidence_success', False)) / len(tests)
            avg_confidence = sum(t.get('confidence_score', 0) for t in tests) / len(tests)
            llm_usage = sum(1 for t in tests if t.get('should_use_llm', False)) / len(tests)
            
            print(f"   {category.upper()}: {len(tests)} tests")
            print(f"      Parsing success: {parsing_success_rate*100:.1f}%")
            print(f"      Confidence success: {confidence_success_rate*100:.1f}%")
            print(f"      Avg confidence: {avg_confidence:.3f}")
            print(f"      LLM usage: {llm_usage*100:.1f}%")
    
    # Key insights
    print(f"\n🔍 KEY INSIGHTS:")
    
    # Check if low confidence queries are being sent to LLM
    low_confidence_queries = [
        t for category_tests in results.values() 
        for t in category_tests 
        if t.get('confidence_score', 1.0) < 0.5
    ]
    
    llm_correctly_used = sum(1 for t in low_confidence_queries if t.get('should_use_llm', False))
    
    print(f"   • Low confidence queries (<0.5): {len(low_confidence_queries)}")
    if low_confidence_queries:
        print(f"   • Correctly sent to LLM: {llm_correctly_used}/{len(low_confidence_queries)} ({llm_correctly_used/len(low_confidence_queries)*100:.1f}%)")
    else:
        print(f"   • Correctly sent to LLM: N/A (no low confidence queries found)")
    
    # Check if high confidence queries are handled by parser
    high_confidence_queries = [
        t for category_tests in results.values() 
        for t in category_tests 
        if t.get('confidence_score', 0.0) >= 0.75
    ]
    
    parser_correctly_used = sum(1 for t in high_confidence_queries if not t.get('should_use_llm', True))
    
    print(f"   • High confidence queries (>=0.75): {len(high_confidence_queries)}")
    if high_confidence_queries:
        print(f"   • Correctly handled by parser: {parser_correctly_used}/{len(high_confidence_queries)} ({parser_correctly_used/len(high_confidence_queries)*100:.1f}%)")
    else:
        print(f"   • Correctly handled by parser: N/A (no high confidence queries found)")
    
    # Success criteria
    success_criteria = [
        ("Overall parsing accuracy", parsing_correct/total_tests >= 0.7),
        ("Confidence accuracy", confidence_correct/total_tests >= 0.8),
        ("LLM fallback for low confidence", (llm_correctly_used/len(low_confidence_queries) >= 0.9) if low_confidence_queries else True),
        ("Parser handling high confidence", (parser_correctly_used/len(high_confidence_queries) >= 0.9) if high_confidence_queries else True)
    ]
    
    print(f"\n✅ SUCCESS CRITERIA:")
    all_passed = True
    for criterion, passed in success_criteria:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"   {status}: {criterion}")
        if not passed:
            all_passed = False
    
    print(f"\n{'='*80}")
    if all_passed:
        print(f"🎉 ALL TESTS PASSED - Confidence system is working correctly!")
    else:
        print(f"⚠️  SOME TESTS FAILED - Confidence system needs adjustment")
    
    return all_passed

if __name__ == "__main__":
    success = run_confidence_validation_tests()
    sys.exit(0 if success else 1) 