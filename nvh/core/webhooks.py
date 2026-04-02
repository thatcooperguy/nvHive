"""Webhook notification system for Council events."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class WebhookEvent:
    """Event types that can trigger webhooks."""
    QUERY_COMPLETE = "query.complete"
    COUNCIL_COMPLETE = "council.complete"
    BUDGET_THRESHOLD = "budget.threshold_reached"
    BUDGET_EXCEEDED = "budget.exceeded"
    PROVIDER_DOWN = "provider.circuit_open"
    PROVIDER_RECOVERED = "provider.circuit_closed"
    PROVIDER_ERROR = "provider.error"


@dataclass
class WebhookConfig:
    url: str
    events: list[str]           # which events to send
    secret: str = ""            # HMAC-SHA256 signing secret
    enabled: bool = True
    retry_count: int = 3
    timeout_seconds: int = 10


@dataclass
class WebhookPayload:
    event: str
    timestamp: float
    data: dict[str, Any]


class WebhookManager:
    """Manages webhook registrations and dispatches events."""

    def __init__(self) -> None:
        self._hooks: list[WebhookConfig] = []
        self._queue: asyncio.Queue[tuple[WebhookConfig, WebhookPayload]] = asyncio.Queue()
        self._running = False
        self._worker_task: asyncio.Task[None] | None = None

    def register(self, config: WebhookConfig) -> None:
        """Register a webhook endpoint."""
        self._hooks.append(config)
        logger.debug("Registered webhook: %s (events: %s)", config.url, config.events)

    def load_from_config(self, webhooks_config: list[dict[str, Any]]) -> None:
        """Load webhooks from the YAML config."""
        for item in webhooks_config:
            cfg = WebhookConfig(
                url=item.get("url", ""),
                events=item.get("events", []),
                secret=item.get("secret", ""),
                enabled=item.get("enabled", True),
                retry_count=item.get("retry_count", 3),
                timeout_seconds=item.get("timeout_seconds", 10),
            )
            if cfg.url:
                self.register(cfg)

    async def emit(self, event: str, data: dict[str, Any]) -> None:
        """Emit an event to all matching webhooks (non-blocking)."""
        payload = WebhookPayload(
            event=event,
            timestamp=time.time(),
            data=data,
        )
        for hook in self._hooks:
            if not hook.enabled:
                continue
            if hook.events and event not in hook.events:
                continue
            await self._queue.put((hook, payload))

    async def _dispatch(self, hook: WebhookConfig, payload: WebhookPayload) -> bool:
        """Send a webhook with retries and HMAC signing."""
        body = json.dumps({
            "event": payload.event,
            "timestamp": payload.timestamp,
            "data": payload.data,
        })

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "Council-Webhooks/1.0",
            "X-Council-Event": payload.event,
        }

        if hook.secret:
            sig = _sign_payload(body, hook.secret)
            headers["X-Council-Signature"] = f"sha256={sig}"

        for attempt in range(hook.retry_count):
            try:
                async with httpx.AsyncClient(timeout=hook.timeout_seconds) as client:
                    response = await client.post(hook.url, content=body, headers=headers)
                    if response.status_code < 300:
                        logger.debug(
                            "Webhook delivered: %s event=%s status=%d",
                            hook.url, payload.event, response.status_code,
                        )
                        return True
                    logger.warning(
                        "Webhook non-2xx: %s event=%s status=%d",
                        hook.url, payload.event, response.status_code,
                    )
            except Exception as exc:
                logger.warning(
                    "Webhook attempt %d/%d failed: %s event=%s error=%s",
                    attempt + 1, hook.retry_count, hook.url, payload.event, exc,
                )

            if attempt < hook.retry_count - 1:
                backoff = 2 ** attempt
                await asyncio.sleep(backoff)

        logger.error(
            "Webhook failed after %d attempts: %s event=%s",
            hook.retry_count, hook.url, payload.event,
        )
        return False

    async def _worker(self) -> None:
        """Background worker that drains the webhook queue."""
        while self._running:
            try:
                hook, payload = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                asyncio.create_task(self._dispatch(hook, payload))
                self._queue.task_done()
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Webhook worker error: %s", exc)

    async def start(self) -> None:
        """Start the background webhook dispatcher."""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker())
        logger.debug("Webhook dispatcher started.")

    async def stop(self) -> None:
        """Stop the dispatcher and drain the queue."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

        # Drain remaining items
        while not self._queue.empty():
            try:
                hook, payload = self._queue.get_nowait()
                await self._dispatch(hook, payload)
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
            except Exception as exc:
                logger.error("Error draining webhook queue: %s", exc)

        logger.debug("Webhook dispatcher stopped.")

    def list_hooks(self) -> list[dict[str, Any]]:
        """Return configured webhooks with secrets masked."""
        return [
            {
                "url": h.url,
                "events": h.events,
                "secret": "***" if h.secret else "",
                "enabled": h.enabled,
                "retry_count": h.retry_count,
                "timeout_seconds": h.timeout_seconds,
            }
            for h in self._hooks
        ]


def _sign_payload(payload: str, secret: str) -> str:
    """Generate HMAC-SHA256 signature."""
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Pre-formatted payload helpers
# ---------------------------------------------------------------------------

def format_budget_alert(
    daily_spend: float,
    daily_limit: float,
    monthly_spend: float,
    monthly_limit: float,
    threshold_pct: float,
) -> dict[str, Any]:
    """Format a budget threshold alert payload."""
    return {
        "daily_spend_usd": round(daily_spend, 6),
        "daily_limit_usd": round(daily_limit, 2),
        "monthly_spend_usd": round(monthly_spend, 6),
        "monthly_limit_usd": round(monthly_limit, 2),
        "threshold_pct": round(threshold_pct * 100, 1),
        "daily_pct_used": round(daily_spend / daily_limit * 100, 1) if daily_limit > 0 else 0,
        "monthly_pct_used": round(monthly_spend / monthly_limit * 100, 1) if monthly_limit > 0 else 0,
    }


def format_provider_alert(
    provider: str,
    event_type: str,
    error: str | None = None,
    latency_ms: int | None = None,
) -> dict[str, Any]:
    """Format a provider health alert payload."""
    data: dict[str, Any] = {
        "provider": provider,
        "event": event_type,
    }
    if error is not None:
        data["error"] = error
    if latency_ms is not None:
        data["latency_ms"] = latency_ms
    return data


def format_query_complete(
    provider: str,
    model: str,
    tokens: int,
    cost: float,
    latency_ms: int,
    mode: str,
) -> dict[str, Any]:
    """Format a query completion payload."""
    return {
        "provider": provider,
        "model": model,
        "total_tokens": tokens,
        "cost_usd": round(cost, 6),
        "latency_ms": latency_ms,
        "mode": mode,
    }
