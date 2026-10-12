"""Python SDK for the Reep football entity register.

The SDK is in development. Version 0.0.1 reserves the package name; see
https://reep.football for the register itself.
"""

import logging
from importlib.metadata import version

from reep.errors import ReepError

__all__ = ["ReepError", "__version__"]

__version__ = version("reep")

logging.getLogger(__name__).addHandler(logging.NullHandler())
