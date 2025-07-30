#!/usr/bin/env python3
"""
Clear corrupted cache data after fixing cache serialization issue
"""

import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.utils.cache_config import get_redis_client
from app.services.nba_cache import NBAGameCache

def main():
    print("Clearing corrupted cache data...")
    
    # Get Redis client
    redis_client = get_redis_client()
    if not redis_client:
        print("Redis not available or not configured")
        return
        
    try:
        # Clear all NBA cache data
        cache = NBAGameCache(redis_client)
        
        # Clear all cache patterns
        patterns_to_clear = [
            'nba:player_logs:*',
            'nba:team_stats:*',
            'nba:season:*',
            'nba:computed:*',
            'nba:table:*'
        ]
        
        total_cleared = 0
        for pattern in patterns_to_clear:
            cleared = cache.clear_cache_pattern(pattern)
            total_cleared += cleared
            print(f"Cleared {cleared} keys matching pattern: {pattern}")
            
        print(f"\nTotal cache entries cleared: {total_cleared}")
        print("Cache cleared successfully! The API should now work without 500 errors.")
        
    except Exception as e:
        print(f"Error clearing cache: {e}")
        print("You may need to restart Redis manually")

if __name__ == '__main__':
    main()