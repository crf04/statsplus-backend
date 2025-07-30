"""
Cache management routes for NBA Backend API

Provides endpoints for cache statistics, management, and testing.
"""

from flask import Blueprint, jsonify, request
from ..utils.cache_config import get_redis_client
from ..services.nba_cache import NBAGameCache
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

cache_bp = Blueprint('cache', __name__, url_prefix='/api/cache')

@cache_bp.route('/status', methods=['GET'])
def cache_status():
    """
    Get cache status and statistics.
    
    Returns:
        JSON response with cache status and stats
    """
    try:
        redis_client = get_redis_client()
        cache = NBAGameCache(redis_client)
        stats = cache.get_cache_stats()
        
        return jsonify({
            'status': 'success',
            'cache_status': stats
        }), 200
        
    except Exception as e:
        logger.error(f"Cache status check failed: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@cache_bp.route('/clear', methods=['DELETE'])
def clear_cache():
    """
    Clear cache entries by pattern.
    
    Query Parameters:
        pattern (str, optional): Redis key pattern to clear. Default: '*'
        
    Returns:
        JSON response with number of cleared entries
    """
    try:
        pattern = request.args.get('pattern', '*')
        
        redis_client = get_redis_client()
        cache = NBAGameCache(redis_client)
        
        if not cache.enabled:
            return jsonify({
                'status': 'error',
                'message': 'Cache is not enabled'
            }), 400
        
        cleared_count = cache.clear_cache_pattern(pattern)
        
        return jsonify({
            'status': 'success',
            'message': f'Cleared {cleared_count} cache entries',
            'pattern': pattern,
            'cleared_count': cleared_count
        }), 200
        
    except Exception as e:
        logger.error(f"Cache clear failed: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@cache_bp.route('/test', methods=['POST'])
def test_cache():
    """
    Test cache functionality by setting and getting a test value.
    
    Returns:
        JSON response with test results
    """
    try:
        redis_client = get_redis_client()
        cache = NBAGameCache(redis_client)
        
        if not cache.enabled:
            return jsonify({
                'status': 'error',
                'message': 'Cache is not enabled'
            }), 400
        
        # Test cache operations
        test_key = 'nba:test:cache_test'
        test_value = {'test': True, 'timestamp': str(datetime.now())}
        
        # Set test value
        set_result = cache.set(test_key, test_value, 60)  # 60 seconds TTL
        
        # Get test value
        get_result = cache.get(test_key)
        
        # Clean up
        cache.delete(test_key)
        
        return jsonify({
            'status': 'success',
            'cache_enabled': cache.enabled,
            'set_successful': set_result,
            'get_successful': get_result is not None,
            'values_match': get_result == test_value if get_result else False
        }), 200
        
    except Exception as e:
        logger.error(f"Cache test failed: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500