from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .storage import iso, utcnow

_RETRY_DELAYS = (
    timedelta(minutes=10),
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(days=1),
)
_TERMINAL_ERRORS = frozenset(
    {
        "image_unavailable",
        "image_too_large",
        "invalid_image_data",
        "invalid_base64_image",
        "unsupported_file_source",
        "empty_or_oversized_image",
        "invalid_response",
    }
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_sources (
 source_key TEXT PRIMARY KEY, asset_id INTEGER NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media_jobs (
 source_key TEXT PRIMARY KEY,payload TEXT NOT NULL,refs TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0,next_at TEXT NOT NULL
);
"""


def _error_code(error: str) -> str:
    code = error.removeprefix("media_model_failed:")
    return code.split(":", 1)[0]


def _terminal(error: str) -> bool:
    code = _error_code(error)
    return code in _TERMINAL_ERRORS or code.startswith("invalid_")


def _retry_delay(attempts: int) -> timedelta:
    return _RETRY_DELAYS[min(max(attempts - 1, 0), len(_RETRY_DELAYS) - 1)]


class MediaStore:
    def __init__(self, conn, lock) -> None:
        self._conn = conn
        self._lock = lock
        with self._conn:
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(media_jobs)")
            }
            if "last_error" not in columns:
                self._conn.execute(
                    "ALTER TABLE media_jobs ADD COLUMN last_error TEXT NOT NULL DEFAULT ''"
                )
            now = iso(utcnow())
            failed = self._conn.execute(
                """SELECT source_key, attempts FROM media_jobs
                   WHERE status='failed' ORDER BY next_at, source_key"""
            ).fetchall()
            for index, row in enumerate(failed):
                attempts = max(int(row["attempts"]), 1)
                next_at = utcnow() + _retry_delay(attempts) + timedelta(minutes=index)
                self._conn.execute(
                    "UPDATE media_jobs SET status='pending', next_at=? WHERE source_key=?",
                    (iso(next_at), row["source_key"]),
                )
            self._conn.execute(
                """UPDATE media_jobs
                   SET status='pending'
                   WHERE status='queued' AND next_at>?""",
                (now,),
            )
            self._conn.execute(
                """UPDATE media_jobs
                   SET status='pending', next_at=?
                   WHERE status='queued' AND next_at<=?""",
                (iso(utcnow() + timedelta(minutes=1)), now),
            )

    def queue_job(self, job: dict, raw_id: int, index: int) -> None:
        key = job["media"]["key"]
        with self._conn:
            row = self._conn.execute(
                "SELECT refs FROM media_jobs WHERE source_key=?", (key,)
            ).fetchone()
            refs = json.loads(row[0]) if row else []
            if [raw_id, index] not in refs:
                refs.append([raw_id, index])
            self._conn.execute(
                "INSERT INTO media_jobs(source_key,payload,refs,next_at) VALUES (?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET refs=excluded.refs",
                (key, json.dumps(job), json.dumps(refs), iso(utcnow())),
            )

    def pending_jobs(self, limit: int) -> list[dict]:
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT * FROM media_jobs WHERE status='pending' AND next_at<=? ORDER BY next_at LIMIT ?",
                (iso(utcnow()), limit),
            )
        ]

    def queued(self, key: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE media_jobs SET status='queued' WHERE source_key=?", (key,)
            )

    def job_refs(self, key: str) -> list:
        row = self._conn.execute(
            "SELECT refs FROM media_jobs WHERE source_key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else []

    def finish_job(self, key: str, failed: bool | str = False) -> None:
        with self._conn:
            if not failed:
                self._conn.execute("DELETE FROM media_jobs WHERE source_key=?", (key,))
                return
            error = failed if isinstance(failed, str) else "unknown"
            row = self._conn.execute(
                "SELECT attempts FROM media_jobs WHERE source_key=?", (key,)
            ).fetchone()
            attempts = (int(row["attempts"]) if row else 0) + 1
            if _terminal(error):
                status = "failed"
                next_at = iso(utcnow())
            else:
                status = "pending"
                next_at = iso(utcnow() + _retry_delay(attempts))
            self._conn.execute(
                """UPDATE media_jobs
                   SET attempts=?, status=?, next_at=?, last_error=?
                   WHERE source_key=?""",
                (attempts, status, next_at, error[:300], key),
            )

    def media_by_source(self, source_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT a.* FROM media_assets a JOIN media_sources s ON s.asset_id=a.id WHERE s.source_key = ? ORDER BY id DESC LIMIT 1",
                (source_key,),
            ).fetchone()
        return dict(row) if row else None

    def media_by_hash(self, content_hash: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM media_assets WHERE content_hash = ?", (content_hash,)
            ).fetchone()
        return dict(row) if row else None

    def note_media_source(self, asset_id: int, source_key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO media_sources VALUES (?,?,?) ON CONFLICT(source_key) DO UPDATE SET asset_id=excluded.asset_id,last_seen_at=excluded.last_seen_at",
                (source_key, asset_id, iso(utcnow())),
            )
            self._conn.execute(
                "UPDATE media_assets SET last_seen_at=? WHERE id=?",
                (iso(utcnow()), asset_id),
            )

    def save_media(
        self,
        *,
        content_hash: str,
        source_key: str,
        kind: str,
        native: dict,
        description: str,
        tags: list[str],
        reason: str,
        favorite: bool,
        storage: str,
        source_url: str,
        file_path: str = "",
        qq_emoji_id: str = "",
        review_version: str = "",
    ) -> dict:
        stamp = iso(utcnow())
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO media_assets (
                    content_hash, source_key, kind, native_json, description, tags_json,
                    favorite_reason, favorite, storage, source_url, file_path, qq_emoji_id,
                    created_at, last_seen_at, reviewed_at, review_version, review_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(content_hash) DO UPDATE SET
                    source_key = excluded.source_key, last_seen_at = excluded.last_seen_at""",
                (
                    content_hash,
                    source_key,
                    kind,
                    json.dumps(native, ensure_ascii=False),
                    description,
                    json.dumps(tags, ensure_ascii=False),
                    reason,
                    int(favorite),
                    storage,
                    source_url,
                    file_path,
                    qq_emoji_id,
                    stamp,
                    stamp,
                    stamp if review_version else None,
                    review_version,
                    reason,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM media_assets WHERE content_hash = ?", (content_hash,)
            ).fetchone()
        self.note_media_source(int(row["id"]), source_key)
        return dict(row)

    def attach_media(self, raw_id: int, media_index: int, asset_id: int) -> None:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT media_json FROM raw_messages WHERE id = ?", (raw_id,)
            ).fetchone()
            if row is None:  # A queued image may outlive raw-message retention.
                return
            media = json.loads(row["media_json"])
            media[media_index]["asset_id"] = asset_id
            self._conn.execute(
                "UPDATE raw_messages SET media_json = ? WHERE id = ?",
                (json.dumps(media, ensure_ascii=False), raw_id),
            )
            self._conn.execute(
                "UPDATE media_assets SET last_seen_at = ? WHERE id = ?",
                (iso(utcnow()), asset_id),
            )

    def enrich_media(self, rows: list[dict]) -> list[dict]:
        for row in rows:
            descriptions = []
            for media in json.loads(row["media_json"]):
                asset_id = media.get("asset_id")
                if asset_id is not None:
                    with self._lock:
                        asset = self._conn.execute(
                            "SELECT description, tags_json FROM media_assets WHERE id = ?",
                            (asset_id,),
                        ).fetchone()
                    if asset:
                        tags = json.loads(asset["tags_json"])
                        descriptions.append(
                            "、".join(tags[:5])[:100]
                            if tags
                            else asset["description"][:100]
                        )
            row["media_descriptions"] = descriptions
        return rows

    def favorite_media(self) -> list[dict]:
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM media_assets WHERE favorite = 1 AND enabled = 1 ORDER BY id DESC"
                )
            ]

    def favorite_media_count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM media_assets WHERE favorite = 1 AND enabled = 1"
            ).fetchone()[0]

    def recover_media_reviews(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE media_review_log SET decision = 'defer', reason = 'interrupted' WHERE decision = 'pending'"
            )

    def claim_media_review(
        self,
        *,
        version: str,
        daily_limit: int,
        interval_days: int,
        at_capacity: bool,
        now: datetime,
    ) -> tuple[int, dict] | None:
        day_start = now.astimezone(ZoneInfo("Asia/Shanghai")).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        cutoff = iso(now - timedelta(days=interval_days))
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            attempts = self._conn.execute(
                "SELECT COUNT(*) FROM media_review_log WHERE created_at >= ?",
                (iso(day_start),),
            ).fetchone()[0]
            if attempts >= daily_limit:
                return None
            row = self._conn.execute(
                """SELECT * FROM media_assets AS asset WHERE favorite = 1 AND enabled = 1
                   AND (reviewed_at IS NULL OR review_version != ? OR reviewed_at < ?)
                   AND NOT EXISTS (SELECT 1 FROM media_review_log AS review
                                   WHERE review.asset_id = asset.id AND review.created_at >= ?)
                   ORDER BY (reviewed_at IS NOT NULL), (review_version = ?),
                            CASE WHEN ? THEN COALESCE(last_used_at, created_at) ELSE reviewed_at END,
                            id LIMIT 1""",
                (version, cutoff, iso(day_start), version, int(at_capacity)),
            ).fetchone()
            if row is None:
                return None
            cursor = self._conn.execute(
                "INSERT INTO media_review_log(asset_id, created_at, decision, rule_version) VALUES (?, ?, 'pending', ?)",
                (row["id"], iso(now), version),
            )
            return int(cursor.lastrowid), dict(row)

    def finish_media_review(
        self,
        review_id: int,
        asset: dict,
        *,
        decision: str,
        reason: str,
        version: str,
        now: datetime,
    ) -> tuple[str, str]:
        with self._lock, self._conn:
            if decision in {"keep", "retire"}:
                stamp = iso(now)
                cursor = self._conn.execute(
                    """UPDATE media_assets SET reviewed_at = ?, review_version = ?, review_reason = ?,
                       enabled = ?, auto_retired_at = ? WHERE id = ? AND favorite = 1 AND enabled = 1
                       AND reviewed_at IS ? AND review_version = ?""",
                    (
                        stamp,
                        version,
                        reason,
                        int(decision == "keep"),
                        stamp if decision == "retire" else None,
                        asset["id"],
                        asset["reviewed_at"],
                        asset["review_version"],
                    ),
                )
                if not cursor.rowcount:
                    decision, reason = "defer", "asset_changed_during_review"
            self._conn.execute(
                "UPDATE media_review_log SET decision = ?, reason = ? WHERE id = ?",
                (decision, reason, review_id),
            )
        return decision, reason

    def media_review_status(self, limit: int = 10) -> list[dict]:
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(
                    "SELECT asset_id, created_at, decision, reason FROM media_review_log ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            ]

    def qq_media_ids(self) -> set[str]:
        with self._lock:
            return {
                row[0]
                for row in self._conn.execute(
                    "SELECT qq_emoji_id FROM media_assets WHERE qq_emoji_id != ''"
                )
            }

    def promote_qq_media(self, asset_id: int, qq_id: str, url: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE media_assets SET favorite = 1, storage = 'qq', qq_emoji_id = ?, source_url = ? WHERE id = ?",
                (qq_id, url, asset_id),
            )

    def sync_qq_media(self, favorites: dict[str, str]) -> None:
        with self._lock, self._conn:
            for row in self._conn.execute(
                "SELECT id, qq_emoji_id FROM media_assets WHERE storage = 'qq'"
            ).fetchall():
                qq_id = row["qq_emoji_id"]
                self._conn.execute(
                    "UPDATE media_assets SET favorite = ?, source_url = COALESCE(?, source_url) WHERE id = ?",
                    (int(qq_id in favorites), favorites.get(qq_id), row["id"]),
                )

    def note_media_sent(self, asset_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE media_assets SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
                (iso(utcnow()), asset_id),
            )

    def set_media_enabled(
        self,
        asset_id: int,
        enabled: bool,
        *,
        directory: Path,
        quarantine_directory: Path,
        version: str,
    ) -> bool:
        with self._lock, self._conn:
            # Serialize restoration with maintenance moves/deletes, including a recovery after a crash.
            self._conn.execute("BEGIN IMMEDIATE")
            asset = self._conn.execute(
                "SELECT * FROM media_assets WHERE id = ? AND favorite = 1", (asset_id,)
            ).fetchone()
            if asset is None:
                return False
            local_available = False
            if asset["file_path"]:
                original = Path(asset["file_path"])
                if (
                    original.resolve() != original
                    or original.parent != directory
                    or not re.fullmatch(
                        re.escape(asset["content_hash"])
                        + r"\.(?:gif|jpg|png|webp|img)",
                        original.name,
                    )
                ):
                    raise ValueError("Unexpected sticker file path")
                if asset["quarantine_path"]:
                    quarantined = Path(asset["quarantine_path"])
                    if (
                        quarantined.resolve() != quarantined
                        or quarantined.parent != quarantine_directory
                        or not re.fullmatch(
                            r"\d{8}T\d{6}Z--" + re.escape(original.name),
                            quarantined.name,
                        )
                    ):
                        raise ValueError("Unexpected sticker quarantine path")
                    if not original.exists() and quarantined.is_file():
                        if (
                            hashlib.sha256(quarantined.read_bytes()).hexdigest()
                            != asset["content_hash"]
                        ):
                            raise ValueError("Quarantined sticker hash mismatch")
                        directory.mkdir(parents=True, exist_ok=True)
                        quarantined.replace(original)
                local_available = original.is_file()
            if enabled and asset["storage"] == "local" and not local_available:
                raise FileNotFoundError("Local sticker is no longer recoverable")
            self._conn.execute(
                """UPDATE media_assets SET enabled = ?, auto_retired_at = NULL,
                   quarantine_path = CASE WHEN ? THEN '' ELSE quarantine_path END,
                   reviewed_at = ?, review_version = ?, review_reason = ? WHERE id = ?""",
                (
                    int(enabled),
                    int(local_available),
                    iso(utcnow()),
                    version,
                    "管理员手动启用" if enabled else "管理员手动停用，保留原图",
                    asset_id,
                ),
            )
        return True
