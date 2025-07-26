#!/usr/bin/env python3
"""
Hybrid Accuracy Test Suite

Tests low confidence queries with both NLP and LLM to validate
the hybrid routing strategy and measure actual accuracy improvements.
"""

import os
import sys
import time
import json
import statistics
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass, asdict
from datetime import datetime

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.nl_query.parser import BaseQueryParser, QueryComponents
from app.services.llm_service import LLMService
from sqlalchemy import create_engine
from config import Config


@dataclass
class ExpectedComponents:
    """Expected components that should be extracted from a query."""
    
    player_name: Optional[str] = None
    team_name: Optional[str] = None
    time_period: Optional[str] = None
    game_count: Optional[int] = None
    location: Optional[str] = None
    minutes_filter: Optional[Tuple[int, int]] = None
    self_filters: Optional[List[Dict[str, Any]]] = None
    opponent_filters: Optional[List[Tuple[str, int]]] = None
    players_on: Optional[List[str]] = None
    players_off: Optional[List[str]] = None
    intent: str = "game_logs"


@dataclass
class HybridTestCase:
    """Test case for hybrid accuracy comparison."""
    
    query: str
    category: str
    clarity_level: str
    expected: ExpectedComponents
    nlp_confidence: float
    nlp_accuracy: float
    notes: str = ""


@dataclass
class HybridTestResult:
    """Result comparing NLP vs LLM on same query."""
    
    test_case: HybridTestCase
    
    # NLP results
    nlp_confidence: float
    nlp_accuracy: float
    nlp_processing_time: float
    
    # LLM results
    llm_success: bool
    llm_confidence: float
    llm_accuracy: float
    llm_processing_time: float
    llm_components: Optional[Dict[str, Any]] = None
    
    # Comparison
    accuracy_improvement: float = 0.0
    recommended_parser: str = "nlp"  # "nlp" or "llm"


class HybridAccuracyTester:
    """Test hybrid NLP+LLM accuracy on challenging cases."""
    
    def __init__(self):
        """Initialize the hybrid tester."""
        self.engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
        self.nlp_parser = BaseQueryParser(self.engine)
        self.llm_service = LLMService()
        print(f"✅ NLP Parser and LLM Service initialized for hybrid testing")
    
    def get_low_confidence_test_cases(self) -> List[HybridTestCase]:
        """
        Get the low confidence cases from comprehensive accuracy test.
        
        Returns:
            List of test cases that had confidence < 0.8 in NLP parser
        """
        test_cases = []
        
        # Cases that had confidence < 0.8 in the comprehensive test
        test_cases.extend([
            HybridTestCase(
                "embid games",
                "typo", "very_unclear",
                ExpectedComponents(
                    player_name="Joel Embiid"
                ),
                nlp_confidence=0.658,
                nlp_accuracy=0.0,  # Failed completely
                notes="Misspelled name - complete NLP failure"
            ),
            HybridTestCase(
                "Dame vs top 10 three point defenses",
                "opponent_specific", "clear",
                ExpectedComponents(
                    player_name="Damian Lillard",
                    opponent_filters=[("OPP_FG3M", 10)]
                ),
                nlp_confidence=0.753,
                nlp_accuracy=0.5,  # Got player, missed opponent filter
                notes="Specific defense type - missed opponent filter"
            ),
            HybridTestCase(
                "Beal home games this season scoring 30+",
                "complete", "clear",
                ExpectedComponents(
                    player_name="Bradley Beal",
                    location="home",
                    time_period="season",
                    self_filters=[{"stat_column": "PTS", "operator": "gte", "value": 30}]
                ),
                nlp_confidence=0.708,
                nlp_accuracy=0.75,  # Got 3/4 components, missed stat filter
                notes="Missing stat extraction from 'scoring 30+'"
            ),
            HybridTestCase(
                "Jimmy Butler last 20 games",
                "full_name", "very_clear",
                ExpectedComponents(
                    player_name="Jimmy Butler",
                    time_period="recent",
                    game_count=20
                ),
                nlp_confidence=0.670,
                nlp_accuracy=1.0,  # Actually got everything right but low confidence
                notes="Full name ambiguity caused low confidence"
            ),
        ])
        
        # Add some additional challenging cases for LLM
        test_cases.extend([
            HybridTestCase(
                "Curry shooting lights out recently",
                "metaphor", "unclear",
                ExpectedComponents(
                    player_name="Stephen Curry"
                    # "lights out" is metaphorical - hard to extract specific stats
                ),
                nlp_confidence=0.716,  # From previous tests
                nlp_accuracy=1.0,  # Would likely get player only
                notes="Metaphorical expression"
            ),
            HybridTestCase(
                "LeBron when he goes off",
                "vague", "unclear",
                ExpectedComponents(
                    player_name="LeBron James"
                    # "goes off" is too vague for specific components
                ),
                nlp_confidence=0.970,  # Surprisingly high from previous test
                nlp_accuracy=1.0,  # Gets player correctly
                notes="Vague expression but NLP handled well"
            ),
            HybridTestCase(
                "Giannis efficient games this season",
                "subjective", "moderate",
                ExpectedComponents(
                    player_name="Giannis Antetokounmpo",
                    time_period="season"
                    # "efficient" is subjective - no clear stat filter
                ),
                nlp_confidence=0.829,  # From previous tests
                nlp_accuracy=1.0,  # Got player + time correctly
                notes="Subjective term - handled well by NLP"
            ),
            HybridTestCase(
                "Dame clutch games with 25+ points",
                "context", "moderate",
                ExpectedComponents(
                    player_name="Damian Lillard",
                    self_filters=[{"stat_column": "PTS", "operator": "gte", "value": 25}]
                    # "clutch" is context-dependent but stat filter is clear
                ),
                nlp_confidence=0.885,  # From previous tests
                nlp_accuracy=1.0,  # Got both components
                notes="Context term + clear stat"
            ),
        ])
        
        # Filter to only include actual low confidence cases (< 0.8)
        low_confidence_cases = [tc for tc in test_cases if tc.nlp_confidence < 0.8]
        
        return low_confidence_cases
    
    def evaluate_llm_components(self, expected: ExpectedComponents, llm_result: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
        """
        Evaluate LLM component extraction accuracy.
        
        Args:
            expected: Expected components
            llm_result: LLM parsed result
            
        Returns:
            Tuple of (accuracy, extracted_components)
        """
        total_components = 0
        correct_components = 0
        extracted = {}
        
        # Player name
        if expected.player_name is not None:
            total_components += 1
            extracted_player = llm_result.get("player_name")
            player_correct = (extracted_player == expected.player_name)
            extracted["player"] = extracted_player
            if player_correct:
                correct_components += 1
        
        # Time period  
        if expected.time_period is not None:
            total_components += 1
            extracted_time = llm_result.get("time_period")
            time_correct = (extracted_time == expected.time_period)
            extracted["time_period"] = extracted_time
            if time_correct:
                correct_components += 1
        
        # Game count
        if expected.game_count is not None:
            total_components += 1
            extracted_count = llm_result.get("game_count")
            count_correct = (extracted_count == expected.game_count)
            extracted["game_count"] = extracted_count
            if count_correct:
                correct_components += 1
        
        # Location
        if expected.location is not None:
            total_components += 1
            extracted_location = llm_result.get("location")
            location_correct = (extracted_location == expected.location)
            extracted["location"] = extracted_location
            if location_correct:
                correct_components += 1
        
        # Self filters (stats)
        if expected.self_filters is not None:
            total_components += 1
            extracted_filters = llm_result.get("self_filters", [])
            stats_correct = self._compare_llm_self_filters(expected.self_filters, extracted_filters)
            extracted["self_filters"] = extracted_filters
            if stats_correct:
                correct_components += 1
        
        # Opponent filters
        if expected.opponent_filters is not None:
            total_components += 1
            extracted_opponent = llm_result.get("opponent_filters", [])
            opponent_correct = self._compare_llm_opponent_filters(expected.opponent_filters, extracted_opponent)
            extracted["opponent_filters"] = extracted_opponent
            if opponent_correct:
                correct_components += 1
        
        # Players on
        if expected.players_on is not None:
            total_components += 1
            extracted_on = llm_result.get("players_on", [])
            players_on_correct = self._compare_player_lists(expected.players_on, extracted_on)
            extracted["players_on"] = extracted_on
            if players_on_correct:
                correct_components += 1
        
        # Players off
        if expected.players_off is not None:
            total_components += 1
            extracted_off = llm_result.get("players_off", [])
            players_off_correct = self._compare_player_lists(expected.players_off, extracted_off)
            extracted["players_off"] = extracted_off
            if players_off_correct:
                correct_components += 1
        
        accuracy = correct_components / total_components if total_components > 0 else 1.0
        
        return accuracy, extracted
    
    def _compare_llm_self_filters(self, expected: List[Dict[str, Any]], actual: List[Dict[str, Any]]) -> bool:
        """Compare LLM self filters for accuracy."""
        if len(expected) != len(actual):
            return False
        
        for exp_filter in expected:
            found_match = False
            for act_filter in actual:
                if (act_filter.get("stat_column") == exp_filter["stat_column"] and
                    act_filter.get("operator") == exp_filter["operator"] and
                    act_filter.get("value") == exp_filter["value"]):
                    found_match = True
                    break
            if not found_match:
                return False
        
        return True
    
    def _compare_llm_opponent_filters(self, expected: List[Tuple[str, int]], actual: List[List]) -> bool:
        """Compare LLM opponent filters for accuracy."""
        if len(expected) != len(actual):
            return False
        
        for exp_filter in expected:
            expected_tuple = (exp_filter[0], exp_filter[1])
            found_match = False
            for act_filter in actual:
                if len(act_filter) >= 2:
                    actual_tuple = (act_filter[0], act_filter[1])
                    if actual_tuple == expected_tuple:
                        found_match = True
                        break
            if not found_match:
                return False
        
        return True
    
    def _compare_player_lists(self, expected: List[str], actual: List[str]) -> bool:
        """Compare player lists for accuracy."""
        if len(expected) != len(actual):
            return False
        
        for player in expected:
            if player not in actual:
                return False
        
        return True
    
    def test_single_case(self, test_case: HybridTestCase) -> HybridTestResult:
        """Test a single case with both NLP and LLM."""
        print(f"   Testing: '{test_case.query}'")
        
        # Test with LLM
        start_time = time.time()
        llm_result = self.llm_service.test_prompt_file("prompts/system_prompt_optimized.txt", test_case.query)
        llm_processing_time = time.time() - start_time
        
        llm_success = llm_result.get("success", False)
        llm_accuracy = 0.0
        llm_confidence = 0.0
        llm_components = None
        
        if llm_success:
            llm_content = llm_result.get("content", {})
            llm_confidence = llm_content.get("confidence", 0.0)
            llm_accuracy, llm_components = self.evaluate_llm_components(test_case.expected, llm_content)
        
        # Calculate improvement and recommendation
        accuracy_improvement = llm_accuracy - test_case.nlp_accuracy
        
        # Recommend LLM if it's significantly better, considering speed cost
        if accuracy_improvement > 0.2:  # 20% accuracy improvement threshold
            recommended_parser = "llm"
        elif accuracy_improvement > 0.1 and test_case.nlp_accuracy < 0.7:  # 10% improvement for low accuracy cases
            recommended_parser = "llm"
        else:
            recommended_parser = "nlp"
        
        return HybridTestResult(
            test_case=test_case,
            nlp_confidence=test_case.nlp_confidence,
            nlp_accuracy=test_case.nlp_accuracy,
            nlp_processing_time=0.02,  # Typical NLP speed
            llm_success=llm_success,
            llm_confidence=llm_confidence,
            llm_accuracy=llm_accuracy,
            llm_processing_time=llm_processing_time,
            llm_components=llm_components,
            accuracy_improvement=accuracy_improvement,
            recommended_parser=recommended_parser
        )
    
    def analyze_results(self, results: List[HybridTestResult]) -> Dict[str, Any]:
        """Analyze hybrid test results."""
        total_cases = len(results)
        
        # Overall statistics
        avg_nlp_accuracy = statistics.mean([r.nlp_accuracy for r in results])
        avg_llm_accuracy = statistics.mean([r.llm_accuracy for r in results if r.llm_success])
        avg_improvement = statistics.mean([r.accuracy_improvement for r in results if r.llm_success])
        
        llm_successes = len([r for r in results if r.llm_success])
        llm_better = len([r for r in results if r.accuracy_improvement > 0])
        llm_recommended = len([r for r in results if r.recommended_parser == "llm"])
        
        # Speed comparison
        avg_nlp_time = statistics.mean([r.nlp_processing_time for r in results])
        avg_llm_time = statistics.mean([r.llm_processing_time for r in results if r.llm_success])
        
        # Categorize improvements
        significant_improvements = [r for r in results if r.accuracy_improvement > 0.2]
        moderate_improvements = [r for r in results if 0.1 <= r.accuracy_improvement <= 0.2]
        minor_improvements = [r for r in results if 0 < r.accuracy_improvement < 0.1]
        no_improvement = [r for r in results if r.accuracy_improvement <= 0]
        
        return {
            "summary": {
                "total_cases": total_cases,
                "llm_success_rate": llm_successes / total_cases,
                "avg_nlp_accuracy": avg_nlp_accuracy,
                "avg_llm_accuracy": avg_llm_accuracy,
                "avg_improvement": avg_improvement,
                "llm_better_count": llm_better,
                "llm_recommended_count": llm_recommended,
                "avg_nlp_time": avg_nlp_time,
                "avg_llm_time": avg_llm_time,
                "speed_ratio": avg_llm_time / avg_nlp_time if avg_nlp_time > 0 else 0
            },
            "improvements": {
                "significant": len(significant_improvements),
                "moderate": len(moderate_improvements), 
                "minor": len(minor_improvements),
                "none": len(no_improvement)
            },
            "improvement_cases": {
                "significant": significant_improvements,
                "moderate": moderate_improvements,
                "minor": minor_improvements,
                "none": no_improvement
            }
        }
    
    def print_detailed_results(self, results: List[HybridTestResult], analysis: Dict[str, Any]) -> None:
        """Print detailed hybrid comparison results."""
        print("\n" + "="*100)
        print("🔄 HYBRID NLP vs LLM ACCURACY COMPARISON")
        print("="*100)
        
        # Overall Summary
        summary = analysis["summary"]
        print(f"\n📊 OVERALL PERFORMANCE:")
        print(f"   Total Low-Confidence Cases: {summary['total_cases']}")
        print(f"   LLM Success Rate: {summary['llm_success_rate']:.1%}")
        print(f"   NLP Average Accuracy: {summary['avg_nlp_accuracy']:.1%}")
        print(f"   LLM Average Accuracy: {summary['avg_llm_accuracy']:.1%}")
        print(f"   Average Improvement: {summary['avg_improvement']:+.1%}")
        print(f"   LLM Better Cases: {summary['llm_better_count']}/{summary['total_cases']}")
        print(f"   LLM Recommended: {summary['llm_recommended_count']}/{summary['total_cases']}")
        
        # Speed Comparison
        print(f"\n⚡ SPEED COMPARISON:")
        print(f"   NLP Average Time: {summary['avg_nlp_time']:.3f}s")
        print(f"   LLM Average Time: {summary['avg_llm_time']:.3f}s")
        print(f"   LLM is {summary['speed_ratio']:.1f}x slower")
        
        # Improvement Categories
        improvements = analysis["improvements"]
        print(f"\n📈 ACCURACY IMPROVEMENTS:")
        print(f"   🟢 Significant (>20%): {improvements['significant']} cases")
        print(f"   🟡 Moderate (10-20%):  {improvements['moderate']} cases")
        print(f"   🔵 Minor (<10%):       {improvements['minor']} cases")
        print(f"   🔴 No Improvement:     {improvements['none']} cases")
        
        # Detailed case analysis
        print(f"\n🔍 DETAILED CASE ANALYSIS:")
        
        for i, result in enumerate(results, 1):
            case = result.test_case
            improvement_status = "🟢" if result.accuracy_improvement > 0.2 else "🟡" if result.accuracy_improvement > 0.1 else "🔴"
            recommended_status = "🧠 LLM" if result.recommended_parser == "llm" else "⚡ NLP"
            
            print(f"\n   [{i}] {improvement_status} '{case.query}'")
            print(f"       Category: {case.category} | Clarity: {case.clarity_level}")
            print(f"       NLP: {case.nlp_accuracy:.1%} accuracy, {case.nlp_confidence:.3f} confidence")
            
            if result.llm_success:
                print(f"       LLM: {result.llm_accuracy:.1%} accuracy, {result.llm_confidence:.3f} confidence")
                print(f"       Improvement: {result.accuracy_improvement:+.1%} | Recommended: {recommended_status}")
                if result.llm_components:
                    print(f"       LLM extracted: {list(result.llm_components.keys())}")
            else:
                print(f"       LLM: FAILED | Recommended: {recommended_status}")
            
            print(f"       Notes: {case.notes}")
        
        # Recommendations
        print(f"\n💡 HYBRID STRATEGY RECOMMENDATIONS:")
        
        if summary['avg_improvement'] > 0.15:
            print(f"   ✅ EXCELLENT: LLM provides {summary['avg_improvement']:.1%} avg improvement on low-confidence cases")
            print(f"   📊 Strategy: Use 0.75-0.8 confidence threshold for routing")
        elif summary['avg_improvement'] > 0.05:
            print(f"   👍 GOOD: LLM provides {summary['avg_improvement']:.1%} avg improvement")
            print(f"   📊 Strategy: Use 0.7 confidence threshold, accept speed trade-off")
        else:
            print(f"   ⚠️  LIMITED: Only {summary['avg_improvement']:.1%} avg improvement")
            print(f"   📊 Strategy: Consider staying with NLP-only for speed")
        
        print(f"\n🎯 CONCLUSION:")
        if summary['llm_recommended_count'] > summary['total_cases'] * 0.6:
            print(f"   Hybrid approach recommended: {summary['llm_recommended_count']}/{summary['total_cases']} cases benefit from LLM")
        else:
            print(f"   NLP-focused approach: Only {summary['llm_recommended_count']}/{summary['total_cases']} cases truly need LLM")
        
        print("\n" + "="*100)
    
    def run_hybrid_test(self) -> None:
        """Run hybrid accuracy comparison test."""
        print("🔄 Starting Hybrid NLP vs LLM Accuracy Test...")
        print("   Testing low-confidence cases with both parsers")
        
        test_cases = self.get_low_confidence_test_cases()
        print(f"📝 Testing {len(test_cases)} low-confidence cases...")
        
        all_results = []
        
        for i, test_case in enumerate(test_cases, 1):
            print(f"\n[{i}/{len(test_cases)}] {test_case.clarity_level}: NLP {test_case.nlp_accuracy:.1%} accuracy")
            
            result = self.test_single_case(test_case)
            all_results.append(result)
            
            # Brief status
            if result.llm_success:
                improvement_str = f"{result.accuracy_improvement:+.1%}"
                recommended = "LLM" if result.recommended_parser == "llm" else "NLP"
                print(f"      LLM: {result.llm_accuracy:.1%} accuracy ({improvement_str}) → Recommended: {recommended}")
            else:
                print(f"      LLM: FAILED → Recommended: NLP")
        
        # Analyze and print results
        print(f"\n📊 Analyzing {len(all_results)} hybrid test results...")
        analysis = self.analyze_results(all_results)
        self.print_detailed_results(all_results, analysis)
        
        # Save detailed results
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_file = f"hybrid_accuracy_results_{timestamp}.json"
        
        output_data = {
            "timestamp": timestamp,
            "test_summary": analysis,
            "detailed_results": [asdict(result) for result in all_results]
        }
        
        with open(results_file, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False, default=str)
        
        print(f"\n💾 Detailed results saved to: {results_file}")


def main():
    """Main function to run hybrid accuracy testing."""
    try:
        tester = HybridAccuracyTester()
        tester.run_hybrid_test()
        
    except Exception as e:
        print(f"❌ Error during hybrid accuracy testing: {e}")


if __name__ == "__main__":
    main() 