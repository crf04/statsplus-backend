"""WSGI entrypoint for running the Flask app on production servers.

This module exposes a typed `app` variable that Gunicorn (or any WSGI server)
can import as the application object.
"""

from typing import Final

from flask import Flask

# Import the application created in `run.py`. The module-level `app` in
# `run.py` initializes the Flask application and registers routes/blueprints.
from run import app as flask_app


def create_app() -> Flask:
    """Create and return the Flask application instance.

    Returns
    -------
    Flask
        The configured Flask application instance.
    """

    return flask_app


# The object Gunicorn looks for: `wsgi:app`
app: Final[Flask] = create_app()

