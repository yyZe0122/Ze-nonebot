from __future__ import annotations

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Database:
    """Run complete repository operations on one connection-owning worker."""

    def __init__(self) -> None:
        self._worker = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="persona-db"
        )

    @classmethod
    async def open(cls, path: Path) -> "Database":
        from .store import Store

        db = cls()
        try:
            db.store = await db.run(Store, path)
        except BaseException:
            db._worker.shutdown(wait=True)
            raise
        return db

    async def run(self, operation: Callable[..., T], *args, **kwargs) -> T:
        work = asyncio.get_running_loop().run_in_executor(
            self._worker, partial(operation, *args, **kwargs)
        )
        # Caller cancellation never interrupts a database transaction halfway through.
        return await asyncio.shield(work)

    async def close(self) -> None:
        await self.run(self.store.close)
        self._worker.shutdown(wait=True)
