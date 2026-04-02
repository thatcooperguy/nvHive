"""Conversation context management: history, token tracking, window management."""

from __future__ import annotations

from nvh.providers.base import CompletionResponse, Message
from nvh.storage import repository as repo


class ConversationManager:
    """Manages multi-turn conversation state and persistence."""

    def __init__(self, context_threshold: float = 0.8):
        self.context_threshold = context_threshold

    async def create_conversation(
        self,
        provider: str = "",
        model: str = "",
    ) -> str:
        """Create a new conversation and return its ID."""
        conv = await repo.create_conversation(provider=provider, model=model)
        return conv.id

    async def get_or_create_conversation(
        self,
        conversation_id: str | None = None,
        continue_last: bool = False,
        provider: str = "",
        model: str = "",
    ) -> str:
        """Get an existing conversation or create a new one."""
        if conversation_id:
            conv = await repo.get_conversation(conversation_id)
            if conv:
                return conv.id
            raise ValueError(f"Conversation {conversation_id} not found")

        if continue_last:
            conv = await repo.get_latest_conversation()
            if conv:
                return conv.id

        return await self.create_conversation(provider=provider, model=model)

    async def add_user_message(
        self,
        conversation_id: str,
        content: str,
    ) -> None:
        """Add a user message to the conversation."""
        await repo.add_message(
            conversation_id=conversation_id,
            role="user",
            content=content,
        )

    async def add_assistant_message(
        self,
        conversation_id: str,
        response: CompletionResponse,
    ) -> None:
        """Add an assistant response to the conversation."""
        await repo.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=response.content,
            provider=response.provider,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
        )

    async def get_messages(self, conversation_id: str) -> list[Message]:
        """Get all messages in a conversation as Message objects."""
        db_messages = await repo.get_messages(conversation_id)
        return [
            Message(role=m.role, content=m.content)
            for m in db_messages
        ]

    async def get_context_messages(
        self,
        conversation_id: str,
        system_prompt: str | None = None,
    ) -> list[Message]:
        """Get messages formatted for sending to a provider."""
        messages = await self.get_messages(conversation_id)

        result: list[Message] = []
        if system_prompt:
            result.append(Message(role="system", content=system_prompt))
        result.extend(messages)

        return result

    async def check_context_window(
        self,
        conversation_id: str,
        context_window: int,
        estimate_fn=None,
    ) -> tuple[bool, int, float]:
        """Check if conversation context is approaching the model's limit.

        Returns:
            (within_limit, estimated_tokens, utilization_ratio)
        """
        messages = await self.get_messages(conversation_id)
        all_text = " ".join(m.content for m in messages)

        if estimate_fn:
            tokens = estimate_fn(all_text)
        else:
            tokens = len(all_text) // 4

        if context_window <= 0:
            return True, tokens, 0.0

        ratio = tokens / context_window
        within = ratio < self.context_threshold

        return within, tokens, ratio

    async def list_conversations(self, limit: int = 20):
        """List recent conversations."""
        return await repo.list_conversations(limit=limit)
