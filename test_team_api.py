#!/usr/bin/env python3
"""
Test script for team matchup API endpoints
"""
import requests
import json
import sys
from urllib.parse import quote

API_BASE = 'http://127.0.0.1:5000/api'

def test_team_stats_endpoint():
    """Test the team stats endpoint with different categories"""
    teams_to_test = [
        'Los Angeles Lakers',
        'Golden State Warriors',
        'Boston Celtics'
    ]
    
    categories_to_test = [
        'Traditional',
        'Playtypes',
        'Assists',
        'Zone Shooting'
    ]
    
    print("🏀 Testing Team Stats API Endpoints")
    print("=" * 50)
    
    for team in teams_to_test:
        print(f"\n📊 Testing team: {team}")
        
        for category in categories_to_test:
            try:
                url = f"{API_BASE}/teams/stats?team={quote(team)}&category={quote(category)}"
                print(f"  📈 Testing {category}... ", end="")
                
                response = requests.get(url, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    if data:
                        print("✅ SUCCESS")
                        
                        # Show some sample data for the first team
                        if team == teams_to_test[0] and category == 'Traditional':
                            print("    Sample data:")
                            for key, value in list(data.items())[:5]:
                                print(f"      {key}: {value}")
                            print("      ...")
                    else:
                        print("⚠️  EMPTY RESPONSE")
                else:
                    print(f"❌ FAILED (Status: {response.status_code})")
                    print(f"      Error: {response.text}")
                    
            except requests.exceptions.RequestException as e:
                print(f"❌ NETWORK ERROR: {e}")
            except Exception as e:
                print(f"❌ ERROR: {e}")

def test_teams_list_endpoint():
    """Test the teams list endpoint"""
    print("\n🏆 Testing Teams List Endpoint")
    print("=" * 50)
    
    try:
        url = f"{API_BASE}/teams"
        print("📋 Fetching teams list... ", end="")
        
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            teams = response.json()
            print(f"✅ SUCCESS ({len(teams)} teams found)")
            
            print("  First 10 teams:")
            for i, team in enumerate(teams[:10]):
                print(f"    {i+1}. {team}")
                
            if len(teams) > 10:
                print(f"    ... and {len(teams) - 10} more")
        else:
            print(f"❌ FAILED (Status: {response.status_code})")
            print(f"    Error: {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"❌ NETWORK ERROR: {e}")
    except Exception as e:
        print(f"❌ ERROR: {e}")

def test_server_availability():
    """Test if the Flask server is running"""
    print("🔍 Testing Server Availability")
    print("=" * 50)
    
    try:
        # Try a simple request to check if server is running
        response = requests.get(f"{API_BASE}/teams", timeout=5)
        if response.status_code == 200:
            print("✅ Server is running and responding")
            return True
        else:
            print(f"⚠️  Server responded with status {response.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        print("❌ Server is not running or not accessible")
        print("   Please make sure the Flask server is started with: python run.py")
        return False
    except Exception as e:
        print(f"❌ Error checking server: {e}")
        return False

def main():
    """Main test function"""
    print("🚀 NBA Team Matchup API Test Suite")
    print("=" * 50)
    
    # Check server availability first
    if not test_server_availability():
        print("\n⚠️  Cannot proceed with tests - server is not available")
        sys.exit(1)
    
    # Run the tests
    test_teams_list_endpoint()
    test_team_stats_endpoint()
    
    print("\n" + "=" * 50)
    print("✅ Test suite completed!")
    print("\n💡 If you see errors, make sure:")
    print("   1. Flask server is running (python run.py)")
    print("   2. Database file exists and has data") 
    print("   3. All required dependencies are installed")

if __name__ == "__main__":
    main()