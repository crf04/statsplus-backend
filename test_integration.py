import requests
import json

# Test the new natural language query endpoint
def test_nl_endpoint():
    """Test the /api/nl-query endpoint"""
    
    test_queries = [
        "LeBron James last 10 games",
        "Stephen Curry with Klay Thompson",
        "Giannis at home this season",
        "KD without Kyrie Irving"
    ]
    
    for query in test_queries:
        print(f"\n🧪 Testing: '{query}'")
        print("-" * 50)
        
        try:
            response = requests.post(
                'http://127.0.0.1:5000/api/nl-query',
                json={'query': query},
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                print(f"✅ Success! Confidence: {result.get('confidence', 0):.0%}")
                print(f"   Player: {result.get('player_name', 'None')}")
                if result.get('game_count'):
                    print(f"   Games: Last {result.get('game_count')}")
                if result.get('location'):
                    print(f"   Location: {result.get('location')}")
                if result.get('players_on'):
                    print(f"   Playing with: {', '.join(result.get('players_on'))}")
                if result.get('players_off'):
                    print(f"   Playing without: {', '.join(result.get('players_off'))}")
            else:
                print(f"❌ Error {response.status_code}: {response.text}")
                
        except requests.exceptions.ConnectionError:
            print("❌ Connection error - Make sure Flask app is running on http://127.0.0.1:5000")
        except requests.exceptions.Timeout:
            print("❌ Request timeout")
        except Exception as e:
            print(f"❌ Unexpected error: {e}")

if __name__ == "__main__":
    print("🚀 Testing Natural Language Query Integration")
    print("=" * 60)
    test_nl_endpoint()
    print("\n" + "=" * 60)
    print("🏁 Test completed!") 