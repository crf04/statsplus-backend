"""NBA API configuration with connection pooling and optimized timeouts."""
import socket
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
import os

logger = logging.getLogger(__name__)

def get_nba_api_session():
    """Create optimized HTTP session for NBA API calls with connection pooling and retries.
    
    Returns:
        requests.Session: Configured session for NBA API requests
    """
    session = requests.Session()
    
    # Get configuration from environment variables
    connect_timeout = int(os.getenv('NBA_API_TIMEOUT_CONNECT', '10'))
    read_timeout = int(os.getenv('NBA_API_TIMEOUT_READ', '30'))
    max_retries = int(os.getenv('NBA_API_MAX_RETRIES', '3'))
    pool_connections = int(os.getenv('NBA_API_POOL_CONNECTIONS', '10'))
    pool_maxsize = int(os.getenv('NBA_API_POOL_MAXSIZE', '20'))
    
    # Configure retry strategy with exponential backoff
    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=1,  # Wait 1s, 2s, 4s between retries
        status_forcelist=[429, 500, 502, 503, 504],  # Retry on these HTTP status codes
        allowed_methods=["HEAD", "GET", "OPTIONS"]  # Only retry safe methods
    )
    
    # Configure adapter with connection pooling and keep-alive
    adapter = HTTPAdapter(
        pool_connections=pool_connections,      # Connection pool size per host
        pool_maxsize=pool_maxsize,              # Max connections per pool
        max_retries=retry_strategy,
        socket_options=[
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),  # Enable keep-alive
            (socket.SOL_TCP, socket.TCP_KEEPIDLE, 120),   # Keep-alive idle time
            (socket.SOL_TCP, socket.TCP_KEEPINTVL, 30),   # Keep-alive interval
            (socket.SOL_TCP, socket.TCP_KEEPCNT, 3)       # Keep-alive probes
        ] if hasattr(socket, 'SOL_TCP') else [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
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

def get_shared_nba_session():
    """Get shared NBA API session instance for maximum connection reuse.
    
    Returns:
        requests.Session: Shared session instance
    """
    global _nba_session
    if _nba_session is None:
        _nba_session = get_nba_api_session()
    return _nba_session

def close_nba_session():
    """Close the shared NBA API session and cleanup connections."""
    global _nba_session
    if _nba_session is not None:
        _nba_session.close()
        _nba_session = None
        logger.info("Closed NBA API session and cleaned up connections")