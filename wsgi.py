"""WSGI entrypoint for production servers."""

from typing import Final

from flask import Flask

from app import create_app


# The object Gunicorn looks for: `wsgi:app`
app: Final[Flask] = create_app()
