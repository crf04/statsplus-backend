"""Gunicorn configuration for the production web process (see Procfile).

The app, including the natural-language parser's spaCy model, is built once in
the master (``preload_app``) and shared copy-on-write with every worker, so a
worker recycled by ``max_requests`` is forked in well under a second instead of
rebuilding the dependency graph.  Anything that must not cross ``fork`` is
started or reset per worker in ``post_fork``; see "Pre-forking process model"
in docs/ARCHITECTURE.md.

Trade-off: an import-time or app-factory failure now stops the master (and the
deploy) instead of failing one worker at a time.
"""

from __future__ import annotations

import gc
import os

# Mirrors app.services.job_service.DEFER_DISPATCHER_ENV as a literal so loading
# this config imports nothing from the app; a test pins the two together.
DEFER_DISPATCHER_ENV = "STATSPLUS_DEFER_DATA_REFRESH_DISPATCHER"

# Set before Gunicorn preloads the app so the master builds the durable refresh
# coordinator without its recovery dispatch or poller thread.
os.environ[DEFER_DISPATCHER_ENV] = "1"

wsgi_app = "wsgi:app"
preload_app = True
bind = [f"0.0.0.0:{os.environ.get('PORT', '8000')}"]
workers = 4
threads = 2
timeout = 180
keepalive = 5
max_requests = 1000
max_requests_jitter = 100
# Gunicorn's control socket runs a thread in the master, and every recycled
# worker is forked from the master; keep the master single-threaded.  Nothing
# on Railway uses ``gunicornc``.
control_socket_disable = True


def _dependencies(server):
    """Return the dependency graph of the app Gunicorn preloaded."""

    return server.app.wsgi().extensions["dependencies"]


def when_ready(server):
    """Release the master's resources and freeze its objects before forking.

    Building the app opened database connections in the master; closing them
    here keeps the master from holding idle connections for its lifetime.
    Frozen objects move to the collector's permanent generation, so a
    worker's garbage collections never write to (and copy) the pages they
    share with the master.
    """

    engine = getattr(_dependencies(server), "engine", None)
    if engine is not None:
        engine.dispose()
    gc.freeze()


def post_fork(server, worker):
    """Give each worker its own database connections and job dispatcher."""

    dependencies = _dependencies(server)
    engine = getattr(dependencies, "engine", None)
    if engine is not None:
        # Forget, without closing, any pooled DBAPI connection inherited from
        # the master; closing it here would tear down a socket the master owns.
        engine.dispose(close=False)
    jobs = getattr(dependencies, "data_refresh_jobs_service", None)
    if jobs is not None:
        jobs.start_dispatcher()
    # Processes this worker starts should own their dispatcher again.
    os.environ.pop(DEFER_DISPATCHER_ENV, None)
