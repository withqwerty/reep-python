"""Backends that answer register lookups: local DuckDB and the hosted API."""

from reep.backends.base import Backend
from reep.backends.local import LocalBackend

__all__ = ["Backend", "LocalBackend"]
