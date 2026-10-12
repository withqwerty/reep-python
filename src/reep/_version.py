"""The installed package version."""

from importlib.metadata import version

__version__ = version("reep")

#: The User-Agent that the SDK sends. It carries no personal data.
USER_AGENT = f"reep-python/{__version__}"
