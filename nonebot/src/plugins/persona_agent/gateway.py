from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from nonebot.log import logger

from .config import AgentConfig, ModelConfig
from .model import Completion, ModelClient
from .scheduling import ModelSlots
from .storage import Database, utcnow


@dataclass(frozen=True)
class ModelLease:
    client: ModelClient
    config: ModelConfig
    name: str
    purpose: str
    max_tokens: int
    timeout: float
    daily_limit: int


@dataclass(frozen=True)
class ModelFailure:
    reason: str
    http_status: int | None = None


def _model_failure(exc: Exception, phase: str) -> ModelFailure:
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return ModelFailure("queue_timeout" if phase == "queue" else "request_timeout")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        reason = (
            "credential_rejected"
            if status in {401, 403}
            else "upstream_timeout"
            if status in {408, 504}
            else "rate_limited"
            if status == 429
            else "http_error"
        )
        return ModelFailure(reason, status)
    if isinstance(exc, httpx.RequestError):
        return ModelFailure("connection_error")
    if isinstance(exc, (ValueError, KeyError, IndexError, TypeError)):
        return ModelFailure("invalid_response")
    return ModelFailure("model_error")


class ModelGateway:
    def __init__(self, config: AgentConfig, db: Database) -> None:
        self.config = config
        self.db = db
        self.slots = ModelSlots(config.budget.max_concurrent_replies)
        self._clients: dict[str, tuple[ModelClient, int]] = {}
        self._auth_failed: set[str] = set()

    @asynccontextmanager
    async def lease(self, purpose: str):
        task = "media" if purpose == "media_review" else purpose
        binding = None if task == "chat" else getattr(self.config.model_tasks, task)
        name = (binding.model if binding else "") or self.config.active_model
        settings = self.config.models[name].model_copy(deep=True)
        credential = os.getenv(settings.api_key_env, "")
        key = hashlib.sha256(
            (settings.model_dump_json() + credential).encode()
        ).hexdigest()
        if key not in self._clients:
            client = ModelClient(settings)
            # An in-flight request keeps the key selected at lease time.
            client.api_key = credential
            self._clients[key] = (client, 0)
        client, users = self._clients[key]
        self._clients[key] = (client, users + 1)
        lease = ModelLease(
            client,
            settings,
            name,
            purpose,
            binding.max_tokens if binding else self.config.budget.max_reply_tokens,
            binding.timeout_seconds
            if binding
            else self.config.scheduler.chat_timeout_seconds,
            binding.daily_token_budget if binding else 0,
        )
        try:
            yield lease
        finally:
            client, users = self._clients[key]
            self._clients[key] = (client, users - 1)
            names = {self.config.active_model} | {
                getattr(self.config.model_tasks, task).model or self.config.active_model
                for task in ("participation", "media", "memory")
            }
            active = {
                hashlib.sha256(
                    (
                        self.config.models[name].model_dump_json()
                        + os.getenv(self.config.models[name].api_key_env, "")
                    ).encode()
                ).hexdigest()
                for name in names
            }
            for cache_key, (cached, references) in list(self._clients.items()):
                if not references and cache_key not in active:
                    self._clients.pop(cache_key)
                    await cached.close()

    async def complete(
        self,
        messages: list[dict],
        user_id: int,
        *,
        request_id: str,
        purpose: str = "chat",
        priority: int = 0,
        lease: ModelLease | None = None,
    ) -> Completion | ModelFailure:
        if lease is None:
            async with self.lease(purpose) as selected:
                return await self.complete(
                    messages,
                    user_id,
                    request_id=request_id,
                    purpose=purpose,
                    priority=priority,
                    lease=selected,
                )
        clock = asyncio.get_running_loop().time
        started = clock()
        request_start = started
        waited = 0.0
        attempt = 0
        phase = "queue"
        runtime = self.db.store.runtime
        auth_key = hashlib.sha256(
            (lease.config.model_dump_json() + lease.client.api_key).encode()
        ).hexdigest()
        if auth_key in self._auth_failed:
            logger.warning(
                "Model skipped: request_id={} purpose={} reason=credential_rejected",
                request_id,
                purpose,
            )
            return ModelFailure("credential_rejected")
        call_id = None
        try:
            async with asyncio.timeout(lease.timeout):
                async with self.slots.slot(priority):
                    waited = clock() - started
                    phase = "budget"
                    limit = self.config.daily_budget_for(user_id)
                    if (
                        limit > 0
                        and await self.db.run(self.db.store.tokens_today, user_id)
                        >= limit
                    ):
                        logger.info(
                            "Model skipped: request_id={} reason=user_budget",
                            request_id,
                        )
                        return ModelFailure("user_budget")
                    if (
                        lease.daily_limit > 0
                        and await self.db.run(runtime.task_tokens_today, purpose)
                        >= lease.daily_limit
                    ):
                        logger.info(
                            "Model skipped: request_id={} reason=task_budget",
                            request_id,
                        )
                        return ModelFailure("task_budget")
                    for attempt in (1, 2):
                        phase = "request"
                        request_start = clock()
                        call_id = await self.db.run(
                            runtime.model_start,
                            request_id,
                            purpose,
                            user_id,
                            lease.config.model,
                        )
                        logger.info(
                            "Model request: request_id={} purpose={} attempt={} model={} queue_seconds={:.2f} max_tokens={} deadline_seconds={}",
                            request_id,
                            purpose,
                            attempt,
                            lease.config.model,
                            waited,
                            lease.max_tokens,
                            lease.timeout,
                        )
                        try:
                            result = await lease.client.complete(
                                messages,
                                lease.max_tokens,
                                timeout=min(
                                    lease.config.timeout_seconds,
                                    max(0.01, lease.timeout - (clock() - started)),
                                ),
                            )
                        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                            status = (
                                exc.response.status_code
                                if isinstance(exc, httpx.HTTPStatusError)
                                else None
                            )
                            if status in {401, 403}:
                                self._auth_failed.add(auth_key)
                            failure = _model_failure(exc, phase)
                            await self.db.run(
                                runtime.model_finish,
                                call_id,
                                "failed",
                                elapsed=clock() - request_start,
                                estimated=True,
                                error=f"{failure.reason}:{type(exc).__name__}:{status}",
                            )
                            call_id = None
                            # A timeout may already have consumed provider tokens; never blindly replay it.
                            retry = isinstance(exc, httpx.ConnectError) or status in {
                                429,
                                502,
                                503,
                                504,
                            }
                            if attempt == 1 and retry:
                                logger.warning(
                                    "Model retry: request_id={} purpose={} attempt={} reason={} http_status={} request_seconds={:.2f}",
                                    request_id,
                                    purpose,
                                    attempt,
                                    failure.reason,
                                    status,
                                    clock() - request_start,
                                )
                                phase = "retry_wait"
                                await asyncio.sleep(1)
                                continue
                            raise
                        elapsed = clock() - request_start
                        await self.db.run(
                            runtime.model_finish,
                            call_id,
                            "completed",
                            tokens=result.total_tokens,
                            estimated=result.estimated,
                            elapsed=elapsed,
                        )
                        call_id = None
                        await self.db.run(
                            self.db.store.add_tokens,
                            user_id,
                            result.total_tokens,
                            utcnow(),
                        )
                        logger.info(
                            "Model reply: request_id={} purpose={} model={} tokens={} "
                            "reasoning_tokens={} estimated={} elapsed={:.2f}s finish_reason={}",
                            request_id,
                            purpose,
                            result.model,
                            result.total_tokens,
                            result.reasoning_tokens,
                            result.estimated,
                            elapsed,
                            result.finish_reason,
                        )
                        return result
        except asyncio.CancelledError:
            if call_id is not None:
                await self.db.run(
                    runtime.model_finish,
                    call_id,
                    "interrupted",
                    elapsed=clock() - request_start,
                    estimated=True,
                )
            raise
        except Exception as exc:
            failure = _model_failure(exc, phase)
            elapsed = clock() - started
            if phase == "queue":
                waited = elapsed
            if call_id is not None:
                await self.db.run(
                    runtime.model_finish,
                    call_id,
                    "failed",
                    elapsed=clock() - request_start,
                    estimated=True,
                    error=f"{failure.reason}:{type(exc).__name__}",
                )
            logger.warning(
                "Model failed: request_id={} purpose={} reason={} phase={} attempt={} error={} http_status={} "
                "queue_seconds={:.2f} request_seconds={:.2f} elapsed={:.2f}s usage_unknown={}",
                request_id,
                purpose,
                failure.reason,
                phase,
                attempt,
                type(exc).__name__,
                failure.http_status,
                waited,
                clock() - request_start if attempt else 0.0,
                elapsed,
                attempt > 0,
            )
            return failure

    async def close(self) -> None:
        for client, _ in list(self._clients.values()):
            await client.close()
        self._clients.clear()
