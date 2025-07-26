#!/usr/bin/env python3
"""
Comprehensive Opponent Filter Test

Test all opponent filter types with various phrasings to validate
the LLM's understanding of all defensive categories.
"""

import os
import sys
import time
import json
from typing import List, Dict, Any, Tuple

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.llm_service import LLMService


def get_comprehensive_opponent_filter_queries() -> Dict[str, List[Tuple[str, str, int]]]:
    """
    Get comprehensive opponent filter test queries.
    
    Returns:
        Dict mapping filter categories to lists of (query, expected_filter, expected_rank) tuples
    """
    return {
        # Shooting Defense Categories
        "catch_and_shoot": [
            ("Curry against top 5 catch and shoot defenses", "C&S PTS", 5),
            ("Dame vs worst 8 catch-and-shoot teams", "C&S PTS", -8),
            ("Klay against bottom 3 catch and shoot defenses", "C&S PTS", -3),
            ("Harden vs elite catch-and-shoot defense teams", "C&S PTS", 10),
        ],
        
        "pull_up_shooting": [
            ("Luka against top 6 pull-up defenses", "PU PTS", 6),
            ("Dame vs worst pull-up shooting defenses", "PU PTS", -10),
            ("Curry against bottom 5 pull-up teams", "PU PTS", -5),
        ],
        
        "three_point_defense": [
            ("Curry vs top 10 three point defenses", "OPP_FG3M", 10),
            ("Dame against worst 8 three point defenses", "OPP_FG3M", -8),
            ("Klay vs bottom 5 three-point defensive teams", "OPP_FG3M", -5),
            ("Harden against elite three point defense", "OPP_FG3M", 10),
        ],
        
        # Play-Type Defense Categories  
        "transition": [
            ("LeBron vs top 5 transition defenses", "Transition", 5),
            ("Giannis against worst transition teams", "Transition", -10),
            ("Westbrook vs bottom 3 fast-break defenses", "Transition", -3),
            ("Fox against elite transition defense", "Transition", 10),
        ],
        
        "isolation": [
            ("KD vs top 7 isolation defenses", "Isolation", 7),
            ("Luka against worst iso defenses", "Isolation", -10),
            ("Tatum vs bottom 4 isolation teams", "Isolation", -4),
        ],
        
        "post_up": [
            ("Embiid vs top 5 post-up defenses", "Postup", 5),
            ("Jokic against worst post defenses", "Postup", -10),
            ("AD vs bottom 3 post-up teams", "Postup", -3),
        ],
        
        "pick_and_roll": [
            ("Dame vs top 6 pick and roll ball handler defenses", "PRBallHandler", 6),
            ("Luka against worst pick-and-roll defenses", "PRBallHandler", -10),
            ("Giannis vs top 5 pick and roll roll man defenses", "PRRollMan", 5),
        ],
        
        # Advanced Defense Categories
        "spot_up": [
            ("Curry vs top 8 spot-up defenses", "Spotup", 8),
            ("Dame against worst spot up teams", "Spotup", -10),
        ],
        
        "handoff": [
            ("Curry vs top 5 handoff defenses", "Handoff", 5),
            ("Dame against worst dribble handoff teams", "Handoff", -8),
        ],
        
        "off_screen": [
            ("Klay vs top 6 off-screen defenses", "OffScreen", 6),
            ("Curry against worst off screen teams", "OffScreen", -7),
        ],
        
        "cutting": [
            ("LeBron vs top 5 cutting defenses", "Cut", 5),
            ("Giannis against worst cutting teams", "Cut", -8),
        ],
        
        "paint_protection": [
            ("Giannis vs top 5 paint defenses", "Less Than 10 ft", 5),
            ("Zion against worst paint protection teams", "Less Than 10 ft", -6),
        ],
        
        # Overall Defense Categories
        "overall_defense": [
            ("LeBron vs top 5 defenses", "OPP_PTS", 5),
            ("KD against top 10 defensive teams", "OPP_PTS", 10),
            ("Curry vs worst defenses", "OPP_PTS", -10),
            ("Dame against bottom 7 defensive teams", "OPP_PTS", -7),
        ],
        
        "rebounding_defense": [
            ("Giannis vs top 5 defensive rebounding teams", "OPP_REB", 5),
            ("Jokic against worst rebounding defenses", "OPP_REB", -8),
        ],
        
        "assist_defense": [
            ("Luka vs top 6 teams that limit assists", "OPP_AST", 6),
            ("Harden against teams that allow most assists", "OPP_AST", -10),
        ],
        
        "turnover_defense": [
            ("Curry vs teams that force most turnovers", "OPP_TOV", 5),
            ("Dame against teams that don't force turnovers", "OPP_TOV", -8),
        ],
        
        "fouling": [
            ("Harden vs teams that foul least", "OPP_FTA", 5),
            ("Embiid against teams that foul most", "OPP_FTA", -7),
        ],
        
        "stocks_defense": [
            ("LeBron vs top 5 teams in steals and blocks", "OPP_STOCKS", 5),
            ("KD against teams with worst steal/block defense", "OPP_STOCKS", -6),
        ]
    }


def test_opponent_filter_category(llm_service: LLMService, category: str, queries: List[Tuple[str, str, int]]) -> Dict[str, Any]:
    """Test a specific opponent filter category."""
    
    print(f"\n🎯 Testing {category.replace('_', ' ').title()} ({len(queries)} queries)")
    
    results = []
    success_count = 0
    total_time = 0
    
    for i, (query, expected_filter, expected_rank) in enumerate(queries, 1):
        print(f"   [{i}/{len(queries)}] {query}")
        
        start_time = time.time()
        result = llm_service.test_prompt_file("prompts/system_prompt_optimized.txt", query)
        processing_time = time.time() - start_time
        total_time += processing_time
        
        success = False
        extracted_filter = None
        extracted_rank = None
        confidence = 0.0
        
        if result.get("success"):
            content = result.get("content", {})
            opponent_filters = content.get("opponent_filters", [])
            confidence = content.get("confidence", 0.0)
            
            if opponent_filters and len(opponent_filters) > 0:
                extracted_filter, extracted_rank = opponent_filters[0]
                
                # Check if extraction matches expectation
                filter_match = extracted_filter == expected_filter
                rank_match = extracted_rank == expected_rank
                
                if filter_match and rank_match:
                    success = True
                    print(f"      ✅ PERFECT: [{extracted_filter}, {extracted_rank}] (conf: {confidence:.3f})")
                elif filter_match:
                    success = True
                    print(f"      🟡 FILTER OK: [{extracted_filter}, {extracted_rank}] vs expected [{expected_filter}, {expected_rank}]")
                else:
                    print(f"      ❌ WRONG FILTER: [{extracted_filter}, {extracted_rank}] vs expected [{expected_filter}, {expected_rank}]")
            else:
                print(f"      ❌ NO FILTER EXTRACTED (conf: {confidence:.3f})")
        else:
            print(f"      ❌ LLM FAILED: {result.get('error', 'Unknown error')}")
        
        results.append({
            "query": query,
            "expected_filter": expected_filter,
            "expected_rank": expected_rank,
            "extracted_filter": extracted_filter,
            "extracted_rank": extracted_rank,
            "success": success,
            "confidence": confidence,
            "processing_time": processing_time
        })
        
        if success:
            success_count += 1
    
    avg_time = total_time / len(queries) if queries else 0
    success_rate = success_count / len(queries) if queries else 0
    
    print(f"   📊 Results: {success_rate:.1%} success ({success_count}/{len(queries)}) | Avg: {avg_time:.2f}s")
    
    return {
        "category": category,
        "success_count": success_count,
        "total_queries": len(queries),
        "success_rate": success_rate,
        "avg_time": avg_time,
        "results": results
    }


def main():
    """Run comprehensive opponent filter testing."""
    
    if not os.getenv('OPENAI_API_KEY'):
        print("❌ OpenAI API key not found.")
        return
    
    print("🎯 COMPREHENSIVE OPPONENT FILTER TEST")
    print("Testing all opponent filter types with various phrasings...")
    
    llm_service = LLMService()
    test_queries = get_comprehensive_opponent_filter_queries()
    
    all_results = []
    total_success = 0
    total_queries = 0
    total_time = 0
    
    # Test each category
    for category, queries in test_queries.items():
        category_result = test_opponent_filter_category(llm_service, category, queries)
        all_results.append(category_result)
        
        total_success += category_result["success_count"]
        total_queries += category_result["total_queries"]
        total_time += category_result["avg_time"] * category_result["total_queries"]
    
    # Overall summary
    overall_success_rate = total_success / total_queries if total_queries > 0 else 0
    overall_avg_time = total_time / total_queries if total_queries > 0 else 0
    
    print(f"\n" + "="*80)
    print(f"📊 OVERALL OPPONENT FILTER TEST RESULTS")
    print(f"="*80)
    print(f"Total Queries: {total_queries}")
    print(f"Overall Success Rate: {overall_success_rate:.1%} ({total_success}/{total_queries})")
    print(f"Average Processing Time: {overall_avg_time:.2f}s")
    
    # Category breakdown
    print(f"\n📈 CATEGORY BREAKDOWN:")
    for result in all_results:
        status = "🟢" if result["success_rate"] >= 0.8 else "🟡" if result["success_rate"] >= 0.6 else "🔴"
        print(f"   {status} {result['category'].replace('_', ' ').title():<25} "
              f"{result['success_rate']:.1%} ({result['success_count']}/{result['total_queries']})")
    
    # Failed queries analysis
    failed_queries = []
    for result in all_results:
        for query_result in result["results"]:
            if not query_result["success"]:
                failed_queries.append(query_result)
    
    if failed_queries:
        print(f"\n❌ FAILED QUERIES ({len(failed_queries)}):")
        for i, failure in enumerate(failed_queries[:10], 1):  # Show first 10
            print(f"   {i}. {failure['query']}")
            print(f"      Expected: [{failure['expected_filter']}, {failure['expected_rank']}]")
            print(f"      Got: [{failure['extracted_filter']}, {failure['extracted_rank']}]")
    
    # Performance assessment
    if overall_success_rate >= 0.9:
        print(f"\n🎉 EXCELLENT! Opponent filters working very well across all categories")
    elif overall_success_rate >= 0.8:
        print(f"\n👍 GOOD! Most opponent filters working well")
    elif overall_success_rate >= 0.7:
        print(f"\n⚠️  ACCEPTABLE: Decent performance but room for improvement")
    else:
        print(f"\n🔧 NEEDS WORK: Significant issues with opponent filter parsing")
    
    # Save detailed results
    timestamp = int(time.time())
    results_file = f"opponent_filter_test_results_{timestamp}.json"
    
    with open(results_file, 'w', encoding='utf-8') as f:
        json.dump({
            "timestamp": timestamp,
            "overall_summary": {
                "total_queries": total_queries,
                "total_success": total_success,
                "success_rate": overall_success_rate,
                "avg_time": overall_avg_time
            },
            "category_results": all_results
        }, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 Detailed results saved to: {results_file}")


if __name__ == "__main__":
    main() 