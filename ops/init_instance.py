#!/usr/bin/env python3
"""Prepare instance directories and missing templates without starting the bot."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    os.umask(0o077)
    if (ROOT / ".qqbot-root").read_text(encoding="utf-8").strip() != "qqbot-lifecycle-v1":
        raise RuntimeError("Missing project ownership marker")
    directories = tuple(
        ROOT / relative
        for relative in (
            "config", "state/nonebot/stickers", "state/maintenance",
            "cache/tmp", "cache/quarantine/stickers", "cache/embedding-models",
            "logs/nonebot", "logs/ops", "backups/nonebot",
        )
    )
    templates = (
        (ROOT / "nonebot/.env.example", ROOT / "config/nonebot.env"),
        (ROOT / "nonebot/persona_agent.env.example", ROOT / "config/persona_agent.env"),
        (ROOT / "nonebot/persona_agent.yaml.example", ROOT / "config/persona_agent.yaml"),
    )
    ledger = ROOT / "state/nonebot/forget-ledger.jsonl"
    targets = (*directories, *(target for _, target in templates), ledger)
    for path in targets:
        if path.resolve() != path:
            raise RuntimeError(f"Initialization refuses a symlink or redirected path: {path}")
    for path in directories:
        if path.exists() and not path.is_dir():
            raise NotADirectoryError(path)
    for path in (*(target for _, target in templates), ledger):
        if path.exists() and not path.is_file():
            raise RuntimeError(f"Expected a regular file: {path}")
    create_ledger = not ledger.exists()
    if create_ledger:
        # Include the former layouts so an old instance cannot receive an empty ledger.
        for relative in (
            "state", "data", "nonebot/data", "cache/quarantine/stickers",
            "logs/nonebot", "logs/ops", "backups/nonebot",
        ):
            directory = ROOT / relative
            if directory.is_file() or directory.is_symlink() or any(
                path.is_file() or path.is_symlink() for path in directory.rglob("*")
            ):
                raise RuntimeError(
                    "Existing runtime data found without the current forgetting ledger. "
                    "Restore the CURRENT ledger before initializing or starting this instance."
                )

    contents = [(source.read_bytes(), target) for source, target in templates]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for content, target in contents:
        try:
            with target.open("xb") as stream:
                stream.write(content)
            print(f"Created {target.relative_to(ROOT)}")
        except FileExistsError:
            print(f"Kept {target.relative_to(ROOT)}")
    if create_ledger:
        try:
            with ledger.open("x", encoding="utf-8"):
                pass
            print(f"Created {ledger.relative_to(ROOT)}")
        except FileExistsError:
            print(f"Kept {ledger.relative_to(ROOT)}")
    else:
        print(f"Kept {ledger.relative_to(ROOT)}")
    print("Instance files are ready. Fill in config/ before starting the bot.")


if __name__ == "__main__":
    main()
