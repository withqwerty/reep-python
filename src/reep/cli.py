from reep import __version__

MESSAGE = f"""reep {__version__}: the v1 SDK and CLI are in development.

Use the register today:
  download  https://reep.football/downloads
  API       https://reep.football/api
"""


def main() -> int:
    print(MESSAGE, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
