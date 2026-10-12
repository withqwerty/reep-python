"""Exceptions raised by the reep package."""

__all__ = ["ReepError"]


class ReepError(Exception):
    """Base class for every error that the reep package raises.

    Catch this class to handle any failure from the SDK in one place.
    """
