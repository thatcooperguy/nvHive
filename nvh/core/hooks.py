"""NVHive Hooks — event-driven extensibility.

Users register hooks in their config (~/.hive/config.yaml) or HIVE.md.
Hooks run shell commands or Python callables before/after events.

Config example:
  hooks:
    - event: pre_query
      command: "echo 'Query starting: {{prompt}}'"
    - event: post_query
      command: "notify-send 'NVHive' 'Query complete: {{cost}}'"
    - event: on_error
      command: "curl -X POST https://my-webhook.com/error -d '{{error}}'"
    - event: budget_warning
      command: "echo 'Budget alert: {{spend}} / {{limit}}'"

Events:
  pre_query       — before any query (prompt, advisor, mode)
  post_query      — after query completes (response, cost, latency)
  pre_convene     — before council session
  post_convene    — after council with all member responses
  on_error        — when a query fails
  on_fallback     — when primary advisor fails and fallback kicks in
  budget_warning  — when spending hits alert threshold
  session_start   — REPL session begins
  session_end     — REPL session ends (with savings summary)
  model_loaded    — local model loaded/pulled
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class Hook:
    event: str
    command: str = ""           # shell command (supports {{var}} templates)
    callback: Callable | None = None  # Python callable alternative
    enabled: bool = True
    timeout_seconds: int = 10

@dataclass
class HookContext:
    """Variables available to hook templates."""
    event: str
    prompt: str = ""
    response: str = ""
    advisor: str = ""
    model: str = ""
    cost: str = "0"
    latency_ms: int = 0
    tokens: int = 0
    mode: str = ""
    error: str = ""
    spend: str = "0"
    limit: str = "0"

class HookManager:
    def __init__(self):
        self._hooks: list[Hook] = []

    def register(self, hook: Hook) -> None:
        self._hooks.append(hook)

    def load_from_config(self, hooks_config: list[dict]) -> None:
        """Load hooks from YAML config."""
        for h in hooks_config:
            self.register(Hook(
                event=h.get("event", ""),
                command=h.get("command", ""),
                enabled=h.get("enabled", True),
                timeout_seconds=h.get("timeout", 10),
            ))

    async def emit(self, event: str, context: HookContext) -> None:
        """Fire all hooks matching this event."""
        for hook in self._hooks:
            if hook.event == event and hook.enabled:
                try:
                    if hook.callback:
                        result = hook.callback(context)
                        if asyncio.iscoroutine(result):
                            await result
                    elif hook.command:
                        cmd = self._render(hook.command, context)
                        await self._run_command(cmd, hook.timeout_seconds)
                except Exception as e:
                    logger.warning(f"Hook '{hook.event}' failed: {e}")

    def _render(self, template: str, ctx: HookContext) -> str:
        """Replace {{var}} placeholders with context values."""
        result = template
        for field_name in ("event", "prompt", "response", "advisor", "model",
                           "cost", "latency_ms", "tokens", "mode", "error",
                           "spend", "limit"):
            val = str(getattr(ctx, field_name, ""))
            result = result.replace(f"{{{{{field_name}}}}}", val)
        return result

    async def _run_command(self, cmd: str, timeout: int) -> None:
        """Run a shell command with timeout."""
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            logger.warning(f"Hook command timed out: {cmd[:50]}...")

    def list_hooks(self) -> list[dict]:
        return [{"event": h.event, "command": h.command, "enabled": h.enabled} for h in self._hooks]
