"""Command-line entry point for the ``reep`` command."""

import argparse
from collections.abc import Sequence

from reep import __version__

MESSAGE = f"""reep {__version__}: the v1 SDK and CLI are in development.

Use the register today:
  download  https://reep.football/downloads
  API       https://reep.football/api
"""


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``reep`` command.

    Args:
        argv: Command-line arguments without the program name. ``None`` reads
            them from ``sys.argv``.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="reep",
        description="Python SDK and CLI for the Reep football entity register.",
    )
    parser.add_argument("--version", action="version", version=f"reep {__version__}")
    parser.parse_args(argv)
    print(MESSAGE, end="")
    return 0
