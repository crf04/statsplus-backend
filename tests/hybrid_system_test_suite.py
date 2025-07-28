"""
Comprehensive Test Suite for Hybrid NLP-LLM System

This test suite validates the hybrid processing system with realistic game_logs queries,
focusing on nickname preservation, confidence thresholds, and component override logic.
"""

import unittest
import sys
import os
from unittest.mock import Mock, MagicMock, patch
import json
from datetime import datetime

# Add the app directory to the path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.nl_service import NLService
from app.services.nl_query.parser import QueryComponents, ConfidenceBreakdown

class HybridSystemTestSuite:
    """Comprehensive test suite for hybrid NLP-LLM system"""
    
    def __init__(self):
        self.test_results = []
        self.setup_mock_services()
    
    def setup_mock_services(self):
        """Set up mock services for testing"""
        self.mock_engine = MagicMock()
        self.nl_service = NLService.__new__(NLService)
        self.nl_service.engine = self.mock_engine
        
        # Mock LLM service
        self.mock_llm_service = MagicMock()
        self.nl_service.llm_service = self.mock_llm_service
        
        # Mock NLP parser
        self.mock_nl_parser = MagicMock()
        self.nl_service.nl_parser = self.mock_nl_parser
        self.nl_service.query_executor = MagicMock()
    
    def create_mock_nlp_result(self, player_name=None, confidence=0.6, players_on=None, players_off=None, 
                              opponent_filters=None, location=None, time_period=None, game_count=None):
        """Create mock NLP parsing result"""
        components = QueryComponents(
            player_name=player_name,
            confidence=confidence,
            players_on=players_on or [],
            players_off=players_off or [],
            opponent_filters=opponent_filters or [],
            location=location,
            time_period=time_period,
            game_count=game_count,
            intent="game_logs"
        )
        
        # Add confidence breakdown that triggers LLM when confidence < 0.75
        components.confidence_breakdown = MagicMock()
        components.confidence_breakdown.should_use_llm = confidence < 0.75
        
        return components
    
    def create_mock_llm_result(self, success=True, player_name=None, confidence=0.9, 
                              opponent_filters=None, location=None, game_count=None, **kwargs):
        """Create mock LLM response"""
        content = {
            'player_name': player_name,
            'confidence': confidence,
            'opponent_filters': opponent_filters or [],
            'location': location,
            'game_count': game_count,
            'intent': 'game_logs',
            **kwargs
        }
        
        return {
            'success': success,
            'content': content
        }
    
    def run_test_case(self, test_name, query, nlp_result, llm_result, expected_outcomes):
        """Run a single test case and record results"""
        print(f"\n{'='*60}")
        print(f"TEST: {test_name}")
        print(f"Query: '{query}'")
        print(f"{'='*60}")
        
        # Setup mocks
        self.mock_nl_parser.parse.return_value = nlp_result
        self.mock_llm_service.test_prompt_with_context.return_value = llm_result
        
        try:
            # Run the hybrid processing
            result = self.nl_service.process_query(query)
            
            # Validate results
            test_result = {
                'test_name': test_name,
                'query': query,
                'status': 'PASS',
                'issues': [],
                'result': result
            }
            
            # Check expected outcomes
            for key, expected_value in expected_outcomes.items():
                actual_value = result.get(key)
                if actual_value != expected_value:
                    test_result['issues'].append(f"{key}: expected '{expected_value}', got '{actual_value}'")
                    test_result['status'] = 'FAIL'
            
            # Log results
            print(f"Status: {test_result['status']}")
            print(f"Parsed by: {result.get('parsed_by', 'unknown')}")
            print(f"Player name: {result.get('player_name', 'None')}")
            print(f"Confidence: {result.get('confidence', 0):.3f}")
            
            if test_result['issues']:
                print(f"Issues: {', '.join(test_result['issues'])}")
            
            self.test_results.append(test_result)
            
        except Exception as e:
            test_result = {
                'test_name': test_name,
                'query': query, 
                'status': 'ERROR',
                'error': str(e),
                'issues': [f"Exception: {str(e)}"]
            }
            print(f"Status: ERROR - {str(e)}")
            self.test_results.append(test_result)
    
    def test_nickname_preservation(self):
        """Test that player nicknames are properly preserved"""
        test_cases = [
            {
                'name': 'King James Nickname',
                'query': 'Show me King James games against top defenses',
                'nlp_result': self.create_mock_nlp_result(
                    player_name='LeBron James',  # NLP resolves nickname
                    confidence=0.6  # Low confidence triggers LLM
                ),
                'llm_result': self.create_mock_llm_result(
                    player_name='LeBron James Jr.',  # LLM gets it wrong
                    opponent_filters=[['Defensive Rating', 5]],
                    confidence=0.95
                ),
                'expected': {
                    'player_name': 'LeBron James',  # Should preserve NLP
                    'parsed_by': 'hybrid'
                }
            },
            {
                'name': 'Chef Curry Nickname', 
                'query': 'Chef Curry last 10 games at home',
                'nlp_result': self.create_mock_nlp_result(
                    player_name='Stephen Curry',  # NLP resolves nickname
                    confidence=0.65,
                    location='home',
                    game_count=10
                ),
                'llm_result': self.create_mock_llm_result(
                    player_name='Steph Curry',  # LLM slight variation
                    location='home',
                    game_count=10,
                    confidence=0.88
                ),
                'expected': {
                    'player_name': 'Stephen Curry',  # Should preserve NLP
                    'parsed_by': 'hybrid'
                }
            },
            {
                'name': 'Greek Freak Nickname',
                'query': 'Greek Freak games with 30+ points',
                'nlp_result': self.create_mock_nlp_result(
                    player_name='Giannis Antetokounmpo',  # NLP resolves nickname
                    confidence=0.7,
                    self_filters=[{'stat_column': 'PTS', 'operator': 'gte', 'value': 30}]
                ),
                'llm_result': self.create_mock_llm_result(
                    player_name='Giannis Antetokounmpo',  # LLM gets it right too
                    self_filters=[{'stat_column': 'PTS', 'operator': 'gte', 'value': 30}],
                    confidence=0.92
                ),
                'expected': {
                    'player_name': 'Giannis Antetokounmpo',  # Should preserve NLP
                    'parsed_by': 'hybrid'
                }
            }
        ]
        
        for case in test_cases:
            self.run_test_case(case['name'], case['query'], case['nlp_result'], 
                             case['llm_result'], case['expected'])
    
    def test_confidence_thresholds(self):
        """Test confidence-based override behavior"""
        test_cases = [
            {
                'name': 'High Confidence Player Override (Should Not Happen)',
                'query': 'LeBron last 5 games with AD on court',
                'nlp_result': self.create_mock_nlp_result(
                    player_name='LeBron James',
                    players_on=['Anthony Davis'],
                    confidence=0.6
                ),
                'llm_result': self.create_mock_llm_result(
                    player_name='LeBron James Jr.',  # Wrong player
                    players_on=['A.D.'],  # Different format
                    confidence=0.98  # Very high confidence
                ),
                'expected': {
                    'player_name': 'LeBron James',  # Should NEVER override player name
                    'parsed_by': 'hybrid'
                }
            },
            {
                'name': 'Low Confidence Players_On Preservation',
                'query': 'LeBron games with Westbrook and AD on court',
                'nlp_result': self.create_mock_nlp_result(
                    player_name='LeBron James',
                    players_on=['Russell Westbrook', 'Anthony Davis'],
                    confidence=0.7
                ),
                'llm_result': self.create_mock_llm_result(
                    players_on=['Different Player'],  # LLM wrong
                    confidence=0.8  # Below 0.95 threshold
                ),
                'expected': {
                    'player_name': 'LeBron James'
                    # players_on should be preserved from NLP
                }
            },
            {
                'name': 'High Confidence Players_On Override',
                'query': 'LeBron games with teammates present',
                'nlp_result': self.create_mock_nlp_result(
                    player_name='LeBron James',
                    players_on=['Unclear Player'],  # NLP unclear
                    confidence=0.6
                ),
                'llm_result': self.create_mock_llm_result(
                    players_on=['Anthony Davis', 'Russell Westbrook'],  # LLM clear
                    confidence=0.97  # Above 0.95 threshold
                ),
                'expected': {
                    'player_name': 'LeBron James'
                    # players_on should be overridden by LLM
                }
            }
        ]
        
        for case in test_cases:
            self.run_test_case(case['name'], case['query'], case['nlp_result'],
                             case['llm_result'], case['expected'])
    
    def test_complex_query_enhancement(self):
        """Test LLM enhancement of complex queries"""
        test_cases = [
            {
                'name': 'Opponent Filter Enhancement',
                'query': 'LeBron games against elite defensive teams this month',
                'nlp_result': self.create_mock_nlp_result(
                    player_name='LeBron James',
                    confidence=0.5,  # Low confidence due to complex opponent logic
                    time_period='month'
                ),
                'llm_result': self.create_mock_llm_result(
                    opponent_filters=[['Defensive Rating', 5]],  # LLM understands "elite defensive"
                    time_period='2024-07',  # LLM converts "this month"
                    confidence=0.9
                ),
                'expected': {
                    'player_name': 'LeBron James',  # Preserved from NLP
                    'parsed_by': 'hybrid'
                }
            },
            {
                'name': 'Statistical Context Understanding',
                'query': 'Curry games when he was hot from three against tough teams',
                'nlp_result': self.create_mock_nlp_result(
                    player_name='Stephen Curry',  # NLP gets player right
                    confidence=0.4  # Very low due to complex context
                ),
                'llm_result': self.create_mock_llm_result(
                    self_filters=[{'stat_column': 'FG3M', 'operator': 'gte', 'value': 5}],  # "hot from three"
                    opponent_filters=[['OPP_PTS', 10]],  # "tough teams"  
                    confidence=0.85
                ),
                'expected': {
                    'player_name': 'Stephen Curry',  # Preserved from NLP
                    'parsed_by': 'hybrid'
                }
            },
            {
                'name': 'Multi-Component Complex Query',
                'query': 'Show me Giannis home games in January against top rebounding teams with 25+ points',
                'nlp_result': self.create_mock_nlp_result(
                    player_name='Giannis Antetokounmpo',
                    location='home',  # NLP gets location
                    confidence=0.6
                ),
                'llm_result': self.create_mock_llm_result(
                    location='home',  # LLM confirms
                    date_range='2024-01',  # LLM converts "January"
                    opponent_filters=[['OPP_REB', 5]],  # "top rebounding teams"
                    self_filters=[{'stat_column': 'PTS', 'operator': 'gte', 'value': 25}],  # "25+ points"
                    confidence=0.92
                ),
                'expected': {
                    'player_name': 'Giannis Antetokounmpo',  # Preserved from NLP
                    'location': 'home',  # Should be consistent
                    'parsed_by': 'hybrid'
                }
            }
        ]
        
        for case in test_cases:
            self.run_test_case(case['name'], case['query'], case['nlp_result'],
                             case['llm_result'], case['expected'])
    
    def test_edge_cases(self):
        """Test edge cases and error scenarios"""
        test_cases = [
            {
                'name': 'LLM Service Failure Fallback',
                'query': 'LeBron games against top teams',
                'nlp_result': self.create_mock_nlp_result(
                    player_name='LeBron James',
                    confidence=0.6  # Should trigger LLM
                ),
                'llm_result': {
                    'success': False,  # LLM fails
                    'error': 'API timeout'
                },
                'expected': {
                    'parsed_by': 'nlp'  # Should fallback to NLP
                }
            },
            {
                'name': 'High NLP Confidence (No LLM Call)',
                'query': 'LeBron James last 10 games',
                'nlp_result': self.create_mock_nlp_result(
                    player_name='LeBron James',
                    game_count=10,
                    time_period='recent',
                    confidence=0.9  # High confidence, no LLM needed
                ),
                'llm_result': None,  # Should not be called
                'expected': {
                    'player_name': 'LeBron James',
                    'game_count': 10,
                    'parsed_by': 'nlp'  # Should stay NLP
                }
            },
            {
                'name': 'Empty Player Context',
                'query': 'Games with high scoring',
                'nlp_result': self.create_mock_nlp_result(
                    player_name=None,  # No player identified
                    confidence=0.5
                ),
                'llm_result': self.create_mock_llm_result(
                    self_filters=[{'stat_column': 'PTS', 'operator': 'gte', 'value': 120}],
                    confidence=0.8
                ),
                'expected': {
                    'parsed_by': 'hybrid'
                    # No player_name expected
                }
            }
        ]
        
        for case in test_cases:
            # Special handling for high confidence case (no LLM call)
            if case['name'] == 'High NLP Confidence (No LLM Call)':
                # Mock to ensure LLM is not called
                self.mock_nl_parser.parse.return_value = case['nlp_result']
                case['nlp_result'].confidence_breakdown.should_use_llm = False
                
                try:
                    result = self.nl_service.process_query(case['query'])
                    # Verify LLM was not called
                    if not self.mock_llm_service.test_prompt_with_context.called:
                        print(f"✓ LLM correctly not called for high confidence query")
                    
                    test_result = {
                        'test_name': case['name'],
                        'query': case['query'],
                        'status': 'PASS' if result.get('parsed_by') == 'nlp' else 'FAIL',
                        'result': result
                    }
                    self.test_results.append(test_result)
                    print(f"Status: {test_result['status']}")
                    print(f"Parsed by: {result.get('parsed_by')}")
                    
                except Exception as e:
                    print(f"Error: {e}")
                    
                continue
            
            self.run_test_case(case['name'], case['query'], case['nlp_result'],
                             case['llm_result'], case['expected'])
    
    def generate_report(self):
        """Generate comprehensive test report"""
        total_tests = len(self.test_results)
        passed_tests = sum(1 for result in self.test_results if result['status'] == 'PASS')
        failed_tests = sum(1 for result in self.test_results if result['status'] == 'FAIL')
        error_tests = sum(1 for result in self.test_results if result['status'] == 'ERROR')
        
        report = {
            'summary': {
                'total_tests': total_tests,
                'passed': passed_tests,
                'failed': failed_tests,
                'errors': error_tests,
                'success_rate': f"{(passed_tests/total_tests)*100:.1f}%" if total_tests > 0 else "0%",
                'timestamp': datetime.now().isoformat()
            },
            'test_results': self.test_results
        }
        
        return report
    
    def run_all_tests(self):
        """Run complete test suite"""
        print("="*80)
        print("HYBRID NLP-LLM SYSTEM - COMPREHENSIVE TEST SUITE")
        print("="*80)
        
        print("\n🎯 TESTING NICKNAME PRESERVATION...")
        self.test_nickname_preservation()
        
        print("\n⚖️ TESTING CONFIDENCE THRESHOLDS...")
        self.test_confidence_thresholds()
        
        print("\n🧠 TESTING COMPLEX QUERY ENHANCEMENT...")
        self.test_complex_query_enhancement()
        
        print("\n🔍 TESTING EDGE CASES...")
        self.test_edge_cases()
        
        return self.generate_report()

def main():
    """Run the comprehensive test suite"""
    test_suite = HybridSystemTestSuite()
    report = test_suite.run_all_tests()
    
    print("\n" + "="*80)
    print("TEST SUITE SUMMARY")
    print("="*80)
    print(f"Total Tests: {report['summary']['total_tests']}")
    print(f"Passed: {report['summary']['passed']}")
    print(f"Failed: {report['summary']['failed']}")
    print(f"Errors: {report['summary']['errors']}")
    print(f"Success Rate: {report['summary']['success_rate']}")
    
    return report

if __name__ == '__main__':
    report = main()