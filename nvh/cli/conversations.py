"""Conversation management commands for the Hive CLI."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from nvh.storage import repository as repo

conversation_app = typer.Typer(help="Manage conversations")
console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine from a sync CLI context."""
    return asyncio.run(coro)


def _relative_time(dt: datetime) -> str:
    """Return a human-readable relative timestamp (e.g. '2 hours ago')."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    delta = now - dt
    total_seconds = int(delta.total_seconds())

    if total_seconds < 60:
        return "just now"
    if total_seconds < 3600:
        minutes = total_seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if total_seconds < 86400:
        hours = total_seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if total_seconds < 172800:
        return "yesterday"
    days = total_seconds // 86400
    if days < 30:
        return f"{days} days ago"
    if days < 365:
        months = days // 30
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} ago"


def _role_style(role: str) -> str:
    """Return a Rich markup style for a given message role."""
    return {
        "user": "bold cyan",
        "assistant": "bold green",
        "system": "bold yellow",
    }.get(role, "bold white")


async def _ensure_db() -> None:
    """Initialise the database if it hasn't been already."""
    await repo.init_db()


# ---------------------------------------------------------------------------
# hive conversation list
# ---------------------------------------------------------------------------

@conversation_app.command("list")
def conversation_list(
    limit: int = typer.Option(20, "-n", "--limit", help="Number of conversations to show"),
):
    """Show recent conversations."""

    async def _list():
        await _ensure_db()
        conversations = await repo.list_conversations(limit=limit)
        if not conversations:
            console.print("[dim]No conversations found.[/dim]")
            return

        table = Table(title=f"Recent Conversations (last {len(conversations)})", show_lines=False)
        table.add_column("ID", style="dim", no_wrap=True, max_width=12)
        table.add_column("Title / Preview", max_width=40)
        table.add_column("Provider", max_width=12)
        table.add_column("Model", max_width=20)
        table.add_column("Msgs", justify="right")
        table.add_column("Tokens", justify="right")
        table.add_column("Cost", justify="right")
        table.add_column("Last Updated", max_width=16)

        for conv in conversations:
            short_id = conv.id[:8]
            title = (conv.title or "[dim]—[/dim]")
            if len(title) > 40:
                title = title[:37] + "..."
            cost = f"${conv.total_cost_usd:.4f}" if conv.total_cost_usd else "[dim]$0.0000[/dim]"
            tokens = str(conv.total_tokens) if conv.total_tokens else "[dim]0[/dim]"
            table.add_row(
                short_id,
                title,
                conv.provider or "[dim]—[/dim]",
                conv.model or "[dim]—[/dim]",
                str(conv.message_count),
                tokens,
                cost,
                _relative_time(conv.updated_at),
            )

        console.print(table)
        console.print(
            "[dim]Tip: use [bold]hive conversation show <id>[/bold] with the first 8 chars of an ID.[/dim]"
        )

    _run(_list())


# ---------------------------------------------------------------------------
# hive conversation show <id>
# ---------------------------------------------------------------------------

@conversation_app.command("show")
def conversation_show(
    conversation_id: str = typer.Argument(..., help="Conversation ID (or prefix)"),
):
    """Display a full conversation with all messages."""

    async def _show():
        await _ensure_db()
        conv = await _resolve_conversation(conversation_id)
        if conv is None:
            console.print(f"[red]Conversation not found: {conversation_id}[/red]")
            raise typer.Exit(1)

        messages = await repo.get_messages(conv.id)

        # Header panel
        header_lines = [
            f"[bold]ID:[/bold]       {conv.id}",
            f"[bold]Provider:[/bold] {conv.provider or '—'}",
            f"[bold]Model:[/bold]    {conv.model or '—'}",
            f"[bold]Messages:[/bold] {conv.message_count}",
            f"[bold]Tokens:[/bold]   {conv.total_tokens:,}",
            f"[bold]Cost:[/bold]     ${conv.total_cost_usd:.4f}",
            f"[bold]Created:[/bold]  {_relative_time(conv.created_at)}",
            f"[bold]Updated:[/bold]  {_relative_time(conv.updated_at)}",
        ]
        if conv.title:
            header_lines.insert(1, f"[bold]Title:[/bold]    {conv.title}")
        console.print(Panel("\n".join(header_lines), title="Conversation", border_style="blue"))

        if not messages:
            console.print("[dim]No messages.[/dim]")
            return

        for msg in messages:
            role_style = _role_style(msg.role)
            meta_parts: list[str] = [f"[{role_style}]{msg.role.upper()}[/{role_style}]"]
            if msg.provider:
                meta_parts.append(f"[dim]{msg.provider}[/dim]")
            if msg.model:
                meta_parts.append(f"[dim]{msg.model}[/dim]")
            if msg.input_tokens or msg.output_tokens:
                meta_parts.append(
                    f"[dim]{msg.input_tokens} in / {msg.output_tokens} out tokens[/dim]"
                )
            if msg.cost_usd:
                meta_parts.append(f"[dim]${msg.cost_usd:.4f}[/dim]")
            if msg.latency_ms:
                meta_parts.append(f"[dim]{msg.latency_ms}ms[/dim]")

            title = "  ".join(meta_parts)
            border = {
                "user": "cyan",
                "assistant": "green",
                "system": "yellow",
            }.get(msg.role, "white")

            console.print(Panel(msg.content, title=title, border_style=border, padding=(0, 1)))

    _run(_show())


# ---------------------------------------------------------------------------
# hive conversation delete <id>
# ---------------------------------------------------------------------------

@conversation_app.command("delete")
def conversation_delete(
    conversation_id: str = typer.Argument(..., help="Conversation ID (or prefix)"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation prompt"),
):
    """Delete a conversation and all its messages."""

    async def _delete():
        await _ensure_db()
        conv = await _resolve_conversation(conversation_id)
        if conv is None:
            console.print(f"[red]Conversation not found: {conversation_id}[/red]")
            raise typer.Exit(1)

        preview = conv.title or conv.id
        if not yes:
            confirm = typer.confirm(
                f"Delete conversation '{preview}' ({conv.message_count} messages)?"
            )
            if not confirm:
                console.print("[dim]Aborted.[/dim]")
                raise typer.Exit(0)

        deleted = await repo.delete_conversation(conv.id)
        if deleted:
            console.print(f"[green]Deleted conversation {conv.id[:8]}.[/green]")
        else:
            console.print(f"[red]Failed to delete conversation {conv.id}.[/red]")
            raise typer.Exit(1)

    _run(_delete())


# ---------------------------------------------------------------------------
# hive conversation export <id>
# ---------------------------------------------------------------------------

@conversation_app.command("export")
def conversation_export(
    conversation_id: str = typer.Argument(..., help="Conversation ID (or prefix)"),
    format: str = typer.Option("json", "-f", "--format", help="Export format: json, markdown"),
    output_file: str | None = typer.Option(None, "-o", "--output", help="Write to file instead of stdout"),
):
    """Export a conversation to JSON or Markdown."""

    async def _export():
        await _ensure_db()
        conv = await _resolve_conversation(conversation_id)
        if conv is None:
            console.print(f"[red]Conversation not found: {conversation_id}[/red]")
            raise typer.Exit(1)

        messages = await repo.get_messages(conv.id)

        if format == "json":
            data = {
                "id": conv.id,
                "title": conv.title,
                "provider": conv.provider,
                "model": conv.model,
                "message_count": conv.message_count,
                "total_tokens": conv.total_tokens,
                "total_cost_usd": str(conv.total_cost_usd),
                "created_at": conv.created_at.isoformat(),
                "updated_at": conv.updated_at.isoformat(),
                "messages": [
                    {
                        "id": m.id,
                        "sequence": m.sequence,
                        "role": m.role,
                        "content": m.content,
                        "provider": m.provider,
                        "model": m.model,
                        "input_tokens": m.input_tokens,
                        "output_tokens": m.output_tokens,
                        "cost_usd": str(m.cost_usd),
                        "latency_ms": m.latency_ms,
                        "created_at": m.created_at.isoformat(),
                    }
                    for m in messages
                ],
            }
            text = json.dumps(data, indent=2)

        elif format == "markdown":
            lines: list[str] = []
            title = conv.title or conv.id
            lines.append(f"# {title}\n")
            lines.append(f"**Provider:** {conv.provider or '—'}  ")
            lines.append(f"**Model:** {conv.model or '—'}  ")
            lines.append(f"**Messages:** {conv.message_count}  ")
            lines.append(f"**Total tokens:** {conv.total_tokens:,}  ")
            lines.append(f"**Total cost:** ${conv.total_cost_usd:.4f}  ")
            lines.append(f"**Created:** {conv.created_at.isoformat()}  ")
            lines.append(f"**Updated:** {conv.updated_at.isoformat()}  ")
            lines.append("")

            for msg in messages:
                lines.append(f"---\n\n### {msg.role.upper()}")
                meta: list[str] = []
                if msg.provider:
                    meta.append(f"provider: {msg.provider}")
                if msg.model:
                    meta.append(f"model: {msg.model}")
                if msg.input_tokens or msg.output_tokens:
                    meta.append(f"tokens: {msg.input_tokens} in / {msg.output_tokens} out")
                if msg.cost_usd:
                    meta.append(f"cost: ${msg.cost_usd:.4f}")
                if meta:
                    lines.append(f"*{', '.join(meta)}*")
                lines.append("")
                lines.append(msg.content)
                lines.append("")

            text = "\n".join(lines)

        else:
            console.print(f"[red]Unknown format '{format}'. Use json or markdown.[/red]")
            raise typer.Exit(1)

        if output_file:
            from pathlib import Path
            Path(output_file).write_text(text, encoding="utf-8")
            console.print(f"[green]Exported to {output_file}[/green]")
        else:
            print(text)

    _run(_export())


# ---------------------------------------------------------------------------
# hive conversation search <query>
# ---------------------------------------------------------------------------

@conversation_app.command("search")
def conversation_search(
    query: str = typer.Argument(..., help="Text to search for across conversation messages"),
    limit: int = typer.Option(20, "-n", "--limit", help="Maximum number of results"),
):
    """Search conversation content by keyword."""

    async def _search():
        await _ensure_db()
        results = await repo.search_conversations(query=query, limit=limit)
        if not results:
            console.print(f"[dim]No conversations matched '{query}'.[/dim]")
            return

        console.print(f"Found [bold]{len(results)}[/bold] result{'s' if len(results) != 1 else ''} for [bold]\"{query}\"[/bold]\n")

        for conv, snippet in results:
            short_id = conv.id[:8]
            title = conv.title or "[dim](no title)[/dim]"
            updated = _relative_time(conv.updated_at)

            # Highlight the query in the snippet (case-insensitive)
            highlighted = _highlight_snippet(snippet, query)

            header = (
                f"[bold]{title}[/bold]  "
                f"[dim]{short_id}[/dim]  "
                f"[dim]{conv.provider or '—'}/{conv.model or '—'}[/dim]  "
                f"[dim]{updated}[/dim]"
            )
            console.print(Panel(highlighted, title=header, border_style="dim", padding=(0, 1)))

    _run(_search())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _resolve_conversation(id_prefix: str):
    """Look up a conversation by exact ID or unique 8-char prefix."""
    # Try exact match first
    conv = await repo.get_conversation(id_prefix)
    if conv:
        return conv

    # Fall back to prefix search across the recent list
    all_convs = await repo.list_conversations(limit=500)
    matches = [c for c in all_convs if c.id.startswith(id_prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        console.print(
            f"[yellow]Ambiguous prefix '{id_prefix}' matches {len(matches)} conversations. "
            "Provide a longer prefix.[/yellow]"
        )
        return None
    return None


def _highlight_snippet(text: str, query: str) -> str:
    """Wrap occurrences of query in the snippet with Rich bold markup."""
    lower_text = text.lower()
    lower_query = query.lower()
    result_parts: list[str] = []
    pos = 0
    while True:
        idx = lower_text.find(lower_query, pos)
        if idx == -1:
            result_parts.append(text[pos:])
            break
        result_parts.append(text[pos:idx])
        result_parts.append(f"[bold yellow]{text[idx:idx + len(query)]}[/bold yellow]")
        pos = idx + len(query)
    return "".join(result_parts)
