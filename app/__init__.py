"""Agent package.

Exposes the ADK `app` object that the serving/deployment layer imports.
"""

from .agent import app

__all__ = ["app"]
