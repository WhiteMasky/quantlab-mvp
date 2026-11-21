"""Public local-only API factory; the engine is loaded only on first use."""

from .app import create_app

__all__ = ['create_app']
