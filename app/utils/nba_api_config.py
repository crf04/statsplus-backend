"""NBA API configuration with connection pooling and optimized timeouts."""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging

from app.config.settings import RuntimeSettings, get_runtime_settings

logger = logging.getLogger(__name__)

DEFAULT_NBA_STATS_TIMEOUT_SECONDS = 10.0


def get_nba_stats_timeout(settings: RuntimeSettings | None = None) -> float:
    """Return the timeout used by calls made through the ``nba_api`` package."""
    runtime_settings = settings or get_runtime_settings()
    return runtime_settings.providers.nba_stats_timeout_seconds


class LoggingHTTPAdapter(HTTPAdapter):
    """Custom HTTP adapter that logs retry attempts."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    
    def send(self, request, **kwargs):
        """Send request with retry logging."""
        try:
            response = super().send(request, **kwargs)
            return response
        except Exception as e:
            # Log when we're about to retry due to an exception
            if hasattr(self.config, 'max_retries') and self.config['max_retries'].total > 0:
                logger.warning(f"NBA API request to {request.url} failed, will retry: {str(e)}")
            raise


def log_retry_attempt(retry_state):
    """Callback function to log retry attempts.
    
    Args:
        retry_state: Retry state object containing attempt information
    """
    attempt_number = retry_state.attempt_number
    url = getattr(retry_state, 'url', 'unknown')
    
    if attempt_number > 1:
        logger.warning(f"NBA API retry attempt #{attempt_number-1} for {url}")


class RetryWithLogging(Retry):
    """Custom Retry class that logs retry attempts."""
    
    def increment(self, method=None, url=None, response=None, error=None, _pool=None, _stacktrace=None):
        """Override increment to log retry attempts."""
        # Log the retry attempt
        if self.total is not None and self.total > 0:
            attempt_num = (self.total - (self.total - 1)) if self.total > 1 else 1
            if response is not None:
                logger.warning(f"NBA API retry #{attempt_num} for {url} - Status: {response.status}, Reason: {response.reason}")
            elif error is not None:
                logger.warning(f"NBA API retry #{attempt_num} for {url} - Error: {str(error)}")
        
        return super().increment(method, url, response, error, _pool, _stacktrace)

def get_nba_api_session(settings: RuntimeSettings | None = None):
    """Create optimized HTTP session for NBA API calls with connection pooling and retries.
    
    Returns:
        requests.Session: Configured session for NBA API requests
    """
    runtime_settings = settings or get_runtime_settings()
    session = requests.Session()

    connect_timeout = runtime_settings.providers.pbp_connect_timeout_seconds
    read_timeout = runtime_settings.providers.pbp_read_timeout_seconds
    max_retries = runtime_settings.providers.pbp_max_retries
    pool_connections = runtime_settings.providers.pbp_pool_connections
    pool_maxsize = runtime_settings.providers.pbp_pool_maxsize
    
    # Configure retry strategy with exponential backoff and logging
    retry_strategy = RetryWithLogging(
        total=max_retries,
        backoff_factor=1,  # Wait 1s, 2s, 4s between retries
        status_forcelist=[429, 500, 502, 503, 504],  # Retry on these HTTP status codes
        allowed_methods=["HEAD", "GET", "OPTIONS"]  # Only retry safe methods
    )
    
    # Configure adapter with connection pooling and keep-alive
    adapter = LoggingHTTPAdapter(
        pool_connections=pool_connections,      # Connection pool size per host
        pool_maxsize=pool_maxsize,              # Max connections per pool
        max_retries=retry_strategy
    )
    
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    # Set default timeouts (connect, read)
    session.timeout = (connect_timeout, read_timeout)
    
    # Set common headers for NBA API requests
    session.headers.update({
        'User-Agent': 'NBA-Backend-API/1.0',
        'Accept': 'application/json',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive'
    })
    
    logger.info(f"Created NBA API session with timeouts=({connect_timeout}, {read_timeout}), "
                f"retries={max_retries}, pool_size={pool_connections}")
    
    return session

# Global session instance for reuse across requests
_nba_session = None

def get_shared_nba_session(settings: RuntimeSettings | None = None):
    """Get shared NBA API session instance for maximum connection reuse.
    
    Returns:
        requests.Session: Shared session instance
    """
    global _nba_session
    if _nba_session is None:
        _nba_session = get_nba_api_session(settings)
    return _nba_session

def close_nba_session():
    """Close the shared NBA API session and cleanup connections."""
    global _nba_session
    if _nba_session is not None:
        _nba_session.close()
        _nba_session = None
        logger.info("Closed NBA API session and cleaned up connections")
