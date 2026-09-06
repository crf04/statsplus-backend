"""Performance monitoring utilities for NBA API calls and other operations."""
import time
import logging
from functools import wraps
from typing import Callable, Any
import requests

logger = logging.getLogger(__name__)

def monitor_nba_api_calls(func: Callable) -> Callable:
    """Decorator to monitor NBA API call performance and log timing metrics.
    
    Args:
        func: Function making NBA API calls to monitor
        
    Returns:
        Wrapped function with performance monitoring
    """
    @wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        start_time = time.time()
        function_name = f"{func.__module__}.{func.__name__}"
        
        try:
            result = func(*args, **kwargs)
            duration = time.time() - start_time
            
            # Log successful API calls
            if duration > 5.0:  # Log slow calls (>5s)
                logger.warning(f"SLOW NBA API call {function_name} completed in {duration:.2f}s")
            else:
                logger.info(f"NBA API call {function_name} completed in {duration:.2f}s")
            
            return result
            
        except requests.exceptions.Timeout as e:
            duration = time.time() - start_time
            logger.error(f"NBA API call {function_name} TIMED OUT after {duration:.2f}s: {e}")
            raise
            
        except requests.exceptions.RequestException as e:
            duration = time.time() - start_time
            logger.error(f"NBA API call {function_name} FAILED after {duration:.2f}s: {e}")
            raise
            
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"NBA API call {function_name} ERROR after {duration:.2f}s: {e}")
            raise
    
    return wrapper
