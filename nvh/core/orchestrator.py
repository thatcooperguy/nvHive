"""Local LLM Orchestration Engine.

Uses the local Nemotron model as an intelligent brain to:
1. Smart Route — analyze query intent and pick the best cloud LLM
2. Generate Agents — create custom expert personas per question
3. Optimize Prompts — rewrite prompts tuned for target LLM strengths
4. Evaluate Responses — check if the answer is good enough
5. Synthesize — merge multi-provider responses locally (free)
6. Compress Context — summarize long conversations before cloud calls

Tiers based on local model capability:
  OFF   — keyword routing, template agents (nemotron-mini or no local model)
  LIGHT — smart routing + prompt optimization (nemotron-small)
  FULL  — all features (nemotron 70B+)
  AUTO  — detect from model size (default)

All orchestration calls are FREE (local model). The cost savings come from:
  - Better routing = fewer expensive cloud calls
  - Prompt optimization = fewer tokens needed
  - Response evaluation = avoid retries
  - Local synthesis = don't pay cloud to merge responses
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class OrchestrationMode(StrEnum):
    OFF = "off"       # keyword routing, template agents
    LIGHT = "light"   # smart routing + prompt optimization
    FULL = "full"     # everything: routing, agents, eval, synthesis
    AUTO = "auto"     # detect from model size


@dataclass
class OrchestrationConfig:
    mode: OrchestrationMode = OrchestrationMode.AUTO
    min_model_size_for_light: int = 6   # GB VRAM needed for light mode
    min_model_size_for_full: int = 20   # GB VRAM needed for full mode
    routing_timeout_ms: int = 3000      # max time for LLM routing decision
    eval_timeout_ms: int = 5000         # max time for response evaluation


class LocalOrchestrator:
    """Uses local LLM to orchestrate cloud LLM calls."""

    def __init__(self, config: OrchestrationConfig | None = None):
        self.config = config or OrchestrationConfig()
        self._effective_mode: OrchestrationMode | None = None
        self._local_provider = None

    async def initialize(self, registry, gpu_vram_gb: float = 0) -> OrchestrationMode:
        """Initialize and determine effective orchestration mode.

        Args:
            registry: ProviderRegistry to find local Ollama provider
            gpu_vram_gb: Available GPU VRAM (0 = auto-detect)

        Returns:
            The effective orchestration mode
        """
        # Try to get local provider
        if registry.has("ollama"):
            self._local_provider = registry.get("ollama")

        if self.config.mode != OrchestrationMode.AUTO:
            self._effective_mode = self.config.mode
            # But downgrade if no local provider
            if self._effective_mode != OrchestrationMode.OFF and not self._local_provider:
                logger.info("Orchestration downgraded to OFF — no local model available")
                self._effective_mode = OrchestrationMode.OFF
            return self._effective_mode

        # Auto-detect from GPU VRAM
        if not self._local_provider:
            self._effective_mode = OrchestrationMode.OFF
        elif gpu_vram_gb >= self.config.min_model_size_for_full:
            self._effective_mode = OrchestrationMode.FULL
        elif gpu_vram_gb >= self.config.min_model_size_for_light:
            self._effective_mode = OrchestrationMode.LIGHT
        else:
            self._effective_mode = OrchestrationMode.OFF

        logger.info(f"Orchestration mode: {self._effective_mode.value} (VRAM: {gpu_vram_gb:.0f}GB)")
        return self._effective_mode

    @property
    def mode(self) -> OrchestrationMode:
        return self._effective_mode or OrchestrationMode.OFF

    @property
    def is_active(self) -> bool:
        return self.mode != OrchestrationMode.OFF

    # ----- Smart Routing (LIGHT+) -----

    async def smart_route(self, query: str, available_advisors: list[str]) -> dict:
        """Use local LLM to analyze query and recommend the best advisor.

        Returns dict with:
          advisor: recommended advisor name
          reason: why this advisor was chosen
          task_type: detected task type
          complexity: low/medium/high
          needs_web: bool
          needs_code: bool
          is_private: bool (should stay local)
        """
        if self.mode == OrchestrationMode.OFF or not self._local_provider:
            return {}

        from nvh.providers.base import Message

        prompt = f"""Analyze this user query and recommend the best AI advisor.

Available advisors: {', '.join(available_advisors)}

Query: "{query}"

Respond in this exact format (one line each):
advisor: <name from the list above>
reason: <one sentence why>
task_type: <code|writing|research|math|conversation|analysis>
complexity: <low|medium|high>
needs_web: <yes|no>
needs_code: <yes|no>
is_private: <yes|no>"""

        try:
            start = time.monotonic()
            response = await self._local_provider.complete(
                messages=[Message(role="user", content=prompt)],
                temperature=0.1,
                max_tokens=200,
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)

            if elapsed_ms > self.config.routing_timeout_ms:
                logger.warning(f"Smart routing took {elapsed_ms}ms (limit: {self.config.routing_timeout_ms}ms)")

            # Parse the response
            result = {}
            for line in response.content.strip().splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    key = key.strip().lower().replace(" ", "_")
                    value = value.strip().lower()
                    if key in ("advisor", "reason", "task_type", "complexity"):
                        result[key] = value
                    elif key in ("needs_web", "needs_code", "is_private"):
                        result[key] = value in ("yes", "true", "1")

            result["orchestrated"] = True
            result["routing_ms"] = elapsed_ms
            return result

        except Exception as e:
            logger.debug(f"Smart routing failed, falling back to keywords: {e}")
            return {}

    # ----- Dynamic Agent Generation (LIGHT+) -----

    async def generate_custom_agents(self, query: str, num_agents: int = 3) -> list[dict]:
        """Use local LLM to generate custom expert personas tailored to the query.

        Returns list of dicts with: role, expertise, system_prompt
        """
        if self.mode == OrchestrationMode.OFF or not self._local_provider:
            return []

        from nvh.providers.base import Message

        prompt = f"""Create {num_agents} expert personas to analyze this question. Each expert should have a DIFFERENT perspective that creates productive tension.

Question: "{query}"

For each expert, respond in this exact format:

AGENT 1:
role: <job title>
expertise: <2-3 areas of knowledge>
system_prompt: <2 sentences instructing the AI to act as this expert>

AGENT 2:
...

Make the experts diverse — they should disagree constructively."""

        try:
            response = await self._local_provider.complete(
                messages=[Message(role="user", content=prompt)],
                temperature=0.7,
                max_tokens=800,
            )

            agents = []
            current: dict = {}
            for line in response.content.strip().splitlines():
                line = line.strip()
                if line.startswith("AGENT") or (not line and current):
                    if current.get("role"):
                        agents.append(current)
                    current = {}
                elif ":" in line:
                    key, _, value = line.partition(":")
                    key = key.strip().lower().replace(" ", "_")
                    if key in ("role", "expertise", "system_prompt"):
                        current[key] = value.strip()

            if current.get("role"):
                agents.append(current)

            return agents[:num_agents]

        except Exception as e:
            logger.debug(f"Custom agent generation failed: {e}")
            return []

    # ----- Prompt Optimization (LIGHT+) -----

    async def optimize_prompt(self, query: str, target_advisor: str) -> str:
        """Rewrite a prompt to get better results from a specific advisor.

        The local LLM knows each advisor's strengths and rewrites the prompt
        to play to those strengths.
        """
        if self.mode not in (OrchestrationMode.LIGHT, OrchestrationMode.FULL) or not self._local_provider:
            return query

        from nvh.providers.base import Message

        prompt = f"""Rewrite this user query to get the best possible response from {target_advisor}.

Original query: "{query}"

Rules:
- Keep the same intent and meaning
- Make it clearer and more specific
- Add context that helps the AI give a better answer
- Keep it concise — don't add unnecessary padding
- Return ONLY the rewritten query, nothing else"""

        try:
            response = await self._local_provider.complete(
                messages=[Message(role="user", content=prompt)],
                temperature=0.3,
                max_tokens=500,
            )
            optimized = response.content.strip().strip('"').strip("'")

            # Sanity check — if the "optimized" prompt is too different or empty, use original
            if not optimized or len(optimized) < 5:
                return query
            if len(optimized) > len(query) * 5:
                return query  # way too long, probably hallucinated

            return optimized

        except Exception as e:
            logger.debug(f"Prompt optimization failed: {e}")
            return query

    # ----- Response Evaluation (FULL only) -----

    async def evaluate_response(self, query: str, response_text: str, advisor: str) -> dict:
        """Have the local LLM evaluate if a cloud response is good enough.

        Returns dict with:
          quality: 1-10
          is_complete: bool
          issues: list of strings
          should_retry: bool
          retry_with: suggested advisor for retry (if needed)
        """
        if self.mode != OrchestrationMode.FULL or not self._local_provider:
            return {"quality": 7, "is_complete": True, "should_retry": False}

        from nvh.providers.base import Message

        prompt = f"""Evaluate this AI response. Be critical.

User asked: "{query}"

{advisor} responded: "{response_text[:2000]}"

Rate on these criteria (respond in this exact format):
quality: <1-10>
is_complete: <yes|no>
issues: <comma-separated list of problems, or "none">
should_retry: <yes|no>
retry_with: <advisor name or "none">"""

        try:
            eval_response = await self._local_provider.complete(
                messages=[Message(role="user", content=prompt)],
                temperature=0.1,
                max_tokens=200,
            )

            result: dict = {"quality": 7, "is_complete": True, "should_retry": False, "issues": []}
            for line in eval_response.content.strip().splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    key = key.strip().lower()
                    value = value.strip()
                    if key == "quality":
                        try:
                            result["quality"] = int(value)
                        except Exception:
                            pass
                    elif key == "is_complete":
                        result["is_complete"] = value.lower() in ("yes", "true")
                    elif key == "issues":
                        result["issues"] = [i.strip() for i in value.split(",") if i.strip() != "none"]
                    elif key == "should_retry":
                        result["should_retry"] = value.lower() in ("yes", "true")
                    elif key == "retry_with":
                        if value.lower() != "none":
                            result["retry_with"] = value

            return result

        except Exception as e:
            logger.debug(f"Response evaluation failed: {e}")
            return {"quality": 7, "is_complete": True, "should_retry": False}

    # ----- Local Synthesis (FULL only) -----

    async def synthesize_locally(self, query: str, responses: dict[str, str]) -> str:
        """Synthesize multiple advisor responses using the local LLM (free).

        Instead of paying a cloud LLM to synthesize, the local model does it.
        """
        if self.mode != OrchestrationMode.FULL or not self._local_provider:
            return ""

        from nvh.providers.base import Message

        parts = [f"Multiple AI advisors answered this question:\n\nQuestion: \"{query}\"\n"]
        for advisor, text in responses.items():
            parts.append(f"\n--- {advisor} ---\n{text[:1500]}\n")
        parts.append(
            "\nSynthesize the best answer from all responses. "
            "Note agreements and disagreements. Be concise."
        )

        try:
            response = await self._local_provider.complete(
                messages=[Message(role="user", content="".join(parts))],
                temperature=0.3,
                max_tokens=1500,
            )
            return response.content.strip()
        except Exception as e:
            logger.debug(f"Local synthesis failed: {e}")
            return ""

    # ----- Context Compression (FULL only) -----

    async def compress_context(self, messages: list, max_tokens: int = 2000) -> str:
        """Summarize a long conversation to save tokens on cloud API calls.

        The local LLM reads the full history and produces a compressed summary
        that preserves key information.
        """
        if self.mode != OrchestrationMode.FULL or not self._local_provider:
            return ""

        from nvh.providers.base import Message

        history = "\n".join(f"[{m.role}] {m.content[:500]}" for m in messages)

        prompt = f"""Summarize this conversation into a concise context brief.
Preserve: key decisions, important facts, user preferences, and current topic.
Remove: greetings, redundant exchanges, and verbose explanations.

Conversation:
{history}

Write a brief summary (under {max_tokens // 4} words):"""

        try:
            response = await self._local_provider.complete(
                messages=[Message(role="user", content=prompt)],
                temperature=0.2,
                max_tokens=max_tokens,
            )
            return response.content.strip()
        except Exception as e:
            logger.debug(f"Context compression failed: {e}")
            return ""
