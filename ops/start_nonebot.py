#!/usr/bin/env python3
"""Start the bot through the same entry point used for local development."""

import runpy
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "nonebot"


def main() -> None:
    sys.path.insert(0, str(SOURCE))

    runpy.run_path(str(SOURCE / "bot.py"), run_name="__main__")


if __name__ == "__main__":
    main()
