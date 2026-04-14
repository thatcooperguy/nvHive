"""Interactive REPL for Hive CLI.

Launch with `hive repl`. Supports multi-turn conversations, inline /commands,
hive mode, streaming responses, and session cost tracking.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule

from nvh.core.engine import Engine
from nvh.providers.base import Message

console = Console()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HELP_TEXT = """
[bold]NVHive REPL — available commands[/bold]

  [cyan]/advisor <name>[/cyan]        Switch advisor (e.g. /advisor anthropic)
  [cyan]/model <name>[/cyan]          Switch model (e.g. /model gpt-4o)
  [cyan]/system <prompt>[/cyan]       Set system prompt
  [cyan]/clear[/cyan]                 Clear conversation history and start fresh
  [cyan]/convene[/cyan]               Toggle council mode on/off (or use /mode)
  [cyan]/mode <ask|convene|poll|throwdown>[/cyan]  Set default query mode
  [cyan]/auto-agents[/cyan]           Toggle auto-agent generation on/off
  [cyan]/cabinet <name>[/cyan]        Set agent cabinet (executive, engineering, etc.)
  [cyan]/cost[/cyan]                  Show cumulative session cost
  [cyan]/history[/cyan]               Show conversation history for this session
  [cyan]/save <path>[/cyan]           Export conversation to a JSON file
  [cyan]/remember <text>[/cyan]       Save a memory that persists across sessions
  [cyan]/forget <keyword>[/cyan]      Remove memories matching a keyword
  [cyan]/memories[/cyan]              List all stored memories
  [cyan]/tools[/cyan]                 Toggle tool use on/off and show available tools
  [cyan]/do <task>[/cyan]             Run agent loop on a task (hands-free mode)
  [cyan]/setup[/cyan]                 Re-run guided setup (GPU, providers, Ollama)
  [cyan]/code[/cyan]                  Switch to coding focus for the next message
  [cyan]/write[/cyan]                 Switch to writing focus for the next message
  [cyan]/research[/cyan]              Switch to research focus for the next message
  [cyan]/math[/cyan]                  Switch to math focus for the next message
  [cyan]/help[/cyan]                  Show this help message
  [cyan]/quit[/cyan] or [cyan]/exit[/cyan]        Exit the REPL

[dim]Multi-line input: start a block with \"\"\" and end it with \"\"\" on its own line.[/dim]
[dim]Ctrl+C — cancel current query and return to prompt.[/dim]
[dim]Ctrl+D — exit the REPL.[/dim]
"""

VALID_MODES = ("ask", "convene", "poll", "throwdown")

PRESETS = [
    "executive", "engineering", "security_review",
    "code_review", "product", "data", "full_board",
]


# ---------------------------------------------------------------------------
# Session State
# ---------------------------------------------------------------------------

class ReplSession:
    """Holds all mutable REPL state for the current session."""

    def __init__(
        self,
        provider: str | None,
        model: str | None,
        council_mode: bool,
        auto_agents: bool,
        preset: str | None,
        system_prompt: str | None,
        mode: str = "ask",
    ) -> None:
        self.provider = provider
        self.model = model
        self.council_mode = council_mode
        self.auto_agents = auto_agents
        self.preset = preset
        self.system_prompt = system_prompt
        self.mode = mode  # "ask", "convene", "poll", "throwdown"

        self.turn: int = 0
        self.session_cost: Decimal = Decimal("0")
        self.local_tokens: int = 0  # track tokens from free/local providers
        self.tools_enabled: bool = False  # LLM tool use toggle
        # Conversation history: list of {"role": str, "content": str}
        self.history: list[dict[str, str]] = []
        # Focus mode: one-shot override for the next message (None = no focus)
        self.focus: str | None = None
        # Offline tracking
        self.offline: bool = False

    @property
    def prompt_str(self) -> str:
        model_part = self.model or "auto"
        mode_tag = f" {self.mode}" if self.mode != "ask" else ""
        focus_tag = f" :{self.focus}" if self.focus else ""
        offline_tag = " offline" if self.offline else ""
        return f"[bold green][{model_part} #{self.turn + 1}{mode_tag}{focus_tag}{offline_tag}][/bold green] > "

    def add_user(self, content: str) -> None:
        self.history.append({"role": "user", "content": content})

    def add_assistant(self, content: str, provider: str = "", model: str = "") -> None:
        entry: dict[str, str] = {"role": "assistant", "content": content}
        if provider:
            entry["provider"] = provider
        if model:
            entry["model"] = model
        self.history.append(entry)

    def clear(self) -> None:
        self.history.clear()
        self.turn = 0

    def to_messages(self) -> list[Message]:
        """Convert history to Message list for the engine."""
        msgs: list[Message] = []
        if self.system_prompt:
            msgs.append(Message(role="system", content=self.system_prompt))
        for entry in self.history:
            msgs.append(Message(role=entry["role"], content=entry["content"]))
        return msgs


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def _read_line(prompt_markup: str) -> str | None:
    """
    Read a single line from stdin, printing a Rich-rendered prompt first.
    Returns None on EOF (Ctrl+D).
    """
    console.print(prompt_markup, end="")
    try:
        return input()
    except EOFError:
        return None


def _read_multiline() -> str:
    """Read lines until a \"\"\" terminator is encountered."""
    console.print('[dim]  (multi-line mode — end block with """ on its own line)[/dim]')
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == '"""':
            break
        lines.append(line)
    return "\n".join(lines)


def _get_input(session: ReplSession) -> str | None:
    """
    Read one full user input (possibly multi-line).
    Returns None on EOF.
    Strips surrounding whitespace.
    """
    raw = _read_line(session.prompt_str)
    if raw is None:
        return None
    stripped = raw.strip()
    if stripped == '"""':
        stripped = _read_multiline().strip()
    return stripped


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def _run_do_command_async(task: str, session: ReplSession, engine: Engine) -> None:
    """Async implementation of the /do agent loop for the REPL."""
    import time as _time

    from nvh.core.agent_loop import AgentStep, run_agent_loop
    from nvh.core.tools import ToolRegistry

    task_preview = task if len(task) <= 50 else task[:47] + "..."
    console.print()
    console.print(Panel(
        f"[bold]Task:[/bold] {task_preview}",
        title="[bold cyan]Agent Working[/bold cyan]",
        border_style="cyan",
        expand=False,
    ))
    console.print()

    tools = ToolRegistry()
    start = _time.monotonic()

    def on_step(step: AgentStep) -> None:
        thought_preview = step.thought[:80].rstrip() if step.thought else ""
        label = f"[bold]Step {step.iteration}[/bold]"
        if thought_preview and thought_preview != "Task complete":
            label += f": {thought_preview}{'...' if len(step.thought) > 80 else ''}"
        console.print(label)
        for call in step.tool_calls:
            args_str = ", ".join(f"{k}={repr(v)[:40]}" for k, v in call.get("args", {}).items())
            console.print(f"  [dim]→ tool:[/dim] [cyan]{call['tool']}[/cyan]({args_str})")
        for result in step.tool_results:
            if result.success:
                preview = result.output[:60].replace("\n", " ").rstrip()
                console.print(f"  [green]✓[/green] [dim]{preview}{'...' if len(result.output) > 60 else ''}[/dim]")
            else:
                console.print(f"  [red]✗[/red] [dim]{result.error}[/dim]")
        if not step.tool_calls:
            console.print("  [dim](no tools — generating final answer)[/dim]")
        console.print()

    result = await run_agent_loop(
        task=task,
        engine=engine,
        tools=tools,
        provider=session.provider,
        model=session.model,
        auto_approve_safe=True,
        on_step=on_step,
    )

    elapsed = _time.monotonic() - start

    console.print(Panel(
        result.final_response,
        title="[bold green]Result[/bold green]",
        border_style="green",
    ))
    status = "[green]completed[/green]" if result.completed else "[yellow]incomplete[/yellow]"
    console.print(
        f"\n[dim]{result.total_iterations} step(s) | "
        f"{result.total_tool_calls} tool call(s) | "
        f"{elapsed:.1f}s | {status}[/dim]\n"
    )
    if result.error and not result.completed:
        console.print(f"[dim yellow]Note: {result.error}[/dim yellow]\n")


def _handle_command(line: str, session: ReplSession):
    """
    Process a /command line.
    Returns True if the REPL should continue, False if it should exit.
    For /do, returns a ("do", task) tuple for the REPL loop to await.
    """
    parts = line.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/quit", "/exit"):
        return False

    elif cmd == "/help":
        console.print(HELP_TEXT)

    elif cmd == "/clear":
        session.clear()
        console.print("[dim]Conversation cleared.[/dim]")

    elif cmd == "/advisor":
        if not arg:
            console.print("[yellow]Usage: /advisor <name>[/yellow]")
        else:
            session.provider = arg
            console.print(f"[dim]Advisor set to [bold]{arg}[/bold][/dim]")

    elif cmd == "/model":
        if not arg:
            console.print("[yellow]Usage: /model <name>[/yellow]")
        else:
            session.model = arg
            console.print(f"[dim]Model set to [bold]{arg}[/bold][/dim]")

    elif cmd == "/system":
        if not arg:
            session.system_prompt = None
            console.print("[dim]System prompt cleared.[/dim]")
        else:
            session.system_prompt = arg
            console.print("[dim]System prompt set.[/dim]")

    elif cmd == "/convene":
        session.council_mode = not session.council_mode
        state = "ON" if session.council_mode else "OFF"
        if session.council_mode:
            session.mode = "convene"
        elif session.mode == "convene":
            session.mode = "ask"
        console.print(f"[dim]Council mode: [bold]{state}[/bold][/dim]")

    elif cmd == "/mode":
        if not arg or arg not in VALID_MODES:
            console.print(
                f"[yellow]Usage: /mode <{'|'.join(VALID_MODES)}>[/yellow]\n"
                "[dim]  ask      → single advisor (default)[/dim]\n"
                "[dim]  convene  → every question goes to council[/dim]\n"
                "[dim]  poll     → every question polls all advisors[/dim]\n"
                "[dim]  throwdown → every question gets two-pass analysis[/dim]"
            )
        else:
            session.mode = arg
            session.council_mode = (arg == "convene")
            console.print(f"[dim]Mode set to [bold]{arg}[/bold][/dim]")

    elif cmd == "/auto-agents":
        session.auto_agents = not session.auto_agents
        state = "ON" if session.auto_agents else "OFF"
        console.print(f"[dim]Auto-agents: [bold]{state}[/bold][/dim]")

    elif cmd == "/cabinet":
        if not arg:
            console.print(f"[yellow]Usage: /cabinet <name>. Available: {', '.join(PRESETS)}[/yellow]")
        else:
            session.preset = arg
            console.print(f"[dim]Agent cabinet set to [bold]{arg}[/bold][/dim]")

    elif cmd == "/cost":
        console.print(
            f"[dim]Session cost so far: [bold]${session.session_cost:.4f}[/bold] "
            f"over {session.turn} turn(s)[/dim]"
        )

    elif cmd == "/history":
        _print_history(session)

    elif cmd == "/save":
        if not arg:
            console.print("[yellow]Usage: /save <path>[/yellow]")
        else:
            _save_conversation(session, arg)

    elif cmd == "/remember":
        if not arg:
            console.print("[yellow]Usage: /remember <text>[/yellow]")
        else:
            from nvh.core.memory import get_memory_store
            store = get_memory_store()
            mem = store.add(arg, memory_type="fact", source="user")
            console.print(f"[dim]Memory saved: [bold][{mem.id}][/bold] {arg[:60]}{'...' if len(arg) > 60 else ''}[/dim]")

    elif cmd == "/forget":
        if not arg:
            console.print("[yellow]Usage: /forget <keyword>[/yellow]")
        else:
            from nvh.core.memory import get_memory_store
            store = get_memory_store()
            removed = store.forget(arg)
            if removed:
                console.print(f"[dim]Removed [bold]{removed}[/bold] memory{'s' if removed != 1 else ''} matching '{arg}'.[/dim]")
            else:
                console.print(f"[dim]No memories found matching '{arg}'.[/dim]")

    elif cmd == "/memories":
        from nvh.core.memory import get_memory_store
        store = get_memory_store()
        memories = store.get_all()
        if not memories:
            console.print("[dim]No memories stored yet. Use /remember <text> to add one.[/dim]")
        else:
            console.print(f"\n[bold]Stored memories ({len(memories)})[/bold]")
            for m in memories:
                console.print(
                    f"  [bold cyan][{m.id}][/bold cyan] "
                    f"[dim][{m.type}][/dim] {m.content}"
                )
            console.print()

    elif cmd == "/tools":
        from nvh.core.tools import ToolRegistry
        session.tools_enabled = not session.tools_enabled
        state = "ON" if session.tools_enabled else "OFF"
        console.print(f"[dim]Tool use: [bold]{state}[/bold][/dim]")
        if session.tools_enabled:
            registry = ToolRegistry()
            console.print()
            console.print("[bold]Available tools:[/bold]")
            for tool in registry.list_tools():
                safe_tag = "" if tool.safe else " [yellow](unsafe)[/yellow]"
                console.print(f"  [cyan]{tool.name}[/cyan]{safe_tag} — {tool.description}")
            console.print()

    elif cmd == "/do":
        if not arg:
            console.print("[yellow]Usage: /do <task>[/yellow]")
            console.print("[dim]Example: /do \"Find all TODO comments and summarize them\"[/dim]")
        else:
            # Return the coroutine — run_repl will await it
            return ("do", arg)

    elif cmd == "/setup":
        from nvh.cli.setup import guided_setup
        guided_setup(console)
        # Reload env keys so the REPL picks up any newly configured providers
        from nvh.cli.setup import load_env_keys
        load_env_keys()
        console.print("[dim]Setup complete. New providers are available for this session.[/dim]")

    elif cmd in ("/code", "/write", "/research", "/math"):
        focus_name = cmd[1:]  # strip leading "/"
        session.focus = focus_name
        console.print(f"[dim]Focus set to [bold]{focus_name}[/bold] for your next message.[/dim]")

    else:
        console.print(f"[yellow]Unknown command: {cmd}. Type /help for a list of commands.[/yellow]")

    return True


def _print_history(session: ReplSession) -> None:
    if not session.history:
        console.print("[dim]No conversation history yet.[/dim]")
        return
    console.print()
    for i, entry in enumerate(session.history):
        role = entry["role"]
        content = entry["content"]
        if role == "user":
            console.print(f"[bold cyan]You (turn {i // 2 + 1}):[/bold cyan]")
            console.print(f"  {content}")
        elif role == "assistant":
            provider = entry.get("provider", "")
            model = entry.get("model", "")
            label = f"{provider}/{model}" if provider else "assistant"
            console.print(f"[bold green]{label}:[/bold green]")
            console.print(Markdown(content))
        console.print()


def _save_conversation(session: ReplSession, path_str: str) -> None:
    out_path = Path(path_str).expanduser()
    payload = {
        "exported_at": datetime.now().isoformat(),
        "session_cost_usd": str(session.session_cost),
        "turns": session.turn,
        "advisor": session.provider,
        "model": session.model,
        "hive_mode": session.council_mode,
        "auto_agents": session.auto_agents,
        "cabinet": session.preset,
        "system_prompt": session.system_prompt,
        "history": session.history,
    }
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2))
        console.print(f"[dim]Conversation saved to [bold]{out_path}[/bold][/dim]")
    except Exception as exc:
        console.print(f"[red]Failed to save: {exc}[/red]")


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

async def _run_simple_query(
    engine: Engine,
    session: ReplSession,
    prompt: str,
) -> tuple[str, str, str, Decimal]:
    """
    Stream a simple (non-hive) query, printing deltas in real time.
    Returns (full_content, provider_name, model_name, cost).
    Raises asyncio.CancelledError on Ctrl+C — caller must handle.
    """
    decision = engine.router.route(
        query=prompt,
        provider_override=session.provider,
        model_override=session.model,
    )
    prov = engine.registry.get(decision.provider)
    pconfig = engine.config.providers.get(decision.provider)
    pmodel = session.model or decision.model or (pconfig.default_model if pconfig else "")

    # Build message list including history
    msgs = session.to_messages()
    # Remove any trailing assistant turns — history already has the full context
    # but we need to append the new user message (not yet in history)
    msgs.append(Message(role="user", content=prompt))

    start = time.monotonic()
    accumulated = ""
    cost: Decimal = Decimal("0")
    final_provider = decision.provider
    final_model = pmodel or decision.model

    stream_iter = prov.stream(
        messages=msgs,
        model=pmodel or None,
        temperature=engine.config.defaults.temperature,
        max_tokens=engine.config.defaults.max_tokens,
        system_prompt=session.system_prompt,
    )

    console.print()  # blank line before response
    async for chunk in stream_iter:
        if chunk.delta:
            console.print(chunk.delta, end="", highlight=False)
            accumulated += chunk.delta
        if chunk.is_final:
            if chunk.cost_usd is not None:
                cost = chunk.cost_usd
            if chunk.model:
                final_model = chunk.model
            if chunk.provider:
                final_provider = chunk.provider

    console.print()  # newline after streamed response

    elapsed_ms = int((time.monotonic() - start) * 1000)
    _print_turn_meta(final_provider, final_model, elapsed_ms, cost)

    return accumulated, final_provider, final_model, cost


async def _run_council_query(
    engine: Engine,
    session: ReplSession,
    prompt: str,
) -> tuple[str, Decimal]:
    """
    Run a hive query, displaying member responses and synthesis.
    Returns (synthesis_content, total_cost).
    Raises asyncio.CancelledError on Ctrl+C.
    """
    # Show agent info if applicable
    if session.auto_agents or session.preset:
        from nvh.core.agents import generate_agents, get_preset_agents
        enabled_count = len(engine.registry.list_enabled())
        if session.preset:
            personas = get_preset_agents(session.preset, prompt)
        else:
            personas = generate_agents(prompt, num_agents=enabled_count)
        console.print()
        console.print("[bold]Hive Mode[/bold] — expert advisors:")
        for p in personas:
            console.print(f"  [bold cyan]{p.role}[/bold cyan] — {p.expertise}")
        console.print()
    else:
        num = len(engine.registry.list_enabled())
        console.print(f"\n[bold]Hive Mode[/bold] — querying {num} advisors...\n")

    result = await engine.run_council(
        prompt=prompt,
        system_prompt=session.system_prompt,
        auto_agents=session.auto_agents,
        agent_preset=session.preset,
    )

    # Print each member response
    for label, resp in result.member_responses.items():
        persona = resp.metadata.get("persona", "")
        display_label = persona if persona else label
        console.print(Rule(f"[bold]{display_label}[/bold]", style="blue"))
        console.print(Markdown(resp.content))
        console.print(f"[dim]  {resp.provider}/{resp.model} | ${resp.cost_usd:.4f}[/dim]")
        console.print()

    # Print synthesis
    synthesis_content = ""
    if result.synthesis:
        console.print(Rule("[bold green]Synthesis[/bold green]", style="green"))
        console.print(Markdown(result.synthesis.content))
        synthesis_content = result.synthesis.content
    else:
        # No synthesis — use the first member response content as "output"
        if result.member_responses:
            synthesis_content = next(iter(result.member_responses.values())).content

    if result.failed_members:
        failed = ", ".join(
            k for k in result.failed_members if not k.startswith("_")
        )
        if failed:
            console.print(f"[yellow]  Failed members: {failed}[/yellow]")

    console.print(
        f"\n[dim]Hive total: ${result.total_cost_usd:.4f} | "
        f"{result.total_latency_ms}ms | "
        f"quorum {'met' if result.quorum_met else 'NOT met'}[/dim]"
    )

    return synthesis_content, result.total_cost_usd


def _print_turn_meta(provider: str, model: str, latency_ms: int, cost: Decimal) -> None:
    parts = []
    if provider:
        parts.append(f"[bold]{provider}[/bold]/{model}")
    if cost:
        parts.append(f"${cost:.4f}")
    if latency_ms:
        parts.append(f"{latency_ms}ms")
    if parts:
        console.print(f"[dim]  {' | '.join(parts)}[/dim]")


# ---------------------------------------------------------------------------
# Session summary helpers
# ---------------------------------------------------------------------------

# Rough estimate: average cloud cost per 1K tokens (input+output blended)
_CLOUD_COST_PER_1K_TOKENS = Decimal("0.004")
_FREE_PROVIDERS = frozenset({
    "ollama", "groq", "google", "cohere", "together", "fireworks",
    "cerebras", "sambanova", "huggingface", "ai21", "mock",
})


def _print_savings_summary(session: ReplSession) -> None:
    """Print session summary with savings estimate on exit."""
    if session.turn == 0:
        return

    # Estimate tokens from free/local providers by scanning history
    free_turns = sum(
        1 for entry in session.history
        if entry.get("role") == "assistant"
        and entry.get("provider", "") in _FREE_PROVIDERS
    )

    savings = Decimal("0")
    if free_turns > 0:
        # Rough estimate: ~300 tokens per turn average
        estimated_tokens = free_turns * 300
        savings = (Decimal(estimated_tokens) / 1000) * _CLOUD_COST_PER_1K_TOKENS

    spent = session.session_cost
    parts = [
        f"Session: [bold]{session.turn}[/bold] "
        f"{'query' if session.turn == 1 else 'queries'}",
        f"[bold]${spent:.2f}[/bold] spent",
    ]
    if savings > Decimal("0"):
        parts.append(f"Saved ~[bold green]${savings:.2f}[/bold green] vs cloud")

    console.print(f"[dim]{' | '.join(parts)}[/dim]")


# ---------------------------------------------------------------------------
# Main REPL loop
# ---------------------------------------------------------------------------

async def run_repl(
    engine: Engine,
    provider: str | None = None,
    model: str | None = None,
    council_mode: bool = False,
    auto_agents: bool = False,
    preset: str | None = None,
    system_prompt: str | None = None,
    mode: str = "ask",
) -> None:
    """
    Run the interactive REPL loop.

    Args:
        engine: Initialized (or uninitialised) Engine instance.
        provider: Default provider override.
        model: Default model override.
        council_mode: Start with council mode enabled.
        auto_agents: Start with auto-agent generation enabled.
        preset: Default agent preset name.
        system_prompt: Initial system prompt.
        mode: Default query mode — "ask", "convene", "poll", or "throwdown".
    """
    await engine.initialize()

    # Sync council_mode with mode parameter for consistency
    if mode == "convene":
        council_mode = True

    session = ReplSession(
        provider=provider,
        model=model,
        council_mode=council_mode,
        auto_agents=auto_agents,
        preset=preset,
        system_prompt=system_prompt,
        mode=mode,
    )

    # At session start, load memories and inject into system prompt
    from nvh.core.memory import get_memory_store
    memory_store = get_memory_store()
    memory_context = memory_store.get_context_prompt()
    if memory_context and not session.system_prompt:
        session.system_prompt = memory_context
    elif memory_context:
        session.system_prompt = memory_context + "\n\n" + session.system_prompt

    # Startup banner
    enabled = engine.registry.list_enabled()

    # Build advisor status line — mark free/local providers
    free_providers = {"ollama", "groq", "google", "cohere", "together", "fireworks",
                      "cerebras", "sambanova", "huggingface", "ai21", "mock"}
    if enabled:
        advisor_parts = []
        for adv in enabled:
            tag = " (free)" if adv in free_providers else ""
            advisor_parts.append(f"{adv}{tag}")
        advisor_line = ", ".join(advisor_parts)
    else:
        advisor_line = None

    # Determine display model
    default_model = (
        model
        or getattr(engine.config.defaults, "model", None)
        or "auto"
    )
    mode_display = session.mode

    if advisor_line:
        banner_body = (
            f"[bold cyan]Advisors:[/bold cyan] {advisor_line}\n"
            f"[bold cyan]Model:[/bold cyan]    {default_model}   "
            f"[dim]mode: {mode_display}[/dim]\n\n"
            "[dim]Just type your question and press Enter.[/dim]\n"
            "[dim]Type [bold]/help[/bold] for commands, [bold]/quit[/bold] to exit.[/dim]"
        )
    else:
        banner_body = (
            "[yellow]No advisors configured yet.[/yellow]\n"
            "Run: [bold]nvh ollama[/bold]  (or nvh groq, nvh github, nvh openai ...)\n\n"
            "[dim]Type [bold]/help[/bold] for commands, [bold]/quit[/bold] to exit.[/dim]"
        )

    console.print(
        Panel(
            banner_body,
            title="[bold]NVHive[/bold]",
            border_style="blue",
            expand=False,
        )
    )
    if system_prompt:
        preview = system_prompt[:60] + ("..." if len(system_prompt) > 60 else "")
        console.print(f"[dim]System prompt: {preview}[/dim]")
    console.print()

    # Main loop
    while True:
        # --- Read input ---
        try:
            user_input = _get_input(session)
        except KeyboardInterrupt:
            # Ctrl+C at the prompt: just print a newline and continue
            console.print()
            continue

        # EOF / Ctrl+D
        if user_input is None:
            console.print()
            _print_savings_summary(session)
            console.print("[dim]Goodbye.[/dim]")
            break

        # Empty input
        if not user_input:
            continue

        # --- Inline command ---
        if user_input.startswith("/"):
            result = _handle_command(user_input, session)
            # /do returns a ("do", task) sentinel to run asynchronously
            if isinstance(result, tuple) and result[0] == "do":
                try:
                    await asyncio.shield(_run_do_command_async(result[1], session, engine))
                except KeyboardInterrupt:
                    console.print("\n[yellow]  Agent cancelled.[/yellow]")
                except Exception as exc:
                    console.print(f"[red]Agent error: {exc}[/red]")
                continue
            if not result:
                _print_savings_summary(session)
                console.print("[dim]Goodbye.[/dim]")
                break
            continue

        # --- Offline check (every turn, non-blocking) ---
        was_offline = session.offline
        try:
            session.offline = not await engine.check_connectivity()
        except Exception:
            pass  # keep previous offline state on error
        if was_offline and not session.offline:
            console.print("[dim][back online — cloud advisors available][/dim]")
        elif not was_offline and session.offline:
            console.print("[dim][offline — using local models only][/dim]")

        # --- Check if this is a system action (not a question) ---
        from nvh.core.action_detector import detect_action
        action = detect_action(user_input)
        if action:
            console.print(f"[dim][action → {action.description}][/dim]")
            if action.requires_confirm:
                args_display = ", ".join(f"{k}={v}" for k, v in action.arguments.items())
                console.print(f"[yellow]  {action.tool_name}({args_display})[/yellow]")
                import typer as _typer
                if not _typer.confirm("  Execute?", default=True):
                    console.print("[dim]  Cancelled.[/dim]")
                    continue
            try:
                from nvh.core.tools import ToolRegistry
                tools = ToolRegistry()
                result = await tools.execute(action.tool_name, action.arguments)
                if result.success:
                    console.print(result.output)
                else:
                    console.print(f"[red]{result.error}[/red]")
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")
            continue

        # --- Query ---
        session.turn += 1
        session.add_user(user_input)

        # These are populated by the non-hive path
        resp_provider_out: str = ""
        resp_model_out: str = ""
        content: str = ""
        cost: Decimal = Decimal("0")

        # Apply focus mode: inject a one-shot system prompt override and provider hint
        focus_systems: dict[str, str] = {
            "code": (
                "You are an expert software engineer. Provide clear, correct, well-structured code. "
                "When writing code, include brief explanations of key decisions. "
                "Prefer idiomatic solutions. Highlight any edge cases or caveats."
            ),
            "write": (
                "You are a skilled writer. Write clearly and engagingly. "
                "Match the format to the request (email, essay, blog post, etc.)."
            ),
            "research": (
                "You are a thorough research assistant. Synthesize information from multiple sources. "
                "Always cite your sources. Highlight areas of consensus and disagreement."
            ),
            "math": (
                "You are an expert mathematician. Solve problems step by step, showing all work. "
                "Use clear notation. Verify your answer when possible."
            ),
        }
        _focus_system_override: str | None = None
        if session.focus:
            _focus_system_override = focus_systems.get(session.focus)
            console.print(f"[dim][focus: {session.focus}][/dim]")
            session.focus = None  # consume the one-shot focus

        # Build a temporary session view with focus system prompt merged in
        _effective_system = session.system_prompt
        if _focus_system_override:
            if _effective_system:
                _effective_system = _focus_system_override + "\n\n" + _effective_system
            else:
                _effective_system = _focus_system_override

        # Save/restore system_prompt around the query so focus is truly one-shot
        _orig_system = session.system_prompt
        session.system_prompt = _effective_system

        # If offline, steer toward local providers
        _offline_provider_override: str | None = session.provider
        if session.offline and not session.provider:
            enabled = engine.registry.list_enabled()
            for local_prov in ("ollama", "llm7"):
                if local_prov in enabled:
                    _offline_provider_override = local_prov
                    break

        try:
            if session.council_mode:
                content, cost = await asyncio.shield(
                    _run_council_query(engine, session, user_input)
                )
            else:
                # Temporarily override provider for offline routing
                _saved_provider = session.provider
                session.provider = _offline_provider_override
                try:
                    content, resp_provider_out, resp_model_out, cost = await asyncio.shield(
                        _run_simple_query(engine, session, user_input)
                    )
                finally:
                    session.provider = _saved_provider

        except KeyboardInterrupt:
            # Ctrl+C during a query — cancel and return to prompt
            console.print("\n[yellow]  Query cancelled.[/yellow]")
            # Remove the user message we just added since it got no response
            if session.history and session.history[-1]["role"] == "user":
                session.history.pop()
            session.turn -= 1
            session.system_prompt = _orig_system
            continue
        except asyncio.CancelledError:
            console.print("\n[yellow]  Query cancelled.[/yellow]")
            if session.history and session.history[-1]["role"] == "user":
                session.history.pop()
            session.turn -= 1
            session.system_prompt = _orig_system
            continue
        except Exception as exc:
            console.print(f"\n[red]Error: {exc}[/red]")
            # Revert turn count — the query failed
            if session.history and session.history[-1]["role"] == "user":
                session.history.pop()
            session.turn -= 1
            session.system_prompt = _orig_system
            continue

        # Restore original system prompt (focus was one-shot)
        session.system_prompt = _orig_system

        # Accumulate cost and save assistant turn to history
        session.session_cost += cost
        if session.council_mode:
            session.add_assistant(content, provider="hive", model="synthesis")
        else:
            session.add_assistant(content, provider=resp_provider_out, model=resp_model_out)
