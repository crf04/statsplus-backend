#!/usr/bin/env python3
"""
Test runner for NBA Backend Natural Language Query Processing

Usage:
    python run_tests.py                  # Run all tests
    python run_tests.py --unit-only      # Run only unit tests
    python run_tests.py --integration    # Run only integration tests
"""

import sys
import os
import subprocess
import argparse

def run_tests(test_type="all"):
    """Run the specified test type"""
    
    # Ensure we're in the right directory
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    
    print("=" * 60)
    print("NBA Backend - Natural Language Query Processing Tests")
    print("=" * 60)
    
    if test_type in ["all", "unit"]:
        print("\n🧪 Running Unit Tests...")
        result = subprocess.run([
            sys.executable, "-m", "unittest", 
            "tests.test_nl_query", "-v"
        ], capture_output=False)
        
        if result.returncode != 0:
            print("❌ Unit tests failed!")
            return False
        else:
            print("✅ Unit tests passed!")
    
    if test_type in ["all", "integration"]:
        print("\n🔗 Running Integration Tests...")
        result = subprocess.run([
            sys.executable, "tests/test_nl_query.py"
        ], capture_output=False)
        
        if result.returncode != 0:
            print("❌ Integration tests failed!")
            return False
        else:
            print("✅ Integration tests passed!")
    
    print("\n🎉 All tests completed successfully!")
    return True

def main():
    parser = argparse.ArgumentParser(description="Run NBA Backend NL Query Tests")
    parser.add_argument("--unit-only", action="store_true", 
                       help="Run only unit tests")
    parser.add_argument("--integration", action="store_true",
                       help="Run only integration tests")
    
    args = parser.parse_args()
    
    if args.unit_only:
        test_type = "unit"
    elif args.integration:
        test_type = "integration"
    else:
        test_type = "all"
    
    success = run_tests(test_type)
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main() 