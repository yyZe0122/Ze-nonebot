from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict

import httpx

from .config import ModelConfig
from .vision import ImageContent, materialize_images


class ImageLoader:
    """Bounded, short-lived byte cache; ordinary images never go to disk."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            follow_redirects=True, headers={"User-Agent": "persona-agent"}
        )
        self._slots = asyncio.Semaphore(4)
        self._pending: dict[str, asyncio.Task] = {}
        self._cache: OrderedDict[str, tuple[float, list[ImageContent], int]] = (
            OrderedDict()
        )
        self._bytes = 0

    async def load(
        self, source: str, config: ModelConfig, *, strict: bool = False
    ) -> list[ImageContent]:
        if (
            not source
            or not config.supports_images
            or config.max_images_per_message == 0
        ):
            return []
        key = hashlib.sha256(source.encode()).hexdigest()
        pending_key = key + (":strict" if strict else "")
        now = asyncio.get_running_loop().time()
        for cached, (expires, _, size) in list(self._cache.items()):
            if expires < now:
                self._cache.pop(cached)
                self._bytes -= size
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key][1]
        if pending_key not in self._pending:

            async def fetch():
                try:
                    async with (
                        self._slots,
                        asyncio.timeout(config.image_timeout_seconds + 2),
                    ):
                        result = await materialize_images(
                            (source,),
                            config=config,
                            client=self._client,
                            strict=strict,
                        )
                    if result:
                        size = sum(len(item.url) for item in result)
                        self._cache[key] = (
                            asyncio.get_running_loop().time() + 30,
                            result,
                            size,
                        )
                        self._bytes += size
                        while self._bytes > 24 * 1024 * 1024:
                            _, (_, _, removed) = self._cache.popitem(last=False)
                            self._bytes -= removed
                    return result
                except ValueError:
                    if strict:
                        raise
                    return []
                except TimeoutError:
                    if strict:
                        raise ValueError("image_download_transient")
                    return []
                finally:
                    self._pending.pop(pending_key, None)

            self._pending[pending_key] = asyncio.create_task(fetch())
        return await asyncio.shield(self._pending[pending_key])

    async def close(self) -> None:
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._cache.clear()
        await self._client.aclose()
