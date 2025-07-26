#!/usr/bin/env python3
"""
LLM Speed Comparison Test

Compare processing times between original verbose prompt and optimized prompt.
"""

import os
import sys
import time
import statistics
from typing import List, Tuple

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.llm_service import LLMService


def test_prompt_speed(prompt_file: str, test_queries: List[str], label: str) -> Tuple[float, List[float], List[int]]:
    """Test processing speed with a specific prompt file."""
    print(f"\n🧪 Testing {label}...")
    
    llm_service = LLMService()
    processing_times = []
    token_counts = []
    
    for i, query in enumerate(test_queries, 1):
        print(f"   [{i}/{len(test_queries)}] {query[:50]}...")
        
        start_time = time.time()
        result = llm_service.test_prompt_file(prompt_file, query)
        processing_time = time.time() - start_time
        
        processing_times.append(processing_time)
        
        if result.get("success") and result.get("usage"):
            total_tokens = result["usage"]["total_tokens"]
            token_counts.append(total_tokens)
            print(f"       ⏱️  {processing_time:.2f}s | 🔢 {total_tokens} tokens")
        else:
            print(f"       ❌ Failed or no usage data")
    
    avg_time = statistics.mean(processing_times)
    avg_tokens = statistics.mean(token_counts) if token_counts else 0
    
    print(f"\n📊 {label} Results:")
    print(f"   Average Time: {avg_time:.2f}s")
    print(f"   Average Tokens: {avg_tokens:.0f}")
    print(f"   Min Time: {min(processing_times):.2f}s")
    print(f"   Max Time: {max(processing_times):.2f}s")
    
    return avg_time, processing_times, token_counts


def main():
    """Run speed comparison test."""
    
    # Check API key
    if not os.getenv('OPENAI_API_KEY'):
        print("❌ OpenAI API key not found. Set OPENAI_API_KEY environment variable.")
        return
    
    # Test queries (subset for speed)
    test_queries = [
        "LeBron last 10 games with 30+ points",
        "Curry with Klay but without Poole last 5 games",
        "Giannis home games where he scores 25+ points and gets 10+ rebounds",
        "Dame against top 5 defenses with 8+ threes",
        "Luka recent games with Kyrie where he plays 35+ minutes"
    ]
    
    print("🚀 LLM Speed Comparison Test")
    print(f"📝 Testing {len(test_queries)} queries with both prompt versions...")
    
    # Test original verbose prompt
    original_avg, original_times, original_tokens = test_prompt_speed(
        "prompts/system_prompt.txt", 
        test_queries, 
        "Original Verbose Prompt"
    )
    
    # Test optimized prompt
    optimized_avg, optimized_times, optimized_tokens = test_prompt_speed(
        "prompts/system_prompt_optimized.txt", 
        test_queries, 
        "Optimized Compact Prompt"
    )
    
    # Calculate improvements
    speed_improvement = original_avg / optimized_avg if optimized_avg > 0 else 0
    token_reduction = (statistics.mean(original_tokens) - statistics.mean(optimized_tokens)) if original_tokens and optimized_tokens else 0
    
    print(f"\n🎯 COMPARISON RESULTS:")
    print(f"   Original Average: {original_avg:.2f}s | {statistics.mean(original_tokens):.0f} tokens")
    print(f"   Optimized Average: {optimized_avg:.2f}s | {statistics.mean(optimized_tokens):.0f} tokens")
    print(f"   Speed Improvement: {speed_improvement:.1f}x faster")
    print(f"   Token Reduction: {token_reduction:.0f} tokens saved ({token_reduction/statistics.mean(original_tokens)*100:.1f}%)")
    
    # Time savings calculation
    queries_per_day = 1000  # Example volume
    daily_time_saved = (original_avg - optimized_avg) * queries_per_day
    print(f"   Daily Time Saved: {daily_time_saved:.0f} seconds ({daily_time_saved/60:.1f} minutes) for {queries_per_day} queries")
    
    print(f"\n✅ Speed test complete! Use the optimized prompt for production.")


if __name__ == "__main__":
    main() 