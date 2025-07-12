#!/usr/bin/env python3
"""
Test script to evaluate parser accuracy for different player combinations
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '.'))

from app.services.nl_query.parser import BaseQueryParser
from sqlalchemy import create_engine
import sqlite3

class MockEngine:
    """Mock database engine for testing"""
    def connect(self):
        return self
    
    def execute(self, query):
        class MockResult:
            def fetchall(self):
                # Return a comprehensive list of NBA players for testing
                return [
                    ("LeBron James",), ("Anthony Davis",), ("Anthony Edwards",), 
                    ("Stephen Curry",), ("Austin Reaves",), ("Klay Thompson",),
                    ("Draymond Green",), ("Kevin Durant",), ("Kyrie Irving",),
                    ("Luka Doncic",), ("Jayson Tatum",), ("Jaylen Brown",),
                    ("Giannis Antetokounmpo",), ("Damian Lillard",), ("Chris Paul",),
                    ("Devin Booker",), ("Russell Westbrook",), ("Kawhi Leonard",),
                    ("Paul George",), ("Jimmy Butler",), ("Bam Adebayo",),
                    ("Nikola Jokic",), ("Jamal Murray",), ("Joel Embiid",),
                    ("James Harden",), ("Tyrese Maxey",), ("Donovan Mitchell",),
                    ("Darius Garland",), ("Trae Young",), ("De'Aaron Fox",),
                    ("Scottie Barnes",), ("Pascal Siakam",), ("Fred VanVleet",),
                    ("Victor Wembanyama",), ("Alperen Sengun",), ("Jalen Green",),
                    ("Paolo Banchero",), ("Franz Wagner",), ("Cade Cunningham",),
                    ("Shai Gilgeous-Alexander",), ("Josh Giddey",), ("Ja Morant",),
                    ("Jaren Jackson Jr.",), ("Desmond Bane",), ("Zion Williamson",),
                    ("Brandon Ingram",), ("CJ McCollum",), ("Karl-Anthony Towns",),
                    ("Anthony Edwards",), ("Jaden McDaniels",), ("Rudy Gobert",),
                ]
        return MockResult()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        pass

def create_test_cases():
    """Create comprehensive test cases for player combinations"""
    return [
        # BASIC SINGLE PLAYER WITH SINGLE "WITH" PLAYER
        {
            "query": "LeBron James with Anthony Davis last 10 games",
            "expected": {
                "main_player": "LeBron James",
                "players_on": ["Anthony Davis"],
                "players_off": []
            }
        },
        {
            "query": "Stephen Curry with Klay Thompson this season",
            "expected": {
                "main_player": "Stephen Curry", 
                "players_on": ["Klay Thompson"],
                "players_off": []
            }
        },
        
        # SINGLE PLAYER WITH MULTIPLE "WITH" PLAYERS
        {
            "query": "LeBron James with Anthony Davis and Austin Reaves",
            "expected": {
                "main_player": "LeBron James",
                "players_on": ["Anthony Davis", "Austin Reaves"],
                "players_off": []
            }
        },
        {
            "query": "Curry with Klay, Draymond, and Kevin Durant",
            "expected": {
                "main_player": "Stephen Curry",
                "players_on": ["Klay Thompson", "Draymond Green", "Kevin Durant"],
                "players_off": []
            }
        },
        {
            "query": "Luka with Kyrie Irving and Christian Wood last 5 games",
            "expected": {
                "main_player": "Luka Dončić",  # Canonical form with accent
                "players_on": ["Kyrie Irving"],  # Christian Wood might not be in our test data
                "players_off": []
            }
        },
        
        # SINGLE PLAYER WITH SINGLE "WITHOUT" PLAYER
        {
            "query": "LeBron James without Anthony Davis",
            "expected": {
                "main_player": "LeBron James",
                "players_on": [],
                "players_off": ["Anthony Davis"]
            }
        },
        {
            "query": "Stephen Curry without Draymond Green home games",
            "expected": {
                "main_player": "Stephen Curry",
                "players_on": [],
                "players_off": ["Draymond Green"],
                "location": "home"
            }
        },
        
        # SINGLE PLAYER WITH MULTIPLE "WITHOUT" PLAYERS
        {
            "query": "LeBron without AD and Russell Westbrook",
            "expected": {
                "main_player": "LeBron James",
                "players_on": [],
                "players_off": ["Anthony Davis", "Russell Westbrook"]
            }
        },
        {
            "query": "Curry without Klay Thompson, Draymond, and Wiggins",
            "expected": {
                "main_player": "Stephen Curry",
                "players_on": [],
                "players_off": ["Klay Thompson", "Draymond Green"]  # Wiggins might not be in our test data
            }
        },
        
        # MIXED SCENARIOS - WITH AND WITHOUT
        {
            "query": "Jayson Tatum with Jaylen Brown but without Marcus Smart",
            "expected": {
                "main_player": "Jayson Tatum",
                "players_on": ["Jaylen Brown"],
                "players_off": []  # Marcus Smart might not be in our test data
            }
        },
        {
            "query": "Luka with Kyrie but without Christian Wood and Dwight Powell",
            "expected": {
                "main_player": "Luka Dončić",  # Canonical form with accent
                "players_on": ["Kyrie Irving"],
                "players_off": []  # Wood and Powell might not be in our test data
            }
        },
        
        # COMPLEX PHRASINGS
        {
            "query": "Giannis alongside Damian Lillard last 15 games",
            "expected": {
                "main_player": "Giannis Antetokounmpo",
                "players_on": ["Damian Lillard"],
                "players_off": []
            }
        },
        {
            "query": "Chris Paul playing with Devin Booker and Kevin Durant",
            "expected": {
                "main_player": "Chris Paul",
                "players_on": ["Devin Booker", "Kevin Durant"],
                "players_off": []
            }
        },
        {
            "query": "Joel Embiid excluding James Harden this season",
            "expected": {
                "main_player": "Joel Embiid",
                "players_on": [],
                "players_off": ["James Harden"]
            }
        },
        
        # USING ALIASES AND NICKNAMES
        {
            "query": "King James with AD and Austin Reaves",
            "expected": {
                "main_player": "LeBron James",
                "players_on": ["Anthony Davis", "Austin Reaves"],
                "players_off": []
            }
        },
        {
            "query": "Steph with KD minus Draymond",
            "expected": {
                "main_player": "Stephen Curry",
                "players_on": ["Kevin Durant"],
                "players_off": ["Draymond Green"]
            }
        },
        
        # EDGE CASES
        {
            "query": "Victor Wembanyama with Chris Paul when healthy",
            "expected": {
                "main_player": "Victor Wembanyama",
                "players_on": ["Chris Paul"],
                "players_off": []
            }
        },
        {
            "query": "Shai Gilgeous-Alexander without Josh Giddey road games",
            "expected": {
                "main_player": "Shai Gilgeous-Alexander",
                "players_on": [],
                "players_off": ["Josh Giddey"],
                "location": "away"
            }
        },
        
        # MULTIPLE MAIN PLAYERS (SHOULD PICK FIRST)
        {
            "query": "LeBron James and Stephen Curry with Anthony Davis",
            "expected": {
                "main_player": "LeBron James",  # Should pick first as main
                "players_on": ["Anthony Davis"],
                "players_off": []
            }
        },
        
        # TYPOS AND MISSPELLINGS
        {
            "query": "lebron james with anthony davis and austin reaves",
            "expected": {
                "main_player": "LeBron James",
                "players_on": ["Anthony Davis", "Austin Reaves"],
                "players_off": []
            }
        },
        {
            "query": "steph curry with klay thompsn",  # typo in Thompson
            "expected": {
                "main_player": "Stephen Curry",
                "players_on": [],  # Might not match due to typo
                "players_off": []
            }
        },
        
        # LOCATION TESTING
        {
            "query": "LeBron James home games",
            "expected": {
                "main_player": "LeBron James",
                "players_on": [],
                "players_off": [],
                "location": "home"
            }
        },
        {
            "query": "Stephen Curry away games",
            "expected": {
                "main_player": "Stephen Curry",
                "players_on": [],
                "players_off": [],
                "location": "away"
            }
        },
        {
            "query": "Giannis at home with Damian Lillard",
            "expected": {
                "main_player": "Giannis Antetokounmpo",
                "players_on": ["Damian Lillard"],
                "players_off": [],
                "location": "home"
            }
        },
        {
            "query": "Luka Dončić on the road without Kyrie Irving",
            "expected": {
                "main_player": "Luka Dončić",
                "players_on": [],
                "players_off": ["Kyrie Irving"],
                "location": "away"
            }
        },
        {
            "query": "Joel Embiid home games with James Harden",
            "expected": {
                "main_player": "Joel Embiid",
                "players_on": ["James Harden"],
                "players_off": [],
                "location": "home"
            }
        },
        {
            "query": "Jayson Tatum away games last 10",
            "expected": {
                "main_player": "Jayson Tatum",
                "players_on": [],
                "players_off": [],
                "location": "away"
            }
        },
        {
            "query": "Chris Paul at home",
            "expected": {
                "main_player": "Chris Paul",
                "players_on": [],
                "players_off": [],
                "location": "home"
            }
        },
        {
            "query": "Victor Wembanyama on the road",
            "expected": {
                "main_player": "Victor Wembanyama",
                "players_on": [],
                "players_off": [],
                "location": "away"
            }
        }
    ]

def evaluate_result(test_case, result):
    """Evaluate how well the parser result matches the expected result"""
    expected = test_case["expected"]
    
    # Check main player
    main_match = (result.player_name == expected["main_player"]) if expected["main_player"] else (result.player_name is None)
    
    # Check players_on
    result_on = set(result.players_on) if result.players_on else set()
    expected_on = set(expected["players_on"]) if expected["players_on"] else set()
    on_match = result_on == expected_on
    
    # Check players_off  
    result_off = set(result.players_off) if result.players_off else set()
    expected_off = set(expected["players_off"]) if expected["players_off"] else set()
    off_match = result_off == expected_off
    
    # Check location
    location_match = (result.location == expected.get("location")) if "location" in expected else (result.location is None)
    
    # Calculate score
    score = 0
    if main_match: score += 1
    if on_match: score += 1  
    if off_match: score += 1
    if location_match: score += 1
    
    return {
        "score": score,
        "max_score": 4,
        "main_match": main_match,
        "on_match": on_match,
        "off_match": off_match,
        "location_match": location_match,
        "details": {
            "main_player": {
                "expected": expected["main_player"],
                "actual": result.player_name,
                "match": main_match
            },
            "players_on": {
                "expected": expected["players_on"],
                "actual": result.players_on,
                "match": on_match
            },
            "players_off": {
                "expected": expected["players_off"],
                "actual": result.players_off,
                "match": off_match
            },
            "location": {
                "expected": expected.get("location"),
                "actual": result.location,
                "match": location_match
            }
        }
    }

def run_comprehensive_test():
    """Run comprehensive test of player combination parsing"""
    print("🏀 NBA PLAYER COMBINATION PARSER - COMPREHENSIVE TEST")
    print("=" * 70)
    
    # Initialize parser
    parser = BaseQueryParser(MockEngine())
    
    # Get test cases
    test_cases = create_test_cases()
    
    results = []
    total_score = 0
    total_max_score = 0
    
    print(f"Testing {len(test_cases)} scenarios...\n")
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"Test {i}: {test_case['query']}")
        print("-" * 50)
        
        try:
            # Parse the query
            result = parser.parse(test_case["query"])
            
            # Evaluate result
            evaluation = evaluate_result(test_case, result)
            results.append(evaluation)
            
            total_score += evaluation["score"]
            total_max_score += evaluation["max_score"]
            
            # Print results
            print(f"✅ Main Player: {result.player_name} {'✓' if evaluation['main_match'] else '✗'}")
            print(f"✅ Players ON: {result.players_on} {'✓' if evaluation['on_match'] else '✗'}")
            print(f"✅ Players OFF: {result.players_off} {'✓' if evaluation['off_match'] else '✗'}")
            print(f"✅ Location: {result.location} {'✓' if evaluation['location_match'] else '✗'}")
            print(f"📊 Score: {evaluation['score']}/{evaluation['max_score']}")
            
            if evaluation["score"] < evaluation["max_score"]:
                print("❌ EXPECTED:")
                print(f"   Main: {test_case['expected']['main_player']}")
                print(f"   ON: {test_case['expected']['players_on']}")
                print(f"   OFF: {test_case['expected']['players_off']}")
                print(f"   Location: {test_case['expected'].get('location', 'None')}")
            
        except Exception as e:
            print(f"❌ ERROR: {e}")
            results.append({"score": 0, "max_score": 4, "error": str(e)})
            total_max_score += 4
        
        print()
    
    # Summary
    print("=" * 70)
    print("📊 COMPREHENSIVE TEST SUMMARY")
    print("=" * 70)
    
    accuracy = (total_score / total_max_score) * 100 if total_max_score > 0 else 0
    print(f"🎯 Overall Accuracy: {accuracy:.1f}% ({total_score}/{total_max_score})")
    
    # Category breakdown
    main_correct = sum(1 for r in results if r.get("main_match", False))
    on_correct = sum(1 for r in results if r.get("on_match", False))
    off_correct = sum(1 for r in results if r.get("off_match", False))
    location_correct = sum(1 for r in results if r.get("location_match", False))
    
    print(f"📈 Main Player Accuracy: {main_correct}/{len(test_cases)} ({main_correct/len(test_cases)*100:.1f}%)")
    print(f"📈 Players ON Accuracy: {on_correct}/{len(test_cases)} ({on_correct/len(test_cases)*100:.1f}%)")
    print(f"📈 Players OFF Accuracy: {off_correct}/{len(test_cases)} ({off_correct/len(test_cases)*100:.1f}%)")
    print(f"📈 Location Accuracy: {location_correct}/{len(test_cases)} ({location_correct/len(test_cases)*100:.1f}%)")
    
    # Find problem areas
    failed_tests = [i for i, r in enumerate(results) if r.get("score", 0) < 4]
    if failed_tests:
        print(f"\n❌ Tests needing improvement: {len(failed_tests)}")
        for i in failed_tests[:5]:  # Show first 5 failures
            print(f"   - Test {i+1}: {test_cases[i]['query']}")
    
    print("\n🔧 Recommendations:")
    if main_correct < len(test_cases) * 0.9:
        print("   - Improve main player detection")
    if on_correct < len(test_cases) * 0.8:
        print("   - Enhance 'with' player parsing")
    if off_correct < len(test_cases) * 0.8:
        print("   - Enhance 'without' player parsing")
    if location_correct < len(test_cases) * 0.8:
        print("   - Enhance location detection")

if __name__ == "__main__":
    run_comprehensive_test() 