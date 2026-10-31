from __future__ import annotations

import asyncio
import math
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from .config import EmbeddingConfig


class LocalEmbedding:
    """One CPU model, owned by a worker thread in the NoneBot process."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self.key = "fastembed:v1:" + config.model
        self.ready = False
        self._worker = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="memory-embedding"
        )

    async def _run(self, function, *args):
        future = asyncio.get_running_loop().run_in_executor(
            self._worker, partial(function, *args)
        )
        return await asyncio.shield(future)

    def _load(self) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(
            model_name=self.config.model,
            cache_dir=str(self.config.cache_directory),
            threads=self.config.threads,
            providers=["CPUExecutionProvider"],
        )

    async def load(self) -> None:
        await self._run(self._load)
        self.ready = True

    def _embed(self, texts: list[str], query: bool) -> list[list[float]]:
        generate = self._model.query_embed if query else self._model.passage_embed
        vectors = []
        for vector in generate(texts, batch_size=self.config.batch_size):
            values = [float(value) for value in vector]
            norm = math.sqrt(sum(value * value for value in values))
            if not values or not math.isfinite(norm) or norm <= 0:
                raise ValueError("Embedding model returned an invalid vector")
            vectors.append([value / norm for value in values])
        if len(vectors) != len(texts):
            raise ValueError("Embedding model returned the wrong batch size")
        return vectors

    async def encode(
        self, texts: list[str], *, query: bool = False
    ) -> list[list[float]]:
        return await self._run(self._embed, texts, query)

    async def close(self) -> None:
        self.ready = False
        await asyncio.to_thread(self._worker.shutdown, wait=True, cancel_futures=True)
