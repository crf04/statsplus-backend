"""
Test LLM with precise opponent filter wording and rankings
"""

from app.services.llm_service import LLMService
from app.services.nl_query.parser import BaseQueryParser
from sqlalchemy import create_engine
import time

def test_precise_opponent_filters():
    print('PRECISE OPPONENT FILTER WORDING TEST')
    print('=' * 50)
    
    # Initialize services
    engine = create_engine('sqlite:///nba_play_types.db')
    parser = BaseQueryParser(engine)
    llm_service = LLMService()
    
    # Test cases with precise wording and expected exact results
    test_cases = [
        {
            'query': 'Dame vs top 10 rebounding teams',
            'expected_player': 'Damian Lillard',
            'expected_filter': ['OPP_REB', 10],
            'description': 'Top 10 rebounding teams (positive ranking)'
        },
        {
            'query': 'Giannis vs bottom 10 assists allowed teams',
            'expected_player': 'Giannis Antetokounmpo', 
            'expected_filter': ['OPP_AST', -10],
            'description': 'Bottom 10 assists allowed (negative ranking)'
        },
        {
            'query': 'LeBron against top 5 scoring teams',
            'expected_player': 'LeBron James',
            'expected_filter': ['OPP_PTS', 5],
            'description': 'Top 5 scoring teams'
        },
        {
            'query': 'Curry vs worst 8 defensive teams',
            'expected_player': 'Stephen Curry',
            'expected_filter': ['OPP_PTS', -8],
            'description': 'Worst 8 defensive teams (negative ranking)'
        },
        {
            'query': 'Jokic against top 6 steal allowing teams',
            'expected_player': 'Nikola Jokic',
            'expected_filter': ['OPP_STL', 6],
            'description': 'Top 6 steal allowing teams'
        },
        {
            'query': 'Embiid vs bottom 5 turnover forcing teams',
            'expected_player': 'Joel Embiid',
            'expected_filter': ['OPP_TOV', -5],
            'description': 'Bottom 5 turnover forcing teams'
        },
        {
            'query': 'Tatum against top 12 three point allowing teams',
            'expected_player': 'Jayson Tatum',
            'expected_filter': ['OPP_FG3M', 12],
            'description': 'Top 12 three point allowing teams'
        }
    ]
    
    results = []
    
    for i, test_case in enumerate(test_cases, 1):
        query = test_case['query']
        expected_filter = test_case['expected_filter']
        
        print(f'\n[{i}/{len(test_cases)}] Testing: "{query}"')
        print(f'  Expected: {test_case["expected_player"]} | {expected_filter}')
        print(f'  Description: {test_case["description"]}')
        
        # Step 1: Verify NLP triggers LLM
        nlp_result = parser.parse(query)
        nlp_triggered = nlp_result.confidence_breakdown.should_use_llm
        print(f'  NLP triggered LLM: {nlp_triggered}')
        
        if not nlp_triggered:
            print('  SKIP: NLP did not trigger LLM')
            results.append({'query': query, 'success': False, 'reason': 'No LLM trigger'})
            continue
        
        # Step 2: Test LLM parsing
        try:
            start_time = time.time()
            llm_result = llm_service.parse_query(query)
            execution_time = time.time() - start_time
            
            if llm_result:
                player = llm_result.player_name
                opponent_filters = llm_result.opponent_filters
                
                print(f'  LLM Player: {player}')
                print(f'  LLM Opponent Filters: {opponent_filters}')
                print(f'  Execution Time: {execution_time:.3f}s')
                
                # Evaluate success
                expected_player = test_case['expected_player']
                player_match = (player and expected_player.lower().replace(' ', '') in player.lower().replace(' ', ''))
                
                # Check if any opponent filter matches expected
                filter_match = False
                if opponent_filters:
                    for actual_filter in opponent_filters:
                        if len(actual_filter) >= 2:
                            actual_type = actual_filter[0]
                            actual_rank = actual_filter[1]
                            expected_type = expected_filter[0] 
                            expected_rank = expected_filter[1]
                            
                            # Check for exact match or reasonable mapping
                            type_match = (actual_type == expected_type or 
                                        # Allow some flexibility in filter type matching
                                        ('OPP_PTS' in [actual_type, expected_type] and 'PTS' in actual_type) or
                                        ('OPP_REB' in [actual_type, expected_type] and 'REB' in actual_type) or
                                        ('OPP_AST' in [actual_type, expected_type] and 'AST' in actual_type))
                            
                            rank_match = actual_rank == expected_rank
                            
                            if type_match and rank_match:
                                filter_match = True
                                break
                
                success = player_match and filter_match
                
                print(f'  Player Match: {player_match}')
                print(f'  Filter Match: {filter_match}')
                print(f'  OVERALL SUCCESS: {success}')
                
                if filter_match:
                    print(f'  -> PERFECT: Exact ranking and filter type')
                elif opponent_filters:
                    print(f'  -> PARTIAL: Got filters but not exact match')
                else:
                    print(f'  -> FAILED: No opponent filters extracted')
                
                results.append({
                    'query': query,
                    'success': success,
                    'player_match': player_match,
                    'filter_match': filter_match,
                    'actual_filters': opponent_filters,
                    'expected_filter': expected_filter,
                    'execution_time': execution_time
                })
            else:
                print('  LLM returned no result')
                results.append({'query': query, 'success': False, 'reason': 'No LLM result'})
                
        except Exception as e:
            print(f'  LLM Error: {e}')
            results.append({'query': query, 'success': False, 'reason': f'Error: {e}'})
    
    # Summary analysis
    print('\n' + '=' * 50)
    print('PRECISE OPPONENT FILTER TEST RESULTS')
    print('=' * 50)
    
    total_tests = len(results)
    successes = [r for r in results if r.get('success', False)]
    success_count = len(successes)
    
    print(f'\nOverall Success Rate: {success_count}/{total_tests} ({success_count/total_tests:.1%})')
    
    # Detailed analysis
    player_matches = sum(1 for r in results if r.get('player_match', False))
    filter_matches = sum(1 for r in results if r.get('filter_match', False))
    
    print(f'Player Name Accuracy: {player_matches}/{total_tests} ({player_matches/total_tests:.1%})')
    print(f'Filter Accuracy: {filter_matches}/{total_tests} ({filter_matches/total_tests:.1%})')
    
    if successes:
        avg_time = sum(r.get('execution_time', 0) for r in successes) / len(successes)
        print(f'Average Execution Time: {avg_time:.3f}s')
    
    # Show successes vs expected
    print('\nSUCCESSES (Actual vs Expected):')
    for result in successes:
        actual = result.get('actual_filters', [])
        expected = result['expected_filter']
        query = result['query'][:35] + '...' if len(result['query']) > 35 else result['query']
        print(f'  "{query}"')
        print(f'    Expected: {expected}')
        print(f'    Actual:   {actual}')
        print()
    
    # Show failures
    failures = [r for r in results if not r.get('success', False)]
    if failures:
        print('FAILURES:')
        for failure in failures:
            query = failure['query'][:35] + '...' if len(failure['query']) > 35 else failure['query']
            print(f'  "{query}"')
            if 'actual_filters' in failure:
                print(f'    Expected: {failure["expected_filter"]}')
                print(f'    Actual: {failure.get("actual_filters", [])}')
            print(f'    Reason: {failure.get("reason", "Unknown")}')
            print()
    
    return results

if __name__ == "__main__":
    test_precise_opponent_filters()