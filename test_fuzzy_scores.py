#!/usr/bin/env python3

"""
Test fuzzy matching scores for legitimate vs problematic cases
"""

from rapidfuzz import fuzz

def test_fuzzy_scores():
    """Test fuzzy matching scores"""
    
    # Test cases: (query, alias, expected_result)
    test_cases = [
        # Legitimate cases (should match)
        ("lebron", "lebron james", "GOOD"),
        ("curry", "stephen curry", "GOOD"), 
        ("doncic", "luka doncic", "GOOD"),
        ("cp3", "cp3", "GOOD"),
        ("antman", "ant-man", "GOOD"),
        ("kg", "kevin garnett", "GOOD"),
        ("ad", "anthony davis", "GOOD"),
        ("kd", "kevin durant", "GOOD"),
        ("lebron", "king james", "GOOD"),
        ("curry", "chef curry", "GOOD"),
        ("giannis", "greek freak", "GOOD"),
        
        # Misspellings (should still match)
        ("lebron", "lebron james", "GOOD"),
        ("currey", "stephen curry", "GOOD"),  # common misspelling
        ("doncik", "luka doncic", "GOOD"),   # typo
        ("antonis", "giannis antetokounmpo", "GOOD"),  # partial name
        
        # Problematic cases (should NOT match)
        ("points", "point god", "BAD"),
        ("triple", "mr triple double", "BAD"),
        ("double", "mr triple double", "BAD"),
        ("cp points", "point god", "BAD"),
        ("cp triple double", "mr triple double", "BAD"),
        ("king", "king james", "BAD"),  # too generic
        ("chef", "chef curry", "BAD"),  # too generic
        ("greek", "greek freak", "BAD"), # too generic
    ]
    
    print("=== Fuzzy Matching Score Analysis ===")
    print("Testing with partial_ratio scorer...")
    print()
    
    print("LEGITIMATE CASES:")
    for query, alias, expected in test_cases:
        if expected == "GOOD":
            score = fuzz.partial_ratio(query.lower(), alias.lower())
            print(f"  '{query}' -> '{alias}' = {score}%")
    
    print("\nPROBLEMATIC CASES:")
    for query, alias, expected in test_cases:
        if expected == "BAD":
            score = fuzz.partial_ratio(query.lower(), alias.lower())
            print(f"  '{query}' -> '{alias}' = {score}%")
    
    # Test different thresholds
    print("\n=== THRESHOLD ANALYSIS ===")
    thresholds = [85, 90, 95, 98]
    
    for threshold in thresholds:
        print(f"\nWith threshold {threshold}%:")
        good_matches = 0
        bad_matches = 0
        
        for query, alias, expected in test_cases:
            score = fuzz.partial_ratio(query.lower(), alias.lower())
            would_match = score >= threshold
            
            if expected == "GOOD" and would_match:
                good_matches += 1
            elif expected == "BAD" and would_match:
                bad_matches += 1
        
        total_good = sum(1 for _, _, expected in test_cases if expected == "GOOD")
        total_bad = sum(1 for _, _, expected in test_cases if expected == "BAD")
        
        print(f"  Good matches: {good_matches}/{total_good} ({good_matches/total_good*100:.1f}%)")
        print(f"  Bad matches: {bad_matches}/{total_bad} ({bad_matches/total_bad*100:.1f}%)")
        print(f"  Precision: {good_matches/(good_matches+bad_matches)*100:.1f}%")

if __name__ == "__main__":
    test_fuzzy_scores() 