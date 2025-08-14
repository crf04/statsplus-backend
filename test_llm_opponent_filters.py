"""
Test Suite for LLM Fallback Effectiveness on Opponent Filter Queries

This script tests how effectively the LLM service handles opponent filter queries
that traditional NLP parsing cannot handle. It measures success rates, identifies
failure patterns, and provides comprehensive performance metrics.
"""

import asyncio
import json
import time
from typing import Dict, List, Tuple, Any, Optional
from dataclasses import dataclass
from app.services.llm_service import LLMService
from app.services.nl_query.parser import BaseQueryParser
from sqlalchemy import create_engine

@dataclass
class TestCase:
    """Represents a single test case for LLM fallback testing"""
    query: str
    category: str
    expected_player: str
    expected_opponent_filters: List[List]  # Expected opponent_filters format
    description: str
    difficulty: str  # 'easy', 'medium', 'hard'

@dataclass
class TestResult:
    """Results from testing a single query"""
    test_case: TestCase
    nlp_triggered_llm: bool
    llm_success: bool
    llm_response: Optional[Dict[str, Any]]
    extracted_player: Optional[str]
    extracted_opponent_filters: List[List]
    execution_time: float
    error_message: Optional[str]

class LLMOpponentFilterTestSuite:
    """Comprehensive test suite for LLM opponent filter effectiveness"""
    
    def __init__(self):
        self.engine = create_engine('sqlite:///nba_play_types.db')
        self.parser = BaseQueryParser(self.engine)
        self.llm_service = None
        self.test_cases = self._create_test_cases()
        
    def _create_test_cases(self) -> List[TestCase]:
        """Create comprehensive test cases covering various opponent filter scenarios"""
        return [
            # EASY - Clear opponent filter language
            TestCase(
                query="LeBron against elite defensive teams",
                category="Quality + Category",
                expected_player="LeBron James",
                expected_opponent_filters=[["Defensive Rating", 5]],
                description="Simple elite defense query",
                difficulty="easy"
            ),
            TestCase(
                query="Curry vs top rebounding teams", 
                category="Quality + Category",
                expected_player="Stephen Curry",
                expected_opponent_filters=[["OPP_REB", 5]],
                description="Top rebounding teams query",
                difficulty="easy"
            ),
            TestCase(
                query="Dame against strong offensive teams",
                category="Quality + Category", 
                expected_player="Damian Lillard",
                expected_opponent_filters=[["OPP_PTS", 5]],
                description="Strong offensive teams query",
                difficulty="easy"
            ),
            
            # MEDIUM - Specific shot types that failed in NLP
            TestCase(
                query="LeBron against pullup 2s teams",
                category="Specific Shot Type",
                expected_player="LeBron James", 
                expected_opponent_filters=[["PU 2s", 10]],
                description="Pullup 2s teams (previously failed)",
                difficulty="medium"
            ),
            TestCase(
                query="Curry vs pullup 3s teams",
                category="Specific Shot Type",
                expected_player="Stephen Curry",
                expected_opponent_filters=[["PU 3s", 10]], 
                description="Pullup 3s teams (previously failed)",
                difficulty="medium"
            ),
            TestCase(
                query="Tatum against catch and shoot 3 teams",
                category="Specific Shot Type",
                expected_player="Jayson Tatum",
                expected_opponent_filters=[["C&S 3s", 10]],
                description="Catch and shoot 3s teams",
                difficulty="medium"
            ),
            
            # MEDIUM - Playtypes
            TestCase(
                query="Jokic vs transition teams",
                category="Playtypes",
                expected_player="Nikola Jokic",
                expected_opponent_filters=[["Transition", 10]],
                description="Transition teams query",
                difficulty="medium"
            ),
            TestCase(
                query="Embiid against isolation teams", 
                category="Playtypes",
                expected_player="Joel Embiid",
                expected_opponent_filters=[["Isolation", 10]],
                description="Isolation teams query",
                difficulty="medium"
            ),
            
            # HARD - Complex/ambiguous language
            TestCase(
                query="LeBron games vs teams that struggle defensively",
                category="Complex Description",
                expected_player="LeBron James",
                expected_opponent_filters=[["OPP_PTS", -5]],  # Bottom defensive teams
                description="Complex defensive struggle description",
                difficulty="hard"
            ),
            TestCase(
                query="Curry against teams that give up lots of threes",
                category="Complex Description", 
                expected_player="Stephen Curry",
                expected_opponent_filters=[["OPP_FG3M", -5]],  # Teams allowing many 3s
                description="Complex three-point defense description",
                difficulty="hard"
            ),
            TestCase(
                query="Giannis vs bad rebounding teams",
                category="Complex Description",
                expected_player="Giannis Antetokounmpo", 
                expected_opponent_filters=[["OPP_REB", -5]],  # Bad rebounding teams
                description="Bad rebounding teams query",
                difficulty="hard"
            ),
            TestCase(
                query="Luka against teams weak on pick and roll defense",
                category="Complex Description",
                expected_player="Luka Doncic",
                expected_opponent_filters=[["PRBallHandler", -5]],
                description="Complex pick and roll defense description", 
                difficulty="hard"
            ),
            
            # HARD - Multiple filters
            TestCase(
                query="Kawhi vs elite defensive teams that allow pullups",
                category="Multiple Filters",
                expected_player="Kawhi Leonard",
                expected_opponent_filters=[["Defensive Rating", 5], ["PU PTS", -5]],
                description="Multiple opponent filter criteria",
                difficulty="hard"
            ),
            
            # EDGE CASES
            TestCase(
                query="Steph against teams ranked top 3 in defense",
                category="Numeric Ranking",
                expected_player="Stephen Curry", 
                expected_opponent_filters=[["Defensive Rating", 3]],
                description="Specific numeric ranking",
                difficulty="medium"
            ),
            TestCase(
                query="KD vs worst 8 teams defensively",
                category="Numeric Ranking",
                expected_player="Kevin Durant",
                expected_opponent_filters=[["Defensive Rating", -8]],
                description="Worst N teams query",
                difficulty="medium"
            )
        ]
    
    async def run_comprehensive_test(self) -> Dict[str, Any]:
        """Run the complete test suite and return comprehensive results"""
        print("🧪 Starting LLM Opponent Filter Test Suite")
        print("=" * 60)
        
        # Initialize LLM service
        try:
            self.llm_service = LLMService()
            print("✅ LLM Service initialized")
        except Exception as e:
            print(f"❌ LLM Service initialization failed: {e}")
            return {"error": "LLM Service unavailable", "results": []}
        
        results = []
        
        # Test each case
        for i, test_case in enumerate(self.test_cases, 1):
            print(f"\n[{i}/{len(self.test_cases)}] Testing: {test_case.query}")
            print(f"    Expected: {test_case.expected_player} | {test_case.expected_opponent_filters}")
            
            result = await self._test_single_case(test_case)
            results.append(result)
            
            # Print immediate result
            if result.llm_success:
                print(f"    ✅ SUCCESS: {result.extracted_player} | {result.extracted_opponent_filters}")
            else:
                print(f"    ❌ FAILED: {result.error_message}")
        
        # Analyze results
        analysis = self._analyze_results(results)
        
        # Print comprehensive report
        self._print_comprehensive_report(results, analysis)
        
        return {
            "results": results,
            "analysis": analysis,
            "total_tests": len(results),
            "timestamp": time.time()
        }
    
    async def _test_single_case(self, test_case: TestCase) -> TestResult:
        """Test a single query case"""
        start_time = time.time()
        
        try:
            # Step 1: Check if NLP triggers LLM
            nlp_components = self.parser.parse(test_case.query)
            nlp_triggered_llm = nlp_components.confidence_breakdown.should_use_llm
            
            if not nlp_triggered_llm:
                return TestResult(
                    test_case=test_case,
                    nlp_triggered_llm=False,
                    llm_success=False,
                    llm_response=None,
                    extracted_player=nlp_components.player_name,
                    extracted_opponent_filters=nlp_components.opponent_filters,
                    execution_time=time.time() - start_time,
                    error_message="NLP did not trigger LLM fallback"
                )
            
            # Step 2: Test LLM directly
            llm_result = self.llm_service.parse_query(test_case.query)
            
            if not llm_result:
                return TestResult(
                    test_case=test_case,
                    nlp_triggered_llm=nlp_triggered_llm,
                    llm_success=False,
                    llm_response=None,
                    extracted_player=None,
                    extracted_opponent_filters=[],
                    execution_time=time.time() - start_time,
                    error_message="LLM returned no result"
                )
            
            # Step 3: Evaluate LLM response
            extracted_player = llm_result.player_name
            extracted_opponent_filters = llm_result.opponent_filters
            
            # Check success criteria
            player_match = self._normalize_player_name(extracted_player) == self._normalize_player_name(test_case.expected_player)
            filter_match = self._evaluate_opponent_filters(extracted_opponent_filters, test_case.expected_opponent_filters)
            
            llm_success = player_match and filter_match
            
            return TestResult(
                test_case=test_case,
                nlp_triggered_llm=nlp_triggered_llm,
                llm_success=llm_success,
                llm_response=llm_result.__dict__ if hasattr(llm_result, '__dict__') else str(llm_result),
                extracted_player=extracted_player,
                extracted_opponent_filters=extracted_opponent_filters,
                execution_time=time.time() - start_time,
                error_message=None if llm_success else f"Player: {player_match}, Filters: {filter_match}"
            )
            
        except Exception as e:
            return TestResult(
                test_case=test_case,
                nlp_triggered_llm=nlp_triggered_llm if 'nlp_triggered_llm' in locals() else False,
                llm_success=False,
                llm_response=None,
                extracted_player=None,
                extracted_opponent_filters=[],
                execution_time=time.time() - start_time,
                error_message=f"Exception: {str(e)}"
            )
    
    def _normalize_player_name(self, name: Optional[str]) -> str:
        """Normalize player names for comparison"""
        if not name:
            return ""
        return name.lower().replace(".", "").replace(" ", "")
    
    def _evaluate_opponent_filters(self, actual: List[List], expected: List[List]) -> bool:
        """Evaluate if opponent filters match expectations (flexible matching)"""
        if not actual and not expected:
            return True
        
        if not actual or not expected:
            return False
        
        # Convert to sets for comparison (order doesn't matter)
        actual_set = {tuple(f) for f in actual}
        expected_set = {tuple(f) for f in expected}
        
        # Check for partial matches (at least one filter matches)
        return len(actual_set.intersection(expected_set)) > 0
    
    def _analyze_results(self, results: List[TestResult]) -> Dict[str, Any]:
        """Analyze test results and generate comprehensive metrics"""
        total_tests = len(results)
        nlp_triggers = sum(1 for r in results if r.nlp_triggered_llm)
        llm_successes = sum(1 for r in results if r.llm_success)
        
        # Group by difficulty
        by_difficulty = {}
        for result in results:
            diff = result.test_case.difficulty
            if diff not in by_difficulty:
                by_difficulty[diff] = {"total": 0, "success": 0}
            by_difficulty[diff]["total"] += 1
            if result.llm_success:
                by_difficulty[diff]["success"] += 1
        
        # Group by category
        by_category = {}
        for result in results:
            cat = result.test_case.category
            if cat not in by_category:
                by_category[cat] = {"total": 0, "success": 0}
            by_category[cat]["total"] += 1
            if result.llm_success:
                by_category[cat]["success"] += 1
        
        # Failure analysis
        failures = [r for r in results if not r.llm_success]
        failure_reasons = {}
        for failure in failures:
            reason = failure.error_message or "Unknown"
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
        
        return {
            "overall": {
                "total_tests": total_tests,
                "nlp_triggered_llm": nlp_triggers,
                "nlp_trigger_rate": nlp_triggers / total_tests if total_tests > 0 else 0,
                "llm_successes": llm_successes,
                "llm_success_rate": llm_successes / total_tests if total_tests > 0 else 0,
                "avg_execution_time": sum(r.execution_time for r in results) / total_tests if total_tests > 0 else 0
            },
            "by_difficulty": {k: {**v, "success_rate": v["success"] / v["total"] if v["total"] > 0 else 0} for k, v in by_difficulty.items()},
            "by_category": {k: {**v, "success_rate": v["success"] / v["total"] if v["total"] > 0 else 0} for k, v in by_category.items()},
            "failure_analysis": failure_reasons,
            "failures": [{"query": f.test_case.query, "reason": f.error_message} for f in failures]
        }
    
    def _print_comprehensive_report(self, results: List[TestResult], analysis: Dict[str, Any]):
        """Print detailed test report"""
        print("\n" + "=" * 80)
        print("🏁 LLM OPPONENT FILTER TEST SUITE RESULTS")
        print("=" * 80)
        
        # Overall metrics
        overall = analysis["overall"]
        print(f"\n📊 OVERALL PERFORMANCE:")
        print(f"   Total Tests: {overall['total_tests']}")
        print(f"   NLP Triggered LLM: {overall['nlp_triggered_llm']}/{overall['total_tests']} ({overall['nlp_trigger_rate']:.1%})")
        print(f"   LLM Success Rate: {overall['llm_successes']}/{overall['total_tests']} ({overall['llm_success_rate']:.1%})")
        print(f"   Avg Execution Time: {overall['avg_execution_time']:.3f}s")
        
        # By difficulty
        print(f"\n📈 SUCCESS RATE BY DIFFICULTY:")
        for difficulty, stats in analysis["by_difficulty"].items():
            print(f"   {difficulty.upper()}: {stats['success']}/{stats['total']} ({stats['success_rate']:.1%})")
        
        # By category
        print(f"\n📋 SUCCESS RATE BY CATEGORY:")
        for category, stats in analysis["by_category"].items():
            print(f"   {category}: {stats['success']}/{stats['total']} ({stats['success_rate']:.1%})")
        
        # Failure analysis
        if analysis["failure_analysis"]:
            print(f"\n❌ FAILURE ANALYSIS:")
            for reason, count in analysis["failure_analysis"].items():
                print(f"   {reason}: {count} cases")
        
        # Individual failures
        if analysis["failures"]:
            print(f"\n🔍 FAILED QUERIES:")
            for failure in analysis["failures"][:5]:  # Show first 5
                print(f"   \"{failure['query']}\" - {failure['reason']}")
            if len(analysis["failures"]) > 5:
                print(f"   ... and {len(analysis['failures']) - 5} more")
        
        print("\n" + "=" * 80)

async def main():
    """Run the LLM opponent filter test suite"""
    test_suite = LLMOpponentFilterTestSuite()
    results = await test_suite.run_comprehensive_test()
    
    # Save results to file
    with open('llm_opponent_filter_test_results.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"\n💾 Results saved to: llm_opponent_filter_test_results.json")

if __name__ == "__main__":
    asyncio.run(main())