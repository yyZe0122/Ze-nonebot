from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import Scope
from .media_store import SCHEMA as MEDIA_SCHEMA
from .media_store import MediaStore
from .memory_store import SCHEMA as MEMORY_SCHEMA
from .memory_store import MemoryStore
from .policy import group_conversation_date, summarize_episode
from .runtime_store import SCHEMA as RUNTIME_SCHEMA
from .runtime_store import RuntimeStore

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    scope TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    decision TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    group_id INTEGER,
    message_id INTEGER,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    scope TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    group_id INTEGER,
    sender TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS raw_messages_fts USING fts5(
    text,
    content='raw_messages',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    start_message_id INTEGER NOT NULL,
    end_message_id INTEGER NOT NULL,
    summary TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT,
    scope TEXT NOT NULL,
    subject_user_id INTEGER,
    group_id INTEGER,
    source_scope TEXT NOT NULL,
    layer TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    confidence REAL NOT NULL,
    salience REAL NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    evidence_count INTEGER NOT NULL DEFAULT 1,
    UNIQUE(scope, subject_user_id, group_id, layer, text_hash)
);

CREATE TABLE IF NOT EXISTS usage_daily (
    day TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    tokens INTEGER NOT NULL,
    PRIMARY KEY(day, user_id)
);

CREATE TABLE IF NOT EXISTS group_state (
    group_id INTEGER PRIMARY KEY,
    last_spoke_at TEXT,
    consecutive_replies INTEGER NOT NULL DEFAULT 0,
    cooldown_until TEXT,
    active_thread_until TEXT,
    thread_replies INTEGER NOT NULL DEFAULT 0,
    followup_user_id INTEGER,
    recent_speakers_json TEXT NOT NULL DEFAULT '[]',
    last_segment TEXT
);

CREATE TABLE IF NOT EXISTS conversation_sessions (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    user_id INTEGER,
    group_id INTEGER,
    conversation_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS private_activity (
    bot_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    last_user_at TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    last_attempt_at TEXT,
    last_proactive_at TEXT,
    next_check_at TEXT NOT NULL,
    awaiting_reply INTEGER NOT NULL DEFAULT 0,
    day TEXT NOT NULL DEFAULT '',
    sent_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(bot_id, user_id)
);

CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS media_assets (
    id INTEGER PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    source_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    native_json TEXT NOT NULL DEFAULT '{}',
    description TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    favorite_reason TEXT NOT NULL DEFAULT '',
    favorite INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    storage TEXT NOT NULL DEFAULT 'observed',
    qq_emoji_id TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS media_source_key ON media_assets(source_key);
CREATE TABLE IF NOT EXISTS media_review_log (
    id INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    rule_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS media_review_time ON media_review_log(created_at, asset_id);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def text_hash(value: str) -> str:
    return hashlib.sha256(" ".join(value.lower().split()).encode()).hexdigest()


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA + MEDIA_SCHEMA + MEMORY_SCHEMA + RUNTIME_SCHEMA)
        with self._conn:
            self._migrate()
        self.runtime = RuntimeStore(self._conn)
        self.media = MediaStore(self._conn, self._lock)
        self.memory = MemoryStore(self._conn, path.parent / "forget-ledger.jsonl")

    def _migrate(self) -> None:
        self._conn.execute("BEGIN")
        memory_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(memory_entries)")
        }
        for name, definition in {
            "participants_json": "TEXT NOT NULL DEFAULT '[]'",
            "change_kind": "TEXT NOT NULL DEFAULT 'new'",
        }.items():
            if name not in memory_columns:
                self._conn.execute(
                    f"ALTER TABLE memory_entries ADD COLUMN {name} {definition}"
                )
        self._conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES (5)")
        raw_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(raw_messages)")
        }
        additions = {
            "message_key": "TEXT",
            "message_id": "INTEGER",
            "bot_id": "TEXT NOT NULL DEFAULT ''",
            "sender_name": "TEXT NOT NULL DEFAULT ''",
            "reply_to_id": "INTEGER",
            "reply_sender": "TEXT NOT NULL DEFAULT ''",
            "reply_text": "TEXT NOT NULL DEFAULT ''",
            "is_proactive": "INTEGER NOT NULL DEFAULT 0",
            "media_json": "TEXT NOT NULL DEFAULT '[]'",
            "is_recalled": "INTEGER NOT NULL DEFAULT 0",
            "context_hidden": "INTEGER NOT NULL DEFAULT 0",
            "is_control": "INTEGER NOT NULL DEFAULT 0",
            "chat_key": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in raw_columns:
                self._conn.execute(
                    f"ALTER TABLE raw_messages ADD COLUMN {name} {definition}"
                )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS raw_message_key ON raw_messages(message_key) WHERE message_key IS NOT NULL"
        )
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(group_state)")
        }
        if "thread_replies" not in columns:
            self._conn.execute(
                "ALTER TABLE group_state ADD COLUMN thread_replies INTEGER NOT NULL DEFAULT 0"
            )
        if "followup_user_id" not in columns:
            self._conn.execute(
                "ALTER TABLE group_state ADD COLUMN followup_user_id INTEGER"
            )
        if "last_segment" not in columns:
            self._conn.execute("ALTER TABLE group_state ADD COLUMN last_segment TEXT")
        if not self._conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 1"
        ).fetchone():
            # Rebuild once from retained raw text, also repairing old empty-text deletes.
            self._conn.execute("DROP TABLE raw_messages_fts")
            self._conn.execute("""CREATE VIRTUAL TABLE raw_messages_fts USING fts5(
                text, content='raw_messages', content_rowid='id', tokenize='trigram')""")
            self._conn.execute(
                "INSERT INTO raw_messages_fts(raw_messages_fts) VALUES('rebuild')"
            )
            self._conn.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS raw_conversation_time ON raw_messages(conversation_id, created_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS raw_expiry ON raw_messages(expires_at)"
        )
        if not self._conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 2"
        ).fetchone():
            if self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'profiles'"
            ).fetchone():
                if (
                    self._conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
                    == 0
                ):
                    self._conn.execute("DROP TABLE profiles")
            self._conn.execute("INSERT INTO schema_migrations(version) VALUES (2)")
        media_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(media_assets)")
        }
        for name, definition in {
            "reviewed_at": "TEXT",
            "review_version": "TEXT NOT NULL DEFAULT ''",
            "review_reason": "TEXT NOT NULL DEFAULT ''",
            "auto_retired_at": "TEXT",
            "quarantine_path": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in media_columns:
                self._conn.execute(
                    f"ALTER TABLE media_assets ADD COLUMN {name} {definition}"
                )
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version) VALUES (3)"
        )
        episode_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(episodes)")
        }
        if "context_hidden" not in episode_columns:
            self._conn.execute(
                "ALTER TABLE episodes ADD COLUMN context_hidden INTEGER NOT NULL DEFAULT 0"
            )
        for row in self._conn.execute(
            "SELECT id,bot_id,scope,group_id,conversation_id FROM raw_messages WHERE chat_key='' "
        ).fetchall():
            target = (
                row["group_id"]
                if row["scope"] == "group"
                else row["conversation_id"].split(":")[1]
            )
            self._conn.execute(
                "UPDATE raw_messages SET chat_key=? WHERE id=?",
                (f"{row['bot_id']}:{row['scope']}:{target}", row["id"]),
            )
        self._conn.execute(
            "INSERT OR IGNORE INTO media_sources SELECT source_key,id,last_seen_at FROM media_assets"
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO memory_meta VALUES ('raw_sequence','0')"
        )
        self._conn.execute(
            "UPDATE memory_meta SET value=MAX(CAST(value AS INTEGER),(SELECT COALESCE(MAX(id),0) FROM raw_messages)) WHERE key='raw_sequence'"
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO memory_meta VALUES ('episode_sequence','0')"
        )
        self._conn.execute(
            "UPDATE memory_meta SET value=MAX(CAST(value AS INTEGER),(SELECT COALESCE(MAX(id),0) FROM episodes)) WHERE key='episode_sequence'"
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def audit(
        self,
        *,
        scope: Scope,
        allowed: bool,
        decision: str,
        user_id: int,
        group_id: int | None,
        message_id: int | None,
        text: str,
        retention: timedelta,
        now: datetime | None = None,
    ) -> None:
        current = now or utcnow()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit_log (
                    created_at, expires_at, scope, allowed, decision,
                    user_id, group_id, message_id, text, text_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    iso(current),
                    iso(current + retention),
                    scope,
                    int(allowed),
                    decision,
                    user_id,
                    group_id,
                    message_id,
                    text,
                    text_hash(text),
                ),
            )
            self._conn.commit()

    def add_raw(
        self,
        *,
        scope: Scope,
        conversation_id: str,
        user_id: int,
        group_id: int | None,
        sender: str,
        text: str,
        retention: timedelta,
        now: datetime | None = None,
        bot_id: str = "",
        message_id: int | None = None,
        sender_name: str = "",
        reply_to_id: int | None = None,
        reply_sender: str = "",
        reply_text: str = "",
        is_proactive: bool = False,
        media: list[dict] | None = None,
        is_control: bool = False,
    ) -> int | None:
        current = now or utcnow()
        target = group_id if scope == "group" else conversation_id.split(":")[1]
        message_key = (
            f"{bot_id}:{scope}:{target}:{message_id}"
            if message_id is not None
            else None
        )
        chat_key = f"{bot_id}:{scope}:{target}"
        hidden = self.memory.blocked(
            bot_id=bot_id,
            user_id=user_id,
            scope=scope,
            group_id=group_id,
            message_key=message_key or "",
            text=text,
        )
        if reply_to_id is not None:
            quote_key = f"{chat_key}:{reply_to_id}"
            hidden = hidden or bool(
                self._conn.execute(
                    "SELECT 1 FROM raw_messages WHERE message_key=? AND (context_hidden=1 OR is_recalled=1)",
                    (quote_key,),
                ).fetchone()
            )
            hidden = hidden or bool(
                self._conn.execute(
                    "SELECT 1 FROM memory_blocks WHERE kind='source' AND value=?",
                    (quote_key,),
                ).fetchone()
            )
        with self._lock:
            raw_id = (
                int(
                    self._conn.execute(
                        "SELECT value FROM memory_meta WHERE key='raw_sequence'"
                    ).fetchone()[0]
                )
                + 1
            )
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO raw_messages (
                    id, created_at, expires_at, scope, conversation_id,
                    user_id, group_id, sender, text, message_key, message_id,
                    bot_id, sender_name, reply_to_id, reply_sender, reply_text, is_proactive, media_json, chat_key, context_hidden, is_control
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raw_id,
                    iso(current),
                    iso(current + retention),
                    scope,
                    conversation_id,
                    user_id,
                    group_id,
                    sender,
                    text,
                    message_key,
                    message_id,
                    bot_id,
                    sender_name,
                    reply_to_id,
                    reply_sender,
                    reply_text,
                    int(is_proactive),
                    json.dumps(media or [], ensure_ascii=False),
                    chat_key,
                    int(hidden),
                    int(is_control),
                ),
            )
            if cursor.rowcount == 0:
                self._conn.commit()
                return None
            self._conn.execute(
                "INSERT INTO raw_messages_fts(rowid, text) VALUES (?, ?)",
                (cursor.lastrowid, text),
            )
            self._conn.execute(
                "UPDATE memory_meta SET value=? WHERE key='raw_sequence'",
                (str(raw_id),),
            )
            self._conn.commit()
            raw_id = int(cursor.lastrowid)
            if user_id and not is_control and not hidden and text.strip():
                self.memory.enqueue(raw_id)
            return raw_id

    def recent_messages(
        self,
        conversation_id: str,
        limit: int,
        before: datetime | None = None,
    ) -> list[sqlite3.Row]:
        is_short = len(conversation_id.split(":")) <= 2
        with self._lock:
            if before is None:
                if is_short:
                    return list(
                        self._conn.execute(
                            """
                            SELECT * FROM raw_messages
                            WHERE (conversation_id = ? OR conversation_id LIKE ? || ':%') AND is_recalled = 0 AND context_hidden = 0 AND is_control = 0
                            ORDER BY created_at DESC, id DESC LIMIT ?
                            """,
                            (conversation_id, conversation_id, limit),
                        )
                    )[::-1]
                return list(
                    self._conn.execute(
                        """
                        SELECT * FROM raw_messages
                        WHERE conversation_id = ? AND is_recalled = 0 AND context_hidden = 0 AND is_control = 0
                        ORDER BY created_at DESC, id DESC LIMIT ?
                        """,
                        (conversation_id, limit),
                    )
                )[::-1]
            if is_short:
                return list(
                    self._conn.execute(
                        """
                        SELECT * FROM raw_messages
                        WHERE (conversation_id = ? OR conversation_id LIKE ? || ':%') AND created_at < ? AND is_recalled = 0 AND context_hidden = 0 AND is_control = 0
                        ORDER BY created_at DESC, id DESC LIMIT ?
                        """,
                        (conversation_id, conversation_id, iso(before), limit),
                    )
                )[::-1]
            return list(
                self._conn.execute(
                    """
                    SELECT * FROM raw_messages
                    WHERE conversation_id = ? AND created_at < ? AND is_recalled = 0 AND context_hidden = 0 AND is_control = 0
                    ORDER BY created_at DESC, id DESC LIMIT ?
                    """,
                    (conversation_id, iso(before), limit),
                )
            )[::-1]

    def search_raw(self, conversation_id: str, query: str, limit: int) -> list[dict]:
        terms = [term for term in query.split() if term.strip()]
        if not terms:
            return []
        match = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:12])
        is_short = len(conversation_id.split(":")) <= 2
        with self._lock:
            try:
                if is_short:
                    rows = self._conn.execute(
                        """
                        SELECT raw_messages.id, raw_messages.sender, raw_messages.text
                        FROM raw_messages_fts
                        JOIN raw_messages ON raw_messages.id = raw_messages_fts.rowid
                        WHERE raw_messages_fts MATCH ? AND raw_messages.is_recalled = 0 AND raw_messages.context_hidden = 0 AND raw_messages.is_control = 0
                          AND (raw_messages.conversation_id = ? OR raw_messages.conversation_id LIKE ? || ':%')
                        ORDER BY bm25(raw_messages_fts) LIMIT ?
                        """,
                        (match, conversation_id, conversation_id, limit),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        """
                        SELECT raw_messages.id, raw_messages.sender, raw_messages.text
                        FROM raw_messages_fts
                        JOIN raw_messages ON raw_messages.id = raw_messages_fts.rowid
                        WHERE raw_messages_fts MATCH ? AND raw_messages.is_recalled = 0 AND raw_messages.context_hidden = 0 AND raw_messages.is_control = 0 AND raw_messages.conversation_id = ?
                        ORDER BY bm25(raw_messages_fts) LIMIT ?
                        """,
                        (match, conversation_id, limit),
                    ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(row) for row in rows]

    def latest_episode_end(self, conversation_id: str) -> int:
        is_short = len(conversation_id.split(":")) <= 2
        with self._lock:
            if is_short:
                row = self._conn.execute(
                    "SELECT MAX(end_message_id) AS end_id FROM episodes WHERE conversation_id = ? OR conversation_id LIKE ? || ':%'",
                    (conversation_id, conversation_id),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT MAX(end_message_id) AS end_id FROM episodes WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
        return int(row["end_id"] or 0)

    def add_episode(
        self,
        *,
        conversation_id: str,
        start_message_id: int,
        end_message_id: int,
        summary: str,
        retention: timedelta,
        now: datetime | None = None,
    ) -> None:
        current = now or utcnow()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO episodes (
                    id, created_at, expires_at, conversation_id,
                    start_message_id, end_message_id, summary
                )
                VALUES ((SELECT CAST(value AS INTEGER)+1 FROM memory_meta WHERE key='episode_sequence'), ?, ?, ?, ?, ?, ?)
                """,
                (
                    iso(current),
                    iso(current + retention),
                    conversation_id,
                    start_message_id,
                    end_message_id,
                    summary,
                ),
            )
            self._conn.execute(
                "UPDATE memory_meta SET value=CAST(value AS INTEGER)+1 WHERE key='episode_sequence'"
            )
            self._conn.commit()

    def recent_episodes(
        self,
        conversation_id: str,
        limit: int,
        now: datetime | None = None,
        *,
        before_message_id: int | None = None,
    ) -> list[dict]:
        current = iso(now or utcnow())
        is_short = len(conversation_id.split(":")) <= 2
        with self._lock:
            if is_short:
                rows = self._conn.execute(
                    """
                    SELECT id, summary FROM episodes
                    WHERE context_hidden=0 AND (conversation_id = ? OR conversation_id LIKE ? || ':%') AND expires_at > ?
                      AND (? IS NULL OR end_message_id < ?)
                    ORDER BY id DESC LIMIT ?
                    """,
                    (
                        conversation_id,
                        conversation_id,
                        current,
                        before_message_id,
                        before_message_id,
                        limit,
                    ),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT id, summary FROM episodes
                    WHERE context_hidden=0 AND conversation_id = ? AND expires_at > ?
                      AND (? IS NULL OR end_message_id < ?)
                    ORDER BY id DESC LIMIT ?
                    """,
                    (
                        conversation_id,
                        current,
                        before_message_id,
                        before_message_id,
                        limit,
                    ),
                ).fetchall()
        return [dict(row) for row in rows][::-1]

    def update_episode(
        self, conversation_id: str, now: datetime, retention_days: int
    ) -> None:
        rows = self.recent_messages(conversation_id, 24)
        covered = self.latest_episode_end(conversation_id)
        fresh = [row for row in rows if row["id"] > covered]
        if len(fresh) < 8:
            return
        summary = summarize_episode([(row["sender"], row["text"]) for row in fresh])
        if summary:
            self.add_episode(
                conversation_id=conversation_id,
                start_message_id=fresh[0]["id"],
                end_message_id=fresh[-1]["id"],
                summary=summary,
                retention=timedelta(days=retention_days),
                now=now,
            )

    def assign_private_session(self, raw_id: int, user_id: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE raw_messages SET conversation_id=? WHERE id=?",
                (self.get_active_private_session(user_id), raw_id),
            )

    def get_active_private_session(self, user_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT conversation_id FROM conversation_sessions
                WHERE scope = 'private' AND user_id = ? AND status = 'active'
                ORDER BY id DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row:
                return str(row["conversation_id"])
            return f"private:{user_id}"

    def reset_private_session(self, user_id: int, now: datetime | None = None) -> str:
        current = now or utcnow()
        current_iso = iso(current)
        with self._lock:
            active_rows = self._conn.execute(
                """
                SELECT id, conversation_id FROM conversation_sessions
                WHERE scope = 'private' AND user_id = ? AND status = 'active'
                """,
                (user_id,),
            ).fetchall()
            for row in active_rows:
                self._conn.execute(
                    "UPDATE conversation_sessions SET status = 'closed', closed_at = ? WHERE id = ?",
                    (current_iso, row["id"]),
                )

            existing_default = self._conn.execute(
                "SELECT 1 FROM raw_messages WHERE conversation_id = ? LIMIT 1",
                (f"private:{user_id}",),
            ).fetchone()
            if existing_default:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO conversation_sessions (
                        scope, user_id, conversation_id, status, created_at, closed_at
                    ) VALUES ('private', ?, ?, 'closed', ?, ?)
                    """,
                    (user_id, f"private:{user_id}", current_iso, current_iso),
                )

            new_conv_id = f"private:{user_id}:{uuid4().hex}"
            self._conn.execute(
                """
                INSERT INTO conversation_sessions (
                    scope, user_id, conversation_id, status, created_at
                ) VALUES ('private', ?, ?, 'active', ?)
                """,
                (user_id, new_conv_id, current_iso),
            )
            self._conn.commit()
            return new_conv_id

    def get_group_conversation_id(
        self, group_id: int, now: datetime | None = None
    ) -> str:
        current = now or utcnow()
        current_iso = iso(current)
        seg = group_conversation_date(current)
        conv_id = f"group:{group_id}:{seg}"
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO conversation_sessions (
                    scope, group_id, conversation_id, status, created_at
                ) VALUES ('group', ?, ?, 'active', ?)
                """,
                (group_id, conv_id, current_iso),
            )
            self._conn.commit()
        return conv_id

    def purge(
        self,
        now: datetime | None = None,
        *,
        usage_retention_days: int = 90,
        observed_media_retention_days: int = 30,
    ) -> dict[str, int]:
        current_dt = now or utcnow()
        current = iso(current_dt)
        with self._lock, self._conn:
            before = {
                table: self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "raw_messages",
                    "episodes",
                    "audit_log",
                    "conversation_sessions",
                    "media_assets",
                    "media_review_log",
                    "usage_daily",
                )
            }
            expired = self._conn.execute(
                "SELECT id, text FROM raw_messages WHERE expires_at <= ?",
                (current,),
            ).fetchall()
            for row in expired:
                self._conn.execute(
                    "INSERT INTO raw_messages_fts(raw_messages_fts, rowid, text) VALUES('delete', ?, ?)",
                    (row["id"], row["text"]),
                )
            self._conn.execute(
                "DELETE FROM raw_messages WHERE expires_at <= ?", (current,)
            )
            self._conn.execute("DELETE FROM episodes WHERE expires_at <= ?", (current,))
            self._conn.execute(
                "DELETE FROM audit_log WHERE expires_at <= ?", (current,)
            )
            self._conn.execute(
                "UPDATE memories SET status = 'archived' WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (current,),
            )

            # Clean up closed private sessions older than 24 hours
            cutoff_24h = iso(current_dt - timedelta(hours=24))
            old_closed_sessions = self._conn.execute(
                "SELECT conversation_id FROM conversation_sessions WHERE status = 'closed' AND closed_at <= ?",
                (cutoff_24h,),
            ).fetchall()
            for row in old_closed_sessions:
                c_id = str(row["conversation_id"])
                msg_ids = self._conn.execute(
                    "SELECT id, text FROM raw_messages WHERE conversation_id = ?",
                    (c_id,),
                ).fetchall()
                for m_row in msg_ids:
                    self._conn.execute(
                        "INSERT INTO raw_messages_fts(raw_messages_fts, rowid, text) VALUES('delete', ?, ?)",
                        (m_row["id"], m_row["text"]),
                    )
                self._conn.execute(
                    "DELETE FROM raw_messages WHERE conversation_id = ?", (c_id,)
                )
                self._conn.execute(
                    "DELETE FROM episodes WHERE conversation_id = ?", (c_id,)
                )
                self._conn.execute(
                    "DELETE FROM conversation_sessions WHERE conversation_id = ?",
                    (c_id,),
                )

            # A date segment is an archive partition, not a context or retention boundary.
            self._conn.execute(
                """
                DELETE FROM conversation_sessions WHERE scope = 'group'
                  AND NOT EXISTS (SELECT 1 FROM raw_messages WHERE raw_messages.conversation_id = conversation_sessions.conversation_id)
                  AND NOT EXISTS (SELECT 1 FROM episodes WHERE episodes.conversation_id = conversation_sessions.conversation_id)
                """
            )

            self._conn.execute(
                "DELETE FROM usage_daily WHERE day < ?",
                (
                    (current_dt - timedelta(days=usage_retention_days))
                    .astimezone(UTC)
                    .date()
                    .isoformat(),
                ),
            )
            self._conn.execute(
                "DELETE FROM media_review_log WHERE created_at < ?",
                (iso(current_dt - timedelta(days=observed_media_retention_days)),),
            )
            self._conn.execute(
                """DELETE FROM media_assets WHERE storage = 'observed' AND favorite = 0 AND last_seen_at < ?
                   AND NOT EXISTS (
                       SELECT 1 FROM raw_messages, json_each(raw_messages.media_json) AS media
                       WHERE json_extract(media.value, '$.asset_id') = media_assets.id
                   )""",
                (iso(current_dt - timedelta(days=observed_media_retention_days)),),
            )
            self._conn.execute(
                "DELETE FROM memory_jobs WHERE raw_id NOT IN (SELECT id FROM raw_messages)"
            )
            self._conn.execute(
                "DELETE FROM media_sources WHERE asset_id NOT IN (SELECT id FROM media_assets)"
            )
            self._conn.execute(
                "DELETE FROM media_jobs WHERE NOT EXISTS (SELECT 1 FROM json_each(media_jobs.refs) ref JOIN raw_messages r ON r.id=json_extract(ref.value,'$[0]'))"
            )
            return {
                table: count
                - self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table, count in before.items()
            }

    def note_private_message(
        self,
        bot_id: str,
        user_id: int,
        *,
        incoming: bool,
        proactive: bool = False,
        now: datetime,
        next_check_at: datetime,
    ) -> None:
        stamp = iso(now)
        day = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        with self._lock:
            if incoming:
                self._conn.execute(
                    """
                    INSERT INTO private_activity (bot_id, user_id, last_user_at, last_message_at, next_check_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(bot_id, user_id) DO UPDATE SET
                        last_user_at = excluded.last_user_at,
                        last_message_at = excluded.last_message_at,
                        awaiting_reply = 0,
                        next_check_at = excluded.next_check_at
                    """,
                    (bot_id, user_id, stamp, stamp, iso(next_check_at)),
                )
            elif proactive:
                self._conn.execute(
                    """
                    UPDATE private_activity SET last_message_at = ?, last_proactive_at = ?, awaiting_reply = 1,
                        sent_count = CASE WHEN day = ? THEN sent_count + 1 ELSE 1 END, day = ?, next_check_at = ?
                    WHERE bot_id = ? AND user_id = ?
                    """,
                    (stamp, stamp, day, day, iso(next_check_at), bot_id, user_id),
                )
            else:
                self._conn.execute(
                    "UPDATE private_activity SET last_message_at = ?, next_check_at = ? WHERE bot_id = ? AND user_id = ?",
                    (stamp, iso(next_check_at), bot_id, user_id),
                )
            self._conn.commit()

    def proactive_candidates(self, now: datetime) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """
                SELECT * FROM private_activity
                WHERE next_check_at <= ?
                """,
                (iso(now),),
            ).fetchall()

    def schedule_proactive(
        self, bot_id: str, user_id: int, now: datetime, next_check_at: datetime
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE private_activity SET last_attempt_at = ?, next_check_at = ? WHERE bot_id = ? AND user_id = ?",
                (iso(now), iso(next_check_at), bot_id, user_id),
            )
            self._conn.commit()

    def tokens_today(self, user_id: int, now: datetime | None = None) -> int:
        day = (now or utcnow()).date().isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT tokens FROM usage_daily WHERE day = ? AND user_id = ?",
                (day, user_id),
            ).fetchone()
        return int(row["tokens"]) if row else 0

    def recall_message(self, bot_id: str, group_id: int, message_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE raw_messages SET is_recalled=1 WHERE bot_id=? AND group_id=? AND message_id=?",
                (bot_id, group_id, message_id),
            )
        self.memory.invalidate_source(f"{bot_id}:group:{group_id}:{message_id}")

    def raw_available(self, raw_id: int) -> bool:
        self.memory.epoch()
        with self._lock:
            row = self._conn.execute(
                "SELECT is_recalled,context_hidden FROM raw_messages WHERE id = ?",
                (raw_id,),
            ).fetchone()
        return row is not None and not row["is_recalled"] and not row["context_hidden"]

    def raw_message(self, raw_id: int) -> dict:
        with self._lock:
            return dict(
                self._conn.execute(
                    "SELECT * FROM raw_messages WHERE id = ?", (raw_id,)
                ).fetchone()
            )

    def quoted_message(
        self, conversation_id: str, bot_id: str, message_id: int
    ) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM raw_messages WHERE context_hidden=0 AND is_control=0 AND bot_id = ? AND message_id = ?
                   AND (conversation_id = ? OR conversation_id LIKE ? || ':%')
                   ORDER BY id DESC LIMIT 1""",
                (bot_id, message_id, conversation_id, conversation_id),
            ).fetchone()
        return dict(row) if row else None

    def context_messages(
        self, conversation_id: str, bot_id: str, ids: list[int]
    ) -> list[dict]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"""SELECT * FROM raw_messages WHERE id IN ({placeholders}) AND bot_id=?
            AND (conversation_id=? OR conversation_id LIKE ?||':%')
            AND context_hidden=0 AND is_recalled=0 AND is_control=0 ORDER BY id""",
            (*ids, bot_id, conversation_id, conversation_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def recent_dialogue(
        self,
        conversation_id: str,
        bot_id: str,
        user_id: int,
        before: datetime,
        limit: int = 8,
    ) -> list[dict]:
        rows = self._conn.execute(
            """SELECT r.* FROM raw_messages r
            WHERE r.bot_id=? AND (r.conversation_id=? OR r.conversation_id LIKE ?||':%')
            AND r.created_at<=? AND r.context_hidden=0 AND r.is_recalled=0 AND r.is_control=0
            AND (r.user_id=? OR (r.user_id=0 AND (r.scope='private' OR EXISTS (
              SELECT 1 FROM raw_messages s WHERE s.chat_key=r.chat_key AND s.user_id=?
              AND s.context_hidden=0 AND s.is_recalled=0 AND s.message_id=r.reply_to_id))))
            ORDER BY r.created_at DESC,r.id DESC LIMIT ?""",
            (
                bot_id,
                conversation_id,
                conversation_id,
                iso(before),
                user_id,
                user_id,
                limit,
            ),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def recent_media_message(
        self, conversation_id: str, bot_id: str, before: datetime, seconds: float
    ) -> dict | None:
        rows = self._conn.execute(
            """SELECT * FROM raw_messages WHERE bot_id=?
            AND (conversation_id=? OR conversation_id LIKE ?||':%')
            AND created_at>=? AND created_at<? AND user_id!=0 AND media_json!='[]'
            AND context_hidden=0 AND is_recalled=0 AND is_control=0
            ORDER BY created_at DESC,id DESC""",
            (
                bot_id,
                conversation_id,
                conversation_id,
                iso(before - timedelta(seconds=seconds)),
                iso(before),
            ),
        )
        for row in rows:
            if any(
                item["kind"] in {"image", "mface", "forward", "node"}
                for item in json.loads(row["media_json"])
            ):
                return dict(row)
        return None

    def reply_chain(
        self, conversation_id: str, bot_id: str, message_id: int
    ) -> list[dict]:
        chain = []
        seen = set()
        for _ in range(3):
            if message_id is None or message_id in seen:
                break
            seen.add(message_id)
            row = self.quoted_message(conversation_id, bot_id, message_id)
            if row is None or row["is_recalled"]:
                break
            chain.append(row)
            message_id = row["reply_to_id"]
            if message_id is None and row["user_id"] == 0:
                origin = self._conn.execute(
                    """SELECT t.source_message_id FROM reply_parts p JOIN reply_turns t
                    USING(request_id) WHERE p.raw_id=? AND t.bot_id=?""",
                    (row["id"], bot_id),
                ).fetchone()
                if origin is not None:
                    message_id = origin[0]
        return chain

    def add_tokens(
        self, user_id: int, tokens: int, now: datetime | None = None
    ) -> None:
        day = (now or utcnow()).date().isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO usage_daily(day, user_id, tokens) VALUES (?, ?, ?)
                ON CONFLICT(day, user_id) DO UPDATE SET tokens = tokens + excluded.tokens
                """,
                (day, user_id, tokens),
            )
            self._conn.commit()

    def group_state(self, group_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM group_state WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        if not row:
            return {
                "last_spoke_at": None,
                "active_thread_until": None,
                "followup_user_id": None,
                "recent_speakers": [],
            }
        return {
            "last_spoke_at": row["last_spoke_at"],
            "active_thread_until": row["active_thread_until"],
            "followup_user_id": row["followup_user_id"],
            "recent_speakers": json.loads(row["recent_speakers_json"]),
        }

    def save_group_state(self, group_id: int, state: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO group_state (
                    group_id, last_spoke_at, active_thread_until,
                    followup_user_id, recent_speakers_json
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    last_spoke_at = excluded.last_spoke_at,
                    active_thread_until = excluded.active_thread_until,
                    followup_user_id = excluded.followup_user_id,
                    recent_speakers_json = excluded.recent_speakers_json
                """,
                (
                    group_id,
                    state["last_spoke_at"],
                    state["active_thread_until"],
                    state.get("followup_user_id"),
                    json.dumps(state["recent_speakers"], ensure_ascii=False),
                ),
            )
            self._conn.commit()
