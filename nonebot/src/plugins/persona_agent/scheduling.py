from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from itertools import count


class ModelSlots:
    """FIFO within each priority, with at most one background request running."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.active = 0
        self.background = 0
        self._sequence = count()
        self._waiting: list[tuple[int, int]] = []
        self._changed = asyncio.Condition()

    @property
    def idle(self) -> bool:
        return self.active == 0 and not self._waiting

    @asynccontextmanager
    async def slot(self, priority: int):
        token = (priority, next(self._sequence))
        async with self._changed:
            self._waiting.append(token)
            try:

                def ready() -> bool:
                    eligible = [
                        item
                        for item in self._waiting
                        if item[0] < 2 or self.background == 0
                    ]
                    return (
                        self.active < self.limit
                        and bool(eligible)
                        and token == min(eligible)
                    )

                await self._changed.wait_for(ready)
            except BaseException:
                self._waiting.remove(token)
                self._changed.notify_all()
                raise
            self._waiting.remove(token)
            self.active += 1
            self.background += int(priority >= 2)
            self._changed.notify_all()
        try:
            yield
        finally:
            async with self._changed:
                self.active -= 1
                self.background -= int(priority >= 2)
                self._changed.notify_all()
