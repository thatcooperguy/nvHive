"""Council orchestrator: parallel dispatch, consensus, and synthesis."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

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


class CouncilOrchestrator:
    """Orchestrates parallel multi-LLM queries and consensus."""

    def __init__(
        self,
        config: CouncilConfig,
        registry: ProviderRegistry,
    ):
        self.config = config
        self.registry = registry

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
            provider_names = self.registry.list_enabled()

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

        # Dispatch to all members in parallel
        start = time.monotonic()
        member_responses: dict[str, CompletionResponse] = {}
        failed_members: dict[str, str] = {}

        tasks = {}
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
            tasks[label] = asyncio.create_task(
                self._call_member(member, msgs, member_system, temperature, max_tokens, timeout)
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
        except asyncio.TimeoutError:
            for label in [m.label for m in members]:
                if label not in member_responses and label not in failed_members:
                    failed_members[label] = "timed out"

        total_elapsed = int((time.monotonic() - start) * 1000)
        quorum_met = len(member_responses) >= quorum
        member_cost = sum(r.cost_usd for r in member_responses.values())

        # Synthesis
        synthesis: CompletionResponse | None = None
        if quorum_met and synthesize and len(member_responses) > 1:
            await on_event({"type": "synthesis_start"})

            synth_provider_name = self.config.council.synthesis_provider
            if not synth_provider_name or not self.registry.has(synth_provider_name):
                available = self.registry.list_enabled()
                synth_provider_name = available[0] if available else None

            if synth_provider_name:
                synth_provider = self.registry.get(synth_provider_name)
                synth_model = self._get_model_for_provider(synth_provider_name)

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

                synth_accumulated = ""
                synth_last_chunk = None

                try:
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
                            })
                        if chunk.is_final:
                            break

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

                    await on_event({
                        "type": "synthesis_complete",
                        "content": synth_accumulated,
                        "tokens": synth_tokens,
                        "cost": synth_cost,
                    })

                except Exception as exc:
                    failed_members["_synthesis"] = str(exc)
            else:
                failed_members["_synthesis"] = (
                    "No synthesis provider available — configure a synthesis advisor.\n"
                    "Run: nvh config set council.synthesis_provider groq"
                )

        total_cost = member_cost + (synthesis.cost_usd if synthesis else Decimal("0"))

        await on_event({
            "type": "council_complete",
            "total_cost": str(total_cost),
            "total_latency_ms": total_elapsed,
            "quorum_met": quorum_met,
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

        # Use the synthesis provider
        synth_provider_name = self.config.council.synthesis_provider
        if not synth_provider_name or not self.registry.has(synth_provider_name):
            # Fall back to first available provider
            available = self.registry.list_enabled()
            if not available:
                raise ValueError(
                    "No advisor available for council synthesis.\n"
                    "Run: nvh setup  (to configure advisors)\n"
                    "Or set a synthesis provider: nvh config set council.synthesis_provider groq"
                )
            synth_provider_name = available[0]

        synth_provider = self.registry.get(synth_provider_name)
        synth_model = self._get_model_for_provider(synth_provider_name)

        response = await synth_provider.complete(
            messages=[Message(role="user", content=synthesis_prompt)],
            model=synth_model or None,
            temperature=0.3,  # Lower temp for synthesis
            max_tokens=4096,
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

        synth_provider_name = self.config.council.synthesis_provider
        if not synth_provider_name or not self.registry.has(synth_provider_name):
            available = self.registry.list_enabled()
            synth_provider_name = available[0] if available else list(responses.keys())[0]

        synth_provider = self.registry.get(synth_provider_name)
        response = await synth_provider.complete(
            messages=[Message(role="user", content=judge_prompt)],
            temperature=0.1,
            max_tokens=4096,
        )

        response.metadata["strategy"] = "best_of"
        return response
