#!/usr/bin/env python3
"""
Comprehensive LLM Testing Script for NBA Query Processing

This script extracts test queries from the comprehensive test suite and runs them
through the LLM service to evaluate performance and identify strengths/weaknesses.
"""

import os
import sys
import json
import time
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.llm_service import LLMService, LLMError
from config import Config


@dataclass
class LLMTestResult:
    """Result of an LLM test execution."""
    
    query: str
    category: str
    success: bool
    llm_response: Dict[str, Any]
    parsed_correctly: bool
    confidence_score: float
    field_confidence: Dict[str, float]
    extracted_components: Dict[str, Any]
    errors: List[str]
    processing_time: float
    usage_stats: Optional[Dict[str, Any]] = None


class ComprehensiveLLMTester:
    """Comprehensive LLM testing using queries from the test suite."""
    
    def __init__(self):
        """Initialize the LLM tester."""
        try:
            self.llm_service = LLMService()
            print(f"✅ LLM Service initialized successfully")
        except LLMError as e:
            print(f"❌ Failed to initialize LLM Service: {e}")
            raise
    
    def get_comprehensive_test_queries(self) -> Dict[str, List[str]]:
        """
        Get comprehensive test queries organized by category.
        
        Returns:
            Dict mapping category names to lists of test queries
        """
        return {
            # Multi-filter stress combinations
            "maximum_complexity": [
                "LeBron last 15 home games with AD and Russ but without Dwight Powell where he scores 30+ points and 8+ rebounds and 6+ assists and plays 35+ minutes against top 5 defenses",
                "Curry past 20 away games with Klay and Draymond where he makes 8+ threes and 25+ points and less than 4 turnovers and plays between 32 and 40 minutes against worst 10 three point defenses",
                "Giannis recent 12 road games without Dame but with Brook Lopez where he shoots 15+ field goals and makes 10+ free throws and gets double-digit rebounds and 5+ assists with 30+ minutes",
                "Luka last 10 games at home with Kyrie and Dwight Powell but without Tim Hardaway where he scores between 25 and 40 points and attempts 20+ shots and makes 4+ threes with exactly 35 minutes",
                "Tatum past 8 away games with Brown but without Smart where he scores 25+ points and shoots 12+ field goals and makes 3+ threes and gets 8+ rebounds against top 15 defenses with 32+ minutes"
            ],
            
            # Nested player relationships
            "player_relationships": [
                "LeBron with AD and Russ but without Dwight and THJ last 10 games",
                "Curry with Klay, Draymond, and Wiggins but without Poole and Wiseman this season",
                "Giannis with Dame and Brook but without Khris and Bobby at home",
                "Luka with Kyrie, Christian Wood, and Josh Green but without Tim Hardaway Jr and Maxi Kleber on the road",
                "Tatum with Brown, Smart, Horford, and Williams but without Grant Williams recent 15 games",
                "KD with Kyrie and Nic Claxton but without Ben Simmons and Joe Harris where he scores 30+ points"
            ],
            
            # Multi-stat combinations
            "statistical_filters": [
                "LeBron games where he scores 25+ points and gets 10+ rebounds and 8+ assists",
                "Westbrook games where he gets double-digit points and double-digit rebounds and double-digit assists",
                "Curry games where he attempts 15+ field goals and makes 8+ threes and scores 30+ points",
                "Dame games where he takes 20+ shots and makes 12+ field goals and attempts 10+ threes",
                "Giannis games where he gets 12+ rebounds and 2+ blocks and 1+ steals and scores 25+ points",
                "AD games where he blocks 3+ shots and gets 10+ defensive rebounds and scores 20+ points",
                "Jokic games where he plays 35+ minutes and scores 25+ points and gets 12+ rebounds and 8+ assists",
                "Embiid games where he plays between 30 and 38 minutes and scores 28+ points and blocks 2+ shots"
            ],
            
            # Edge cases and ambiguity
            "edge_cases": [
                "LeBron games where he wears number 6 and scores 30+ points",
                "Curry games with number 30 where he makes 8+ threes",
                "Giannis last 10 games where he scores exactly 34 points",
                "Dame recent 15 games where he plays 35+ minutes",
                "Luka last 20 games where he scores between 25 and 35 points and plays 32+ minutes"
            ],
            
            # Similar player names
            "name_disambiguation": [
                "Kevin Durant with Kyrie Irving last 10 games",
                "Anthony Davis with Anthony Edwards recent 5 games",
                "LeBron James with King James last 15 games",
                "Stephen Curry with Steph Curry this season",
                "Brown with Tatum last 10 games",
                "Green with Curry at home"
            ],
            
            # Statistical term confusion
            "term_confusion": [
                "LeBron games with exactly 28 points and approximately 10 rebounds",
                "Curry games with around 25 points and nearly 8 threes",
                "Giannis games with over 30 points and under 5 turnovers",
                "Dame games with between 20 and 30 points and more than 8 assists",
                "Luka games with precisely 35 points and roughly 12 rebounds",
                "Tatum games with about 25 points and close to 8 rebounds"
            ],
            
            # Fantasy basketball queries
            "fantasy_analysis": [
                "LeBron last 5 games at home with AD where he scores 25+ points and gets 8+ rebounds and 6+ assists with 35+ minutes",
                "Curry away games against top 10 defenses where he makes 6+ threes and scores 28+ points",
                "Giannis games without Dame where he scores 30+ points and gets 12+ rebounds with 36+ minutes",
                "Alperen Sengun recent 10 games where he scores 15+ points and gets 8+ rebounds and 5+ assists with less than 32 minutes",
                "Luka home games with Kyrie where he scores between 35 and 50 points and gets 10+ rebounds"
            ],
            
            # Analyst research queries
            "analyst_queries": [
                "KD games where he scores 30+ points and plays 35+ minutes against top 5 defenses on the road",
                "Tatum with Brown and Smart but without Williams where he scores 25+ points and gets 8+ rebounds",
                "Dame against worst 8 three point defenses where he attempts 12+ threes and makes 6+ threes",
                "Kawhi games where he plays between 25 and 35 minutes and scores 22+ points with 6+ rebounds",
                "Victor Wembanyama recent 15 games where he blocks 2+ shots and scores 18+ points and gets 10+ rebounds"
            ],
            
            # Casual fan queries
            "casual_queries": [
                "LeBron last 10 games with 30+ points",
                "Curry this season with 8+ threes",
                "Giannis home games with double-digit rebounds",
                "Luka recent games where he scores more than 25 points",
                "Dame games where he makes over 6 threes",
                "Tatum with Brown last 15 games where he scores 20+ points"
            ],
            
            # Stress and performance
            "stress_tests": [
                "LeBron James last twenty games at home during this season with Anthony Davis and Russell Westbrook but without Dwight Howard and Kendrick Nunn where he scores at least thirty points and gets a minimum of eight rebounds and distributes six or more assists and plays between thirty-five and forty-two minutes and attempts fifteen or more field goals and makes at least twelve field goals and shoots over eighty percent from the free throw line against top ten defensive teams",
                "Stephen Curry past fifteen away games this season with Klay Thompson, Draymond Green, and Andrew Wiggins but without Jordan Poole and James Wiseman where he makes eight or more three pointers and scores twenty-five or more points and attempts at least fifteen three point shots and plays more than thirty minutes and has fewer than four turnovers against bottom five three point defensive teams",
                "LeBron with AD, Russ, Dwight, Austin Reaves, and Malik Monk but without THT and Kendrick Nunn last 10 games",
                "Curry with Klay, Draymond, Wiggins, Looney, Poole, and Kuminga but without Wiseman and Moody this season"
            ]
        }
    
    def test_single_query(self, query: str, category: str) -> LLMTestResult:
        """
        Test a single query with the LLM service.
        
        Args:
            query: The natural language query
            category: Category of the test
            
        Returns:
            LLMTestResult with detailed analysis
        """
        start_time = time.time()
        errors = []
        
        try:
            # Query the LLM
            llm_response = self.llm_service.query_llm(query)
            processing_time = time.time() - start_time
            
            if not llm_response["success"]:
                return LLMTestResult(
                    query=query,
                    category=category,
                    success=False,
                    llm_response=llm_response,
                    parsed_correctly=False,
                    confidence_score=0.0,
                    field_confidence={},
                    extracted_components={},
                    errors=[llm_response.get("error", "Unknown LLM error")],
                    processing_time=processing_time
                )
            
            # Extract components from LLM response
            content = llm_response["content"]
            
            # Check if parsing was successful
            parsed_correctly = True
            if isinstance(content, dict) and "parsing_error" not in content:
                confidence_score = content.get("confidence", 0.0)
                field_confidence = content.get("field_confidence", {})
                extracted_components = {k: v for k, v in content.items() if k not in ["confidence", "field_confidence"]}
            else:
                parsed_correctly = False
                confidence_score = 0.0
                field_confidence = {}
                extracted_components = {}
                errors.append("Failed to parse LLM response as structured JSON")
            
            return LLMTestResult(
                query=query,
                category=category,
                success=True,
                llm_response=llm_response,
                parsed_correctly=parsed_correctly,
                confidence_score=confidence_score,
                field_confidence=field_confidence,
                extracted_components=extracted_components,
                errors=errors,
                processing_time=processing_time,
                usage_stats=llm_response.get("usage")
            )
            
        except Exception as e:
            processing_time = time.time() - start_time
            return LLMTestResult(
                query=query,
                category=category,
                success=False,
                llm_response={},
                parsed_correctly=False,
                confidence_score=0.0,
                field_confidence={},
                extracted_components={},
                errors=[f"Exception during testing: {str(e)}"],
                processing_time=processing_time
            )
    
    def analyze_results(self, results: List[LLMTestResult]) -> Dict[str, Any]:
        """
        Analyze test results and generate comprehensive report.
        
        Args:
            results: List of test results
            
        Returns:
            Dict containing detailed analysis
        """
        total_queries = len(results)
        successful_queries = len([r for r in results if r.success])
        parsed_correctly = len([r for r in results if r.parsed_correctly])
        
        # Category analysis
        category_stats = {}
        for result in results:
            if result.category not in category_stats:
                category_stats[result.category] = {
                    "total": 0,
                    "successful": 0,
                    "parsed_correctly": 0,
                    "avg_confidence": 0.0,
                    "avg_processing_time": 0.0,
                    "confidence_scores": []
                }
            
            stats = category_stats[result.category]
            stats["total"] += 1
            if result.success:
                stats["successful"] += 1
            if result.parsed_correctly:
                stats["parsed_correctly"] += 1
                stats["confidence_scores"].append(result.confidence_score)
        
        # Calculate averages
        for category, stats in category_stats.items():
            if stats["confidence_scores"]:
                stats["avg_confidence"] = sum(stats["confidence_scores"]) / len(stats["confidence_scores"])
            category_results = [r for r in results if r.category == category]
            if category_results:
                stats["avg_processing_time"] = sum(r.processing_time for r in category_results) / len(category_results)
        
        # Component extraction analysis
        component_analysis = {}
        components = ["player_name", "team_name", "game_count", "date_range", "opponent_filters", 
                     "location", "minutes_filter", "self_filters", "players_on", "players_off", "intent"]
        
        for component in components:
            extracted_count = 0
            total_with_component = 0
            confidence_scores = []
            
            for result in results:
                if result.parsed_correctly:
                    # Check if component was extracted (not null/empty)
                    value = result.extracted_components.get(component)
                    if value is not None and value != [] and value != "":
                        extracted_count += 1
                    
                    # Count field confidence if available
                    field_conf = result.field_confidence.get(component)
                    if field_conf is not None:
                        confidence_scores.append(field_conf)
                        total_with_component += 1
            
            component_analysis[component] = {
                "extraction_count": extracted_count,
                "extraction_rate": extracted_count / parsed_correctly if parsed_correctly > 0 else 0.0,
                "avg_field_confidence": sum(confidence_scores) / len(confidence_scores) if confidence_scores else 0.0,
                "confidence_samples": len(confidence_scores)
            }
        
        return {
            "summary": {
                "total_queries": total_queries,
                "successful_queries": successful_queries,
                "success_rate": successful_queries / total_queries if total_queries > 0 else 0.0,
                "parsed_correctly": parsed_correctly,
                "parsing_success_rate": parsed_correctly / total_queries if total_queries > 0 else 0.0,
                "avg_confidence": sum(r.confidence_score for r in results if r.parsed_correctly) / parsed_correctly if parsed_correctly > 0 else 0.0,
                "avg_processing_time": sum(r.processing_time for r in results) / total_queries if total_queries > 0 else 0.0
            },
            "category_breakdown": category_stats,
            "component_analysis": component_analysis,
            "failed_queries": [
                {"query": r.query, "category": r.category, "errors": r.errors}
                for r in results if not r.success or not r.parsed_correctly
            ]
        }
    
    def print_detailed_results(self, results: List[LLMTestResult], analysis: Dict[str, Any]) -> None:
        """Print detailed test results with analysis."""
        print("\n" + "="*100)
        print("🤖 COMPREHENSIVE LLM TESTING RESULTS")
        print("="*100)
        
        # Overall Summary
        summary = analysis["summary"]
        print(f"\n📊 OVERALL PERFORMANCE:")
        print(f"   Total Queries: {summary['total_queries']}")
        print(f"   Success Rate: {summary['success_rate']:.1%} ({summary['successful_queries']}/{summary['total_queries']})")
        print(f"   Parsing Success: {summary['parsing_success_rate']:.1%} ({summary['parsed_correctly']}/{summary['total_queries']})")
        print(f"   Average Confidence: {summary['avg_confidence']:.3f}")
        print(f"   Average Processing Time: {summary['avg_processing_time']:.2f}s")
        
        # Category Breakdown
        print(f"\n📈 PERFORMANCE BY CATEGORY:")
        for category, stats in analysis["category_breakdown"].items():
            success_rate = stats["successful"] / stats["total"] if stats["total"] > 0 else 0.0
            parsing_rate = stats["parsed_correctly"] / stats["total"] if stats["total"] > 0 else 0.0
            
            status = "🟢" if success_rate >= 0.9 else "🟡" if success_rate >= 0.7 else "🔴"
            print(f"   {status} {category.replace('_', ' ').title():<25} "
                  f"Success: {success_rate:.1%} | "
                  f"Parsing: {parsing_rate:.1%} | "
                  f"Confidence: {stats['avg_confidence']:.3f} | "
                  f"Time: {stats['avg_processing_time']:.2f}s")
        
        # Component Analysis
        print(f"\n🔍 COMPONENT EXTRACTION ANALYSIS:")
        for component, stats in analysis["component_analysis"].items():
            status = "🟢" if stats["extraction_rate"] >= 0.8 else "🟡" if stats["extraction_rate"] >= 0.5 else "🔴"
            print(f"   {status} {component.replace('_', ' ').title():<20} "
                  f"Extracted: {stats['extraction_rate']:.1%} "
                  f"({stats['extraction_count']} times) | "
                  f"Field Confidence: {stats['avg_field_confidence']:.3f}")
        
        # Failed Queries
        if analysis["failed_queries"]:
            print(f"\n❌ FAILED QUERIES ({len(analysis['failed_queries'])}):")
            for i, failure in enumerate(analysis["failed_queries"][:10], 1):  # Show first 10
                print(f"   {i}. [{failure['category']}] {failure['query'][:80]}...")
                if failure["errors"]:
                    print(f"      Error: {failure['errors'][0]}")
        
        # Top Performing Queries
        successful_results = [r for r in results if r.parsed_correctly]
        if successful_results:
            top_confidence = sorted(successful_results, key=lambda x: x.confidence_score, reverse=True)[:5]
            print(f"\n🏆 TOP CONFIDENCE QUERIES:")
            for i, result in enumerate(top_confidence, 1):
                print(f"   {i}. [{result.category}] Confidence: {result.confidence_score:.3f}")
                print(f"      Query: {result.query[:80]}...")
        
        print("\n" + "="*100)
    
    def run_comprehensive_test(self, max_queries_per_category: Optional[int] = None) -> None:
        """
        Run comprehensive LLM testing.
        
        Args:
            max_queries_per_category: Limit queries per category for faster testing
        """
        print("🚀 Starting Comprehensive LLM Testing...")
        
        test_queries = self.get_comprehensive_test_queries()
        all_results = []
        
        total_queries = sum(len(queries) for queries in test_queries.values())
        if max_queries_per_category:
            total_queries = min(total_queries, len(test_queries) * max_queries_per_category)
        
        print(f"📝 Testing {total_queries} queries across {len(test_queries)} categories...")
        
        query_count = 0
        for category, queries in test_queries.items():
            print(f"\n🔄 Testing {category} ({len(queries)} queries)...")
            
            # Limit queries if specified
            test_set = queries[:max_queries_per_category] if max_queries_per_category else queries
            
            for i, query in enumerate(test_set, 1):
                query_count += 1
                print(f"   [{query_count}/{total_queries}] Testing: {query[:60]}...")
                
                result = self.test_single_query(query, category)
                all_results.append(result)
                
                # Brief status
                status = "✅" if result.success and result.parsed_correctly else "❌"
                conf = f"(conf: {result.confidence_score:.3f})" if result.parsed_correctly else ""
                print(f"      {status} {conf}")
        
        # Analyze and print results
        print(f"\n📊 Analyzing {len(all_results)} test results...")
        analysis = self.analyze_results(all_results)
        self.print_detailed_results(all_results, analysis)
        
        # Save detailed results to file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_file = f"llm_test_results_{timestamp}.json"
        
        output_data = {
            "timestamp": timestamp,
            "test_summary": analysis,
            "detailed_results": [asdict(result) for result in all_results]
        }
        
        with open(results_file, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
        
        print(f"\n💾 Detailed results saved to: {results_file}")


def main():
    """Main function to run comprehensive LLM testing."""
    try:
        tester = ComprehensiveLLMTester()
        
        # Check if API key is available
        if not os.getenv('OPENAI_API_KEY'):
            print("❌ OpenAI API key not found in environment variables")
            print("Please set OPENAI_API_KEY before running LLM tests")
            return
        
        # Run comprehensive test
        # Use max_queries_per_category=3 for quick testing, None for full test
        tester.run_comprehensive_test(max_queries_per_category=None)
        
    except LLMError as e:
        print(f"❌ LLM Service Error: {e}")
    except KeyboardInterrupt:
        print("\n⏹️  Testing interrupted by user")
    except Exception as e:
        print(f"❌ Unexpected error: {e}")


if __name__ == "__main__":
    main() 