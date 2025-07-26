#!/usr/bin/env python3
"""
Test opponent filter parsing with improved optimized prompt.
"""

import os
import sys
import time
import json

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.llm_service import LLMService


def test_opponent_filters():
    """Test opponent filter parsing specifically."""
    
    if not os.getenv('OPENAI_API_KEY'):
        print("❌ OpenAI API key not found.")
        return
    
    # Opponent filter test queries (these were failing before)
    test_queries = [
        "Curry away games against top 10 defenses where he makes 6+ threes",
        "Dame against worst 8 three point defenses where he attempts 12+ threes",  
        "KD games against top 5 defenses on the road",
        "LeBron vs bottom 3 transition teams last 10 games",
        "Giannis against worst catch and shoot defenses at home",
        "Tatum vs top 15 defenses with Brown last 5 games"
    ]
    
    print("🎯 Testing Opponent Filter Parsing with Improved Optimized Prompt")
    print(f"📝 Testing {len(test_queries)} opponent filter queries...\n")
    
    llm_service = LLMService()
    
    success_count = 0
    total_time = 0
    
    for i, query in enumerate(test_queries, 1):
        print(f"[{i}/{len(test_queries)}] {query}")
        
        start_time = time.time()
        result = llm_service.test_prompt_file("prompts/system_prompt_optimized.txt", query)
        processing_time = time.time() - start_time
        total_time += processing_time
        
        if result.get("success"):
            content = result.get("content", {})
            opponent_filters = content.get("opponent_filters", [])
            confidence = content.get("confidence", 0.0)
            
            if opponent_filters:
                print(f"  ✅ SUCCESS: {opponent_filters} (conf: {confidence:.3f}) [{processing_time:.2f}s]")
                success_count += 1
            else:
                print(f"  ❌ NO FILTERS EXTRACTED (conf: {confidence:.3f}) [{processing_time:.2f}s]")
                print(f"     Raw response: {content}")
        else:
            print(f"  ❌ FAILED: {result.get('error', 'Unknown error')} [{processing_time:.2f}s]")
        
        # Show usage stats for first query
        if i == 1 and result.get("usage"):
            usage = result["usage"]
            print(f"     Token usage: {usage['total_tokens']} total ({usage['prompt_tokens']} prompt + {usage['completion_tokens']} completion)")
        
        print()
    
    # Summary
    success_rate = success_count / len(test_queries) * 100
    avg_time = total_time / len(test_queries)
    
    print(f"📊 RESULTS:")
    print(f"   Success Rate: {success_rate:.1f}% ({success_count}/{len(test_queries)})")
    print(f"   Average Time: {avg_time:.2f}s")
    print(f"   Total Time: {total_time:.2f}s")
    
    if success_rate >= 80:
        print(f"   🎉 EXCELLENT! Opponent filters working well with optimized prompt")
    elif success_rate >= 60:
        print(f"   👍 GOOD! Most opponent filters working")
    else:
        print(f"   ⚠️  NEEDS WORK: Low success rate on opponent filters")


if __name__ == "__main__":
    test_opponent_filters() 