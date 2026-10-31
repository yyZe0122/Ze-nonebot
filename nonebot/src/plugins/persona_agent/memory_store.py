from __future__ import annotations

import json
import os
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .storage import iso, text_hash, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_entries (
 id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, user_id INTEGER NOT NULL,
 scope TEXT NOT NULL, group_id INTEGER, kind TEXT NOT NULL, text TEXT NOT NULL,
 fingerprint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, supersedes INTEGER, legacy_id INTEGER,
 participants_json TEXT NOT NULL DEFAULT '[]', change_kind TEXT NOT NULL DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS memory_subject ON memory_entries(bot_id,user_id,scope,group_id,status);
CREATE INDEX IF NOT EXISTS memory_group ON memory_entries(bot_id,scope,group_id,status);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_entries_fts USING fts5(text, tokenize='trigram');
CREATE TABLE IF NOT EXISTS memory_evidence (
 id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL, message_key TEXT NOT NULL,
 raw_id INTEGER, created_at TEXT NOT NULL, user_id INTEGER NOT NULL,
 excerpt TEXT NOT NULL, body_hash TEXT NOT NULL, UNIQUE(memory_id,message_key)
);
CREATE TABLE IF NOT EXISTS memory_jobs (
 raw_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_at TEXT NOT NULL, error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS memory_pending ON memory_jobs(status,next_at,raw_id);
CREATE TABLE IF NOT EXISTS memory_blocks (
 block_key TEXT PRIMARY KEY, kind TEXT NOT NULL, bot_id TEXT NOT NULL, user_id INTEGER NOT NULL,
 scope TEXT NOT NULL, group_id INTEGER, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forgetting_applied (id TEXT PRIMARY KEY, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_vectors (
 memory_id INTEGER PRIMARY KEY REFERENCES memory_entries(id) ON DELETE CASCADE,
 model_key TEXT NOT NULL, fingerprint TEXT NOT NULL, vector BLOB NOT NULL
);
"""


def fingerprint(text: str) -> str:
    return text_hash(re.sub(r"\W+", "", text).lower())


@dataclass(frozen=True)
class MemoryRecord:
    id: int
    text: str
    source_scope: str
    layer: str
    kind: str
    confidence: float
    salience: float
    subject_user_id: int
    group_id: int | None
    participants: tuple[int, ...] = ()
    updated_at: str = ""


class MemoryStore:
    def __init__(self, conn, ledger: Path) -> None:
        self.conn = conn
        self.ledger = ledger
        self.pending = False
        self.replay()

    def epoch(self) -> int:
        if self.pending:
            raise RuntimeError(
                "Forgetting is pending; model context is disabled until reconciliation"
            )
        row = self.conn.execute(
            "SELECT value FROM memory_meta WHERE key='epoch'"
        ).fetchone()
        return int(row[0]) if row else 0

    def _advance(self) -> None:
        self.conn.execute(
            "INSERT INTO memory_meta VALUES ('epoch','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1"
        )

    def _visible(
        self, entry, bot_id: str, user_id: int, scope: str, group_id: int | None
    ) -> bool:
        if entry["bot_id"] != bot_id:
            return False
        if scope == "group":
            return entry["scope"] == "group" and entry["group_id"] == group_id
        return entry["user_id"] == user_id or (
            entry["kind"] == "experience"
            and entry["user_id"] == 0
            and user_id in json.loads(entry["participants_json"])
        )

    def can_edit(self, entry, user_id: int, scope: str) -> bool:
        if entry["status"] != "active":
            return False
        if entry["user_id"]:
            return entry["user_id"] == user_id
        return scope == "group" and any(
            source["user_id"] == user_id for source in self.evidence(entry["id"])
        )

    def list(
        self,
        bot_id: str,
        user_id: int,
        scope: str,
        group_id: int | None,
        limit=30,
        offset=0,
    ) -> list[dict]:
        self.epoch()
        rows = self.conn.execute(
            "SELECT * FROM memory_entries WHERE bot_id=? AND status='active' ORDER BY updated_at DESC,id DESC",
            (bot_id,),
        )
        return [
            dict(row)
            for row in rows
            if self._visible(row, bot_id, user_id, scope, group_id)
        ][offset : offset + limit]

    def get(
        self,
        memory_id: int,
        bot_id: str,
        user_id: int,
        scope: str,
        group_id: int | None,
    ) -> dict | None:
        self.epoch()
        row = self.conn.execute(
            "SELECT * FROM memory_entries WHERE id=?", (memory_id,)
        ).fetchone()
        return (
            dict(row)
            if row and self._visible(row, bot_id, user_id, scope, group_id)
            else None
        )

    def evidence(self, memory_id: int) -> list[dict]:
        self.epoch()
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM memory_evidence WHERE memory_id=? ORDER BY created_at",
                (memory_id,),
            )
        ]

    def recall(
        self,
        bot_id: str,
        user_id: int,
        scope: str,
        group_id: int | None,
        query: str,
        limit: int,
        *,
        query_vector=(),
        model_key: str = "",
        peers: tuple[int, ...] = (),
        profile_limit: int = 4,
        min_similarity: float = 0.45,
    ) -> list[MemoryRecord]:
        self.epoch()
        # Apply visibility before scoring. A global top-k followed by filtering can leak
        # cross-chat information and lose the best results inside a small group.
        rows = self.visible(bot_id, user_id, scope, group_id, model_key=model_key)
        by_id = {row["id"]: row for row in rows}
        terms = set(
            re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", query.lower()[:600])
        )
        grams = {part[i : i + 2] for part in terms for i in range(len(part) - 1)}
        words = sorted(
            {part[i : i + 3] for part in terms for i in range(len(part) - 2)}
        )[:40]
        keyword = {}
        if words:
            search = " OR ".join('"' + word.replace('"', '""') + '"' for word in words)
            keyword = {
                row[0]: 1.0
                for row in self.conn.execute(
                    "SELECT rowid FROM memory_entries_fts WHERE memory_entries_fts MATCH ?",
                    (search,),
                )
                if row[0] in by_id
            }
        semantic = {}
        for row in rows:
            overlap = sum(gram in row["text"].lower() for gram in grams)
            if overlap:
                keyword[row["id"]] = keyword.get(row["id"], 0) + overlap
            vector = row["vector"]
            if (
                query_vector
                and vector is not None
                and len(vector) == len(query_vector) * 4
            ):
                values = struct.unpack(f"<{len(query_vector)}f", vector)
                similarity = sum(a * b for a, b in zip(query_vector, values))
                if similarity >= min_similarity:
                    semantic[row["id"]] = similarity
        scores = {}
        for ranking in (keyword, semantic):
            for index, ident in enumerate(
                sorted(ranking, key=ranking.get, reverse=True)[:40]
            ):
                scores[ident] = scores.get(ident, 0) + 1 / (20 + index)
        now = utcnow()
        for ident in scores:
            row = by_id[ident]
            if row["kind"] in {"experience", "impression"}:
                days = max(
                    0,
                    (now - datetime.fromisoformat(row["updated_at"])).total_seconds()
                    / 86400,
                )
                scores[ident] *= 0.7 + 0.3 / (1 + days / 30)
            if row["user_id"] in {user_id, *peers}:
                scores[ident] *= 1.1

        # A small current profile is available even for short messages like "晚上好".
        selected = []
        subjects = (
            list(dict.fromkeys([user_id, *peers])) if scope == "group" else [user_id]
        )
        for subject in subjects[:4]:
            profile = [
                r
                for r in rows
                if r["user_id"] == subject
                and r["kind"] in {"fact", "preference", "impression"}
            ]
            profile.sort(
                key=lambda r: (r["kind"] != "impression", r["updated_at"]), reverse=True
            )
            quota = (
                profile_limit if scope == "private" else 2 if subject == user_id else 1
            )
            stable = [r for r in profile if r["kind"] != "impression"]
            impressions = [r for r in profile if r["kind"] == "impression"][:1]
            core = (stable[: max(1, quota - len(impressions))] + impressions)[:quota]
            selected.extend(r["id"] for r in core)
        norms = [r for r in rows if r["kind"] == "group"]
        norms.sort(key=lambda r: r["updated_at"], reverse=True)
        selected = selected[: max(0, profile_limit - bool(norms))] + [
            r["id"] for r in norms[:1]
        ]
        selected = selected[: min(profile_limit, limit)]
        for ident in sorted(
            scores,
            key=lambda ident: (scores[ident], by_id[ident]["updated_at"]),
            reverse=True,
        ):
            if ident not in selected:
                selected.append(ident)
            if len(selected) >= limit:
                break
        return [
            MemoryRecord(
                row["id"],
                row["text"],
                row["scope"],
                row["kind"],
                "inferred" if row["kind"] == "impression" else "claim",
                0.5 if row["kind"] == "impression" else 1.0,
                1.0,
                row["user_id"],
                row["group_id"],
                tuple(json.loads(row["participants_json"])),
                row["updated_at"],
            )
            for row in (by_id[ident] for ident in selected[:limit])
        ]

    def visible(self, bot_id, user_id, scope, group_id, *, model_key="") -> list[dict]:
        self.epoch()
        if scope == "group":
            clause, args = "m.scope='group' AND m.group_id=?", (group_id,)
        else:
            clause = "(m.user_id=? OR (m.user_id=0 AND m.kind='experience' AND EXISTS (SELECT 1 FROM json_each(m.participants_json) p WHERE p.value=?)))"
            args = (user_id, user_id)
        return [
            dict(row)
            for row in self.conn.execute(
                f"""SELECT m.*,v.vector FROM memory_entries m LEFT JOIN memory_vectors v
            ON v.memory_id=m.id AND v.model_key=? AND v.fingerprint=m.fingerprint
            WHERE m.bot_id=? AND m.status='active' AND {clause} ORDER BY m.updated_at DESC,m.id DESC""",
                (model_key, bot_id, *args),
            )
        ]

    def pending_vectors(self, model_key: str, limit: int) -> list[dict]:
        return [
            dict(row)
            for row in self.conn.execute(
                """SELECT m.id,m.text,m.fingerprint FROM memory_entries m LEFT JOIN memory_vectors v
            ON v.memory_id=m.id WHERE m.status='active'
            AND (v.memory_id IS NULL OR v.model_key!=? OR v.fingerprint!=m.fingerprint)
            ORDER BY m.updated_at DESC,m.id DESC LIMIT ?""",
                (model_key, limit),
            )
        ]

    def save_vectors(
        self, model_key: str, records: list[dict], vectors: list[list[float]]
    ) -> None:
        with self.conn:
            for row, vector in zip(records, vectors, strict=True):
                self.conn.execute(
                    """INSERT INTO memory_vectors(memory_id,model_key,fingerprint,vector)
                    SELECT id,?,fingerprint,? FROM memory_entries WHERE id=? AND fingerprint=? AND status='active'
                    ON CONFLICT(memory_id) DO UPDATE SET model_key=excluded.model_key,
                    fingerprint=excluded.fingerprint,vector=excluded.vector""",
                    (
                        model_key,
                        struct.pack(f"<{len(vector)}f", *vector),
                        row["id"],
                        row["fingerprint"],
                    ),
                )

    def blocked(
        self,
        *,
        bot_id: str,
        user_id: int,
        scope: str,
        group_id: int | None,
        message_key: str = "",
        text: str = "",
        entry_text: str = "",
    ) -> bool:
        self.epoch()
        values = [
            ("source", message_key),
            ("body", text_hash(text)),
            ("entry", fingerprint(entry_text)),
        ]
        for kind, value in values:
            if (
                (kind == "source" and not message_key)
                or (kind == "body" and len(text.strip()) < 8)
                or (kind == "entry" and not entry_text)
            ):
                continue
            if self.conn.execute(
                """SELECT 1 FROM memory_blocks WHERE kind=? AND value=? AND (user_id=? OR kind='source')
               AND bot_id IN ('',?) AND scope=? AND group_id IS ?""",
                (kind, value, user_id, bot_id, scope, group_id),
            ).fetchone():
                return True
        return False

    def save(
        self,
        *,
        bot_id: str,
        user_id: int,
        scope: str,
        group_id: int | None,
        kind: str,
        text: str,
        sources: list[dict],
        excerpt: str,
        limit: int,
        supersedes=None,
        expected_epoch=None,
        participants: tuple[int, ...] = (),
        change_kind: str = "new",
        actor_user_id: int | None = None,
    ) -> int | None:
        if expected_epoch is not None and expected_epoch != self.epoch():
            return None
        if self.blocked(
            bot_id=bot_id,
            user_id=user_id,
            scope=scope,
            group_id=group_id,
            entry_text=text,
        ):
            return None
        if scope == "private" and (
            user_id == 0 or any(person != user_id for person in participants)
        ):
            return None
        if user_id == 0 and kind not in {"experience", "group"}:
            return None
        old = None
        if supersedes is not None:
            old = self.get(
                supersedes, bot_id, actor_user_id or user_id, scope, group_id
            )
            if old is None or old["status"] != "active" or old["user_id"] != user_id:
                return None
            if actor_user_id is not None:
                if not self.can_edit(old, actor_user_id, scope):
                    return None
            elif old["scope"] != scope or old["group_id"] != group_id:
                return None
        for source in sources:
            current = self.conn.execute(
                "SELECT * FROM raw_messages WHERE id=?", (source["id"],)
            ).fetchone()
            if (
                current is None
                or current["context_hidden"]
                or current["is_recalled"]
                or not current["user_id"]
                or current["bot_id"] != bot_id
                or current["scope"] != scope
                or current["group_id"] != group_id
                or (scope == "private" and current["user_id"] != user_id)
                or self.blocked(
                    bot_id=bot_id,
                    user_id=current["user_id"],
                    scope=scope,
                    group_id=group_id,
                    message_key=current["message_key"] or "",
                    text=current["text"],
                )
            ):
                return None
        if not sources or not any(excerpt in source["text"] for source in sources):
            return None
        digest = fingerprint(text)
        existing = self.conn.execute(
            """SELECT id,participants_json FROM memory_entries WHERE bot_id=? AND user_id=? AND scope=?
          AND group_id IS ? AND fingerprint=? AND kind=? AND status='active'""",
            (bot_id, user_id, scope, group_id, digest, kind),
        ).fetchone()
        if supersedes is not None:
            existing = None  # Each correction has its own evidence and kind, even if its text is unchanged.
        with self.conn:
            if existing:
                memory_id = existing[0]
                self.conn.execute(
                    "UPDATE memory_entries SET updated_at=?,participants_json=? WHERE id=?",
                    (
                        iso(utcnow()),
                        json.dumps(
                            sorted(
                                set(participants)
                                | set(json.loads(existing["participants_json"]))
                            )
                        ),
                        memory_id,
                    ),
                )
            else:
                count = self.conn.execute(
                    "SELECT COUNT(*) FROM memory_entries WHERE bot_id=? AND user_id=? AND scope=? AND group_id IS ? AND status='active'",
                    (bot_id, user_id, scope, group_id),
                ).fetchone()[0]
                if count >= limit and supersedes is None:
                    retired = self.conn.execute(
                        """SELECT id FROM memory_entries WHERE bot_id=? AND user_id=? AND scope=?
                        AND group_id IS ? AND status='active'
                        ORDER BY CASE kind WHEN 'impression' THEN 0 WHEN 'experience' THEN 1 ELSE 2 END,
                        updated_at,id LIMIT 1""",
                        (bot_id, user_id, scope, group_id),
                    ).fetchone()[0]
                    self.conn.execute(
                        "UPDATE memory_entries SET status='archived' WHERE id=?",
                        (retired,),
                    )
                    self.conn.execute(
                        "DELETE FROM memory_entries_fts WHERE rowid=?", (retired,)
                    )
                    self.conn.execute(
                        "DELETE FROM memory_vectors WHERE memory_id=?", (retired,)
                    )
                    self._advance()
                memory_id = self.conn.execute(
                    """INSERT INTO memory_entries(bot_id,user_id,scope,group_id,kind,text,fingerprint,created_at,updated_at,supersedes,participants_json,change_kind)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        bot_id,
                        user_id,
                        scope,
                        group_id,
                        kind,
                        text,
                        digest,
                        iso(utcnow()),
                        iso(utcnow()),
                        supersedes,
                        json.dumps(sorted(set(participants))),
                        change_kind,
                    ),
                ).lastrowid
                self.conn.execute(
                    "INSERT INTO memory_entries_fts(rowid,text) VALUES (?,?)",
                    (memory_id, text),
                )
            for source in sources:
                source_excerpt = (
                    excerpt if excerpt in source["text"] else source["text"][:240]
                )
                self.conn.execute(
                    """INSERT OR IGNORE INTO memory_evidence(memory_id,message_key,raw_id,created_at,user_id,excerpt,body_hash)
                  VALUES (?,?,?,?,?,?,?)""",
                    (
                        memory_id,
                        source["message_key"],
                        source["id"],
                        source["created_at"],
                        source["user_id"],
                        source_excerpt,
                        text_hash(source["text"]),
                    ),
                )
            if supersedes is not None and supersedes != memory_id:
                self.conn.execute(
                    "UPDATE memory_entries SET status='superseded' WHERE id=? AND id!=?",
                    (supersedes, memory_id),
                )
                self.conn.execute(
                    "DELETE FROM memory_entries_fts WHERE rowid=? AND rowid!=?",
                    (supersedes, memory_id),
                )
                self.conn.execute(
                    "DELETE FROM memory_vectors WHERE memory_id=?", (supersedes,)
                )
                # Never re-extract the superseded evidence, including a batch already queued.
                self.conn.execute(
                    """UPDATE memory_jobs SET status='superseded' WHERE raw_id IN
                    (SELECT raw_id FROM memory_evidence WHERE memory_id=?)
                    AND raw_id NOT IN (SELECT raw_id FROM memory_evidence WHERE memory_id=?)""",
                    (supersedes, memory_id),
                )
                self._hide_chat_summaries(old, iso(utcnow()))
                self._advance()
            return memory_id

    def enqueue(self, raw_id: int, immediate: bool = False) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO memory_jobs(raw_id,next_at) VALUES (?,?)",
                (raw_id, iso(utcnow())),
            )

    def next_batch(self, limit: int) -> list[dict]:
        row = self.conn.execute(
            """SELECT r.* FROM memory_jobs j JOIN raw_messages r ON r.id=j.raw_id
          WHERE j.status='pending' AND j.next_at<=? AND r.context_hidden=0 AND r.is_recalled=0 AND r.is_control=0
          ORDER BY j.raw_id LIMIT 1""",
            (iso(utcnow()),),
        ).fetchone()
        if row is None:
            return []
        return [
            dict(item)
            for item in self.conn.execute(
                """SELECT r.* FROM memory_jobs j JOIN raw_messages r ON r.id=j.raw_id
          WHERE j.status='pending' AND j.next_at<=? AND r.chat_key=? AND r.context_hidden=0
          AND r.is_recalled=0 AND r.is_control=0 ORDER BY r.id LIMIT ?""",
                (iso(utcnow()), row["chat_key"], limit),
            )
        ]

    def finish_batch(self, ids: list[int], error: str = "") -> None:
        with self.conn:
            for raw_id in ids:
                self.conn.execute(
                    """UPDATE memory_jobs SET attempts=attempts+1,
                  status=CASE WHEN ?='' THEN 'done' WHEN attempts>=2 THEN 'failed' ELSE 'pending' END,
                  next_at=?,error=? WHERE raw_id=? AND status='pending'""",
                    (error, iso(utcnow() + timedelta(minutes=10)), error, raw_id),
                )

    def _related_sources(self, memory_ids: set[int], blocks: list[dict]) -> list[dict]:
        """Persist dependency message keys before deletion, including expired bot replies."""
        rows = [
            dict(row)
            for row in self.conn.execute(
                "SELECT id,message_key,chat_key,reply_to_id,bot_id,user_id,scope,group_id,conversation_id,text,created_at FROM raw_messages"
            )
        ]
        parts = [
            dict(row)
            for row in self.conn.execute(
                "SELECT t.*,p.raw_id,p.sent_message_id FROM reply_turns t JOIN reply_parts p USING(request_id) WHERE p.sent_message_id IS NOT NULL"
            )
        ]
        for part in parts:
            part["sources"] = set(json.loads(part["sources_json"]))
            part["memories"] = set(json.loads(part["memories_json"]))
            part["episodes"] = set(json.loads(part["episodes_json"]))
            part["key"] = (
                f"{part['bot_id']}:{part['scope']}:{part['target']}:{part['sent_message_id']}"
            )
        evidence = self.conn.execute(
            "SELECT memory_id,raw_id FROM memory_evidence"
        ).fetchall()
        episodes = [dict(row) for row in self.conn.execute("SELECT * FROM episodes")]
        prefixes = {
            f"{b['scope']}:{b['group_id'] if b['scope'] == 'group' else b['user_id']}"
            for b in blocks
        }
        affected_episodes = {
            row["id"]
            for row in episodes
            if any(
                row["conversation_id"] == prefix
                or row["conversation_id"].startswith(prefix + ":")
                for prefix in prefixes
            )
        }
        affected_memories = set(memory_ids)
        affected_raw = {
            row["raw_id"]
            for row in evidence
            if row["memory_id"] in memory_ids and row["raw_id"] is not None
        }
        keys = {b["value"] for b in blocks if b["kind"] == "source"}
        body_keys = {
            (b["bot_id"], b["user_id"], b["scope"], b["group_id"], b["value"])
            for b in blocks
            if b["kind"] == "body"
        }
        for row in rows:
            digest = text_hash(row["text"])
            if any(
                (bot, row["user_id"], row["scope"], row["group_id"], digest)
                in body_keys
                for bot in ("", row["bot_id"])
            ):
                affected_raw.add(row["id"])
        while True:
            before = (
                len(keys),
                len(affected_raw),
                len(affected_memories),
                len(affected_episodes),
            )
            for row in rows:
                if (
                    row["id"] in affected_raw
                    or f"{row['chat_key']}:{row['reply_to_id']}" in keys
                ):
                    affected_raw.add(row["id"])
                    if row["message_key"]:
                        keys.add(row["message_key"])
            affected_memories.update(
                row["memory_id"] for row in evidence if row["raw_id"] in affected_raw
            )
            for part in parts:
                if (
                    part["sources"] & affected_raw
                    or part["memories"] & affected_memories
                    or part["episodes"] & affected_episodes
                ):
                    keys.add(part["key"])
                    if part["raw_id"] is not None:
                        affected_raw.add(part["raw_id"])
            for episode in episodes:
                if any(
                    row["id"] in affected_raw
                    and episode["start_message_id"]
                    <= row["id"]
                    <= episode["end_message_id"]
                    and (
                        row["conversation_id"] == episode["conversation_id"]
                        or row["conversation_id"].startswith(
                            episode["conversation_id"] + ":"
                        )
                    )
                    for row in rows
                ):
                    affected_episodes.add(episode["id"])
            if before == (
                len(keys),
                len(affected_raw),
                len(affected_memories),
                len(affected_episodes),
            ):
                break
        result = {}
        for part in parts:
            if part["key"] in keys:
                result[part["key"]] = {
                    "bot_id": part["bot_id"],
                    "user_id": 0,
                    "scope": part["scope"],
                    "group_id": part["target"] if part["scope"] == "group" else None,
                    "kind": "source",
                    "value": part["key"],
                    "raw_id": part["raw_id"],
                    "created_at": part["created_at"],
                }
        for row in rows:
            if row["message_key"] in keys:
                result[row["message_key"]] = {
                    name: row[name]
                    for name in ("bot_id", "user_id", "scope", "group_id", "created_at")
                }
                result[row["message_key"]].update(
                    kind="source", value=row["message_key"], raw_id=row["id"]
                )
        return list(result.values())

    def forget(self, entry: dict) -> int:
        self.epoch()
        # Include revision ancestors/descendants; retaining superseded evidence would defeat forgetting.
        ids = {entry["id"]}
        entries = self.conn.execute("SELECT * FROM memory_entries").fetchall()
        while True:
            signatures = {
                (
                    row["bot_id"],
                    row["user_id"],
                    row["scope"],
                    row["group_id"],
                    row["fingerprint"],
                )
                for row in entries
                if row["id"] in ids
            }
            related = {
                row["id"]
                for row in entries
                if row["supersedes"] in ids
                or (
                    row["bot_id"],
                    row["user_id"],
                    row["scope"],
                    row["group_id"],
                    row["fingerprint"],
                )
                in signatures
            }
            parents = {
                row[0]
                for ident in ids
                for row in self.conn.execute(
                    "SELECT supersedes FROM memory_entries WHERE id=? AND supersedes IS NOT NULL",
                    (ident,),
                )
            }
            expanded = ids | related | parents
            if expanded == ids:
                break
            ids = expanded
        blocks = []
        for ident in ids:
            row = self.conn.execute(
                "SELECT * FROM memory_entries WHERE id=?", (ident,)
            ).fetchone()
            if row is None:
                continue
            base = {
                "bot_id": row["bot_id"],
                "user_id": row["user_id"],
                "scope": row["scope"],
                "group_id": row["group_id"],
            }
            blocks.append({**base, "kind": "entry", "value": row["fingerprint"]})
            if row["legacy_id"] is not None:
                old = self.conn.execute(
                    "SELECT text_hash FROM memories WHERE id=?", (row["legacy_id"],)
                ).fetchone()
                if old:
                    blocks.append({**base, "kind": "legacy", "value": old[0]})
            for evidence in self.evidence(ident):
                blocks.append(
                    {
                        **base,
                        "user_id": evidence["user_id"],
                        "kind": "source",
                        "value": evidence["message_key"],
                        "raw_id": evidence["raw_id"],
                        "created_at": evidence["created_at"],
                    }
                )
                source = self.conn.execute(
                    "SELECT text FROM raw_messages WHERE id=?", (evidence["raw_id"],)
                ).fetchone()
                if (source and len(source[0].strip()) >= 8) or len(
                    evidence["excerpt"].strip()
                ) >= 8:
                    blocks.append(
                        {
                            **base,
                            "user_id": evidence["user_id"],
                            "kind": "body",
                            "value": evidence["body_hash"],
                        }
                    )
        blocks.extend(self._related_sources(ids, blocks))
        record = {
            "id": uuid4().hex,
            "at": iso(utcnow()),
            "blocks": blocks,
            "raw_sequence": int(
                self.conn.execute(
                    "SELECT value FROM memory_meta WHERE key='raw_sequence'"
                ).fetchone()[0]
            ),
        }
        payload = json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        # Persist suppression before deleting any evidence. Startup replays an interrupted operation.
        self.pending = True
        with self.ledger.open("a", encoding="utf-8") as stream:
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        removed = self._apply(record, text_hash(payload))
        self.pending = False
        return removed

    def replay(self) -> None:
        records = []
        if self.ledger.exists():
            for line in self.ledger.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if not re.fullmatch(r"[0-9a-f]{32}", record["id"]) or not isinstance(
                    record["blocks"], list
                ):
                    raise ValueError("Invalid forgetting ledger")
                records.append((record, text_hash(line)))
        digests = {record["id"]: digest for record, digest in records}
        for row in self.conn.execute("SELECT * FROM forgetting_applied"):
            if digests.get(row["id"]) != row["digest"]:
                raise RuntimeError(
                    "Forgetting ledger is missing or older than the database; restore the current ledger first"
                )
        for record, digest in records:
            if not self.conn.execute(
                "SELECT 1 FROM forgetting_applied WHERE id=?", (record["id"],)
            ).fetchone():
                self._apply(record, digest)

    def _apply(self, record: dict, digest: str) -> int:
        removed = set()
        previously_hidden = {
            row[0]
            for row in self.conn.execute(
                "SELECT id FROM raw_messages WHERE context_hidden=1"
            )
        }
        with self.conn:
            self.conn.execute(
                "UPDATE memory_meta SET value=MAX(CAST(value AS INTEGER),?) WHERE key='raw_sequence'",
                (record["raw_sequence"],),
            )
            for block in record["blocks"]:
                key = text_hash(json.dumps(block, sort_keys=True))
                self.conn.execute(
                    "INSERT OR IGNORE INTO memory_blocks VALUES (?,?,?,?,?,?,?)",
                    (
                        key,
                        block["kind"],
                        block["bot_id"],
                        block["user_id"],
                        block["scope"],
                        block["group_id"],
                        block["value"],
                    ),
                )
                args = (
                    block["user_id"],
                    block["scope"],
                    block["group_id"],
                    block["bot_id"],
                    block["value"],
                )
                if block["kind"] == "entry":
                    removed.update(
                        row[0]
                        for row in self.conn.execute(
                            "SELECT id FROM memory_entries WHERE user_id=? AND scope=? AND group_id IS ? AND bot_id IN ('',?) AND fingerprint=?",
                            args,
                        )
                    )
                elif block["kind"] == "legacy":
                    self.conn.execute(
                        "DELETE FROM memories WHERE subject_user_id=? AND source_scope=? AND group_id IS ? AND text_hash=?",
                        (
                            block["user_id"],
                            block["scope"],
                            block["group_id"],
                            block["value"],
                        ),
                    )
                elif block["kind"] in {"source", "body"}:
                    field = "message_key" if block["kind"] == "source" else "body_hash"
                    removed.update(
                        row[0]
                        for row in self.conn.execute(
                            f"SELECT e.memory_id FROM memory_evidence e JOIN memory_entries m ON m.id=e.memory_id WHERE e.{field}=? AND e.user_id=? AND m.scope=? AND m.group_id IS ? AND m.bot_id IN ('',?)",
                            (block["value"], *args[:4]),
                        )
                    )
                    if block["kind"] == "source":
                        self.conn.execute(
                            "UPDATE raw_messages SET context_hidden=1 WHERE message_key=?",
                            (block["value"],),
                        )
                        self.conn.execute(
                            "UPDATE raw_messages SET context_hidden=1 WHERE chat_key||':'||reply_to_id=?",
                            (block["value"],),
                        )
                        self.conn.execute(
                            """UPDATE raw_messages SET context_hidden=1 WHERE id IN
                          (SELECT p.raw_id FROM reply_parts p JOIN reply_turns t USING(request_id),json_each(t.sources_json) src
                           WHERE src.value=? AND t.bot_id=? AND t.scope=? AND t.target=? AND t.created_at>=?)""",
                            (
                                block["raw_id"],
                                block["bot_id"],
                                block["scope"],
                                block["group_id"]
                                if block["scope"] == "group"
                                else block["user_id"],
                                block["created_at"],
                            ),
                        )
                    else:
                        for row in self.conn.execute(
                            "SELECT id,text FROM raw_messages WHERE user_id=? AND scope=? AND group_id IS ? AND bot_id IN ('',?)",
                            args[:4],
                        ).fetchall():
                            if text_hash(row["text"]) == block["value"]:
                                self.conn.execute(
                                    "UPDATE raw_messages SET context_hidden=1 WHERE id=?",
                                    (row["id"],),
                                )
                self._hide_chat_summaries(block, record["at"])
            for ident in removed:
                self.conn.execute(
                    "UPDATE raw_messages SET context_hidden=1 WHERE id IN (SELECT raw_id FROM memory_evidence WHERE memory_id=?)",
                    (ident,),
                )
                self.conn.execute(
                    "UPDATE raw_messages SET context_hidden=1 WHERE id IN (SELECT p.raw_id FROM reply_parts p JOIN reply_turns t USING(request_id),json_each(t.memories_json) m WHERE m.value=?)",
                    (ident,),
                )
                self.conn.execute(
                    "DELETE FROM memory_evidence WHERE memory_id=?", (ident,)
                )
                self.conn.execute(
                    "DELETE FROM memory_entries_fts WHERE rowid=?", (ident,)
                )
                self.conn.execute(
                    "DELETE FROM memories WHERE id IN (SELECT legacy_id FROM memory_entries WHERE id=?)",
                    (ident,),
                )
                self.conn.execute("DELETE FROM memory_entries WHERE id=?", (ident,))
            self._hide_dependents()
            # A quoted copy can have produced another memory; it has the same invalid provenance.
            dependent = [
                row[0]
                for row in self.conn.execute(
                    "SELECT DISTINCT e.memory_id,r.id FROM memory_evidence e JOIN raw_messages r ON r.id=e.raw_id WHERE r.context_hidden=1"
                )
                if row[1] not in previously_hidden
            ]
            for ident in dependent:
                self.conn.execute(
                    "DELETE FROM memory_evidence WHERE memory_id=?", (ident,)
                )
                self.conn.execute(
                    "DELETE FROM memory_entries_fts WHERE rowid=?", (ident,)
                )
                self.conn.execute(
                    "DELETE FROM memories WHERE id IN (SELECT legacy_id FROM memory_entries WHERE id=?)",
                    (ident,),
                )
                self.conn.execute("DELETE FROM memory_entries WHERE id=?", (ident,))
            removed.update(dependent)
            self.conn.execute(
                "INSERT OR IGNORE INTO forgetting_applied VALUES (?,?)",
                (record["id"], digest),
            )
            self._advance()
        return len(removed)

    def _hide_chat_summaries(self, source, before: str) -> None:
        # Old summaries have only ranges, and the original raw rows may have expired.
        # Invalidate the historical summary cache for this conversation conservatively.
        target = source["group_id"] if source["scope"] == "group" else source["user_id"]
        prefix = f"{source['scope']}:{target}"
        self.conn.execute(
            "UPDATE episodes SET context_hidden=1 WHERE created_at<=? AND (conversation_id=? OR conversation_id LIKE ?||':%')",
            (before, prefix, prefix),
        )

    def _hide_dependents(self) -> None:
        while True:
            changed = self.conn.execute("""UPDATE raw_messages AS r SET context_hidden=1 WHERE context_hidden=0 AND EXISTS
              (SELECT 1 FROM raw_messages s WHERE s.context_hidden=1 AND s.chat_key=r.chat_key AND s.message_id=r.reply_to_id)""").rowcount
            changed += self.conn.execute("""UPDATE raw_messages SET context_hidden=1 WHERE context_hidden=0 AND id IN
              (SELECT p.raw_id FROM reply_parts p JOIN reply_turns t USING(request_id),json_each(t.sources_json) src
               JOIN raw_messages r ON r.id=src.value WHERE r.context_hidden=1)""").rowcount
            changed += self.conn.execute("""UPDATE raw_messages SET context_hidden=1 WHERE context_hidden=0 AND id IN
              (SELECT p.raw_id FROM reply_parts p JOIN reply_turns t USING(request_id),json_each(t.episodes_json) src
               JOIN episodes e ON e.id=src.value WHERE e.context_hidden=1)""").rowcount
            changed += self.conn.execute("""UPDATE raw_messages SET context_hidden=1 WHERE context_hidden=0 AND id IN
              (SELECT p.raw_id FROM reply_parts p JOIN reply_turns t USING(request_id),json_each(t.memories_json) src
               JOIN memory_evidence e ON e.memory_id=src.value JOIN raw_messages r ON r.id=e.raw_id WHERE r.context_hidden=1)""").rowcount
            changed += self.conn.execute("""UPDATE episodes SET context_hidden=1 WHERE context_hidden=0 AND EXISTS (SELECT 1 FROM raw_messages r
              WHERE r.context_hidden=1 AND (r.conversation_id=episodes.conversation_id OR r.conversation_id LIKE episodes.conversation_id||':%') AND r.id BETWEEN start_message_id AND end_message_id)""").rowcount
            if not changed:
                break
        self.conn.execute(
            "UPDATE memory_jobs SET status='blocked',error='' WHERE raw_id IN (SELECT id FROM raw_messages WHERE context_hidden=1 OR is_recalled=1)"
        )
        self.conn.execute(
            "UPDATE memory_entries SET status='source_hidden' WHERE status='active' AND id IN (SELECT e.memory_id FROM memory_evidence e JOIN raw_messages r ON r.id=e.raw_id WHERE r.context_hidden=1)"
        )
        self.conn.execute(
            "DELETE FROM memory_entries_fts WHERE rowid IN (SELECT id FROM memory_entries WHERE status!='active')"
        )

    def invalidate_source(self, message_key: str) -> None:
        with self.conn:
            entries = [
                row[0]
                for row in self.conn.execute(
                    "SELECT memory_id FROM memory_evidence WHERE message_key=?",
                    (message_key,),
                )
            ]
            for ident in entries:
                self.conn.execute(
                    "UPDATE memory_entries SET status='source_recalled' WHERE id=?",
                    (ident,),
                )
                self.conn.execute(
                    "DELETE FROM memory_entries_fts WHERE rowid=?", (ident,)
                )
            self.conn.execute(
                "UPDATE raw_messages SET context_hidden=1 WHERE message_key=?",
                (message_key,),
            )
            self._hide_dependents()
            self._advance()
