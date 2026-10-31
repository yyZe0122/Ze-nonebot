#!/usr/bin/env python3
"""Reapply the CURRENT forgetting ledger after an offline database restoration."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    sys.path.insert(0, str(ROOT / "nonebot"))

    from src.plugins.persona_agent.store import Store

    status = subprocess.run(
        ["systemctl", "--user", "is-active", "nonebot.service"],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode not in {0, 3} or status.stdout.strip() not in {
        "inactive",
        "failed",
    }:
        raise RuntimeError(
            "Stop nonebot.service before reconciling a restored database"
        )

    database = ROOT / "state/nonebot/persona_agent.sqlite3"
    if (
        not database.is_file()
        or not (database.parent / "forget-ledger.jsonl").is_file()
    ):
        raise RuntimeError(
            "Restore the database and preserve the current forget-ledger.jsonl first"
        )
    store = Store(database)
    store.close()
    print(
        "Current forgetting ledger reconciled; no messages or model requests were sent."
    )


if __name__ == "__main__":
    main()
