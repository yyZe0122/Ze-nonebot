from __future__ import annotations

import json
from datetime import timedelta

from .storage import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_calls (
 id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, purpose TEXT NOT NULL, user_id INTEGER NOT NULL,
 model TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT NOT NULL,
 tokens INTEGER NOT NULL DEFAULT 0, estimated INTEGER NOT NULL DEFAULT 0,
 elapsed REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS model_call_time ON model_calls(started_at, purpose);
CREATE TABLE IF NOT EXISTS reply_turns (
 request_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, bot_id TEXT NOT NULL,
 scope TEXT NOT NULL, target INTEGER NOT NULL, source_message_id INTEGER,
 outcome TEXT NOT NULL, sources_json TEXT NOT NULL DEFAULT '[]', memories_json TEXT NOT NULL DEFAULT '[]',
 episodes_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS reply_parts (
 request_id TEXT NOT NULL, part INTEGER NOT NULL, status TEXT NOT NULL,
 sent_message_id INTEGER, raw_id INTEGER, updated_at TEXT NOT NULL,
 PRIMARY KEY(request_id, part)
);
"""


class RuntimeStore:
    def __init__(self, conn) -> None:
        self.conn = conn
        with conn:
            conn.execute(
                "UPDATE model_calls SET outcome='interrupted', finished_at=? WHERE outcome='running'",
                (iso(utcnow()),),
            )
            conn.execute(
                "UPDATE reply_parts SET status='unknown' WHERE status='sending'"
            )
            conn.execute(
                "UPDATE reply_turns SET outcome='interrupted' WHERE outcome='running'"
            )

    def get(self, key: str, default=None):
        row = self.conn.execute(
            "SELECT value FROM runtime_state WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO runtime_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def model_start(
        self, request_id: str, purpose: str, user_id: int, model: str
    ) -> int:
        with self.conn:
            return self.conn.execute(
                "INSERT INTO model_calls(request_id,purpose,user_id,model,started_at,outcome) VALUES (?,?,?,?,?,'running')",
                (request_id, purpose, user_id, model, iso(utcnow())),
            ).lastrowid

    def reserve_image_search(self, *, lens=False, daily_limit=20, lens_limit=200) -> str:
        """Reserve before network I/O; failed calls also consume this local allowance."""
        now = utcnow()
        key = "image-source-usage"
        state = self.get(key, {})
        if lens:
            cutoff = (now - timedelta(days=31)).timestamp()
            calls = [at for at in state.get("lens", []) if at > cutoff]
            if len(calls) >= lens_limit:
                return "quota"
            state["lens"] = [*calls, now.timestamp()]
        else:
            day = now.date().isoformat()
            count = state.get("count", 0) if state.get("day") == day else 0
            if count >= daily_limit:
                return "daily_limit"
            if state.get("next_at", 0) > now.timestamp():
                return "cooldown"
            state.update(day=day, count=count + 1, next_at=now.timestamp() + 10)
        self.put(key, state)
        return ""

    def image_search_remaining(self, daily_limit: int) -> int:
        state = self.get("image-source-usage", {})
        used = (
            state.get("count", 0)
            if state.get("day") == utcnow().date().isoformat()
            else 0
        )
        return max(0, daily_limit - used)

    def model_finish(
        self,
        call_id: int,
        outcome: str,
        *,
        tokens=0,
        estimated=False,
        elapsed=0.0,
        error="",
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE model_calls SET finished_at=?,outcome=?,tokens=?,estimated=?,elapsed=?,error=? WHERE id=?",
                (
                    iso(utcnow()),
                    outcome,
                    tokens,
                    int(estimated),
                    elapsed,
                    error,
                    call_id,
                ),
            )

    def task_tokens_today(self, purpose: str) -> int:
        start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        if purpose in {"media", "media_review"}:
            return self.conn.execute(
                "SELECT COALESCE(SUM(tokens),0) FROM model_calls WHERE purpose IN ('media','media_review') AND started_at>=?",
                (iso(start),),
            ).fetchone()[0]
        return self.conn.execute(
            "SELECT COALESCE(SUM(tokens),0) FROM model_calls WHERE purpose=? AND started_at>=?",
            (purpose, iso(start)),
        ).fetchone()[0]

    def task_calls_today(self, purpose: str) -> int:
        start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.conn.execute(
            "SELECT COUNT(*) FROM model_calls WHERE purpose=? AND started_at>=?",
            (purpose, iso(start)),
        ).fetchone()[0]

    def turn_start(
        self,
        request_id: str,
        event,
        sources: list[int],
        memories: list[int],
        episodes: list[int],
        *,
        once: bool = False,
    ) -> bool:
        with self.conn:
            statement = "INSERT OR IGNORE" if once else "INSERT"
            cursor = self.conn.execute(
                statement + " INTO reply_turns VALUES (?,?,?,?,?,?, 'running',?,?,?)",
                (
                    request_id,
                    iso(utcnow()),
                    event.bot_id,
                    event.scope,
                    event.route[2],
                    event.message_id,
                    json.dumps(sources),
                    json.dumps(memories),
                    json.dumps(episodes),
                ),
            )
            return cursor.rowcount == 1

    def turn_finish(self, request_id: str, outcome: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE reply_turns SET outcome=? WHERE request_id=?",
                (outcome, request_id),
            )

    def part(
        self, request_id: str, index: int, status: str, sent_id=None, raw_id=None
    ) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO reply_parts VALUES (?,?,?,?,?,?) ON CONFLICT(request_id,part)
              DO UPDATE SET status=excluded.status,sent_message_id=excluded.sent_message_id,
              raw_id=excluded.raw_id,updated_at=excluded.updated_at""",
                (request_id, index, status, sent_id, raw_id, iso(utcnow())),
            )

    def purge(self, days: int) -> None:
        cutoff = iso(utcnow() - timedelta(days=days))
        with self.conn:
            self.conn.execute("DELETE FROM model_calls WHERE started_at<?", (cutoff,))
            self.conn.execute(
                "DELETE FROM reply_turns WHERE created_at<? AND NOT EXISTS (SELECT 1 FROM reply_parts p JOIN raw_messages r ON r.id=p.raw_id WHERE p.request_id=reply_turns.request_id)",
                (cutoff,),
            )
            self.conn.execute(
                "DELETE FROM reply_parts WHERE request_id NOT IN (SELECT request_id FROM reply_turns)"
            )
