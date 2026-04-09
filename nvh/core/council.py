"""Council orchestrator: parallel dispatch, consensus, and synthesis."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

logger = logging.getLogger(__name__)

from nvh.config.settings import CouncilConfig
from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    Message,
    ProviderError,
    Usage,
)
from nvh.providers.registry import ProviderRegistry


@dataclass
class CouncilMember:
    provider: str
    model: str
    weight: float
    persona: str = ""        # role name, e.g. "Software Architect"
    system_prompt: str = ""  # persona-specific system prompt


@dataclass
class CouncilResponse:
    """Result of a council session."""
    member_responses: dict[str, CompletionResponse]
    failed_members: dict[str, str]  # provider -> error message
    synthesis: CompletionResponse | None
    strategy: str
    total_cost_usd: Decimal
    total_latency_ms: int
    quorum_met: bool
    members: list[CouncilMember]
    agents_used: list[str] = field(default_factory=list)  # persona roles assigned
    confidence_score: float | None = field(default=None)  # 0.0-1.0 agreement level
    agreement_summary: str | None = field(default=None)  # human-readable summary


class CouncilOrchestrator:
    """Orchestrates parallel multi-LLM queries and consensus."""

    def __init__(
        self,
        config: CouncilConfig,
        registry: ProviderRegistry,
        rate_manager=None,  # ProviderRateManager | None — optional for health-aware selection
    ):
        self.config = config
        self.registry = registry
        self.rate_manager = rate_manager

    # Minimum health score for a provider to be considered usable.
    # 0.0 = circuit breaker open (hard fail); 0.3 = half-open (probing); >0.3 = closed.
    _MIN_HEALTH = 0.2

    def _is_healthy(self, provider: str) -> bool:
        """Return True if provider is healthy enough to try.

        Falls back to True when no rate_manager is attached (legacy callers /
        tests) so behavior is unchanged unless the engine wires one in.
        """
        if self.rate_manager is None:
            return True
        try:
            return self.rate_manager.get_health_score(provider) >= self._MIN_HEALTH
        except Exception:
            return True

    def _healthy_enabled(self) -> list[str]:
        """Enabled providers filtered by health score, in registry order.

        If the filter would leave us with nothing, fall back to the full
        enabled list — better to try a degraded provider than to abort.
        """
        enabled = self.registry.list_enabled()
        healthy = [p for p in enabled if self._is_healthy(p)]
        return healthy or enabled

    def _resolve_members(
        self,
        members_override: list[str] | None = None,
        weights_override: dict[str, float] | None = None,
    ) -> list[CouncilMember]:
        """Determine which providers participate and their weights."""
        weights = weights_override or self.config.council.default_weights

        if members_override:
            provider_names = members_override
        else:
            provider_names = [
                name for name in weights
                if self.registry.has(name)
            ]

        if not provider_names:
            provider_names = self._healthy_enabled()
        else:
            # Pinned members (explicit config or override) are honored
            # as-is, but warn loudly about any that are currently
            # unhealthy so it's not a mystery why the session fails.
            import logging as _logging
            _log = _logging.getLogger(__name__)
            unhealthy = [
                name for name in provider_names
                if self.registry.has(name) and not self._is_healthy(name)
            ]
            if unhealthy:
                _log.warning(
                    "Council includes unhealthy advisors: %s — "
                    "they will be tried anyway since the caller pinned "
                    "them explicitly. Expect member_failed events.",
                    ", ".join(unhealthy),
                )

        # Assign weights, defaulting to equal
        total_specified = sum(weights.get(p, 0) for p in provider_names)
        members = []
        for name in provider_names:
            if not self.registry.has(name):
                continue
            w = weights.get(name, 0)
            if w == 0 and total_specified == 0:
                w = 1.0 / len(provider_names)
            members.append(CouncilMember(
                provider=name,
                model=self._get_model_for_provider(name),
                weight=w,
            ))

        # Normalize weights
        total = sum(m.weight for m in members)
        if total > 0 and abs(total - 1.0) > 0.01:
            for m in members:
                m.weight = m.weight / total

        return members

    def _get_model_for_provider(self, provider_name: str) -> str:
        pconfig = self.config.providers.get(provider_name)
        return pconfig.default_model if pconfig else ""

    async def _analyze_agreement(
        self,
        query: str,
        member_responses: dict[str, CompletionResponse],
        *,
        use_llm: bool = True,
    ) -> tuple[float | None, str | None]:
        """Analyze agreement across member responses.

        Returns (confidence_score, agreement_summary).
        Tries an LLM call first (if use_llm=True); falls back to a simple heuristic.
        """
        if len(member_responses) < 2:
            return None, None

        # --- Try LLM-based analysis ---
        if not use_llm:
            return self._heuristic_agreement(member_responses)

        synth_provider_name = self.config.council.synthesis_provider
        if not synth_provider_name or not self.registry.has(synth_provider_name):
            available = self.registry.list_enabled()
            synth_provider_name = available[0] if available else None

        if synth_provider_name:
            try:
                provider = self.registry.get(synth_provider_name)
                model = self._get_model_for_provider(synth_provider_name)
                n = len(member_responses)
                parts = [
                    f"Given these {n} responses to the question '{query[:200]}', "
                    "rate their agreement on a scale of 0-10 and summarize: "
                    "do they agree on the core answer? Where do they diverge?\n\n"
                    "Respond in EXACTLY this format:\n"
                    "SCORE: <number 0-10>\n"
                    "SUMMARY: <one sentence>\n\n"
                ]
                for label, resp in member_responses.items():
                    # Truncate each response to keep prompt short
                    content = resp.content[:300]
                    parts.append(f"--- {label} ---\n{content}\n\n")

                analysis = await asyncio.wait_for(
                    provider.complete(
                        messages=[Message(role="user", content="".join(parts))],
                        model=model or None,
                        temperature=0.1,
                        max_tokens=200,
                    ),
                    timeout=15,
                )

                # Parse score and summary from response
                text = analysis.content
                score_match = re.search(r"SCORE:\s*(\d+(?:\.\d+)?)", text)
                summary_match = re.search(r"SUMMARY:\s*(.+)", text)

                if score_match:
                    raw_score = float(score_match.group(1))
                    confidence = min(max(raw_score / 10.0, 0.0), 1.0)
                    summary = summary_match.group(1).strip() if summary_match else None
                    return confidence, summary

            except Exception:
                pass  # Fall through to heuristic

        # --- Heuristic fallback ---
        return self._heuristic_agreement(member_responses)

    @staticmethod
    def _heuristic_agreement(
        member_responses: dict[str, CompletionResponse],
    ) -> tuple[float, str]:
        """Simple heuristic: keyword overlap + length similarity."""
        contents = [r.content.lower() for r in member_responses.values()]
        n = len(contents)

        # Extract keyword sets (words 4+ chars, skip common words)
        stop = {
            "that", "this", "with", "from", "have", "will", "been",
            "they", "their", "would", "could", "should", "about",
            "which", "there", "these", "those", "what", "when",
            "were", "also", "into", "more", "than", "some", "very",
            "just", "your",
        }
        keyword_sets = []
        for c in contents:
            words = set(re.findall(r"\b[a-z]{4,}\b", c)) - stop
            keyword_sets.append(words)

        # Pairwise Jaccard similarity
        overlaps = []
        for i in range(n):
            for j in range(i + 1, n):
                union = keyword_sets[i] | keyword_sets[j]
                if union:
                    overlaps.append(len(keyword_sets[i] & keyword_sets[j]) / len(union))
                else:
                    overlaps.append(0.0)

        avg_overlap = sum(overlaps) / len(overlaps) if overlaps else 0.0

        # Length similarity (coefficient of variation)
        lengths = [len(c) for c in contents]
        mean_len = sum(lengths) / n
        if mean_len > 0:
            variance = sum((ln - mean_len) ** 2 for ln in lengths) / n
            cv = variance**0.5 / mean_len
            len_similarity = max(0.0, 1.0 - cv)
        else:
            len_similarity = 0.0

        # Combine: 70% keyword overlap, 30% length similarity
        confidence = 0.7 * avg_overlap + 0.3 * len_similarity
        confidence = min(max(confidence, 0.0), 1.0)

        # Generate summary
        agree_count = sum(1 for o in overlaps if o > 0.3) if overlaps else 0
        total_pairs = len(overlaps)
        if confidence >= 0.7:
            summary = f"Strong consensus ({n}/{n} largely agree on core approach)"
        elif confidence >= 0.4:
            summary = f"Partial agreement ({agree_count}/{total_pairs} pairs overlap significantly)"
        else:
            summary = f"Split decision — responses diverge significantly across {n} members"

        return confidence, summary

    async def run_council(
        self,
        query: str,
        members_override: list[str] | None = None,
        weights_override: dict[str, float] | None = None,
        strategy: str | None = None,
        system_prompt: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        timeout: int | None = None,
        messages: list[Message] | None = None,
        synthesize: bool = True,
        auto_agents: bool = False,
        agent_preset: str | None = None,
        num_agents: int | None = None,
    ) -> CouncilResponse:
        """Run a council session: dispatch to all members, then synthesize.

        Args:
            auto_agents: Auto-generate expert personas based on the query content.
                Each council member gets a unique persona system prompt.
            agent_preset: Use a named preset (e.g. "executive", "engineering",
                "security_review", "code_review", "product", "data", "full_board").
            num_agents: Number of agent personas to generate (default: match member count).
        """
        members = self._resolve_members(members_override, weights_override)
        strategy = strategy or self.config.council.strategy
        timeout = timeout or self.config.council.timeout
        quorum = self.config.council.quorum

        if not members:
            raise ValueError(
                "No council members available — council mode requires at least 2 advisors.\n"
                "Run: nvh setup  (to configure advisors)\n"
                "Or add providers to your config: nvh config set council.members groq,openai,anthropic"
            )

        # Auto-generate agent personas
        agents_used: list[str] = []
        if auto_agents or agent_preset:
            from nvh.core.agents import generate_agents, get_preset_agents

            n = num_agents or len(members)

            if agent_preset:
                personas = get_preset_agents(agent_preset, query)
            else:
                personas = generate_agents(query, num_agents=n)

            # Assign personas to members (round-robin if more members than personas)
            for i, member in enumerate(members):
                if i < len(personas):
                    persona = personas[i]
                    member.persona = persona.role
                    member.system_prompt = persona.system_prompt
                    member.weight += persona.weight_boost
                    agents_used.append(persona.role)

            # Re-normalize weights after boost
            total = sum(m.weight for m in members)
            if total > 0 and abs(total - 1.0) > 0.01:
                for m in members:
                    m.weight = m.weight / total

        # Build message list
        if messages:
            msgs = list(messages)
        else:
            msgs = [Message(role="user", content=query)]

        # Dispatch members with rate-limit-aware staggering.
        # Members using different providers fire in parallel.
        # Members sharing a provider are staggered by 2s to
        # avoid burning the rate limit on free tiers.
        start = time.monotonic()
        member_responses: dict[str, CompletionResponse] = {}
        failed_members: dict[str, str] = {}

        # Group by provider to detect shared-provider members
        _seen_providers: dict[str, int] = {}
        _member_delays: dict[str, float] = {}
        for member in members:
            count = _seen_providers.get(member.provider, 0)
            _member_delays[member.provider + str(count)] = (
                count * 2.0
            )
            _seen_providers[member.provider] = count + 1

        tasks = {}
        _provider_idx: dict[str, int] = {}
        for member in members:
            # Use persona system prompt if assigned, otherwise fall back to user's system prompt
            member_system = member.system_prompt or system_prompt
            base_label = (
                f"{member.provider}:{member.persona}"
                if member.persona else member.provider
            )
            # Ensure unique labels when same provider appears twice
            label = base_label
            suffix = 2
            while label in tasks:
                label = f"{base_label}#{suffix}"
                suffix += 1
            # Stagger members sharing the same provider
            idx = _provider_idx.get(member.provider, 0)
            delay = idx * 2.0  # 2s between same-provider members
            _provider_idx[member.provider] = idx + 1

            if delay > 0:
                tasks[label] = asyncio.create_task(
                    self._call_member_delayed(
                        member, msgs, member_system,
                        temperature, max_tokens, timeout,
                        delay,
                    ),
                )
            else:
                tasks[label] = asyncio.create_task(
                    self._call_member(
                        member, msgs, member_system,
                        temperature, max_tokens, timeout,
                    ),
                )

        # Wait for all tasks (with overall timeout)
        done, pending = await asyncio.wait(
            tasks.values(),
            timeout=timeout + 5,  # extra buffer
        )

        # Cancel and await any still-pending tasks to prevent resource leaks
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        # Collect results
        for label, task in tasks.items():
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc:
                    failed_members[label] = str(exc)
                else:
                    resp = task.result()
                    # Annotate response with persona info
                    if ":" in label:
                        resp.metadata["persona"] = label.split(":", 1)[1]
                    member_responses[label] = resp
            else:
                failed_members[label] = "Timed out"

        total_elapsed = int((time.monotonic() - start) * 1000)

        # Check quorum
        quorum_met = len(member_responses) >= quorum

        # Calculate total cost of member responses
        member_cost = sum(r.cost_usd for r in member_responses.values())

        # Analyze agreement across member responses
        confidence_score: float | None = None
        agreement_summary: str | None = None
        if quorum_met and len(member_responses) > 1:
            confidence_score, agreement_summary = await self._analyze_agreement(
                query=query,
                member_responses=member_responses,
                use_llm=synthesize,
            )

        # Synthesize if we have quorum
        synthesis = None
        if quorum_met and synthesize and len(member_responses) > 1:
            try:
                synthesis = await self._synthesize(
                    query=query,
                    member_responses=member_responses,
                    members=members,
                    strategy=strategy,
                    system_prompt=system_prompt,
                    agents_used=agents_used,
                )
            except Exception as e:
                # If synthesis fails, we still return member responses
                failed_members["_synthesis"] = str(e)

        total_cost = member_cost + (synthesis.cost_usd if synthesis else Decimal("0"))

        return CouncilResponse(
            member_responses=member_responses,
            failed_members=failed_members,
            synthesis=synthesis,
            strategy=strategy,
            total_cost_usd=total_cost,
            total_latency_ms=total_elapsed,
            quorum_met=quorum_met,
            members=members,
            agents_used=agents_used,
            confidence_score=confidence_score,
            agreement_summary=agreement_summary,
        )

    async def run_council_streaming(
        self,
        query: str,
        on_event: Callable[[dict], Awaitable[None]],
        members_override: list[str] | None = None,
        weights_override: dict[str, float] | None = None,
        strategy: str | None = None,
        system_prompt: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        timeout: int | None = None,
        synthesize: bool = True,
        auto_agents: bool = False,
        agent_preset: str | None = None,
        num_agents: int | None = None,
        budget_check: Callable[[], Awaitable[None]] | None = None,
    ) -> CouncilResponse:
        """Run a council session, streaming per-member tokens via on_event callback.

        Events emitted (in order):
          council_start       — session started, member/agent info
          member_start        — a member has begun generating
          member_chunk        — a streaming token delta from a member
          member_complete     — a member finished successfully
          member_failed       — a member failed
          synthesis_start     — synthesis has begun
          synthesis_chunk     — a streaming token from the synthesis step
          synthesis_complete  — synthesis finished
          council_complete    — entire session done
        """
        members = self._resolve_members(members_override, weights_override)
        strategy = strategy or self.config.council.strategy
        timeout = timeout or self.config.council.timeout
        quorum = self.config.council.quorum

        if not members:
            raise ValueError(
                "No council members available — council mode requires at least 2 advisors.\n"
                "Run: nvh setup  (to configure advisors)\n"
                "Or add providers to your config: nvh config set council.members groq,openai,anthropic"
            )

        # Auto-generate agent personas
        agents_used: list[str] = []
        if auto_agents or agent_preset:
            from nvh.core.agents import generate_agents, get_preset_agents

            n = num_agents or len(members)
            if agent_preset:
                personas = get_preset_agents(agent_preset, query)
            else:
                personas = generate_agents(query, num_agents=n)

            for i, member in enumerate(members):
                if i < len(personas):
                    persona = personas[i]
                    member.persona = persona.role
                    member.system_prompt = persona.system_prompt
                    member.weight += persona.weight_boost
                    agents_used.append(persona.role)

            total = sum(m.weight for m in members)
            if total > 0 and abs(total - 1.0) > 0.01:
                for m in members:
                    m.weight = m.weight / total

        import uuid
        session_id = str(uuid.uuid4())

        # Emit council_start
        await on_event({
            "type": "council_start",
            "session_id": session_id,
            "members": [
                {
                    "provider": m.provider,
                    "model": m.model,
                    "weight": m.weight,
                    "persona": m.persona,
                }
                for m in members
            ],
            "agents": agents_used,
        })

        msgs = [Message(role="user", content=query)]

        member_responses: dict[str, CompletionResponse] = {}
        failed_members: dict[str, str] = {}

        start = time.monotonic()

        async def _stream_member(member: CouncilMember) -> None:
            member_system = member.system_prompt or system_prompt
            label = f"{member.provider}:{member.persona}" if member.persona else member.provider

            await on_event({
                "type": "member_start",
                "member": label,
                "provider": member.provider,
                "persona": member.persona or "",
            })

            provider = self.registry.get(member.provider)
            accumulated = ""
            last_chunk = None
            member_start_time = time.monotonic()

            try:
                async for chunk in provider.stream(
                    messages=msgs,
                    model=member.model or None,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    system_prompt=member_system,
                ):
                    last_chunk = chunk
                    if chunk.delta:
                        accumulated += chunk.delta
                        await on_event({
                            "type": "member_chunk",
                            "member": label,
                            "delta": chunk.delta,
                            "accumulated": accumulated,
                        })
                    if chunk.is_final:
                        break

                latency_ms = int((time.monotonic() - member_start_time) * 1000)
                tokens = last_chunk.usage.total_tokens if last_chunk and last_chunk.usage else 0
                cost_usd = str(last_chunk.cost_usd) if last_chunk and last_chunk.cost_usd is not None else "0"

                # Build a CompletionResponse from the streamed data
                resp = CompletionResponse(
                    content=accumulated,
                    model=last_chunk.model if last_chunk else member.model,
                    provider=member.provider,
                    usage=last_chunk.usage if last_chunk and last_chunk.usage else Usage(),
                    cost_usd=last_chunk.cost_usd if last_chunk and last_chunk.cost_usd is not None else Decimal("0"),
                    latency_ms=latency_ms,
                    finish_reason=last_chunk.finish_reason if last_chunk and last_chunk.finish_reason else FinishReason.STOP,
                )
                if member.persona:
                    resp.metadata["persona"] = member.persona

                member_responses[label] = resp

                await on_event({
                    "type": "member_complete",
                    "member": label,
                    "content": accumulated,
                    "tokens": tokens,
                    "cost": cost_usd,
                    "latency_ms": latency_ms,
                })

            except Exception as exc:
                failed_members[label] = str(exc)
                await on_event({
                    "type": "member_failed",
                    "member": label,
                    "error": str(exc),
                })

        # Run all member streams concurrently with timeout
        council_timeout = timeout or self.config.council.timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *[_stream_member(m) for m in members],
                    return_exceptions=True,
                ),
                timeout=council_timeout + 5,
            )
        except TimeoutError:
            for label in [m.label for m in members]:
                if label not in member_responses and label not in failed_members:
                    failed_members[label] = "timed out"

        total_elapsed = int((time.monotonic() - start) * 1000)
        quorum_met = len(member_responses) >= quorum
        member_cost = sum(r.cost_usd for r in member_responses.values())

        # Analyze agreement across member responses
        confidence_score, agreement_summary = await self._analyze_agreement(
            query=query,
            member_responses=member_responses,
        )

        # Synthesis
        synthesis: CompletionResponse | None = None
        if quorum_met and synthesize and len(member_responses) > 1:
            # Re-check the budget before synthesis. Council members
            # already consumed budget in parallel; if they collectively
            # blew through the limit, we don't want synthesis to add
            # another LLM call on top and silently exceed the cap.
            # The caller (API layer) passes a budget_check coroutine
            # so the council orchestrator doesn't need to know about
            # engine internals.
            if budget_check is not None:
                try:
                    await budget_check()
                except Exception as budget_exc:
                    err_msg = f"Budget exceeded before synthesis: {budget_exc}"
                    failed_members["_synthesis"] = err_msg
                    await on_event({
                        "type": "error",
                        "error": err_msg,
                        "phase": "synthesis_budget",
                    })
                    synth_candidates = []  # skip the synthesis loop below
                else:
                    synth_candidates = self._synthesis_candidates(member_responses)
            else:
                synth_candidates = self._synthesis_candidates(member_responses)

            if synth_candidates:
                await on_event({"type": "synthesis_start"})
                # Build synthesis prompt (reuse _weighted_synthesis logic)
                label_weights: dict[str, float] = {}
                for m in members:
                    lbl = f"{m.provider}:{m.persona}" if m.persona else m.provider
                    label_weights[lbl] = m.weight

                has_personas = any(m.persona for m in members)
                if has_personas:
                    synth_parts = [
                        "You are a synthesis engine. A council of expert advisors have analyzed the same "
                        "question, each from their domain of expertise. Your job is to produce a single, "
                        "comprehensive response that integrates the best insights from each expert, "
                        "weighted by their assigned importance.\n\n"
                        f"**Original Query:** {query}\n\n"
                        "**Expert Responses:**\n"
                    ]
                else:
                    synth_parts = [
                        "You are a synthesis engine. Multiple AI models have responded to the same query. "
                        "Your job is to produce a single, high-quality response that combines the best "
                        "elements of each response, weighted by their assigned importance weights.\n\n"
                        f"**Original Query:** {query}\n\n"
                        "**Responses:**\n"
                    ]

                for lbl, resp in member_responses.items():
                    weight = label_weights.get(lbl, 0)
                    persona = resp.metadata.get("persona", "")
                    display = persona if persona else lbl
                    synth_parts.append(
                        f"\n--- {display} (weight: {weight:.0%}) ---\n{resp.content}\n"
                    )

                if has_personas:
                    synth_parts.append(
                        "\n**Instructions:**\n"
                        "1. Identify points of agreement across experts.\n"
                        "2. Highlight unique insights that only certain experts raised.\n"
                        "3. For disagreements, note the tension and explain the trade-off.\n"
                        "4. Produce a unified recommendation that balances all perspectives.\n"
                        "5. Reference experts by their role.\n"
                        "6. End with a brief 'Key Takeaways' section.\n"
                    )
                else:
                    synth_parts.append(
                        "\n**Instructions:**\n"
                        "1. Identify points of agreement across responses.\n"
                        "2. For disagreements, favor the response from higher-weighted models.\n"
                        "3. Produce a single coherent answer that represents the weighted consensus.\n"
                        "4. Note any significant disagreements between models.\n"
                        "5. Do NOT mention the individual models by name in your response.\n"
                    )

                synthesis_prompt = "".join(synth_parts)

                # Rotate through candidates until one succeeds. Each attempt has
                # its own timeout so a stalled provider can't hang the UI.
                # Bound attempts to avoid burning through every provider — 3 is
                # enough to escape transient rate limits without punishing the user.
                synth_attempts = min(3, len(synth_candidates))
                synth_per_attempt_timeout = 60.0
                synth_error: Exception | None = None
                synth_tried: list[str] = []

                for synth_attempt in range(synth_attempts):
                    synth_provider_name = synth_candidates[synth_attempt]
                    synth_tried.append(synth_provider_name)
                    synth_provider = self.registry.get(synth_provider_name)
                    synth_model = self._get_model_for_provider(synth_provider_name)

                    # Reset per-attempt state — if we partially streamed before
                    # failing, the frontend will see a fresh stream below.
                    synth_accumulated = ""
                    synth_last_chunk = None

                    async def _run_synth_stream():
                        nonlocal synth_accumulated, synth_last_chunk
                        async for chunk in synth_provider.stream(
                            messages=[Message(role="user", content=synthesis_prompt)],
                            model=synth_model or None,
                            temperature=0.3,
                            max_tokens=4096,
                        ):
                            synth_last_chunk = chunk
                            if chunk.delta:
                                synth_accumulated += chunk.delta
                                await on_event({
                                    "type": "synthesis_chunk",
                                    "delta": chunk.delta,
                                    "accumulated": synth_accumulated,
                                    "provider": synth_provider_name,
                                })
                            if chunk.is_final:
                                break

                    try:
                        await asyncio.wait_for(
                            _run_synth_stream(),
                            timeout=synth_per_attempt_timeout,
                        )

                        synth_tokens = synth_last_chunk.usage.total_tokens if synth_last_chunk and synth_last_chunk.usage else 0
                        synth_cost = str(synth_last_chunk.cost_usd) if synth_last_chunk and synth_last_chunk.cost_usd is not None else "0"

                        synthesis = CompletionResponse(
                            content=synth_accumulated,
                            model=synth_last_chunk.model if synth_last_chunk else synth_model,
                            provider=synth_provider_name,
                            usage=synth_last_chunk.usage if synth_last_chunk and synth_last_chunk.usage else Usage(),
                            cost_usd=synth_last_chunk.cost_usd if synth_last_chunk and synth_last_chunk.cost_usd is not None else Decimal("0"),
                            latency_ms=0,
                            finish_reason=FinishReason.STOP,
                        )
                        synthesis.metadata["strategy"] = strategy
                        synthesis.metadata["members"] = list(member_responses.keys())
                        synthesis.metadata["synthesis_provider"] = synth_provider_name

                        if self.rate_manager is not None:
                            try:
                                self.rate_manager.record_success(synth_provider_name)
                            except Exception as rec_exc:
                                logger.debug("rate_manager.record_success failed: %s", rec_exc)

                        await on_event({
                            "type": "synthesis_complete",
                            "content": synth_accumulated,
                            "tokens": synth_tokens,
                            "cost": synth_cost,
                            "provider": synth_provider_name,
                        })
                        synth_error = None
                        break

                    except TimeoutError as exc:
                        synth_error = exc
                        if self.rate_manager is not None:
                            try:
                                self.rate_manager.record_failure(synth_provider_name, exc)
                            except Exception as rec_exc:
                                logger.debug("rate_manager.record_failure failed: %s", rec_exc)
                        await on_event({
                            "type": "synthesis_retry",
                            "provider": synth_provider_name,
                            "error": f"timed out after {synth_per_attempt_timeout:.0f}s",
                            "attempt": synth_attempt + 1,
                            "max_attempts": synth_attempts,
                        })
                        continue
                    except Exception as exc:
                        synth_error = exc
                        if self.rate_manager is not None:
                            try:
                                self.rate_manager.record_failure(synth_provider_name, exc)
                            except Exception as rec_exc:
                                logger.debug("rate_manager.record_failure failed: %s", rec_exc)
                        await on_event({
                            "type": "synthesis_retry",
                            "provider": synth_provider_name,
                            "error": str(exc),
                            "attempt": synth_attempt + 1,
                            "max_attempts": synth_attempts,
                        })
                        continue

                # If every attempt failed, surface a terminal error so the
                # UI stops spinning. Previously this only wrote to
                # failed_members and the WebSocket never heard about it.
                if synthesis is None and synth_error is not None:
                    err_msg = (
                        f"Synthesis failed after {len(synth_tried)} attempt(s) "
                        f"across providers: {', '.join(synth_tried)}. "
                        f"Last error: {synth_error}"
                    )
                    failed_members["_synthesis"] = err_msg
                    await on_event({
                        "type": "error",
                        "error": err_msg,
                        "phase": "synthesis",
                        "tried": synth_tried,
                    })
            else:
                err_msg = (
                    "No synthesis provider available — configure a synthesis advisor.\n"
                    "Run: nvh config set council.synthesis_provider groq"
                )
                failed_members["_synthesis"] = err_msg
                await on_event({
                    "type": "error",
                    "error": err_msg,
                    "phase": "synthesis",
                })

        total_cost = member_cost + (synthesis.cost_usd if synthesis else Decimal("0"))

        await on_event({
            "type": "council_complete",
            "total_cost": str(total_cost),
            "total_latency_ms": total_elapsed,
            "quorum_met": quorum_met,
            "confidence_score": confidence_score,
            "agreement_summary": agreement_summary,
        })

        return CouncilResponse(
            member_responses=member_responses,
            failed_members=failed_members,
            synthesis=synthesis,
            strategy=strategy,
            total_cost_usd=total_cost,
            total_latency_ms=total_elapsed,
            quorum_met=quorum_met,
            members=members,
            agents_used=agents_used,
            confidence_score=confidence_score,
            agreement_summary=agreement_summary,
        )

    async def _call_member_delayed(
        self,
        member: CouncilMember,
        messages: list[Message],
        system_prompt: str | None,
        temperature: float,
        max_tokens: int,
        timeout: int,
        delay: float,
    ) -> CompletionResponse:
        """Call a council member after a delay (rate limit stagger)."""
        await asyncio.sleep(delay)
        return await self._call_member(
            member, messages, system_prompt,
            temperature, max_tokens, timeout,
        )

    async def _call_member(
        self,
        member: CouncilMember,
        messages: list[Message],
        system_prompt: str | None,
        temperature: float,
        max_tokens: int,
        timeout: int,
    ) -> CompletionResponse:
        """Call a single council member with timeout."""
        provider = self.registry.get(member.provider)
        try:
            response = await asyncio.wait_for(
                provider.complete(
                    messages=messages,
                    model=member.model or None,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    system_prompt=system_prompt,
                ),
                timeout=timeout,
            )
            return response
        except TimeoutError:
            raise ProviderError(
                f"Provider {member.provider} timed out after {timeout}s",
                provider=member.provider,
            )

    async def _synthesize(
        self,
        query: str,
        member_responses: dict[str, CompletionResponse],
        members: list[CouncilMember],
        strategy: str,
        system_prompt: str | None = None,
        agents_used: list[str] | None = None,
    ) -> CompletionResponse:
        """Synthesize member responses using the configured strategy."""
        if strategy == "majority_vote":
            return self._majority_vote(member_responses, members)
        elif strategy == "best_of":
            return await self._best_of(query, member_responses, members)
        else:
            # Default: weighted_consensus
            return await self._weighted_synthesis(query, member_responses, members)

    def _majority_vote(
        self,
        responses: dict[str, CompletionResponse],
        members: list[CouncilMember],
    ) -> CompletionResponse:
        """Simple majority vote — return the most common response (by content similarity)."""
        if len(responses) == 1:
            return list(responses.values())[0]

        # For MVP: return the response from the highest-weighted member
        weights = {m.provider: m.weight for m in members}
        best_provider = max(responses.keys(), key=lambda p: weights.get(p, 0))
        best = responses[best_provider]

        # Add attribution
        content = f"**Selected response** (from {best_provider}, highest weight):\n\n{best.content}"
        return CompletionResponse(
            content=content,
            model=best.model,
            provider=best.provider,
            usage=best.usage,
            cost_usd=Decimal("0"),  # No extra cost for vote
            latency_ms=0,
            finish_reason=FinishReason.STOP,
            metadata={"strategy": "majority_vote", "selected_from": best_provider},
        )

    async def _weighted_synthesis(
        self,
        query: str,
        responses: dict[str, CompletionResponse],
        members: list[CouncilMember],
    ) -> CompletionResponse:
        """Use a synthesis LLM to combine responses with weights."""
        # Build label -> weight map
        label_weights: dict[str, float] = {}
        for m in members:
            label = f"{m.provider}:{m.persona}" if m.persona else m.provider
            label_weights[label] = m.weight

        has_personas = any(m.persona for m in members)

        # Build synthesis prompt
        if has_personas:
            synthesis_parts = [
                "You are a synthesis engine. A council of expert advisors have analyzed the same "
                "question, each from their domain of expertise. Your job is to produce a single, "
                "comprehensive response that integrates the best insights from each expert, "
                "weighted by their assigned importance.\n\n"
                f"**Original Query:** {query}\n\n"
                "**Expert Responses:**\n"
            ]
        else:
            synthesis_parts = [
                "You are a synthesis engine. Multiple AI models have responded to the same query. "
                "Your job is to produce a single, high-quality response that combines the best elements "
                "of each response, weighted by their assigned importance weights.\n\n"
                f"**Original Query:** {query}\n\n"
                "**Responses:**\n"
            ]

        for label, response in responses.items():
            weight = label_weights.get(label, 0)
            persona = response.metadata.get("persona", "")
            display = persona if persona else label
            synthesis_parts.append(
                f"\n--- {display} (weight: {weight:.0%}) ---\n"
                f"{response.content}\n"
            )

        if has_personas:
            synthesis_parts.append(
                "\n**Instructions:**\n"
                "1. Identify points of agreement across experts.\n"
                "2. Highlight unique insights that only certain experts raised.\n"
                "3. For disagreements, note the tension and explain the trade-off.\n"
                "4. Produce a unified recommendation that balances all perspectives.\n"
                "5. Reference experts by their role (e.g., 'The Security Engineer raised...').\n"
                "6. End with a brief 'Key Takeaways' section.\n"
            )
        else:
            synthesis_parts.append(
                "\n**Instructions:**\n"
                "1. Identify points of agreement across responses.\n"
                "2. For disagreements, favor the response from higher-weighted models.\n"
                "3. Produce a single coherent answer that represents the weighted consensus.\n"
                "4. Note any significant disagreements between models.\n"
                "5. Do NOT mention the individual models by name in your response.\n"
            )

        synthesis_prompt = "".join(synthesis_parts)

        # Use the synthesis provider — with retry rotation
        # to handle rate limits on free tiers
        response = await self._synthesis_with_retry(
            synthesis_prompt, responses,
        )

        response.metadata["strategy"] = "weighted_consensus"
        response.metadata["members"] = list(responses.keys())
        return response

    async def _best_of(
        self,
        query: str,
        responses: dict[str, CompletionResponse],
        members: list[CouncilMember],
    ) -> CompletionResponse:
        """Have a judge LLM select the best response."""
        parts = [
            "You are a response quality judge. Multiple AI models answered the same query. "
            "Select the BEST response and return it verbatim. At the end, briefly explain your choice.\n\n"
            f"**Query:** {query}\n\n"
        ]

        for i, (provider, response) in enumerate(responses.items(), 1):
            parts.append(f"\n--- Response {i} ({provider}) ---\n{response.content}\n")

        parts.append(
            "\nReturn the best response verbatim, then add a brief note explaining your selection."
        )

        judge_prompt = "".join(parts)

        response = await self._synthesis_with_retry(
            judge_prompt, responses,
        )
        response.metadata["strategy"] = "best_of"
        return response

    def _synthesis_candidates(
        self,
        member_responses: dict[str, CompletionResponse],
    ) -> list[str]:
        """Build a prioritized list of synthesis providers to try.

        Order:
          1. Configured `council.synthesis_provider` (if healthy)
          2. Healthy providers NOT used as council members (fresh rate limits)
          3. Healthy member providers as last resort
          4. Unhealthy providers at the tail, only if nothing healthy is left

        Unhealthy = circuit breaker open or health score below `_MIN_HEALTH`.
        Rate manager is optional; without it, every provider is treated as
        healthy (legacy behavior).
        """
        # Strip persona suffix to get provider names from labels
        member_providers: set[str] = set()
        for label in member_responses:
            if ":" in label:
                member_providers.add(label.split(":")[0])
            elif "#" in label:
                member_providers.add(label.split("#")[0])
            else:
                member_providers.add(label)

        enabled = self.registry.list_enabled()
        healthy: list[str] = []
        unhealthy: list[str] = []

        # 1. Configured synthesis provider first (if registered)
        configured = self.config.council.synthesis_provider
        if configured and self.registry.has(configured):
            if self._is_healthy(configured):
                healthy.append(configured)
            else:
                unhealthy.append(configured)

        # 2. Non-member providers
        for p in enabled:
            if p in healthy or p in unhealthy:
                continue
            if p in member_providers:
                continue
            (healthy if self._is_healthy(p) else unhealthy).append(p)

        # 3. Member providers as last resort
        for p in enabled:
            if p in healthy or p in unhealthy:
                continue
            (healthy if self._is_healthy(p) else unhealthy).append(p)

        # Healthy first, then unhealthy as a desperate fallback
        return healthy + unhealthy

    def _pick_synthesis_provider(
        self,
        member_responses: dict[str, CompletionResponse],
    ) -> str | None:
        """Pick the best synthesis provider. Kept for callers that want a single name."""
        candidates = self._synthesis_candidates(member_responses)
        return candidates[0] if candidates else None

    async def _synthesis_with_retry(
        self,
        prompt: str,
        member_responses: dict[str, CompletionResponse],
        max_retries: int = 3,
    ) -> CompletionResponse:
        """Run synthesis with provider rotation and backoff.

        Tries the configured synthesis provider first, then rotates
        through other available providers if rate-limited. Includes
        brief backoff between retries to let rate limits recover.
        """
        import logging as _logging
        _log = _logging.getLogger(__name__)

        # Build prioritized, health-aware provider list (shared with streaming path)
        candidates = self._synthesis_candidates(member_responses)

        if not candidates:
            raise ValueError(
                "No provider available for synthesis.\n"
                "Run: nvh setup  (to configure advisors)"
            )

        last_error: Exception | None = None

        # Try each candidate in order, bounded by max_retries. Don't revisit
        # providers we already saw fail — that just wastes the user's time.
        attempts = min(max_retries, len(candidates))
        for attempt in range(attempts):
            provider_name = candidates[attempt]
            provider = self.registry.get(provider_name)
            model = self._get_model_for_provider(provider_name)

            try:
                # Brief backoff on retry (not first attempt)
                if attempt > 0:
                    backoff = min(5.0 * (attempt + 1), 15.0)
                    _log.info(
                        "Synthesis retry %d/%d with %s"
                        " (backoff %.0fs)",
                        attempt + 1, attempts,
                        provider_name, backoff,
                    )
                    await asyncio.sleep(backoff)

                response = await asyncio.wait_for(
                    provider.complete(
                        messages=[
                            Message(
                                role="user",
                                content=prompt,
                            ),
                        ],
                        model=model or None,
                        temperature=0.3,
                        max_tokens=4096,
                    ),
                    timeout=60,
                )
                response.metadata["synthesis_provider"] = (
                    provider_name
                )
                if self.rate_manager is not None:
                    try:
                        self.rate_manager.record_success(provider_name)
                    except Exception as rec_exc:
                        logger.debug("rate_manager.record_success failed: %s", rec_exc)
                return response

            except Exception as e:
                last_error = e
                _log.warning(
                    "Synthesis attempt %d failed (%s): %s",
                    attempt + 1, provider_name, e,
                )
                if self.rate_manager is not None:
                    try:
                        self.rate_manager.record_failure(provider_name, e)
                    except Exception as rec_exc:
                        logger.debug("rate_manager.record_failure failed: %s", rec_exc)
                continue

        # All retries exhausted — raise with context
        raise ValueError(
            f"Council synthesis failed after {max_retries}"
            f" attempts across {len(candidates)} providers.\n"
            f"Last error: {last_error}\n"
            f"Tried: {', '.join(candidates[:3])}\n"
            f"Fix: nvh config set"
            f" council.synthesis_provider <provider>"
        )
