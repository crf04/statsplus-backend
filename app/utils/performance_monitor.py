"""Performance monitoring utilities for NBA API calls and other operations."""
import time
import logging
from functools import wraps
from typing import Optional, Callable, Any
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

def monitor_database_query(query_name: Optional[str] = None) -> Callable:
    """Decorator to monitor database query performance.
    
    Args:
        query_name: Optional name for the query being monitored
        
    Returns:
        Decorator function
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            start_time = time.time()
            name = query_name or f"{func.__module__}.{func.__name__}"
            
            try:
                result = func(*args, **kwargs)
                duration = time.time() - start_time
                
                if duration > 2.0:  # Log slow queries (>2s)
                    logger.warning(f"SLOW DB query {name} completed in {duration:.2f}s")
                else:
                    logger.debug(f"DB query {name} completed in {duration:.2f}s")
                
                return result
                
            except Exception as e:
                duration = time.time() - start_time
                logger.error(f"DB query {name} FAILED after {duration:.2f}s: {e}")
                raise
        
        return wrapper
    return decorator

class PerformanceTimer:
    """Context manager for timing code blocks."""
    
    def __init__(self, operation_name: str, log_level: str = "info"):
        """Initialize performance timer.
        
        Args:
            operation_name: Name of operation being timed
            log_level: Logging level (debug, info, warning, error)
        """
        self.operation_name = operation_name
        self.log_level = log_level.lower()
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
    
    def __enter__(self) -> 'PerformanceTimer':
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.end_time = time.time()
        duration = self.duration
        
        if exc_type is not None:
            logger.error(f"Operation {self.operation_name} FAILED after {duration:.2f}s")
        else:
            log_func = getattr(logger, self.log_level, logger.info)
            log_func(f"Operation {self.operation_name} completed in {duration:.2f}s")
    
    @property
    def duration(self) -> float:
        """Get duration of timed operation in seconds."""
        if self.start_time is None:
            return 0.0
        end_time = self.end_time or time.time()
        return end_time - self.start_time

def log_api_performance(url: str, duration: float, status_code: Optional[int] = None, 
                       error: Optional[str] = None) -> None:
    """Log API call performance metrics.
    
    Args:
        url: API endpoint URL
        duration: Request duration in seconds
        status_code: HTTP status code if successful
        error: Error message if failed
    """
    if error:
        logger.error(f"API call to {url} FAILED after {duration:.2f}s: {error}")
    elif status_code and status_code >= 400:
        logger.warning(f"API call to {url} returned {status_code} in {duration:.2f}s")
    elif duration > 5.0:
        logger.warning(f"SLOW API call to {url} completed in {duration:.2f}s (status: {status_code})")
    else:
        logger.info(f"API call to {url} completed in {duration:.2f}s (status: {status_code})")

# Usage examples:
# @monitor_nba_api_calls
# def fetch_player_stats():
#     ...
#
# @monitor_database_query("get_player_games")
# def get_player_games(player_id):
#     ...
#
# with PerformanceTimer("expensive_calculation"):
#     result = expensive_operation()