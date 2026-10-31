#!/usr/bin/env python3
"""NoneBot data retention and backups. Never imports or starts the bot."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400
STAMP = re.compile(r"^\d{8}T\d{6}Z$")
ASSET = re.compile(r"^[0-9a-f]{64}\.(?:gif|jpg|png|webp|img)$")


def checked(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if path == ROOT or not path.is_relative_to(ROOT):
        raise ValueError("Path is outside the managed project")
    for parent in [path, *path.parents]:
        if parent == ROOT:
            break
        if parent.is_symlink():
            raise ValueError("Symlink is not a managed cleanup target: " + str(path))
    return path


def files(directory: Path) -> list[Path]:
    checked(directory)
    if not directory.exists():
        return []
    result = []
    for base, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(base) / name).is_symlink()]
        for name in names:
            path = Path(base) / name
            if not path.is_symlink() and path.is_file():
                result.append(checked(path))
    return result


def read_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path: Path, value) -> None:
    checked(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def database(path: Path, *, writable: bool = False) -> sqlite3.Connection:
    checked(path)
    return sqlite3.connect(
        path.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=10
    )


class Maintenance:
    def __init__(self) -> None:
        if (ROOT / ".qqbot-root").read_text().strip() != "qqbot-lifecycle-v1":
            raise RuntimeError("Missing project ownership marker")
        self.policy = read_json(ROOT / "ops/lifecycle.json", {})
        self.now = datetime.now(UTC)
        self.actions: list[dict] = []
        self.alerts: list[str] = []
        self.errors: list[str] = []

    def note(self, action: str, path: Path, reason: str, size: int = 0) -> None:
        self.actions.append(
            {
                "action": action,
                "path": str(path.relative_to(ROOT)),
                "reason": reason,
                "bytes": size,
            }
        )

    def remove(self, path: Path, reason: str) -> None:
        checked(path)
        if path.is_dir():
            # rmtree unlinks child symlinks without following them. Reject mounted subtrees.
            device = path.stat().st_dev
            if device != ROOT.stat().st_dev:
                raise ValueError("Mounted retention target")
            for base, dirs, _ in os.walk(path, followlinks=False):
                if any(
                    not (Path(base) / name).is_symlink()
                    and (Path(base) / name).stat().st_dev != device
                    for name in dirs
                ):
                    raise ValueError("Mounted subtree in retention target")
            size = sum(p.stat().st_size for p in files(path))
            shutil.rmtree(path)
        else:
            size = path.stat().st_size
            path.unlink()
        self.note("delete", path, reason, size)

    def clean_logs(
        self, paths: list[Path], protected: set[Path], days: int, budget: int
    ) -> None:
        ordered = sorted(paths, key=lambda p: p.stat().st_mtime)
        total = sum(path.stat().st_size for path in ordered)
        for path in ordered:
            if path in protected:
                continue
            age = self.now.timestamp() - path.stat().st_mtime
            if age > days * DAY or total > budget:
                size = path.stat().st_size
                self.remove(path, "log_expired" if age > days * DAY else "log_budget")
                total -= size
        if total > budget:
            self.alerts.append("Active/protected logs exceed their budget")

    def logs(self) -> None:
        nonebot = ROOT / "logs/nonebot"
        self.clean_logs(
            [p for p in files(nonebot) if p.name.startswith("nonebot")],
            {nonebot / "nonebot.log"},
            self.policy["application_log_days"],
            self.policy["application_log_bytes"],
        )
        operational = ROOT / "logs/ops"
        self.clean_logs(
            [
                p
                for p in files(operational)
                if re.fullmatch(r"maintenance-\d{4}-\d{2}-\d{2}\.jsonl", p.name)
            ],
            {operational / ("maintenance-" + self.now.date().isoformat() + ".jsonl")},
            self.policy["maintenance_log_days"],
            self.policy["maintenance_log_bytes"],
        )

    def temporary(self) -> None:
        candidates = files(ROOT / "cache/tmp")
        candidates += [
            p
            for p in files(ROOT / "state/nonebot/stickers")
            if re.fullmatch(r"[0-9a-f]{64}\.(?:gif|jpg|png|webp|img)\.tmp", p.name)
        ]
        for path in candidates:
            if (
                self.now.timestamp() - path.stat().st_mtime
                > self.policy["temporary_hours"] * 3600
            ):
                self.remove(path, "temporary_expired")
        for path in (ROOT / "backups/nonebot").glob(".incomplete-*"):
            if (
                STAMP.fullmatch(path.name.removeprefix(".incomplete-"))
                and self.now.timestamp() - path.stat().st_mtime > DAY
            ):
                self.remove(path, "interrupted_backup")

    def stickers(self) -> None:
        directory = ROOT / "state/nonebot/stickers"
        quarantine = ROOT / "cache/quarantine/stickers"
        with database(
            ROOT / "state/nonebot/persona_agent.sqlite3", writable=True
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            self.retired_stickers(db, directory, quarantine)
            references = {
                checked(Path(row[0]))
                for row in db.execute(
                    "SELECT file_path FROM media_assets WHERE file_path != '' AND quarantine_path = ''"
                )
            }
            managed = {
                checked(Path(row[0]))
                for row in db.execute(
                    "SELECT quarantine_path FROM media_assets WHERE quarantine_path != ''"
                )
            }
            for path in files(quarantine):
                if path in managed:
                    continue
                stamp, separator, original_name = path.name.partition("--")
                if (
                    not separator
                    or not STAMP.fullmatch(stamp)
                    or not ASSET.fullmatch(original_name)
                ):
                    continue
                original = checked(directory / original_name)
                if original in references and not original.exists():
                    directory.mkdir(parents=True, exist_ok=True)
                    os.replace(path, original)
                    self.note("restore", original, "asset_reference_restored")
                elif original not in references or original.exists():
                    quarantined_at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
                        tzinfo=UTC
                    )
                    if self.now - quarantined_at > timedelta(
                        days=self.policy["orphan_quarantine_days"]
                    ):
                        self.remove(path, "unreferenced_asset_quarantine_expired")
            for path in files(directory):
                if (
                    ASSET.fullmatch(path.name)
                    and path not in references
                    and self.now.timestamp() - path.stat().st_mtime > DAY
                ):
                    quarantine.mkdir(parents=True, exist_ok=True)
                    destination = quarantine / (
                        self.now.strftime("%Y%m%dT%H%M%SZ") + "--" + path.name
                    )
                    if not destination.exists():
                        os.replace(path, destination)
                        self.note("quarantine", path, "unreferenced_asset")

    def retired_stickers(
        self, db: sqlite3.Connection, directory: Path, quarantine: Path
    ) -> None:
        rows = db.execute("SELECT * FROM media_assets WHERE file_path != ''").fetchall()
        referenced = {
            row[0]
            for row in db.execute(
                """SELECT DISTINCT json_extract(media.value, '$.asset_id')
               FROM raw_messages, json_each(raw_messages.media_json) AS media
               WHERE expires_at > ? AND is_recalled = 0 AND json_extract(media.value, '$.asset_id') IS NOT NULL""",
                (self.now.isoformat(),),
            )
        }
        for row in rows:
            original = checked(Path(row["file_path"]))
            if (
                original.parent != directory
                or not ASSET.fullmatch(original.name)
                or original.stem != row["content_hash"]
            ):
                raise ValueError("Unexpected sticker path: " + str(original))
            quarantined = (
                checked(Path(row["quarantine_path"]))
                if row["quarantine_path"]
                else None
            )
            # A crash after rename but before SQLite commit leaves the old path recorded.
            if quarantined is None and not original.exists():
                matches = sorted(
                    path
                    for path in files(quarantine)
                    if path.name.endswith("--" + original.name)
                    and STAMP.fullmatch(path.name.split("--", 1)[0])
                )
                if matches:
                    quarantined = matches[-1]
                    db.execute(
                        "UPDATE media_assets SET quarantine_path = ? WHERE id = ?",
                        (str(quarantined), row["id"]),
                    )
            if quarantined is not None:
                stamp, separator, name = quarantined.name.partition("--")
                if (
                    quarantined.parent != quarantine
                    or not separator
                    or not STAMP.fullmatch(stamp)
                    or name != original.name
                ):
                    raise ValueError("Unexpected quarantine path: " + str(quarantined))
                if not quarantined.exists() and original.exists():
                    db.execute(
                        "UPDATE media_assets SET quarantine_path = '' WHERE id = ?",
                        (row["id"],),
                    )
                    quarantined = None
            protected = (
                row["enabled"]
                or row["auto_retired_at"] is None
                or row["id"] in referenced
            )
            if protected:
                if quarantined is not None:
                    if not original.exists():
                        if (
                            hashlib.sha256(quarantined.read_bytes()).hexdigest()
                            != row["content_hash"]
                        ):
                            raise ValueError(
                                "Sticker restore hash mismatch: " + str(row["id"])
                            )
                        directory.mkdir(parents=True, exist_ok=True)
                        os.replace(quarantined, original)
                        self.note(
                            "restore",
                            original,
                            "asset_reference_restored:" + str(row["id"]),
                        )
                    db.execute(
                        "UPDATE media_assets SET quarantine_path = '' WHERE id = ?",
                        (row["id"],),
                    )
                continue
            if quarantined is not None:
                since = datetime.strptime(
                    quarantined.name.split("--", 1)[0], "%Y%m%dT%H%M%SZ"
                ).replace(tzinfo=UTC)
                if self.now - since > timedelta(
                    days=self.policy["orphan_quarantine_days"]
                ):
                    if quarantined.exists():
                        self.remove(
                            quarantined,
                            "retired_asset_quarantine_expired:" + str(row["id"]),
                        )
                    db.execute(
                        "UPDATE media_assets SET file_path = '', quarantine_path = '' WHERE id = ?",
                        (row["id"],),
                    )
                continue
            if (
                original.is_file()
                and self.now.timestamp() - original.stat().st_mtime > DAY
            ):
                quarantine.mkdir(parents=True, exist_ok=True)
                destination = checked(
                    quarantine
                    / (self.now.strftime("%Y%m%dT%H%M%SZ") + "--" + original.name)
                )
                if destination.exists():
                    raise ValueError(
                        "Quarantine target already exists: " + str(destination)
                    )
                db.execute(
                    "UPDATE media_assets SET quarantine_path = ? WHERE id = ?",
                    (str(destination), row["id"]),
                )
                os.replace(original, destination)
                self.note(
                    "quarantine", destination, "auto_retired_asset:" + str(row["id"])
                )

    def rotate_backups(self) -> None:
        daily = ROOT / "backups/nonebot"
        for path in daily.glob("*"):
            if path.is_dir() and STAMP.fullmatch(path.name):
                manifest = read_json(path / "manifest.json", {})
                if manifest.get(
                    "kind"
                ) == "nonebot-daily-v1" and self.now - datetime.fromisoformat(
                    manifest["created_at"]
                ) > timedelta(days=self.policy["backup_days"]):
                    self.remove(path, "daily_backup_expired")

    def inventory(self) -> dict:
        state_bytes = sum(
            p.stat().st_size
            for directory in (ROOT / "state/nonebot", ROOT / "state/maintenance")
            for p in files(directory)
        )
        space = shutil.disk_usage(ROOT)
        history_path = ROOT / "state/maintenance/storage-history.json"
        history = read_json(history_path, [])
        today = self.now.date().isoformat()
        history = [
            {"day": row["day"], "state_bytes": row["state_bytes"]}
            for row in history
            if row["day"] != today
            and row["day"] >= (self.now - timedelta(days=8)).date().isoformat()
        ]
        if state_bytes > self.policy["persistent_content_alert_bytes"]:
            self.alerts.append(
                "Persistent content exceeded size threshold; favorites and memories were preserved"
            )
        if space.free / space.total < self.policy["minimum_free_ratio"]:
            self.alerts.append("Filesystem free space is below threshold")
        with database(ROOT / "state/nonebot/persona_agent.sqlite3") as db:
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            free = db.execute("PRAGMA freelist_count").fetchone()[0] * page_size
            size = db.execute("PRAGMA page_count").fetchone()[0] * page_size
        if free > 64 * 1024 * 1024 and free / size > 0.25:
            self.alerts.append(
                "NoneBot database has reusable free pages; consider backed-up maintenance VACUUM"
            )
        history.append({"day": today, "state_bytes": state_bytes})
        write_json(history_path, history)
        return {
            "state_bytes": state_bytes,
            "filesystem_free_bytes": space.free,
            "nonebot_free_pages_bytes": free,
        }

    def backup(self) -> None:
        stamp = self.now.strftime("%Y%m%dT%H%M%SZ")
        destination = checked(ROOT / "backups/nonebot" / stamp)
        staging = checked(destination.with_name(".incomplete-" + stamp))
        staging.mkdir(parents=True, mode=0o700)
        source_db = ROOT / "state/nonebot/persona_agent.sqlite3"

        def copy_source(source: Path) -> None:
            source = checked(source)
            target = staging / "files" / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            before = source.stat()
            shutil.copy2(source, target)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ino,
            ):
                raise RuntimeError(
                    "Source changed during backup: " + str(source.relative_to(ROOT))
                )

        # Hold a short write reservation for the NoneBot snapshot and its media copies.
        # Manual restoration uses the same SQLite lock; configuration copies follow.
        with database(source_db, writable=True) as guard:
            guard.execute("BEGIN IMMEDIATE")
            with (
                database(source_db) as reader,
                sqlite3.connect(staging / "nonebot.sqlite3") as writer,
            ):
                reader.backup(writer, pages=256, sleep=0.05)
            for row in guard.execute(
                "SELECT file_path, quarantine_path FROM media_assets WHERE file_path != ''"
            ):
                source = Path(row[1] or row[0])
                if row[1] and not source.exists() and Path(row[0]).is_file():
                    source = Path(row[0])
                copy_source(source)
            ledger = ROOT / "state/nonebot/forget-ledger.jsonl"
            if ledger.exists():
                copy_source(ledger)
        for relative in (
            "config/nonebot.env",
            "config/persona_agent.env",
            "config/persona_agent.yaml",
            "state/nonebot/active_model.txt",
        ):
            source = ROOT / relative
            if source.is_file():
                copy_source(source)
        digests = {
            str(path.relative_to(staging)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in files(staging)
        }
        write_json(
            staging / "manifest.json",
            {
                "kind": "nonebot-daily-v1",
                "created_at": self.now.isoformat(),
                "sha256": digests,
            },
        )
        os.replace(staging, destination)
        self.note("backup", destination, "consistent_sqlite_and_persistent_assets")

    def finish(self, command: str, inventory: dict) -> None:
        report = {
            "at": self.now.isoformat(),
            "command": command,
            "actions": self.actions,
            "alerts": self.alerts,
            "errors": self.errors,
            "inventory": inventory,
        }
        path = (
            ROOT
            / "logs/ops"
            / ("maintenance-" + self.now.date().isoformat() + ".jsonl")
        )
        checked(path).parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            stream.write(json.dumps(report, ensure_ascii=False) + "\n")
        write_json(ROOT / "state/maintenance" / (command + "-latest.json"), report)
        print(
            json.dumps(
                {
                    "command": command,
                    "actions": len(self.actions),
                    "freed_bytes": sum(
                        item["bytes"]
                        for item in self.actions
                        if item["action"] == "delete"
                    ),
                    "alerts": self.alerts,
                    "errors": self.errors,
                    "inventory": inventory,
                },
                ensure_ascii=False,
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("clean", "backup", "inspect"))
    args = parser.parse_args()
    os.umask(0o077)
    maintenance = Maintenance()
    lock_path = checked(ROOT / "cache/maintenance.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"skipped":"another maintenance operation is active"}')
            return 0
        inventory = {}
        try:
            if args.command == "backup":
                maintenance.backup()
                maintenance.rotate_backups()
            elif args.command == "clean":
                maintenance.temporary()
                maintenance.stickers()
                maintenance.logs()
                maintenance.rotate_backups()
            inventory = maintenance.inventory()
        except Exception as exc:
            maintenance.errors.append(type(exc).__name__ + ": " + str(exc))
        maintenance.finish(args.command, inventory)
    return 1 if maintenance.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
