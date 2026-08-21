"""NBA API configuration with connection pooling and optimized timeouts."""
import requests
from urllib3.util.retry import Retry
import logging

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.utils.telemetry import increment_retry_count

logger = logging.getLogger(__name__)

DEFAULT_NBA_STATS_TIMEOUT_SECONDS = 10.0


def get_nba_stats_timeout(settings: RuntimeSettings | None = None) -> float:
    """Return the timeout used by calls made through the ``nba_api`` package."""
    runtime_settings = settings or get_runtime_settings()
    return runtime_settings.providers.nba_stats_timeout_seconds


def _safe_url_path(url):
    """Return a URL without its query string for operator logs."""
    return url.split("?", 1)[0] if url else "unknown"


def log_retry_attempt(retry_state):
    """Callback function to log retry attempts without exposing query strings.

    Args:
        retry_state: Retry state object containing attempt information
    """
    attempt_number = retry_state.attempt_number
    url = _safe_url_path(getattr(retry_state, "url", "unknown"))

    if attempt_number > 1:
        logger.warning("NBA API retry attempt #%d for %s", attempt_number - 1, url)


class RetryWithLogging(Retry):
    """Custom Retry class that counts retries for provider telemetry."""

    def increment(self, method=None, url=None, response=None, error=None, _pool=None, _stacktrace=None):
        """Override increment to count retries for telemetry.

        Each retry increments the thread-local counter read by the enclosing
        provider tracker, so provider events expose the number of upstream
        retries for the operation.  Query strings and exception text are
        never logged.
        """
        # ``Retry.increment`` raises ``MaxRetryError`` when this call would
        # exhaust the budget.  Count only the transitions that return a new
        # retry state; counting before the superclass call records one retry
        # too many for terminal failures (and records a retry when total=0).
        next_retry = super().increment(
            method, url, response, error, _pool, _stacktrace
        )
        increment_retry_count()
        if url is not None:
            logger.warning(
                "NBA API retrying after status %s for %s",
                getattr(response, "status", "error"),
                _safe_url_path(url),
            )

        return next_retry

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
        backoff_factor=2,
        backoff_jitter=1,
        status_forcelist=[429, 500, 502, 503, 504],  # Retry on these HTTP status codes
        allowed_methods=["HEAD", "GET", "OPTIONS"],  # Only retry safe methods
        respect_retry_after_header=True,
    )
    
    # Configure adapter with connection pooling and keep-alive
    adapter = requests.adapters.HTTPAdapter(
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
