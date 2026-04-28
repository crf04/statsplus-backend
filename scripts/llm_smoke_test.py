"""
Manual smoke test script for the live LLM service.

This calls the OpenAI API and is intentionally kept out of pytest collection.
Set OPENAI_API_KEY before running it directly.
"""

import json
import sys
import os

# Add the project root to the Python path when run from scripts/.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.llm_service import LLMService, LLMError

# This file is a manual live-API smoke script, not a pytest test module.
__test__ = False


def test_basic_queries():
    """Test basic NBA query parsing"""
    test_queries = [
        "LeBron James last 10 games",
        "Stephen Curry this season",
        "Kevin Durant vs catch and shoot teams",
        "Luka Doncic with 25+ points",
        "Giannis at home last 5 games",
        "Anthony Davis vs top 5 transition teams",
        "Damian Lillard without CJ McCollum",
        "Jayson Tatum with Jaylen Brown last 15 games"
    ]
    
    try:
        print("🏀 Testing LLM Service with Basic Queries")
        print("=" * 60)
        
        # Initialize LLM service
        llm_service = LLMService()
        
        for i, query in enumerate(test_queries, 1):
            print(f"\n{i}. Testing: '{query}'")
            print("-" * 40)
            
            # Add delay to respect rate limits
            if i > 1:
                print("⏳ Waiting 3 seconds to respect rate limits...")
                import time
                time.sleep(3)
            
            result = llm_service.query_llm(query)
            
            if result["success"]:
                print(f"✅ Success (attempt {result['attempt']})")
                print(f"Model: {result['model']}")
                
                if result.get('usage'):
                    tokens = result['usage']
                    print(f"Tokens: {tokens.get('prompt_tokens', 0)} + {tokens.get('completion_tokens', 0)} = {tokens.get('total_tokens', 0)}")
                
                content = result["content"]
                if isinstance(content, dict) and "parsing_error" not in content:
                    print("Parsed JSON:")
                    print(json.dumps(content, indent=2))
                else:
                    print("⚠️ JSON Parsing Failed")
                    print("Raw Response:")
                    print(result["raw_response"])
            else:
                print(f"❌ Failed: {result.get('error', 'Unknown error')}")
            
            print()
    
    except LLMError as e:
        print(f"❌ LLM Service Error: {e}")
        print("\n💡 To fix this:")
        print("1. Create a .env file in the project root")
        print("2. Add: OPENAI_API_KEY=your_api_key_here")
        print("3. Install dependencies: pip install openai python-dotenv")
    
    except Exception as e:
        print(f"❌ Unexpected error: {e}")


def test_complex_queries():
    """Test more complex NBA queries"""
    complex_queries = [
        "LeBron James last 10 games against top 5 catch and shoot teams with 25+ points",
        "Stephen Curry at home with Draymond Green but without Klay Thompson",
        "Kevin Durant vs teams that allow a lot of assists playing 30+ minutes",
        "Luka Doncic against transition teams with 20+ points and 10+ assists",
        "Giannis vs top 10 three point defenses with double-digit rebounds"
    ]
    
    try:
        print("🏀 Testing LLM Service with Complex Queries")
        print("=" * 60)
        
        llm_service = LLMService()
        
        for i, query in enumerate(complex_queries, 1):
            print(f"\n{i}. Testing: '{query}'")
            print("-" * 40)
            
            # Add delay to respect rate limits
            if i > 1:
                print("⏳ Waiting 3 seconds to respect rate limits...")
                import time
                time.sleep(3)
            
            result = llm_service.query_llm(query)
            
            if result["success"]:
                print(f"✅ Success")
                content = result["content"]
                
                if isinstance(content, dict) and "parsing_error" not in content:
                    # Highlight key extracted fields
                    print(f"Player: {content.get('player_name')}")
                    print(f"Game Count: {content.get('game_count')}")
                    print(f"Opponent Filters: {content.get('opponent_filters', [])}")
                    print(f"Self Filters: {content.get('self_filters', [])}")
                    print(f"Players On: {content.get('players_on', [])}")
                    print(f"Players Off: {content.get('players_off', [])}")
                    print(f"Confidence: {content.get('confidence', 0.0)}")
                else:
                    print("⚠️ Parsing issue")
                    print(result["raw_response"])
            else:
                print(f"❌ Failed: {result.get('error')}")
    
    except Exception as e:
        print(f"❌ Error: {e}")


def test_custom_prompt():
    """Test with a custom system prompt"""
    custom_prompt = """You are an NBA stats expert. Extract these fields from queries:
- player_name: The main player
- game_count: Number of games mentioned
- confidence: Your confidence (0-1)

Return JSON only. Example:
{"player_name": "LeBron James", "game_count": 10, "confidence": 0.95}"""
    
    test_query = "LeBron James last 15 games"
    
    try:
        print("🏀 Testing Custom System Prompt")
        print("=" * 60)
        
        llm_service = LLMService()
        
        print(f"Query: '{test_query}'")
        print(f"Custom Prompt: {custom_prompt[:100]}...")
        print("-" * 40)
        
        result = llm_service.query_llm(test_query, system_prompt=custom_prompt)
        
        if result["success"]:
            print("✅ Success with custom prompt")
            print("Response:")
            print(json.dumps(result["content"], indent=2))
        else:
            print(f"❌ Failed: {result.get('error')}")
    
    except Exception as e:
        print(f"❌ Error: {e}")


def interactive_test():
    """Interactive testing mode"""
    try:
        print("🏀 Interactive LLM Testing Mode")
        print("=" * 60)
        print("Enter NBA queries to test (type 'quit' to exit)")
        
        llm_service = LLMService()
        
        while True:
            query = input("\nEnter query: ").strip()
            
            if query.lower() in ['quit', 'exit', 'q']:
                break
            
            if not query:
                continue
            
            print("-" * 40)
            result = llm_service.query_llm(query)
            
            if result["success"]:
                print("✅ Success")
                content = result["content"]
                
                if isinstance(content, dict) and "parsing_error" not in content:
                    print("Extracted fields:")
                    for key, value in content.items():
                        if value is not None and value != [] and value != "":
                            print(f"  {key}: {value}")
                else:
                    print("Raw response:")
                    print(result["raw_response"])
            else:
                print(f"❌ Failed: {result.get('error')}")
    
    except KeyboardInterrupt:
        print("\n\nExiting...")
    except Exception as e:
        print(f"❌ Error: {e}")


def test_prompt_file():
    """Test with a custom prompt file"""
    try:
        print("🏀 Testing Custom Prompt File")
        print("=" * 60)
        
        prompt_file = input("Enter prompt file path (default: prompts/system_prompt.txt): ").strip()
        if not prompt_file:
            prompt_file = "prompts/system_prompt.txt"
        
        test_query = input("Enter test query: ").strip()
        if not test_query:
            test_query = "LeBron James last 10 games"
        
        llm_service = LLMService()
        
        print(f"Testing file: {prompt_file}")
        print(f"Query: '{test_query}'")
        print("-" * 40)
        
        result = llm_service.test_prompt_file(prompt_file, test_query)
        
        if result["success"]:
            print("✅ Success")
            content = result["content"]
            
            if isinstance(content, dict) and "parsing_error" not in content:
                print("Response:")
                print(json.dumps(content, indent=2))
            else:
                print("Raw response:")
                print(result["raw_response"])
        else:
            print(f"❌ Failed: {result.get('error')}")
    
    except Exception as e:
        print(f"❌ Error: {e}")


def main():
    """Main test function"""
    print("🏀 NBA LLM Service Testing")
    print("=" * 60)
    print("Choose test mode:")
    print("1. Basic queries")
    print("2. Complex queries")
    print("3. Custom prompt test")
    print("4. Interactive mode")
    print("5. Test prompt file")
    print("6. All tests")
    
    try:
        choice = input("\nEnter choice (1-6): ").strip()
        
        if choice == "1":
            test_basic_queries()
        elif choice == "2":
            test_complex_queries()
        elif choice == "3":
            test_custom_prompt()
        elif choice == "4":
            interactive_test()
        elif choice == "5":
            test_prompt_file()
        elif choice == "6":
            test_basic_queries()
            print("\n" + "="*60 + "\n")
            test_complex_queries()
            print("\n" + "="*60 + "\n")
            test_custom_prompt()
        else:
            print("Invalid choice")
    
    except KeyboardInterrupt:
        print("\nExiting...")


if __name__ == "__main__":
    main() 
