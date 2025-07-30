#!/usr/bin/env python3
"""
Debug cache serialization issue
"""

import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.game_service import GameService
from sqlalchemy import create_engine

def main():
    print("Testing cache serialization...")
    
    # Initialize service
    engine = create_engine('sqlite:///nba_play_types.db')
    game_service = GameService(engine)
    
    print(f"Cache enabled: {game_service.cache and game_service.cache.enabled}")
    
    # Clear cache first
    if game_service.cache:
        cleared = game_service.cache.clear_cache_pattern('nba:player_logs:*')
        print(f"Cleared {cleared} cache entries")
    
    try:
        print("\n1. Testing _fetch_game_logs_from_api directly (no cache)...")
        result1 = game_service._fetch_game_logs_from_api('LeBron James', '2024-25')
        print(f"Direct API result type: {type(result1)}")
        print(f"Is tuple: {isinstance(result1, tuple)}")
        if isinstance(result1, tuple):
            print(f"Tuple length: {len(result1)}")
            print(f"First element type: {type(result1[0])}")
            print(f"Second element type: {type(result1[1])}")
        
        print("\n2. Testing _get_game_logs (with cache miss)...")
        result2 = game_service._get_game_logs('LeBron James', '2024-25')
        print(f"Cache miss result type: {type(result2)}")
        print(f"Is tuple: {isinstance(result2, tuple)}")
        if isinstance(result2, tuple):
            print(f"Tuple length: {len(result2)}")
            print(f"First element type: {type(result2[0])}")
            print(f"Second element type: {type(result2[1])}")
        
        print("\n3. Testing _get_game_logs again (cache hit expected)...")
        result3 = game_service._get_game_logs('LeBron James', '2024-25')
        print(f"Cache hit result type: {type(result3)}")
        print(f"Is tuple: {isinstance(result3, tuple)}")
        if isinstance(result3, tuple):
            print(f"Tuple length: {len(result3)}")
            print(f"First element type: {type(result3[0])}")
            print(f"Second element type: {type(result3[1])}")
        else:
            print(f"Cache returned: {str(result3)[:200]}...")
            
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    main()