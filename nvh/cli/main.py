"""NVHive CLI — the main entry point for all commands.

Usage:
    nvh "What is machine learning?"          # Smart default — uses your profile settings
    nvh ask "Debug this code" -a anthropic   # Ask a specific advisor
    nvh convene "Should we use Rust?"        # Convene a council of agents
    nvh poll "Write a sort function"         # Poll all advisors
    nvh throwdown "Best database for SaaS?"  # Two-pass deep analysis with all APIs

The tool responds to: nvh, nvhive, nvHive, NVHive, NVHIVE (all are aliases).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import webbrowser
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# ----------------------------------------------------------------------
# Windows asyncio proactor GC crash workaround
# ----------------------------------------------------------------------
#
# Python bug: https://github.com/python/cpython/issues/81485
#
# On Windows, when httpx/litellm leave AsyncClient sockets open past
# the end of the event loop, the garbage collector eventually calls
# `_ProactorBasePipeTransport.__del__`, which walks attributes of an
# already-torn-down transport and access-violates (exit code 0xC0000005
# / 3221225477). `nvh status`, `nvh ask ...`, and most other query
# commands were all segfaulting on clean exit because of this.
#
# We defang `__del__` here — by the time the interpreter is in GC at
# process exit, we genuinely don't care about "did you close the
# transport?" warnings; we care about not crashing. Sockets are
# reclaimed by the OS when the process dies regardless.
if sys.platform == "win32":
    try:
        import asyncio.proactor_events as _proactor_events  # noqa: E402
        _proactor_events._ProactorBasePipeTransport.__del__ = lambda self: None  # type: ignore[method-assign]
    except Exception:
        pass

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from nvh import __version__
from nvh.config.settings import DEFAULT_CONFIG_PATH
from nvh.utils.ollama import DEFAULT_OLLAMA_URL, ollama_base_url

# Fix Windows legacy console (cp1252) Unicode crashes on symbols like ✓/✗.
# Without this, Rich raises UnicodeEncodeError whenever it tries to print
# a non-latin1 glyph (reported on Windows 11 via `nvh test`).
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _check_serve_deps() -> bool:
    """Return True if server dependencies (fastapi, uvicorn) are installed.

    If missing, prints a helpful install message and returns False.
    """
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        console.print(
            "[red]Server dependencies are not installed.[/red]\n"
            "  Install them with: [bold]pip install nvhive\\[serve][/bold]"
        )
        return False
    return True


def _format_cli_error(e: Exception) -> str:
    """Format an exception into a helpful, actionable CLI error message."""
    from nvh.providers.base import (
        AuthenticationError,
        ContentFilterError,
        InsufficientQuotaError,
        ProviderError,
        ProviderUnavailableError,
        RateLimitError,
        TokenLimitError,
    )

    msg = str(e)
    if isinstance(e, AuthenticationError):
        provider = getattr(e, "provider", "unknown")
        return (
            f"[red]Authentication failed[/red] ({provider}): {msg}\n"
            f"  Fix: [bold]nvh setup[/bold] to reconfigure, or check your API key."
        )
    if isinstance(e, RateLimitError):
        provider = getattr(e, "provider", "unknown")
        return (
            f"[yellow]Rate limited[/yellow] ({provider}): {msg}\n"
            f"  Wait a moment or try: [bold]nvh ask --advisor groq \"your question\"[/bold]"
        )
    if isinstance(e, InsufficientQuotaError):
        return (
            f"[yellow]Quota/budget exceeded[/yellow]: {msg}\n"
            f"  Free options:\n"
            f"    [bold]nvh ask --local \"your question\"[/bold]     (local, no cost)\n"
            f"    [bold]nvh ask --advisor groq ...[/bold]    (free tier)"
        )
    if isinstance(e, TokenLimitError):
        return (
            f"[yellow]Input too long[/yellow]: {msg}\n"
            f"  Try shortening the prompt or specifying a model with a larger context window."
        )
    if isinstance(e, ContentFilterError):
        return f"[yellow]Content filtered[/yellow]: {msg}"
    if isinstance(e, ProviderUnavailableError):
        return (
            f"[red]Provider unavailable[/red]: {msg}\n"
            f"  Check status: [bold]nvh status[/bold]"
        )
    if isinstance(e, ProviderError):
        # This includes the detailed fallback chain error we improved above
        return f"[red]{msg}[/red]"

    # Generic fallback — still better than bare "Error: ..."
    return f"[red]Error:[/red] {msg}"


# The callback handles `nvh "question"` with no subcommand — smart default mode
app = typer.Typer(
    name="nvh",
    help="NVHive — Multi-LLM orchestration. Just type: nvh \"your question\"",
    no_args_is_help=False,
)
console = Console(legacy_windows=False) if sys.platform == "win32" else Console()
# Usage errors and the human half of --json runs; stdout stays machine-readable.
err_console = Console(stderr=True, legacy_windows=False) if sys.platform == "win32" else Console(stderr=True)


async def _smart_default(prompt: str, *, force_iterative: bool = False):
    """Smart default handler — the universal nvh entry point.

    Detects intent from the prompt and automatically picks the best
    strategy based on what advisors are available. Users never need
    to pick a mode — nvhive assembles the right team for each task.

    Flow:
    1. SYSTEM ACTION? (install, open, find, kill) → execute directly
    2. --iterative or complex coding? → iterative multi-agent QA loop
    3. CODING TASK? (fix, refactor, add, implement, write) → agent mode
    4. CODE REVIEW? (review, check, audit) → review mode
    5. TEST REQUEST? (test, add tests, coverage) → test-gen mode
    6. COMPLEX QUESTION + 3+ healthy advisors? → council (auto-team)
    7. SIMPLE QUESTION → single best advisor

    In performant mode (default), the number of healthy advisors drives
    the sophistication: more advisors = multi-model verification,
    council synthesis, cross-architecture review. In cost mode, stick
    to the cheapest healthy provider.
    """
    from nvh.config.settings import load_config

    # --- Step 1: System actions (no LLM needed) ---
    from nvh.core.action_detector import detect_action
    from nvh.core.engine import Engine
    action = detect_action(prompt)
    if action:
        await _execute_action(action)
        return

    # --- Step 2: Initialize engine and detect what's available ---
    config = load_config()
    engine = Engine(config=config)
    await engine.initialize()

    healthy_advisors = [
        p for p in engine.registry.list_enabled()
        if engine.rate_manager.get_health_score(p) >= 0.2
    ]
    num_advisors = len(healthy_advisors)
    optimize = getattr(config.defaults, "mode", "performant")
    is_performant = optimize != "cost"

    # --- Step 3: Detect intent from the prompt ---
    intent = _classify_intent(prompt)

    # --- Step 4: Route to the right mode ---

    # Iterative QA loop — forced via --iterative or auto for complex coding
    use_iterative = force_iterative or (
        intent == "iterative_coding" and is_performant and num_advisors >= 2
    )
    if use_iterative:
        console.print(
            f"[bold dim][[bold green]iterative[/bold green] → multi-agent QA loop | {num_advisors} advisor(s)][/bold dim]\n"
        )
        try:
            from rich.live import Live
            from rich.text import Text

            from nvh.core.iterative_loop import (
                IterativeResult,
                format_iterative_result,
                iterative_solve,
            )

            # ── Rich live progress panel for iterative solve ──
            _live_lines: list[str] = []

            def _build_live_panel() -> Panel:
                markup = "\n".join(_live_lines)
                body = Text.from_markup(markup) if _live_lines else Text("Starting...")
                return Panel(body, title="[bold]Iterative Solve[/bold]", border_style="blue")

            def _on_progress(stage: str, message: str, fraction: float) -> None:
                verdict_colors = {"PASSED": "green", "PARTIAL": "yellow", "FAILED": "red"}
                if stage.startswith("round_"):
                    # Extract verdict from message like "Round 2: PARTIAL"
                    for v, c in verdict_colors.items():
                        if v in message:
                            message = message.replace(v, f"[{c}]{v}[/{c}]")
                            break
                elif stage == "budget_exceeded":
                    message = f"[red]{message}[/red]"

                pct = int(fraction * 100)
                _live_lines.append(f"  [dim][{pct:3d}%][/dim] {message}")
                live.update(_build_live_panel())

            def _on_round_detail(rnd) -> None:
                """Called after each round to show agent details."""
                verdict_colors = {"PASSED": "green", "PARTIAL": "yellow", "FAILED": "red"}
                vc = verdict_colors.get(rnd.qa_verdict, "white")
                _live_lines.append(
                    f"        Agents: [cyan]{', '.join(rnd.agents_used)}[/cyan]"
                )
                if rnd.spawned_agents:
                    _live_lines.append(
                        f"        Spawned: [magenta]{', '.join(rnd.spawned_agents)}[/magenta]"
                    )
                _live_lines.append(
                    f"        Verdict: [{vc}]{rnd.qa_verdict}[/{vc}]"
                    f"  [dim](${rnd.cost_usd:.4f}, {rnd.duration_ms}ms)[/dim]"
                )
                live.update(_build_live_panel())

            with Live(_build_live_panel(), console=console, refresh_per_second=4) as live:
                result: IterativeResult = await iterative_solve(
                    task=prompt,
                    engine=engine,
                    working_dir=Path(".").resolve(),
                    on_progress=_on_progress,
                )
                # After solve completes, add per-round detail lines
                for rnd in result.rounds:
                    _on_round_detail(rnd)
                live.update(_build_live_panel())

            # ── Final synthesis panel ──
            console.print()
            console.print(Panel(
                Markdown(result.final_synthesis),
                title="[bold green]Final Synthesis[/bold green]" if result.converged
                else "[bold yellow]Final Synthesis (not converged)[/bold yellow]",
                border_style="green" if result.converged else "yellow",
            ))
            console.print(format_iterative_result(result))
        except Exception as e:
            console.print(_format_cli_error(e))
        return

    if intent == "coding":
        console.print(f"[bold dim][[green]agent[/green] → coding task detected | {num_advisors} advisor(s)][/bold dim]\n")
        try:
            from pathlib import Path as _Path

            from nvh.core.agent_loop import AgentStep
            from nvh.core.agentic import AgentMode, auto_detect_config, run_coding_agent

            mode = AgentMode.MULTI if (is_performant and num_advisors >= 2) else AgentMode.SINGLE
            agent_config = auto_detect_config(engine, mode=mode)
            agent_config.quality_gates = True

            def on_step(step: AgentStep) -> None:
                thought = step.thought[:100].rstrip() if step.thought else ""
                label = f"[bold]Step {step.iteration}[/bold]"
                if thought and thought != "Task complete":
                    label += f": {thought}"
                console.print(label)
                for call in step.tool_calls:
                    args_str = ", ".join(f"{k}={repr(v)[:50]}" for k, v in call.get("args", {}).items())
                    console.print(f"  [dim]tool:[/dim] [cyan]{call['tool']}[/cyan]({args_str})")
                for result in step.tool_results:
                    if result.success:
                        preview = result.output[:80].replace("\n", " ").rstrip()
                        console.print(f"  [green]ok[/green] [dim]{preview}{'...' if len(result.output) > 80 else ''}[/dim]")
                    else:
                        console.print(f"  [red]err[/red] [dim]{result.error[:80]}[/dim]")
                console.print()

            import sys as _sys

            def _stream_token(delta: str) -> None:
                _sys.stdout.write(delta)
                _sys.stdout.flush()

            result = await run_coding_agent(
                task=prompt,
                engine=engine,
                config=agent_config,
                working_dir=_Path(".").resolve(),
                on_step=on_step,
                on_token=_stream_token,
            )
            if result.error:
                console.print(f"\n[red]{result.error}[/red]")
            else:
                console.print()  # newline after streamed output
                console.print(Panel(result.final_summary or "(completed)", title="[bold green]Result[/bold green]", border_style="green"))
                if result.files_modified or result.files_created:
                    for f in result.files_modified:
                        console.print(f"  [yellow]M[/yellow] {f}")
                    for f in result.files_created:
                        console.print(f"  [green]A[/green] {f}")
            console.print(f"\n[dim]{result.total_iterations} step(s) | {result.total_tool_calls} tool(s) | {result.duration_ms}ms[/dim]")
        except Exception as e:
            console.print(_format_cli_error(e))

    elif intent == "review":
        console.print(f"[bold dim][[yellow]review[/yellow] → {num_advisors} advisor(s)][/bold dim]\n")
        try:
            from pathlib import Path as _Path

            from nvh.core.agent_review import review_changes
            from nvh.core.agentic import AgentMode, auto_detect_config

            mode = AgentMode.MULTI if (is_performant and num_advisors >= 2) else AgentMode.SINGLE
            agent_config = auto_detect_config(engine, mode=mode)
            result = await review_changes(engine, agent_config, _Path(".").resolve(), "staged")

            status = "[green]Approved[/green]" if result.approved else "[yellow]Changes requested[/yellow]"
            console.print(f"[bold]{status}[/bold] — {result.summary}\n")
            for finding in result.findings:
                sev_color = {"high": "red", "medium": "yellow", "low": "cyan", "info": "dim"}.get(finding.severity, "white")
                loc = f"{finding.file}:{finding.line}" if finding.line else finding.file
                console.print(f"  [{sev_color}]{finding.severity.upper()}[/{sev_color}] {finding.category} at {loc}")
                console.print(f"    {finding.issue}")
            console.print(f"\n[dim]{len(result.findings)} finding(s) | {', '.join(result.reviewer_models)} | {result.duration_ms}ms[/dim]")
        except Exception as e:
            console.print(_format_cli_error(e))

    elif intent == "testgen":
        console.print(f"[bold dim][[blue]test-gen[/blue] → {num_advisors} advisor(s)][/bold dim]\n")
        try:
            from pathlib import Path as _Path

            from nvh.core.agent_testgen import generate_tests
            from nvh.core.agentic import auto_detect_config

            agent_config = auto_detect_config(engine)
            # Extract target file from the prompt if mentioned
            import re
            file_match = re.search(r'(\S+\.py)', prompt)
            target = file_match.group(1) if file_match else "--coverage-gaps"

            result = await generate_tests(engine, agent_config, _Path(".").resolve(), target)
            if result.test_file:
                console.print(f"[green]Tests written to:[/green] {result.test_file}")
            console.print(f"[bold]Generated:[/bold] {result.tests_generated} | [green]Passing:[/green] {result.tests_passing} | [red]Failing:[/red] {result.tests_failing}")
            console.print(f"\n[dim]{result.duration_ms}ms | model: {result.model_used}[/dim]")
        except Exception as e:
            console.print(_format_cli_error(e))

    elif intent == "complex" and is_performant and num_advisors >= 3:
        # Complex question + enough advisors → council automatically
        # Smart agent-to-LLM matching: each expert persona gets the
        # best LLM for their specialty based on learning engine data
        try:
            from nvh.core.agent_matching import format_team_report, match_agents_to_providers
            from nvh.core.agents import generate_agents
            personas = generate_agents(prompt, num_agents=min(num_advisors, 5))
            assignments = match_agents_to_providers(personas, engine)
            if assignments:
                console.print("[bold dim][[magenta]council[/magenta] → assembling expert team][/bold dim]\n")
                console.print(format_team_report(assignments))
                console.print()
            else:
                console.print(f"[bold dim][[magenta]council[/magenta] → {num_advisors} advisors, auto-team][/bold dim]\n")
        except Exception:
            console.print(f"[bold dim][[magenta]council[/magenta] → {num_advisors} advisors, auto-team][/bold dim]\n")

        try:
            result = await engine.run_council(
                prompt=prompt,
                auto_agents=True,
                synthesize=True,
            )
            if result.synthesis:
                console.print(result.synthesis.content)
                confidence_part = ""
                if result.confidence_score is not None:
                    pct = int(result.confidence_score * 100)
                    summary = result.agreement_summary or ""
                    confidence_part = f" | Confidence: {pct}%"
                    if summary:
                        confidence_part += f" — {summary}"
                console.print(
                    f"\n[bold]Agents:[/bold] [dim]{', '.join(result.agents_used)}[/dim]"
                    f" | [bold]Cost:[/bold] [dim]${result.total_cost_usd:.4f}[/dim]"
                    f" | [bold]Latency:[/bold] [dim]{result.total_latency_ms}ms[/dim]"
                    f"{confidence_part}"
                )
            else:
                for label, resp in result.member_responses.items():
                    console.print(Panel(resp.content, title=label, border_style="blue"))
        except Exception as e:
            console.print(_format_cli_error(e))

    else:
        # Simple question or cost mode → single best advisor
        try:
            decision = engine.router.route(prompt)
            mode_label = "ask" if num_advisors <= 2 else "ask (simple)"
            console.print(f"[bold dim][[cyan]ask[/cyan] → {decision.provider}/{decision.model}][/bold dim]\n")
            with console.status(f"Querying {decision.provider}...", spinner="dots"):
                resp = await engine.query(prompt=prompt, stream=False)
            console.print(resp.content)
            if resp.fallback_from:
                console.print(f"\n[yellow]↪ Failover: {resp.fallback_from} → {resp.provider}[/yellow]")
            meta_parts = [
                f"[bold]Advisor:[/bold] [dim]{resp.provider}[/dim]",
                f"[bold]Model:[/bold] [dim]{resp.model}[/dim]",
                f"[bold]Tokens:[/bold] [dim]{resp.usage.input_tokens}/{resp.usage.output_tokens}[/dim]",
                f"[bold]Cost:[/bold] [dim]${resp.cost_usd:.4f}[/dim]",
                f"[bold]Latency:[/bold] [dim]{resp.latency_ms}ms[/dim]",
            ]
            console.print(f"\n{' | '.join(meta_parts)}")
        except Exception as e:
            console.print(_format_cli_error(e))


def _classify_intent(prompt: str) -> str:
    """Classify the user's prompt into an intent category.

    Returns one of: "iterative_coding", "coding", "review", "testgen", "complex", "simple"

    This is deliberately keyword-based for speed and reliability.
    The TF-IDF classifier in action_detector.py handles system actions;
    this handles LLM-destined prompts.
    """
    p = prompt.lower().strip()

    import re

    # ORDER MATTERS: more specific intents are checked first so
    # "add tests" matches testgen (specific) not coding (generic).

    # Test generation — checked FIRST because "add tests" would
    # otherwise match the generic coding pattern "add + noun"
    testgen_patterns = [
        r'\b(add|write|create|generate)\s+(unit\s+)?tests?\b',
        r'\btest\s+(coverage|generation|gen)\b',
        r'\bcoverage\s+gaps?\b',
        r'\btest.gen\b',
        r'\btest\s+coverage\b',
        r'\bhow\s+to\s+test\s+(this|that|it|the)\b',
        r'\bneed\s+tests?\s+(for|on|in)\b',
    ]
    for pattern in testgen_patterns:
        if re.search(pattern, p):
            return "testgen"

    # Review indicators
    review_patterns = [
        r'\breview\b.*\b(change|code|pr|pull|commit|diff|staged)\b',
        r'\breview\s+(my|the|this)\b',
        r'\bcheck\s+(my|the|this)\s+(code|changes|pr|diff)\b',
        r'\baudit\s+(the|this|my)\s*(code|security|codebase)\b',
        r'\blook\s+at\s+(my|the|this)\s+(code|changes|diff|pr|pull)\b',
        r'\bwhat\s+do\s+you\s+think\s+of\s+(this|my|the)\s+(code|implementation|solution|approach)\b',
        r'\bis\s+(this|my|the)\s+(code|implementation|solution)\s+(correct|safe|good|ok|secure|clean)\b',
    ]
    for pattern in review_patterns:
        if re.search(pattern, p):
            return "review"

    # Complex coding — tasks that benefit from iterative multi-agent solving
    # (multi-step, cross-cutting, or explicitly architectural coding tasks)
    iterative_patterns = [
        r'\b(architect|redesign|rewrite|overhaul|rearchitect)\b',
        r'\b(refactor|migrate|convert|port)\b.*\b(entire|whole|all|full|codebase|project|system)\b',
        r'\b(build|create|implement)\s+(a\s+)?(full|complete|entire|end.to.end|whole)\b',
        r'\b(fix|debug|investigate)\b.*\b(multiple|several|all|every|across)\b',
        r'\b(design\s+and\s+implement|plan\s+and\s+build|architect\s+and\s+code)\b',
    ]
    for pattern in iterative_patterns:
        if re.search(pattern, p):
            return "iterative_coding"

    # Coding task — the user wants code changed
    coding_patterns = [
        r'\b(fix|refactor|implement|add|create|write|build|update|change|modify|remove|delete|rename|move|extract|inline|optimize)\b.*(code|function|method|class|file|module|endpoint|api|bug|error|feature|provider|parser|field|handler|config|route|component|service|model|schema|migration|script|command|tool|plugin|hook|middleware|decorator|fixture|helper|util|wrapper|adapter|client|server|view|page|template|style)',
        r'\b(fix|refactor|implement|add|create|build|update|remove)\b.*\.(py|js|ts|tsx|jsx|go|rs|java|cpp|c|rb|sh)\b',
        r'\bfix\s+(the|this|my|a)\b',
        r'\badd\s+(a|the)?\s*(new\s+)?(endpoint|route|function|method|class|feature|provider|handler|middleware)\b',
        r'\brefactor\b',
        r'\bimplement\b',
        # "debug/troubleshoot/investigate" + any context
        r'\b(debug|troubleshoot|investigate)\b.+',
        # "help me" + coding verb
        r'\bhelp\s+me\s+(fix|write|build|debug|implement|create|refactor|deploy|setup|set\s+up)\b',
        # "why is ... broken/failing/crashing/erroring"
        r'\bwhy\s+(is|are|does|do)\b.+\b(broken|failing|crashing|erroring|not\s+working)\b',
        # "make ... work/faster/better"
        r'\bmake\b.+\b(work|faster|better|slower|efficient)\b',
        # "how do I" + coding verb
        r'\bhow\s+do\s+i\s+(implement|connect|integrate|deploy|setup|set\s+up|configure|build|fix|debug)\b',
        # File extensions mentioned even without a verb
        r'\.\b(py|js|ts|tsx|jsx|go|rs|java|cpp|c|rb|sh|css|html|vue|svelte|kt|swift|scala|sql)\b',
        # Error/exception/crash/bug context
        r'\b(error|exception|crash|bug|traceback|stack\s*trace|segfault|panic)\b.*\b(in|with|when|from|on|at|after|before|during)\b',
        r'\b(in|with|when|from|on|at)\b.*\b(error|exception|crash|bug|traceback|stack\s*trace|segfault|panic)\b',
        # Migration/upgrade/convert/port (require "from" or a direct object to avoid matching "should we migrate")
        r'\b(migrate|upgrade|convert|port)\s+(the|from|my|our|this)\b',
    ]
    for pattern in coding_patterns:
        if re.search(pattern, p):
            return "coding"

    # Complex question indicators (benefit from council)
    complex_patterns = [
        r'\b(compare|vs|versus|trade.?off|pros?\s+and\s+cons?|should\s+(we|i)|which\s+is\s+better|debate|evaluate|analyze|architect)\b',
        r'\b(design|architecture|strategy|approach|recommend|suggest)\b.*\b(for|to|about|a|the|scalab|system|service|platform)\b',
        r'\bexplain.*(how|why|when).*\b(work|different|compare|scale|perform)\b',
        # "what's the best way to"
        r'\bwhat.?s\s+the\s+best\s+(way|approach|method|practice)\b',
        # "how should I approach"
        r'\bhow\s+should\s+i\s+(approach|handle|structure|organize|design)\b',
        # Multi-part questions (contain "and" + question mark)
        r'\band\b.*\?',
    ]
    for pattern in complex_patterns:
        if re.search(pattern, p):
            return "complex"

    return "simple"

async def _execute_action(action):
    """Execute a detected system action directly — no LLM needed."""
    from nvh.core.tools import ToolRegistry

    tools = ToolRegistry()
    tool = tools.get(action.tool_name)
    if not tool:
        console.print(f"[red]Tool not found: {action.tool_name}[/red]")
        return

    # Show what we're about to do
    console.print(f"[dim][action → {action.description}][/dim]")

    # Confirm unsafe actions
    if action.requires_confirm:
        args_display = ", ".join(f"{k}={v}" for k, v in action.arguments.items())
        console.print(f"[yellow]  {action.tool_name}({args_display})[/yellow]")
        import typer
        if not typer.confirm("  Execute?", default=True):
            console.print("[dim]  Cancelled.[/dim]")
            return

    # Execute
    try:
        result = await tools.execute(action.tool_name, action.arguments)
        if result.success:
            console.print(result.output)
        else:
            console.print(f"[red]{result.error}[/red]")
    except Exception as e:
        console.print(_format_cli_error(e))


async def _launch_default_repl():
    """Launch the REPL with smart defaults — local-first, zero config."""
    from nvh.config.settings import load_config
    from nvh.core.engine import Engine

    config = load_config()
    engine = Engine(config=config)
    enabled = await engine.initialize()

    if not enabled:
        # No providers configured — guide the user
        console.print("[bold yellow]Welcome to NVHive![/bold yellow]\n")
        console.print("No AI advisors are configured yet. Let's set you up:\n")
        console.print("  [bold]nvh setup[/bold]                       — configure free AI providers (recommended)")
        console.print("  [bold]nvh models pull --recommended[/bold]   — set up local AI on your GPU")
        console.print("  [bold]nvh advisor login openai[/bold]        — add your OpenAI API key")
        console.print("  [bold]nvh advisor login groq[/bold]          — add Groq (free, ultra-fast)\n")

        # Check if Ollama is available even without config
        try:
            import httpx
            resp = httpx.get(f"{ollama_base_url()}/api/tags", timeout=2)
            if resp.status_code == 200:
                console.print("[green]Ollama detected! Enabling local AI...[/green]")
                # Auto-enable ollama and continue to REPL
                from nvh.cli.repl import run_repl
                await run_repl(engine=engine)
                return
        except Exception:
            pass

        return

    from nvh.cli.repl import run_repl
    await run_repl(engine=engine)


# ---------------------------------------------------------------------------
# Advisor-as-command: nvh openai "question" or nvh openai (setup)
# ---------------------------------------------------------------------------

KNOWN_ADVISORS = {
    "openai": {"name": "OpenAI", "url": "https://platform.openai.com/api-keys", "free_tier": False},
    "anthropic": {
        "name": "Anthropic",
        "url": "https://console.anthropic.com/settings/keys",
        "free_tier": False,
    },
    "google": {
        "name": "Google Gemini",
        "url": "https://aistudio.google.com/apikey",
        "free_tier": True, "free_info": "15 req/min free",
    },
    "groq": {
        "name": "Groq",
        "url": "https://console.groq.com/keys",
        "free_tier": True,
        "free_info": "Free tier: 30 req/min, 14.4K tok/min",
    },
    "grok": {"name": "Grok (xAI)", "url": "https://console.x.ai", "free_tier": False},
    "mistral": {
        "name": "Mistral",
        "url": "https://console.mistral.ai/api-keys",
        "free_tier": True,
        "free_info": "Free Experiment plan: 2 RPM",
    },
    "cohere": {
        "name": "Cohere",
        "url": "https://dashboard.cohere.com/api-keys",
        "free_tier": True,
        "free_info": "Trial API key included on signup",
    },
    "deepseek": {
        "name": "DeepSeek",
        "url": "https://platform.deepseek.com",
        "free_tier": False,
        "free_info": "Very cheap: $0.07/M tokens",
    },
    "ollama": {
        "name": "Ollama (Local)",
        "url": "https://ollama.com/download",
        "free_tier": True,
        "free_info": "Unlimited, free, runs on your GPU",
    },
    "mock": {
        "name": "Mock (Testing)", "url": "",
        "free_tier": True,
        "free_info": "Testing only, no real API calls",
    },
    "perplexity": {
        "name": "Perplexity",
        "url": "https://www.perplexity.ai/settings/api",
        "free_tier": False,
        "free_info": "Search-augmented responses with citations",
    },
    "together": {
        "name": "Together AI",
        "url": "https://api.together.xyz/settings/api-keys",
        "free_tier": False,
        "free_info": "Requires $5 minimum purchase",
    },
    "fireworks": {
        "name": "Fireworks AI",
        "url": "https://fireworks.ai/account/api-keys",
        "free_tier": True,
        "free_info": "Free tier available",
    },
    "openrouter": {
        "name": "OpenRouter",
        "url": "https://openrouter.ai/keys",
        "free_tier": False,
        "free_info": "Routes to best available provider",
    },
    "cerebras": {
        "name": "Cerebras",
        "url": "https://cloud.cerebras.ai",
        "free_tier": True,
        "free_info": "Free tier: 30 req/min",
    },
    "sambanova": {
        "name": "SambaNova",
        "url": "https://cloud.sambanova.ai",
        "free_tier": True,
        "free_info": "Free tier available",
    },
    "huggingface": {
        "name": "Hugging Face",
        "url": "https://huggingface.co/settings/tokens",
        "free_tier": True,
        "free_info": "Free Inference API",
    },
    "ai21": {
        "name": "AI21 Labs",
        "url": "https://studio.ai21.com/account/api-key",
        "free_tier": True,
        "free_info": "Free tier available",
    },
    "nvidia": {
        "name": "NVIDIA NIM",
        "url": "https://build.nvidia.com",
        "free_tier": True,
        "free_info": (
            "1000+ free API credits, 40 RPM,"
            " NVIDIA Developer Program"
        ),
    },
    "siliconflow": {
        "name": "SiliconFlow",
        "url": "https://cloud.siliconflow.cn",
        "free_tier": True,
        "free_info": "Permanently free models at 1000 RPM",
    },
    "llm7": {
        "name": "LLM7",
        "url": "https://llm7.io",
        "free_tier": True,
        "free_info": (
            "Anonymous access: 30 RPM,"
            " no signup required"
        ),
    },
}


# ---------------------------------------------------------------------------
# Pre-0.42 spellings, kept as hidden aliases for one release: old name ->
# replacement spelling. Every entry is registered through _alias(); the
# did-you-mean hint and docs/COMMANDS.md read this table. Commands hidden
# for other reasons (benchmark, the removed template group) stay out of it.
# ---------------------------------------------------------------------------

DEPRECATED_ALIASES: dict[str, str] = {
    # query modes -> nvh ask
    "code": "ask --focus code",
    "write": "ask --focus write",
    "research": "ask --focus research",
    "math": "ask --focus math",
    "quick": "ask --fast --raw",
    "safe": "ask --local",
    "pipe": "ask --raw",
    "clip": "ask --clipboard",
    # diagnostic verbs -> nvh status
    "health": "status --providers",
    "why": "status --routing",
    "doctor": "status --deep",
    "test": "status --smoke",
    "smoke": "status --smoke",
    "debug": "status --report",
    "selfcheck": "status --report --live --imports",
    # knowledge base -> nvh rag
    "knowledge": "rag",
    "learn": "rag add",
    # `nvh nvidia` is the infrastructure dashboard, so that advisor is
    # reachable only via `nvh ask -p nvidia`.
    **{name: f"ask -p {name}" for name in KNOWN_ADVISORS if name not in ("mock", "nvidia")},
}


def _pop_flag(argv: list[str], *spellings: str, value: bool = False) -> str | bool | None:
    """Remove every occurrence of a legacy flag from argv; the last value (or True) wins."""
    found: str | bool | None = None
    i = 0
    while i < len(argv):
        name, eq, inline = argv[i].partition("=")
        if name not in spellings:
            i += 1
            continue
        if not value:
            found = True
            del argv[i]
        elif eq:
            found = inline
            del argv[i]
        else:
            found = argv[i + 1] if i + 1 < len(argv) else ""
            del argv[i:i + 2]
    return found


def _alias(name: str, *, translate=None, note: str | None = None) -> None:
    """Register `nvh <name>` as a hidden forwarder to its DEPRECATED_ALIASES target.

    The alias declares no options of its own: everything after the name is
    re-parsed by the real command, so `nvh debug --live` is exactly
    `nvh status --report --live` and `--help` shows the target's help.
    ``translate(argv)`` may rewrite the assembled argv (legacy flag
    spellings) or return None once it has handled the call itself.
    """
    target = DEPRECATED_ALIASES[name]

    def run_alias(ctx: typer.Context) -> None:
        argv = [*target.split(), *ctx.args]
        if "--help" in ctx.args:
            err_console.print(f"[dim]`nvh {name}` is now `nvh {target}`; showing its help.[/dim]")
        if translate is not None:
            argv = translate(argv)
            if argv is None:
                return
        app(argv)

    run_alias.__name__ = name
    run_alias.__doc__ = f"(alias) nvh {target}" + (f" — {note}" if note else "")
    app.command(
        name, hidden=True, add_help_option=False,
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )(run_alias)


def _provider_translate(name: str):
    """`nvh <provider>` with no question was the key-paste flow; otherwise `nvh ask -p <provider>`."""
    def translate(argv: list[str]) -> list[str] | None:
        rest = argv[3:]
        question = [
            tok for i, tok in enumerate(rest)
            if not tok.startswith("-") and (i == 0 or rest[i - 1] not in ("-m", "--model", "-s", "--system"))
        ]
        if question or "--help" in rest:
            return argv
        advisor_login(name, headless=False)
        return None
    return translate


for _adv_name in KNOWN_ADVISORS:
    if _adv_name in DEPRECATED_ALIASES:
        _alias(_adv_name, translate=_provider_translate(_adv_name), note=KNOWN_ADVISORS[_adv_name]["name"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async function from sync CLI context."""
    return asyncio.run(coro)


def _get_engine():
    from nvh.core.engine import Engine
    return Engine()


def _print_metadata(resp, show: bool = True):
    """Print response metadata (provider, model, tokens, cost, latency)."""
    if not show:
        return
    # Show failover on its own line if present
    if resp.fallback_from:
        console.print(f"\n[yellow]↪ Failover: {resp.fallback_from} → {resp.provider}[/yellow]")

    parts = []
    if resp.provider:
        parts.append(f"[bold]Provider:[/bold] [dim]{resp.provider}[/dim]")
    if resp.model:
        parts.append(f"[bold]Model:[/bold] [dim]{resp.model}[/dim]")
    if resp.usage.total_tokens:
        parts.append(f"[bold]Tokens:[/bold] [dim]{resp.usage.input_tokens} in / {resp.usage.output_tokens} out[/dim]")
    if resp.cost_usd:
        parts.append(f"[bold]Cost:[/bold] [dim]${resp.cost_usd:.4f}[/dim]")
    if resp.latency_ms:
        parts.append(f"[bold]Latency:[/bold] [dim]{resp.latency_ms}ms[/dim]")
    if resp.cache_hit:
        parts.append("[dim](cached)[/dim]")

    if parts:
        console.print(f"\n{' | '.join(parts)}")


def _format_output(content: str, fmt: str) -> None:
    """Print content in the requested format."""
    if fmt == "markdown":
        console.print(Markdown(content))
    elif fmt == "json":
        import json
        console.print_json(json.dumps({"content": content}))
    elif fmt == "raw":
        print(content, end="")
    else:
        console.print(content)


_TEMPLATE_MIGRATION_HINT = (
    "Prompt templates now live on agent profiles: add a `prompt_template:` field "
    "(use {{input}} for the prompt) to $NVH_HOME/agent-profiles/<name>.yaml, "
    "then run `nvh ask --template <name>`."
)


def _render_profile_template(
    name: str, prompt: str, variables: dict[str, str],
) -> tuple[str, str | None]:
    """Render ``prompt`` through agent profile ``name``'s ``prompt_template``.

    Returns ``(rendered_prompt, profile_system_prompt_or_None)``.
    """
    from nvh.integrations.wizard.profiles import get_profile

    profile = get_profile(name)
    if profile is None:
        raise ValueError(f"No agent profile named '{name}'. {_TEMPLATE_MIGRATION_HINT}")
    if not profile.prompt_template.strip():
        raise ValueError(f"Profile '{name}' has no prompt_template. {_TEMPLATE_MIGRATION_HINT}")
    return profile.render_prompt(prompt, variables), (profile.system_prompt.strip() or None)


# ~100k chars ≈ 25-30k tokens, safe for most model context windows.
_STDIN_MAX_CHARS = 100_000


def _read_stdin() -> str:
    """Piped stdin, capped at _STDIN_MAX_CHARS (the rest is drained and noted)."""
    if sys.stdin.isatty():
        return ""
    text = sys.stdin.read(_STDIN_MAX_CHARS)
    if sys.stdin.read(1):
        try:
            while sys.stdin.read(8192):
                pass
        except Exception:
            pass
        text += "\n\n[Content truncated — input exceeded limit]"
    return text


# ---------------------------------------------------------------------------
# nvh ask — the one query command. --focus/--fast/--local/--clipboard and
# stdin replace the pre-0.42 code/write/research/math/quick/safe/clip/pipe
# clones, which live on below as hidden aliases for one release.
# ---------------------------------------------------------------------------

# focus -> (system prompt, advisor preference order when -p is not given)
FOCUS_MODES: dict[str, tuple[str, list[str]]] = {
    "code": (
        "You are an expert software engineer. Provide clear, correct, well-structured code. "
        "When writing code, include brief explanations of key decisions. "
        "Prefer idiomatic solutions. Highlight any edge cases or caveats.",
        ["anthropic", "openai", "groq", "google", "deepseek"],
    ),
    "write": (
        "You are a skilled writer. Produce clear, engaging, well-structured text. "
        "Match the format to the request (email, essay, blog post, etc.).",
        ["anthropic", "openai", "google", "groq"],
    ),
    "research": (
        "You are a thorough research assistant. Synthesize information from multiple sources. "
        "Always cite your sources. Highlight areas of consensus and disagreement. "
        "Provide a balanced, well-structured summary.",
        ["perplexity", "anthropic", "openai", "google"],
    ),
    "math": (
        "You are an expert mathematician. Solve problems step by step, showing all work. "
        "Use clear notation. Verify your answer when possible. "
        "If there are multiple approaches, briefly mention alternatives after the main solution.",
        ["openai", "deepseek", "anthropic", "google", "groq"],
    ),
}
FAST_PROVIDERS = ["groq", "deepseek", "ollama"]
_JSON_ONLY_SYSTEM = (
    "You must respond with valid JSON only. No markdown, no explanation outside the JSON. "
    "Use an appropriate JSON structure for the request."
)


def _ask(
    prompt: str | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    system: str | None = None,
    output: str = "text",
    stream: bool = True,
    max_tokens: int | None = None,
    temperature: float | None = None,
    no_cache: bool = False,
    strategy: str = "best",
    continue_: bool = False,
    conversation: str | None = None,
    profile: str | None = None,
    verbose: bool = False,
    quiet: bool = False,
    privacy: bool = False,
    template: str | None = None,
    var: list[str] | None = None,
    file: str | None = None,
    knowledge: bool = False,
    prefer_nvidia: bool = False,
    escalate: bool = False,
    verify: bool = False,
    focus: str | None = None,
    fast: bool = False,
    local: bool = False,
    clipboard: bool = False,
    copy: bool = False,
) -> None:
    """Body of `nvh ask`; the hidden aliases call it with their fixed flags."""
    preferred: list[str] = []
    if focus:
        if focus not in FOCUS_MODES:
            console.print(f"[red]Unknown focus '{focus}'. Choose from: {', '.join(FOCUS_MODES)}[/red]")
            raise typer.Exit(1)
        focus_system, preferred = FOCUS_MODES[focus]
        system = f"{focus_system}\n\n{system}" if system else focus_system
    if fast:
        strategy = "cheapest"
        preferred = preferred or FAST_PROVIDERS
    if local:
        provider, privacy = "ollama", True

    # --template names an agent profile whose prompt_template wraps the prompt
    if template:
        template_vars: dict[str, str] = {}
        for item in (var or []):
            if "=" not in item:
                console.print(f"[red]Error: --var '{item}' must be in key=value format.[/red]")
                raise typer.Exit(1)
            k, _, v = item.partition("=")
            template_vars[k.strip()] = v.strip()
        try:
            prompt, template_system = _render_profile_template(
                template, prompt or "", template_vars,
            )
        except ValueError as e:
            console.print(f"[red]Template error: {e}[/red]")
            raise typer.Exit(1)
        if template_system and not system:
            system = template_system

    file_content = ""
    if file:
        file_path_obj = Path(file)
        if not file_path_obj.exists():
            console.print(f"[red]Error: File not found: {file}[/red]")
            raise typer.Exit(1)
        try:
            file_content = file_path_obj.read_text()
        except Exception as e:
            console.print(f"[red]Error reading file {file}: {e}[/red]")
            raise typer.Exit(1)

    clip_content = ""
    if clipboard:
        try:
            clip_content = _read_clipboard()
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        if not clip_content.strip():
            console.print("[yellow]Clipboard is empty.[/yellow]")
            raise typer.Exit(1)
        if not quiet:
            preview = clip_content[:80].replace("\n", " ") + ("..." if len(clip_content) > 80 else "")
            console.print(f"[dim]Clipboard ({len(clip_content)} chars): {preview}[/dim]\n")

    stdin_content = _read_stdin()
    if not prompt and not stdin_content and not file_content and not clip_content:
        console.print(
            "[red]Error: No prompt provided."
            " Pass a prompt, --file, --clipboard, or pipe input via stdin.[/red]"
        )
        raise typer.Exit(1)

    parts_to_join = []
    if prompt:
        parts_to_join.append(prompt)
    if file_content:
        parts_to_join.append(f"```\n{file_content}\n```")
    if clip_content:
        parts_to_join.append(clip_content)
    if stdin_content:
        parts_to_join.append(stdin_content)
    full_prompt = "\n\n".join(parts_to_join)

    # RAG: prepend chunks retrieved from the local index if requested
    if knowledge:
        from nvh.integrations.rag import ask as rag_ask
        from nvh.integrations.rag import format_context_block

        rag_result = _run(rag_ask(full_prompt))
        rag_context = (
            format_context_block(rag_result.get("chunks", []))
            if rag_result.get("ok") else ""
        )
        if rag_context:
            full_prompt = rag_context + "\n\n" + full_prompt
            if not quiet:
                console.print("[dim][rag context injected][/dim]")
        elif not rag_result.get("ok"):
            console.print(f"[dim][rag unavailable: {rag_result.get('error')}][/dim]")
        else:
            console.print(
                "[dim][rag: no relevant chunks — add documents with"
                " 'nvh rag add <file>' or 'nvh rag ingest <folder>'][/dim]"
            )

    def _copy_result(text: str) -> None:
        if not copy:
            return
        try:
            _write_clipboard(text)
            if not quiet:
                console.print("[dim]Answer copied to clipboard.[/dim]")
        except RuntimeError as e:
            console.print(f"[yellow]Could not copy to clipboard: {e}[/yellow]")

    async def _run_query():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config(profile=profile)
        # Apply --prefer-nvidia CLI flag (overrides config setting)
        if prefer_nvidia:
            config.defaults.prefer_nvidia = True
        engine = Engine(config=config)
        enabled = await engine.initialize()

        if local and not engine.registry.has("ollama"):
            console.print(
                "[red]--local needs Ollama.[/red] Install it from https://ollama.com,"
                " then run [bold]nvh models pull --recommended[/bold]."
            )
            raise typer.Exit(1)
        chosen = provider or next((p for p in preferred if p in enabled), None)

        if focus == "research" and provider is None and "perplexity" not in enabled:
            await _research_council(engine, full_prompt, system, output=output, quiet=quiet)
            return

        if local:
            console.print("[dim][local mode — Ollama only, nothing leaves this machine, nothing stored][/dim]")
        elif privacy:
            console.print("[dim][privacy mode — no data stored][/dim]")
        if focus and not quiet:
            console.print(f"[dim][focus: {focus} → {chosen or 'auto'}][/dim]")

        if verbose:
            from nvh.core.router import classify_task
            classification = classify_task(full_prompt)
            console.print(
                f"[dim]Task type: {classification.task_type.value}"
                f" (confidence: {classification.confidence:.2f})[/dim]"
            )

        if stream and output == "text":
            # Stream the response
            decision = engine.router.route(
                full_prompt,
                provider_override=chosen,
                model_override=model,
                strategy=strategy,
            )

            if verbose:
                console.print(
                    f"[dim]Routed to: {decision.provider}"
                    f"/{decision.model} ({decision.reason})[/dim]"
                )

            prov = engine.registry.get(decision.provider)
            pconfig = config.providers.get(decision.provider)
            pmodel = model or decision.model or (pconfig.default_model if pconfig else "")

            from nvh.providers.base import Message
            msgs = [Message(role="user", content=full_prompt)]
            if system:
                msgs.insert(0, Message(role="system", content=system))

            start = time.monotonic()
            accumulated = ""

            try:
                stream_iter = prov.stream(
                    messages=msgs,
                    model=pmodel or None,
                    temperature=temperature,
                    max_tokens=max_tokens or config.defaults.max_tokens,
                    system_prompt=system,
                )
                async for chunk in stream_iter:
                    if chunk.delta:
                        console.print(chunk.delta, end="")
                        accumulated += chunk.delta

                    if chunk.is_final and not quiet:
                        elapsed = int((time.monotonic() - start) * 1000)
                        console.print()  # newline
                        parts = [f"[bold]Provider:[/bold] [dim]{decision.provider}[/dim]", f"[bold]Model:[/bold] [dim]{pmodel}[/dim]"]
                        if chunk.usage:
                            parts.append(
                                f"[bold]Tokens:[/bold] [dim]{chunk.usage.input_tokens}"
                                f" in / {chunk.usage.output_tokens} out[/dim]"
                            )
                        if chunk.cost_usd:
                            parts.append(f"[bold]Cost:[/bold] [dim]${chunk.cost_usd:.4f}[/dim]")
                        parts.append(f"[bold]Latency:[/bold] [dim]{elapsed}ms[/dim]")
                        console.print(f"\n{' | '.join(parts)}")
            except Exception as e:
                err_console.print(f"\n{_format_cli_error(e)}")
                raise typer.Exit(1)
            _copy_result(accumulated)
        else:
            # Non-streaming
            try:
                with console.status(f"Querying {chosen or 'advisor'}...", spinner="dots"):
                    resp = await engine.query(
                        prompt=full_prompt,
                        provider=chosen,
                        model=model,
                        system_prompt=system,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stream=False,
                        use_cache=not no_cache and not privacy,
                        strategy=strategy,
                        conversation_id=None if privacy else conversation,
                        continue_last=False if privacy else continue_,
                        privacy=privacy,
                        escalate=escalate,
                        verify=verify,
                    )

                # Show escalation info before the response
                if resp.metadata.get("escalated") and not quiet:
                    meta = resp.metadata
                    console.print(
                        f"[dim][ask → {meta.get('initial_provider', '?')}"
                        f" → escalated to {resp.provider}/{resp.model}"
                        f" (confidence: {meta.get('initial_confidence', 0):.0%})][/dim]"
                    )

                _format_output(resp.content, output)
                _print_metadata(resp, show=not quiet)
                _copy_result(resp.content)

                # Show verification result after metadata
                veri = resp.metadata.get("verification")
                if veri and not quiet:
                    verdict = veri.get("verdict", "unverified")
                    v_conf = veri.get("confidence", 0)
                    verifier = veri.get("verifier", "unknown")
                    issues = veri.get("issues", [])
                    if verdict == "correct":
                        console.print(
                            f"[dim]Verified ✓ by {verifier}"
                            f" (confidence: {v_conf:.0f}/10)[/dim]"
                        )
                    elif verdict in ("partially_correct", "incorrect"):
                        issue_text = ", ".join(issues) if issues else "see correction"
                        console.print(
                            f"[dim]Verification ⚠ by {verifier}:"
                            f" {verdict} — \"{issue_text}\"[/dim]"
                        )
                    else:
                        console.print(
                            f"[dim]Verification: {verdict}[/dim]"
                        )
            except Exception as e:
                # stderr, so `... | nvh ask --raw` pipelines never see error text on stdout
                err_console.print(_format_cli_error(e))
                raise typer.Exit(1)

    _run(_run_query())


async def _research_council(engine, prompt: str, system: str | None, *, output: str, quiet: bool) -> None:
    """`--focus research` without Perplexity: the pre-0.42 `nvh research` path —
    an auto-agent council whose synthesis and agreement summary are printed."""
    if not quiet:
        console.print("[dim][research → no Perplexity advisor, synthesizing from multiple advisors][/dim]\n")
    try:
        with console.status("Convening research council...", spinner="dots"):
            result = await engine.run_council(
                prompt=prompt, system_prompt=system, auto_agents=True, synthesize=True,
            )
    except Exception as e:
        err_console.print(_format_cli_error(e))
        raise typer.Exit(1)
    if result.synthesis is None:
        for label, resp in result.member_responses.items():
            console.print(Panel(resp.content, title=label, border_style="blue"))
        return
    _format_output(result.synthesis.content, output)
    if quiet:
        return
    parts = [
        f"[bold]Agents:[/bold] [dim]{', '.join(result.agents_used) or 'auto'}[/dim]",
        f"[bold]Cost:[/bold] [dim]${result.total_cost_usd:.4f}[/dim]",
        f"[bold]Latency:[/bold] [dim]{result.total_latency_ms}ms[/dim]",
    ]
    if result.confidence_score is not None:
        confidence = f"[bold]Confidence:[/bold] [dim]{int(result.confidence_score * 100)}%[/dim]"
        if result.agreement_summary:
            confidence += f" — {result.agreement_summary}"
        parts.append(confidence)
    console.print(f"\n{' | '.join(parts)}")


@app.command(rich_help_panel="Query Modes")
def ask(
    prompt: str | None = typer.Argument(None, help="The prompt to send to the LLM"),
    provider: str | None = typer.Option(None, "-p", "--advisor", help="Advisor to use"),
    model: str | None = typer.Option(None, "-m", "--model", help="Model to use"),
    system: str | None = typer.Option(None, "-s", "--system", help="System prompt"),
    output: str = typer.Option(
        "text", "-o", "--output",
        help="Output format: text, json, markdown, raw",
    ),
    stream: bool = typer.Option(True, "--stream/--no-stream", help="Stream output"),
    max_tokens: int | None = typer.Option(None, "--max-tokens", help="Max output tokens"),
    temperature: float | None = typer.Option(None, "-t", "--temperature", help="Temperature"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass cache"),
    strategy: str = typer.Option(
        "best", "--strategy",
        help="Routing: best, cheapest, fastest, best-for-task",
    ),
    continue_: bool = typer.Option(False, "-c", "--continue", help="Continue last conversation"),
    conversation: str | None = typer.Option(
        None, "--conversation",
        help="Continue a specific conversation",
    ),
    profile: str | None = typer.Option(None, "--profile", help="Config profile to use"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Show routing details"),
    quiet: bool = typer.Option(False, "-q", "--quiet", help="Suppress metadata"),
    privacy: bool = typer.Option(
        False, "--privacy",
        help="Privacy mode: disable logging, caching,"
        " and conversation persistence",
    ),
    template: str | None = typer.Option(None, "--template", help="Prompt template name to use"),
    var: list[str] | None = typer.Option(
        None, "--var",
        help="Template variable as key=value (repeatable)",
    ),
    file: str | None = typer.Option(
        None, "-f", "--file",
        help="Include a file's contents in the prompt",
    ),
    output_json: bool = typer.Option(False, "--json", help="Shorthand for --output json"),
    output_raw: bool = typer.Option(
        False, "--raw",
        help="Shorthand for --output raw"
        " (no metadata, just the answer)",
    ),
    knowledge: bool = typer.Option(
        False, "--knowledge", "-k",
        help="Augment prompt with chunks retrieved from your local RAG index (nvh rag)",
    ),
    prefer_nvidia: bool = typer.Option(
        False, "--prefer-nvidia",
        help="Bias routing toward NVIDIA providers"
        " (ollama/Nemotron, NIM, Triton)",
    ),
    escalate: bool = typer.Option(
        False, "--escalate",
        help="Try cheap model first, escalate to premium if low confidence",
    ),
    verify: bool = typer.Option(
        False, "--verify",
        help="Cross-check response with a different model for accuracy",
    ),
    focus: str | None = typer.Option(
        None, "--focus", rich_help_panel="Modes",
        help="Focus mode: code, write, math, research (system prompt + advisor preference)",
    ),
    fast: bool = typer.Option(
        False, "--fast", rich_help_panel="Modes",
        help="Cheapest/fastest advisor (Groq > DeepSeek > Ollama), no frills",
    ),
    local: bool = typer.Option(
        False, "--local", rich_help_panel="Modes",
        help="Ollama only — nothing leaves this machine, nothing is stored",
    ),
    clipboard: bool = typer.Option(
        False, "--clipboard", rich_help_panel="Modes",
        help="Add the clipboard contents to the prompt",
    ),
    copy: bool = typer.Option(
        False, "--copy", rich_help_panel="Modes",
        help="Copy the answer to the clipboard",
    ),
):
    """Ask an advisor a question — the one query command.

    The prompt comes from the argument, --file, --clipboard, piped stdin, or
    any combination. --focus tunes the system prompt and advisor preference,
    --fast picks the cheapest advisor, --local keeps everything on Ollama.

    Examples:
        nvh ask "Explain the CAP theorem"
        nvh ask --focus code -f main.py "Fix the bug on line 42"
        git diff --staged | nvh ask --raw "Write a commit message"
        nvh ask --local "Review this NDA" -f contract.txt
        nvh ask --fast "What does HTTP 429 mean?"
    """
    if output_json:
        output = "json"
    elif output_raw:
        output = "raw"
        quiet = True
    _ask(
        prompt, provider=provider, model=model, system=system, output=output,
        stream=stream, max_tokens=max_tokens, temperature=temperature,
        no_cache=no_cache, strategy=strategy, continue_=continue_,
        conversation=conversation, profile=profile, verbose=verbose, quiet=quiet,
        privacy=privacy, template=template, var=var, file=file,
        knowledge=knowledge, prefer_nvidia=prefer_nvidia, escalate=escalate,
        verify=verify, focus=focus, fast=fast, local=local, clipboard=clipboard,
        copy=copy,
    )


# ---------------------------------------------------------------------------
# Clipboard helpers (used by `ask --clipboard/--copy` and the `clip` alias)
# ---------------------------------------------------------------------------

def _read_clipboard() -> str:
    """Read clipboard contents using platform-appropriate command."""
    import platform
    import subprocess

    system = platform.system()
    try:
        if system == "Darwin":
            result = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
            return result.stdout
        else:
            # Linux — try xclip, then xsel as fallback
            try:
                result = subprocess.run(
                    ["xclip", "-o", "-selection", "clipboard"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    return result.stdout
            except FileNotFoundError:
                pass
            result = subprocess.run(
                ["xsel", "--clipboard", "--output"],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout
    except Exception as e:
        raise RuntimeError(f"Could not read clipboard: {e}") from e


def _write_clipboard(text: str) -> None:
    """Write text to the clipboard using platform-appropriate command."""
    import platform
    import subprocess

    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["pbcopy"], input=text, text=True, timeout=5, check=True)
        else:
            try:
                subprocess.run(
                    ["xclip", "-selection", "clipboard"],
                    input=text, text=True, timeout=5, check=True,
                )
                return
            except FileNotFoundError:
                pass
            subprocess.run(
                ["xsel", "--clipboard", "--input"],
                input=text, text=True, timeout=5, check=True,
            )
    except Exception as e:
        raise RuntimeError(f"Could not write clipboard: {e}") from e


_CLIP_ACTIONS = {
    "ask": (
        "Answer any questions about the following content,"
        " or describe it if no question is obvious:"
    ),
    "explain": "Explain the following clearly and concisely:",
    "fix": (
        "Fix any bugs, errors, or issues in the following code. "
        "Return the corrected version with a brief explanation of what was changed:"
    ),
    "summarize": "Summarize the following in a few sentences:",
    "translate": "Translate the following text to English (or if already English, to Spanish):",
}


# ---------------------------------------------------------------------------
# Pre-0.42 query-mode spellings — hidden aliases of `nvh ask` for one release.
# The translators map the old flag spellings (`-a`, `--tone`, pipe's `--json`,
# clip's ACTION) onto ask's before the argv is re-parsed by `nvh ask`.
# ---------------------------------------------------------------------------

def _translate_code(argv: list[str]) -> list[str]:
    return ["-p" if tok == "-a" else tok for tok in argv]


def _translate_write(argv: list[str]) -> list[str]:
    tone = _pop_flag(argv, "--tone", value=True) or "professional"
    return [*argv, "-s", f"Write with a {tone} tone."]


def _translate_pipe(argv: list[str]) -> list[str]:
    # pipe's --json asked the model for JSON; ask's --json is an output format.
    argv = ["-p" if tok in ("-a", "--provider") else tok for tok in argv]
    if _pop_flag(argv, "--json"):
        argv += ["-s", _JSON_ONLY_SYSTEM]
    return argv


def _translate_clip(argv: list[str]) -> list[str]:
    """`nvh clip [ACTION] [-a ADVISOR] [-c]` -> `nvh ask --clipboard [-p ADVISOR] [--copy] "<prompt>"`."""
    head, rest = argv[:2], argv[2:]
    action, out = "ask", []
    while rest:
        tok = rest.pop(0)
        if tok == "-a":
            out += ["-p", *rest[:1]]
            rest = rest[1:]
        elif tok == "-c":
            out.append("--copy")
        elif tok.startswith("-"):
            out.append(tok)
        else:
            action = tok
    if action not in _CLIP_ACTIONS:
        console.print(f"[red]Unknown action '{action}'. Choose from: {', '.join(_CLIP_ACTIONS)}[/red]")
        raise typer.Exit(1)
    return [*head, *out, _CLIP_ACTIONS[action]]


_alias("code", translate=_translate_code)
_alias("write", translate=_translate_write)
_alias("research")
_alias("math")
_alias("quick")
_alias("safe")
_alias("pipe", translate=_translate_pipe)
_alias("clip", translate=_translate_clip)


# ---------------------------------------------------------------------------
# hive convene (hive mode)
# ---------------------------------------------------------------------------

@app.command("convene", rich_help_panel="Multi-Model")
def convene_cmd(
    prompt: str = typer.Argument(..., help="The prompt to send to the hive"),
    members: str | None = typer.Option(None, "--members", help="Comma-separated advisor list"),
    weights: str | None = typer.Option(
        None, "--weights",
        help="Advisor weights, e.g. openai=0.4,anthropic=0.6",
    ),
    strategy: str | None = typer.Option(
        None, "--strategy",
        help="Consensus: weighted_consensus, majority_vote, best_of",
    ),
    system: str | None = typer.Option(None, "-s", "--system", help="System prompt"),
    output: str = typer.Option("text", "-o", "--output", help="Output format: text, json, table"),
    max_tokens: int | None = typer.Option(None, "--max-tokens"),
    temperature: float | None = typer.Option(None, "-t", "--temperature"),
    no_synthesize: bool = typer.Option(
        False, "--no-synthesize",
        help="Skip synthesis, show raw responses",
    ),
    auto_agents: bool = typer.Option(
        False, "--auto-agents", "-a",
        help="Auto-generate expert personas based on query content",
    ),
    preset: str | None = typer.Option(
        None, "--cabinet",
        help="Agent cabinet: executive, engineering,"
        " security_review, code_review, product, product_resilience, data, full_board",
    ),
    num_agents: int | None = typer.Option(
        None, "--num-agents", "-n",
        help="Number of agent personas to generate",
    ),
    profile: str | None = typer.Option(None, "--profile"),
    quiet: bool = typer.Option(False, "-q", "--quiet"),
    privacy: bool = typer.Option(
        False, "--privacy",
        help="Privacy mode: disable logging, caching,"
        " and conversation persistence",
    ),
    output_raw: bool = typer.Option(
        False, "--raw",
        help="Output just the synthesis text, no panels",
    ),
):
    """Convene a hive session — query multiple LLMs and synthesize consensus.

    Use --auto-agents to auto-generate expert personas (e.g., Architect, Security Engineer)
    based on the query content. Each hive member adopts a unique expert perspective.

    Use --cabinet to pick a named group of experts (e.g., --cabinet executive for CEO/CFO/CTO/PM).
    """
    async def _run_council():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config(profile=profile)
        engine = Engine(config=config)
        await engine.initialize()

        if privacy:
            console.print("[dim][privacy mode — no data stored][/dim]")

        member_list = members.split(",") if members else None
        weight_dict = None
        if weights:
            weight_dict = {}
            for pair in weights.split(","):
                k, v = pair.split("=")
                weight_dict[k.strip()] = float(v.strip())

        # Show agent info if using auto-agents
        if auto_agents or preset:
            from nvh.core.agents import generate_agents, get_preset_agents
            if preset:
                personas = get_preset_agents(preset, prompt)
            else:
                personas = generate_agents(
                    prompt,
                    num_agents=num_agents or len(
                        member_list or engine.registry.list_enabled()
                    ),
                )

            console.print("[bold]Hive Mode[/bold] — auto-generated expert advisors:\n")
            for p in personas:
                console.print(f"  [bold cyan]{p.role}[/bold cyan] — {p.expertise}")
            console.print()
        else:
            member_count = len(
                member_list or engine.registry.list_enabled()
            )
            console.print(
                f"[bold]Hive Mode[/bold] — querying"
                f" {member_count} advisors...\n"
            )

        try:
            result = await engine.run_council(
                prompt=prompt,
                members=member_list,
                weights=weight_dict,
                strategy=strategy,
                system_prompt=system,
                temperature=temperature,
                max_tokens=max_tokens,
                synthesize=not no_synthesize,
                auto_agents=auto_agents,
                agent_preset=preset,
                num_agents=num_agents,
                privacy=privacy,
            )
        except Exception as e:
            console.print(_format_cli_error(e))
            raise typer.Exit(1)

        if output_raw:
            # Raw mode: output just the synthesis text (or member responses if no synthesis)
            if result.synthesis:
                print(result.synthesis.content, end="")
            else:
                for label, resp in result.member_responses.items():
                    print(f"--- {label} ---\n{resp.content}\n", end="")
            return

        if output == "json":
            import json
            data = {
                "member_responses": {
                    p: {
                        "content": r.content,
                        "model": r.model,
                        "cost_usd": str(r.cost_usd),
                        "latency_ms": r.latency_ms,
                    }
                    for p, r in result.member_responses.items()
                },
                "synthesis": {
                    "content": result.synthesis.content if result.synthesis else None,
                    "cost_usd": str(result.synthesis.cost_usd) if result.synthesis else "0",
                } if result.synthesis else None,
                "total_cost_usd": str(result.total_cost_usd),
                "total_latency_ms": result.total_latency_ms,
                "strategy": result.strategy,
                "quorum_met": result.quorum_met,
                "confidence_score": result.confidence_score,
                "agreement_summary": result.agreement_summary,
            }
            console.print_json(json.dumps(data, indent=2))
            return

        # Display member responses
        for label, resp in result.member_responses.items():
            persona = resp.metadata.get("persona", "")
            # Find matching member weight
            weight = 0.0
            for m in result.members:
                ml = f"{m.provider}:{m.persona}" if m.persona else m.provider
                if ml == label:
                    weight = m.weight
                    break

            if persona:
                header = (
                    f"{persona} ({resp.provider})"
                    f" [weight: {weight:.0%}]"
                    f"  {resp.latency_ms}ms"
                    f"  ${resp.cost_usd:.4f}"
                )
                console.print(Panel(resp.content, title=header, border_style="blue"))
            else:
                header = (
                    f"{label} [weight: {weight:.0%}]"
                    f"  {resp.latency_ms}ms"
                    f"  ${resp.cost_usd:.4f}"
                )
                console.print(Panel(resp.content, title=header, border_style="blue"))

        # Display failures
        for label, error in result.failed_members.items():
            if label != "_synthesis":
                console.print(Panel(
                    f"[red]{error}[/red]",
                    title=f"{label} (FAILED)",
                    border_style="red",
                ))

        # Display synthesis
        if result.synthesis:
            console.print()
            console.print(Panel(
                result.synthesis.content,
                title=f"SYNTHESIS ({result.strategy})",
                border_style="green",
            ))

        # Display confidence
        if result.confidence_score is not None:
            pct = int(result.confidence_score * 100)
            summary = result.agreement_summary or ""
            confidence_text = f"Confidence: {pct}%"
            if summary:
                confidence_text += f" — {summary}"
            console.print(f"\n[dim]{confidence_text}[/dim]")

        if not quiet:
            parts = [
                f"Advisors: {len(result.member_responses)}/{len(result.members)}",
                f"Total cost: ${result.total_cost_usd:.4f}",
                f"Total latency: {result.total_latency_ms}ms",
                f"Strategy: {result.strategy}",
                f"Quorum: {'met' if result.quorum_met else 'NOT MET'}",
            ]
            if result.agents_used:
                parts.append(f"Agents: {', '.join(result.agents_used)}")
            console.print(f"\n[dim]{' | '.join(parts)}[/dim]")

    _run(_run_council())


# ---------------------------------------------------------------------------
# hive poll
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Multi-Model")
def poll(
    prompt: str = typer.Argument(..., help="The prompt to poll across advisors"),
    providers: str | None = typer.Option(None, "--advisors", help="Comma-separated advisor list"),
    output: str = typer.Option("text", "-o", "--output", help="Output format: text, json, table"),
    system: str | None = typer.Option(None, "-s", "--system"),
    max_tokens: int | None = typer.Option(None, "--max-tokens"),
    temperature: float | None = typer.Option(None, "-t", "--temperature"),
    profile: str | None = typer.Option(None, "--profile"),
):
    """Poll multiple advisors and compare their responses side by side."""
    async def _run_compare():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config(profile=profile)
        engine = Engine(config=config)
        await engine.initialize()

        provider_list = providers.split(",") if providers else None

        console.print("[bold]Poll Mode[/bold] — querying advisors...\n")

        try:
            results = await engine.compare(
                prompt=prompt,
                providers=provider_list,
                system_prompt=system,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            console.print(_format_cli_error(e))
            raise typer.Exit(1)

        if output == "json":
            import json
            data = {
                p: {
                    "content": r.content,
                    "model": r.model,
                    "cost_usd": str(r.cost_usd),
                    "latency_ms": r.latency_ms,
                }
                for p, r in results.items()
            }
            console.print_json(json.dumps(data, indent=2))
            return

        if output == "table":
            table = Table(title="Advisor Comparison")
            table.add_column("Provider", style="bold")
            table.add_column("Model")
            table.add_column("Response", max_width=60)
            table.add_column("Tokens", justify="right")
            table.add_column("Cost", justify="right")
            table.add_column("Latency", justify="right")

            for pname, resp in results.items():
                preview = resp.content[:200] + ("..." if len(resp.content) > 200 else "")
                table.add_row(
                    pname,
                    resp.model,
                    preview,
                    str(resp.usage.total_tokens),
                    f"${resp.cost_usd:.4f}",
                    f"{resp.latency_ms}ms",
                )
            console.print(table)
            return

        for pname, resp in results.items():
            header = f"{pname}/{resp.model}  {resp.latency_ms}ms  ${resp.cost_usd:.4f}"
            console.print(Panel(resp.content, title=header, border_style="cyan"))

    _run(_run_compare())


# ---------------------------------------------------------------------------
# nvh batch — run multiple prompts from a file and collect structured results
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Multi-Model")
def batch(
    file: str = typer.Argument(..., help="File containing prompts (txt, json, or yaml)"),
    output: str | None = typer.Option(
        None, "-o", "--output", help="Output file path (.json, .csv, or .txt)",
    ),
    provider: str | None = typer.Option(None, "-p", "--provider", help="Force a specific provider"),
    model: str | None = typer.Option(None, "-m", "--model", help="Force a specific model"),
    parallel: int = typer.Option(1, "--parallel", help="Number of prompts to run concurrently"),
    system: str | None = typer.Option(None, "-s", "--system", help="System prompt for all queries"),
    profile: str | None = typer.Option(None, "--profile", help="Config profile to use"),
    fmt: str = typer.Option("text", "--format", help="Console output format: text, json, csv"),
):
    """Batch mode — run multiple prompts from a file and collect structured results.

    The prompts file can be:
    - Plain text: one prompt per line
    - JSON: a list of objects with at least a "prompt" key
      (optional per-item keys: "provider", "model", "system")

    File format is auto-detected by extension (.json for JSON, everything else
    treated as plain text, one prompt per line).

    Examples:
        nvh batch prompts.txt
        nvh batch prompts.json -o results.json
        nvh batch prompts.txt -o results.csv --provider groq
        nvh batch prompts.txt --parallel 3
    """
    import json as json_mod
    import time

    file_path = Path(file)
    if not file_path.exists():
        console.print(f"[red]Error: File not found: {file}[/red]")
        raise typer.Exit(1)

    # --- Parse prompts file ---
    raw = file_path.read_text().strip()
    prompts: list[dict] = []

    if file_path.suffix.lower() == ".json":
        try:
            data = json_mod.loads(raw)
        except json_mod.JSONDecodeError as exc:
            console.print(f"[red]Error: Invalid JSON in {file}: {exc}[/red]")
            raise typer.Exit(1)
        if not isinstance(data, list):
            console.print("[red]Error: JSON file must contain a list of prompt objects.[/red]")
            raise typer.Exit(1)
        for idx, item in enumerate(data):
            if isinstance(item, str):
                prompts.append({"prompt": item})
            elif isinstance(item, dict) and "prompt" in item:
                prompts.append(item)
            else:
                console.print(
                    f"[red]Error: Item {idx} in JSON must be a string or "
                    f"object with a 'prompt' key.[/red]",
                )
                raise typer.Exit(1)
    elif file_path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
            data = yaml.safe_load(raw)
        except ImportError:
            console.print(
                "[red]Error: PyYAML is required for YAML files. "
                "Install it with: pip install pyyaml[/red]",
            )
            raise typer.Exit(1)
        except Exception as exc:
            console.print(f"[red]Error: Invalid YAML in {file}: {exc}[/red]")
            raise typer.Exit(1)
        if not isinstance(data, list):
            console.print("[red]Error: YAML file must contain a list of prompt objects.[/red]")
            raise typer.Exit(1)
        for idx, item in enumerate(data):
            if isinstance(item, str):
                prompts.append({"prompt": item})
            elif isinstance(item, dict) and "prompt" in item:
                prompts.append(item)
            else:
                console.print(
                    f"[red]Error: Item {idx} in YAML must be a string or "
                    f"mapping with a 'prompt' key.[/red]",
                )
                raise typer.Exit(1)
    else:
        # Plain text — one prompt per line
        for line in raw.splitlines():
            line = line.strip()
            if line:
                prompts.append({"prompt": line})

    if not prompts:
        console.print("[red]Error: No prompts found in file.[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Batch Mode[/bold] — {len(prompts)} prompt(s), parallelism={parallel}\n")

    # --- Run prompts ---
    async def _run_batch():
        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config(profile=profile)
        engine = Engine(config=config)
        await engine.initialize()

        results: list[dict] = []
        semaphore = asyncio.Semaphore(parallel)

        async def _process_one(idx: int, item: dict, progress, task_id) -> dict:
            async with semaphore:
                p = item["prompt"]
                p_provider = item.get("provider") or provider
                p_model = item.get("model") or model
                p_system = item.get("system") or system

                start_t = time.monotonic()
                try:
                    resp = await engine.query(
                        prompt=p,
                        provider=p_provider,
                        model=p_model,
                        system_prompt=p_system,
                        stream=False,
                    )
                    elapsed = int((time.monotonic() - start_t) * 1000)
                    result = {
                        "index": idx,
                        "prompt": p,
                        "content": resp.content,
                        "provider": resp.provider or p_provider or "",
                        "model": resp.model or p_model or "",
                        "latency_ms": resp.latency_ms or elapsed,
                        "cost_usd": str(resp.cost_usd or "0"),
                        "tokens_in": resp.usage.input_tokens if resp.usage else 0,
                        "tokens_out": resp.usage.output_tokens if resp.usage else 0,
                        "error": None,
                    }
                except Exception as exc:
                    elapsed = int((time.monotonic() - start_t) * 1000)
                    result = {
                        "index": idx,
                        "prompt": p,
                        "content": "",
                        "provider": p_provider or "",
                        "model": p_model or "",
                        "latency_ms": elapsed,
                        "cost_usd": "0",
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "error": str(exc),
                    }
                progress.advance(task_id)
                return result

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("Running prompts…", total=len(prompts))
            tasks = [
                _process_one(i, item, progress, task_id)
                for i, item in enumerate(prompts)
            ]
            results = await asyncio.gather(*tasks)

        # Sort by original index
        results = sorted(results, key=lambda r: r["index"])

        # --- Save / display results ---
        errors = [r for r in results if r["error"]]
        successes = [r for r in results if not r["error"]]
        total_cost = sum(Decimal(r["cost_usd"]) for r in results)

        # Determine output format from --output file extension or --format
        out_fmt = fmt
        if output:
            out_path = Path(output)
            ext = out_path.suffix.lower()
            if ext == ".json":
                out_fmt = "json"
            elif ext == ".csv":
                out_fmt = "csv"
            else:
                out_fmt = "text"

        # Build output data
        if out_fmt == "json":
            json_str = json_mod.dumps(results, indent=2, ensure_ascii=False)
            if output:
                Path(output).write_text(json_str)
                console.print(f"\n[green]Results saved to {output}[/green]")
            else:
                console.print_json(json_str)

        elif out_fmt == "csv":
            import csv
            import io

            fieldnames = [
                "index", "prompt", "content", "provider", "model",
                "latency_ms", "cost_usd", "tokens_in", "tokens_out", "error",
            ]
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
            csv_str = buf.getvalue()

            if output:
                Path(output).write_text(csv_str)
                console.print(f"\n[green]Results saved to {output}[/green]")
            else:
                console.print(csv_str)

        else:
            # Text output
            for r in results:
                if r["error"]:
                    console.print(Panel(
                        f"[red]Error: {r['error']}[/red]",
                        title=f"[{r['index']}] {r['prompt'][:60]}",
                        border_style="red",
                    ))
                else:
                    header = (
                        f"[{r['index']}] {r['provider']}/{r['model']}  "
                        f"{r['latency_ms']}ms  ${r['cost_usd']}"
                    )
                    console.print(Panel(
                        r["content"],
                        title=header,
                        subtitle=r["prompt"][:80],
                        border_style="cyan",
                    ))

            if output:
                lines = []
                for r in results:
                    lines.append(f"--- [{r['index']}] {r['prompt'][:80]} ---")
                    if r["error"]:
                        lines.append(f"ERROR: {r['error']}")
                    else:
                        lines.append(r["content"])
                    lines.append("")
                Path(output).write_text("\n".join(lines))
                console.print(f"\n[green]Results saved to {output}[/green]")

        # Summary
        console.print(
            f"\n[bold]Batch complete:[/bold] {len(successes)} succeeded, "
            f"{len(errors)} failed, total cost ${total_cost:.4f}",
        )

    _run(_run_batch())


# ---------------------------------------------------------------------------
# nvh throwdown — two-pass deep analysis with all APIs and agents
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Multi-Model")
def throwdown(
    prompt: str = typer.Argument(..., help="The question for the throwdown"),
    cabinet: str | None = typer.Option(None, "--cabinet", "-c", help="Agent cabinet to use"),
    num_agents: int | None = typer.Option(None, "-n", "--num-agents", help="Number of agents"),
    profile: str | None = typer.Option(None, "--profile"),
    quiet: bool = typer.Option(False, "-q", "--quiet"),
    quick: bool = typer.Option(
        False, "--quick",
        help="Single pass instead of two (cheaper throwdown)",
    ),
):
    """Throwdown mode — two-pass deep analysis with all advisors and agents.

    Pass 1: All advisors respond independently with auto-generated expert agents.
    Pass 2: The responses from Pass 1 are fed back for critique and refinement.
    Final: A meta-synthesis combines both passes into a definitive answer.

    Use --quick for a single-pass version that skips Pass 2 (faster and cheaper).

    This is the most thorough (and most expensive) analysis mode.
    """
    async def _run_throwdown():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config(profile=profile)
        engine = Engine(config=config)
        await engine.initialize()

        console.print("[bold red]THROWDOWN MODE[/bold red] — two-pass deep analysis\n")

        # Pass 1: Convene with auto-agents
        console.print("[bold]Pass 1:[/bold] Initial council with expert agents...\n")
        try:
            pass1 = await engine.run_council(
                prompt=prompt,
                auto_agents=True,
                agent_preset=cabinet,
                num_agents=num_agents,
                synthesize=True,
            )
        except Exception as e:
            console.print(f"[red]Pass 1 failed: {e}[/red]")
            raise typer.Exit(1)

        if not quiet:
            for label, resp in pass1.member_responses.items():
                persona = resp.metadata.get("persona", label)
                console.print(f"  [dim]{persona}: {resp.content[:100]}...[/dim]")

        if pass1.synthesis:
            console.print(Panel(
                pass1.synthesis.content,
                title="Pass 1 Synthesis",
                border_style="blue",
            ))

        if quick:
            # Quick mode: single pass, just show Pass 1 synthesis
            console.print("\n[dim](--quick: skipping Pass 2)[/dim]")
            final_content = pass1.synthesis.content if pass1.synthesis else ""
            if final_content:
                console.print(Panel(
                    final_content,
                    title="[bold green]THROWDOWN RESULT (quick)[/bold green]",
                    border_style="green",
                ))
            total_cost = pass1.total_cost_usd
            total_time = pass1.total_latency_ms
            console.print(f"\n[dim]Throwdown complete (quick) | Total cost: ${total_cost:.4f} | "
                         f"Total time: {total_time}ms | "
                         f"Agents used: {', '.join(pass1.agents_used)}[/dim]")
        else:
            # Pass 2: Feed Pass 1 results back for critique
            console.print("\n[bold]Pass 2:[/bold] Critique and refinement...\n")

            critique_prompt = (
                f"Original question: {prompt}\n\n"
                f"A council of AI experts produced this initial analysis:\n\n"
                f"{pass1.synthesis.content if pass1.synthesis else 'No synthesis available'}\n\n"
                f"Individual expert responses were:\n"
            )
            for label, resp in pass1.member_responses.items():
                persona = resp.metadata.get("persona", label)
                critique_prompt += f"\n--- {persona} ---\n{resp.content[:500]}\n"

            critique_prompt += (
                "\n\nNow critique this analysis. What did the experts miss? "
                "What assumptions are wrong? What alternative perspectives weren't considered? "
                "Provide a refined, improved answer that addresses these gaps."
            )

            try:
                pass2 = await engine.run_council(
                    prompt=critique_prompt,
                    auto_agents=True,
                    agent_preset=cabinet,
                    num_agents=num_agents,
                    synthesize=True,
                )
            except Exception as e:
                console.print(f"[red]Pass 2 failed: {e}[/red]")
                # Still show Pass 1 results
                raise typer.Exit(1)

            if pass2.synthesis:
                console.print(Panel(
                    pass2.synthesis.content,
                    title="Pass 2 — Refined Analysis",
                    border_style="yellow",
                ))

            # Final meta-synthesis
            console.print("\n[bold]Final Synthesis:[/bold] Combining both passes...\n")

            # Use the best available advisor for the final synthesis
            final_prompt = (
                f"Original question: {prompt}\n\n"
                f"Pass 1 analysis:\n{pass1.synthesis.content if pass1.synthesis else ''}\n\n"
                "Pass 2 critique and refinement:\n"
                f"{pass2.synthesis.content if pass2.synthesis else ''}\n\n"
                "Produce a definitive final answer that"
                " integrates the best insights from both passes. "
                f"Be concise, actionable, and highlight the key decision points."
            )

            try:
                final = await engine.query(prompt=final_prompt, stream=False)
                console.print(Panel(
                    final.content,
                    title="[bold green]THROWDOWN RESULT[/bold green]",
                    border_style="green",
                ))
            except Exception as e:
                console.print(f"[red]Final synthesis failed: {e}[/red]")

            # Stats
            total_cost = (
                pass1.total_cost_usd
                + pass2.total_cost_usd
                + (final.cost_usd if final else 0)
            )
            total_time = (
                pass1.total_latency_ms
                + pass2.total_latency_ms
                + (final.latency_ms if final else 0)
            )
            console.print(f"\n[dim]Throwdown complete | Total cost: ${total_cost:.4f} | "
                         f"Total time: {total_time}ms | "
                         f"Agents used: {', '.join(pass1.agents_used)}[/dim]")

    _run(_run_throwdown())


# ---------------------------------------------------------------------------
# nvh status --routing — routing explainability for the last query
# ---------------------------------------------------------------------------


def _status_routing():
    """Task classification, provider scores and why the chosen provider won."""
    import json as _json
    from pathlib import Path as _Path

    why_path = _Path.home() / ".hive" / "last_query.json"
    if not why_path.exists():
        console.print(
            "[dim]No query to explain yet."
            " Run a query first, then nvh status --routing.[/dim]",
        )
        return

    try:
        ctx = _json.loads(why_path.read_text())
    except Exception:
        console.print("[red]Could not read last query context.[/red]")
        return

    console.print()
    console.print("[bold]Routing Explanation[/bold]")
    console.print()

    # Query
    prompt = ctx.get("prompt", "?")
    console.print(f'  [dim]Query:[/dim] "{prompt}"')
    console.print()

    # Task classification
    task = ctx.get("task_type", "?")
    conf = ctx.get("classification_confidence", 0)
    console.print(
        f"  [bold]Task:[/bold] {task}"
        f" (confidence: {conf:.0%})",
    )
    console.print()

    # Decision
    provider = ctx.get("provider", "?")
    model = ctx.get("model", "?")
    reason = ctx.get("reason", "")
    console.print(
        f"  [bold]Routed to:[/bold]"
        f" [green]{provider}/{model}[/green]",
    )
    if reason:
        console.print(f"  [dim]Reason: {reason}[/dim]")
    console.print()

    # Scores breakdown
    scores = ctx.get("scores", {})
    if scores:
        console.print("  [bold]Provider Scores:[/bold]")
        table = Table(
            show_header=True,
            header_style="bold cyan",
            pad_edge=False,
            box=None,
        )
        table.add_column("Signal", style="dim")
        table.add_column("Score", justify="right")

        for signal in [
            "capability", "cost", "latency", "health",
            "composite",
        ]:
            val = scores.get(signal)
            if val is not None:
                style = ""
                if signal == "composite":
                    style = "bold green"
                table.add_row(
                    signal, f"{val:.3f}", style=style,
                )

        console.print(table)
        console.print()

    # Outcome
    latency = ctx.get("latency_ms", 0)
    cost = ctx.get("cost_usd", "0")
    tokens = ctx.get("tokens", {})
    console.print("  [bold]Outcome:[/bold]")
    console.print(
        f"    Latency: {latency}ms"
        f" | Cost: ${cost}"
        f" | Tokens: {tokens.get('input', 0)}"
        f"/{tokens.get('output', 0)}",
    )

    # Extras
    if ctx.get("fallback_from"):
        console.print(
            f"    [yellow]Fallback:"
            f" {ctx['fallback_from']}"
            f" → {provider}[/yellow]",
        )
    if ctx.get("cache_hit"):
        console.print("    [green]Cache hit[/green]")
    if ctx.get("escalated"):
        console.print(
            "    [yellow]Escalated from"
            " cheaper provider[/yellow]",
        )
    if ctx.get("verification"):
        v = ctx["verification"]
        verdict = v.get("verdict", "?")
        vconf = v.get("confidence", 0)
        verifier = v.get("verifier", "?")
        console.print(
            f"    Verified by {verifier}:"
            f" {verdict} ({vconf}/10)",
        )

    ts = ctx.get("timestamp", "")
    if ts:
        console.print(f"\n  [dim]{ts}[/dim]")
    console.print()


# ---------------------------------------------------------------------------
# nvh drift — model quality drift detection
# ---------------------------------------------------------------------------


@app.command(rich_help_panel="Admin")
def drift(
    reroute: bool = typer.Option(False, "--reroute", help="Auto-deprioritize degraded providers"),
):
    """Detect provider quality drift and optionally reroute.

    Compares recent query scores to historical averages.
    Alerts when a provider's quality drops >20%.

    Examples:
        nvh drift
        nvh drift --reroute
    """
    async def _run_drift():
        from nvh.core.drift_detector import format_drift_alerts
        from nvh.core.engine import Engine

        engine = Engine()
        await engine.initialize()

        alerts = engine.check_drift()
        output = format_drift_alerts(alerts)
        if not alerts:
            console.print(f"[green]{output}[/green]")
        else:
            console.print(f"[yellow]{output}[/yellow]")

        if reroute and alerts:
            actions = engine.auto_reroute()
            console.print()
            console.print("[bold]Reroute actions:[/bold]")
            for action in actions:
                console.print(f"  {action}")

    _run(_run_drift())


# ---------------------------------------------------------------------------
# nvh routing-stats — learned routing intelligence dashboard
# ---------------------------------------------------------------------------


@app.command(rich_help_panel="Admin")
def routing_stats(
    provider: str = typer.Option(None, "--provider", "-p", help="Filter by provider name"),
    task: str = typer.Option(None, "--task", "-t", help="Filter by task type"),
    reset: bool = typer.Option(False, "--reset", help="Wipe all learned scores"),
):
    """Routing intelligence dashboard — learned vs static capability scores.

    Shows how the adaptive learning engine has adjusted provider scores
    based on real routing outcomes. Use --reset to start fresh.

    Examples:
        nvh routing-stats
        nvh routing-stats --provider groq
        nvh routing-stats --task code_gen
        nvh routing-stats --reset
    """
    async def _run_routing_stats():
        from nvh.storage import repository as repo

        if reset:
            confirm = typer.confirm(
                "This will delete all learned routing scores. Continue?"
            )
            if not confirm:
                console.print("[dim]Aborted.[/dim]")
                return
            deleted = await repo.reset_learned_scores()
            console.print(f"[green]Cleared {deleted} learned score(s).[/green]")
            return

        stats = await repo.get_routing_stats(provider=provider, task_type=task)
        total_obs = await repo.get_outcome_count()

        # Determine unique providers in the stats
        providers_seen = {s["provider"] for s in stats}

        console.print()
        console.print(
            "[bold cyan]Routing Intelligence Dashboard[/bold cyan]"
        )
        console.print()

        status_label = "[green]ACTIVE[/green]" if total_obs > 0 else "[dim]INACTIVE[/dim]"
        console.print(
            f"  Learning: {status_label}"
            f" ({total_obs} observations across"
            f" {len(providers_seen)} providers)"
        )
        console.print()

        if not stats:
            console.print(
                "  [dim]No learned scores yet. Route some queries"
                " to start learning![/dim]"
            )
            return

        table = Table(
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Provider", style="bold")
        table.add_column("Task Type")
        table.add_column("Static", justify="right")
        table.add_column("Learned", justify="right")
        table.add_column("Samples", justify="right")
        table.add_column("Delta", justify="right")

        # Try to load static scores for comparison
        static_scores: dict[tuple[str, str], float] = {}
        try:
            from nvh.config.settings import load_config
            from nvh.core.engine import Engine

            config = load_config()
            engine = Engine(config=config)
            await engine.initialize()
            registry = engine.registry
            for s in stats:
                key = (s["provider"], s["task_type"])
                try:
                    caps = registry.get_capabilities(s["provider"])
                    if caps and s["task_type"] in caps:
                        static_scores[key] = float(caps[s["task_type"]])
                except Exception:
                    pass
        except Exception:
            pass

        for s in stats:
            key = (s["provider"], s["task_type"])
            static = static_scores.get(key)
            learned = s["learned_capability"]

            static_str = f"{static:.2f}" if static is not None else "[dim]—[/dim]"
            learned_str = f"{learned:.2f}"

            if static is not None:
                delta = learned - static
                if delta > 0:
                    delta_str = f"[green]+{delta:.2f}[/green]"
                elif delta < 0:
                    delta_str = f"[red]{delta:.2f}[/red]"
                else:
                    delta_str = f"{delta:.2f}"
            else:
                delta_str = "[dim]—[/dim]"

            table.add_row(
                s["provider"],
                s["task_type"],
                static_str,
                learned_str,
                str(s["sample_count"]),
                delta_str,
            )

        console.print(table)
        console.print()

    _run(_run_routing_stats())


# ---------------------------------------------------------------------------
# nvh history
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Admin")
def history(
    limit: int = typer.Option(10, "--limit", "-n", help="Number of queries to show"),
    provider: str | None = typer.Option(None, "--provider", "-p", help="Filter by provider"),
):
    """Show recent queries with routing decisions and cost."""

    def _relative_time(dt) -> str:
        """Format a datetime as a human-friendly relative string."""
        from datetime import UTC, datetime

        if dt is None:
            return "?"
        now = datetime.now(UTC)
        # Ensure dt is timezone-aware
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        delta = now - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} min ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} hr ago"
        days = hours // 24
        return f"{days}d ago"

    async def _run_history():
        from nvh.storage.repository import get_recent_queries, init_db

        await init_db()
        entries = await get_recent_queries(limit=limit, provider=provider)

        if not entries:
            console.print("[dim]No queries found.[/dim]")
            return

        table = Table(
            title="Recent Queries",
            show_lines=False,
            padding=(0, 1),
        )
        table.add_column("#", style="dim", width=4)
        table.add_column("Prompt", max_width=40, no_wrap=True)
        table.add_column("Provider", style="green")
        table.add_column("Cost", justify="right")
        table.add_column("Latency", justify="right")
        table.add_column("When", style="dim")

        for i, entry in enumerate(entries, 1):
            prompt = entry["prompt"] or f"[{entry['mode']}]"
            if len(prompt) > 38:
                prompt = prompt[:35] + "..."

            cost = f"${entry['cost_usd']:.4f}" if entry["cost_usd"] else "$0.0000"
            latency = f"{entry['latency_ms']}ms" if entry["latency_ms"] else "—"
            when = _relative_time(entry["created_at"])

            table.add_row(
                str(i),
                f'"{prompt}"',
                entry["provider"],
                cost,
                latency,
                when,
            )

        console.print(table)

    _run(_run_history())


# ---------------------------------------------------------------------------
# nvh status — every diagnostic tier over the shared checks registry in
# nvh.integrations.diagnostics.checks. The pre-0.42 health / doctor / test /
# debug / selfcheck / why verbs are hidden aliases of one tier each.
# ---------------------------------------------------------------------------

def _json_default(obj: Any) -> str:
    """json.dumps fallback: check data carries Decimal budgets, Paths and timestamps."""
    if isinstance(obj, (Decimal, Path, datetime)):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


_STATUS_ICONS = {
    "pass": "[green]✓[/green]",
    "warn": "[yellow]![/yellow]",
    "fail": "[red]✗[/red]",
    "skip": "[dim]-[/dim]",
    "info": "[dim]·[/dim]",
}
_STATUS_LABELS = {
    "pass": "[green]PASS[/green]",
    "warn": "[yellow]WARN[/yellow]",
    "fail": "[red]FAIL[/red]",
    "skip": "[dim]SKIP[/dim]",
    "info": "[dim]INFO[/dim]",
}


@app.command(rich_help_panel="Admin")
def status(
    providers: bool = typer.Option(
        False, "--providers", rich_help_panel="Tiers",
        help="Advisor health, scores and the failover chain",
    ),
    deep: bool = typer.Option(
        False, "--deep", rich_help_panel="Tiers",
        help="Full diagnostic: config, keys, advisors, Ollama, GPU, disk, environment",
    ),
    smoke: bool = typer.Option(
        False, "--smoke", rich_help_panel="Tiers",
        help="Offline smoke test of the workspace (storage, Ollama, WebUI runtime, packs)",
    ),
    report: bool = typer.Option(
        False, "--report", rich_help_panel="Tiers",
        help="Write a redacted JSON support bundle (deep checks + smoke + snapshot)",
    ),
    routing: bool = typer.Option(
        False, "--routing", rich_help_panel="Tiers",
        help="Explain how the last query was routed",
    ),
    json_output: bool = typer.Option(False, "--json", help="JSON on stdout (glance, --providers, --deep, --smoke)"),
    fix: bool = typer.Option(False, "--fix", help="--deep: offer to restart Ollama / pull missing models"),
    storage_only: bool = typer.Option(False, "--storage", help="--deep: only the persistent-storage preflight"),
    home_dir: str | None = typer.Option(None, "--home-dir", help="NVH_HOME to check (default: the active one)"),
    min_free_gb: float = typer.Option(200.0, "--min-free-gb", help="Free space expected for local models and ComfyUI"),
    imports: bool = typer.Option(False, "--imports", help="--smoke/--report: also import every core module"),
    live: bool = typer.Option(False, "--live", help="--report: include one live Wizard round-trip"),
    strict: bool = typer.Option(False, "--strict", help="--smoke/--report: warnings exit 1 too"),
    output: str | None = typer.Option(None, "-o", "--output", help="--report: bundle path (default $NVH_HOME/support/)"),
    send: bool = typer.Option(False, "--send", help="--report: copy the bundle to the clipboard"),
    nvidia_report: bool = typer.Option(False, "--nvidia-report", help="--report: also run nvidia-bug-report.sh"),
    # Hidden: the selfcheck alias's --query / --quiet land here.
    live_prompt: str = typer.Option("Say hello in one sentence", "--live-prompt", hidden=True),
    quiet: bool = typer.Option(False, "--quiet", hidden=True),
):
    """System status — one command, five tiers.

    With no tier: GPU, local models, advisors, budget and the service
    pipeline at a glance. --providers, --deep, --smoke, --report and
    --routing go deeper; every tier reads the same checks registry.

    Examples:
        nvh status                       glance
        nvh status --deep --fix          diagnose, then repair Ollama / models
        nvh status --smoke --json        CI gate for the local workspace
        nvh status --report --live       support bundle with a live Wizard turn
    """
    tiers = {"providers": providers, "deep": deep, "smoke": smoke, "report": report, "routing": routing}
    chosen = [name for name, on in tiers.items() if on]
    if storage_only and not chosen:
        chosen = ["deep"]
    if len(chosen) > 1:
        console.print(f"[red]Pick one tier: {' / '.join('--' + t for t in chosen)}[/red]")
        raise typer.Exit(2)
    _run_status(
        chosen[0] if chosen else None,
        json_output=json_output, fix=fix, storage_only=storage_only,
        home_dir=home_dir, min_free_gb=min_free_gb, imports=imports, live=live,
        live_prompt=live_prompt, strict=strict, output=output, send=send,
        nvidia_report=nvidia_report, quiet=quiet,
    )


def _run_status(
    tier: str | None,
    *,
    json_output: bool = False,
    fix: bool = False,
    storage_only: bool = False,
    home_dir: str | None = None,
    min_free_gb: float = 200.0,
    imports: bool = False,
    live: bool = False,
    live_prompt: str = "Say hello in one sentence",
    strict: bool = False,
    output: str | None = None,
    send: bool = False,
    nvidia_report: bool = False,
    quiet: bool = False,
) -> None:
    from nvh.integrations.diagnostics import checks as diag

    ctx = diag.CheckContext(home_dir=home_dir, min_free_gb=min_free_gb)
    if tier == "routing":
        _status_routing()
    elif tier == "smoke":
        _status_smoke(ctx, imports=imports, json_output=json_output, strict=strict)
    elif tier == "report":
        _status_report(
            ctx, imports=imports, live=live, live_prompt=live_prompt, strict=strict,
            output=output, send=send, nvidia_report=nvidia_report, quiet=quiet,
        )
    elif tier == "deep":
        _status_deep(ctx, json_output=json_output, fix=fix, storage_only=storage_only)
    elif tier == "providers":
        _status_providers(ctx, json_output=json_output)
    else:
        _status_glance(ctx, json_output=json_output)


def _status_glance(ctx, *, json_output: bool = False) -> None:
    import json as _json

    from rich.rule import Rule

    from nvh.integrations.diagnostics import checks as diag

    results = _run(diag.run_checks(diag.GLANCE, ctx))
    if json_output:
        print(_json.dumps(diag.summarize(results), indent=2, default=_json_default))
        return
    by_id: dict[str, list] = {}
    for r in results:
        by_id.setdefault(r.id, []).append(r)

    def first(check_id: str):
        rows = by_id.get(check_id)
        return rows[0] if rows else None

    console.print(f"[bold]NVHive v{__version__}[/bold]")
    console.print(Rule(style="dim"))

    gpu = first("gpu")
    gpus = gpu.data.get("gpus", []) if gpu else []
    gpu_line = (
        " | ".join(f"{g['name']} ({g['vram_gb']:.0f} GB) — {g['utilization_pct']}% utilized" for g in gpus)
        if gpus else (gpu.detail if gpu else "unavailable")
    )
    console.print(f"[bold]GPU:[/bold]      {gpu_line}")

    cloud = first("cloud_session")
    if cloud and cloud.data.get("cloud"):
        console.print(f"  [bold green]Cloud:[/bold green]     {cloud.data['summary']}")

    ollama = first("ollama")
    models = ollama.data.get("models", []) if ollama else []
    if models:
        models_line = ", ".join(f"{m} (loaded)" for m in models[:3])
        if len(models) > 3:
            models_line += f" +{len(models) - 3} more"
    elif ollama and ollama.status == "pass":
        models_line = "none loaded (run: nvh models pull --recommended)"
    else:
        models_line = "Ollama not reachable"
    console.print(f"[bold]Models:[/bold]   {models_line}")

    health = [r for r in by_id.get("provider_health", []) if r.data.get("provider")]
    if health:
        marks = ", ".join(
            f"{r.data['provider']} {'[green]✓[/green]' if r.data.get('healthy') else '[red]✗[/red]'}"
            for r in health
        )
        online = sum(1 for r in health if r.data.get("healthy"))
        advisors_line = f"{online}/{len(health)} online — {marks}"
    else:
        advisors_line = "none configured (run: nvh setup)"
    console.print(f"[bold]Advisors:[/bold] {advisors_line}")

    budget = first("budget")
    console.print(f"[bold]Budget:[/bold]   {budget.detail if budget else 'unavailable'}")
    savings = first("savings")
    if savings:
        console.print(f"[bold]Savings:[/bold]  {savings.detail}")
    services = first("services")
    if services:
        console.print(f"[bold]Services:[/bold] {services.detail}")

    default_mode = getattr(ctx.config.defaults, "mode", "ask") if ctx.config is not None else "ask"
    console.print(
        f"[bold]Mode:[/bold]     {default_mode} (default)"
        " — change with: nvh config set defaults.mode convene"
    )
    console.print(Rule(style="dim"))


def _status_providers(ctx, *, json_output: bool) -> None:
    import json as _json

    from nvh.integrations.diagnostics import checks as diag

    results = _run(diag.run_checks(diag.PROVIDERS, ctx))
    if json_output:
        print(_json.dumps(diag.summarize(results), indent=2, default=_json_default))
        return
    health = [r for r in results if r.id == "provider_health" and r.data.get("provider")]
    if not health:
        console.print("[red]No providers enabled.[/red]\n  Run: [bold]nvh setup[/bold]")
        raise typer.Exit(1)

    table = Table(title="Provider Health & Resilience", show_header=True, header_style="bold cyan")
    table.add_column("Provider", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Health", justify="right")
    table.add_column("Latency / error")
    table.add_column("In Fallback Chain", justify="center")
    healthy = 0
    for r in sorted(health, key=lambda r: r.data["provider"]):
        score = r.data.get("score", 0.5)
        if score >= 0.8:
            status_str, healthy = "[green]Healthy[/green]", healthy + 1
        elif score >= 0.4:
            status_str = "[yellow]Degraded[/yellow]"
        elif score >= 0.1:
            status_str = "[red]Unhealthy[/red]"
        else:
            status_str = "[red bold]Down[/red bold]"
        pos = r.data.get("chain_position", 0)
        table.add_row(
            r.data["provider"], status_str, f"{score:.0%}",
            f"{_STATUS_ICONS[r.status]} {r.detail}", f"#{pos}" if pos else "[dim]—[/dim]",
        )
    console.print(table)
    console.print()
    console.print(f"  [bold]{healthy}/{len(health)}[/bold] providers healthy")
    if healthy >= 3:
        console.print("  [green]Resilient[/green] — your workflow survives any single provider outage")
    elif healthy >= 2:
        console.print("  [yellow]Partial resilience[/yellow] — add more providers with [bold]nvh setup --all[/bold]")
    else:
        console.print("  [red]Vulnerable[/red] — only 1 healthy provider. Run [bold]nvh setup[/bold] to add more.")
    chain = next((r for r in results if r.id == "fallback_chain"), None)
    if chain:
        console.print(f"\n  Fallback chain: [bold]{chain.detail}[/bold]")
    console.print()


def _print_check_table(results, title: str) -> None:
    from nvh.integrations.diagnostics import checks as diag

    table = Table(title=title, show_lines=False)
    table.add_column("Check", style="bold", min_width=35)
    table.add_column("Status", justify="center", min_width=8)
    table.add_column("Detail")
    for r in results:
        table.add_row(r.title, _STATUS_LABELS.get(r.status, r.status.upper()), r.detail)
    console.print(table)

    summary = diag.summarize(results)
    summary_parts = [
        f"[{colour}]{summary[key]} {label}[/{colour}]"
        for key, label, colour in (
            ("passed", "passed", "green"), ("warned", "warnings", "yellow"), ("failed", "failures", "red"),
        )
        if summary[key]
    ]
    console.print(f"\nResults: {', '.join(summary_parts)} ({summary['total']} checks total)")

    if summary["fixes"]:
        console.print("\n[bold]Suggested fixes:[/bold]")
        for i, fix in enumerate(summary["fixes"], 1):
            console.print(f"  {i}. {fix}")


def _confirm_fix(prompt: str) -> bool:
    try:
        answer = console.input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer not in ("n", "no")


def _apply_deep_fixes(ctx, results) -> list:
    """Interactive repairs for `--deep --fix`; returns the rows to append."""
    from nvh.integrations.diagnostics import checks as diag

    extra = []
    by_id = {r.id: r for r in results}
    ollama = by_id.get("ollama")
    ollama_cfg = ctx.config.providers.get("ollama") if ctx.config is not None else None
    if ollama and ollama.status != "pass" and ollama_cfg is not None and ollama_cfg.enabled:
        console.print("\n[yellow]Ollama is enabled in config but not running.[/yellow]")
        if _confirm_fix("  Restart Ollama now? [Y/n] "):
            from nvh.cli.setup import _find_ollama_binary, _start_ollama

            ollama_bin = _find_ollama_binary()
            if ollama_bin is None:
                console.print("  [red]ollama binary not found.[/red] Install from https://ollama.com")
            elif _start_ollama(console, ollama_bin):
                ctx.reset_ollama()
                if ctx.ollama_models is not None:
                    extra.append(diag.CheckResult(
                        "ollama", "Ollama (restarted)", "pass",
                        f"now running, {len(ctx.ollama_models)} model(s)",
                    ))

    required = by_id.get("ollama_required_models")
    missing = required.data.get("missing", []) if required else []
    if missing:
        console.print(
            f"\n[yellow]{len(missing)} required model(s) missing in Ollama:[/yellow]"
            f" [bold]{', '.join(missing)}[/bold]"
        )
        if _confirm_fix("  Pull them now? [Y/n] "):
            from nvh.cli.setup import _find_ollama_binary, _pull_model

            ollama_bin = _find_ollama_binary() or "ollama"
            pulled = [m for m in missing if _pull_model(console, m, ollama_bin)]
            if pulled:
                extra.append(diag.CheckResult(
                    "ollama_required_models", "Ollama required models (fixed)", "pass",
                    f"pulled {', '.join(pulled)}",
                ))
    return extra


def _status_deep(ctx, *, json_output: bool, fix: bool, storage_only: bool) -> None:
    import json as _json

    from nvh.integrations.diagnostics import checks as diag

    # Under --json stdout is the payload; everything human goes to stderr.
    out = err_console if json_output else console

    if storage_only:
        from nvh.integrations.workspace.storage import ensure_storage

        storage = ensure_storage(ctx.home_dir, min_free_gb=ctx.min_free_gb)
        out.print("[bold]Storage preflight[/bold]\n")
        out.print(f"  NVH_HOME:  [bold]{storage.layout.home}[/bold]")
        out.print(f"  Env file:  {storage.env_file}")
        out.print(f"  Writable:  {'yes' if storage.writable else 'no'}")
        out.print(
            f"  Free:      {storage.free_gb if storage.free_gb is not None else '?'} GB"
            f" / minimum {storage.min_free_gb:.0f} GB"
        )
        for warning in storage.warnings:
            out.print(f"  [yellow]![/yellow] {warning}")
        out.print(f"\n  [green]Activate:[/green] source {storage.env_file}")
        raise typer.Exit(0 if storage.ok and storage.configured_by != "default" else 1)

    out.print("[bold]nvh status --deep[/bold] — running diagnostics...\n")
    results = _run(diag.run_checks(diag.DEEP, ctx))
    if fix:
        results.extend(_apply_deep_fixes(ctx, results))
    summary = diag.summarize(results)

    if json_output:
        print(_json.dumps({"schema_version": 2, **summary}, indent=2, default=_json_default))
        raise typer.Exit(0 if summary["failed"] == 0 else 1)

    _print_check_table(results, "Diagnostic Results")
    if summary["failed"]:
        raise typer.Exit(1)


def _status_smoke(ctx, *, imports: bool, json_output: bool, strict: bool) -> None:
    import json as _json

    from rich.rule import Rule

    from nvh.integrations.diagnostics import checks as diag
    from nvh.integrations.diagnostics.smoke_tests import smoke_test_report

    report = smoke_test_report(home_dir=ctx.home_dir, imports=imports)
    # The registry's smoke rows (API probes against NVH_API_URL; "skip" when
    # nothing listens) ride along under "checks" so the JSON keeps its shape.
    checks = _run(diag.run_checks(diag.SMOKE, ctx))
    summary = diag.summarize(checks)
    report["checks"] = summary
    failed = report["failed"] or summary["failed"]
    warned = report["warnings"] or summary["warned"]
    exit_code = 1 if failed or (strict and warned) else 0

    if json_output:
        print(_json.dumps(report, indent=2, default=_json_default))
        raise typer.Exit(exit_code)

    console.print()
    console.print(Rule("nvHive Smoke Test"))
    for t in report["tests"]:
        console.print(f"  {_STATUS_ICONS.get(t['status'], '?')} {t['title']}  [dim]{t['summary']}[/dim]")
        if t.get("detail") and t["status"] in ("warn", "fail"):
            console.print(f"      [dim]{t['detail']}[/dim]")
    for r in checks:
        console.print(f"  {_STATUS_ICONS.get(r.status, '?')} {r.title}  [dim]{r.detail}[/dim]")
        if r.fix and r.status in ("warn", "fail"):
            console.print(f"      [dim]{r.fix}[/dim]")
    console.print()
    console.print(f"  {report['summary']}")
    if summary["total"]:
        console.print(
            f"  API: {summary['passed']} passed, {summary['warned']} warning(s), "
            f"{summary['failed']} failed, {summary['skipped']} skipped"
        )
    if exit_code:
        console.print("  [dim]Fix hints: nvh status --deep --fix · nvh workstation[/dim]")
    console.print()
    raise typer.Exit(exit_code)


def _nvidia_bug_report() -> dict[str, Any]:
    import subprocess

    path = os.path.expanduser("~/nvh/nvidia-bug-report.log.gz")
    try:
        result = subprocess.run(
            ["nvidia-bug-report.sh", "--output-file", path],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "nvidia-bug-report.sh not found (NVIDIA driver may not be installed)"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "nvidia-bug-report.sh timed out (60s)"}
    if result.returncode == 0 and os.path.exists(path):
        return {"ok": True, "path": path, "size_kb": round(os.path.getsize(path) / 1024)}
    return {"ok": False, "error": f"exit {result.returncode}: {result.stderr.strip()[:200]}"}


def _status_report(
    ctx,
    *,
    imports: bool,
    live: bool,
    live_prompt: str,
    strict: bool,
    output: str | None,
    send: bool,
    nvidia_report: bool,
    quiet: bool,
) -> None:
    """Redacted JSON support bundle; nothing leaves $NVH_HOME/support/."""
    import json as _json

    from nvh import telemetry as _telemetry
    from nvh.integrations.diagnostics import checks as diag
    from nvh.integrations.diagnostics.smoke_tests import smoke_test_report
    from nvh.integrations.workspace.passport import support_snapshot
    from nvh.integrations.workspace.storage import nvh_home as _nvh_home

    home_path, home_source = _nvh_home(ctx.home_dir)

    def say(text: str) -> None:
        if not quiet:
            console.print(text)

    say("[bold]nvh status --report[/bold] — gathering support bundle...\n")
    say(f"  NVH_HOME: [bold]{home_path}[/bold] ([dim]{home_source}[/dim])\n")

    bundle: dict[str, Any] = {
        "schema_version": 2,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "nvh_home": str(home_path),
        "nvh_home_source": home_source,
        "nvh_version": diag.nvh_version(),
        "platform": diag.platform_summary(),
        "components": {},
        "status": {"ok": True, "failures": [], "warnings": []},
    }
    failures: list[str] = bundle["status"]["failures"]
    warnings: list[str] = bundle["status"]["warnings"]
    soft = failures if strict else warnings

    say("  [dim]→ running checks ...[/dim]")
    results = _run(diag.run_checks(diag.REPORT, ctx))
    summary = diag.summarize(results)
    bundle["components"]["checks"] = summary
    if summary["failed"]:
        failures.append(f"checks: {summary['failed']} failure(s)")
    if summary["warned"]:
        soft.append(f"checks: {summary['warned']} warning(s)")

    say("  [dim]→ running smoke test ...[/dim]")
    smoke: dict[str, Any] | None = None
    try:
        smoke = smoke_test_report(home_dir=str(home_path), imports=imports)
        bundle["components"]["smoke"] = smoke
        if smoke["failed"]:
            failures.append(f"smoke: {smoke['failed']} hard failure(s)")
        if smoke["warnings"]:
            soft.append(f"smoke: {smoke['warnings']} warning(s)")
    except Exception as e:  # noqa: BLE001 — bundle continues even if one block fails
        bundle["components"]["smoke"] = {"error": str(e)[:500]}
        failures.append(f"smoke: crashed ({type(e).__name__})")

    if not live:
        bundle["components"]["wizard_live_turn"] = {"skipped": True}
    else:
        say("  [dim]→ exercising live Wizard turn ...[/dim]")
        try:
            from nvh.core.engine import Engine

            async def _live() -> dict[str, Any]:
                engine = Engine()
                await engine.initialize()
                t0 = time.monotonic()
                resp = await engine.query(live_prompt)
                return {
                    "ok": True,
                    "provider": getattr(resp, "provider", "?"),
                    "model": getattr(resp, "model", "?"),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "reply_chars": len(getattr(resp, "content", "") or ""),
                }

            result = _run(_live())
            bundle["components"]["wizard_live_turn"] = result
            _telemetry.emit(
                "first_wizard_turn",
                {
                    "provider": result.get("provider"),
                    "model": result.get("model"),
                    "duration_ms": result.get("duration_ms"),
                    "source": "status-report",
                },
                home_dir=ctx.home_dir,
            )
        except Exception as e:  # noqa: BLE001
            err = str(e)[:500]
            rate_limited = "rate" in err.lower() or "429" in err
            bundle["components"]["wizard_live_turn"] = {"ok": False, "error": err, "rate_limited": rate_limited}
            if rate_limited and not strict:
                warnings.append("wizard_live_turn: rate limited (transient)")
            else:
                failures.append(f"wizard_live_turn: {type(e).__name__}")

    say("  [dim]→ writing redacted workspace snapshot ...[/dim]")
    try:
        # Embed the sections already gathered above instead of re-running them.
        snap = support_snapshot(
            home_dir=str(home_path), include_logs=True,
            smoke_tests=smoke, registry_checks=summary,
        )
        bundle["components"]["support_snapshot"] = {
            "path": snap.get("path"),
            "workspace_id": snap.get("passport", {}).get("workspace_id"),
            "rootless": snap.get("passport", {}).get("rootless"),
            "excludes": snap.get("excludes", []),
        }
    except Exception as e:  # noqa: BLE001
        bundle["components"]["support_snapshot"] = {"error": str(e)[:500]}
        warnings.append(f"support_snapshot: {type(e).__name__}")

    try:
        bundle["components"]["telemetry"] = _telemetry.summary(home_dir=ctx.home_dir)
    except Exception as e:  # noqa: BLE001
        bundle["components"]["telemetry"] = {"error": str(e)[:500]}

    if nvidia_report:
        say("  [dim]→ running nvidia-bug-report.sh ...[/dim]")
        bundle["components"]["nvidia_bug_report"] = _nvidia_bug_report()

    bundle["status"]["ok"] = not failures

    if output:
        bundle_path = Path(output).expanduser()
    else:
        stamp = bundle["created_at"].replace(":", "").replace("-", "")
        bundle_path = home_path / "support" / f"status-report-{stamp}.json"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    text = _json.dumps(bundle, indent=2, sort_keys=True, default=str)
    bundle_path.write_text(text, encoding="utf-8")

    if send:
        try:
            _write_clipboard(text)
            say("  [dim]Bundle copied to clipboard.[/dim]")
        except RuntimeError as e:
            say(f"  [yellow]{e}[/yellow]")

    if quiet:
        print(str(bundle_path))
    else:
        console.print()
        if bundle["status"]["ok"]:
            console.print(f"  [bold green]Bundle OK[/bold green] → {bundle_path}")
        else:
            console.print(f"  [bold red]Bundle has failures[/bold red] → {bundle_path}")
            for f in failures:
                console.print(f"    [red]✗[/red] {f}")
        for w in warnings:
            console.print(f"    [yellow]![/yellow] {w}")
        attention = [r for r in results if r.status in ("warn", "fail")]
        if attention:
            console.print()
            _print_check_table(attention, "Needs attention")
        console.print(
            "\n  [dim]Send the bundle file above to support, or paste"
            " its contents into the issue.[/dim]"
        )
    raise typer.Exit(0 if bundle["status"]["ok"] else 1)


# Pre-0.42 diagnostic verbs — hidden aliases of one `nvh status` tier each.
# Every flag is re-parsed by `nvh status`; only the spellings that no longer
# exist there are translated.

def _translate_test(argv: list[str]) -> list[str]:
    """Pre-0.42 `nvh test` flags: --api feeds the API probes, the rest are accepted and ignored."""
    api_url = _pop_flag(argv, "--api", value=True)
    if api_url:
        os.environ["NVH_API_URL"] = api_url
    ignored = [
        flag for flag in ("--webui", "--no-webui", "--no-providers", "--fix", "--quick")
        if _pop_flag(argv, flag, value=flag == "--webui") is not None
    ]
    if ignored:
        err_console.print(
            f"[dim]nvh test: {' '.join(ignored)} no longer apply — `nvh status --smoke` runs"
            " the same offline checks every time (these flags go away in 0.43).[/dim]"
        )
    return argv


def _translate_selfcheck(argv: list[str]) -> list[str]:
    if _pop_flag(argv, "--no-live-query"):
        argv.remove("--live")
    query = _pop_flag(argv, "--query", value=True)
    return [*argv, "--live-prompt", query] if query else argv


_alias("health")
_alias("why")
_alias("doctor")
_alias("test", translate=_translate_test)
_alias("smoke", translate=_translate_test)
_alias("debug")
_alias("selfcheck", translate=_translate_selfcheck)


# ---------------------------------------------------------------------------
# nvh setup — one-shot free tier wizard
# ---------------------------------------------------------------------------

EULA_TEXT = """
NVHive — Terms of Use

NVIDIA DISCLAIMER: NVHive is an independent project. It is NOT developed,
maintained, endorsed, or affiliated with NVIDIA Corporation. NVIDIA,
GeForce, Nemotron, DGX, and NIM are trademarks of NVIDIA Corporation.

By proceeding, you agree to:

1. The NVHive EULA (see EULA.md) and Privacy Policy (see PRIVACY.md)
2. Your email may be used to create accounts on free AI provider platforms
3. Each provider has its own Terms of Service which you accept during signup
4. Queries sent to cloud AI providers are subject to THEIR data policies
5. API keys are stored locally on your machine (OS keychain — never transmitted)
6. NVHive does not collect telemetry, analytics, or personal data
7. Local AI processing (nvh ask --local) keeps all data on your device
8. Free tiers have rate limits — NVHive manages these automatically
9. Nemotron model usage is subject to NVIDIA's model license terms
10. AI-generated content should be reviewed before relying on it

Your email is stored locally at ~/.hive/user.json for provider signups only.
It is NEVER sent to NVHive servers (we don't have any).

Full terms: https://github.com/thatcooperguy/nvHive/blob/main/EULA.md
Privacy: https://github.com/thatcooperguy/nvHive/blob/main/PRIVACY.md
""".strip()


# Free providers grouped by signup friction
ZERO_SIGNUP = [
    ("ollama", "Ollama (Local AI)", "Runs on your GPU — no signup needed"),
    ("llm7", "LLM7", "Anonymous access — no signup needed"),
]

EMAIL_SIGNUP = [
    ("groq", "Groq", "https://console.groq.com/keys", "Ultra-fast, 30 RPM free"),
    ("cerebras", "Cerebras", "https://cloud.cerebras.ai/", "Fastest inference, 30 RPM free"),
    ("fireworks", "Fireworks AI", "https://fireworks.ai/", "10 RPM free"),
    ("siliconflow", "SiliconFlow", "https://cloud.siliconflow.cn/", "1000 RPM free — best limits"),
    ("cohere", "Cohere", "https://dashboard.cohere.com/api-keys", "Trial key, RAG specialist"),
    ("ai21", "AI21 Labs", "https://studio.ai21.com/", "$10 free credit, 256K context"),
    ("sambanova", "SambaNova", "https://cloud.sambanova.ai/", "200K tokens/day free"),
    ("huggingface", "Hugging Face", "https://huggingface.co/settings/tokens", "Free Inference API"),
]

ACCOUNT_SIGNUP = [
    (
        "google", "Google Gemini",
        "https://aistudio.google.com/apikey",
        "Google account, 15 RPM free",
    ),
    ("nvidia", "NVIDIA NIM", "https://build.nvidia.com/", "NVIDIA Dev account, 1000 credits"),
    ("mistral", "Mistral", "https://console.mistral.ai/api-keys", "Phone verify, 2 RPM free"),
]


@app.command(rich_help_panel="Admin")
def setup(
    email: str | None = typer.Option(None, "--email", "-e", help="Your email for provider signups"),
    all_providers: bool = typer.Option(
        False, "--all",
        help="Set up ALL free providers (opens many browser tabs)",
    ),
    skip_eula: bool = typer.Option(False, "--accept-terms", help="Accept terms without prompting"),
):
    """One-shot setup — configure all free AI providers at once.

    Walks you through setting up every free AI provider so NVHive
    works with maximum capability out of the gate.

    Zero-signup providers (Ollama, LLM7) are enabled immediately.
    For others, signup pages are opened and you paste the API key.

    Examples:
        nvh setup                     # interactive wizard
        nvh setup --email me@uni.edu  # pre-fill email for signups
        nvh setup --all               # set up everything at once
    """
    # Step 1: EULA
    if not skip_eula:
        console.print(Panel(
            EULA_TEXT,
            title="[bold]NVHive — Terms of Use[/bold]",
            border_style="green",
        ))
        if not typer.confirm("\nDo you agree to these terms?", default=True):
            console.print("[dim]Setup cancelled.[/dim]")
            raise typer.Exit()

    # Step 2: Collect email
    if not email:
        email = typer.prompt("Your email (used for provider signups)", default="")

    # Save user profile locally (never transmitted to NVHive)
    if email:
        import json
        from pathlib import Path
        user_file = Path.home() / ".hive" / "user.json"
        user_file.parent.mkdir(parents=True, exist_ok=True)
        user_data = {}
        if user_file.exists():
            try:
                user_data = json.loads(user_file.read_text())
            except Exception:
                pass
        user_data["email"] = email
        user_data["eula_accepted"] = True
        user_data["eula_version"] = "1.0"
        try:
            from datetime import UTC, datetime
            user_data["accepted_at"] = datetime.now(UTC).isoformat()
        except Exception:
            pass
        user_file.write_text(json.dumps(user_data, indent=2))
        console.print(f"[dim]Profile saved locally: {user_file}[/dim]")

    console.print()

    # Step 3: Zero-signup providers (auto-enable)
    console.print(
        "[bold green]Step 1/3: Zero-signup providers"
        "[/bold green] (enabled immediately)\n"
    )
    _ollama_url = ollama_base_url()
    for name, display, desc in ZERO_SIGNUP:
        if name == "ollama":
            try:
                import httpx
                resp = httpx.get(f"{_ollama_url}/api/tags", timeout=3)
                if resp.status_code == 200:
                    models = resp.json().get("models", [])
                    console.print(
                        f"  [green]✓[/green] {display}"
                        f" — {desc} ({len(models)} models ready)"
                    )
                    if _ollama_url != DEFAULT_OLLAMA_URL:
                        console.print(f"    [dim]Using custom URL: {_ollama_url}[/dim]")
                else:
                    console.print(
                        f"  [yellow]![/yellow] {display}"
                        " — not running. Start with: ollama serve"
                    )
            except Exception:
                console.print(f"  [yellow]![/yellow] {display} — not detected at {_ollama_url}")
                console.print("    Install rootlessly: nvh studio --install rootless-ollama -y")
                console.print("    Custom URL: export OLLAMA_BASE_URL=http://host:port")
        else:
            console.print(f"  [green]✓[/green] {display} — {desc}")

    console.print()

    # Step 4: Email-signup providers
    console.print(
        "[bold green]Step 2/3: Email-signup providers"
        "[/bold green] (free, just need a key)\n"
    )

    providers_to_setup = EMAIL_SIGNUP if not all_providers else EMAIL_SIGNUP + ACCOUNT_SIGNUP
    configured = 0
    skipped = 0

    for name, display, url, desc in providers_to_setup:
        # Check if already configured
        has_key = False
        try:
            import keyring
            has_key = bool(keyring.get_password("nvhive", f"{name}_api_key"))
        except Exception:
            pass
        if not has_key:
            from nvh.providers.registry import resolve_provider_key
            has_key = bool(resolve_provider_key(name)[0])

        if has_key:
            console.print(f"  [green]✓[/green] {display} — already configured")
            configured += 1
            continue

        console.print(f"\n  [bold]{display}[/bold] — {desc}")

        # Build signup URL with email pre-fill where possible
        signup_url = url
        if email and "google" not in url.lower():
            # Some providers support email pre-fill via URL params
            if "?" in signup_url:
                signup_url += f"&email={email}"
            # Don't append email to URLs that don't support it

        do_setup = typer.confirm(f"  Set up {display}?", default=True)
        if not do_setup:
            skipped += 1
            continue

        console.print(f"  Opening: [link={signup_url}]{signup_url}[/link]")
        webbrowser.open(signup_url)

        key = typer.prompt(
            f"  Paste your {display} API key (or Enter to skip)",
            default="", hide_input=True,
        )
        if key:
            key = key.strip()
            # Validate key format (basic sanity check)
            if len(key) < 10:
                console.print(
                    f"  [red]✗ Key looks too short ({len(key)} chars)."
                    " Skipping — double-check and re-run nvh setup.[/red]"
                )
                skipped += 1
                continue

            # Quick connectivity test
            _key_valid = False
            with console.status(f"  Testing {display} key..."):
                try:
                    import httpx
                    _test_urls = {
                        "groq": "https://api.groq.com/openai/v1/models",
                        "cerebras": "https://api.cerebras.ai/v1/models",
                        "fireworks": "https://api.fireworks.ai/inference/v1/models",
                        "cohere": "https://api.cohere.ai/v1/models",
                        "google": "https://generativelanguage.googleapis.com/v1/models",
                        "mistral": "https://api.mistral.ai/v1/models",
                    }
                    _test_url = _test_urls.get(name)
                    if _test_url:
                        _headers = {"Authorization": f"Bearer {key}"}
                        if name == "google":
                            # Google uses query param
                            _test_url += f"?key={key}"
                            _headers = {}
                        _resp = httpx.get(_test_url, headers=_headers, timeout=8)
                        if _resp.status_code in (200, 201):
                            _key_valid = True
                        elif _resp.status_code in (401, 403):
                            console.print(
                                f"  [red]✗ Key rejected by {display}"
                                f" (HTTP {_resp.status_code})."
                                " Check the key and try again.[/red]"
                            )
                            skipped += 1
                            continue
                        else:
                            # Non-auth error — key might still be fine, store it
                            _key_valid = True
                    else:
                        # No test URL for this provider — trust the key
                        _key_valid = True
                except Exception:
                    # Network error — can't validate, store anyway
                    _key_valid = True

            # Store the key
            try:
                import keyring
                keyring.set_password("nvhive", f"{name}_api_key", key)
                if _key_valid:
                    console.print(f"  [green]✓ {display} configured and verified![/green]")
                else:
                    console.print(
                        f"  [green]✓ {display} key stored[/green]"
                        " [dim](could not verify — will test on first use)[/dim]"
                    )
                configured += 1
            except Exception:
                # Keyring unavailable — give specific fallback guidance
                _env_var = f"{name.upper()}_API_KEY"
                console.print(f"  [yellow]Keychain unavailable. To use {display}:[/yellow]")
                console.print(
                    f"    Option 1: export {_env_var}={key[:4]}..."
                    "  (add to ~/.bashrc or ~/.zshrc)"
                )
                console.print(
                    "    Option 2: Add to ~/.hive/config.yaml"
                    f" under providers.{name}.api_key"
                )
                skipped += 1
        else:
            console.print(f"  [dim]Skipped {display}[/dim]")
            skipped += 1

    # Step 5: Account-signup providers (if not already covered by --all)
    if not all_providers and ACCOUNT_SIGNUP:
        console.print(
            "\n[bold green]Step 3/3: Account-based providers"
            "[/bold green] (need existing account)\n"
        )
        for name, display, url, desc in ACCOUNT_SIGNUP:
            has_key = False
            try:
                import keyring
                has_key = bool(keyring.get_password("nvhive", f"{name}_api_key"))
            except Exception:
                pass

            if has_key:
                console.print(f"  [green]✓[/green] {display} — already configured")
                configured += 1
                continue

            console.print(f"  [dim]{display} — {desc}[/dim]")
            do_setup = typer.confirm(f"  Set up {display}?", default=False)
            if do_setup:
                webbrowser.open(url)
                key = typer.prompt(f"  Paste your {display} API key", default="", hide_input=True)
                if key:
                    try:
                        import keyring
                        keyring.set_password("nvhive", f"{name}_api_key", key)
                        console.print(f"  [green]✓ {display} configured![/green]")
                        configured += 1
                    except Exception:
                        pass
                else:
                    skipped += 1
            else:
                skipped += 1

    # GPU detection + auto-pull all models that fit
    console.print()
    console.print(
        "[bold green]Step 3/3: Local GPU inference"
        "[/bold green]\n",
    )
    try:
        from nvh.utils.gpu import detect_gpus, recommend_models
        gpus = detect_gpus()
        if gpus:
            gpu = gpus[0]
            console.print(
                f"  [green]Detected:[/green] {gpu.name}"
                f" ({gpu.vram_gb:.0f}GB VRAM)",
            )
            recs = recommend_models(gpus)
            if recs:
                # If multiple models recommended, ask the
                # user's preference when there's a real
                # tradeoff (one big model vs two smaller)
                total_vram = sum(g.vram_gb for g in gpus)
                if (
                    len(recs) > 1
                    and total_vram >= 12
                    and total_vram < 48
                ):
                    console.print(
                        "  [bold]Choose local model"
                        " strategy:[/bold]",
                    )
                    console.print(
                        f"    1. Both models for"
                        f" local council:"
                        f" {', '.join(r.model for r in recs)}"
                        f" [green](recommended)[/green]",
                    )
                    # Find the largest single model
                    # that fits
                    single = recs[0]
                    console.print(
                        f"    2. Single larger model:"
                        f" {single.model} only"
                        f" (more VRAM headroom)",
                    )
                    choice = typer.prompt(
                        "  Choice", default="1",
                    )
                    if choice == "2":
                        recs = [single]

                console.print(
                    f"  Models: "
                    f"{', '.join(r.model for r in recs)}",
                )

                # Check if Ollama is running AND pull missing models.
                # Use the HTTP API for pulls (via setup._pull_model) rather
                # than shelling out to the `ollama` CLI — the daemon can be
                # up without the CLI binary being on PATH (common with
                # portable installs at ~/.nvh/ollama/ollama), and an
                # uncaught FileNotFoundError would bail the whole block
                # with a misleading "Ollama not detected" message.
                _ollama_base = ollama_base_url()
                daemon_reachable = False
                existing: list[str] = []
                try:
                    import httpx as _hx
                    _r = _hx.get(f"{_ollama_base}/api/tags", timeout=3)
                    if _r.status_code == 200:
                        daemon_reachable = True
                        # Keep both full name and base for comparison —
                        # rec.model might be tag-less (`nemotron`) while
                        # Ollama returns `nemotron:latest`.
                        for m in _r.json().get("models", []):
                            nm = m.get("name", "")
                            existing.append(nm)
                            existing.append(nm.split(":")[0])
                except Exception:
                    daemon_reachable = False

                if daemon_reachable:
                    from nvh.cli.setup import _find_ollama_binary, _pull_model
                    ollama_bin = _find_ollama_binary() or "ollama"
                    pulled_ok = 0
                    for rec in recs:
                        # Match either the full name or the base tag
                        already = (
                            rec.model in existing
                            or rec.model.split(":")[0] in existing
                        )
                        if already:
                            console.print(
                                f"  [green]✓[/green] {rec.model}"
                                f" already installed",
                            )
                            pulled_ok += 1
                            continue

                        # _pull_model does a registry check first, uses
                        # HTTP streaming with progress bar, and swallows
                        # per-model exceptions — so one failure doesn't
                        # abort the loop or bail to the outer except.
                        try:
                            if _pull_model(console, rec.model, ollama_bin):
                                pulled_ok += 1
                        except Exception as _e:
                            console.print(
                                f"  [yellow]{rec.model} pull failed:"
                                f" {_e}[/yellow]",
                            )

                    if pulled_ok >= 2:
                        console.print(
                            "  [green]Local council ready —"
                            " multiple models for consensus[/green]",
                        )
                else:
                    console.print(
                        "  [dim]Ollama daemon not reachable at"
                        f" {_ollama_base} — start it with:"
                        " ollama serve[/dim]",
                    )
                    for rec in recs:
                        console.print(
                            f"  [dim]Then: ollama"
                            f" pull {rec.model}[/dim]",
                        )
        else:
            console.print(
                "  [dim]No NVIDIA GPU detected"
                " — local inference will use CPU mode[/dim]",
            )
    except Exception:
        console.print(
            "  [dim]GPU detection unavailable[/dim]",
        )

    # Write config — setup was previously logging "Setup complete!" without
    # ever creating ~/.hive/config.yaml. The REPL, doctor, and SDK all
    # depend on that file existing; silently skipping it meant the user
    # had to manually run `nvh config init` afterwards to recover.
    try:
        from nvh.cli.setup import _ollama_running, _write_config

        # Build the configured_providers dict from env vars (what setup
        # collected via env/keyring into OS environment during this run).
        env_map = {
            "groq": "GROQ_API_KEY",
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
        }
        configured_keys: dict[str, str] = {}
        for provider, env_var in env_map.items():
            v = os.environ.get(env_var, "").strip()
            if v:
                configured_keys[provider] = v

        ollama_up, _ = _ollama_running()
        config_path = _write_config(configured_keys, ollama_enabled=ollama_up)
        console.print(
            f"\n[dim]Config saved to {config_path}[/dim]"
        )
    except Exception as _cfg_exc:
        console.print(
            f"\n[yellow]Note: could not write config automatically"
            f" ({_cfg_exc}). Run [bold]nvh config init[/bold] to"
            f" create one.[/yellow]"
        )

    # Summary
    total_free = len(ZERO_SIGNUP) + configured
    console.print("\n[bold green]Setup complete![/bold green]")
    console.print(
        f"  {total_free} free advisors ready, {skipped} skipped",
    )
    console.print()
    console.print("  [bold]Next steps:[/bold]")
    console.print(
        "    Verify everything works: "
        " [bold]nvh status --smoke[/bold]",
    )
    console.print(
        "    Try a query:             "
        " [bold]nvh \"What is the meaning of life?\"[/bold]",
    )
    console.print(
        "    Launch interactive chat:  "
        " [bold]nvh[/bold]",
    )
    console.print(
        "    Start the web dashboard:  "
        " [bold]nvh webui[/bold]",
    )
    if skipped > 0:
        console.print(
            f"    Set up {skipped} more providers:"
            f" [bold]nvh setup --all[/bold]",
        )
    console.print()


# ---------------------------------------------------------------------------
# nvh conversation
# ---------------------------------------------------------------------------

from nvh.cli.conversations import conversation_app  # noqa: E402

app.add_typer(conversation_app, name="conversation", rich_help_panel="Subcommands")


# ---------------------------------------------------------------------------
# hive config
# ---------------------------------------------------------------------------

config_app = typer.Typer(help="Manage configuration")
app.add_typer(config_app, name="config", rich_help_panel="Subcommands")


@config_app.command("init")
def config_init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing config"),
):
    """Initialize Hive configuration with interactive setup."""
    from nvh.config.settings import DEFAULT_CONFIG_PATH, generate_default_config, get_config_dir

    get_config_dir()

    if DEFAULT_CONFIG_PATH.exists() and not force:
        console.print(f"Config already exists at [bold]{DEFAULT_CONFIG_PATH}[/bold]")
        console.print("Use --force to overwrite.")
        return

    config_content = generate_default_config()

    # Interactive advisor setup
    console.print("[bold]Hive Setup[/bold]\n")
    console.print("Let's configure your LLM advisors.\n")

    providers_to_enable = []

    for name, url in [
        ("openai", "https://platform.openai.com/api-keys"),
        ("anthropic", "https://console.anthropic.com/settings/keys"),
        ("google", "https://aistudio.google.com/apikey"),
    ]:
        if typer.confirm(f"Configure {name}?", default=False):
            console.print(f"  Get your API key at: [link={url}]{url}[/link]")
            open_browser = typer.confirm("  Open in browser?", default=True)
            if open_browser:
                webbrowser.open(url)
            key = typer.prompt(f"  Paste your {name} API key", hide_input=True, default="")
            if key:
                try:
                    import keyring
                    keyring.set_password("nvhive", f"{name}_api_key", key)
                    console.print("  [green]Key stored securely in keychain[/green]")
                except Exception:
                    console.print(
                        "  [yellow]Keychain unavailable."
                        " Key will be read from env var.[/yellow]"
                    )
                providers_to_enable.append(name)

    # Check for Ollama (supports OLLAMA_BASE_URL env var)
    _ollama_cfg_url = ollama_base_url()
    console.print(f"\nChecking for local Ollama at {_ollama_cfg_url}...")
    try:
        import httpx
        resp = httpx.get(f"{_ollama_cfg_url}/api/tags", timeout=3)
        if resp.status_code == 200:
            models = resp.json().get("models", [])
            console.print(f"  [green]Ollama detected! {len(models)} models available.[/green]")
            providers_to_enable.append("ollama")
    except Exception:
        console.print("  [dim]Ollama not detected (not running or not installed)[/dim]")
        console.print("  [dim]Custom URL: export OLLAMA_BASE_URL=http://host:port[/dim]")

    # Update config to enable selected providers
    for name in providers_to_enable:
        config_content = config_content.replace(
            f"  {name}:\n    api_key:",
            f"  {name}:\n    enabled: true\n    api_key:",
        ).replace(
            "    type: ollama\n    enabled: false",
            "    type: ollama\n    enabled: true",
        ) if name == "ollama" else config_content.replace(
            f"  {name}:\n    api_key: ${{{name.upper()}_API_KEY}}\n    default_model:",
            (
                f"  {name}:\n    enabled: true\n"
                f"    api_key: ${{{name.upper()}_API_KEY}}\n"
                "    default_model:"
            ),
        )

    # Set default provider
    if providers_to_enable:
        default = providers_to_enable[0]
        config_content = config_content.replace('  provider: ""', f'  provider: {default}')

    DEFAULT_CONFIG_PATH.write_text(config_content)
    console.print(f"\n[green]Config written to {DEFAULT_CONFIG_PATH}[/green]")
    default_adv = providers_to_enable[0] if providers_to_enable else "none"
    console.print(f"Default advisor: [bold]{default_adv}[/bold]")
    console.print("\nRun [bold]hive ask \"Hello, world!\"[/bold] to test your setup!")


@config_app.command("get")
def config_get(
    key: str = typer.Argument(
        ..., help="Config key (dot notation, e.g. defaults.provider)"
    ),
):
    """Get a configuration value."""
    from nvh.config.settings import load_config
    config = load_config()
    parts = key.split(".")
    obj: any = config.model_dump()
    for part in parts:
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            console.print(f"[red]Key not found: {key}[/red]")
            raise typer.Exit(1)
    # Mask secrets
    if "key" in key.lower() or "secret" in key.lower():
        if isinstance(obj, str) and len(obj) > 8:
            obj = obj[:4] + "..." + obj[-4:]
    console.print(f"{key} = {obj}")


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key"),
    value: str = typer.Argument(..., help="Value to set"),
):
    """Set a configuration value."""
    from nvh.config.settings import load_config, save_config
    config = load_config()
    data = config.model_dump()

    parts = key.split(".")
    obj = data
    for part in parts[:-1]:
        if part not in obj:
            obj[part] = {}
        obj = obj[part]

    # Type coercion
    if value.lower() in ("true", "false"):
        obj[parts[-1]] = value.lower() == "true"
    elif value.isdigit():
        obj[parts[-1]] = int(value)
    else:
        try:
            obj[parts[-1]] = float(value)
        except ValueError:
            obj[parts[-1]] = value

    from nvh.config.settings import CouncilConfig
    new_config = CouncilConfig(**data)
    save_config(new_config)
    console.print(f"[green]Set {key} = {value}[/green]")


@config_app.command("edit")
def config_edit():
    """Open config file in $EDITOR."""
    import os

    from nvh.config.settings import DEFAULT_CONFIG_PATH
    editor = os.environ.get("EDITOR", "vi")
    os.system(f"{editor} {DEFAULT_CONFIG_PATH}")


@config_app.command("export")
def config_export(
    output: str | None = typer.Option(
        None, "--output", "-o",
        help="Output file path (default: stdout)",
    ),
):
    """Export the current config with API keys masked.

    API keys are shown as first-4 + last-4 characters. Raw key values are
    removed; only ${ENV_VAR} references are kept. The output is safe to share
    and can be re-imported after adding real keys.
    """
    import re as _re

    import yaml as _yaml

    from nvh.config.settings import load_config

    config = load_config()
    data = config.model_dump(mode="json")

    env_pattern = _re.compile(r"^\$\{[^}]+\}$")

    def _mask_value(v: str) -> str:
        """Show first 4 + last 4 chars; keep ${ENV_VAR} references as-is."""
        if not v:
            return v
        if env_pattern.match(v):
            return v  # already a reference — keep it
        if len(v) <= 8:
            return "****"
        return v[:4] + "****" + v[-4:]

    def _scrub(node: object) -> object:
        """Recursively mask all API key / secret fields."""
        if isinstance(node, dict):
            result = {}
            for k, val in node.items():
                k_lower = k.lower()
                if any(kw in k_lower for kw in ("api_key", "secret", "password", "token")):
                    if isinstance(val, str):
                        result[k] = _mask_value(val)
                    else:
                        result[k] = val
                else:
                    result[k] = _scrub(val)
            return result
        if isinstance(node, list):
            return [_scrub(item) for item in node]
        return node

    scrubbed = _scrub(data)

    header = (
        "# Hive config export\n"
        "# Add your API keys before importing.\n"
        "# Import with: hive config import <file>\n"
        "#\n"
    )
    yaml_text = _yaml.dump(scrubbed, default_flow_style=False, sort_keys=False)
    full_output = header + yaml_text

    if output:
        out_path = Path(output)
        out_path.write_text(full_output)
        console.print(f"[green]Config exported to {out_path}[/green]")
    else:
        console.print(full_output, highlight=False)


@config_app.command("import")
def config_import(
    file: str = typer.Argument(..., help="Path to the config YAML file to import"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Import a config file, backing up the existing config first.

    Validates the YAML and Pydantic schema before writing. Warns about any
    ${ENV_VAR} references that are not currently set in the environment.
    """
    import yaml as _yaml

    from nvh.config.settings import (
        DEFAULT_CONFIG_PATH,
        CouncilConfig,
        get_config_dir,
    )

    src = Path(file)
    if not src.exists():
        console.print(f"[red]File not found: {src}[/red]")
        raise typer.Exit(1)

    # Parse YAML
    try:
        raw = _yaml.safe_load(src.read_text()) or {}
    except _yaml.YAMLError as exc:
        console.print(f"[red]YAML parse error: {exc}[/red]")
        raise typer.Exit(1)

    # Validate against schema (without env interpolation — keep ${VAR} as-is)
    try:
        CouncilConfig(**raw)
    except Exception as exc:
        console.print(f"[red]Config schema validation failed:[/red]\n{exc}")
        raise typer.Exit(1)

    # Scan for unresolved ${ENV_VAR} references
    import re as _re
    env_pattern = _re.compile(r"\$\{([^}:]+)(?::-(.*?))?\}")
    unset_vars: list[str] = []

    def _find_unset(node: object) -> None:
        if isinstance(node, str):
            for m in env_pattern.finditer(node):
                var_name = m.group(1)
                default  = m.group(2)
                if os.environ.get(var_name) is None and default is None:
                    unset_vars.append(var_name)
        elif isinstance(node, dict):
            for v in node.values():
                _find_unset(v)
        elif isinstance(node, list):
            for v in node:
                _find_unset(v)

    _find_unset(raw)

    if unset_vars:
        console.print(
            "[yellow]Warning: the following environment variables are referenced "
            "in the config but not set:[/yellow]"
        )
        for var in sorted(set(unset_vars)):
            console.print(f"  [yellow]${var}[/yellow]")
        console.print()
        if not yes:
            for var in sorted(set(unset_vars)):
                val = typer.prompt(
                    f"  Enter value for {var} (leave blank to skip)",
                    default="",
                    show_default=False,
                )
                if val:
                    os.environ[var] = val

    if not yes:
        if not typer.confirm(
            f"Write config to {DEFAULT_CONFIG_PATH}? (existing file will be backed up)",
            default=True,
        ):
            console.print("[dim]Import cancelled.[/dim]")
            raise typer.Exit(0)

    # Back up existing config
    get_config_dir()
    if DEFAULT_CONFIG_PATH.exists():
        bak = DEFAULT_CONFIG_PATH.with_suffix(".yaml.bak")
        import shutil as _shutil
        _shutil.copy2(DEFAULT_CONFIG_PATH, bak)
        console.print(f"[dim]Backup written to {bak}[/dim]")

    # Write new config
    DEFAULT_CONFIG_PATH.write_text(src.read_text())
    console.print(f"[green]Config imported from {src} → {DEFAULT_CONFIG_PATH}[/green]")


@config_app.command("diff")
def config_diff(
    file: str = typer.Argument(..., help="Config file to compare against the current config"),
):
    """Show differences between the current config and a file.

    Highlights changes to routing rules, weights, budgets, and provider settings
    in a Rich table.
    """
    import yaml as _yaml

    from nvh.config.settings import CouncilConfig, load_config

    src = Path(file)
    if not src.exists():
        console.print(f"[red]File not found: {src}[/red]")
        raise typer.Exit(1)

    # Load both configs
    try:
        current = load_config()
    except Exception as exc:
        console.print(f"[red]Failed to load current config: {exc}[/red]")
        raise typer.Exit(1)

    try:
        raw = _yaml.safe_load(src.read_text()) or {}
        other = CouncilConfig(**raw)
    except Exception as exc:
        console.print(f"[red]Failed to load {src}: {exc}[/red]")
        raise typer.Exit(1)

    cur_data = current.model_dump(mode="json")
    oth_data = other.model_dump(mode="json")

    # Flatten a nested dict into dot-notation paths
    def _flatten(d: dict, prefix: str = "") -> dict[str, object]:
        out: dict[str, object] = {}
        for k, v in d.items():
            full_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                out.update(_flatten(v, full_key))
            elif isinstance(v, list):
                out[full_key] = v
            else:
                out[full_key] = v
        return out

    cur_flat = _flatten(cur_data)
    oth_flat = _flatten(oth_data)

    all_keys = sorted(set(cur_flat) | set(oth_flat))

    # Collect changed rows
    changed: list[tuple[str, str, str, bool]] = []
    for key in all_keys:
        cur_val = cur_flat.get(key, "[missing]")
        oth_val = oth_flat.get(key, "[missing]")

        # Mask secret fields for display
        key_lower = key.lower()
        is_secret = any(kw in key_lower for kw in ("api_key", "secret", "password", "token"))
        if is_secret:
            def _mask(v: object) -> str:
                s = str(v)
                if not s or s == "[missing]":
                    return s
                if len(s) <= 8:
                    return "****"
                return s[:4] + "****" + s[-4:]
            cur_disp = _mask(cur_val)
            oth_disp = _mask(oth_val)
        else:
            cur_disp = str(cur_val)
            oth_disp = str(oth_val)

        is_different = str(cur_val) != str(oth_val)
        changed.append((key, cur_disp, oth_disp, is_different))

    # Only show fields that differ, plus always show high-interest sections
    interesting_prefixes = (
        "routing.", "council.", "budget.", "defaults.", "cache.",
    )
    rows_to_show = [
        row for row in changed
        if row[3] or any(row[0].startswith(p) for p in interesting_prefixes)
    ]

    if not rows_to_show:
        console.print("[green]No differences found — configs are identical.[/green]")
        return

    from nvh.config.settings import DEFAULT_CONFIG_PATH
    table = Table(
        title=f"Config diff: current ({DEFAULT_CONFIG_PATH.name}) vs {src.name}",
        show_lines=True,
    )
    table.add_column("Key", style="bold", min_width=35)
    table.add_column(f"Current ({DEFAULT_CONFIG_PATH.name})", min_width=25)
    table.add_column(f"File ({src.name})", min_width=25)
    table.add_column("Changed", justify="center", min_width=7)

    for key, cur_disp, oth_disp, is_diff in rows_to_show:
        changed_cell = "[yellow]YES[/yellow]" if is_diff else "[dim]—[/dim]"
        cur_cell  = f"[green]{cur_disp}[/green]"  if is_diff else f"[dim]{cur_disp}[/dim]"
        oth_cell  = f"[yellow]{oth_disp}[/yellow]" if is_diff else f"[dim]{oth_disp}[/dim]"
        table.add_row(key, cur_cell, oth_cell, changed_cell)

    console.print(table)

    diff_count = sum(1 for row in rows_to_show if row[3])
    console.print(f"\n[bold]{diff_count}[/bold] field(s) differ.")


@config_app.command("migrate")
def config_migrate(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show changes without writing"),
    file: str | None = typer.Option(
        None, "--file", "-f", help="Config file to migrate (default: the user config)",
    ),
):
    """Rewrite retired model IDs and providers in config.yaml.

    Providers retire model IDs without notice; this rewrites the ones nvHive
    knows about (RETIRED_MODEL_RENAMES in nvh.cli.setup), removes providers
    whose service shut down, drops the dead top-level `hooks:` key, and
    keeps ${ENV_VAR} references untouched.
    """
    import shutil as _shutil

    import yaml as _yaml

    from nvh.cli.setup import migrate_config_data
    from nvh.config.settings import DEFAULT_CONFIG_PATH

    target = Path(file) if file else DEFAULT_CONFIG_PATH
    if not target.exists():
        console.print(f"[red]Config not found: {target}[/red]")
        raise typer.Exit(1)
    try:
        raw = _yaml.safe_load(target.read_text()) or {}
    except _yaml.YAMLError as exc:
        console.print(f"[red]YAML parse error: {exc}[/red]")
        raise typer.Exit(1)
    if not isinstance(raw, dict):
        console.print(f"[red]Config must be a YAML mapping: {target}[/red]")
        raise typer.Exit(1)

    migrated, changes = migrate_config_data(raw)
    if not changes:
        console.print("[green]Config is up to date — nothing to migrate.[/green]")
        return

    for change in changes:
        console.print(f"  {change}")
    if dry_run:
        console.print(
            f"\n[dim]Dry run — {len(changes)} change(s) not written to {target}[/dim]"
        )
        return

    bak = target.with_suffix(".yaml.bak")
    _shutil.copy2(target, bak)
    target.write_text(_yaml.safe_dump(migrated, default_flow_style=False, sort_keys=False))
    console.print(
        f"\n[green]Applied {len(changes)} change(s) to {target}[/green] [dim](backup: {bak})[/dim]"
    )


# ---------------------------------------------------------------------------
# hive advisor
# ---------------------------------------------------------------------------

advisor_app = typer.Typer(help="Manage LLM advisors")
app.add_typer(advisor_app, name="advisor", rich_help_panel="Subcommands")


@advisor_app.command("list")
def advisor_list():
    """List configured advisors and their status."""
    from nvh.config.settings import load_config
    config = load_config()

    table = Table(title="Configured Advisors")
    table.add_column("Advisor", style="bold")
    table.add_column("Enabled")
    table.add_column("Default Model")
    table.add_column("API Key")

    from nvh.providers.registry import resolve_provider_key

    for name, pconfig in config.providers.items():
        has_key = bool(resolve_provider_key(name, pconfig, ptype=pconfig.type or name)[0])
        if not has_key:
            try:
                import keyring
                has_key = bool(keyring.get_password("nvhive", f"{name}_api_key"))
            except Exception:
                pass

        key_status = "[green]configured[/green]" if has_key else "[red]missing[/red]"
        if name == "ollama":
            key_status = "[dim]not required[/dim]"

        table.add_row(
            name,
            "[green]yes[/green]" if pconfig.enabled else "[dim]no[/dim]",
            pconfig.default_model or "[dim]—[/dim]",
            key_status,
        )

    console.print(table)


@advisor_app.command("info")
def advisor_info(
    name: str = typer.Argument(..., help="Advisor name (e.g. openai, groq, ollama)"),
):
    """Show detailed advisor profile — strengths, weaknesses, and when to use."""
    from nvh.core.advisor_profiles import ADVISOR_PROFILES

    profile = ADVISOR_PROFILES.get(name)
    if not profile:
        console.print(f"[red]Unknown advisor: {name}[/red]")
        console.print(f"Available: {', '.join(ADVISOR_PROFILES.keys())}")
        raise typer.Exit(1)

    # Header
    console.print(f"\n[bold]{profile.display_name}[/bold]")
    free_label = "Yes" if profile.has_free_tier else "No"
    console.print(
        f"Cost tier: {profile.cost_tier}"
        f" | Free tier: {free_label}"
    )
    if profile.free_tier_limits:
        console.print(f"[green]{profile.free_tier_limits}[/green]")

    # Scores
    console.print(
        f"\n[dim]Quality: {profile.quality_weight:.0%}"
        f" | Speed: {profile.speed_weight:.0%}"
        f" | Cost efficiency: {profile.cost_weight:.0%}"
        f" | Reliability: {profile.reliability_weight:.0%}[/dim]"
    )

    # Special capabilities
    caps = []
    if profile.has_search:
        caps.append("Web Search")
    if profile.is_fast:
        caps.append("Ultra-Fast")
    if profile.is_local:
        caps.append("Local/Private")
    if profile.is_reasoning:
        caps.append("Deep Reasoning")
    if profile.long_context:
        caps.append("Long Context (100K+)")
    if caps:
        console.print(f"[cyan]Capabilities: {', '.join(caps)}[/cyan]")

    # Strengths
    console.print("\n[green]Strengths:[/green]")
    for s in profile.strengths:
        console.print(f"  [green]+[/green] {s}")

    # Best for
    console.print("\n[blue]Best for:[/blue]")
    for b in profile.best_for:
        console.print(f"  [blue]→[/blue] {b}")

    # Weaknesses
    console.print("\n[yellow]Weaknesses:[/yellow]")
    for w in profile.weaknesses:
        console.print(f"  [yellow]![/yellow] {w}")

    # Avoid for
    console.print("\n[red]Avoid for:[/red]")
    for a in profile.avoid_for:
        console.print(f"  [red]✗[/red] {a}")

    console.print()


@advisor_app.command("add")
def advisor_add(
    name: str = typer.Argument(..., help="Advisor name (e.g. openai)"),
    key: str = typer.Option("", "--key", "-k", help="API key"),
):
    """Add or update an advisor's API key."""
    if not key:
        key = typer.prompt("API key", hide_input=True)

    try:
        import keyring
        keyring.set_password("nvhive", f"{name}_api_key", key)
        console.print(f"[green]API key for {name} stored in keychain.[/green]")
    except Exception:
        console.print(
            f"[yellow]Keychain unavailable."
            f" Set {name.upper()}_API_KEY environment"
            " variable instead.[/yellow]"
        )


@advisor_app.command("remove")
def advisor_remove(name: str = typer.Argument(..., help="Advisor name")):
    """Remove an advisor's API key (keychain + .env) and disable it in config."""
    from nvh.cli.setup import disable_provider_in_config, provider_config_files, remove_key

    removed = remove_key(name)
    if removed["keyring"]:
        console.print(f"[green]API key for {name} removed from keychain.[/green]")
    if removed["env_file"]:
        paths = ", ".join(str(p) for p in removed["env_paths"])
        console.print(f"[green]Removed {', '.join(removed['env_file'])} from {paths}.[/green]")
    if not removed["keyring"] and not removed["env_file"]:
        console.print(f"[yellow]No stored API key found for {name}.[/yellow]")

    for cfg in provider_config_files():
        if disable_provider_in_config(cfg, name):
            console.print(f"[green]Disabled {name} in {cfg}.[/green]")


@advisor_app.command("test")
def advisor_test(
    name: str | None = typer.Argument(None, help="Advisor to test (omit for all)"),
    all_: bool = typer.Option(False, "--all", help="Test all configured advisors"),
):
    """Test advisor connectivity and API key validity."""
    async def _run_test():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config()
        engine = Engine(config=config)
        enabled = await engine.initialize()

        providers_to_test = [name] if name else enabled

        for pname in providers_to_test:
            if not engine.registry.has(pname):
                console.print(f"  [red]✗ {pname}: not configured or not enabled[/red]")
                continue

            provider = engine.registry.get(pname)
            console.print(f"  Testing {pname}...", end=" ")

            health = await provider.health_check()
            if health.healthy:
                console.print(f"[green]✓ {pname}: OK ({health.latency_ms}ms)[/green]")
            else:
                console.print(f"[red]✗ {pname}: {health.error}[/red]")

    _run(_run_test())


@advisor_app.command("login")
def advisor_login(
    name: str = typer.Argument(..., help="Advisor to login to"),
    headless: bool = typer.Option(False, "--headless", help="Don't open browser"),
):
    """Interactive login flow for an advisor."""
    info = KNOWN_ADVISORS.get(name, {})
    if info.get("free_tier"):
        console.print(f"[green]Free tier available: {info.get('free_info', '')}[/green]")

    if name == "ollama":
        console.print("Ollama doesn't require authentication. Checking connectivity...")
        base = ollama_base_url()
        try:
            import httpx
            resp = httpx.get(f"{base}/api/tags", timeout=3)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                console.print(f"[green]✓ Ollama detected! {len(models)} models available.[/green]")
            else:
                console.print("[red]Ollama returned an error.[/red]")
        except Exception:
            console.print(f"[red]Ollama not reachable at {base}.[/red]")
        return

    if name in ("google", "aws", "azure"):
        # Check for cloud CLI tools
        import shutil
        cli_tools = {"google": "gcloud", "aws": "aws", "azure": "az"}
        tool = cli_tools.get(name)
        if tool and shutil.which(tool):
            console.print(
                f"[green]Detected {tool} CLI."
                f" You can authenticate via: [bold]{tool} auth login[/bold][/green]"
            )
            console.print("Or paste an API key manually below.")

    url = info.get("url", "")
    if url:
        console.print(f"Get your API key at: [link={url}]{url}[/link]")
        if not headless:
            if typer.confirm("Open in browser?", default=True):
                webbrowser.open(url)

    key = typer.prompt(f"Paste your {name} API key", hide_input=True, default="")
    if key:
        try:
            import keyring
            keyring.set_password("nvhive", f"{name}_api_key", key)
            console.print("[green]Key stored securely in keychain.[/green]")
        except Exception:
            console.print(f"[yellow]Set {name.upper()}_API_KEY in your environment.[/yellow]")

        # Validate
        console.print("Validating key...", end=" ")
        async def _validate():
            from nvh.config.settings import load_config
            from nvh.core.engine import Engine
            config = load_config()
            if name in config.providers:
                config.providers[name].enabled = True
            engine = Engine(config=config)
            await engine.initialize()
            if engine.registry.has(name):
                health = await engine.registry.get(name).health_check()
                if health.healthy:
                    console.print(f"[green]✓ Valid! ({health.latency_ms}ms)[/green]")
                else:
                    console.print(f"[red]✗ Invalid: {health.error}[/red]")
        _run(_validate())


# ---------------------------------------------------------------------------
# hive budget
# ---------------------------------------------------------------------------

budget_app = typer.Typer(help="Budget and cost management")
app.add_typer(budget_app, name="budget", rich_help_panel="Subcommands")


@budget_app.command("status")
def budget_status():
    """Show current spending and budget limits."""
    async def _run_budget():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()

        status = await engine.get_budget_status()

        table = Table(title="Budget Status")
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")
        table.add_column("Limit", justify="right")

        daily_limit = f"${status['daily_limit']:.2f}" if status['daily_limit'] > 0 else "unlimited"
        monthly_limit = (
            f"${status['monthly_limit']:.2f}"
            if status['monthly_limit'] > 0
            else "unlimited"
        )

        table.add_row("Daily spend", f"${status['daily_spend']:.4f}", daily_limit)
        table.add_row("Monthly spend", f"${status['monthly_spend']:.4f}", monthly_limit)
        table.add_row("Daily queries", str(status['daily_queries']), "—")
        table.add_row("Monthly queries", str(status['monthly_queries']), "—")

        # Savings row — show how much was saved by running local models this month
        from nvh.storage import repository as repo
        savings = await repo.get_savings("monthly")
        table.add_row(
            "Savings (local)",
            f"${savings['total_savings']:.2f}",
            f"{savings['local_queries']} local queries",
        )

        console.print(table)

        if status['by_provider']:
            prov_table = Table(title="Spend by Advisor (Today)")
            prov_table.add_column("Advisor")
            prov_table.add_column("Spend", justify="right")
            for p, s in status['by_provider'].items():
                prov_table.add_row(p, f"${s:.4f}")
            console.print(prov_table)

    _run(_run_budget())


@app.command(rich_help_panel="Admin")
def savings():
    """Show how much money you've saved by using local models."""
    async def _run_savings():
        from nvh.storage import repository as repo
        await repo.init_db()
        data = await repo.get_savings("monthly")

        local_q = data["local_queries"]
        cloud_q = data["cloud_queries"]
        total_q = data["total_queries"]
        saved = data["total_savings"]
        actual_spend = data["cloud_spend"]
        hypothetical = actual_spend + data["estimated_cloud_cost"]
        pct = data["savings_pct"]

        lines = []
        lines.append(f"[bold]Total queries this month:[/bold]  {total_q}  "
                     f"([green]{local_q} local[/green] + [blue]{cloud_q} cloud[/blue])")
        lines.append("")

        if local_q == 0:
            lines.append("[yellow]No local model queries recorded yet.[/yellow]")
            lines.append(
                "Run queries with a local model"
                " (Ollama, LM Studio, etc.) to start saving."
            )
        else:
            lines.append(
                f"[bold green]Money saved this month:[/bold green]"
                f"       [bold]${saved:.2f}[/bold]"
            )
            lines.append(f"[dim]If you'd used cloud for everything:   ${hypothetical:.2f}[/dim]")
            lines.append(f"[dim]Your actual cloud spend:              ${actual_spend:.2f}[/dim]")
            lines.append(
                "[bold]Savings percentage:[/bold]"
                f"               [bold cyan]{pct:.1f}%[/bold cyan]"
            )
            lines.append("")
            if pct >= 80:
                lines.append(
                    "[bold green]Outstanding.[/bold green]"
                    " You're running almost everything locally. "
                    "Every dollar counts — keep it up!"
                )
            elif pct >= 50:
                lines.append(
                    "[green]Great work.[/green]"
                    " Over half your queries run free"
                    " on local hardware. "
                    "You're making your budget go further."
                )
            elif pct >= 20:
                lines.append("[yellow]Good start.[/yellow] You're saving real money. "
                             "Try routing more queries to"
                             " local models to stretch your"
                             " budget further.")
            else:
                lines.append("[dim]Tip:[/dim] Point more queries at a local model "
                             "(Ollama, LM Studio) to dramatically cut your costs.")

        panel = Panel(
            "\n".join(lines),
            title="[bold]NVHive Savings Report[/bold]",
            subtitle="[dim]Baseline: GPT-4o ($2.50/1M in, $10/1M out)[/dim]",
            border_style="green",
            padding=(1, 2),
        )
        console.print(panel)

    _run(_run_savings())


# ---------------------------------------------------------------------------
# nvh plugins
# ---------------------------------------------------------------------------

@app.command("plugins", rich_help_panel="Other")
def list_plugins():
    """List installed plugins."""
    from nvh.plugins.manager import PluginManager

    pm = PluginManager()
    found = pm.discover()

    if not found:
        console.print("[dim]No plugins found.[/dim]")
        console.print("Put .py files in ~/.hive/plugins/ or install via pip.")
        return

    table = Table(title="Plugins")
    table.add_column("Name", style="bold")
    table.add_column("Type")
    table.add_column("Source")
    table.add_column("Status")

    for p in found:
        status = "[green]OK[/green]" if not p.error else f"[red]{p.error}[/red]"
        table.add_row(p.name, p.type, p.source, status)

    console.print(table)


# ---------------------------------------------------------------------------
# nvh bench
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Admin")
def bench(
    model: str | None = typer.Option(
        None, "-m", "--model",
        help="Model to benchmark (default: current local model)",
    ),
    quick_mode: bool = typer.Option(
        False, "--quick",
        help="Run only 2 speed tests instead of 4",
    ),
    all_models: bool = typer.Option(
        False, "--all",
        help="Benchmark all loaded local models",
    ),
    quality: bool = typer.Option(
        False, "--quality", "-q",
        help="Run quality comparison (single vs council)",
    ),
    speed_only: bool = typer.Option(
        False, "--speed",
        help="GPU speed test only (skip quality)",
    ),
):
    """Benchmark — GPU speed + AI quality in one command.

    By default runs the GPU speed test (tokens/sec). Add --quality
    to also run the quality comparison (single vs council).

    Examples:
        nvh bench                  # GPU speed test
        nvh bench --quality        # speed + quality comparison
        nvh bench -q               # same, short flag
        nvh bench --speed          # speed only (skip quality)
        nvh bench -m nemotron      # benchmark specific model
        nvh bench --quick          # fast 2-test speed benchmark
    """
    async def _run_bench():
        import httpx

        from nvh.core.benchmark import (
            BENCHMARK_PROMPTS,
            COMMUNITY_BASELINES,
        )
        from nvh.providers.ollama_provider import OllamaProvider
        from nvh.utils.gpu import detect_gpus

        # Detect GPU
        gpus = detect_gpus()
        gpu_name = gpus[0].name if gpus else "CPU"
        vram_gb = gpus[0].vram_gb if gpus else 0.0

        # Discover available Ollama models
        try:
            resp = httpx.get(f"{ollama_base_url()}/api/tags", timeout=5)
            resp.raise_for_status()
            ollama_models = [m.get("name", "") for m in resp.json().get("models", [])]
        except Exception:
            ollama_models = []

        if not ollama_models:
            console.print("[red]No Ollama models found. Is Ollama running?[/red]")
            console.print("[dim]Start Ollama with: ollama serve[/dim]")
            raise typer.Exit(1)

        # Determine which models to benchmark
        if all_models:
            models_to_bench = ollama_models
        elif model:
            # Normalise — allow short names (e.g. "llama3.2-vision" matches "llama3.2-vision:latest")
            matched = [m for m in ollama_models if m == model or m.startswith(model + ":")]
            if not matched:
                console.print(f"[red]Model '{model}' not found in Ollama.[/red]")
                console.print(f"[dim]Available: {', '.join(ollama_models)}[/dim]")
                raise typer.Exit(1)
            models_to_bench = matched[:1]
        else:
            models_to_bench = [ollama_models[0]]

        # Subset of prompts for --quick
        prompts = BENCHMARK_PROMPTS[:2] if quick_mode else BENCHMARK_PROMPTS

        for bench_model in models_to_bench:
            provider = OllamaProvider(default_model=f"ollama/{bench_model}")

            # Header panel
            console.print(Panel(
                f"[bold]GPU:[/bold]   {gpu_name} ({vram_gb:.0f} GB VRAM)\n"
                f"[bold]Model:[/bold] {bench_model}",
                title="[bold cyan]NVHive GPU Benchmark[/bold cyan]",
                border_style="cyan",
                padding=(0, 2),
            ))
            console.print()

            # Progress indicator while running each test
            results_data = []
            for bp in prompts:
                console.print(f"[dim]Running: {bp['name']}...[/dim]", end="\r")
                from nvh.core.benchmark import run_single_benchmark
                result = await run_single_benchmark(
                    provider=provider,
                    model=f"ollama/{bench_model}",
                    prompt=bp["prompt"],
                    max_tokens=bp["max_tokens"],
                )
                results_data.append((bp["name"], result))

            console.print(" " * 40, end="\r")  # clear progress line

            # Build results table
            table = Table(show_header=True, header_style="bold", box=None)
            table.add_column("Test", style="", min_width=18)
            table.add_column("Tokens", justify="right", min_width=7)
            table.add_column("TTFT", justify="right", min_width=7)
            table.add_column("tok/s", justify="right", min_width=7, style="bold cyan")
            table.add_column("Time", justify="right", min_width=7)

            total_tps = 0.0
            total_ttft = 0
            for name, r in results_data:
                table.add_row(
                    name,
                    str(r.output_tokens),
                    f"{r.time_to_first_token_ms}ms",
                    f"{r.tokens_per_second:.1f}",
                    f"{r.total_time_ms / 1000:.1f}s",
                )
                total_tps += r.tokens_per_second
                total_ttft += r.time_to_first_token_ms

            n = len(results_data)
            avg_tps = total_tps / n if n else 0
            avg_ttft = total_ttft // n if n else 0

            table.add_section()
            table.add_row(
                "[bold]AVERAGE[/bold]",
                "",
                f"[bold]{avg_ttft}ms[/bold]",
                f"[bold]{avg_tps:.1f}[/bold]",
                "",
            )

            console.print(table)
            console.print()

            # Community baseline comparison
            baseline = None
            for gpu_key, baseline_tps in COMMUNITY_BASELINES.items():
                if gpu_key.lower() in gpu_name.lower() or gpu_name.lower() in gpu_key.lower():
                    baseline = (gpu_key, baseline_tps)
                    break

            if baseline:
                baseline_label, baseline_tps = baseline
                console.print(
                    f"Community average for {baseline_label}:"
                    f" ~{baseline_tps} tok/s [dim](7B Q4_K_M)[/dim]"
                )
                # Note if the model is larger than the baseline 7B
                model_short = bench_model.split(":")[0]
                is_larger = any(
                    x in model_short
                    for x in ["70b", "34b", "22b", "13b",
                              "small", "medium", "large"]
                )
                size_note = (
                    f" ({model_short} is larger than"
                    " the baseline 7B model)"
                    if is_larger else ""
                )
                console.print(f"Your result: [bold cyan]{avg_tps:.1f} tok/s[/bold cyan]{size_note}")
                console.print()

                # Star rating: ratio of result vs baseline
                # (adjusted: larger models expected to be slower)
                ratio = avg_tps / baseline_tps
                if ratio >= 1.2:
                    stars, label = 5, "Outstanding"
                elif ratio >= 0.9:
                    stars, label = 4, "Excellent"
                elif ratio >= 0.65:
                    stars, label = 3, "Good"
                elif ratio >= 0.4:
                    stars, label = 2, "Fair"
                else:
                    stars, label = 1, "Below average"

                star_str = "⭐" * stars + "☆" * (5 - stars)
                console.print(f"Rating: {star_str} [bold]{label}[/bold] for this model size")
            else:
                console.print(
                    f"[bold cyan]Result: {avg_tps:.1f} tok/s[/bold cyan]"
                    " [dim](no community baseline for this GPU)[/dim]"
                )

            console.print()

    _run(_run_bench())

    # Run quality benchmark if requested
    if quality and not speed_only:
        from nvh.core.quality_benchmark import (
            BenchmarkMode,
            QualityBenchmarkRunner,
            QualityJudge,
            generate_markdown_report,
            load_dataset,
        )

        async def _run_quality():
            from nvh.config.settings import load_config
            from nvh.core.engine import Engine

            config = load_config()
            engine = Engine(config=config)
            await engine.initialize()

            prompts = load_dataset()
            if not prompts:
                console.print(
                    "[red]No benchmark prompts found.[/red]",
                )
                return

            modes_list = [
                BenchmarkMode.SINGLE,
                BenchmarkMode.COUNCIL_FREE,
            ]
            qj = QualityJudge(engine, judge_provider="auto")
            runner = QualityBenchmarkRunner(
                engine, qj, prompts,
            )

            def on_progress(current, total, pid):
                console.print(
                    f"  [dim][{current}/{total}]"
                    f" {pid}[/dim]",
                )

            console.print(
                "\n[bold]Quality Benchmark[/bold]\n",
            )
            report = await runner.run(
                modes=modes_list,
                temperature=0.0,
                max_tokens=2048,
                on_progress=on_progress,
                pace_seconds=15.0,
            )

            _display_benchmark_table(report)

            ep = Path.home() / ".hive" / "benchmark_results.md"
            ep.parent.mkdir(parents=True, exist_ok=True)
            ep.write_text(generate_markdown_report(report))
            console.print(
                f"\n[green]Results saved to {ep}[/green]",
            )

        _run(_run_quality())


# ---------------------------------------------------------------------------
# nvh benchmark (quality) — also callable via nvh bench --quality
# ---------------------------------------------------------------------------


@app.command(hidden=True, rich_help_panel="Admin")
def benchmark(
    mode: str = typer.Option(
        "single,council-free", "-m", "--mode",
        help=(
            "Modes: single, council-free,"
            " council-premium, throwdown, all"
        ),
    ),
    models: str | None = typer.Option(
        None, "--models",
        help="Comma-separated single models to benchmark",
    ),
    council: str | None = typer.Option(
        None, "--council",
        help="Comma-separated council members override",
    ),
    dataset: str | None = typer.Option(
        None, "-d", "--dataset",
        help="Path to custom benchmark YAML",
    ),
    judge: str = typer.Option(
        "auto", "-j", "--judge",
        help="Judge provider: auto, local, or provider name",
    ),
    output: str = typer.Option(
        "table", "-o", "--output",
        help="Output format: table, json, markdown",
    ),
    export_path: str | None = typer.Option(
        None, "--export",
        help="Export results to file",
    ),
    task_types: str | None = typer.Option(
        None, "--tasks",
        help="Filter by task types (comma-separated)",
    ),
    temperature: float = typer.Option(
        0.0, "-t", "--temperature",
    ),
    max_tokens: int = typer.Option(
        2048, "--max-tokens",
    ),
    store: bool = typer.Option(
        True, "--store/--no-store",
        help="Store results in database",
    ),
    profile: str | None = typer.Option(
        None, "--profile",
    ),
):
    """Quality benchmark — compare single model vs council.

    Prefer using: nvh bench --quality (or nvh bench -q)

    Runs prompts through single and council modes, scores each
    response using a blind LLM judge on quality dimensions.

    Examples:
        nvh bench -q                         # recommended way
        nvh benchmark                        # also works
        nvh benchmark --tasks code_generation
    """
    from nvh.core.quality_benchmark import (
        BenchmarkMode,
        QualityBenchmarkRunner,
        QualityJudge,
        generate_json_report,
        generate_markdown_report,
        load_dataset,
    )

    async def _run_benchmark():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config(profile=profile)
        engine = Engine(config=config)
        await engine.initialize()

        # Load dataset
        ds_path = Path(dataset) if dataset else None
        prompts = load_dataset(ds_path)
        if not prompts:
            console.print("[red]No benchmark prompts found.[/red]")
            raise typer.Exit(1)

        # Parse modes
        mode_map = {
            "single": BenchmarkMode.SINGLE,
            "council-free": BenchmarkMode.COUNCIL_FREE,
            "council-premium": BenchmarkMode.COUNCIL_PREMIUM,
            "throwdown": BenchmarkMode.THROWDOWN,
        }
        if mode == "all":
            modes_list = list(BenchmarkMode)
        else:
            parsed = [
                m.strip() for m in mode.split(",")
            ]
            modes_list = []
            for m in parsed:
                if m not in mode_map:
                    console.print(
                        f"[red]Unknown mode '{m}'.[/red]"
                        f" Options: {', '.join(mode_map)}"
                    )
                    raise typer.Exit(1)
                modes_list.append(mode_map[m])

        # Parse filters
        single_provs = (
            [p.strip() for p in models.split(",")]
            if models else None
        )
        council_members = (
            [p.strip() for p in council.split(",")]
            if council else None
        )
        task_filter = (
            [t.strip() for t in task_types.split(",")]
            if task_types else None
        )

        # Setup judge and runner
        qj = QualityJudge(engine, judge_provider=judge)
        runner = QualityBenchmarkRunner(engine, qj, prompts)

        # Progress callback
        def on_progress(current, total, prompt_id):
            console.print(
                f"  [dim][{current}/{total}]"
                f" {prompt_id}[/dim]",
            )

        console.print(
            f"\n[bold]nvHive Quality Benchmark[/bold]"
            f"\n  Prompts: {len(prompts)}"
            f" | Modes: {', '.join(str(m) for m in modes_list)}"
            f" | Judge: {judge}\n"
        )

        report = await runner.run(
            modes=modes_list,
            single_providers=single_provs,
            council_free_members=council_members,
            council_premium_members=council_members,
            temperature=temperature,
            max_tokens=max_tokens,
            task_types=task_filter,
            on_progress=on_progress,
        )

        # Store results
        if store:
            import json as _json

            from nvh.storage import repository as repo
            for pr in report.results:
                for ev in pr.evaluations:
                    scores = {
                        ds.dimension: ds.score
                        for ds in ev.dimension_scores
                    }
                    await repo.log_quality_benchmark(
                        run_id=report.run_id,
                        prompt_id=ev.prompt_id,
                        task_type=pr.prompt.task_type,
                        mode=ev.mode,
                        provider=ev.provider,
                        model=ev.model,
                        overall_score=ev.overall_score,
                        cost_usd=ev.cost_usd,
                        latency_ms=ev.latency_ms,
                        input_tokens=ev.input_tokens,
                        output_tokens=ev.output_tokens,
                        scores_json=_json.dumps(scores),
                    )

        # Display results
        if output == "json":
            console.print(generate_json_report(report))
        elif output == "markdown":
            console.print(
                generate_markdown_report(report),
            )
        else:
            # Rich table output
            _display_benchmark_table(report)

        # Export — auto-save to ~/.hive/ if no path specified
        ep = Path(export_path) if export_path else (
            Path.home() / ".hive" / "benchmark_results.md"
        )
        ep.parent.mkdir(parents=True, exist_ok=True)
        if ep.suffix == ".json":
            ep.write_text(generate_json_report(report))
        else:
            ep.write_text(
                generate_markdown_report(report),
            )
        console.print(
            f"\n[green]Results saved to {ep}[/green]",
        )

        console.print(
            f"\n[dim]Run ID: {report.run_id}"
            f" | Cost: ${report.total_cost_usd:.4f}"
            f" | Duration:"
            f" {report.total_duration_ms / 1000:.1f}s[/dim]\n"
        )

    _run(_run_benchmark())


def _display_benchmark_table(report):
    """Display benchmark results as a Rich table."""
    from nvh.core.quality_benchmark import _MODE_DISPLAY

    if not report.summary:
        console.print("[yellow]No results to display.[/yellow]")
        return

    table = Table(
        title="Quality Benchmark Results",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Mode", style="bold")
    table.add_column("Accuracy", justify="right")
    table.add_column("Complete", justify="right")
    table.add_column("Actionable", justify="right")
    table.add_column("Coherence", justify="right")
    table.add_column("Overall", justify="right", style="bold")
    table.add_column("Avg Cost", justify="right")

    for mode_key, scores in report.summary.items():
        display = _MODE_DISPLAY.get(mode_key, mode_key)
        overall = scores.get("overall", 0)
        # Color overall score
        if overall >= 8.5:
            ov_str = f"[bold green]{overall:.1f}[/bold green]"
        elif overall >= 7.0:
            ov_str = f"[green]{overall:.1f}[/green]"
        elif overall >= 5.0:
            ov_str = f"[yellow]{overall:.1f}[/yellow]"
        else:
            ov_str = f"[red]{overall:.1f}[/red]"

        cost = scores.get("avg_cost", 0)
        cost_str = (
            "[green]$0.0000[/green]"
            if cost == 0
            else f"${cost:.4f}"
        )

        table.add_row(
            display,
            f"{scores.get('accuracy', 0):.1f}",
            f"{scores.get('completeness', 0):.1f}",
            f"{scores.get('actionability', 0):.1f}",
            f"{scores.get('coherence', 0):.1f}",
            ov_str,
            cost_str,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# hive model
# ---------------------------------------------------------------------------

model_app = typer.Typer(help="Browse available models")
app.add_typer(model_app, name="model", rich_help_panel="Subcommands")


@model_app.command("list")
def model_list(
    provider: str | None = typer.Option(None, "-p", "--advisor", help="Filter by advisor"),
):
    """List all available models from the capability catalog."""
    from nvh.providers.registry import get_registry

    registry = get_registry()
    registry.load_capabilities()

    models = registry.list_models(provider=provider)

    table = Table(title="Available Models")
    table.add_column("Model ID", style="bold")
    table.add_column("Provider")
    table.add_column("Context")
    table.add_column("In $/1M", justify="right")
    table.add_column("Out $/1M", justify="right")
    table.add_column("Latency", justify="right")
    table.add_column("Vision")
    table.add_column("Tools")

    for m in sorted(models, key=lambda x: x.provider):
        table.add_row(
            m.model_id,
            m.provider,
            f"{m.context_window:,}",
            f"${m.input_cost_per_1m_tokens}",
            f"${m.output_cost_per_1m_tokens}",
            f"{m.typical_latency_ms}ms",
            "✓" if m.supports_vision else "—",
            "✓" if m.supports_tools else "—",
        )

    console.print(table)


# ---------------------------------------------------------------------------
# hive agent
# ---------------------------------------------------------------------------

agent_app = typer.Typer(
    help="Coding agent (run) and the expert personas it draws on (presets, analyze).",
)
app.add_typer(agent_app, name="agent", rich_help_panel="Subcommands")


@agent_app.command("presets")
def agent_presets():
    """List available hive cabinets and their expert roles."""
    from nvh.core.agents import list_presets

    presets = list_presets()
    table = Table(title="Hive Cabinets")
    table.add_column("Cabinet", style="bold")
    table.add_column("Expert Roles")

    for name, roles in presets.items():
        table.add_row(name, ", ".join(roles))

    console.print(table)


@agent_app.command("analyze")
def agent_analyze(
    prompt: str = typer.Argument(..., help="Query to analyze for agent generation"),
    num: int = typer.Option(5, "-n", "--num", help="Number of agents to generate"),
):
    """Preview which expert agents would be generated for a given query."""
    from nvh.core.agents import generate_agents

    agents = generate_agents(prompt, num_agents=num)

    console.print(f"[bold]Auto-generated hive for:[/bold] {prompt[:100]}...\n")

    table = Table(title=f"{len(agents)} Expert Agents")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Role", style="bold cyan")
    table.add_column("Expertise")
    table.add_column("Perspective")
    table.add_column("Boost", justify="right")

    for i, agent in enumerate(agents, 1):
        table.add_row(
            str(i),
            agent.role,
            agent.expertise[:60] + "..." if len(agent.expertise) > 60 else agent.expertise,
            agent.perspective[:60] + "..." if len(agent.perspective) > 60 else agent.perspective,
            f"+{agent.weight_boost:.0%}" if agent.weight_boost > 0 else "—",
        )

    console.print(table)

    console.print("\n[dim]Run: hive convene \"<query>\" --auto-agents to use these agents[/dim]")
    console.print("[dim]Run: hive convene \"<query>\" --cabinet <name> to use a cabinet[/dim]")


# ---------------------------------------------------------------------------
# hive repl
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Other")
def repl(
    provider: str | None = typer.Option(None, "-p", "--advisor", help="Starting advisor"),
    model: str | None = typer.Option(None, "-m", "--model", help="Starting model"),
    council_mode: bool = typer.Option(False, "--convene", help="Start in hive mode"),
    auto_agents: bool = typer.Option(
        False, "-a", "--auto-agents",
        help="Enable auto-agent generation",
    ),
    preset: str | None = typer.Option(None, "--cabinet", help="Agent cabinet to use"),
    system: str | None = typer.Option(None, "-s", "--system", help="System prompt"),
    profile: str | None = typer.Option(None, "--profile", help="Config profile"),
):
    """Launch interactive REPL with multi-turn conversation support."""
    async def _run_repl():
        from nvh.cli.repl import run_repl
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config(profile=profile)
        engine = Engine(config=config)

        await run_repl(
            engine=engine,
            provider=provider,
            model=model,
            council_mode=council_mode,
            auto_agents=auto_agents,
            preset=preset,
            system_prompt=system,
        )

    _run(_run_repl())


# ---------------------------------------------------------------------------
# hive webhook
# ---------------------------------------------------------------------------

webhook_app = typer.Typer(help="Manage webhook notifications")
app.add_typer(webhook_app, name="webhook", rich_help_panel="Subcommands")


@webhook_app.command("list")
def webhook_list():
    """Show configured webhooks."""
    from nvh.config.settings import load_config

    config = load_config()
    if not config.webhooks:
        console.print("[dim]No webhooks configured.[/dim]")
        console.print("\nAdd webhooks in your config file under the [bold]webhooks:[/bold] key:")
        console.print("  webhooks:")
        console.print("    - url: https://example.com/hook")
        console.print("      events: [budget.threshold_reached, provider.circuit_open]")
        console.print("      secret: my-signing-secret")
        return

    table = Table(title="Configured Webhooks")
    table.add_column("URL", style="bold")
    table.add_column("Events")
    table.add_column("Secret")
    table.add_column("Enabled")

    for wh in config.webhooks:
        events_str = ", ".join(wh.events) if wh.events else "[dim]all[/dim]"
        secret_str = "***" if wh.secret else "[dim]none[/dim]"
        enabled_str = "[green]yes[/green]" if wh.enabled else "[dim]no[/dim]"
        table.add_row(wh.url, events_str, secret_str, enabled_str)

    console.print(table)


@webhook_app.command("test")
def webhook_test(
    url: str = typer.Argument(..., help="Webhook URL to send test payload to"),
    secret: str = typer.Option("", "--secret", "-s", help="HMAC signing secret"),
):
    """Send a test payload to a webhook URL."""
    async def _run_test():
        import time

        from nvh.core.webhooks import WebhookConfig, WebhookEvent, WebhookManager

        manager = WebhookManager()
        manager.register(WebhookConfig(
            url=url,
            events=[],
            secret=secret,
        ))

        hook = manager._hooks[0]
        from nvh.core.webhooks import WebhookPayload
        payload = WebhookPayload(
            event=WebhookEvent.QUERY_COMPLETE,
            timestamp=time.time(),
            data={"message": "This is a test webhook from Hive.", "url": url},
        )

        console.print(f"Sending test webhook to [bold]{url}[/bold]...")
        success = await manager._dispatch(hook, payload)
        if success:
            console.print("[green]Webhook delivered successfully.[/green]")
        else:
            console.print("[red]Webhook delivery failed.[/red]")
            raise typer.Exit(1)

    _run(_run_test())


@webhook_app.command("add")
def webhook_add(
    url: str = typer.Argument(..., help="Webhook endpoint URL"),
    events: str = typer.Option(
        "budget.threshold_reached,provider.circuit_open",
        "--events",
        "-e",
        help="Comma-separated event types",
    ),
    secret: str = typer.Option("", "--secret", "-s", help="HMAC-SHA256 signing secret"),
):
    """Add a webhook to the configuration file."""
    from nvh.config.settings import load_config, save_config

    config = load_config()
    from nvh.config.settings import WebhookConfigModel
    event_list = [e.strip() for e in events.split(",") if e.strip()]
    new_wh = WebhookConfigModel(url=url, events=event_list, secret=secret)

    # Check for duplicate URL
    if any(wh.url == url for wh in config.webhooks):
        console.print(
            f"[yellow]Webhook with URL '{url}'"
            " already exists. Updating events/secret.[/yellow]"
        )
        config.webhooks = [wh if wh.url != url else new_wh for wh in config.webhooks]
    else:
        config.webhooks.append(new_wh)

    save_config(config)
    console.print(f"[green]Webhook added:[/green] {url}")
    console.print(f"  Events: {', '.join(event_list) or 'all'}")
    console.print(f"  Secret: {'set' if secret else 'none'}")


# ---------------------------------------------------------------------------
# hive auth
# ---------------------------------------------------------------------------

auth_app = typer.Typer(help="User authentication management")
app.add_typer(auth_app, name="auth", rich_help_panel="Subcommands")


@auth_app.command("create-user")
def auth_create_user(
    username: str = typer.Argument(..., help="Username for the new user"),
    role: str = typer.Option("user", "--role", "-r", help="Role: admin, user, viewer"),
    email: str | None = typer.Option(None, "--email", "-e", help="Email address (optional)"),
):
    """Create a new user account (prompts for password)."""
    async def _create():
        from nvh.auth.auth import create_user, get_user_count
        from nvh.core.engine import Engine

        engine = Engine()
        await engine.initialize()

        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
        if len(password) < 8:
            console.print("[red]Password must be at least 8 characters.[/red]")
            raise typer.Exit(1)

        count = await get_user_count()
        effective_role = "admin" if count == 0 else role

        try:
            user = await create_user(
                username=username,
                password=password,
                role=effective_role,
                email=email,
            )
        except ValueError as exc:
            console.print(f"[red]Error: {exc}[/red]")
            raise typer.Exit(1)

        console.print(
            f"[green]User created:[/green] {user.username}"
            f" (role: {user.role}, id: {user.id})"
        )
        if count == 0:
            console.print("[yellow]First user automatically granted admin role.[/yellow]")

    _run(_create())


@auth_app.command("create-token")
def auth_create_token(
    name: str = typer.Argument(..., help="Token name/description, e.g. 'CI pipeline'"),
    username: str = typer.Option(..., "--username", "-u", prompt=True, help="Your username"),
    scopes: str = typer.Option("ask,convene,poll", "--scopes", help="Comma-separated scopes"),
):
    """Create an API token for a user (prints it once — save it immediately)."""
    async def _create():
        from nvh.auth.auth import authenticate_user, create_token_for_user
        from nvh.core.engine import Engine

        engine = Engine()
        await engine.initialize()

        password = typer.prompt("Password", hide_input=True)
        user = await authenticate_user(username, password)
        if user is None:
            console.print("[red]Invalid credentials.[/red]")
            raise typer.Exit(1)

        raw_token, token_record = await create_token_for_user(
            user_id=user.id,
            name=name,
            scopes=scopes,
        )

        console.print(
            f"\n[green]API token created:[/green]"
            f" {token_record.name} (id: {token_record.id})"
        )
        console.print("\n[bold yellow]Token (shown once — copy now):[/bold yellow]")
        console.print(f"\n  {raw_token}\n")
        console.print("[dim]Use this token as: Authorization: Bearer <token>[/dim]")

    _run(_create())


@auth_app.command("list-tokens")
def auth_list_tokens(
    username: str = typer.Option(..., "--username", "-u", prompt=True, help="Your username"),
):
    """List your active API tokens."""
    async def _list():
        from nvh.auth.auth import authenticate_user, list_user_tokens
        from nvh.core.engine import Engine

        engine = Engine()
        await engine.initialize()

        password = typer.prompt("Password", hide_input=True)
        user = await authenticate_user(username, password)
        if user is None:
            console.print("[red]Invalid credentials.[/red]")
            raise typer.Exit(1)

        tokens = await list_user_tokens(user.id)
        if not tokens:
            console.print("[dim]No active tokens.[/dim]")
            return

        table = Table(title=f"API Tokens for {username}")
        table.add_column("ID", style="dim")
        table.add_column("Name", style="bold")
        table.add_column("Scopes")
        table.add_column("Created")
        table.add_column("Last Used")

        for t in tokens:
            last_used = t.last_used.strftime("%Y-%m-%d %H:%M") if t.last_used else "—"
            created = t.created_at.strftime("%Y-%m-%d %H:%M")
            table.add_row(t.id[:8] + "...", t.name, t.scopes, created, last_used)

        console.print(table)

    _run(_list())


@auth_app.command("revoke-token")
def auth_revoke_token(
    token_id: str = typer.Argument(..., help="Token ID to revoke"),
    username: str = typer.Option(..., "--username", "-u", prompt=True, help="Your username"),
):
    """Revoke an API token by its ID."""
    async def _revoke():
        from nvh.auth.auth import authenticate_user, revoke_token
        from nvh.core.engine import Engine

        engine = Engine()
        await engine.initialize()

        password = typer.prompt("Password", hide_input=True)
        user = await authenticate_user(username, password)
        if user is None:
            console.print("[red]Invalid credentials.[/red]")
            raise typer.Exit(1)

        revoked = await revoke_token(token_id)
        if revoked:
            console.print(f"[green]Token {token_id} has been revoked.[/green]")
        else:
            console.print(f"[red]Token {token_id} not found.[/red]")
            raise typer.Exit(1)

    _run(_revoke())


# ---------------------------------------------------------------------------
# hive serve
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Infrastructure")
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address"),
    port: int = typer.Option(8000, "--port", help="Port number"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (dev mode)"),
    daemon: bool = typer.Option(
        False, "--daemon",
        help="Install as system service (auto-start on boot)",
    ),
):
    """Start the REST API server.

    Use --daemon to install as a persistent service that starts on boot.
    """
    if daemon:
        import sys as _sys

        from nvh.integrations.services.service import (
            install_launchd_service,
            install_systemd_service,
        )
        console.print("[bold]Installing nvHive proxy as a system service...[/bold]")
        if _sys.platform == "darwin":
            ok, msg = install_launchd_service(host, port)
        else:
            ok, msg = install_systemd_service(host, port)
        if ok:
            console.print(f"[green]✓[/green] {msg}")
            console.print(f"  Proxy will auto-start on boot at http://{host}:{port}")
            console.print("  Manage with: [bold]nvh service status|stop|uninstall[/bold]")
        else:
            console.print(f"[red]✗[/red] {msg}")
        return

    if not _check_serve_deps():
        raise typer.Exit(1)
    from nvh.api.server import run_server
    from nvh.integrations.workspace.hostname import is_hostname_configured
    host_label = "nvhive" if is_hostname_configured() else host
    console.print(f"[bold]Hive API Server[/bold] starting on http://{host_label}:{port}")
    console.print(f"  API docs: http://{host_label}:{port}/docs")
    console.print()
    run_server(host=host, port=port, reload=reload)


@app.command(rich_help_panel="Infrastructure")
def service(
    action: str = typer.Argument("status", help="Action: status, stop, uninstall"),
):
    """Manage the nvHive proxy background service.

    Examples:
        nvh service              Check if proxy service is running
        nvh service status       Same as above
        nvh service stop         Stop the service (keeps it installed)
        nvh service uninstall    Remove the service completely
    """
    from nvh.integrations.services.service import service_status, uninstall_service

    if action == "status":
        running, msg = service_status()
        if running:
            console.print(
                f"  [green]✓[/green] nvHive proxy service:"
                f" [bold green]{msg}[/bold green]"
            )
        else:
            console.print(f"  [yellow]○[/yellow] nvHive proxy service: [bold]{msg}[/bold]")
            if msg == "Not installed":
                console.print("  Install with: [bold]nvh serve --daemon[/bold]")

    elif action == "stop":
        import subprocess
        import sys as _sys
        if _sys.platform == "darwin":
            subprocess.run(["launchctl", "unload",
                           str(
                               Path.home() / "Library"
                               / "LaunchAgents"
                               / "com.nvhive.proxy.plist"
                           )],
                          capture_output=True)
        else:
            subprocess.run(["systemctl", "--user", "stop", "nvhive-proxy"], capture_output=True)
        console.print("  [green]✓[/green] Service stopped")

    elif action == "uninstall":
        ok, msg = uninstall_service()
        if ok:
            console.print(f"  [green]✓[/green] {msg}")
        else:
            console.print(f"  [yellow]○[/yellow] {msg}")

    else:
        console.print(f"  [red]Unknown action: {action}[/red]")
        console.print("  Use: status, stop, uninstall")


# ---------------------------------------------------------------------------
# hive integrate — auto-detect and configure all platforms
# ---------------------------------------------------------------------------


@app.command(rich_help_panel="Admin")
def migrate(
    source: str = typer.Option(
        "auto", "--from",
        help="Source to migrate from: auto, openclaw, claw-code",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Show what would be imported without making changes",
    ),
):
    """Migrate to nvHive from OpenClaw, Claw Code, or other AI tools.

    Detects existing configurations and imports API keys, provider
    settings, and MCP configurations so you're up and running fast.

    Especially useful for OpenClaw users affected by the new API billing
    — nvHive routes across local and cloud providers so you're never locked
    into one provider's pricing.

    Examples:
        nvh migrate                     # auto-detect and import
        nvh migrate --from openclaw     # import from OpenClaw
        nvh migrate --dry-run           # preview without changes
    """
    import json as _json
    from pathlib import Path as _Path

    console.print("[bold]nvHive Migration Tool[/bold]\n")

    found_sources: list[dict] = []

    # --- Detect OpenClaw ---
    openclaw_paths = [
        _Path.home() / ".openclaw" / "config.json",
        _Path.home() / ".config" / "openclaw" / "config.json",
        _Path("openclaw.json"),
    ]
    for p in openclaw_paths:
        if p.exists():
            found_sources.append({"type": "openclaw", "path": p})
            break

    # --- Detect Claw Code ---
    claw_paths = [
        _Path.home() / ".claw" / "config.json",
        _Path.home() / ".config" / "claw-code" / "settings.json",
    ]
    for p in claw_paths:
        if p.exists():
            found_sources.append({"type": "claw-code", "path": p})
            break

    # --- Detect Claude Code MCP configs ---
    claude_mcp = _Path.home() / ".claude" / "claude_desktop_config.json"
    if claude_mcp.exists():
        found_sources.append({"type": "claude-desktop", "path": claude_mcp})

    # --- Detect environment variables from other tools ---
    env_keys: dict[str, str] = {}
    _key_map = {
        "OPENAI_API_KEY": "openai",
        "ANTHROPIC_API_KEY": "anthropic",
        "GROQ_API_KEY": "groq",
        "GOOGLE_API_KEY": "google",
        "MISTRAL_API_KEY": "mistral",
        "COHERE_API_KEY": "cohere",
        "XAI_API_KEY": "grok",
        "DEEPSEEK_API_KEY": "deepseek",
        "FIREWORKS_API_KEY": "fireworks",
        "TOGETHER_API_KEY": "together",
    }
    for env_var, provider_name in _key_map.items():
        val = os.environ.get(env_var, "")
        if val:
            env_keys[provider_name] = env_var

    if source != "auto":
        found_sources = [s for s in found_sources if s["type"] == source]

    # Report findings
    if not found_sources and not env_keys:
        console.print("[yellow]No existing AI tool configurations found.[/yellow]\n")
        console.print("  nvHive checked for: OpenClaw, Claw Code, Claude Desktop configs")
        console.print("  and common API key environment variables.\n")
        console.print("  Get started from scratch: [bold]nvh setup[/bold]")
        return

    # Show what was found
    console.print("[green]Found the following:[/green]\n")

    if found_sources:
        for src in found_sources:
            console.print(f"  Config: [bold]{src['type']}[/bold] at {src['path']}")

    if env_keys:
        console.print(
            "  API keys in environment:"
            f" [bold]{', '.join(sorted(env_keys.keys()))}[/bold]"
        )

    console.print()

    if dry_run:
        console.print("[dim]Dry run — no changes made.[/dim]")
        return

    # Import from found configs
    imported = 0

    for src in found_sources:
        try:
            config_data = _json.loads(src["path"].read_text())
        except Exception as e:
            console.print(f"  [yellow]Could not read {src['path']}: {e}[/yellow]")
            continue

        if src["type"] in ("openclaw", "claw-code"):
            # Extract MCP server configs
            mcp_servers = config_data.get("mcpServers", {})
            if mcp_servers:
                console.print(f"  Found {len(mcp_servers)} MCP server(s) in {src['type']} config")

            # Extract API keys
            providers = config_data.get("providers", config_data.get("apiKeys", {}))
            for pname, pdata in providers.items():
                key = (
                    pdata if isinstance(pdata, str)
                    else pdata.get("api_key", pdata.get("apiKey", ""))
                )
                if key:
                    try:
                        import keyring
                        keyring.set_password("nvhive", f"{pname}_api_key", key)
                        console.print(f"  [green]✓[/green] Imported {pname} API key")
                        imported += 1
                    except Exception:
                        console.print(
                            f"  [yellow]![/yellow] {pname}:"
                            f" set {pname.upper()}_API_KEY"
                            " in your environment"
                        )

    # Register environment keys
    if env_keys:
        for provider_name, env_var in env_keys.items():
            try:
                import keyring
                existing = keyring.get_password("nvhive", f"{provider_name}_api_key")
                if existing:
                    console.print(
                        f"  [dim]Skipped {provider_name}"
                        " — already configured in nvHive[/dim]"
                    )
                else:
                    key = os.environ[env_var]
                    keyring.set_password("nvhive", f"{provider_name}_api_key", key)
                    console.print(f"  [green]✓[/green] Imported {provider_name} from ${env_var}")
                    imported += 1
            except Exception:
                console.print(
                    f"  [green]✓[/green] {provider_name}"
                    f" available via ${env_var}"
                    " (will use env var directly)"
                )
                imported += 1

    console.print(
        f"\n[bold green]Migration complete![/bold green]"
        f" {imported} provider(s) imported.\n"
    )
    console.print("  [bold]Next steps:[/bold]")
    console.print("    Verify:    [bold]nvh status --smoke[/bold]")
    console.print("    Try it:    [bold]nvh \"Hello from nvHive!\"[/bold]")
    console.print("    Dashboard: [bold]nvh webui[/bold]")
    console.print()
    console.print(
        "  [dim]nvHive routes across local and cloud providers"
        " — no more single-provider lock-in.[/dim]"
    )


# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Admin")
def integrate(
    auto: bool = typer.Option(
        False, "--auto", "-y",
        help="Auto-configure all detected platforms without prompting",
    ),
    scan_only: bool = typer.Option(False, "--scan", help="Just scan — don't configure anything"),
):
    """Auto-detect and configure nvHive with all installed AI platforms.

    Scans for NemoClaw, OpenClaw, Claude Code, Cursor, and Claude Desktop,
    then offers to register nvHive with each one automatically.

    Examples:
        nvh integrate          Scan and prompt for each platform
        nvh integrate --auto   Configure everything without prompting
        nvh integrate --scan   Just show what's installed
    """
    from rich.rule import Rule

    from nvh.integrations.diagnostics.detector import (
        detect_platforms,
        register_claude_code,
        register_claude_desktop,
        register_cursor,
        register_nemoclaw,
        register_openclaw,
    )

    console.print()
    console.print(Panel(
        "[bold]NVHive Auto-Integration[/bold]\n"
        "Scanning for AI platforms to connect with nvHive...",
        border_style="blue",
    ))
    console.print()

    platforms = detect_platforms()
    detected = [p for p in platforms if p.detected]
    not_detected = [p for p in platforms if not p.detected]

    # --- Show scan results ---
    console.print(Rule("Detected Platforms"))
    console.print()

    if not detected:
        console.print("  [yellow]No external AI platforms detected.[/yellow]")
        console.print()
        console.print("  [bold]That's OK![/bold] nvHive works great standalone:")
        console.print('    [dim]$[/dim] nvh "What is machine learning?"')
        console.print('    [dim]$[/dim] nvh convene "Should we use Rust or Go?"')
        console.print('    [dim]$[/dim] nvh throwdown "Best database for SaaS?"')
        console.print()
        console.print("  Want to connect nvHive to other tools later? Install any of:")
        console.print()

        integ_table = Table(show_header=True, header_style="bold", padding=(0, 2))
        integ_table.add_column("Platform")
        integ_table.add_column("Install")
        integ_table.add_column("Then run")
        integ_table.add_row("NemoClaw", "nvh nemoclaw --install", "nvh nemoclaw --test")
        integ_table.add_row("OpenClaw", "nvh openclaw --install", "nvh openclaw --test")
        integ_table.add_row(
            "Claude Code",
            "npm i -g @anthropic/claude-code",
            "nvh integrate --auto",
        )
        integ_table.add_row("Cursor", "https://cursor.com", "nvh integrate --auto")
        console.print(integ_table)
        console.print()
        console.print("  After installing, run [bold]nvh integrate[/bold] again to auto-configure.")
        console.print()
        return

    for p in detected:
        status = (
            "[green]✓ configured[/green]"
            if p.already_configured
            else "[yellow]○ not configured[/yellow]"
        )
        console.print(f"  [green]✓[/green] [bold]{p.display_name}[/bold] — {status}")
        console.print(f"    [dim]{p.detection_method}[/dim]")
        if p.integration_type == "mcp":
            console.print("    [dim]Integration: MCP tool server[/dim]")
        else:
            console.print("    [dim]Integration: Inference provider (proxy)[/dim]")
        for note in p.notes:
            console.print(f"    [dim]{note}[/dim]")

    if not_detected:
        console.print()
        console.print("  [dim]Not found:[/dim]", end="")
        console.print(f" [dim]{', '.join(p.display_name for p in not_detected)}[/dim]")

    console.print()

    if scan_only:
        return

    # --- Configure each detected platform ---
    to_configure = [p for p in detected if not p.already_configured]

    if not to_configure:
        console.print("  [bold green]All detected platforms are already configured![/bold green]")
        console.print()
        return

    console.print(Rule("Configure Integrations"))
    console.print()

    registered = 0
    for p in to_configure:
        if not auto:
            confirm = typer.confirm(f"  Configure {p.display_name}?", default=True)
            if not confirm:
                console.print(f"  [dim]Skipped {p.display_name}[/dim]")
                continue

        success = False
        msg = ""

        if p.name == "nemoclaw":
            success, msg = register_nemoclaw()
        elif p.name == "openclaw":
            success, msg = register_openclaw(p.config_path or None)
        elif p.name == "claude_code":
            success, msg = register_claude_code()
        elif p.name == "cursor":
            success, msg = register_cursor(p.config_path or None)
        elif p.name == "claude_desktop":
            success, msg = register_claude_desktop()

        if success:
            console.print(f"  [green]✓[/green] {p.display_name}: {msg}")
            registered += 1
        else:
            console.print(f"  [red]✗[/red] {p.display_name}: {msg}")

    console.print()
    if registered:
        console.print(f"  [bold green]{registered} platform(s) configured![/bold green]")

        # Check if any MCP platforms were configured
        mcp_platforms = [
            p for p in to_configure
            if p.integration_type == "mcp"
            and not p.already_configured
        ]
        proxy_platforms = [
            p for p in to_configure
            if p.integration_type == "inference"
            and not p.already_configured
        ]

        if mcp_platforms:
            console.print()
            console.print("  MCP tools are available immediately (stdio transport).")
            console.print("  Your agent will spawn nvHive automatically when needed.")
        if proxy_platforms:
            console.print()
            console.print("  Start the proxy for NemoClaw: [bold]nvh nemoclaw --start[/bold]")
    console.print()


# ---------------------------------------------------------------------------
# hive openclaw — OpenClaw integration setup
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Infrastructure")
def openclaw(
    test: bool = typer.Option(False, "--test", help="Test if the MCP server is reachable"),
    start: bool = typer.Option(False, "--start", help="Start the MCP server for OpenClaw"),
    config: bool = typer.Option(False, "--config", help="Generate openclaw.json config file"),
    install: bool = typer.Option(
        False, "--install",
        help="Install OpenClaw, register nvHive MCP server, and run a smoke test",
    ),
    output: str | None = typer.Option(None, "-o", "--output", help="Output path for openclaw.json"),
    http: bool = typer.Option(False, "--http", help="Use HTTP transport instead of stdio"),
    port: int = typer.Option(8080, "--port", help="Port for HTTP transport"),
):
    """OpenClaw / NemoClaw integration — multi-LLM routing for agents.

    Anthropic dropped OpenClaw support. nvHive replaces that path
    and gives agents access to every configured provider, local GPU inference,
    and council consensus — more than OpenClaw provided.

    For NemoClaw users: nvHive plugs directly into OpenShell Gateway.
    No OpenClaw dependency. Run `nvh nemoclaw --start` for NemoClaw.

    Examples:
        nvh openclaw              Show setup + migration info
        nvh openclaw --install    Install OpenClaw + register nvHive + smoke test
        nvh openclaw --test       Test MCP server connectivity
        nvh openclaw --start      Start the MCP server
        nvh openclaw --config     Generate openclaw.json
    """
    from rich.rule import Rule

    console.print()
    console.print(Panel(
        "[bold green]NVHive — OpenClaw Migration[/bold green]\n\n"
        "Anthropic dropped OpenClaw support.\n"
        "nvHive replaces that path with smart routing across providers,\n"
        "local GPU inference, and council consensus.\n\n"
        "[bold]Migrate:[/bold] nvh migrate --from openclaw\n"
        "[bold]NemoClaw:[/bold] nvh nemoclaw --start\n"
        "[bold]Claude Code:[/bold] claude mcp add nvhive"
        " -- python -m nvh.mcp_server",
        border_style="green",
    ))

    # --- Install mode: pip install openclaw + register + smoke test ---
    if install:
        from pathlib import Path

        from nvh.integrations.diagnostics.detector import register_openclaw

        console.print()
        console.print(Rule("Install OpenClaw + register nvHive"))
        console.print()

        # Step 1: install openclaw if missing
        if _is_package_installed("openclaw"):
            console.print("  [green]✓[/green] openclaw already installed")
        else:
            console.print("  Installing [bold]openclaw[/bold] via pip...")
            ok, msg = _pip_install_package("openclaw")
            if ok:
                console.print(f"  [green]✓[/green] {msg}")
            else:
                console.print(f"  [red]✗[/red] {msg}")
                console.print()
                console.print(
                    "  [dim]Fix the pip error above, then rerun"
                    " [bold]nvh openclaw --install[/bold][/dim]"
                )
                raise typer.Exit(1)

        # Step 2: register nvHive MCP server in openclaw.json
        console.print()
        console.print("  Registering nvHive MCP server in openclaw.json...")
        ok, msg = register_openclaw(output if output else None)
        if ok:
            console.print(f"  [green]✓[/green] {msg}")
        else:
            console.print(f"  [red]✗[/red] {msg}")
            raise typer.Exit(1)

        # Step 3: smoke test — verify the MCP server module loads
        console.print()
        console.print("  Smoke test: loading nvHive MCP server module...")
        try:
            from nvh.mcp_server import create_server
            create_server()
            console.print("  [green]✓[/green] nvhive-mcp entry point available")
        except ImportError:
            console.print("  [yellow]![/yellow] MCP SDK not installed")
            console.print('  Install with: [bold]pip install "nvhive[mcp]"[/bold]')
        except Exception as e:
            console.print(f"  [yellow]![/yellow] MCP server loads but: {e}")
            console.print("  [dim]This is expected — the server needs a client to connect.[/dim]")

        console.print()
        console.print(Panel(
            "[bold green]OpenClaw setup complete.[/bold green]\n\n"
            "Your OpenClaw agents can now call nvHive tools:\n"
            "  ask, ask_safe, council, throwdown, status,\n"
            "  list_advisors, list_cabinets\n\n"
            "Run [bold]nvh openclaw --test[/bold] to verify,\n"
            "or start an OpenClaw agent and try the tools directly.",
            border_style="green",
        ))
        console.print()
        return

    # --- Test mode ---
    if test:
        console.print()
        console.print(Rule("Connectivity Test"))
        console.print()
        if http:
            try:
                import httpx
                url = f"http://localhost:{port}/mcp"
                console.print(f"  Testing [bold]{url}[/bold] ...")
                httpx.get(url, timeout=5)
                console.print(
                    "  [green]✓[/green] MCP HTTP server is"
                    " [bold green]reachable[/bold green]"
                )
            except Exception as e:
                console.print(f"  [red]✗[/red] Cannot reach MCP server at port {port}")
                console.print(f"  Error: {e}")
                console.print()
                console.print("  Start it: [bold]nvh openclaw --start --http[/bold]")
        else:
            # For stdio, check if nvhive-mcp or nvh module is importable
            try:
                from nvh.mcp_server import create_server
                create_server()
                console.print(
                    "  [green]✓[/green] MCP server module"
                    " loads [bold green]OK[/bold green]"
                )
                console.print("  [green]✓[/green] nvhive-mcp entry point available")
            except ImportError:
                console.print("  [red]✗[/red] MCP SDK not installed")
                console.print('  Install with: [bold]pip install "nvhive[mcp]"[/bold]')
            except Exception as e:
                console.print(f"  [yellow]![/yellow] MCP server loads but: {e}")
                console.print("  This is OK — the server needs an MCP client to connect.")

            console.print()
            console.print("  [dim]For stdio transport, OpenClaw spawns the server automatically.")
            console.print("  No separate start step needed — just add the config.[/dim]")
        console.print()
        return

    # --- Start mode ---
    if start:
        console.print()
        console.print(Rule("Starting NVHive MCP Server for OpenClaw"))
        console.print()
        try:
            from nvh.mcp_server import create_server
        except ImportError:
            console.print("[red]MCP SDK not installed.[/red]")
            console.print('Install with: [bold]pip install "nvhive[mcp]"[/bold]')
            raise typer.Exit(1)

        server = create_server()
        if http:
            console.print(f"  Transport: HTTP on port {port}")
            console.print(f"  Connect clients to: http://localhost:{port}/mcp")
            console.print()
            server.run(transport="streamable-http", host="0.0.0.0", port=port)
        else:
            console.print("  Transport: stdio")
            console.print("  OpenClaw will spawn this server automatically via config.")
            console.print()
            server.run(transport="stdio")
        return

    # --- Config mode ---
    if config:
        from pathlib import Path

        from nvh.integrations.installs.openclaw import write_openclaw_config
        path = write_openclaw_config(output_path=Path(output) if output else None)
        console.print()
        console.print(f"  [green]✓[/green] Config written to [bold]{path}[/bold]")
        console.print()
        console.print("  OpenClaw will auto-discover nvHive tools on next agent run.")
        console.print()
        return

    # --- Default: show setup guide ---
    console.print()
    console.print(Rule("Quick Start"))
    console.print()
    console.print("  [bold]Step 1:[/bold] Install MCP support (if not already)")
    console.print('  [dim]$[/dim] pip install "nvhive[mcp]"')
    console.print()
    console.print("  [bold]Step 2:[/bold] Add nvHive to your OpenClaw config")
    console.print()
    console.print("  [bold]Option A[/bold] — auto-generate openclaw.json:")
    console.print("  [dim]$[/dim] nvh openclaw --config")
    console.print()
    console.print("  [bold]Option B[/bold] — add manually to openclaw.json:")
    console.print()
    console.print(Panel(
        '{\n'
        '  "mcpServers": {\n'
        '    "nvhive": {\n'
        '      "command": "nvhive-mcp"\n'
        '    }\n'
        '  }\n'
        '}',
        title="openclaw.json",
        border_style="dim",
        width=45,
    ))
    console.print()
    console.print("  [bold]Step 3:[/bold] Use nvHive tools in your agent")
    console.print("  Your OpenClaw agent can now call any nvHive tool directly.")
    console.print()

    console.print(Rule("Available Tools"))
    console.print()

    tool_table = Table(show_header=True, header_style="bold green", padding=(0, 2))
    tool_table.add_column("Tool", style="bold")
    tool_table.add_column("What It Does")
    tool_table.add_column("Example Use")

    tool_table.add_row("ask", "Smart-routed LLM query", "Ask any question across every configured provider")
    tool_table.add_row("ask_safe", "Local-only query", "Privacy-sensitive queries via Ollama")
    tool_table.add_row("council", "Multi-model consensus", "Get 3-5 LLMs to debate and synthesize")
    tool_table.add_row(
        "throwdown", "Two-pass deep analysis",
        "Complex questions with critique loop",
    )
    tool_table.add_row("status", "System status", "Check providers, GPU, budget")
    tool_table.add_row("list_advisors", "Available providers", "See which LLMs are configured")
    tool_table.add_row("list_cabinets", "Agent presets", "Browse expert persona groups")

    console.print(tool_table)
    console.print()

    console.print(Rule("Architecture"))
    console.print()
    console.print("  ┌─────────────────────────────────────┐")
    console.print("  │  OpenClaw Agent                     │")
    console.print("  │  ┌──────────┐    ┌──────────────┐   │")
    console.print("  │  │  Agent   │───▶│  MCP Client  │   │")
    console.print("  │  │  Logic   │    │              │   │")
    console.print("  │  └──────────┘    └──────┬───────┘   │")
    console.print("  └────────────────────────-┼───────────┘")
    console.print("                            │ stdio / HTTP")
    console.print("                            ▼")
    console.print("                  ┌──────────────────┐")
    console.print("                  │  NVHive MCP      │")
    console.print("                  │  Server          │")
    console.print("                  │  (nvhive-mcp)    │")
    console.print("                  └────────┬─────────┘")
    console.print("                           │ Smart Router")
    console.print("              ┌─────────-──┼────────────┐")
    console.print("              ▼            ▼            ▼")
    console.print("        ┌──────────┐ ┌──────────┐ ┌──────────┐")
    console.print("        │  Ollama  │ │   Groq   │ │Anthropic │ ...more providers")
    console.print("        │ Nemotron │ │          │ │          │")
    console.print("        └──────────┘ └──────────┘ └──────────┘")
    console.print()

    console.print(Rule("Transport Options"))
    console.print()
    console.print("  [bold]stdio[/bold] (default) — OpenClaw spawns nvHive as a subprocess.")
    console.print("  No manual start needed. Just add the config and go.")
    console.print()
    console.print("  [bold]HTTP[/bold] — for remote or multi-client setups:")
    console.print("  [dim]$[/dim] nvh openclaw --start --http --port 8080")
    console.print("  Then configure OpenClaw with URL: http://localhost:8080/mcp")
    console.print()

    console.print(Rule("Commands"))
    console.print()
    console.print("  [bold]nvh openclaw[/bold]            Show this setup guide")
    console.print("  [bold]nvh openclaw --install[/bold]  Install OpenClaw + register nvHive")
    console.print("  [bold]nvh openclaw --test[/bold]     Test MCP server availability")
    console.print("  [bold]nvh openclaw --start[/bold]    Start MCP server manually")
    console.print("  [bold]nvh openclaw --config[/bold]   Generate openclaw.json")
    console.print(
        "  [bold]nvh openclaw --http[/bold]"
        "     Use HTTP transport (with --start or --test)"
    )
    console.print()


# ---------------------------------------------------------------------------
# hive mcp — MCP server for Claude Code, Cursor, OpenClaw. Bare `nvh mcp`
# starts the server; `nvh mcp servers …` (registered further down) manages
# the external tool servers the Wizard attaches.
# ---------------------------------------------------------------------------

mcp_app = typer.Typer(invoke_without_command=True)
app.add_typer(mcp_app, name="mcp", rich_help_panel="Infrastructure")


@mcp_app.callback()
def mcp(
    ctx: typer.Context,
    transport: str = typer.Option(
        "stdio", "--transport", "-t",
        help="Transport: stdio or streamable-http",
    ),
    port: int = typer.Option(8080, "--port", help="Port for HTTP transport"),
):
    """Start the MCP (Model Context Protocol) server.

    Exposes nvHive tools to Claude Code, Cursor, OpenClaw, and any MCP client.

    Tools provided:
      ask           Smart-routed LLM query
      ask_safe      Local-only query (Ollama)
      council       Multi-model consensus
      throwdown     Two-pass deep analysis
      status        System status
      list_advisors Available providers
      list_cabinets Agent cabinet presets

    Examples:
        nvh mcp                           Start via stdio (default)
        nvh mcp -t streamable-http        Start as HTTP server
        claude mcp add nvhive nvh mcp     Register with Claude Code
        nvh mcp servers list              External tool servers for the Wizard
    """
    if ctx.invoked_subcommand is not None:
        return
    # Under stdio, stdout is the JSON-RPC channel — anything printed there
    # corrupts the stream for the client.
    err = Console(stderr=True)
    try:
        from nvh.mcp_server import create_server
    except ImportError:
        err.print("[red]MCP SDK not installed.[/red]")
        err.print('Install with: [bold]pip install "nvhive[mcp]"[/bold]')
        err.print('  or: [bold]pip install "mcp[cli]"[/bold]')
        raise typer.Exit(1)

    server = create_server()

    if transport == "stdio":
        err.print("[bold]NVHive MCP Server[/bold] starting (stdio transport)")
        err.print("Register with Claude Code:")
        err.print("  [dim]$[/dim] claude mcp add nvhive nvh mcp")
        err.print()
        server.run(transport="stdio")
    elif transport in ("streamable-http", "http", "sse"):
        err.print(f"[bold]NVHive MCP Server[/bold] starting on port {port} (HTTP transport)")
        err.print(f"Connect clients to: http://localhost:{port}/mcp")
        err.print()
        server.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        err.print(f"[red]Unknown transport: {transport}[/red]")
        err.print("Use: stdio, streamable-http")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# nvh estimate — GPU performance emulation
# ---------------------------------------------------------------------------


@app.command(rich_help_panel="Admin")
def estimate(
    gpu: str | None = typer.Option(
        None, "--gpu", "-g",
        help="GPU to estimate for (e.g. rtx_4090, h100_sxm)",
    ),
    model: str | None = typer.Option(
        None, "-m", "--model",
        help="Model to estimate (e.g. llama3.2-vision, qwen3:8b)",
    ),
    list_gpus: bool = typer.Option(
        False, "--list-gpus",
        help="List all known GPUs",
    ),
):
    """Estimate LLM inference speed on any NVIDIA GPU.

    Predicts tokens/second based on memory bandwidth, architecture
    IPC, and measured baselines from real hardware.

    Examples:
        nvh estimate --gpu rtx_4090 --model llama3.2-vision
        nvh estimate --gpu rtx_3080
        nvh estimate --model qwen3:8b
        nvh estimate --list-gpus
    """
    from nvh.utils.gpu_emulation import (
        GPU_DATABASE,
        estimate_all_gpus,
        estimate_all_models,
        estimate_performance,
    )

    if list_gpus:
        table = Table(
            title="Known NVIDIA GPUs",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Key", style="bold")
        table.add_column("Name")
        table.add_column("Arch")
        table.add_column("VRAM", justify="right")
        table.add_column("BW (GB/s)", justify="right")
        table.add_column("CUDA Cores", justify="right")

        for key in sorted(GPU_DATABASE.keys()):
            spec = GPU_DATABASE[key]
            table.add_row(
                key,
                spec.name,
                spec.architecture,
                f"{spec.vram_gb:.0f} GB",
                f"{spec.memory_bandwidth_gbps:.0f}",
                str(spec.cuda_cores),
            )
        console.print(table)
        return

    if gpu and model:
        est = estimate_performance(gpu, model)
        if not est:
            console.print(
                f"[red]Unknown GPU '{gpu}' or model"
                f" '{model}'.[/red]"
                f" Run --list-gpus to see options.",
            )
            return
        console.print(
            f"\n[bold]{est.gpu_name}[/bold]"
            f" + {est.model}\n",
        )
        if not est.fits_in_vram:
            console.print(
                f"  [red]Does not fit in VRAM[/red]"
                f" ({est.basis})",
            )
        else:
            color = "green" if est.confidence == "measured" else "cyan"
            console.print(
                f"  Estimated: [{color}]"
                f"{est.estimated_toks:.1f} tok/s[/{color}]",
            )
            console.print(
                f"  Confidence: {est.confidence}",
            )
            console.print(f"  Basis: {est.basis}")
            console.print(
                f"  VRAM headroom:"
                f" {est.vram_headroom_gb:.1f} GB",
            )
        console.print()
        return

    if gpu:
        results = estimate_all_models(gpu)
        spec = GPU_DATABASE.get(gpu)
        if not spec:
            console.print(f"[red]Unknown GPU '{gpu}'[/red]")
            return

        table = Table(
            title=f"{spec.name} — Estimated Performance",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Model", style="bold")
        table.add_column("Size", justify="right")
        table.add_column("tok/s", justify="right")
        table.add_column("Fits?", justify="center")
        table.add_column("Confidence")

        for est in results:
            color = (
                "green" if est.confidence == "measured"
                else "cyan" if est.fits_in_vram
                else "red"
            )
            table.add_row(
                est.model,
                f"{est.vram_headroom_gb + float(est.estimated_toks > 0) * 0:.0f} GB" if False else "",
                f"[{color}]{est.estimated_toks:.1f}[/{color}]" if est.fits_in_vram else "[red]—[/red]",
                "[green]✓[/green]" if est.fits_in_vram else "[red]✗[/red]",
                est.confidence if est.fits_in_vram else "n/a",
            )

        console.print(table)
        return

    if model:
        results = estimate_all_gpus(model)
        if not results:
            console.print(f"[red]Unknown model '{model}'[/red]")
            return

        table = Table(
            title=f"{model} — Performance Across GPUs",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("GPU", style="bold")
        table.add_column("VRAM", justify="right")
        table.add_column("tok/s", justify="right")
        table.add_column("Confidence")

        for est in results:
            color = (
                "green" if est.confidence == "measured"
                else "cyan"
            )
            table.add_row(
                est.gpu_name,
                f"{GPU_DATABASE.get(est.gpu_name.lower().replace(' ', '_'), GPU_DATABASE.get([k for k in GPU_DATABASE if GPU_DATABASE[k].name == est.gpu_name][0] if any(GPU_DATABASE[k].name == est.gpu_name for k in GPU_DATABASE) else '', None)) and ''}",
                f"[{color}]{est.estimated_toks:.1f}[/{color}]",
                est.confidence,
            )

        console.print(table)
        return

    console.print(
        "Usage: nvh estimate --gpu rtx_4090"
        " --model llama3.2-vision\n"
        "       nvh estimate --gpu rtx_3080"
        "  (all models on this GPU)\n"
        "       nvh estimate --model qwen3:8b"
        "  (this model on all GPUs)\n"
        "       nvh estimate --list-gpus"
        "  (show all known GPUs)",
    )


# ---------------------------------------------------------------------------
# nvh nvidia — NVIDIA infrastructure dashboard
# ---------------------------------------------------------------------------


@app.command(rich_help_panel="Providers")
def nvidia():
    """NVIDIA AI infrastructure dashboard.

    Shows GPU hardware, local models, NIM status, Triton status,
    and the full NVIDIA inference stack available to nvHive.

    Examples:
        nvh nvidia
    """
    async def _nvidia():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine
        from nvh.utils.gpu import detect_gpus

        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()

        console.print(
            "\n[bold green]NVIDIA AI Infrastructure[/bold green]\n",
        )

        # GPU Hardware
        console.print("[bold]GPU Hardware[/bold]")
        try:
            gpus = detect_gpus()
            if gpus:
                for gpu in gpus:
                    console.print(
                        f"  {gpu.name}"
                        f" | {gpu.vram_gb:.0f}GB VRAM"
                        f" ({gpu.memory_free_mb}MB free)"
                        f" | CUDA {gpu.cuda_version}"
                        f" | Driver {gpu.driver_version}",
                    )
            else:
                console.print(
                    "  [dim]No NVIDIA GPU detected"
                    " (Apple Silicon or CPU-only)[/dim]",
                )
        except Exception:
            console.print(
                "  [dim]GPU detection unavailable[/dim]",
            )

        console.print()

        # NVIDIA Providers
        console.print("[bold]NVIDIA Inference Stack[/bold]")
        enabled = engine.registry.list_enabled()
        nvidia_provs = {
            "ollama": (
                "Ollama + Nemotron",
                "Local inference on your GPU",
            ),
            "nvidia": (
                "NVIDIA NIM",
                "Cloud API (1000 free credits)",
            ),
            "triton": (
                "Triton Inference Server",
                "Enterprise on-prem serving",
            ),
        }

        for key, (name, desc) in nvidia_provs.items():
            if key in enabled:
                console.print(
                    f"  [green]Active[/green]  {name}"
                    f" — {desc}",
                )
            else:
                console.print(
                    f"  [dim]Inactive[/dim] {name}"
                    f" — {desc}",
                )

        console.print()

        # Local Models
        if "ollama" in enabled:
            console.print("[bold]Local Models[/bold]")
            try:
                import httpx
                r = httpx.get(
                    f"{ollama_base_url()}/api/tags", timeout=5,
                )
                if r.status_code == 200:
                    models = r.json().get("models", [])
                    if models:
                        for m in models[:10]:
                            name = m.get("name", "?")
                            size = m.get("size", 0)
                            size_gb = size / (1024**3)
                            console.print(
                                f"  {name}"
                                f" ({size_gb:.1f}GB)",
                            )
                    else:
                        console.print(
                            "  [dim]No models installed."
                            " Run: ollama pull"
                            " nemotron-mini[/dim]",
                        )
            except Exception:
                console.print(
                    "  [dim]Could not reach Ollama[/dim]",
                )
            console.print()

        # prefer-nvidia status
        pref = config.defaults.prefer_nvidia
        console.print("[bold]Routing[/bold]")
        if pref:
            console.print(
                "  [green]--prefer-nvidia: ON[/green]"
                " (1.3x bonus for NVIDIA providers)",
            )
        else:
            console.print(
                "  --prefer-nvidia: off",
            )
            console.print(
                "  [dim]Enable: nvh config set"
                " defaults.prefer_nvidia true[/dim]",
            )
        console.print()

    _run(_nvidia())


# ---------------------------------------------------------------------------
# hive nemoclaw — NemoClaw integration setup
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Infrastructure")
def nemoclaw(
    host: str = typer.Option("127.0.0.1", "--host", help="NVHive proxy bind address"),
    port: int = typer.Option(8000, "--port", help="NVHive proxy port"),
    test: bool = typer.Option(False, "--test", help="Test connectivity to a running nvHive proxy"),
    start: bool = typer.Option(False, "--start", help="Start the proxy server for NemoClaw"),
    mcp: bool = typer.Option(False, "--mcp", help="Show MCP tool server setup for NemoClaw"),
    install: bool = typer.Option(
        False, "--install",
        help="Install NemoClaw, register nvHive as inference provider, and run a smoke test",
    ),
):
    """NemoClaw integration — use NVHive as your NemoClaw inference provider and MCP tool server.

    Two integration modes:
    1. Inference Provider — NemoClaw routes all LLM calls through nvHive's smart router
    2. MCP Tool Server — NemoClaw agents can call nvHive tools (ask, council, throwdown)

    Examples:
        nvh nemoclaw              Show setup instructions
        nvh nemoclaw --install    Install NemoClaw + register nvHive + smoke test
        nvh nemoclaw --test       Test if nvHive proxy is reachable
        nvh nemoclaw --start      Start the proxy server for NemoClaw
    """
    from rich.rule import Rule

    console.print()
    console.print(Panel(
        "[bold cyan]NVHive ↔ NemoClaw Integration[/bold cyan]\n"
        "Use NVHive as your NemoClaw inference provider for multi-model\n"
        "smart routing, council consensus, and throwdown analysis.",
        border_style="cyan",
    ))

    # --- Install mode: pip install nemoclaw + register + smoke test ---
    if install:
        import shutil

        from nvh.integrations.diagnostics.detector import register_nemoclaw

        console.print()
        console.print(Rule("Install NemoClaw + register nvHive"))
        console.print()

        # Step 1: install nemoclaw if missing
        if _is_package_installed("nemoclaw"):
            console.print("  [green]✓[/green] nemoclaw already installed")
        else:
            console.print("  Installing [bold]nemoclaw[/bold] via pip...")
            ok, msg = _pip_install_package("nemoclaw")
            if ok:
                console.print(f"  [green]✓[/green] {msg}")
            else:
                console.print(f"  [red]✗[/red] {msg}")
                console.print()
                console.print(
                    "  [dim]Fix the pip error above, then rerun"
                    " [bold]nvh nemoclaw --install[/bold][/dim]"
                )
                raise typer.Exit(1)

        # Step 2: register nvHive as NemoClaw inference provider.
        # This requires the `openshell` CLI — punt to the user if missing,
        # since that usually means they need an interactive login first.
        console.print()
        if shutil.which("openshell") is None:
            console.print(
                "  [yellow]![/yellow] openshell CLI not found —"
                " skipping provider registration"
            )
            console.print(
                "  [dim]Install openshell and run"
                " [bold]openshell login[/bold] first, then:[/dim]"
            )
            console.print()
            _print_openshell_commands(host, port)
        else:
            console.print("  Registering nvHive as NemoClaw inference provider...")
            ok, msg = register_nemoclaw(host=host, port=port)
            if ok:
                console.print(f"  [green]✓[/green] {msg}")
            else:
                console.print(f"  [yellow]![/yellow] {msg}")
                console.print(
                    "  [dim]You may need to run"
                    " [bold]openshell login[/bold] first, or register manually:[/dim]"
                )
                console.print()
                _print_openshell_commands(host, port)

        # Step 3: smoke test — is the nvHive proxy reachable?
        console.print()
        console.print("  Smoke test: checking nvHive proxy reachability...")
        try:
            import httpx
            url = f"http://{host}:{port}/v1/proxy/health"
            resp = httpx.get(url, timeout=3)
            if resp.status_code == 200:
                console.print(f"  [green]✓[/green] Proxy healthy at {url}")
            else:
                console.print(
                    f"  [yellow]![/yellow] Proxy returned {resp.status_code}"
                    f" — start it with [bold]nvh nemoclaw --start[/bold]"
                )
        except Exception:
            console.print(
                "  [yellow]![/yellow] Proxy not running —"
                " start it with [bold]nvh nemoclaw --start[/bold]"
            )

        console.print()
        console.print(Panel(
            "[bold cyan]NemoClaw setup complete.[/bold cyan]\n\n"
            "Next steps:\n"
            "  1. Start proxy: [bold]nvh nemoclaw --start[/bold]\n"
            "  2. Verify:      [bold]nvh nemoclaw --test[/bold]\n"
            "  3. Set default: [bold]openshell inference"
            " set --provider nvhive --model auto[/bold]",
            border_style="cyan",
        ))
        console.print()
        return

    # --- Test mode: check if the proxy is running ---
    if test:
        console.print()
        console.print(Rule("Connectivity Test"))
        console.print()
        try:
            import httpx
            url = f"http://{host}:{port}/v1/proxy/health"
            console.print(f"  Testing [bold]{url}[/bold] ...")
            resp = httpx.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                console.print("  [green]✓[/green] NVHive proxy is [bold green]healthy[/bold green]")
                console.print(
                    "  [green]✓[/green] Engine initialized:"
                    f" {data.get('engine_initialized', '?')}"
                )
                console.print(
                    "  [green]✓[/green] Providers enabled:"
                    f" {data.get('providers_enabled', '?')}"
                )
                providers = data.get("providers", [])
                if providers:
                    console.print(f"  [green]✓[/green] Available: {', '.join(providers)}")
                has_local = data.get("has_local_inference", False)
                if has_local:
                    console.print("  [green]✓[/green] Local inference (Ollama) available")
                else:
                    console.print("  [yellow]![/yellow] No local inference — cloud-only routing")
                console.print()
                console.print("  [bold green]Ready for NemoClaw![/bold green] Register with:")
                console.print()
                _print_openshell_commands(host, port)
            else:
                console.print(f"  [red]✗[/red] Proxy returned status {resp.status_code}")
                console.print("  Start the proxy first: [bold]nvh nemoclaw --start[/bold]")
        except Exception as e:
            console.print(f"  [red]✗[/red] Cannot reach NVHive proxy at {host}:{port}")
            console.print(f"  Error: {e}")
            console.print()
            console.print("  Start the proxy first: [bold]nvh nemoclaw --start[/bold]")
        console.print()
        return

    # --- Start mode: launch the proxy ---
    if start:
        console.print()
        console.print(Rule("Starting NVHive Proxy for NemoClaw"))
        console.print()
        console.print(f"  Binding to [bold]{host}:{port}[/bold]")
        console.print(f"  OpenAI-compatible endpoint: http://{host}:{port}/v1/proxy/chat/completions")
        console.print(f"  Health check: http://{host}:{port}/v1/proxy/health")
        console.print(f"  API docs: http://{host}:{port}/docs")
        console.print()
        console.print("  Register this with NemoClaw using:")
        console.print()
        _print_openshell_commands(host, port)
        console.print()
        if not _check_serve_deps():
            raise typer.Exit(1)
        from nvh.api.server import run_server
        run_server(host=host, port=port, reload=False)
        return

    # --- MCP mode: show MCP tool server setup ---
    if mcp:
        console.print()
        console.print(Rule("NemoClaw MCP Tool Server"))
        console.print()
        console.print("  Give NemoClaw agents direct access to nvHive tools like")
        console.print(
            "  [bold]council[/bold] and [bold]throwdown[/bold]"
            " — in addition to inference routing."
        )
        console.print()
        console.print("  [bold]Step 1:[/bold] Install MCP support")
        console.print('  [dim]$[/dim] pip install "nvhive[mcp]"')
        console.print()
        console.print("  [bold]Step 2:[/bold] Add to your NemoClaw blueprint or agent config:")
        console.print()
        console.print(Panel(
            '{\n'
            '  "mcpServers": {\n'
            '    "nvhive": {\n'
            '      "command": "nvhive-mcp"\n'
            '    }\n'
            '  }\n'
            '}',
            title="NemoClaw Agent Config",
            border_style="cyan",
            width=45,
        ))
        console.print()
        console.print("  [bold]Tools available to NemoClaw agents:[/bold]")
        console.print("    [bold]ask[/bold]           — Smart-routed query across every configured provider")
        console.print("    [bold]ask_safe[/bold]      — Local-only query (nothing leaves machine)")
        console.print("    [bold]council[/bold]       — Multi-model consensus (3-10 LLMs debate)")
        console.print("    [bold]throwdown[/bold]     — Two-pass deep analysis with critique")
        console.print("    [bold]status[/bold]        — Provider and GPU status")
        console.print("    [bold]list_advisors[/bold] — Available providers")
        console.print("    [bold]list_cabinets[/bold] — Expert persona presets")
        console.print()
        console.print("  [dim]Why both inference + MCP?[/dim]")
        console.print("  [dim]Inference: every agent call auto-routes to the best model.[/dim]")
        console.print("  [dim]MCP tools: agent can explicitly request council consensus[/dim]")
        console.print(
            "  [dim]or throwdown analysis when a decision"
            " needs multiple perspectives.[/dim]"
        )
        console.print()
        return

    # --- Default: show setup instructions ---
    console.print()
    console.print(Rule("Quick Start"))
    console.print()
    console.print("  [bold]Step 1:[/bold] Start the NVHive proxy")
    console.print("  [dim]$[/dim] nvh nemoclaw --start")
    console.print()
    console.print("  [bold]Step 2:[/bold] Register NVHive as your NemoClaw inference provider")
    console.print()
    _print_openshell_commands(host, port)
    console.print()
    console.print("  [bold]Step 3:[/bold] Set NVHive as your default inference")
    console.print("  [dim]$[/dim] openshell inference set --provider nvhive --model auto")
    console.print()

    console.print(Rule("Virtual Models"))
    console.print()

    model_table = Table(show_header=True, header_style="bold cyan", padding=(0, 2))
    model_table.add_column("Model", style="bold")
    model_table.add_column("Mode")
    model_table.add_column("Description")

    model_table.add_row(
        "auto", "Smart routing",
        "Best available provider based on query type, cost, and speed",
    )
    model_table.add_row("safe", "Local only", "Routes to Ollama — nothing leaves your machine")
    model_table.add_row("council", "Consensus", "3-model council with synthesis (default)")
    model_table.add_row("council:N", "Consensus", "N-model council (2-10 members)")
    model_table.add_row(
        "throwdown", "Deep analysis",
        "Two-pass analysis with critique and refinement",
    )
    model_table.add_row(
        "<model-id>", "Direct",
        "Route to a specific model (gpt-4o, claude-sonnet-4, etc.)",
    )

    console.print(model_table)
    console.print()

    console.print(Rule("Privacy Header"))
    console.print()
    console.print("  NemoClaw's privacy router can force local-only routing by setting:")
    console.print("  [bold]x-nvhive-privacy: local-only[/bold]")
    console.print()
    console.print("  When this header is present, all inference stays on-device via Ollama,")
    console.print("  regardless of the model name requested. This integrates with NemoClaw's")
    console.print("  content-aware sensitivity routing.")
    console.print()

    console.print(Rule("Architecture"))
    console.print()
    console.print("  ┌─────────────────────────────────────┐")
    console.print("  │  NemoClaw Sandbox                   │")
    console.print("  │  ┌──────────┐    ┌──────────────┐   │")
    console.print("  │  │ OpenClaw │───▶│ inference     │   │")
    console.print("  │  │  Agent   │    │ .local        │   │")
    console.print("  │  └──────────┘    └──────┬───────┘   │")
    console.print("  └────────────────────────-┼───────────┘")
    console.print("                            │ OpenShell Gateway")
    console.print("                            ▼")
    console.print("                  ┌──────────────────┐")
    console.print("                  │   NVHive Proxy   │")
    console.print(f"                  │  {host}:{port}  │")
    console.print("                  │   /v1/proxy/     │")
    console.print("                  └────────┬─────────┘")
    console.print("                           │ Smart Router")
    console.print("              ┌─────────-──┼────────────┐")
    console.print("              ▼            ▼            ▼")
    console.print("        ┌──────────┐ ┌──────────┐ ┌──────────┐")
    console.print("        │  Ollama  │ │   Groq   │ │Anthropic │ ...more providers")
    console.print("        │ Nemotron │ │          │ │          │")
    console.print("        └──────────┘ └──────────┘ └──────────┘")
    console.print()

    console.print(Rule("Commands"))
    console.print()
    console.print("  [bold]nvh nemoclaw[/bold]           Show this setup guide")
    console.print("  [bold]nvh nemoclaw --install[/bold] Install NemoClaw + register nvHive")
    console.print("  [bold]nvh nemoclaw --test[/bold]    Test proxy connectivity")
    console.print("  [bold]nvh nemoclaw --start[/bold]   Start the proxy server")
    console.print("  [bold]nvh nemoclaw --mcp[/bold]     Show MCP tool server setup")
    console.print()


def _print_openshell_commands(host: str, port: int):
    """Print the openshell provider create command."""
    # Use host.openshell.internal for sandbox-to-host communication
    endpoint_host = (
        "host.openshell.internal"
        if host in ("127.0.0.1", "0.0.0.0", "localhost")
        else host
    )
    console.print("  [dim]$[/dim] openshell provider create \\")
    console.print("      --name nvhive \\")
    console.print("      --type openai \\")
    console.print("      --credential OPENAI_API_KEY=nvhive \\")
    console.print(f"      --config OPENAI_BASE_URL=http://{endpoint_host}:{port}/v1/proxy")


def _pip_install_package(package: str) -> tuple[bool, str]:
    """Install a pip package into the current env. Returns (success, message).

    Uses sys.executable -m pip so it respects the active venv / micromamba env.
    """
    import subprocess
    import sys as _sys

    try:
        result = subprocess.run(
            [_sys.executable, "-m", "pip", "install", package],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode == 0:
            return True, f"Installed {package}"
        # Last few lines of stderr are usually the helpful ones
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        return False, f"pip install failed:\n{tail}"
    except subprocess.TimeoutExpired:
        return False, "pip install timed out after 10 minutes"
    except Exception as exc:
        return False, f"pip install error: {exc}"


def _is_package_installed(module_name: str) -> bool:
    """Check if a Python package is importable (fast check via importlib)."""
    import importlib.util
    return importlib.util.find_spec(module_name) is not None


def _try_install_node_no_root(
    console: Console,
    *,
    assume_yes: bool = False,
) -> tuple[str | None, str | None]:
    """Offer to auto-install Node.js into the user's home without root.

    Uses ``fnm`` (Fast Node Manager) - a single-binary, no-root Node manager:
    downloads into ``NVH_HOME/runtimes/fnm``, manages multiple Node versions,
    adds them to PATH on demand. We install fnm, then use it to pull Node
    22 (current LTS) and update this process's PATH so the subsequent npm
    calls in ``nvh webui`` find it.

    Returns ``(node_path, npm_path)`` on success, ``(None, None)`` otherwise.
    Interactive by default: prompts the user before making any network call.
    Pass ``assume_yes=True`` from one-click setup paths.

    Windows is intentionally out of scope — users there should use winget
    (which requires admin for some installs but is the native path).
    """
    import os as _os
    import shutil as _shutil
    import subprocess as _sp

    from nvh.integrations import node_runtime
    from nvh.integrations.workspace.storage import storage_layout

    if sys.platform == "win32":
        return None, None

    layout = storage_layout()
    fnm_root = layout.runtime_dir / "fnm"
    fnm_root.mkdir(parents=True, exist_ok=True)
    env = _os.environ.copy()
    env.update(layout.env())
    env["FNM_DIR"] = str(fnm_root)

    existing_bin = node_runtime.find_rootless_node_bin(layout.runtime_dir)
    if existing_bin:
        _os.environ["PATH"] = f"{existing_bin}{_os.pathsep}{_os.environ.get('PATH', '')}"
        node = _shutil.which("node")
        npm = _shutil.which("npm")
        if node and npm:
            return node, npm

    if not assume_yes:
        try:
            answer = console.input(
                f"  Auto-install Node.js into {fnm_root} via fnm (no root)? [Y/n] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None, None
        if answer in ("n", "no"):
            return None, None

    # Step 1: install fnm if missing. The official installer writes to
    # FNM_DIR and optionally edits shell rc; we bypass the rc
    # edit (--skip-shell) since we'll just prepend to this process's PATH.
    fnm = node_runtime.find_fnm_binary(fnm_root)
    if fnm is None:
        console.print("  Installing fnm...")
        try:
            res = _sp.run(
                ["bash", "-c",
                 "curl -fsSL https://fnm.vercel.app/install"
                 " | bash -s -- --skip-shell"],
                capture_output=True, text=True, timeout=120, env=env,
            )
            if res.returncode != 0:
                console.print("  [yellow]fnm install failed; trying direct Node.js archive.[/yellow]")
                return _try_install_node_tarball_no_root(console, layout)
        except Exception as e:
            console.print(f"  [yellow]fnm install failed; trying direct Node.js archive:[/yellow] {e}")
            return _try_install_node_tarball_no_root(console, layout)

        fnm = node_runtime.find_fnm_binary(fnm_root)
        if not fnm:
            console.print("  [yellow]fnm installed outside NVH_HOME or was not found; trying direct Node.js archive.[/yellow]")
            return _try_install_node_tarball_no_root(console, layout)

    # Step 2: install Node 22 (current LTS). fnm caches under NVH_HOME/runtimes.
    console.print("  Installing Node.js 22 (LTS) via fnm...")
    try:
        res = _sp.run(
            [fnm, "install", "22"],
            capture_output=True, text=True, timeout=300, env=env,
        )
        if res.returncode != 0:
            console.print(
                f"  [red]Node install failed:[/red]"
                f" {res.stderr.strip().splitlines()[-1:]}"
            )
            return _try_install_node_tarball_no_root(console, layout)
    except Exception as e:
        console.print(f"  [yellow]Node install failed; trying direct Node.js archive:[/yellow] {e}")
        return _try_install_node_tarball_no_root(console, layout)

    # Step 3: find the newly-installed node + npm and put them on PATH
    # for THIS process so the existing webui flow finds them. Prefer NVH_HOME,
    # but also tolerate fnm's default ~/.local/share/fnm path.
    bin_dir = node_runtime.find_rootless_node_bin(layout.runtime_dir)
    if not bin_dir:
        console.print("  [yellow]No Node 22 install found via fnm; trying direct Node.js archive.[/yellow]")
        return _try_install_node_tarball_no_root(console, layout)
    _os.environ["PATH"] = f"{bin_dir}{_os.pathsep}{_os.environ.get('PATH', '')}"

    node = _shutil.which("node")
    npm = _shutil.which("npm")
    if not node or not npm:
        console.print(
            f"  [red]Node binaries missing from {bin_dir}[/red]"
        )
        return None, None

    console.print(f"  [green]✓ Node.js ready:[/green] {node}")
    console.print(
        f"  [dim]For new shells, add to your rc file:"
        f"  export PATH=\"{bin_dir}:$PATH\"[/dim]"
    )
    return node, npm


def _try_install_node_tarball_no_root(
    console: Console,
    layout: Any,
) -> tuple[str | None, str | None]:
    """Install Node.js from the official Linux tarball under NVH_HOME."""
    import os as _os
    import shutil as _shutil

    from nvh.integrations import node_runtime

    try:
        console.print("  Installing Node.js 22 directly into NVH_HOME/runtimes/node...")
        bin_dir = node_runtime.install_node_tarball(layout.runtime_dir)
    except Exception as exc:
        console.print(f"  [red]Direct Node.js install failed:[/red] {exc}")
        return None, None

    _os.environ["PATH"] = f"{bin_dir}{_os.pathsep}{_os.environ.get('PATH', '')}"
    node = _shutil.which("node")
    npm = _shutil.which("npm")
    if not node or not npm:
        console.print(f"  [red]Node binaries missing from {bin_dir}[/red]")
        return None, None
    console.print(f"  [green]Node.js ready:[/green] {node}")
    return node, npm


# ---------------------------------------------------------------------------
# hive version
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Admin")
def version():
    """Show NVHive version."""
    console.print(f"NVHive v{__version__}")


@app.command(rich_help_panel="Admin")
def keys(
    open_all: bool = typer.Option(False, "--open", help="Open all signup pages in browser"),
):
    """Show all free API key signup links in one place.

    No interaction needed — just shows the URLs. Open whichever ones you want,
    get the keys, then add them with: nvh <provider>

    Examples:
        nvh keys              # show all links
        nvh keys --open       # open all signup pages in browser
    """
    free_providers = [
        ("Groq", "https://console.groq.com/keys", "30 req/min free — FASTEST inference", "groq"),
        (
            "Google Gemini", "https://aistudio.google.com/apikey",
            "15 req/min free — 1M token context", "google",
        ),
        (
            "Cerebras", "https://cloud.cerebras.ai/",
            "30 req/min free — wafer-scale speed", "cerebras",
        ),
        ("NVIDIA NIM", "https://build.nvidia.com/", "1000 free credits — 100+ models", "nvidia"),
        (
            "SiliconFlow", "https://cloud.siliconflow.cn/",
            "1000 req/min free — best rate limits", "siliconflow",
        ),
        ("Fireworks AI", "https://fireworks.ai/", "10 req/min free", "fireworks"),
        (
            "Mistral", "https://console.mistral.ai/api-keys",
            "2 req/min free — multilingual", "mistral",
        ),
        (
            "SambaNova", "https://cloud.sambanova.ai/",
            "200K tokens/day free — Llama 405B", "sambanova",
        ),
        (
            "Hugging Face", "https://huggingface.co/settings/tokens",
            "Free inference API", "huggingface",
        ),
        ("AI21 Labs", "https://studio.ai21.com/", "$10 free credit — 256K context", "ai21"),
        (
            "Cohere", "https://dashboard.cohere.com/api-keys",
            "1K calls/month free — RAG specialist", "cohere",
        ),
    ]

    console.print("\n[bold]Free AI Provider Signup Links[/bold]")
    console.print("[dim]Get a key from any of these, then add it with: nvh <provider>[/dim]\n")

    table = Table()
    table.add_column("Provider", style="bold")
    table.add_column("Free Tier")
    table.add_column("Signup URL")
    table.add_column("Add Key With")

    for name, url, desc, cmd in free_providers:
        # Check if already configured
        has_key = False
        try:
            import keyring
            has_key = bool(keyring.get_password("nvhive", f"{cmd}_api_key"))
        except Exception:
            pass
        if not has_key:
            has_key = bool(os.environ.get(f"{cmd.upper()}_API_KEY"))

        status = "[green]✓ configured[/green]" if has_key else f"nvh {cmd}"
        table.add_row(name, desc, f"[link={url}]{url}[/link]", status)

        if open_all and not has_key:
            webbrowser.open(url)

    console.print(table)

    console.print("\n[dim]No-signup providers (already working):[/dim]")
    console.print("  [green]✓[/green] LLM7 — anonymous, 30 req/min, no key needed")
    console.print("  [green]✓[/green] Ollama — local GPU, no key needed (install separately)")

    if open_all:
        console.print("\n[green]All signup pages opened in your browser.[/green]")
        console.print("Paste each key with: [bold]nvh <provider>[/bold]")
    console.print()


def _detect_install_mode() -> tuple[str, str]:
    """Detect how NVHive was installed.

    Returns (mode, location) where mode is one of:
      - "git-clone":  install.sh layout at ~/nvh/repo
      - "editable":   pip install -e <path>
      - "pipx":       installed via pipx
      - "pip":        plain pip install nvhive
      - "unknown":    couldn't tell
    """
    import json
    import os
    from pathlib import Path

    # 1. install.sh layout wins if present — that's what `nvh update` used
    #    historically and we don't want to regress it.
    nvh_home = os.environ.get("NVH_HOME", os.path.expanduser("~/nvh"))
    repo_dir = os.path.join(nvh_home, "repo")
    if os.path.isdir(os.path.join(repo_dir, ".git")):
        return "git-clone", repo_dir

    # 2. Inspect the installed dist-info for editable / pipx markers.
    import nvh as _nvh_pkg
    site = Path(_nvh_pkg.__file__).resolve().parent.parent
    dist_info = next(site.glob("nvhive-*.dist-info"), None)

    if dist_info is not None:
        direct_url = dist_info / "direct_url.json"
        if direct_url.is_file():
            try:
                data = json.loads(direct_url.read_text())
                url = data.get("url", "")
                if data.get("dir_info", {}).get("editable"):
                    # url is file:// pointing at the source tree
                    src = url.replace("file:///", "").replace("file://", "")
                    return "editable", src
            except Exception:
                pass

    # 3. pipx installs live under a "pipx" path segment.
    if "pipx" in str(site).lower():
        return "pipx", str(site)

    return "pip", str(site)


def _fetch_latest_version_from_github() -> str | None:
    """Best-effort lookup of the latest version on main.

    Reads ``nvh/__init__.py`` from the raw GitHub URL and extracts
    ``__version__``. Returns None on any error so callers can treat
    the version check as purely informational.
    """
    try:
        import re
        import urllib.request
        url = (
            "https://raw.githubusercontent.com/"
            "thatcooperguy/nvHive/main/nvh/__init__.py"
        )
        with urllib.request.urlopen(url, timeout=5) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
        return m.group(1) if m else None
    except Exception:
        return None


@app.command(rich_help_panel="Admin")
def update(
    check: bool = typer.Option(
        False, "--check",
        help="Only report current vs latest — do not install anything",
    ),
    from_git: bool = typer.Option(
        False, "--from-git",
        help="Install straight from GitHub main (get fixes before a PyPI release)",
    ),
    yes: bool = typer.Option(
        False, "-y", "--yes",
        help="Skip the confirmation prompt",
    ),
):
    """Update NVHive to the latest version.

    Detects how NVHive was installed (git-clone, editable, pipx, pip) and
    uses the matching upgrade path. API keys (stored in the OS keyring)
    and config files (~/.config/nvhive or platform equivalent) are NOT
    touched by any of these upgrade paths, so you will not lose anything.

    Examples:
        nvh update              Detect install mode and upgrade
        nvh update --check      Just show current vs latest, no install
        nvh update --from-git   Force install from GitHub main
        nvh update -y           Skip the confirmation prompt
    """
    import os
    import subprocess

    mode, location = _detect_install_mode()
    current = __version__
    latest = _fetch_latest_version_from_github()

    console.print()
    console.print(Panel(
        f"[bold]NVHive Update[/bold]\n"
        f"  Current version : [bold]{current}[/bold]\n"
        f"  Latest on main  : [bold]{latest or '[dim]unknown[/dim]'}[/bold]\n"
        f"  Install mode    : [bold]{mode}[/bold]\n"
        f"  Location        : [dim]{location}[/dim]",
        border_style="cyan",
    ))
    console.print(
        "  [dim]API keys (OS keyring) and config files are preserved by every"
        " upgrade path below.[/dim]"
    )
    console.print()

    if check:
        if latest and latest != current:
            console.print(f"[yellow]Update available:[/yellow] {current} → {latest}")
            raise typer.Exit(0)
        if latest and latest == current:
            console.print("[green]Already on the latest version.[/green]")
        else:
            console.print("[dim]Could not reach GitHub to check latest version.[/dim]")
        return

    # Build the command for the detected mode.
    git_url = "git+https://github.com/thatcooperguy/nvHive.git"
    if from_git or mode in ("pip", "unknown"):
        # Plain pip path — or the user explicitly asked for git.
        if from_git:
            cmd = [sys.executable, "-m", "pip", "install", "-U", git_url]
            label = f"pip install -U {git_url}"
        else:
            cmd = [sys.executable, "-m", "pip", "install", "-U", "nvhive"]
            label = "pip install -U nvhive"
    elif mode == "git-clone":
        cmd = None  # handled specially below
        label = f"git pull + pip install -e {location}"
    elif mode == "editable":
        cmd = None
        label = f"git pull in {location}"
    elif mode == "pipx":
        if from_git:
            cmd = ["pipx", "install", "--force", git_url]
            label = f"pipx install --force {git_url}"
        else:
            cmd = ["pipx", "upgrade", "nvhive"]
            label = "pipx upgrade nvhive"
    else:
        console.print(f"[red]Unsupported install mode: {mode}[/red]")
        raise typer.Exit(1)

    console.print(f"  Will run: [bold]{label}[/bold]")
    if not yes:
        confirm = typer.confirm("  Proceed?", default=True)
        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)
    console.print()

    try:
        if mode == "git-clone":
            subprocess.run(["git", "-C", location, "pull", "--ff-only"], check=True)
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "-e", location],
                check=True,
            )
        elif mode == "editable" and not from_git:
            if not os.path.isdir(os.path.join(location, ".git")):
                console.print(
                    "[yellow]Editable install but source dir is not a git repo;"
                    " falling back to pip install -U from GitHub.[/yellow]"
                )
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-U", git_url],
                    check=True,
                )
            else:
                subprocess.run(["git", "-C", location, "pull", "--ff-only"], check=True)
        else:
            assert cmd is not None
            subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Update failed (exit {e.returncode}).[/red]")
        raise typer.Exit(1) from e
    except FileNotFoundError as e:
        console.print(f"[red]Update failed: required tool not found — {e}[/red]")
        raise typer.Exit(1) from e

    # Report new version by re-importing nvh in a subprocess so we pick up
    # whatever pip just installed (the running interpreter still has the
    # old __version__ cached in memory).
    try:
        out = subprocess.run(
            [sys.executable, "-c", "import nvh; print(nvh.__version__)"],
            capture_output=True, text=True, check=True,
        )
        new_version = out.stdout.strip()
    except Exception:
        new_version = "?"

    console.print()
    if new_version == current:
        console.print(
            f"[green]Already up to date[/green] (still on {current})."
        )
    else:
        console.print(
            f"[green]Updated:[/green] {current} → [bold]{new_version}[/bold]"
        )
    console.print()


# ---------------------------------------------------------------------------
# nvh webui — install and launch the web interface
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Infrastructure")
def studio(
    list_packs: bool = typer.Option(False, "--list", help="List available rootless AI Studio packs"),
    list_models: bool = typer.Option(False, "--models", help="List recommended local model downloads"),
    install: str | None = typer.Option(
        None,
        "--install",
        "-i",
        help="Install a pack id or bundle: starter, all, llms, agents, claw, comfy, game, creative, music",
    ),
    install_models: str | None = typer.Option(
        None,
        "--install-models",
        help="Install comma-separated model ids, or 'recommended'",
    ),
    force_update: bool = typer.Option(False, "--force-update", help="Update existing packs where possible"),
    home_dir: str | None = typer.Option(
        None,
        "--home-dir",
        help="Persistent NVH_HOME on a mounted volume for models, packs, cache, and ComfyUI",
    ),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation prompts"),
):
    """Install rootless AI Studio packs for LLMs, agents, ComfyUI, games, and music."""
    from nvh.integrations.installs.studio_packs import (
        catalog_with_status,
        expand_pack_ids,
        install_studio_models,
        install_studio_packs,
        model_catalog_with_status,
    )
    from nvh.integrations.workspace.storage import ensure_storage

    storage = ensure_storage(home_dir)
    catalog = catalog_with_status()
    packs = catalog["packs"]

    if list_models or install_models:
        model_catalog = model_catalog_with_status()
        console.print("\n[bold green]NVHive Local Models[/bold green]")
        console.print(f"  [dim]Persistent home: {storage.layout.home}[/dim]")
        console.print(f"  [dim]Detected VRAM: {model_catalog['detected_vram_gb'] or 'unknown'} GB[/dim]\n")
        table = Table(show_header=True, header_style="bold green")
        table.add_column("Model")
        table.add_column("Category")
        table.add_column("VRAM")
        table.add_column("Disk")
        table.add_column("Status")
        table.add_column("Why")
        for model in model_catalog["models"]:
            badges = []
            if model["recommended"]:
                badges.append("recommended")
            if model["installed"]:
                badges.append("installed")
            elif model["fits_vram"]:
                badges.append("fits")
            else:
                badges.append("check vram")
            table.add_row(
                model["id"],
                model["category"],
                f"{model['recommended_vram_gb']} GB" if model["recommended_vram_gb"] else "any",
                f"~{model['estimated_disk_gb']} GB",
                ", ".join(badges),
                model["why_recommended"],
            )
        console.print(table)
        console.print("\nTry: [bold]nvh studio --install-models recommended -y[/bold]")

        if install_models:
            model_ids = (
                model_catalog["recommended_ids"]
                if install_models == "recommended"
                else [item.strip() for item in install_models.split(",") if item.strip()]
            )
            console.print(f"\n[bold green]Local Model Install[/bold green]\n  Models: [bold]{', '.join(model_ids)}[/bold]")
            if not yes and not typer.confirm("Download selected models now?", default=True):
                console.print("Cancelled.")
                return

            async def _install_selected_models() -> None:
                last_log = 0.0
                async for event in install_studio_models(model_ids, force_update=force_update):
                    kind = event.get("event", "")
                    message = event.get("message", "")
                    now = time.monotonic()
                    if kind in {"plan", "model", "step", "complete", "error"}:
                        color = "green" if kind == "complete" else "red" if kind == "error" else "cyan"
                        prefix = f"{kind.upper():>8}"
                        console.print(f"  [{color}]{prefix}[/] {message}")
                    elif kind == "log" and now - last_log > 1.5:
                        console.print(f"  [dim]{message[:140]}[/dim]")
                        last_log = now

            _run(_install_selected_models())
        return

    if list_packs or not install:
        console.print("\n[bold green]NVHive AI Studio Packs[/bold green]")
        console.print(f"  [dim]Persistent home: {storage.layout.home}[/dim]")
        console.print(f"  [dim]Rootless install home: {catalog['root']}[/dim]\n")

        table = Table(show_header=True, header_style="bold green")
        table.add_column("Pack")
        table.add_column("Category")
        table.add_column("VRAM")
        table.add_column("Disk")
        table.add_column("Status")
        table.add_column("Purpose")
        for pack in packs:
            status_label = "installed" if pack["status"]["installed"] else "ready"
            table.add_row(
                pack["id"],
                pack["category"],
                f"{pack['recommended_vram_gb']} GB" if pack["recommended_vram_gb"] else "any",
                f"~{pack['estimated_disk_gb']} GB",
                status_label,
                pack["tagline"],
            )
        console.print(table)
        console.print("\n[bold]Bundles[/bold]")
        for name, pack_ids in catalog["bundles"].items():
            console.print(f"  - [green]{name}[/green]: {', '.join(pack_ids)}")
        console.print("\nTry: [bold]nvh studio --install starter -y[/bold]")
        if not install:
            return

    try:
        pack_ids = expand_pack_ids([install])
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    console.print("\n[bold green]AI Studio Pack Install[/bold green]")
    console.print(f"  Packs: [bold]{', '.join(pack_ids)}[/bold]")
    console.print(f"  Scope: user-space only under {storage.layout.home}")
    if not yes and not typer.confirm("Install selected packs now?", default=True):
        console.print("Cancelled.")
        return

    async def _install() -> None:
        last_log = 0.0
        async for event in install_studio_packs(pack_ids, force_update=force_update):
            kind = event.get("event", "")
            message = event.get("message", "")
            now = time.monotonic()
            if kind in {"plan", "pack", "step", "complete", "error"}:
                color = "green" if kind == "complete" else "red" if kind == "error" else "cyan"
                prefix = f"{kind.upper():>8}"
                console.print(f"  [{color}]{prefix}[/] {message}")
            elif kind == "log" and now - last_log > 1.5:
                console.print(f"  [dim]{message[:140]}[/dim]")
                last_log = now

    _run(_install())


@app.command(rich_help_panel="Infrastructure")
def workstation(
    all_in_one: bool = typer.Option(
        False,
        "--all",
        help="Set up local AI, ComfyUI, desktop launcher, then launch WebUI",
    ),
    launch: bool = typer.Option(False, "--launch", help="Launch the WebUI after setup"),
    desktop: bool = typer.Option(True, "--desktop/--no-desktop", help="Create a Linux desktop icon"),
    with_local_ai: bool = typer.Option(
        False,
        "--with-local-ai",
        help="Ensure Ollama is running and pull recommended chat models",
    ),
    with_comfyui: bool = typer.Option(
        False,
        "--with-comfyui",
        help="Install or update ComfyUI and nvHive starter examples",
    ),
    with_studio_packs: bool = typer.Option(
        False,
        "--with-studio-packs",
        help="Install rootless LLM, agent, ComfyUI-node, game-dev, creative, and music packs",
    ),
    port: int = typer.Option(3000, "--port", help="WebUI port"),
    api_port: int = typer.Option(8000, "--api-port", help="API server port"),
    home_dir: str | None = typer.Option(
        None,
        "--home-dir",
        help="Persistent NVH_HOME on a mounted volume for models, packs, cache, and ComfyUI",
    ),
    min_free_gb: float = typer.Option(
        20.0,
        "--min-free-gb",
        help="Minimum free space required before large local installs",
    ),
    force_large_downloads: bool = typer.Option(
        False,
        "--force-large-downloads",
        help="Proceed with model/ComfyUI downloads even when storage preflight warns",
    ),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation prompts"),
):
    """Prepare an all-in-one AI workstation for students and cloud GPU desktops.

    Examples:
        nvh workstation                 # detect GPU, create launcher, print next steps
        nvh workstation --launch        # open the WebUI flow
        nvh workstation --all -y        # local AI + ComfyUI + launcher + WebUI
    """
    import time as _time

    if all_in_one:
        with_local_ai = True
        with_comfyui = True
        with_studio_packs = True
        launch = True
        desktop = True

    # If --launch is set but the user didn't opt out of local AI, imply
    # with_local_ai=True. The everything-just-works-out-of-the-box contract
    # means the WebUI shouldn't open a /setup page where Ollama is still
    # MISSING-RUNTIME. install.sh runs its own Ollama install + model pull
    # before invoking workstation, so this is belt-and-suspenders for users
    # who run `nvh workstation --launch` directly (skipping curl|bash).
    if launch and not with_local_ai:
        with_local_ai = True

    from nvh.integrations.installs.workstation import (
        detect_workstation_profile,
        ensure_storage,
        workstation_next_steps,
        write_desktop_launcher,
        write_launch_script,
    )

    storage = ensure_storage(home_dir, min_free_gb=min_free_gb)
    profile = detect_workstation_profile(home_dir=storage.layout.home)
    boot_report: dict[str, Any] | None = None
    recommended_torch_profile = "nvidia-cu121"
    try:
        from nvh.integrations.diagnostics.boot_preflight import run_boot_preflight

        boot_report = run_boot_preflight(home_dir=storage.layout.home)
        recommended_torch_profile = (
            boot_report.get("compatibility", {}).get("recommended_torch_profile")
            or recommended_torch_profile
        )
    except Exception as exc:
        console.print(f"  [yellow]![/yellow] Boot preflight skipped: {exc}")
    console.print("\n[bold green]NVHive Student Workstation[/bold green]")
    console.print("  [dim]Target: Linux GPU desktop or forwarded cloud session[/dim]\n")
    console.print(f"  NVH_HOME:   [bold]{storage.layout.home}[/bold]")
    console.print(f"  Env file:   {storage.env_file}")
    console.print(
        f"  Storage:    {'ok' if storage.ok else 'check'}"
        f" ({storage.free_gb if storage.free_gb is not None else '?'} GB free)"
    )
    gpu_label = (
        f"{profile.gpu_name or 'NVIDIA GPU'} ({profile.vram_gb} GB VRAM)"
        if profile.has_gpu
        else "not detected"
    )
    console.print(f"  GPU:        [bold]{gpu_label}[/bold]")
    console.print(f"  Desktop:    {'yes' if profile.has_gui else 'not detected'}")
    console.print(f"  Python:     {profile.python or 'not found'}")
    console.print(f"  Ollama:     {profile.ollama or 'not found'}")
    if profile.recommended_chat_models:
        console.print(f"  Chat models: {', '.join(profile.recommended_chat_models)}")
    console.print(f"  ComfyUI:    {', '.join(profile.recommended_comfy_profiles)} profiles\n")
    if boot_report:
        agent_helper = boot_report.get("agent_helper", {})
        console.print(f"  Boot check: [bold]{boot_report.get('summary')}[/bold]")
        console.print(f"  AI helper:  {agent_helper.get('summary', 'Offline setup helper is available.')}")
        if boot_report.get("changes"):
            for change in boot_report.get("changes", [])[:5]:
                console.print(
                    f"  [yellow]![/yellow] {change.get('label')}: "
                    f"{change.get('before')} -> {change.get('after')}"
                )
        console.print()

    for note in profile.notes:
        console.print(f"  [yellow]![/yellow] {note}")
    if profile.notes:
        console.print()

    storage_needs_attention = (not storage.ok) or storage.configured_by == "default"
    if (with_local_ai or with_comfyui or with_studio_packs) and storage_needs_attention:
        console.print("[yellow]Storage preflight has warnings for large local installs.[/yellow]")
        if storage.warnings:
            for warning in storage.warnings:
                console.print(f"  [yellow]![/yellow] {warning}")
        if storage.configured_by == "default":
            console.print("  [yellow]![/yellow] Choose --home-dir on the mounted persistent file volume.")
        if not force_large_downloads:
            console.print(
                "  [dim]Pick a mounted persistent directory with --home-dir"
                " or rerun with --force-large-downloads.[/dim]"
            )
            raise typer.Exit(1)

    if desktop:
        try:
            desktop_file = write_desktop_launcher(
                port=port,
                api_port=api_port,
                install_comfyui=with_comfyui,
                storage=storage,
            )
            launch_script = write_launch_script(
                port=port,
                api_port=api_port,
                install_comfyui=with_comfyui,
                storage=storage,
            )
            console.print(f"  [green]ok[/green] Desktop launcher: {desktop_file}")
            console.print(f"  [green]ok[/green] Terminal launcher: {launch_script}")
        except Exception as exc:
            console.print(f"  [yellow]![/yellow] Could not create desktop launcher: {exc}")

    if with_local_ai:
        console.print("\n[bold]Local AI setup[/bold]")

        async def _install_local_ai_runtime() -> None:
            from nvh.integrations.installs.studio_packs import install_studio_packs

            last_log = 0.0
            async for event in install_studio_packs(["rootless-ollama"], force_update=False):
                kind = event.get("event", "")
                message = event.get("message", "")
                now = _time.monotonic()
                if kind in {"plan", "pack", "step", "complete", "error"}:
                    color = "green" if kind == "complete" else "red" if kind == "error" else "cyan"
                    console.print(f"  [{color}]{kind}[/] {message}")
                elif kind == "log" and now - last_log > 1.5:
                    console.print(f"  [dim]{message[:140]}[/dim]")
                    last_log = now

        async def _install_local_ai_starter_model() -> None:
            from nvh.integrations.installs.studio_packs import (
                install_studio_models,
                model_catalog_with_status,
            )

            model_catalog = model_catalog_with_status(home_dir=storage.layout.home)
            model_ids = list(model_catalog.get("recommended_ids") or ["gemma3-4b"])
            model_ids = model_ids[:1] or ["gemma3-4b"]

            last_log = 0.0
            async for event in install_studio_models(model_ids, force_update=False):
                kind = event.get("event", "")
                message = event.get("message", "")
                now = _time.monotonic()
                if kind in {"plan", "model", "step", "complete", "error"}:
                    color = "green" if kind == "complete" else "red" if kind == "error" else "cyan"
                    console.print(f"  [{color}]{kind}[/] {message}")
                elif kind == "log" and now - last_log > 1.5:
                    console.print(f"  [dim]{message[:140]}[/dim]")
                    last_log = now

        try:
            _run(_install_local_ai_runtime())
            should_pull = yes or typer.confirm(
                "  Pull the best local model that fits this GPU now? You can add smaller fallbacks later.",
                default=True,
            )
            if should_pull:
                _run(_install_local_ai_starter_model())
            else:
                console.print("  [dim]Skipped model pull. Use the WebUI model picker when ready.[/dim]")
        except Exception as exc:
            console.print(f"  [yellow]![/yellow] Local AI setup skipped: {exc}")

    if with_comfyui:
        console.print("\n[bold]ComfyUI setup[/bold]")

        async def _install_comfy() -> None:
            from nvh.integrations.installs.comfyui import install_comfyui

            last_log = 0.0
            async for event in install_comfyui(torch_profile=recommended_torch_profile):
                kind = event.get("event", "")
                message = event.get("message", "")
                now = _time.monotonic()
                if kind in {"plan", "step", "complete", "error"}:
                    color = "green" if kind == "complete" else "red" if kind == "error" else "cyan"
                    console.print(f"  [{color}]{kind}[/] {message}")
                elif kind == "log" and now - last_log > 1.5:
                    console.print(f"  [dim]{message[:140]}[/dim]")
                    last_log = now

        if yes or typer.confirm(
            "  Install/update ComfyUI now? This can download several GB.",
            default=False,
        ):
            _run(_install_comfy())
        else:
            console.print("  [dim]Skipped. You can install later from WebUI > Setup > ComfyUI.[/dim]")

    if with_studio_packs:
        console.print("\n[bold]AI Studio packs[/bold]")

        async def _install_packs() -> None:
            from nvh.integrations.installs.studio_packs import install_studio_packs

            last_log = 0.0
            async for event in install_studio_packs(["starter"], force_update=False):
                kind = event.get("event", "")
                message = event.get("message", "")
                now = _time.monotonic()
                if kind in {"plan", "pack", "step", "complete", "error"}:
                    color = "green" if kind == "complete" else "red" if kind == "error" else "cyan"
                    console.print(f"  [{color}]{kind}[/] {message}")
                elif kind == "log" and now - last_log > 1.5:
                    console.print(f"  [dim]{message[:140]}[/dim]")
                    last_log = now

        if yes or typer.confirm(
            "  Install the rootless starter pack now? This can download models and Python packages.",
            default=False,
        ):
            _run(_install_packs())
        else:
            console.print("  [dim]Skipped. You can install later with: nvh studio --install starter[/dim]")

    console.print("\n[bold]Next steps[/bold]")
    for step in workstation_next_steps(port=port, storage=storage):
        console.print(f"  - {step}")

    if launch:
        console.print("\n[bold]Launching WebUI...[/bold]")
        webui(
            install_only=False,
            port=port,
            uninstall=False,
            clean=False,
            yes=True,
            no_api=False,
            api_port=api_port,
            dev=False,
            open_browser=True,
            verbose=False,
        )


@app.command(rich_help_panel="Infrastructure")
def webui(
    install_only: bool = typer.Option(False, "--install", help="Install without launching"),
    # 0 is a sentinel meaning "auto-select" (2026-06-10 audit): the old
    # default of 3000 made an EXPLICIT `--port 3000` indistinguishable
    # from "no preference", so the smart cascade below could pick :80
    # while callers like services.start_webui polled :3000 forever.
    port: int = typer.Option(
        0, "--port",
        help="Port for the web UI (0 = auto-select: 80, 3000, 3001, 3002, 8080)",
    ),
    uninstall: bool = typer.Option(
        False, "--uninstall",
        help="Remove the downloaded Web UI (NVH_WEB_HOME) and exit",
    ),
    clean: bool = typer.Option(
        False, "--clean",
        help="Wipe node_modules and .next so the next run rebuilds from source",
    ),
    yes: bool = typer.Option(
        False, "-y", "--yes",
        help="Skip confirmation prompts for --uninstall / --clean",
    ),
    no_api: bool = typer.Option(
        False, "--no-api",
        help="Do not auto-start nvh serve (assume API is managed separately)",
    ),
    api_port: int = typer.Option(
        8000, "--api-port",
        help="Port the API server is expected to listen on",
    ),
    dev: bool = typer.Option(
        False, "--dev",
        help="Run the Next.js development server instead of the production server",
    ),
    open_browser: bool = typer.Option(
        True, "--open/--no-open",
        help="Open the WebUI in your browser when the server is ready",
    ),
    verbose: bool = typer.Option(
        False, "--verbose",
        help="Print detailed WebUI bootstrap diagnostics",
    ),
):
    """Install and launch the nvHive web UI.

    The web UI is optional — nvHive works fully from the CLI.
    This command installs Node.js dependencies, builds the WebUI when needed,
    and starts the optimized Next.js production server. Use --dev only when
    editing the WebUI source.

    First run installs dependencies (~30 seconds).
    Subsequent runs start instantly.

    Examples:
        nvh webui              # install (if needed) and launch on port 3000
        nvh webui --install    # install dependencies only
        nvh webui --port 8080  # launch on a different port
        nvh webui --dev        # run Next.js dev mode for WebUI development
        nvh webui --clean      # wipe node_modules/.next, keep source
        nvh webui --uninstall  # remove the downloaded Web UI entirely
    """
    import shutil
    import subprocess
    import threading
    from datetime import UTC, datetime

    from nvh.integrations import node_runtime
    from nvh.integrations.workspace.storage import storage_layout

    layout = storage_layout()
    cache_web_dir_early = str(layout.webui_dir)
    webui_env = os.environ.copy()
    webui_env.update(layout.env())
    webui_env["npm_config_cache"] = str(layout.cache_dir / "npm")
    webui_env["NEXT_TELEMETRY_DISABLED"] = "1"
    layout.webui_dir.parent.mkdir(parents=True, exist_ok=True)
    layout.logs_dir.mkdir(parents=True, exist_ok=True)
    (layout.cache_dir / "npm").mkdir(parents=True, exist_ok=True)
    webui_log_path = layout.logs_dir / "webui-bootstrap.log"

    def _webui_log(message: str) -> None:
        try:
            with webui_log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now(UTC).isoformat(timespec='seconds')} {message}\n")
        except Exception:
            pass

    def _webui_debug(message: str) -> None:
        _webui_log(message)
        if verbose or os.environ.get("NVH_VERBOSE") in {"1", "true", "True", "yes", "YES"}:
            console.print(f"[dim]{message}[/dim]")

    def _prepend_env_path(path: str | Path | None) -> None:
        if not path:
            return
        entry = str(path)
        current = webui_env.get("PATH", os.environ.get("PATH", ""))
        parts = [p for p in current.split(os.pathsep) if p]
        if entry not in parts:
            webui_env["PATH"] = f"{entry}{os.pathsep}{current}" if current else entry
        process_path = os.environ.get("PATH", "")
        process_parts = [p for p in process_path.split(os.pathsep) if p]
        if entry not in process_parts:
            os.environ["PATH"] = f"{entry}{os.pathsep}{process_path}" if process_path else entry
        _webui_debug(f"PATH includes Node candidate: {entry}")

    def _which_webui(binary: str) -> str | None:
        return shutil.which(binary, path=webui_env.get("PATH"))

    def _log_completed_process(label: str, result: subprocess.CompletedProcess[str]) -> None:
        _webui_log(f"{label} exit={result.returncode}")
        if result.stdout:
            _webui_log(f"{label} stdout tail:\n" + "\n".join(result.stdout.splitlines()[-30:]))
        if result.stderr:
            _webui_log(f"{label} stderr tail:\n" + "\n".join(result.stderr.splitlines()[-30:]))

    def _rootless_firefox_binary() -> str | None:
        apps_dir = Path(getattr(layout, "apps_dir", layout.home / "apps"))
        candidate = apps_dir / "firefox" / "firefox"
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None

    def _safe_extract_tar(archive: Path, target: Path) -> None:
        import tarfile

        target_resolved = target.resolve()

        def _inside_target(path: Path) -> bool:
            resolved = path.resolve()
            return resolved == target_resolved or str(resolved).startswith(f"{target_resolved}{os.sep}")

        with tarfile.open(archive, "r:bz2") as tar:
            for member in tar.getmembers():
                destination = target / member.name
                if not _inside_target(destination):
                    raise RuntimeError(f"Unsafe archive member: {member.name}")
                if member.issym() or member.islnk():
                    link_target = Path(member.linkname)
                    linked = link_target if link_target.is_absolute() else destination.parent / link_target
                    if not _inside_target(linked):
                        raise RuntimeError(f"Unsafe archive link: {member.name} -> {member.linkname}")
            tar.extractall(target)

    def _install_rootless_firefox() -> str | None:
        if os.environ.get("NVH_FIREFOX_AUTO_INSTALL", "1").lower() in {"0", "false", "no", "off"}:
            _webui_log("rootless Firefox auto-install disabled")
            return None
        if not sys.platform.startswith("linux"):
            return None
        import platform
        import urllib.request

        if platform.machine().lower() not in {"x86_64", "amd64"}:
            _webui_log(f"rootless Firefox skipped for unsupported arch: {platform.machine()}")
            return None
        existing = _rootless_firefox_binary()
        if existing:
            return existing

        apps_dir = Path(getattr(layout, "apps_dir", layout.home / "apps"))
        tmp_dir = Path(getattr(layout, "tmp_dir", layout.cache_dir / "tmp"))
        firefox_dir = apps_dir / "firefox"
        archive = layout.cache_dir / "firefox-latest-linux64.tar.bz2"
        extract_dir = tmp_dir / "firefox-extract"
        url = os.environ.get(
            "NVH_FIREFOX_URL",
            "https://download.mozilla.org/?product=firefox-latest-ssl&os=linux64&lang=en-US",
        )
        try:
            apps_dir.mkdir(parents=True, exist_ok=True)
            layout.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp_dir.mkdir(parents=True, exist_ok=True)
            _webui_log(f"downloading rootless Firefox from {url}")
            urllib.request.urlretrieve(url, archive)
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)
            extract_dir.mkdir(parents=True, exist_ok=True)
            _safe_extract_tar(archive, extract_dir)
            extracted = extract_dir / "firefox"
            binary = extracted / "firefox"
            if not binary.exists():
                raise RuntimeError("Firefox archive did not contain firefox/firefox")
            if firefox_dir.exists():
                shutil.rmtree(firefox_dir, ignore_errors=True)
            shutil.move(str(extracted), str(firefox_dir))
            installed = firefox_dir / "firefox"
            installed.chmod(installed.stat().st_mode | 0o111)
            _webui_log(f"rootless Firefox installed at {installed}")
            return str(installed)
        except Exception as exc:
            _webui_log(f"rootless Firefox install failed: {exc}")
            return None

    def _launch_browser_command(label: str, cmd: list[str], url: str) -> bool:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=webui_env,
            )
            time.sleep(0.75)
            code = proc.poll()
            if code not in {None, 0}:
                _webui_log(f"browser {label} exited early with code {code}: {cmd}")
                return False
            _webui_log(f"browser opened with {label}: {url}")
            return True
        except Exception as exc:
            _webui_log(f"browser {label} failed: {exc}")
            return False

    def _firefox_profile_dir(label: str) -> Path:
        configured_profile = os.environ.get("NVH_FIREFOX_PROFILE")
        if configured_profile:
            base = Path(configured_profile).expanduser()
        else:
            state_dir = Path(getattr(layout, "state_dir", layout.home / "state"))
            base = state_dir / "browser-profiles" / label
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _firefox_command(binary: str, label: str, url: str) -> list[str]:
        profile = _firefox_profile_dir(label)
        return [
            binary,
            "--new-instance",
            "--no-remote",
            "--profile",
            str(profile),
            "--new-window",
            url,
        ]

    def _open_browser_url(url: str) -> bool:
        linux_gui = bool(
            os.environ.get("DISPLAY")
            or os.environ.get("WAYLAND_DISPLAY")
            or os.environ.get("XDG_CURRENT_DESKTOP")
        )
        if sys.platform.startswith("linux") and not linux_gui:
            _webui_log(f"browser auto-open skipped; no Linux GUI session for {url}")
            return False

        explicit_browser = os.environ.get("NVH_BROWSER")
        if explicit_browser:
            import shlex

            parts = shlex.split(explicit_browser)
            if parts:
                cmd = [part.replace("{url}", url) for part in parts]
                if not any("{url}" in part for part in parts):
                    cmd.append(url)
                if _launch_browser_command("NVH_BROWSER", cmd, url):
                    return True

        # Prefer any pre-installed browser before attempting a rootless
        # Firefox download. On minimal Linux desktops (e.g. rented cloud
        # GPU desktops) Chromium is already in the taskbar; the
        # Firefox download is slow and can be blocked by network policy,
        # so trying it first leaves users staring at "Browser:
        # http://localhost:3000/setup" with nothing visibly opening.
        rootless_firefox = _rootless_firefox_binary()
        if rootless_firefox:
            console.print(f"  Opening WebUI in rootless Firefox at [bold]{url}[/bold]")
            if _launch_browser_command(
                "rootless-firefox",
                _firefox_command(rootless_firefox, "rootless-firefox", url),
                url,
            ):
                return True

        for browser in ("firefox", "firefox-esr"):
            found = shutil.which(browser, path=webui_env.get("PATH"))
            if not found:
                continue
            console.print(f"  Opening WebUI in {browser} at [bold]{url}[/bold]")
            if _launch_browser_command(browser, _firefox_command(found, browser, url), url):
                return True

        for browser in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome", "brave-browser", "microsoft-edge"):
            found = shutil.which(browser, path=webui_env.get("PATH"))
            if not found:
                continue
            console.print(f"  Opening WebUI in {browser} at [bold]{url}[/bold]")
            if _launch_browser_command(browser, [found, "--new-window", url], url):
                return True

        # No pre-installed browser worked — fall back to installing a
        # rootless Firefox. This is the slow path (~100MB download) so it
        # only runs after everything else has been tried.
        console.print("  No pre-installed browser found; downloading rootless Firefox...")
        installed_firefox = _install_rootless_firefox()
        if installed_firefox and _launch_browser_command(
            "rootless-firefox",
            _firefox_command(installed_firefox, "rootless-firefox", url),
            url,
        ):
            return True

        for opener in ("xdg-open", "gio", "sensible-browser"):
            found = shutil.which(opener, path=webui_env.get("PATH"))
            if not found:
                continue
            cmd = [found, "open", url] if opener == "gio" else [found, url]
            if _launch_browser_command(opener, cmd, url):
                return True

        try:
            opened = webbrowser.open(url)
            _webui_log(f"browser opened with webbrowser={opened}: {url}")
            return bool(opened)
        except Exception as exc:
            _webui_log(f"browser auto-open failed: {exc}")
            return False

    def _schedule_browser_open(url: str, port_to_wait_for: int) -> None:
        if not open_browser:
            _webui_log(f"browser auto-open disabled for {url}")
            return

        def _worker() -> None:
            for _ in range(120):
                try:
                    with socket.create_connection(("127.0.0.1", port_to_wait_for), timeout=0.25):
                        break
                except OSError:
                    time.sleep(0.25)
            _open_browser_url(url)

        threading.Thread(target=_worker, name="nvh-webui-browser-open", daemon=True).start()

    _webui_log("=" * 72)
    _webui_log("nvh webui bootstrap start")
    _webui_log(f"NVH_HOME={layout.home}")
    _webui_log(f"webui_dir={layout.webui_dir}")
    _webui_log(f"runtime_dir={layout.runtime_dir}")
    _webui_log(f"python={sys.executable}")

    rootless_node_bin = node_runtime.find_rootless_node_bin(layout.runtime_dir)
    if rootless_node_bin:
        _prepend_env_path(rootless_node_bin)

    # --- Safe uninstall / clean paths ---------------------------------
    # These intentionally only ever touch NVH_WEB_HOME (the cache dir
    # the download path uses). They refuse to touch a source-tree web/
    # directory, a symlink, or anything that is itself a git repo — in
    # those cases the user installed from source and we must not nuke
    # their working tree. Nothing here touches Node/npm, API keys
    # (OS keyring), or config files.
    def _dir_size_bytes(path: str) -> int:
        total = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    def _fmt_bytes(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024  # type: ignore[assignment]
        return f"{n:.1f} TB"

    def _safety_check(target: str) -> None:
        """Raise typer.Exit if target is unsafe to modify."""
        if os.path.islink(target):
            console.print(
                f"[red]Refusing to touch {target}:[/red] it is a symlink. "
                "Remove it manually if that's intentional."
            )
            raise typer.Exit(1)
        # Only the cache dir is in scope.
        if os.path.abspath(target) != os.path.abspath(cache_web_dir_early):
            console.print(
                f"[red]Refusing to touch {target}:[/red] only "
                f"{cache_web_dir_early} is managed by this command."
            )
            raise typer.Exit(1)
        # Don't stomp on a user's git checkout if they pointed the
        # cache dir at one by hand.
        if os.path.isdir(os.path.join(target, ".git")):
            console.print(
                f"[red]Refusing to touch {target}:[/red] it contains a .git "
                "directory. Remove it manually if that's intentional."
            )
            raise typer.Exit(1)

    if uninstall:
        if not os.path.isdir(cache_web_dir_early):
            console.print(
                f"[dim]Nothing to remove — {cache_web_dir_early} does not exist.[/dim]"
            )
            raise typer.Exit(0)
        _safety_check(cache_web_dir_early)
        size = _dir_size_bytes(cache_web_dir_early)
        console.print(
            f"[bold]Will remove[/bold] {cache_web_dir_early} "
            f"([bold]{_fmt_bytes(size)}[/bold])."
        )
        console.print(
            "[dim]API keys and config files are NOT in this directory "
            "and will not be touched.[/dim]"
        )
        if not yes and not typer.confirm("  Proceed?", default=False):
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)
        shutil.rmtree(cache_web_dir_early)
        console.print(f"[green]Removed.[/green] Freed {_fmt_bytes(size)}.")
        raise typer.Exit(0)

    if clean:
        if not os.path.isdir(cache_web_dir_early):
            console.print(
                f"[dim]Nothing to clean — {cache_web_dir_early} does not exist.[/dim]"
            )
            raise typer.Exit(0)
        _safety_check(cache_web_dir_early)
        freed = 0
        removed: list[str] = []
        for sub in ("node_modules", ".next"):
            p = os.path.join(cache_web_dir_early, sub)
            if os.path.isdir(p) and not os.path.islink(p):
                freed += _dir_size_bytes(p)
                shutil.rmtree(p)
                removed.append(sub)
        if not removed:
            console.print("[dim]Nothing to clean — no node_modules/.next found.[/dim]")
            raise typer.Exit(0)
        console.print(
            f"[green]Cleaned {', '.join(removed)}.[/green] "
            f"Freed {_fmt_bytes(freed)}. Next `nvh webui` run will rebuild."
        )
        raise typer.Exit(0)
    # ------------------------------------------------------------------

    # Find the web directory. When nvHive is installed via pip, the web/
    # folder is not shipped in the wheel, so also check the persistent
    # NVH_WEB_HOME directory and offer to download it on first run.
    cache_web_dir = str(layout.webui_dir)
    web_dir = None
    candidates = [
        os.path.join(
            os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))
            )),
            "web",
        ),
        str(layout.home / "repo" / "web"),
        os.path.expanduser("~/nvh/repo/web"),
        cache_web_dir,
        os.path.join(os.getcwd(), "web"),
    ]
    for candidate in candidates:
        if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "package.json")):
            web_dir = candidate
            break

    web_ref = os.environ.get("NVH_WEB_REF") or f"v{__version__}"

    def _download_webui_zip(destination: str, ref: str) -> bool:
        """Download web/ from GitHub without requiring git."""
        import zipfile
        from urllib.request import Request, urlopen

        ref_kind = "heads" if ref in {"main", "master"} or "/" in ref else "tags"
        zip_url = f"https://github.com/thatcooperguy/nvHive/archive/refs/{ref_kind}/{ref}.zip"
        zip_path = destination + ".zip"
        extract_dir = destination + ".ziptmp"
        try:
            for path in (zip_path, extract_dir):
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.exists(path):
                    os.remove(path)

            req = Request(zip_url, headers={"User-Agent": "nvhive-webui-bootstrap"})
            with urlopen(req, timeout=120) as response, open(zip_path, "wb") as fh:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)

            os.makedirs(extract_dir, exist_ok=True)
            extract_root = os.path.abspath(extract_dir)
            with zipfile.ZipFile(zip_path) as zf:
                for member in zf.infolist():
                    target = os.path.abspath(os.path.join(extract_dir, member.filename))
                    if not target.startswith(extract_root + os.sep) and target != extract_root:
                        raise ValueError(f"unsafe zip member: {member.filename}")
                zf.extractall(extract_dir)

            src_web = ""
            for name in os.listdir(extract_dir):
                candidate_web = os.path.join(extract_dir, name, "web")
                if os.path.isfile(os.path.join(candidate_web, "package.json")):
                    src_web = candidate_web
                    break
            if not src_web:
                console.print("[red]Downloaded archive has no web/ directory.[/red]")
                return False

            if os.path.isdir(destination):
                shutil.rmtree(destination, ignore_errors=True)
            shutil.move(src_web, destination)
            return True
        except Exception as exc:
            console.print(f"[red]Web UI archive download failed:[/red] {exc}")
            return False
        finally:
            if os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
            if os.path.isdir(extract_dir):
                shutil.rmtree(extract_dir, ignore_errors=True)

    if not web_dir:
        # Attempt to download the web/ directory from the upstream repo
        # so pip-installed users get a working `nvh webui` out of the box.
        git = shutil.which("git")

        console.print("[bold]Downloading Web UI (first run)...[/bold]")
        os.makedirs(os.path.dirname(cache_web_dir), exist_ok=True)
        tmp_clone = cache_web_dir + ".tmp"
        if os.path.isdir(tmp_clone):
            shutil.rmtree(tmp_clone, ignore_errors=True)
        downloaded = False
        if git:
            result = subprocess.run(
                [
                    git, "clone", "--depth", "1", "--branch", web_ref,
                    "https://github.com/thatcooperguy/nvHive.git",
                    tmp_clone,
                ],
                capture_output=True,
                text=True,
                env=webui_env,
            )
            if result.returncode != 0 and web_ref != "main":
                console.print(
                    f"[yellow]Could not fetch WebUI ref {web_ref}; trying main.[/yellow]"
                )
                if os.path.isdir(tmp_clone):
                    shutil.rmtree(tmp_clone, ignore_errors=True)
                result = subprocess.run(
                    [
                        git, "clone", "--depth", "1", "--branch", "main",
                        "https://github.com/thatcooperguy/nvHive.git",
                        tmp_clone,
                    ],
                    capture_output=True,
                    text=True,
                    env=webui_env,
                )
            if result.returncode == 0:
                src_web = os.path.join(tmp_clone, "web")
                if os.path.isdir(src_web):
                    if os.path.isdir(cache_web_dir):
                        shutil.rmtree(cache_web_dir, ignore_errors=True)
                    shutil.move(src_web, cache_web_dir)
                    downloaded = True
                else:
                    console.print("[yellow]Downloaded repo has no web/ directory.[/yellow]")
            else:
                console.print("[yellow]git clone failed; trying GitHub archive fallback.[/yellow]")
                stderr = result.stderr.strip()
                if stderr:
                    console.print(f"[dim]{stderr}[/dim]")

        if not downloaded:
            downloaded = _download_webui_zip(cache_web_dir, web_ref)
            if not downloaded and web_ref != "main":
                console.print("[yellow]Trying WebUI archive from main.[/yellow]")
                downloaded = _download_webui_zip(cache_web_dir, "main")

        shutil.rmtree(tmp_clone, ignore_errors=True)
        if not downloaded:
            console.print("[red]Failed to download Web UI.[/red]")
            console.print(
                "Check network access, or install from a GitHub release/source checkout."
            )
            raise typer.Exit(1)
        web_dir = cache_web_dir
        console.print(f"[green]Web UI downloaded to {cache_web_dir}[/green]")

    # Check for Node.js.
    # On Windows, npm ships as npm.cmd; Python's subprocess cannot launch
    # .cmd files by bare name (fails with WinError 2), so resolve the
    # absolute path via shutil.which and use it for all subprocess calls.
    node = _which_webui("node")
    npm = _which_webui("npm")
    if sys.platform == "win32" and not npm:
        # shutil.which should find npm.cmd, but some installers only add
        # npm to PATHEXT via CMD shims — try a few fallbacks.
        for ext in ("npm.cmd", "npm.exe", "npm.bat"):
            candidate = _which_webui(ext)
            if candidate:
                npm = candidate
                break
    _webui_debug(f"Initial node={node or '<missing>'}")
    _webui_debug(f"Initial npm={npm or '<missing>'}")
    if not node or not npm:
        console.print("[yellow]Node.js not found.[/yellow]")
        _webui_log("Node.js not found before rootless bootstrap")
        # Offer to install automatically. On Linux/macOS without root we
        # use fnm (Fast Node Manager): single-binary installer, drops Node
        # under ~/.local/share/fnm and adds to PATH for this process.
        # Windows stays with winget guidance (requires user action).
        node, npm = _try_install_node_no_root(console, assume_yes=yes)
        if node:
            _prepend_env_path(Path(node).parent)
        rootless_node_bin = node_runtime.find_rootless_node_bin(layout.runtime_dir)
        if rootless_node_bin:
            _prepend_env_path(rootless_node_bin)
        node = _which_webui("node") or node
        npm = _which_webui("npm") or npm
        _webui_debug(f"Post-bootstrap node={node or '<missing>'}")
        _webui_debug(f"Post-bootstrap npm={npm or '<missing>'}")
        if not node or not npm:
            console.print("[red]Auto-install failed or declined.[/red]")
            console.print(f"[dim]WebUI bootstrap log: {webui_log_path}[/dim]")
            console.print("Install Node.js 22+ in your rootless nvHive workspace:")
            if sys.platform == "darwin":
                console.print("  brew install node")
            elif sys.platform == "win32":
                console.print("  winget install OpenJS.NodeJS")
            else:
                console.print(
                    "  nvh webui --clean --port 3000 -y"
                )
                console.print("  If that still fails, send the nvHive support snapshot to your VM admin.")
            raise typer.Exit(1)
    elif node:
        _prepend_env_path(Path(node).parent)

    # Install dependencies if needed
    node_modules = os.path.join(web_dir, "node_modules")
    if not os.path.isdir(node_modules):
        console.print("[bold]Installing web UI dependencies...[/bold]")
        _webui_debug(f"Installing dependencies in {web_dir}")
        _webui_debug(f"Using node={node}")
        _webui_debug(f"Using npm={npm}")
        result = subprocess.run(
            [npm, "ci"],
            cwd=web_dir,
            capture_output=True,
            text=True,
            env=webui_env,
        )
        _log_completed_process("npm ci", result)
        if result.returncode != 0:
            # Try npm install as fallback
            result = subprocess.run(
                [npm, "install"],
                cwd=web_dir,
                capture_output=True,
                text=True,
                env=webui_env,
            )
            _log_completed_process("npm install", result)
        if result.returncode != 0:
            console.print("[red]npm install failed.[/red]")
            tail = "\n".join((result.stderr or result.stdout or "").splitlines()[-8:])
            if tail:
                console.print(f"[dim]{tail}[/dim]")
            console.print(f"[dim]WebUI bootstrap log: {webui_log_path}[/dim]")
            raise typer.Exit(1)
        console.print("[green]Dependencies installed.[/green]")

    def _web_build_ready(path: str) -> bool:
        return os.path.isfile(os.path.join(path, ".next", "BUILD_ID"))

    if not dev and not _web_build_ready(web_dir):
        console.print("[bold]Building optimized Web UI (first run)...[/bold]")
        _webui_debug("Building optimized Web UI")
        result = subprocess.run(
            [npm, "run", "build"],
            cwd=web_dir,
            env=webui_env,
        )
        if result.returncode != 0:
            console.print("[red]Web UI build failed.[/red]")
            console.print(f"[dim]WebUI bootstrap log: {webui_log_path}[/dim]")
            console.print(
                "Run [bold]nvh webui --clean[/bold] and try again, "
                "or use [bold]nvh webui --dev[/bold] while developing."
            )
            raise typer.Exit(1)
        console.print("[green]Optimized Web UI build ready.[/green]")

    if install_only:
        console.print("[green]Web UI ready. Run 'nvh webui' to launch.[/green]")
        return

    # --- Smart setup: hostname + best port ---
    import socket

    from nvh.integrations.workspace.hostname import add_hostname, is_hostname_configured

    def _port_available(p: int) -> bool:
        """Check if a port is free to bind."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", p))
                return True
        except OSError:
            return False

    # Step 1: Try to set up hostname (silent, best-effort)
    if not is_hostname_configured():
        ok, msg = add_hostname()
        if ok and "localhost" not in msg:
            console.print("  [green]✓[/green] Hostname configured: nvhive")

    # Step 2: Smart port selection — cascade through preferred ports.
    # Only the 0 sentinel (the typer default) triggers the cascade; any
    # explicitly passed port — including 3000 — is honored verbatim
    # (2026-06-10 audit: `nvh services start` spawns `nvh webui --port
    # 3000` and polls :3000, so a cascade onto :80 left the pipeline
    # polling the wrong port and reporting a false failure).
    if port == 0:
        # Auto-select: try 80 first (if we have access), then 3000, then fallbacks
        preferred = [80, 3000, 3001, 3002, 8080]
        chosen_port = None
        for p in preferred:
            if _port_available(p):
                chosen_port = p
                if p != 3000:
                    console.print(
                        f"  [green]✓[/green] Using port {p}"
                        + (" (privileged)" if p < 1024 else "")
                    )
                break
        if chosen_port is None:
            console.print("[red]All preferred ports are in use (80, 3000-3002, 8080).[/red]")
            console.print("Specify a free port: [bold]nvh webui --port 9000[/bold]")
            raise typer.Exit(1)
    else:
        # User specified a port — use it or fail
        if not _port_available(port):
            console.print(f"[red]Port {port} is already in use.[/red]")
            console.print("Choose a different port: [bold]nvh webui --port 3001[/bold]")
            raise typer.Exit(1)
        chosen_port = port

    # Step 3: Build the access URL
    host_label = "nvhive" if is_hostname_configured() else "localhost"
    port_suffix = "" if chosen_port == 80 else f":{chosen_port}"
    access_url = f"http://{host_label}{port_suffix}"

    # Step 4: Ensure the API server (nvh serve) is running. The web UI
    # makes fetch calls to http://localhost:8000 by default, and will
    # render an empty Advisors/Providers page if the API is down. Auto-
    # start it in the background unless the user opted out with --no-api.
    #
    # The health probes (api_healthy) and stale-process recovery
    # (kill_stale_api) used to live as closures here — PR #65 introduced
    # them inline. They were promoted to nvh.cli.services so the new
    # ``nvh services`` command and the test suite can call them directly.
    # The behavior is unchanged; only the import surface moved.
    import time as _time

    from nvh.cli.services import api_healthy as _api_healthy
    from nvh.cli.services import kill_stale_api as _kill_stale_api
    from nvh.cli.services import port_listening as _api_reachable

    api_proc: subprocess.Popen | None = None
    api_already_running = _api_reachable(api_port)
    api_stale = False

    # If something's on the port, probe its /v1/health to make sure it's not
    # a stale broken instance (engine failed to initialize at startup, etc.).
    # If unhealthy we kill it and start fresh — otherwise we'd reuse a broken
    # API server forever and the WebUI would show "API offline" indefinitely.
    #
    # Guarded by --no-api (2026-06-10 audit): when the caller says the
    # API is managed externally — services.start_webui passes --no-api
    # precisely so this command can't touch the API it just brought up
    # — we must never probe-and-kill it. The single 2s health probe can
    # transiently fail during a Next.js cold boot on a loaded rig, and
    # the old unguarded block then SIGTERM'd the pipeline's freshly
    # health-gated API mid-bring-up.
    if api_already_running and not no_api:
        healthy, reason = _api_healthy(api_port)
        if not healthy:
            console.print(
                f"  [yellow]![/yellow] Existing API on {api_port} is unhealthy ({reason}); "
                f"restarting it."
            )
            _kill_stale_api(api_port)
            # Give the OS a beat to release the port.
            for _ in range(10):
                if not _api_reachable(api_port):
                    break
                _time.sleep(0.3)
            api_already_running = _api_reachable(api_port)
            api_stale = True

    # If serve deps are missing and the API isn't already running externally,
    # treat as --no-api so the web UI still launches (without API features).
    if not no_api and not api_already_running and not _check_serve_deps():
        console.print(
            "  [yellow]![/yellow] Skipping API auto-start. "
            "The web UI will work but Advisors/Providers pages will be empty."
        )
        no_api = True

    if no_api:
        if not api_already_running:
            console.print(
                f"  [yellow]![/yellow] --no-api set but nothing is listening on "
                f"{api_port}; the Advisors/Providers pages will be empty."
            )
    elif api_already_running:
        console.print(f"  [green]✓[/green] API server already running on {api_port} (healthy)")
    else:
        console.print(f"  [bold]Starting API server (nvh serve) on {api_port}...[/bold]")
        # Resolve the current nvh executable so we launch the same install.
        # Basename check (2026-06-10 audit): the previous
        # endswith("python") suffix test missed "python3"/"python3.12" —
        # the usual sys.executable names — and spawned `python3 serve`,
        # which dies instantly trying to run a script file named "serve".
        nvh_exe = shutil.which("nvh") or sys.executable
        if os.path.basename(nvh_exe).lower().startswith("python"):
            api_cmd = [nvh_exe, "-m", "nvh.cli.main", "serve", "--port", str(api_port)]
        else:
            api_cmd = [nvh_exe, "serve", "--port", str(api_port)]

        # Pipe API server stdout+stderr to a real log file. The previous
        # behavior (subprocess.DEVNULL) made every silent boot failure
        # (slow imports, port collision, missing dep, Pydantic config
        # error) invisible to the user — they'd open the WebUI, see empty
        # cards, and have nothing to look at. Now there is one canonical
        # file to read.
        api_log_path: Path | None = None
        api_log_handle = None
        try:
            from nvh.integrations.workspace.storage import nvh_home as _nvh_home
            api_log_path = _nvh_home()[0] / "logs" / "api-server.log"
            api_log_path.parent.mkdir(parents=True, exist_ok=True)
            api_log_handle = open(api_log_path, "a", buffering=1, encoding="utf-8")
            _stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            api_log_handle.write(f"\n--- nvh webui auto-start at {_stamp} ---\n")
            api_log_handle.flush()
        except Exception:
            # Logging is best-effort; if we can't open the log file we
            # still want the API subprocess to start (it just won't be
            # diagnosable). The user only sees the consequences later.
            api_log_path = None
            api_log_handle = None

        try:
            # DAEMONIZE the API so it survives `nvh webui` exit, install
            # terminal close, and SIGHUP. Real-rig 2026-05-21: photo 2 showed
            # API UP, photo 3 (~30s later) showed API DOWN — because the
            # finally-block below USED to terminate the API on `nvh webui`
            # exit, and `nvh webui` was a child of install.sh which got
            # SIGHUP'd when the user's install terminal lost focus.
            #
            # Three pieces of the rootless-daemon pattern (same shape as
            # `start_ollama_with_health_wait` in install.sh, which has
            # worked reliably since PR #66):
            #
            #   1. start_new_session=True  → setsid() so the child is the
            #      leader of its own process group + has no controlling
            #      terminal, so SIGHUP from the install terminal doesn't
            #      cascade to it.
            #   2. stdin=subprocess.DEVNULL → no stdin tty connection.
            #   3. The finally-block below is now a no-op for api_proc —
            #      the API stays running. The user can stop it later with
            #      `nvh services stop` or by killing the pid printed in
            #      api-server.log.
            api_proc = subprocess.Popen(
                api_cmd,
                stdin=subprocess.DEVNULL,
                stdout=api_log_handle or subprocess.DEVNULL,
                stderr=subprocess.STDOUT if api_log_handle else subprocess.DEVNULL,
                env=webui_env,
                start_new_session=True,
            )
        except Exception as e:
            console.print(
                f"  [yellow]![/yellow] Could not auto-start API: {e}. "
                f"Run [bold]nvh serve[/bold] manually in another terminal."
            )
            api_proc = None
            if api_log_handle is not None:
                try:
                    api_log_handle.close()
                except Exception:
                    pass

        if api_proc is not None:
            # Cold-import time for FastAPI + nvh providers on a fresh
            # cloud VM can be 10-15s. 8s was the prior bound and is a
            # common cause of "nothing ever loaded" because the WebUI
            # opens before the API actually accepts connections. We now
            # wait up to 30s, log every 5s so the user knows we're still
            # alive, and emit a structured failure message naming the
            # log file path if it never comes up.
            #
            # Readiness contract upgrade (2026-05-21): the previous wait
            # accepted "TCP port is listening" as ready. That let stale
            # processes whose engine had crashed during init slip through
            # — port is held, /v1/health returns 500, WebUI shows red
            # banner. Now we require full /v1/health success AND
            # engine_initialized: true (via _api_healthy) so we only call
            # the API "ready" when it actually is. Fallback to TCP-only
            # is preserved with a 5s grace at the end of the wait window
            # so older nvh builds without engine_initialized in /v1/health
            # don't silently fail.
            api_ready = False
            api_wait_seconds = 30.0
            api_poll_every = 0.25
            api_status_tick = 5.0  # progress beat
            deadline = _time.monotonic() + api_wait_seconds
            next_tick = _time.monotonic() + api_status_tick
            while _time.monotonic() < deadline:
                healthy, _reason = _api_healthy(api_port)
                if healthy:
                    api_ready = True
                    elapsed = api_wait_seconds - (deadline - _time.monotonic())
                    console.print(
                        f"  [green]✓[/green] API server ready on {api_port}"
                        f" (engine initialized, took {elapsed:.1f}s)"
                    )
                    break
                if api_proc.poll() is not None:
                    log_hint = (
                        f"\n      Log: [bold]{api_log_path}[/bold]"
                        if api_log_path is not None
                        else ""
                    )
                    console.print(
                        f"  [red]✗[/red] API server exited early "
                        f"(code {api_proc.returncode}). "
                        f"The WebUI will load but every panel will be empty "
                        f"until the API is running.{log_hint}\n"
                        f"      Run [bold]nvh serve[/bold] manually or check "
                        f"the log above for the underlying error."
                    )
                    api_proc = None
                    break
                if _time.monotonic() >= next_tick:
                    console.print(
                        f"  [dim]…still waiting for API on {api_port}"
                        f" ({int(_time.monotonic() - (deadline - api_wait_seconds))}s)[/dim]"
                    )
                    next_tick += api_status_tick
                _time.sleep(api_poll_every)

            if not api_ready and api_proc is not None:
                log_hint = (
                    f" Check [bold]{api_log_path}[/bold] for the error."
                    if api_log_path is not None
                    else ""
                )
                console.print(
                    f"  [red]✗[/red] API server did not become healthy within "
                    f"{api_wait_seconds:.0f}s — the WebUI will open but "
                    f"panels will be empty.{log_hint}"
                )
                # Dump the last 25 lines of api-server.log to the console
                # AND to install.log (via the stdout tee from install.sh)
                # so the SystemConsole's Install tab surfaces the failure
                # reason without the user opening a terminal. This is the
                # "everything just works" promise's last-mile fallback:
                # if the install can't fully recover, at least make the
                # error visible in the same place the user is already
                # looking.
                if api_log_path is not None:
                    try:
                        tail_lines = api_log_path.read_text(
                            encoding="utf-8", errors="replace",
                        ).splitlines()[-25:]
                        if tail_lines:
                            console.print(
                                "  [dim]--- api-server.log tail "
                                "(last 25 lines) ---[/dim]"
                            )
                            for _line in tail_lines:
                                console.print(f"  [dim]{_line}[/dim]")
                            console.print("  [dim]--- end tail ---[/dim]")
                    except Exception:
                        # Log dump is best-effort — the WebUI's
                        # SystemConsole + DebugReportButton both also
                        # tail api-server.log directly, so the user has
                        # other paths to the same data.
                        pass

    # Surface the local LLM runtime state. The AI Wizard's router prefers
    # local Ollama (Nemotron broker) when available; if Ollama isn't
    # serving, the Wizard silently falls back to cloud providers — which
    # may not be configured, leaving the user with a "Wizard not working"
    # impression instead of an actionable status. Make it visible.
    def _ollama_reachable() -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 11434), timeout=0.5):
                return True
        except OSError:
            return False

    if _ollama_reachable():
        console.print("  [green]✓[/green] Ollama daemon reachable on 11434 (local Nemotron broker active)")
    else:
        console.print(
            "  [yellow]![/yellow] Ollama not running on 11434 — the AI Wizard will fall back to cloud providers."
        )
        console.print(
            "    [dim]To enable local Nemotron / Llama-vision: "
            "[bold]nvh workstation --all -y[/bold] (installs Ollama + pulls the GPU-matched model).[/dim]"
        )

    console.print("[bold]Starting nvHive Web UI...[/bold]")
    console.print(f"  WebUI: {access_url}")
    _webui_log(f"Starting WebUI at {access_url}")
    browser_url = f"{access_url}/setup"
    console.print(f"  Browser: {browser_url}")
    _schedule_browser_open(browser_url, chosen_port)
    if api_proc is not None:
        # Daemonized (start_new_session=True above) — the API survives
        # Ctrl+C on this terminal AND `nvh webui` exit. This message used
        # to say "will stop with Ctrl+C" which was wrong + got captured
        # into install.log via the install.sh tee, misleading users into
        # thinking they needed to keep the install terminal open.
        console.print(
            f"  [dim]API: http://localhost:{api_port} "
            f"(daemonized; survives Ctrl+C + terminal close)[/dim]"
        )
    elif api_already_running:
        console.print(f"  [dim]API: http://localhost:{api_port} (already running)[/dim]")
    console.print("  [dim]Press Ctrl+C to stop the WebUI (the API stays running)[/dim]")
    console.print()

    try:
        command = [npm, "run", "dev" if dev else "start", "--", "-p", str(chosen_port)]
        subprocess.run(command, cwd=web_dir, env=webui_env)
    except KeyboardInterrupt:
        console.print("\n[dim]Web UI stopped.[/dim]")
    finally:
        # The API server is now DAEMONIZED (start_new_session=True above), so
        # we intentionally do NOT terminate it on `nvh webui` exit. The user
        # explicitly asked for "everything should just work out of the box"
        # which means the API has to survive any terminal that ran the
        # installer. To stop the API later, the user can:
        #   - `nvh services stop`  (recommended; pid-aware)
        #   - kill the pid printed at the top of api-server.log
        # Same lifecycle as Ollama (PR #66) — daemoned-on-launch, owned by
        # the user's session leader, not the install shell.
        if api_proc is not None and api_log_path is not None:
            try:
                console.print(
                    f"  [dim]API left running in background "
                    f"(pid {api_proc.pid}); log: {api_log_path}[/dim]"
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# nvWizard rootless planning and repair
# ---------------------------------------------------------------------------

@app.command("plan", rich_help_panel="Admin")
def wizard_plan_command(
    profile: str = typer.Option(
        "student",
        "--profile",
        help="Mission profile to plan: student, creator, game, music, agent, llm, or full",
    ),
    home_dir: str | None = typer.Option(
        None,
        "--home-dir",
        help="Persistent NVH_HOME on a mounted user-writable volume",
    ),
    min_free_gb: float = typer.Option(
        200.0,
        "--min-free-gb",
        help="Minimum free space recommended for rootless AI workspaces",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print machine-readable JSON",
    ),
):
    """Preview the rootless setup plan for a mission without installing anything."""
    import json

    from nvh.integrations.workspace.passport import workspace_plan

    plan_data = workspace_plan(profile=profile, home_dir=home_dir, min_free_gb=min_free_gb)
    if json_output:
        console.print(json.dumps(plan_data, indent=2))
        return

    console.print(f"[bold]nvWizard Plan:[/bold] {plan_data['title']}")
    console.print(f"  {plan_data['summary']}")
    console.print(f"  Workspace: [bold]{plan_data['passport']['storage_home']}[/bold]\n")

    table = Table(title="Rootless Mission Steps", show_lines=False)
    table.add_column("Step", style="bold")
    table.add_column("Status")
    table.add_column("Action")
    table.add_column("Summary")
    for step in plan_data["steps"]:
        status = step["status"]
        style = "green" if status in {"pass", "ready"} else "yellow" if status == "warn" else "red"
        table.add_row(
            step["title"],
            f"[{style}]{status}[/{style}]",
            step["action_id"] or "[dim]automatic[/dim]",
            step["summary"],
        )
    console.print(table)


@app.command("repair", rich_help_panel="Admin")
def wizard_repair_command(
    home_dir: str | None = typer.Option(
        None,
        "--home-dir",
        help="Persistent NVH_HOME on a mounted user-writable volume",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print machine-readable JSON",
    ),
):
    """Run safe idempotent repairs that never use sudo or delete user assets."""
    import json

    from nvh.integrations.wizard.auto_repair import run_safe_repairs

    result = run_safe_repairs(home_dir=home_dir)
    if json_output:
        console.print(json.dumps(result, indent=2, default=str))
        return

    console.print("[bold]nvWizard Repair[/bold]")
    console.print(f"  {result.get('summary', 'Safe repair complete.')}")
    for item in result.get("results", []):
        status = item.get("status", "unknown")
        style = "green" if status in {"fixed", "ok", "skipped"} else "yellow"
        console.print(f"  [{style}]{status}[/{style}] {item.get('title') or item.get('id')}")


@app.command("wizard", rich_help_panel="Admin")
def wizard_command(
    action: str = typer.Argument(
        "status",
        help="Action: status, plan, repair, or support",
    ),
    profile: str = typer.Option(
        "student",
        "--profile",
        help="Mission profile for plan",
    ),
    home_dir: str | None = typer.Option(
        None,
        "--home-dir",
        help="Persistent NVH_HOME on a mounted user-writable volume",
    ),
    include_logs: bool = typer.Option(
        True,
        "--include-logs/--no-logs",
        help="Include redacted logs in support snapshots",
    ),
    min_free_gb: float = typer.Option(
        200.0,
        "--min-free-gb",
        help="Minimum free space recommended for rootless AI workspaces",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print machine-readable JSON",
    ),
):
    """Rootless nvWizard status, planning, repair, and support snapshot tools."""
    import json

    from nvh.integrations.wizard.auto_repair import run_safe_repairs
    from nvh.integrations.workspace.passport import (
        support_snapshot,
        workspace_passport,
        workspace_plan,
    )

    normalized = action.lower().strip()
    if normalized == "status":
        result = workspace_passport(home_dir=home_dir, create=True, min_free_gb=min_free_gb)
    elif normalized == "plan":
        result = workspace_plan(profile=profile, home_dir=home_dir, min_free_gb=min_free_gb)
    elif normalized == "repair":
        result = run_safe_repairs(home_dir=home_dir)
    elif normalized == "support":
        result = support_snapshot(home_dir=home_dir, include_logs=include_logs, min_free_gb=min_free_gb)
    else:
        console.print("[red]Unknown wizard action.[/red] Use status, plan, repair, or support.")
        raise typer.Exit(2)

    if json_output:
        console.print(json.dumps(result, indent=2, default=str))
        return

    if normalized == "status":
        console.print("[bold]nvWizard Workspace Passport[/bold]")
        console.print(f"  Workspace ID: [bold]{result['workspace_id']}[/bold]")
        console.print(f"  Home:         [bold]{result['storage_home']}[/bold]")
        console.print(f"  Policy:       {result['rootless']['policy_status']}")
        console.print(f"  Passport:     {result['passport_path']}")
    elif normalized == "plan":
        console.print(f"[bold]nvWizard Plan:[/bold] {result['title']}")
        console.print(f"  {result['summary']}")
        for step in result["steps"]:
            console.print(f"  - {step['title']}: {step['status']} ({step['action_id'] or 'automatic'})")
    elif normalized == "repair":
        console.print("[bold]nvWizard Repair[/bold]")
        console.print(f"  {result.get('summary', 'Safe repair complete.')}")
    else:
        console.print("[bold]nvWizard Support Snapshot[/bold]")
        console.print(f"  Saved: [bold]{result['path']}[/bold]")


# ---------------------------------------------------------------------------
# hive template
# ---------------------------------------------------------------------------

# Removed in 0.42 (prompt_template on agent profiles); the group stays hidden
# for one release so the old spellings print the migration hint.
template_app = typer.Typer(help="(removed) Prompt templates moved to agent profiles")
app.add_typer(template_app, name="template", hidden=True, rich_help_panel="Subcommands")


def _template_removed() -> None:
    console.print(f"[yellow]`nvh template` was removed in 0.42.[/yellow] {_TEMPLATE_MIGRATION_HINT}")
    console.print(
        "[dim]Example profile YAML:\n"
        "  name: code_review\n"
        "  system_prompt: You are a senior code reviewer.\n"
        "  prompt_template: |\n"
        "    Review this code for bugs, security and readability:\n"
        "    {{input}}[/dim]"
    )
    raise typer.Exit(1)


@template_app.command("list", hidden=True)
def template_list() -> None:
    _template_removed()


@template_app.command("show", hidden=True)
def template_show(name: str = typer.Argument("")) -> None:
    _template_removed()


@template_app.command("create", hidden=True)
def template_create(name: str = typer.Argument("")) -> None:
    _template_removed()


# ---------------------------------------------------------------------------
# hive workflow
# ---------------------------------------------------------------------------

workflow_app = typer.Typer(help="Manage and run workflow pipelines")
app.add_typer(workflow_app, name="workflow", rich_help_panel="Subcommands")


@workflow_app.command("list")
def workflow_list():
    """List available workflows."""
    from nvh.core.workflows import discover_workflows

    workflows = discover_workflows()
    if not workflows:
        console.print("[dim]No workflows found.[/dim]")
        console.print(
            "[dim]Add YAML files to ~/.hive/workflows/ or .hive/workflows/[/dim]"
        )
        return

    table = Table(title="Available Workflows")
    table.add_column("Name", style="bold cyan")
    table.add_column("Description")
    table.add_column("Path", style="dim")

    for wf_name, path in sorted(workflows.items()):
        try:
            from nvh.core.workflows import load_workflow
            wf = load_workflow(path)
            desc = wf.description or "[dim]—[/dim]"
        except Exception:
            desc = "[red]Error loading[/red]"
        table.add_row(wf_name, desc, str(path))

    console.print(table)
    console.print("\n[dim]Run: nvh workflow run <name> --input \"...\"[/dim]")


@workflow_app.command("run")
def workflow_run(
    name: str = typer.Argument(..., help="Workflow name"),
    input: str = typer.Option("", "--input", "-i", help="Input text passed as {{input}}"),
    file: str = typer.Option("", "--file", "-f", help="Read input from a file"),
):
    """Run a workflow pipeline."""
    from nvh.config.settings import load_config
    from nvh.core.engine import Engine
    from nvh.core.workflows import discover_workflows, load_workflow, run_workflow

    # Resolve input
    input_text = input
    if file:
        try:
            input_text = Path(file).read_text()
        except OSError as e:
            console.print(f"[red]Cannot read file '{file}': {e}[/red]")
            raise typer.Exit(1)

    # Find the workflow
    workflows = discover_workflows()
    if name not in workflows:
        console.print(f"[red]Workflow '{name}' not found.[/red]")
        console.print(f"[dim]Available: {', '.join(sorted(workflows)) or 'none'}[/dim]")
        raise typer.Exit(1)

    try:
        wf = load_workflow(workflows[name])
    except Exception as e:
        console.print(f"[red]Failed to load workflow '{name}': {e}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold cyan]Running workflow:[/bold cyan] {wf.name}")
    if wf.description:
        console.print(f"[dim]{wf.description}[/dim]")
    console.print(f"[dim]{len(wf.steps)} step(s)[/dim]\n")


    def on_step(step_name: str, status: str, result: str) -> None:
        icons = {
            "running": "[yellow]...[/yellow]",
            "done": "[green]OK[/green]",
            "skipped": "[dim]SKIP[/dim]",
            "error": "[red]ERR[/red]",
        }
        icon = icons.get(status, status)
        if status == "running":
            console.print(f"  {icon} {step_name}")
        elif status == "done":
            console.print(f"  {icon} {step_name}" + (f" — {result[:80]}..." if result else ""))
        elif status == "skipped":
            console.print(f"  {icon} {step_name} (skipped)")
        elif status == "error":
            console.print(f"  {icon} {step_name}: {result}")

    async def _run() -> None:
        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()

        initial_vars = {}
        if input_text:
            initial_vars["input"] = input_text

        result = await run_workflow(wf, engine, initial_vars=initial_vars, on_step=on_step)

        console.print()
        if result.success:
            console.print(
                f"[green]Workflow complete.[/green]"
                f" ({result.steps_completed}/{result.steps_total} steps)"
            )
            # Print the final saved variable (last save_as), if any
            last_step = next(
                (s for s in reversed(wf.steps) if s.save_as),
                None,
            )
            if last_step and last_step.save_as in result.variables:
                console.print(f"\n[bold]Result ({last_step.save_as}):[/bold]")
                console.print(Markdown(result.variables[last_step.save_as]))
        else:
            console.print(f"[red]Workflow failed:[/red] {result.error}")
            raise typer.Exit(1)

    _run_async = asyncio.get_event_loop().run_until_complete if False else None
    asyncio.run(_run())


@workflow_app.command("show")
def workflow_show(
    name: str = typer.Argument(..., help="Workflow name"),
):
    """Show workflow steps and description."""
    from nvh.core.workflows import discover_workflows, load_workflow

    workflows = discover_workflows()
    if name not in workflows:
        console.print(f"[red]Workflow '{name}' not found.[/red]")
        console.print(f"[dim]Available: {', '.join(sorted(workflows)) or 'none'}[/dim]")
        raise typer.Exit(1)

    try:
        wf = load_workflow(workflows[name])
    except Exception as e:
        console.print(f"[red]Failed to load workflow '{name}': {e}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold cyan]{wf.name}[/bold cyan]")
    if wf.description:
        console.print(f"[dim]{wf.description}[/dim]")
    console.print(f"\n[bold]{len(wf.steps)} step(s):[/bold]")

    for i, step in enumerate(wf.steps, 1):
        action_style = {
            "ask": "cyan", "convene": "magenta", "poll": "yellow",
            "safe": "green", "shell": "red",
        }.get(step.action, "white")
        console.print(
            f"\n  [bold]{i}. {step.name}[/bold]"
            f"  [{action_style}]{step.action}[/{action_style}]"
        )
        if step.advisor:
            console.print(f"     advisor: {step.advisor}")
        if step.cabinet:
            console.print(f"     cabinet: {step.cabinet}")
        if step.condition:
            console.print(f"     condition: {step.condition}")
        if step.save_as:
            console.print(f"     save_as: [italic]{step.save_as}[/italic]")
        prompt_preview = step.prompt[:120].replace("\n", " ")
        if len(step.prompt) > 120:
            prompt_preview += "..."
        console.print(f"     prompt: [dim]{prompt_preview}[/dim]")

    console.print(f"\n[dim]Run: nvh workflow run {name} --input \"...\"[/dim]")


# ---------------------------------------------------------------------------
# nvh completions
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Admin")
def completions(
    shell: str = typer.Argument("bash", help="Shell: bash, zsh, fish"),
    install: bool = typer.Option(False, "--install", help="Auto-install into shell config"),
):
    """Generate or install shell completion scripts."""
    from nvh.cli.completions import get_completion_script, install_completion

    if shell not in ("bash", "zsh", "fish"):
        console.print(f"[red]Unsupported shell '{shell}'. Choose from: bash, zsh, fish[/red]")
        raise typer.Exit(1)

    try:
        script = get_completion_script(shell)
    except Exception as e:
        console.print(f"[red]Error generating completion script: {e}[/red]")
        raise typer.Exit(1)

    if install:
        success, message = install_completion(shell, script)
        if success:
            console.print(f"[green]Completions installed:[/green] {message}")
            _reload_hint(shell)
        else:
            console.print(f"[red]Installation failed:[/red] {message}")
            raise typer.Exit(1)
    else:
        # Print the script so the user can inspect or pipe it
        console.print(script, highlight=False)
        console.print(
            f"\n[dim]Tip: run `nvh completions {shell} --install` to install automatically.[/dim]"
        )


def _reload_hint(shell: str) -> None:
    hints = {
        "bash": "Run `source ~/.bashrc` or open a new terminal to activate completions.",
        "zsh": "Run `source ~/.zshrc` or open a new terminal to activate completions.",
        "fish": "Open a new terminal to activate completions.",
    }
    console.print(f"[dim]{hints.get(shell, '')}[/dim]")


# ---------------------------------------------------------------------------
# nvh do — agentic hands-free task execution
# ---------------------------------------------------------------------------

@app.command("do", rich_help_panel="Core")
def do_task(
    task: str = typer.Argument(..., help="Task for the agent to complete"),
    advisor: str | None = typer.Option(None, "-a", "--advisor", help="Specific advisor to use"),
    model: str | None = typer.Option(None, "-m", "--model", help="Specific model to use"),
    max_steps: int = typer.Option(15, "--max-steps", help="Maximum agent iterations"),
    auto: bool = typer.Option(
        True, "--auto/--confirm",
        help="Auto-approve safe tools (default: yes)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Show what would be done without executing",
    ),
    sandbox: bool = typer.Option(
        False, "--sandbox",
        help="Require Docker isolation for run_code/shell (refuse the unisolated fallback)",
    ),
    profile: str | None = typer.Option(None, "--profile", help="Config profile to use"),
):
    """Hands-free mode — give NVHive a task and it completes it autonomously.

    The agent can read/write files, search the web, run code, and more.
    Safe tools (read, search) run automatically by default. Unsafe tools
    (write, execute) always ask for confirmation. run_code/shell use Docker
    when it is available; --sandbox makes that mandatory.

    Examples:

      nvh do "Find all TODO comments in this project and create a summary"

      nvh do "Search the web for Python FastAPI tutorials and summarize the top 3"

      nvh do "Read README.md and suggest improvements"

      nvh do "Run the test suite and fix the first failure" --sandbox
    """
    import time as _time

    from nvh.config.settings import load_config
    from nvh.core.agent_loop import AgentStep, run_agent_loop
    from nvh.core.engine import Engine
    from nvh.core.tools import ToolRegistry

    if sandbox:
        os.environ["NVH_SANDBOX_REQUIRE_DOCKER"] = "1"

    async def _run_do():
        config = load_config(profile=profile)
        engine = Engine(config=config)
        await engine.initialize()

        tools = ToolRegistry()

        # --dry-run: show analysis without executing
        if dry_run:
            task_preview = task if len(task) <= 60 else task[:57] + "..."
            console.print()
            console.print(Panel(
                f"[bold]Task:[/bold] {task_preview}",
                title="[bold yellow]Dry Run[/bold yellow]",
                border_style="yellow",
                expand=False,
            ))
            console.print()

            # Show routing decision
            effective_advisor = advisor or config.defaults.provider or "(auto-selected)"
            effective_model = model or "(provider default)"
            console.print(
                f"[bold]Routing:[/bold]"
                f"  advisor=[cyan]{effective_advisor}[/cyan]"
                f"  model=[cyan]{effective_model}[/cyan]"
            )
            console.print(f"[bold]Max steps:[/bold] {max_steps}")
            console.print(f"[bold]Auto-approve safe tools:[/bold] {'yes' if auto else 'no'}")
            console.print()

            # Show available tools
            tool_list = tools.list_tools()
            safe_tools = [t.name for t in tool_list if t.safe]
            unsafe_tools = [t.name for t in tool_list if not t.safe]
            console.print(f"[bold]Available tools ({len(tool_list)} total):[/bold]")
            console.print(f"  [green]Safe (auto-run):[/green] {', '.join(safe_tools) or 'none'}")
            console.print(
                "  [yellow]Unsafe (require approval):[/yellow]"
                f" {', '.join(unsafe_tools) or 'none'}"
            )
            console.print()

            # Show spending cap
            budget = config.budget
            hard = "yes" if budget.hard_stop else "no"
            console.print(
                f"[bold]Spending caps:[/bold]"
                f"  daily=${budget.daily_limit_usd}"
                f"  monthly=${budget.monthly_limit_usd}"
                f"  hard_stop={hard}"
            )
            console.print()

            console.print(
                "[bold yellow]Dry run complete."
                " Remove --dry-run to execute.[/bold yellow]"
            )
            return

        start = _time.monotonic()
        step_count = 0

        # Header panel
        task_preview = task if len(task) <= 50 else task[:47] + "..."
        console.print()
        console.print(Panel(
            f"[bold]Task:[/bold] {task_preview}",
            title="[bold cyan]Agent Working[/bold cyan]",
            border_style="cyan",
            expand=False,
        ))
        console.print()

        def on_step(step: AgentStep) -> None:
            nonlocal step_count
            step_count += 1
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
                    suffix = "..." if len(result.output) > 60 else ""
                    console.print(
                        f"  [green]✓[/green]"
                        f" [dim]{preview}{suffix}[/dim]"
                    )
                else:
                    console.print(f"  [red]✗[/red] [dim]{result.error}[/dim]")
            if not step.tool_calls:
                console.print("  [dim](no tools — generating final answer)[/dim]")
            console.print()

        def confirm_unsafe(tool_name: str, tool_args: dict) -> bool:
            args_str = ", ".join(f"{k}={repr(v)[:40]}" for k, v in tool_args.items())
            console.print(f"\n[yellow]Agent wants to:[/yellow] {tool_name}({args_str})")
            try:
                answer = input("Allow? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            return answer in ("y", "yes")

        result = await run_agent_loop(
            task=task,
            engine=engine,
            tools=tools,
            provider=advisor,
            model=model,
            max_iterations=max_steps,
            auto_approve_safe=auto,
            on_step=on_step,
            confirm_unsafe=confirm_unsafe,
        )

        elapsed = _time.monotonic() - start

        # Result panel
        console.print(Panel(
            result.final_response,
            title="[bold green]Result[/bold green]",
            border_style="green",
        ))

        # Stats line
        status = "[green]completed[/green]" if result.completed else "[yellow]incomplete[/yellow]"
        console.print(
            f"\n[dim]{result.total_iterations} step(s) | "
            f"{result.total_tool_calls} tool call(s) | "
            f"{elapsed:.1f}s | {status}[/dim]"
        )
        if result.error and not result.completed:
            console.print(f"[dim yellow]Note: {result.error}[/dim yellow]")

        # Desktop notification when task finishes
        from nvh.core.notify import notify_task_complete
        preview = result.final_response[:100].replace("\n", " ")
        await notify_task_complete(task[:50], preview)

    _run(_run_do())


# ---------------------------------------------------------------------------
# nvh agent run — tier-aware coding agent (beta). Lives under the `agent`
# group: a top-level `agent` command was silently shadowed by that group.
# ---------------------------------------------------------------------------

@agent_app.command("run")
def agent(
    task: str = typer.Argument("", help="Coding task for the agent to complete"),
    tier: str | None = typer.Option(None, "--tier", help="Force GPU tier: 0-5 (auto-detects if omitted)"),
    mode: str = typer.Option("auto", "--mode", help="Model mode: auto, single, multi"),
    working_dir: str = typer.Option(".", "-d", "--dir", help="Working directory (codebase root)"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip write confirmations"),
    max_steps: int = typer.Option(10, "--max-steps", help="Maximum agent iterations"),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip the verification phase"),
    no_quality: bool = typer.Option(False, "--no-quality", help="Skip lint/syntax quality gates"),
    git: bool = typer.Option(False, "--git", help="Create branch + commit changes"),
    setup: bool = typer.Option(False, "--setup", help="Pull recommended models for your GPU tier"),
    remove: bool = typer.Option(False, "--remove", help="Remove models pulled by --setup"),
    profile: str | None = typer.Option(None, "--profile", help="Config profile to use"),
):
    """[beta] Agentic coding — plan, execute, and verify code changes.

    Uses a hierarchical multi-model approach: a strong model (cloud or
    local 70B) plans and verifies, while a local model executes the
    changes using file and shell tools.

    Automatically scales based on your GPU:

      Tier 5 (128 GB+, DGX Spark):     fully local: 3 models (plan + code + review)
      Tier 4 (96 GB, RTX 6000 Pro BSE): dual-model: 70B + 32B local
      Tier 3 (48 GB, A100/A6000):       cloud planner + local 70B coder (--mode multi for 2 models)
      Tier 2 (24 GB, RTX 3090/4090):    cloud planner + local 27B coder
      Tier 1 (16 GB, RTX 4060 Ti):      cloud planner + local 7B coder
      Tier 0 (no GPU):                  fully cloud

    Examples:

      nvh agent run "Fix the streaming timeout bug in council.py"

      nvh agent run "Add unit tests for the auth middleware" --dir /d/GitHub/project

      nvh agent run "Refactor the router to filter by health score" --tier 3

      nvh agent run "Read the codebase and create a CONTRIBUTING.md" -y
    """
    import time as _time
    from pathlib import Path as _Path

    from nvh.config.settings import load_config
    from nvh.core.agent_loop import AgentStep
    from nvh.core.agentic import (
        TIER_DESCRIPTIONS,
        AgentMode,
        AgentTier,
        auto_detect_config,
        build_agent_config,
        detect_agent_tier,
        run_coding_agent,
    )
    from nvh.core.engine import Engine

    # Parse mode
    mode_enum = {
        "auto": AgentMode.AUTO,
        "single": AgentMode.SINGLE,
        "multi": AgentMode.MULTI,
    }.get(mode, AgentMode.AUTO)

    # ── --setup / --remove: pull or remove recommended models ──────────
    if setup or remove:
        from nvh.utils.gpu import detect_gpus
        gpus = detect_gpus()
        total_vram = sum(g.vram_gb for g in gpus) if gpus else 0
        tier_enum = detect_agent_tier(total_vram)
        agent_config = build_agent_config(tier_enum, mode=mode_enum)

        models_to_manage: list[str] = []
        if agent_config.worker_model and "ollama/" in (agent_config.worker_model or ""):
            # Strip the "ollama/" prefix for the ollama CLI
            models_to_manage.append(agent_config.worker_model.replace("ollama/", ""))
        if agent_config.orchestrator_model and "ollama/" in (agent_config.orchestrator_model or ""):
            orch_model = agent_config.orchestrator_model.replace("ollama/", "")
            if orch_model not in models_to_manage:
                models_to_manage.append(orch_model)

        if not models_to_manage:
            console.print(
                f"[yellow]Tier {tier_enum.value}: no local models needed "
                f"(fully cloud). Nothing to {'pull' if setup else 'remove'}.[/yellow]"
            )
            return

        action = "Pulling" if setup else "Removing"
        console.print(
            f"\n[bold]Agent {action.lower()} for {tier_enum.value} "
            f"({total_vram:.0f} GB VRAM detected):[/bold]"
        )
        for m in models_to_manage:
            console.print(f"  {'[green]+[/green]' if setup else '[red]-[/red]'} {m}")
        console.print()

        import shutil
        ollama_exe = shutil.which("ollama")
        if not ollama_exe:
            console.print(
                "[red]Ollama not found in PATH.[/red]\n"
                "Install rootlessly: [bold]nvh studio --install rootless-ollama -y[/bold]"
            )
            raise typer.Exit(1)

        import subprocess
        for m in models_to_manage:
            cmd = "pull" if setup else "rm"
            console.print(f"[bold]ollama {cmd} {m}[/bold]")
            try:
                subprocess.run(
                    [ollama_exe, cmd, m],
                    check=True,
                    timeout=1800,  # 30 min for large model pulls
                )
                console.print("  [green]done[/green]")
            except subprocess.CalledProcessError as e:
                console.print(f"  [red]failed (exit {e.returncode})[/red]")
            except subprocess.TimeoutExpired:
                console.print("  [red]timed out after 30 minutes[/red]")
        console.print()
        console.print(
            "[green]Setup complete.[/green] Run [bold]nvh agent run \"your task\"[/bold] to start."
            if setup else "[green]Models removed.[/green]"
        )
        return

    if not task:
        console.print("[red]Please provide a task or use --setup / --remove.[/red]")
        console.print("Example: [bold]nvh agent run \"Fix the bug in main.py\"[/bold]")
        raise typer.Exit(1)

    async def _run_agent():
        config = load_config(profile=profile)
        engine = Engine(config=config)
        await engine.initialize()

        # Determine tier
        if tier is not None:
            tier_enum = {
                "0": AgentTier.TIER_0,
                "1": AgentTier.TIER_1,
                "2": AgentTier.TIER_2,
                "3": AgentTier.TIER_3,
                "4": AgentTier.TIER_4,
                "5": AgentTier.TIER_5,
            }.get(tier)
            if tier_enum is None:
                console.print(f"[red]Invalid tier: {tier}. Use 0-5.[/red]")
                raise typer.Exit(1)
            agent_config = build_agent_config(tier_enum, registry=engine.registry, mode=mode_enum)
        else:
            agent_config = auto_detect_config(engine, mode=mode_enum)

        agent_config.max_iterations = max_steps
        agent_config.verify_results = not no_verify
        agent_config.quality_gates = not no_quality
        agent_config.git_integration = git

        work_path = _Path(working_dir).resolve()

        # Header
        console.print()
        tier_desc = TIER_DESCRIPTIONS.get(agent_config.tier, "")
        reviewer_line = (
            f"\n[bold]Reviewer:[/bold] {agent_config.reviewer_model}"
            if agent_config.reviewer_model else ""
        )
        console.print(Panel(
            f"[bold]Task:[/bold] {task}\n"
            f"[bold]Tier:[/bold] {agent_config.tier.value} — {tier_desc}\n"
            f"[bold]Mode:[/bold] {agent_config.mode.value}\n"
            f"[bold]Orchestrator:[/bold] {agent_config.orchestrator_model or 'engine default'}\n"
            f"[bold]Worker:[/bold] {agent_config.worker_model or 'engine default'}"
            f"{reviewer_line}\n"
            f"[bold]Directory:[/bold] {work_path}\n"
            f"[bold]Quality gates:[/bold] {'yes' if agent_config.quality_gates else 'no'} | "
            f"[bold]Git:[/bold] {'yes' if agent_config.git_integration else 'no'} | "
            f"[bold]Verify:[/bold] {'yes' if agent_config.verify_results else 'no'}",
            title="[bold #76B900]Agent Coding (beta)[/bold #76B900]",
            border_style="#76B900",
            expand=False,
        ))
        console.print()

        start = _time.monotonic()
        step_count = 0

        def on_step(step: AgentStep) -> None:
            nonlocal step_count
            step_count += 1
            thought_preview = step.thought[:100].rstrip() if step.thought else ""
            label = f"[bold]Step {step.iteration}[/bold]"
            if thought_preview and thought_preview != "Task complete":
                label += f": {thought_preview}{'...' if len(step.thought) > 100 else ''}"
            console.print(label)
            for call in step.tool_calls:
                args_str = ", ".join(
                    f"{k}={repr(v)[:50]}" for k, v in call.get("args", {}).items()
                )
                console.print(
                    f"  [dim]tool:[/dim] [cyan]{call['tool']}[/cyan]({args_str})"
                )
            for result in step.tool_results:
                if result.success:
                    preview = result.output[:80].replace("\n", " ").rstrip()
                    suffix = "..." if len(result.output) > 80 else ""
                    console.print(f"  [green]ok[/green] [dim]{preview}{suffix}[/dim]")
                else:
                    console.print(f"  [red]err[/red] [dim]{result.error[:80]}[/dim]")
            if not step.tool_calls:
                console.print("  [dim](no tools -- generating final answer)[/dim]")
            console.print()

        def confirm_write(tool_name: str, tool_args: dict) -> bool:
            args_str = ", ".join(
                f"{k}={repr(v)[:50]}" for k, v in tool_args.items()
            )
            console.print(
                f"\n[yellow]Agent wants to:[/yellow] {tool_name}({args_str})"
            )
            try:
                answer = input("Allow? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            return answer in ("y", "yes")

        import sys as _sys

        def _stream_token(delta: str) -> None:
            _sys.stdout.write(delta)
            _sys.stdout.flush()

        result = await run_coding_agent(
            task=task,
            engine=engine,
            config=agent_config,
            working_dir=work_path,
            on_step=on_step,
            confirm_write=None if yes else confirm_write,
            on_token=_stream_token,
        )
        console.print()  # newline after streamed output

        elapsed = _time.monotonic() - start

        # Result panel
        if result.error:
            console.print(Panel(
                f"[red]{result.error}[/red]",
                title="[bold red]Agent Error[/bold red]",
                border_style="red",
            ))
        else:
            console.print(Panel(
                result.final_summary or "(no summary)",
                title="[bold green]Result[/bold green]",
                border_style="green",
            ))

        # Changes summary
        if result.files_modified or result.files_created:
            console.print()
            if result.files_modified:
                console.print("[bold]Modified:[/bold]")
                for f in result.files_modified:
                    console.print(f"  [yellow]M[/yellow] {f}")
            if result.files_created:
                console.print("[bold]Created:[/bold]")
                for f in result.files_created:
                    console.print(f"  [green]A[/green] {f}")

        # Quality gates
        if result.quality_gate_passed is not None:
            gate_color = "green" if result.quality_gate_passed else "red"
            gate_label = "Passed" if result.quality_gate_passed else "Failed"
            console.print(f"\n[bold]Quality gates:[/bold] [{gate_color}]{gate_label}[/{gate_color}]")
            if not result.quality_gate_passed and result.quality_gate_output:
                console.print(f"[dim]{result.quality_gate_output[:200]}[/dim]")

        # Verification status
        if result.verification:
            approved = "APPROVED" in result.verification.upper()
            color = "green" if approved else "yellow"
            label = "Approved" if approved else "Needs review"
            reviewer = f" (by {result.reviewer_model})" if result.reviewer_model else ""
            console.print(f"[bold]Verification:[/bold] [{color}]{label}{reviewer}[/{color}]")

        # Stats
        status = "[green]completed[/green]" if result.completed else "[yellow]incomplete[/yellow]"
        console.print(
            f"\n[dim]{result.total_iterations} step(s) | "
            f"{result.total_tool_calls} tool call(s) | "
            f"{result.files_read.__len__()} file(s) read | "
            f"{elapsed:.1f}s | {status}[/dim]"
        )

    _run(_run_agent())


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# nvh review — AI code review (beta)
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Core")
def review(
    source: str = typer.Argument("staged", help="What to review: staged, HEAD~N..HEAD, or PR number"),
    tier: str | None = typer.Option(None, "--tier", help="Force GPU tier: 0-5"),
    mode: str = typer.Option("auto", "--mode", help="Model mode: auto, single, multi"),
    working_dir: str = typer.Option(".", "-d", "--dir", help="Working directory"),
    profile: str | None = typer.Option(None, "--profile", help="Config profile"),
):
    """[beta] AI-powered code review with multi-model cross-verification.

    Reviews code changes using one or more LLM models. In multi-model mode,
    two different architectures review independently — catching bugs that
    self-review misses.

    Examples:

      nvh review                          # review staged changes
      nvh review HEAD~3..HEAD             # review last 3 commits
      nvh review 42                       # review GitHub PR #42
      nvh review --mode multi             # two models review independently
    """
    from pathlib import Path as _Path

    from nvh.config.settings import load_config
    from nvh.core.agentic import (
        AgentMode,
        AgentTier,
        auto_detect_config,
        build_agent_config,
    )
    from nvh.core.engine import Engine

    async def _run_review():
        config = load_config(profile=profile)
        engine = Engine(config=config)
        await engine.initialize()

        mode_enum = {"auto": AgentMode.AUTO, "single": AgentMode.SINGLE, "multi": AgentMode.MULTI}.get(mode, AgentMode.AUTO)

        if tier is not None:
            tier_map = {str(i): t for i, t in enumerate([AgentTier.TIER_0, AgentTier.TIER_1, AgentTier.TIER_2, AgentTier.TIER_3, AgentTier.TIER_4, AgentTier.TIER_5])}
            agent_config = build_agent_config(tier_map.get(tier, AgentTier.TIER_0), registry=engine.registry, mode=mode_enum)
        else:
            agent_config = auto_detect_config(engine, mode=mode_enum)

        work_path = _Path(working_dir).resolve()

        try:
            from nvh.core.agent_review import review_changes
            result = await review_changes(engine, agent_config, work_path, source)
        except ImportError:
            console.print("[red]Code review module not available.[/red]")
            raise typer.Exit(1)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)

        # Display results
        console.print()
        status = "[green]Approved[/green]" if result.approved else "[yellow]Changes requested[/yellow]"
        console.print(Panel(
            f"[bold]Review:[/bold] {status}\n"
            f"[bold]Summary:[/bold] {result.summary}\n"
            f"[bold]Findings:[/bold] {len(result.findings)}\n"
            f"[bold]Models:[/bold] {', '.join(result.reviewer_models)}",
            title="[bold #76B900]Code Review (beta)[/bold #76B900]",
            border_style="#76B900",
        ))

        for finding in result.findings:
            sev_color = {"high": "red", "medium": "yellow", "low": "cyan", "info": "dim"}.get(finding.severity, "white")
            loc = f"{finding.file}:{finding.line}" if finding.line else finding.file
            console.print(f"\n  [{sev_color}]{finding.severity.upper()}[/{sev_color}] [{sev_color}]{finding.category}[/{sev_color}] at {loc}")
            console.print(f"    {finding.issue}")
            if finding.suggestion:
                console.print(f"    [dim]Fix: {finding.suggestion}[/dim]")

        console.print(f"\n[dim]{len(result.findings)} finding(s) | {result.duration_ms}ms[/dim]")

    _run(_run_review())


# ---------------------------------------------------------------------------
# nvh test-gen — AI test generation (beta)
# ---------------------------------------------------------------------------

@app.command("test-gen", rich_help_panel="Core")
def test_gen(
    target: str = typer.Argument(..., help="File path, --coverage-gaps, or --for-pr"),
    tier: str | None = typer.Option(None, "--tier", help="Force GPU tier: 0-5"),
    working_dir: str = typer.Option(".", "-d", "--dir", help="Working directory"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip write confirmations"),
    profile: str | None = typer.Option(None, "--profile", help="Config profile"),
):
    """[beta] AI-powered test generation with automatic verification.

    Reads source code, identifies untested paths, generates pytest tests,
    runs them, and iterates until they pass. Uses quality gates to ensure
    the generated tests actually work.

    Examples:

      nvh test-gen nvh/core/council.py            # tests for a specific file
      nvh test-gen --coverage-gaps                 # find and fill coverage gaps
      nvh test-gen nvh/api/server.py -y            # auto-approve test writes
    """
    from pathlib import Path as _Path

    from nvh.config.settings import load_config
    from nvh.core.agentic import auto_detect_config
    from nvh.core.engine import Engine

    async def _run_testgen():
        config = load_config(profile=profile)
        engine = Engine(config=config)
        await engine.initialize()
        agent_config = auto_detect_config(engine)
        work_path = _Path(working_dir).resolve()

        try:
            from nvh.core.agent_testgen import generate_tests
        except ImportError:
            console.print("[red]Test generation module not available.[/red]")
            raise typer.Exit(1)

        console.print(Panel(
            f"[bold]Target:[/bold] {target}\n"
            f"[bold]Tier:[/bold] {agent_config.tier.value}\n"
            f"[bold]Worker:[/bold] {agent_config.worker_model or 'engine default'}",
            title="[bold #76B900]Test Generation (beta)[/bold #76B900]",
            border_style="#76B900",
            expand=False,
        ))

        result = await generate_tests(engine, agent_config, work_path, target)

        # Display results
        if result.test_file:
            console.print(f"\n[green]Tests written to:[/green] {result.test_file}")
        console.print(f"[bold]Generated:[/bold] {result.tests_generated} test(s)")
        console.print(f"[bold]Passing:[/bold] [green]{result.tests_passing}[/green] | [bold]Failing:[/bold] [red]{result.tests_failing}[/red]")
        if result.coverage_after is not None:
            console.print(f"[bold]Coverage:[/bold] {result.coverage_before or 0:.0f}% → {result.coverage_after:.0f}%")
        console.print(f"[dim]{result.duration_ms}ms | model: {result.model_used}[/dim]")

    _run(_run_testgen())


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# nvh workspace — multi-repo management (beta)
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Subcommands")
def workspace(
    action: str = typer.Argument("list", help="Action: add, list, scan, remove"),
    paths: list[str] = typer.Argument(None, help="Repo paths to add"),
    name: str = typer.Option("", "--name", help="Workspace name"),
):
    """[beta] Manage multi-repo workspaces for cross-repo agent operations.

    Read across multiple repositories to build consolidated context.
    The agent can understand dependencies between repos and suggest
    coordinated changes.

    Examples:

      nvh workspace add ~/backend ~/frontend ~/shared-lib
      nvh workspace list
      nvh workspace scan
      nvh workspace remove
    """
    from pathlib import Path as _Path

    try:
        from nvh.core.workspace import (
            create_workspace,
            format_workspace_context,
            load_workspace,
            save_workspace,
            scan_workspace,
        )
    except ImportError:
        console.print("[red]Workspace module not available.[/red]")
        raise typer.Exit(1)

    if action == "add" and paths:
        ws = create_workspace([_Path(p).resolve() for p in paths], name=name)
        save_workspace(ws, _Path("."))
        console.print(f"[green]Workspace created with {len(ws.repos)} repo(s).[/green]")
        for r in ws.repos:
            console.print(f"  {r.name}: {r.file_count} files, {r.language}")

    elif action == "list":
        ws = load_workspace(_Path("."))
        if ws is None:
            console.print("[dim]No workspace configured. Use: nvh workspace add ~/repo1 ~/repo2[/dim]")
            return
        console.print(f"[bold]Workspace:[/bold] {ws.name or '(unnamed)'}")
        for r in ws.repos:
            console.print(f"  {r.name}: {r.file_count} files, {r.language} {'(readonly)' if r.readonly else ''}")

    elif action == "scan":
        ws = load_workspace(_Path("."))
        if ws is None:
            console.print("[red]No workspace. Use: nvh workspace add ~/repo1 ~/repo2[/red]")
            raise typer.Exit(1)
        summary = scan_workspace(ws)
        console.print(format_workspace_context(ws, summary))

    elif action == "remove":
        ws_file = _Path(".nvhive/workspace.json")
        if ws_file.exists():
            ws_file.unlink()
            console.print("[green]Workspace removed.[/green]")
        else:
            console.print("[dim]No workspace to remove.[/dim]")


# ---------------------------------------------------------------------------
# nvh snapshot — save/restore environment state
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Admin")
def snapshot(
    action: str = typer.Argument("save", help="Action: save, restore, list"),
    file: str | None = typer.Argument(
        None, help="Snapshot tarball (written by save; read by restore / list)",
    ),
    output: str | None = typer.Option(
        None, "-o", "--output",
        help="Where to write the tarball (default: $NVH_HOME/snapshots/snapshot-<UTC>.tar.gz)",
    ),
    home_dir: str | None = typer.Option(
        None, "--home-dir", help="NVH_HOME to bundle from / restore into (default: active)",
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace files that already exist when restoring",
    ),
):
    """Bundle your NVH_HOME workspace so it survives a new machine.

    Captures the vault, RAG index, install receipts, conversations database
    and workspace preferences into one tarball. API keys and model weights
    are never included — re-run `nvh setup` (or paste keys) and re-pull
    models on the destination.

    Examples:

      nvh snapshot save                           # $NVH_HOME/snapshots/snapshot-<UTC>.tar.gz
      nvh snapshot save ~/backups/state.tar.gz    # custom path (or -o)
      nvh snapshot restore state.tar.gz           # into the active NVH_HOME
      nvh snapshot restore state.tar.gz --overwrite --home-dir /mnt/persist/nvhive
      nvh snapshot list state.tar.gz              # show bundled paths
    """
    from nvh.integrations.workspace.snapshot import (
        export_snapshot,
        import_snapshot,
        list_snapshot,
    )

    # `-o` doubled as the input path before 0.41.1; keep accepting it.
    target = file or output

    if action == "save":
        result = export_snapshot(home_dir=home_dir, out_path=target)
        if not result.get("ok"):
            console.print(f"[red]{result.get('error', 'snapshot failed')}[/red]")
            raise typer.Exit(1)
        manifest = result["manifest"]
        console.print(f"[green]Snapshot saved:[/green] {result['path']}")
        console.print(
            f"  {len(manifest['includes'])} path(s), {result['bytes'] / 1024:.1f} KB,"
            f" from {manifest['source_home']}"
        )
        if not manifest["includes"]:
            console.print("  [yellow]Nothing to bundle yet — is this the right NVH_HOME?[/yellow]")
        console.print(
            "  [dim]API keys are not bundled; re-run `nvh setup` on the new machine.[/dim]"
        )

    elif action == "restore":
        if not target:
            console.print("[red]Usage: nvh snapshot restore <file.tar.gz>[/red]")
            raise typer.Exit(1)
        result = import_snapshot(target, home_dir=home_dir, overwrite=overwrite)
        if not result.get("ok"):
            console.print(f"[red]{result.get('error', 'restore failed')}[/red]")
            raise typer.Exit(1)
        console.print(
            f"[green]Restored {result['extracted']} path(s) into {result['target_home']}[/green]"
        )
        if result["skipped"]:
            console.print(
                f"  {result['skipped']} skipped (already present — use --overwrite to replace)"
            )

    elif action == "list":
        if not target:
            console.print("[red]Usage: nvh snapshot list <file.tar.gz>[/red]")
            raise typer.Exit(1)
        result = list_snapshot(target)
        if not result.get("ok"):
            console.print(f"[red]{result.get('error', 'unreadable snapshot')}[/red]")
            raise typer.Exit(1)
        manifest = result.get("manifest") or {}
        if manifest:
            console.print(
                f"[bold]{target}[/bold] — exported {manifest.get('exported_at', '?')}"
                f" from {manifest.get('source_home', '?')}"
            )
        for member in result["members"]:
            console.print(f"  {member['name']}  [dim]({member['size']} B)[/dim]")

    else:
        console.print(f"[red]Unknown action: {action}[/red] (use save, restore, list)")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# nvh costs — usage and cost reporting
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Admin")
def costs(
    period: str = typer.Argument("today", help="Period: today, week, month"),
):
    """Show usage costs, savings from local inference, and recommendations.

    Examples:

      nvh costs              # today's costs
      nvh costs week         # this week
      nvh costs month        # this month
    """
    async def _run():
        try:
            from nvh.core.cost_tracker import format_cost_report, get_cost_report
        except ImportError:
            console.print("[red]Cost tracker not available.[/red]")
            return
        report = await get_cost_report(period)
        console.print(format_cost_report(report))

    _run(_run())


# ---------------------------------------------------------------------------
# nvh voice — speak your question, hear the answer
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Media")
def voice(
    duration: int = typer.Option(10, "-d", "--duration", help="Recording duration in seconds"),
    stt: str = typer.Option("groq", "--stt", help="Speech-to-text provider: groq, local"),
    tts: str = typer.Option("edge", "--tts", help="Text-to-speech provider: edge, system"),
    advisor: str | None = typer.Option(None, "-a", "--advisor", help="Advisor to use"),
    model: str | None = typer.Option(None, "-m", "--model", help="Model to use"),
    no_speak: bool = typer.Option(False, "--no-speak", help="Skip TTS — show text only"),
):
    """Voice mode — speak your question, hear the answer.

    Records audio from your microphone, transcribes it via Groq Whisper (free),
    sends it to your default advisor, then reads the response aloud.

    Examples:
        nvh voice                      # 10-second recording, Groq STT, Edge TTS
        nvh voice -d 20                # 20-second recording
        nvh voice --no-speak           # transcribe + answer, but skip TTS
        nvh voice -a anthropic         # use a specific advisor
    """
    async def _run_voice():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine
        from nvh.core.voice import play_audio, record_audio, speech_to_text, text_to_speech

        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()

        # Step 1: Record
        console.print(f"[cyan]Recording for {duration}s — speak now...[/cyan]")
        try:
            audio_path = await record_audio(duration=float(duration))
        except RuntimeError as e:
            console.print(f"[red]Recording failed: {e}[/red]")
            raise typer.Exit(1)

        # Step 2: Transcribe
        console.print("[dim]Transcribing...[/dim]")
        try:
            transcript = await speech_to_text(audio_path, provider=stt)
        except Exception as e:
            console.print(f"[red]Transcription failed: {e}[/red]")
            raise typer.Exit(1)

        if not transcript.strip():
            console.print("[yellow]No speech detected.[/yellow]")
            raise typer.Exit(0)

        console.print(f"[bold]You:[/bold] {transcript}")

        # Step 3: Query LLM
        console.print("[dim]Thinking...[/dim]")
        try:
            resp = await engine.query(
                prompt=transcript,
                provider=advisor,
                model=model,
                stream=False,
            )
            answer = resp.content
        except Exception as e:
            console.print(f"[red]Query failed: {e}[/red]")
            raise typer.Exit(1)

        console.print(f"\n[bold green]NVHive:[/bold green] {answer}\n")

        # Step 4: Speak the response
        if not no_speak:
            console.print("[dim]Speaking...[/dim]")
            try:
                audio_out = await text_to_speech(answer, provider=tts)
                if audio_out:
                    await play_audio(audio_out)
                else:
                    console.print(
                        "[yellow]TTS produced no output"
                        " — is edge-tts installed?[/yellow]"
                    )
                    console.print("[dim]Install: pip install edge-tts[/dim]")
            except Exception as e:
                console.print(f"[yellow]TTS failed (answer shown above): {e}[/yellow]")

    _run(_run_voice())


# ---------------------------------------------------------------------------
# nvh imagine — generate an image from a text description
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Media")
def imagine(
    prompt: str = typer.Argument(..., help="Text description of the image to generate"),
    output: str = typer.Option(
        "", "-o", "--output",
        help="Output path for the image (default: auto temp file)",
    ),
    provider: str = typer.Option(
        "auto", "--provider",
        help="Provider: auto, openai, stability, pollinations",
    ),
    size: str = typer.Option("1024x1024", "--size", help="Image dimensions, e.g. 1024x1024"),
    no_open: bool = typer.Option(False, "--no-open", help="Don't open the image after generation"),
):
    """Generate an image from a text description.

    Uses DALL-E 3 (if OpenAI key set), or free Pollinations AI (no key needed).

    Examples:
        nvh imagine "a neon-lit city at night, cyberpunk style"
        nvh imagine "a cat in a space suit" -o cat_space.png
        nvh imagine "abstract mountain landscape" --provider pollinations
        nvh imagine "product mockup on white background" --provider openai
    """
    async def _run_imagine():
        from nvh.core.image_gen import generate_image, open_image

        output_path = output.strip() or None

        console.print(f"[cyan]Generating image:[/cyan] {prompt}")
        console.print(f"[dim]Provider: {provider} | Size: {size}[/dim]\n")

        try:
            result_path = await generate_image(
                prompt=prompt,
                provider=provider,
                output_path=output_path,
                size=size,
            )
        except Exception as e:
            console.print(f"[red]Image generation failed: {e}[/red]")
            raise typer.Exit(1)

        console.print(f"[green]Image saved to:[/green] {result_path}")

        if not no_open:
            console.print("[dim]Opening image...[/dim]")
            open_image(result_path)

    _run(_run_imagine())


# ---------------------------------------------------------------------------
# nvh screenshot — take a screenshot and analyse it
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Media")
def screenshot(
    advisor: str | None = typer.Option(None, "-a", "--advisor", help="Advisor to use for analysis"),
    model: str | None = typer.Option(None, "-m", "--model", help="Model to use"),
    save: str | None = typer.Option(None, "--save", help="Save screenshot to this path"),
    no_analysis: bool = typer.Option(
        False, "--no-analysis",
        help="Just take the screenshot, skip LLM analysis",
    ),
    question: str = typer.Option(
        "Describe this screenshot in detail.",
        "-q", "--question",
        help="Question to ask about the screenshot",
    ),
):
    """Take a screenshot and analyse it with a multimodal LLM.

    Captures the current screen, encodes it, and asks a multimodal advisor to
    describe or answer questions about what is visible.

    Examples:
        nvh screenshot                              # Capture + describe
        nvh screenshot -q "What errors are shown?"  # Ask a specific question
        nvh screenshot --save screen.png            # Save to a specific path
        nvh screenshot --no-analysis --save out.png # Just capture, no LLM
    """
    async def _run_screenshot():
        import base64
        import subprocess
        import sys
        import tempfile

        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        # Capture screenshot
        path = save.strip() if save and save.strip() else tempfile.mktemp(suffix=".png")

        console.print("[cyan]Taking screenshot...[/cyan]")

        captured = False
        if sys.platform == "darwin":
            try:
                subprocess.run(
                    ["screencapture", "-x", path],
                    timeout=5, capture_output=True, check=True,
                )
                captured = True
            except Exception as e:
                console.print(f"[red]screencapture failed: {e}[/red]")
        else:
            import os
            for cmd in [
                ["gnome-screenshot", "-f", path],
                ["scrot", path],
                ["import", "-window", "root", path],
                ["xfce4-screenshooter", "-f", "-s", path],
            ]:
                try:
                    subprocess.run(cmd, timeout=5, capture_output=True)
                    if os.path.exists(path):
                        captured = True
                        break
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    continue

        if not captured:
            console.print(
                "[red]Screenshot failed — no screenshot tool found.[/red]\n"
                "[dim]macOS: screencapture (built-in)\n"
                "Linux: expose scrot, gnome-screenshot, or spectacle in PATH; nvHive will not call apt[/dim]"
            )
            raise typer.Exit(1)

        console.print(f"[dim]Screenshot saved to: {path}[/dim]")

        if no_analysis:
            raise typer.Exit(0)

        # Read and base64-encode
        with open(path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        # Build prompt with embedded image data
        prompt = (
            f"{question}\n\n"
            f"[Screenshot attached as base64 PNG, {len(img_b64)} chars]\n"
            f"data:image/png;base64,{img_b64}"
        )

        console.print("[dim]Sending to multimodal advisor...[/dim]")

        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()

        try:
            resp = await engine.query(
                prompt=prompt,
                provider=advisor,
                model=model,
                stream=False,
                strategy="best",
            )
            console.print()
            console.print(Panel(
                resp.content,
                title="[bold cyan]Screenshot Analysis[/bold cyan]",
                border_style="cyan",
            ))
            console.print(f"\n[dim]Provider: {resp.provider} | Model: {resp.model}[/dim]")
        except Exception as e:
            console.print(f"[red]Analysis failed: {e}[/red]")
            raise typer.Exit(1)

    _run(_run_screenshot())


# ---------------------------------------------------------------------------
# nvh learn — ingest documents into the knowledge base
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# nvh rag — local RAG over your documents ($NVH_HOME/rag/index.sqlite)
# ---------------------------------------------------------------------------

rag_app = typer.Typer(
    help="Index documents locally and retrieve them for grounded answers "
         "(SQLite + Ollama embeddings under $NVH_HOME/rag).",
)
app.add_typer(rag_app, name="rag", rich_help_panel="Subcommands")
_alias("knowledge")
_alias("learn")


def _rag_report(result: dict[str, Any], *, verb: str) -> None:
    if not result.get("ok"):
        console.print(f"[red]{result.get('error', f'{verb} failed')}[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]{verb}:[/green] {result.get('files_ingested', 0)} file(s), "
        f"{result.get('chunks', 0)} chunk(s) → collection [bold]{result.get('collection')}[/bold]"
        + (f", {result['skipped']} skipped" if result.get("skipped") else "")
    )
    if result.get("hint"):
        console.print(f"[yellow]{result['hint']}[/yellow]")
    console.print('[dim]Ask: nvh rag ask "your question" · or nvh ask "..." --knowledge[/dim]')


@rag_app.command("add")
def rag_add(
    paths: list[str] = typer.Argument(..., help="Files (or folders) to index"),
    collection: str | None = typer.Option(
        None, "--collection", "-c", help="Collection name (default: 'default')",
    ),
) -> None:
    """Index one or more files into the local RAG store."""
    from nvh.integrations.rag import ingest_files, ingest_folder

    files: list[Path] = []
    for raw in paths:
        target = Path(raw)
        if not target.exists():
            console.print(f"[red]Path not found: {raw}[/red]")
            raise typer.Exit(1)
        if target.is_dir():
            _rag_report(_run(ingest_folder(target, collection=collection)), verb="Ingested")
        else:
            files.append(target)
    if files:
        _rag_report(_run(ingest_files(files, collection=collection)), verb="Indexed")


@rag_app.command("ingest")
def rag_ingest(
    folder: str = typer.Argument(..., help="Folder to walk (skips .git, node_modules, …)"),
    collection: str | None = typer.Option(
        None, "--collection", "-c", help="Collection name (default: 'default')",
    ),
    max_files: int = typer.Option(2000, "--max-files", help="Safety cap on files per ingest"),
) -> None:
    """Walk a folder and index every supported document in it."""
    from nvh.integrations.rag import ingest_folder

    _rag_report(
        _run(ingest_folder(folder, collection=collection, max_files=max_files)),
        verb="Ingested",
    )


@rag_app.command("ask")
def rag_ask_cmd(
    question: str = typer.Argument(..., help="What to look up"),
    collection: str | None = typer.Option(None, "--collection", "-c", help="Collection to search"),
    top_k: int = typer.Option(5, "-n", "--top-k", help="Chunks to return"),
) -> None:
    """Retrieve the most relevant indexed chunks for a question."""
    from nvh.integrations.rag import ask as rag_ask

    result = _run(rag_ask(question, collection=collection, top_k=top_k))
    if not result.get("ok"):
        console.print(f"[red]{result.get('error', 'retrieval failed')}[/red]")
        raise typer.Exit(1)
    chunks = result.get("chunks", [])
    if not chunks:
        console.print(
            f"[dim]No indexed chunks in collection '{result.get('collection')}'."
            " Add some with `nvh rag add <file>`.[/dim]"
        )
        return
    console.print(f"[bold]Top {len(chunks)} chunk(s) for:[/bold] {question}\n")
    for i, chunk in enumerate(chunks, 1):
        source = Path(chunk.get("source", "?")).name
        console.print(
            f"[bold cyan]{i}.[/bold cyan] [dim]{source} — chunk {chunk.get('chunk_index')}"
            f" · score {chunk.get('score')}[/dim]"
        )
        text = chunk.get("text", "").replace("\n", " ")
        console.print(f"   {text[:300]}{'...' if len(text) > 300 else ''}\n")


# Pre-0.42 `knowledge search`; hidden for one release.
rag_app.command("search", hidden=True)(rag_ask_cmd)


@rag_app.command("list")
def rag_list() -> None:
    """List collections in the local RAG store."""
    from nvh.integrations.rag import list_collections

    collections = list_collections()
    if not collections:
        console.print(
            "[dim]RAG store is empty. Add documents with `nvh rag add <file>`"
            " or `nvh rag ingest <folder>`.[/dim]"
        )
        return
    table = Table(title="RAG collections")
    table.add_column("Collection", style="bold cyan")
    table.add_column("Chunks", justify="right")
    table.add_column("Sources", justify="right")
    for c in collections:
        table.add_row(c["name"], str(c["chunks"]), str(c["sources"]))
    console.print(table)


@rag_app.command("remove")
def rag_remove(
    source: str = typer.Argument(..., help="Source path exactly as shown by `nvh rag ask`"),
    collection: str | None = typer.Option(None, "--collection", "-c"),
) -> None:
    """Drop every chunk of one source from a collection."""
    from nvh.integrations.rag import RagStore
    from nvh.integrations.rag.store import default_collection

    target = Path(source).expanduser()
    resolved = str(target.resolve()) if target.exists() else source
    with RagStore() as store:
        removed = store.delete_source(
            collection=collection or default_collection(), source=resolved,
        )
    if not removed:
        console.print(f"[red]No chunks for source:[/red] {source}")
        raise typer.Exit(1)
    console.print(f"[green]Removed {removed} chunk(s) for[/green] {source}")


@rag_app.command("import-legacy")
def rag_import_legacy(
    memories: bool = typer.Option(
        False, "--memories",
        help="Import the pre-0.42 REPL memories (~/.hive/memory/memories.json) as vault notes instead",
    ),
) -> None:
    """One-shot import of the pre-0.42 ~/.hive/knowledge store (or, with --memories, the REPL memories)."""
    if memories:
        from nvh.cli.repl import import_legacy_memories

        result = import_legacy_memories()
        if result["imported_at"]:
            console.print(
                f"[dim]Already imported on {result['imported_at']};"
                f" delete {result['marker']} to import again.[/dim]"
            )
        elif not result["found"]:
            console.print(f"[dim]No legacy memories at {result['path']}; nothing to import.[/dim]")
        else:
            console.print(f"[green]Imported {result['imported']} legacy memories into the vault[/green]")
        return

    from nvh.integrations.rag import import_legacy_knowledge, legacy_knowledge_status

    status = legacy_knowledge_status()
    if not status["found"]:
        console.print(f"[dim]No legacy knowledge base at {status['path']}; nothing to import.[/dim]")
        return
    if status["imported"]:
        console.print(
            f"[dim]Already imported on {status['imported_at']};"
            " re-importing replaces the same sources.[/dim]"
        )
    console.print(f"Importing {status['documents']} legacy document(s) from {status['path']}...")
    result = _run(import_legacy_knowledge())
    _rag_report(result, verb="Imported")
    console.print(
        f"[dim]{result.get('reingested', 0)} re-read from their original path,"
        f" {result.get('rebuilt', 0)} rebuilt from stored chunks.[/dim]"
    )


# ---------------------------------------------------------------------------
# nvh schedule — recurring AI tasks
# ---------------------------------------------------------------------------

schedule_app = typer.Typer(help="Schedule recurring AI tasks")
app.add_typer(schedule_app, name="schedule", rich_help_panel="Subcommands")


@schedule_app.command("add")
def schedule_add(
    prompt: str = typer.Argument(..., help="Prompt/task to run on schedule"),
    every: str = typer.Option(..., "--every", "-e", help="Interval: 30s, 5m, 1h, 1d"),
    advisor: str = typer.Option("", "-a", "--advisor", help="Advisor to use (default: auto)"),
    mode: str = typer.Option("ask", "--mode", help="Mode: ask, convene, do"),
):
    """Add a recurring scheduled task.

    Examples:

      nvh schedule add "What's the latest AI news?" --every 6h

      nvh schedule add "Check my server CPU" --every 30m --advisor groq

      nvh schedule add "Daily standup summary" --every 1d --mode convene
    """
    from nvh.core.scheduler import Scheduler, parse_interval

    try:
        interval_seconds = parse_interval(every)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    scheduler = Scheduler()
    task = scheduler.add(
        prompt=prompt,
        interval_seconds=interval_seconds,
        advisor=advisor,
        mode=mode,
    )

    interval_label = every
    console.print(f"[green]Scheduled task added:[/green] [bold]{task.id}[/bold]")
    console.print(f"  Prompt:   {task.prompt}")
    console.print(f"  Interval: every {interval_label}")
    if task.advisor:
        console.print(f"  Advisor:  {task.advisor}")
    console.print(f"  Mode:     {task.mode}")
    console.print("\n[dim]Run 'nvh schedule start' to launch the scheduler daemon.[/dim]")


@schedule_app.command("list")
def schedule_list():
    """List all scheduled tasks."""
    import time as _time

    from nvh.core.scheduler import Scheduler

    scheduler = Scheduler()
    tasks = scheduler.list_tasks()

    if not tasks:
        console.print("[dim]No scheduled tasks.[/dim]")
        console.print("[dim]Add one with: nvh schedule add \"your prompt\" --every 1h[/dim]")
        return

    table = Table(title="Scheduled Tasks")
    table.add_column("ID", style="bold cyan")
    table.add_column("Prompt")
    table.add_column("Every", justify="right")
    table.add_column("Advisor", style="dim")
    table.add_column("Mode", style="dim")
    table.add_column("Last Run", style="dim")
    table.add_column("Status")

    now = _time.time()
    for task in tasks:
        # Format interval
        secs = task.interval_seconds
        if secs < 60:
            interval_str = f"{secs}s"
        elif secs < 3600:
            interval_str = f"{secs // 60}m"
        elif secs < 86400:
            interval_str = f"{secs // 3600}h"
        else:
            interval_str = f"{secs // 86400}d"

        last_run = task.last_run[:10] if task.last_run else "never"
        status = "[green]enabled[/green]" if task.enabled else "[dim]disabled[/dim]"
        if task.enabled and task.next_run <= now:
            status = "[yellow]due[/yellow]"

        table.add_row(
            task.id,
            task.prompt[:60] + ("..." if len(task.prompt) > 60 else ""),
            interval_str,
            task.advisor or "auto",
            task.mode,
            last_run,
            status,
        )

    console.print(table)
    console.print(
        f"\n[dim]{len(tasks)} task(s)"
        " | Run 'nvh schedule start' to execute due tasks[/dim]"
    )


@schedule_app.command("remove")
def schedule_remove(
    task_id: str = typer.Argument(..., help="Task ID to remove"),
):
    """Remove a scheduled task."""
    from nvh.core.scheduler import Scheduler

    scheduler = Scheduler()
    removed = scheduler.remove(task_id)
    if removed:
        console.print(f"[green]Removed task:[/green] {task_id}")
    else:
        console.print(f"[red]Task not found:[/red] {task_id}")
        console.print("[dim]Run 'nvh schedule list' to see task IDs.[/dim]")
        raise typer.Exit(1)


@schedule_app.command("start")
def schedule_start(
    interval: int = typer.Option(60, "--interval", "-i", help="Poll interval in seconds"),
    once: bool = typer.Option(False, "--once", help="Run due tasks once then exit"),
):
    """Start the scheduler daemon (runs in foreground, polls for due tasks).

    Press Ctrl+C to stop.

    Examples:

      nvh schedule start            # run forever, check every 60s

      nvh schedule start --once     # run any due tasks right now, then exit
    """

    from nvh.core.notify import notify_task_complete
    from nvh.core.scheduler import Scheduler

    scheduler = Scheduler()

    async def _run_task(task):
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()

        provider_override = task.advisor or None

        try:
            if task.mode == "do":
                from nvh.core.agent_loop import run_agent_loop
                from nvh.core.tools import ToolRegistry
                tools = ToolRegistry()
                result = await run_agent_loop(
                    task=task.prompt,
                    engine=engine,
                    tools=tools,
                    provider=provider_override,
                    auto_approve_safe=True,
                )
                response_text = result.final_response
            else:
                resp = await engine.query(
                    prompt=task.prompt,
                    provider=provider_override,
                )
                response_text = resp.content

            scheduler.mark_completed(task.id)
            await notify_task_complete(task.prompt[:50], response_text)
            return response_text

        except Exception as e:
            scheduler.mark_completed(task.id)
            await notify_task_complete(task.prompt[:50], f"Error: {e}", "")
            return f"Error: {e}"

    async def _daemon():
        console.print(f"[bold cyan]Scheduler started[/bold cyan] (polling every {interval}s)")
        console.print("[dim]Press Ctrl+C to stop.[/dim]\n")

        while True:
            due = scheduler.get_due_tasks()
            if due:
                console.print(f"[dim]{len(due)} due task(s)...[/dim]")
                for task in due:
                    console.print(f"  Running: [bold]{task.id}[/bold] — {task.prompt[:60]}")
                    result_text = await _run_task(task)
                    preview = result_text[:120].replace("\n", " ")
                    console.print(f"  [green]Done:[/green] {preview}\n")

            if once:
                if not due:
                    console.print("[dim]No due tasks.[/dim]")
                break

            await asyncio.sleep(interval)

    try:
        asyncio.run(_daemon())
    except KeyboardInterrupt:
        console.print("\n[dim]Scheduler stopped.[/dim]")


# ---------------------------------------------------------------------------
# nvh git — AI-powered git operations
# ---------------------------------------------------------------------------

git_app = typer.Typer(help="AI-powered git operations (commit messages, reviews, history).")
app.add_typer(git_app, name="git", rich_help_panel="Subcommands")


def _git_run(cmd: str) -> tuple[str, int]:
    """Run a git command and return (stdout+stderr, returncode)."""
    import subprocess
    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    return output.strip(), result.returncode


async def _git_query(prompt: str, system: str | None = None) -> str:
    """Send a prompt to the configured advisor and return the text response."""
    from nvh.config.settings import load_config
    from nvh.core.engine import Engine

    config = load_config()
    engine = Engine(config=config)
    await engine.initialize()
    resp = await engine.query(prompt=prompt, system_prompt=system, stream=False)
    return resp.content


@git_app.command("commit")
def git_commit(
    push: bool = typer.Option(False, "--push", "-p", help="Git push after committing"),
    no_confirm: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    advisor: str | None = typer.Option(None, "-a", "--advisor", help="Advisor to use"),
):
    """Generate an AI commit message from staged changes and optionally commit.

    Reads ``git diff --cached``, sends the diff to your configured advisor, and
    proposes a Conventional Commits-style message.  You can edit or confirm
    before the commit is made.
    """
    diff, rc = _git_run("git diff --cached")
    if rc != 0:
        console.print(f"[red]git error:[/red] {diff}")
        raise typer.Exit(1)
    if not diff:
        console.print("[yellow]No staged changes found. Stage files with `git add` first.[/yellow]")
        raise typer.Exit()

    console.print("[dim]Generating commit message…[/dim]")

    async def _run():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()

        system = (
            "You are an expert software engineer who writes excellent git commit messages. "
            "Follow the Conventional Commits specification: "
            "<type>(<scope>): <short summary>\\n\\n<optional body>\\n\\n<optional footer>. "
            "Types: feat, fix, docs, style, refactor, test, chore, perf, ci. "
            "Keep the subject line under 72 characters. Return ONLY the commit message — "
            "no markdown fences, no extra commentary."
        )
        prompt = f"Write a commit message for these staged changes:\n\n```diff\n{diff}\n```"
        resp = await engine.query(
            prompt=prompt,
            provider=advisor,
            system_prompt=system,
            stream=False,
        )
        return resp.content.strip()

    message = asyncio.run(_run())

    console.print("\n[bold]Proposed commit message:[/bold]\n")
    console.print(Panel(message, border_style="cyan"))

    if not no_confirm:
        action = typer.prompt(
            "\n[c]ommit / [e]dit / [a]bort",
            default="c",
            show_default=True,
        ).lower().strip()
    else:
        action = "c"

    if action.startswith("e"):
        edited = typer.edit(message)
        if edited:
            message = edited.strip()
        console.print("[dim]Using edited message.[/dim]")
        action = "c"

    if action.startswith("c"):
        # Escape double-quotes for the shell
        safe_msg = message.replace('"', '\\"')
        out, rc2 = _git_run(f'git commit -m "{safe_msg}"')
        if rc2 != 0:
            console.print(f"[red]Commit failed:[/red] {out}")
            raise typer.Exit(1)
        console.print(f"[green]Committed.[/green]\n{out}")
        if push:
            push_out, push_rc = _git_run("git push")
            if push_rc != 0:
                console.print(f"[yellow]Push failed:[/yellow] {push_out}")
            else:
                console.print("[green]Pushed.[/green]")
    else:
        console.print("[dim]Aborted.[/dim]")


@git_app.command("review")
def git_review(
    staged: bool = typer.Option(False, "--staged", help="Review only staged changes"),
    advisor: str | None = typer.Option(None, "-a", "--advisor", help="Advisor to use"),
    output: str = typer.Option("text", "-o", "--output", help="Output format: text, markdown"),
):
    """AI review of uncommitted changes.

    Sends your working-tree diff to the advisor and gets a structured code
    review covering correctness, style, potential bugs, and improvements.
    """
    diff_cmd = "git diff --cached" if staged else "git diff"
    diff, rc = _git_run(diff_cmd)
    if rc != 0:
        console.print(f"[red]git error:[/red] {diff}")
        raise typer.Exit(1)
    if not diff:
        label = "staged" if staged else "uncommitted"
        console.print(f"[yellow]No {label} changes found.[/yellow]")
        raise typer.Exit()

    console.print("[dim]Reviewing changes…[/dim]")

    async def _run():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()

        system = (
            "You are a senior software engineer conducting a thorough code review. "
            "Structure your review with these sections:\n"
            "## Summary\nBrief overview of the changes.\n\n"
            "## Issues\nBugs, logic errors, or security concerns (if any).\n\n"
            "## Suggestions\nStyle, readability, and improvement suggestions.\n\n"
            "## Verdict\nOverall assessment: ✅ Ready / ⚠️ Needs work / ❌ Blocked.\n\n"
            "Be constructive and specific. Reference line numbers when possible."
        )
        prompt = f"Please review these code changes:\n\n```diff\n{diff}\n```"
        resp = await engine.query(
            prompt=prompt,
            provider=advisor,
            system_prompt=system,
            stream=False,
        )
        return resp.content

    review = asyncio.run(_run())

    if output == "markdown":
        console.print(Markdown(review))
    else:
        console.print(Panel(review, title="[bold]Code Review[/bold]", border_style="blue"))


@git_app.command("explain")
def git_explain(
    n: int = typer.Option(5, "-n", help="Number of recent commits to explain"),
    advisor: str | None = typer.Option(None, "-a", "--advisor", help="Advisor to use"),
    output: str = typer.Option("text", "-o", "--output", help="Output format: text, markdown"),
):
    """Explain recent git history in plain English.

    Fetches the last N commit messages and their combined diff, then asks the
    advisor to summarise what changed and why.
    """
    log_out, rc1 = _git_run(f"git log --oneline -{n}")
    if rc1 != 0:
        console.print(f"[red]git log failed:[/red] {log_out}")
        raise typer.Exit(1)
    if not log_out:
        console.print("[yellow]No commits found in this repository.[/yellow]")
        raise typer.Exit()

    diff_out, rc2 = _git_run(f"git diff HEAD~{n}..HEAD")
    # Truncate very large diffs so we stay within token limits
    max_diff_chars = 12_000
    if len(diff_out) > max_diff_chars:
        diff_out = (
            diff_out[:max_diff_chars]
            + f"\n\n[... diff truncated at {max_diff_chars} chars ...]"
        )

    console.print(f"[dim]Explaining last {n} commit(s)…[/dim]")

    async def _run():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()

        system = (
            "You are a helpful engineering assistant. Given a git log and diff, "
            "explain in clear plain English what changed, why it matters, and any "
            "notable patterns or concerns. Be concise but thorough."
        )
        prompt = (
            f"Explain what these {n} recent git commit(s) do:\n\n"
            f"## Commit log\n```\n{log_out}\n```\n\n"
            f"## Combined diff\n```diff\n{diff_out}\n```"
        )
        resp = await engine.query(
            prompt=prompt,
            provider=advisor,
            system_prompt=system,
            stream=False,
        )
        return resp.content

    explanation = asyncio.run(_run())

    console.print(f"\n[bold]Last {n} commit(s):[/bold]\n[dim]{log_out}[/dim]\n")
    if output == "markdown":
        console.print(Markdown(explanation))
    else:
        console.print(Panel(
            explanation,
            title="[bold]Git History Explained[/bold]",
            border_style="green",
        ))


# ---------------------------------------------------------------------------
# nvh scan — AI analysis of a codebase
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Core")
def scan(
    path: str = typer.Argument(".", help="Directory to scan (default: current directory)"),
    focus: str = typer.Option(
        "overview",
        "--focus", "-f",
        help="Analysis focus: overview, security, quality, dependencies",
    ),
    advisor: str | None = typer.Option(None, "-a", "--advisor", help="Advisor to use"),
    output: str = typer.Option("text", "-o", "--output", help="Output format: text, markdown"),
    max_files: int = typer.Option(200, "--max-files", help="Maximum files to index"),
):
    """Scan a codebase and produce an AI-powered analysis report.

    Walks the target directory (honouring .gitignore), counts lines by language,
    reads key project files (README, package.json, pyproject.toml, Dockerfile, …),
    and sends a structured summary to the advisor for analysis.

    Focus modes:
      overview      General architecture, tech stack, and suggestions
      security      Common vulnerabilities, secrets exposure, dependency CVEs
      quality       Code smells, test coverage indicators, tech debt
      dependencies  Dependency health, outdated packages, licence issues
    """
    import fnmatch
    import os
    from pathlib import Path as FsPath

    target = FsPath(path).resolve()
    if not target.is_dir():
        console.print(f"[red]Not a directory:[/red] {path}")
        raise typer.Exit(1)

    # ---- Load .gitignore patterns -----------------------------------------------
    gitignore_patterns: list[str] = [
        ".git", ".git/*", "__pycache__", "*.pyc", "*.pyo",
        "node_modules", "node_modules/*", ".venv", "venv", ".env",
        "*.egg-info", "dist", "build", ".tox", ".mypy_cache",
        ".pytest_cache", ".DS_Store", "*.lock",
    ]
    gi_path = target / ".gitignore"
    if gi_path.exists():
        try:
            for line in gi_path.read_text(errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    gitignore_patterns.append(line)
        except Exception:
            pass

    def _is_ignored(rel: str) -> bool:
        parts = rel.replace("\\", "/")
        for pat in gitignore_patterns:
            if fnmatch.fnmatch(parts, pat):
                return True
            if fnmatch.fnmatch(FsPath(parts).name, pat):
                return True
        return False

    # ---- Language detection by extension ----------------------------------------
    ext_lang: dict[str, str] = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
        ".tsx": "TypeScript/React", ".jsx": "JavaScript/React",
        ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
        ".cs": "C#", ".cpp": "C++", ".c": "C", ".h": "C/C++ Header",
        ".rb": "Ruby", ".php": "PHP", ".swift": "Swift", ".dart": "Dart",
        ".scala": "Scala", ".ex": "Elixir", ".exs": "Elixir",
        ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
        ".sql": "SQL", ".html": "HTML", ".css": "CSS", ".scss": "SCSS",
        ".json": "JSON", ".yaml": "YAML", ".yml": "YAML",
        ".toml": "TOML", ".md": "Markdown", ".mdx": "MDX",
        ".dockerfile": "Dockerfile", ".tf": "Terraform", ".hcl": "HCL",
    }

    # ---- Walk directory ----------------------------------------------------------
    console.print(f"[dim]Scanning {target} …[/dim]")

    lang_lines: dict[str, int] = {}
    all_files: list[str] = []
    binary_count = 0

    for root, dirs, files in os.walk(target):
        rel_root = os.path.relpath(root, target)
        # Prune ignored dirs in-place
        dirs[:] = [
            d for d in dirs
            if not _is_ignored(os.path.join(rel_root, d).lstrip("./"))
        ]

        for fname in files:
            rel = os.path.join(rel_root, fname).lstrip("./")
            if _is_ignored(rel):
                continue
            all_files.append(rel)

            ext = FsPath(fname).suffix.lower()
            lang = ext_lang.get(ext)
            if lang:
                try:
                    fpath = os.path.join(root, fname)
                    with open(fpath, encoding="utf-8", errors="ignore") as fh:
                        lcount = sum(1 for _ in fh)
                    lang_lines[lang] = lang_lines.get(lang, 0) + lcount
                except Exception:
                    binary_count += 1
            else:
                binary_count += 1

    total_files = len(all_files)
    total_lines = sum(lang_lines.values())

    # Top languages by line count
    top_langs = sorted(lang_lines.items(), key=lambda kv: kv[1], reverse=True)[:10]

    # ---- Read key project files --------------------------------------------------
    key_files = [
        "README.md", "README.rst", "README.txt", "README",
        "package.json", "pyproject.toml", "setup.py", "setup.cfg",
        "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
        "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
        ".env.example", ".env.sample",
        "requirements.txt", "requirements-dev.txt",
        "Makefile", "justfile", ".github/workflows",
    ]
    key_file_contents: list[str] = []
    for kf in key_files:
        kf_path = target / kf
        if kf_path.is_file():
            try:
                text = kf_path.read_text(errors="replace")
                # Truncate large files
                if len(text) > 3000:
                    text = text[:3000] + "\n[... truncated ...]"
                key_file_contents.append(f"### {kf}\n```\n{text}\n```")
            except Exception:
                pass
        elif kf_path.is_dir():
            # e.g. .github/workflows — list files
            try:
                wf_files = list(kf_path.iterdir())[:5]
                names = ", ".join(f.name for f in wf_files)
                key_file_contents.append(f"### {kf}/\n{names}")
            except Exception:
                pass

    # ---- Focus-specific extra context -------------------------------------------
    focus_context = ""

    if focus == "security":
        # Look for common sensitive patterns
        sensitive_hits: list[str] = []
        sensitive_patterns = [
            ("hardcoded secret", [
                "password =", "secret =", "api_key =",
                "token =", "private_key =",
            ]),
            ("SQL injection risk", ["f\"SELECT", "f'SELECT", '+ "SELECT', "+ 'SELECT'"]),
            ("shell injection risk", ["shell=True", "os.system(", "subprocess.call("]),
            ("insecure hash", ["md5(", "sha1(", "hashlib.md5", "hashlib.sha1"]),
            ("debug left in code", ["print(", "console.log(", "debugger;"]),
        ]
        for rel_file in all_files[:max_files]:
            fpath = target / rel_file
            try:
                text = fpath.read_text(errors="replace")
                for concern, patterns in sensitive_patterns:
                    for pat in patterns:
                        if pat.lower() in text.lower():
                            sensitive_hits.append(f"  [{concern}] {rel_file}")
                            break
            except Exception:
                pass
        if sensitive_hits:
            focus_context = (
                "\n## Potential security findings (static scan)\n"
                + "\n".join(sensitive_hits[:40])
            )
        else:
            focus_context = "\n## Static scan: no obvious sensitive patterns found."

    elif focus == "quality":
        # Run local static code analysis via code_analysis module
        try:
            from nvh.core.code_analysis import analyze_directory, format_analysis_report
            analysis_report = analyze_directory(target)
            focus_context = (
                "\n## Local Static Analysis\n```\n"
                + format_analysis_report(analysis_report)
                + "\n```"
            )
        except Exception as e:
            focus_context = f"\n## Local static analysis unavailable: {e}"

    elif focus == "dependencies":
        dep_files_content: list[str] = []
        for df in [
            "requirements.txt", "package.json",
            "Cargo.toml", "go.mod",
            "pyproject.toml", "pom.xml",
        ]:
            df_path = target / df
            if df_path.exists():
                try:
                    text = df_path.read_text(errors="replace")
                    if len(text) > 4000:
                        text = text[:4000] + "\n[... truncated ...]"
                    dep_files_content.append(f"### {df}\n```\n{text}\n```")
                except Exception:
                    pass
        focus_context = "\n## Dependency files\n" + "\n".join(dep_files_content)

    # ---- Build the summary for the advisor --------------------------------------
    lang_table = "\n".join(f"  {lang}: {lines:,} lines" for lang, lines in top_langs)
    file_sample = "\n".join(all_files[:60])
    if total_files > 60:
        file_sample += f"\n  … and {total_files - 60} more files"

    focus_instructions: dict[str, str] = {
        "overview": (
            "Provide a comprehensive overview covering: tech stack, architecture, "
            "key components, code organisation, strengths, and the top 3 actionable improvements."
        ),
        "security": (
            "Conduct a security audit. Identify vulnerabilities, insecure patterns, "
            "secrets exposure risks, missing security headers or protections, and "
            "dependency risks. Rate severity (critical/high/medium/low) for each finding."
        ),
        "quality": (
            "Assess code quality: test coverage indicators, code smells, duplication, "
            "complexity hotspots, documentation quality, and tech debt. "
            "Prioritise the top 5 quality improvements."
        ),
        "dependencies": (
            "Analyse the dependencies: identify outdated packages, known CVEs if recognisable, "
            "licence compatibility issues, unnecessary or bloated deps, and recommend updates."
        ),
    }
    instruction = focus_instructions.get(focus, focus_instructions["overview"])

    system = (
        "You are an expert software architect and code analyst. "
        "When given a codebase summary, produce a well-structured, actionable report. "
        "Use markdown headings. Be specific — name files and patterns you identify. "
        "Do not hallucinate files that are not listed."
    )

    prompt = (
        f"# Codebase Analysis Request\n\n"
        f"**Directory:** `{target}`\n"
        f"**Focus:** {focus}\n\n"
        f"## File statistics\n"
        f"- Total files: {total_files:,} ({binary_count} binary/unknown)\n"
        f"- Total lines of code: {total_lines:,}\n\n"
        f"## Top languages\n{lang_table}\n\n"
        f"## File tree (first 60 files)\n```\n{file_sample}\n```\n\n"
        f"## Key project files\n" + "\n".join(key_file_contents) +
        focus_context +
        f"\n\n## Your task\n{instruction}"
    )

    console.print(
        f"[dim]Analysing with focus=[bold]{focus}[/bold]"
        f" ({total_files} files, {total_lines:,} lines)…[/dim]"
    )

    async def _run():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()
        resp = await engine.query(
            prompt=prompt,
            provider=advisor,
            system_prompt=system,
            stream=False,
        )
        return resp.content, resp

    report, resp = asyncio.run(_run())

    title = f"[bold]Codebase Scan — {focus.title()}[/bold]  [dim]{target.name}/[/dim]"
    if output == "markdown":
        console.print(Markdown(report))
    else:
        console.print(Panel(Markdown(report), title=title, border_style="magenta"))

    console.print(
        f"\n[dim]Files: {total_files:,} | Lines: {total_lines:,} | "
        f"Provider: {resp.provider} | Model: {resp.model} | "
        f"Cost: ${resp.cost_usd:.4f}[/dim]"
    )


# ---------------------------------------------------------------------------
# Tour — interactive first-run experience
# ---------------------------------------------------------------------------


@app.command(rich_help_panel="Admin")
def tour(
    skip: list[int] = typer.Option(
        [], "--skip", "-s", help="Steps to skip (1, 2, or 3)"
    ),
):
    """Interactive first-run tour — proves multi-LLM value in 90 seconds."""
    import time

    from rich.rule import Rule
    from rich.text import Text

    async def _run_tour():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine
        from nvh.core.router import classify_task

        config = load_config()
        engine = Engine(config=config)
        enabled_providers = await engine.initialize()

        # --- Welcome banner ---
        console.print()
        console.print(
            Panel(
                "[bold white]Welcome to nvHive[/bold white]\n\n"
                "This 90-second tour shows what multi-LLM orchestration "
                "can do.\n"
                "Three live demos, real API calls, real results.",
                border_style="bright_magenta",
                padding=(1, 3),
            )
        )
        console.print()

        # =============================================================
        # Step 1: Routing in action
        # =============================================================
        if 1 not in skip:
            console.print(Rule(
                "[bold cyan]Step 1 of 3[/bold cyan]  "
                "Watch routing in action",
                style="cyan",
            ))
            console.print()

            demo_queries = [
                (
                    "Write a Python function to merge two sorted lists",
                    "coding",
                ),
                (
                    "Write a haiku about the ocean at midnight",
                    "creative",
                ),
            ]

            for query, label in demo_queries:
                console.print(
                    f"  [bold]Query:[/bold] [italic]{query}[/italic]"
                )
                console.print()

                with console.status(
                    f"[dim]Routing {label} question...[/dim]",
                    spinner="dots",
                ):
                    classification = classify_task(query)
                    decision = engine.router.route(query)
                    start = time.monotonic()
                    try:
                        resp = await engine.query(
                            prompt=query, stream=False,
                        )
                        elapsed = int(
                            (time.monotonic() - start) * 1000
                        )
                    except Exception as exc:
                        console.print(
                            f"    [red]Error: {exc}[/red]\n"
                        )
                        continue

                info_table = Table(
                    show_header=False, box=None, padding=(0, 2),
                )
                info_table.add_column("key", style="bold")
                info_table.add_column("value")
                info_table.add_row(
                    "Provider",
                    f"[green]{resp.provider}[/green]",
                )
                info_table.add_row("Model", resp.model)
                info_table.add_row(
                    "Latency", f"{resp.latency_ms or elapsed}ms",
                )
                info_table.add_row("Cost", f"${resp.cost_usd:.4f}")
                info_table.add_row(
                    "Task type", classification.task_type.value,
                )
                info_table.add_row("Why chosen", decision.reason)

                preview = resp.content.strip().replace("\n", " ")
                if len(preview) > 120:
                    preview = preview[:120] + "..."

                console.print(Panel(
                    Text(preview, style="dim"),
                    title=f"[bold]{label.title()} response[/bold]",
                    border_style="blue",
                    padding=(0, 1),
                ))
                console.print(info_table)
                console.print()

            console.print(
                "[green]  --> The router picked different providers"
                " based on the task.[/green]\n"
            )

        # =============================================================
        # Step 2: Council in action
        # =============================================================
        if 2 not in skip:
            console.print(Rule(
                "[bold yellow]Step 2 of 3[/bold yellow]  "
                "See the council in action",
                style="yellow",
            ))
            console.print()

            council_prompt = (
                "Is Python or Rust better for CLI tools?"
            )
            console.print(
                "  [bold]Council query:[/bold] "
                f"[italic]{council_prompt}[/italic]\n"
            )

            with console.status(
                "[dim]Querying council members in parallel...[/dim]",
                spinner="dots",
            ):
                try:
                    result = await engine.run_council(
                        prompt=council_prompt,
                        synthesize=True,
                    )
                except Exception as exc:
                    console.print(
                        f"  [red]Council error: {exc}[/red]\n"
                    )
                    result = None

            if result:
                for member_label, mresp in (
                    result.member_responses.items()
                ):
                    snippet = (
                        mresp.content.strip()
                        .replace("\n", " ")[:100]
                    )
                    if len(mresp.content.strip()) > 100:
                        snippet += "..."
                    console.print(Panel(
                        snippet,
                        title=(
                            f"[bold]{member_label}[/bold]"
                            f" ({mresp.model})"
                        ),
                        border_style="blue",
                        padding=(0, 1),
                    ))

                if result.synthesis:
                    synth_preview = (
                        result.synthesis.content.strip()
                    )
                    if len(synth_preview) > 300:
                        synth_preview = synth_preview[:300] + "..."
                    console.print(Panel(
                        synth_preview,
                        title="[bold green]Synthesis[/bold green]",
                        border_style="green",
                        padding=(0, 1),
                    ))

                member_count = len(result.member_responses)
                total_cost = result.total_cost_usd
                console.print(
                    f"  [dim]Members: {member_count} | "
                    f"Strategy: {result.strategy} | "
                    f"Latency: {result.total_latency_ms}ms | "
                    f"Cost: ${total_cost:.4f}[/dim]"
                )
                console.print()
                console.print(
                    "[green]  --> Multiple LLMs debated, then a "
                    "synthesis merged the best ideas.[/green]\n"
                )

        # =============================================================
        # Step 3: Make it yours
        # =============================================================
        if 3 not in skip:
            console.print(Rule(
                "[bold magenta]Step 3 of 3[/bold magenta]  "
                "Make it yours",
                style="magenta",
            ))
            console.print()

            if enabled_providers:
                provider_list = ", ".join(
                    f"[green]{p}[/green]"
                    for p in enabled_providers
                )
                console.print(
                    f"  [bold]Providers configured:[/bold] "
                    f"{len(enabled_providers)} ({provider_list})"
                )
            else:
                console.print(
                    "  [bold]Providers configured:[/bold] "
                    "[yellow]0[/yellow] — using auto-detected "
                    "defaults"
                )

            console.print()

            suggestions = Table(
                show_header=False, box=None, padding=(0, 2),
            )
            suggestions.add_column("icon", width=3)
            suggestions.add_column("text")
            suggestions.add_row(
                "[bold yellow]+[/bold yellow]",
                "Add a free API key:  [cyan]nvh keys[/cyan]",
            )
            suggestions.add_row(
                "[bold yellow]+[/bold yellow]",
                "Interactive mode:    [cyan]nvh repl[/cyan]",
            )
            suggestions.add_row(
                "[bold yellow]+[/bold yellow]",
                "Web dashboard:       [cyan]nvh webui[/cyan]",
            )
            suggestions.add_row(
                "[bold yellow]+[/bold yellow]",
                "System status:       [cyan]nvh status[/cyan]",
            )
            suggestions.add_row(
                "[bold yellow]+[/bold yellow]",
                "Ask anything:        "
                "[cyan]nvh \"your question\"[/cyan]",
            )

            console.print(Panel(
                suggestions,
                title="[bold]Next steps[/bold]",
                border_style="bright_magenta",
                padding=(1, 2),
            ))
            console.print()

        # --- Farewell ---
        console.print(
            Panel(
                "[bold]Tour complete.[/bold]  You have a multi-LLM "
                "command center at your fingertips.\n"
                "Run [cyan]nvh repl[/cyan] for interactive mode or "
                "[cyan]nvh webui[/cyan] for the dashboard.",
                border_style="bright_green",
                padding=(1, 3),
            )
        )

    _run(_run_tour())


# ---------------------------------------------------------------------------
# Entry point — catches unknown commands and treats them as prompts
# ---------------------------------------------------------------------------

_FIRST_RUN_ENV_KEYS = (
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
)


def _is_first_run() -> bool:
    """Return True when no config file exists and no provider API keys are set.

    Skips on CI and in test environments to avoid triggering setup()
    on fresh CI runners where no config exists by design.
    """
    # Never trigger in CI or test environments
    if os.environ.get("CI") or os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("GITHUB_ACTIONS"):
        return False
    if DEFAULT_CONFIG_PATH.exists():
        return False
    return not any(os.environ.get(k) for k in _FIRST_RUN_ENV_KEYS)


def _known_commands() -> set[str]:
    """Every name Click will dispatch — groups and hidden aliases included."""
    return set(typer.main.get_command(app).commands)


def _suggest_commands(args: list[str]) -> list[str]:
    """Spellings close to a mistyped first word, deprecated aliases resolved
    to their replacement (`nvh docter` -> `nvh status --deep`).

    Fires for a lone word (`nvh statsu`), a word followed by a flag or one
    positional (`nvh docter --fix`, `nvh quik hi`) or by a subcommand of the
    suggested group (`nvh confg set …`). Anything longer is a prompt, not a typo.
    """
    import difflib

    root = typer.main.get_command(app)
    matches = difflib.get_close_matches(args[0].lower(), sorted(root.commands), n=3, cutoff=0.75)
    if len(args) > 2 and not args[1].startswith("-"):
        matches = [m for m in matches if args[1] in getattr(root.commands[m], "commands", {})][:1]
    return list(dict.fromkeys(DEPRECATED_ALIASES.get(m, m) for m in matches))


def _forward(fn, *args, **kwargs) -> None:
    """Call a command body outside Typer, turning typer.Exit into a process exit."""
    try:
        fn(*args, **kwargs)
    except typer.Exit as exc:
        sys.exit(exc.exit_code)


def main():
    """Entry point — routes between subcommands and bare prompts.

    nvh                     → REPL
    nvh version             → subcommand
    nvh statsu              → did-you-mean, exit 2
    nvh "what is AI?"       → bare prompt → LLM
    nvh "install comfyui"   → task-shaped → asks for an explicit `nvh do`
    """
    # Shell completion requests invoke `nvh` (or the `nvhive` alias, whose
    # Click env var is _NVHIVE_COMPLETE) with no argv, which would otherwise
    # fall through to the REPL/guided setup below. Click handles the env var
    # inside app(), so hand off before any dispatching — but only for values
    # shaped like Click instructions ({shell}_source / {shell}_complete), so
    # a stray exported variable can't hijack normal invocations.
    for _complete_var in ("_NVH_COMPLETE", "_NVHIVE_COMPLETE"):
        if os.environ.get(_complete_var, "").endswith(("_source", "_complete")):
            app()
            return

    args = sys.argv[1:]

    # Load API keys from keyring / ~/.hive/.env before config interpolation
    try:
        from nvh.cli.setup import load_env_keys
        load_env_keys()
    except Exception:
        pass

    # Suppress noisy LiteLLM info/debug messages (e.g. "Give Feedback" banners)
    try:
        import litellm
        litellm.suppress_debug_info = True
        import logging
        logging.getLogger("LiteLLM").setLevel(logging.WARNING)
        logging.getLogger("LiteLLM Router").setLevel(logging.WARNING)
        logging.getLogger("LiteLLM Proxy").setLevel(logging.WARNING)
    except Exception:
        pass

    # --skip-setup: suppress first-run guided setup
    if "--skip-setup" in args:
        args = [a for a in args if a != "--skip-setup"]
        sys.argv = [sys.argv[0]] + args
    else:
        # First-run detection: if no config and no API keys, run guided setup.
        # Skip when the user passed flags (--help, --version) or explicit subcommands.
        if not args or (args and not args[0].startswith("-")):
            if _is_first_run():
                from nvh.cli.setup import guided_setup
                guided_setup()
                # If no args were given the setup is all we needed; exit.
                if not args:
                    return

    if not args:
        # Piped stdin with no arguments is the Unix-pipeline case
        if not sys.stdin.isatty():
            _forward(_ask, output="raw", quiet=True)
            return
        _run(_launch_default_repl())
        return

    # Flags like --help, --version should go to Typer directly
    # (but --iterative is ours — strip it and continue to bare-prompt routing)
    if args[0].startswith("-") and args[0] != "--iterative":
        app()
        return

    if args[0].lower() in _known_commands():
        app()
        return

    suggestions = _suggest_commands(args)
    if suggestions:
        # soft_wrap: a replacement spelling must stay on one copyable line
        err_console.print(
            f"[red]nvh: '{args[0]}' is not a command.[/red] Did you mean "
            + " / ".join(f"[bold]nvh {s}[/bold]" for s in suggestions) + "?",
            soft_wrap=True,
        )
        err_console.print(f"[dim]To send it to an advisor anyway: nvh ask \"{' '.join(args)}\"[/dim]")
        sys.exit(2)

    # Piped stdin + words: the words are the prompt, stdin is the payload
    if not sys.stdin.isatty():
        _forward(_ask, " ".join(args), output="raw", quiet=True)
        return

    force_iterative = "--iterative" in args
    prompt = " ".join(a for a in args if a != "--iterative")
    if not prompt:
        _run(_launch_default_repl())
        return

    # A task-shaped bare prompt used to start `nvh do` with --auto — a
    # metered, auto-approved agent run one typo away. Ask for it explicitly.
    from nvh.cli.repl import _is_task_input
    if _is_task_input(prompt) and not force_iterative:
        err_console.print(
            "[yellow]That reads like a task, not a question — bare prompts"
            " no longer start an agent run.[/yellow]"
        )
        err_console.print(f"  Run it:  [bold]nvh do \"{prompt}\"[/bold]")
        err_console.print(f"  Ask it:  [bold]nvh ask \"{prompt}\"[/bold]")
        sys.exit(2)

    _run(_smart_default(prompt, force_iterative=force_iterative))


# ---------------------------------------------------------------------------
# nvh services — single-screen view of the local service pipeline, plus
# surgical start/restart for troubleshooting. The user-facing first-run /
# everyday command remains ``nvh webui``; ``nvh services`` is for the case
# where the pipeline drifted out of sync and the user wants a clear
# table + a deterministic recovery path.
# ---------------------------------------------------------------------------

services_app = typer.Typer(
    help="Inspect / start / restart the local service pipeline (Ollama → API → WebUI).",
    invoke_without_command=True,
)
app.add_typer(services_app, name="services", rich_help_panel="Infrastructure")

# ---------------------------------------------------------------------------
# nvh models — local model manager (roadmap critical: in-app model browser)
# ---------------------------------------------------------------------------

models_app = typer.Typer(
    help="Browse, install, and remove local Ollama models with VRAM-fit guidance.",
)
app.add_typer(models_app, name="models", rich_help_panel="Infrastructure")


@models_app.command("list")
def models_list(
    all_catalog: bool = typer.Option(
        False, "--all", help="Show the full catalog, not just installed models",
    ),
) -> None:
    """Show installed models (and, with --all, the fit-ranked catalog).

    Reads the same VRAM-fit report the WebUI Model Manager uses, so the
    CLI and dashboard agree on what fits the detected GPU.
    """
    from nvh.integrations.diagnostics.model_fit import model_fit_report

    report = model_fit_report()
    vram = report.get("detected_vram_gb") or report.get("vram_gb")
    models = report.get("models", report.get("ranked", []))
    installed = [m for m in models if m.get("installed")]

    def _label(m: dict[str, Any]) -> str:
        target = m.get("install_target") or m.get("id") or "?"
        title = m.get("title")
        return f"{title} ({target})" if title and title != target else str(target)

    if installed:
        console.print(f"[bold]Installed models[/bold] (detected VRAM: {vram or '?'} GB)")
        for m in installed:
            disk = m.get("estimated_disk_gb") or m.get("size_gb") or "?"
            console.print(f"  [green]✓[/green] {_label(m)} (~{disk} GB)")
    else:
        console.print("[dim]No local models installed yet.[/dim]")

    if all_catalog:
        console.print("\n[bold]Catalog[/bold] (best fit first):")
        for m in models:
            if m.get("installed"):
                continue
            fits = m.get("fits_vram")
            mark = "[green]fits[/green]" if fits else "[yellow]tight[/yellow]"
            disk = m.get("estimated_disk_gb") or "?"
            console.print(
                f"  {_label(m)} — {m.get('use_case_label', m.get('category', ''))}"
                f" · ~{disk} GB · {mark}"
            )
    else:
        console.print("\n[dim]Run `nvh models list --all` for the full catalog, "
                      "or `nvh models pull <name>` to install one.[/dim]")


def _recommended_pull_targets() -> list[str]:
    """VRAM tier → catalog models recommended for this GPU that are not installed yet."""
    from nvh.integrations.installs.studio_packs import (
        STUDIO_MODELS,
        _detect_vram_gb,
        _ollama_models,
        _recommended_model_ids,
    )

    vram = _detect_vram_gb()
    wanted = _recommended_model_ids(vram)
    installed = _ollama_models()
    picks = [m for m in sorted(STUDIO_MODELS, key=lambda m: m.priority) if m.id in wanted]
    tier = f"Detected {vram} GB VRAM" if vram else "No GPU detected (CPU tier)"
    console.print(f"[bold]{tier}[/bold] — {len(picks)} recommended model(s):")
    targets: list[str] = []
    for m in picks:
        have = m.install_target in installed or m.install_target.split(":")[0] in installed
        mark = "[green]installed[/green]" if have else f"~{m.estimated_disk_gb} GB"
        console.print(f"  {m.title} ({m.install_target}) · {mark}")
        if not have:
            targets.append(m.install_target)
    console.print()
    return targets


@models_app.command("pull")
def models_pull(
    name: str | None = typer.Argument(None, help="Model name, e.g. llama3.2-vision"),
    recommended: bool = typer.Option(
        False, "--recommended",
        help="Pull the catalog models recommended for the detected GPU VRAM (skips installed)",
    ),
) -> None:
    """Download a model into the local Ollama store with live progress.

    Runs `ollama pull` against the rootless Ollama binary and streams its
    progress. The Wizard picks the new model up on its next turn; the
    WebUI Model Manager drives the same pull over SSE. --recommended pulls
    the VRAM-tier model set (what scripts/ollama-setup.sh used to do).
    """
    import subprocess

    from nvh.integrations.installs.studio_packs import _ollama_binary

    if bool(name) == recommended:
        console.print("[red]Give a model name or --recommended (not both).[/red]")
        raise typer.Exit(1)

    binary = _ollama_binary()
    if not binary:
        console.print(
            "[red]Ollama binary not found.[/red] Install it first: "
            "[bold]nvh workstation --with-local-ai -y[/bold]"
        )
        raise typer.Exit(1)

    targets = [name] if name else _recommended_pull_targets()
    if not targets:
        console.print("[green]✓[/green] Every recommended model is already installed.")
        return

    failed: list[str] = []
    for target in targets:
        console.print(f"[bold]Pulling {target}...[/bold] [dim](Ctrl+C to cancel)[/dim]")
        try:
            proc = subprocess.run([binary, "pull", target], check=False)
        except FileNotFoundError:
            console.print(f"[red]Could not run {binary} pull.[/red]")
            raise typer.Exit(1)
        if proc.returncode == 0:
            console.print(f"[green]✓[/green] {target} ready.")
        else:
            failed.append(target)
            console.print(f"[red]✗[/red] {target} did not install cleanly (exit {proc.returncode}).")
    if failed:
        console.print("[dim]Try `nvh models list --all` for names that fit.[/dim]")
        raise typer.Exit(1)


@models_app.command("rm")
def models_rm(
    name: str = typer.Argument(..., help="Installed model name to remove"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
) -> None:
    """Remove an installed model and reclaim its disk."""
    import httpx

    if not yes and not typer.confirm(f"Delete model '{name}' and reclaim its disk?"):
        raise typer.Exit(0)
    base = ollama_base_url()
    try:
        resp = httpx.request("DELETE", f"{base}/api/delete", json={"name": name}, timeout=30.0)
        if resp.status_code == 404:
            console.print(f"[yellow]Model '{name}' not found.[/yellow]")
            raise typer.Exit(1)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        console.print(f"[red]Could not reach Ollama: {exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]✓[/green] Removed {name}.")


# ---------------------------------------------------------------------------
# nvh mcp servers — external MCP tool servers (roadmap critical #1, 2026-08-05)
# ---------------------------------------------------------------------------

mcp_servers_app = typer.Typer(
    help="Attach external MCP tool servers to the AI Wizard (config: $NVH_HOME/config/mcp-servers.json).",
)
mcp_app.add_typer(mcp_servers_app, name="servers")


@mcp_servers_app.command("list")
def mcp_list() -> None:
    """Show configured MCP servers + their cached tool status."""
    from nvh.integrations.mcp_client import (
        load_mcp_config,
        mcp_config_path,
        servers_status,
    )

    config = load_mcp_config()
    if not config:
        console.print(
            f"No MCP servers configured. Create [bold]{mcp_config_path()}[/bold] "
            "with a Claude-Desktop-style mcpServers object, e.g.:\n"
        )
        console.print(
            '  {\n    "mcpServers": {\n      "filesystem": {\n'
            '        "command": "npx",\n'
            '        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],\n'
            '        "auto_approve": ["read_file", "list_directory"]\n'
            "      }\n    }\n  }\n"
        )
        console.print("Then run [bold]nvh mcp servers refresh[/bold] to connect + cache tools.")
        return
    for s in servers_status():
        if s["cached"] and s["ok"]:
            state = f"[green]✓[/green] {s['tool_count']} tools (refreshed {s['refreshed_at']})"
        elif s["cached"]:
            state = f"[red]✗[/red] {s['error']}"
        else:
            state = "[yellow]not refreshed — run `nvh mcp servers refresh`[/yellow]"
        console.print(f"  [bold]{s['name']}[/bold] ({s['command']}): {state}")
        if s["ok"] and s["tools"]:
            auto = set(s["auto_approve"])
            for t in s["tools"]:
                marker = "[green]auto[/green]" if t in auto else "[yellow]confirm[/yellow]"
                console.print(f"      {t} ({marker})")


@mcp_servers_app.command("refresh")
def mcp_refresh_cmd() -> None:
    """Connect to each enabled server, list its tools, rewrite the cache.

    The Wizard picks the refreshed tools up on its next chat turn (the
    registry reads the cache on every build). The API server also warms
    this cache automatically on startup.
    """
    import asyncio as _asyncio

    from nvh.integrations.mcp_client import (
        MISSING_SDK_HINT,
        load_mcp_config,
        refresh_all_tools,
    )

    if not load_mcp_config():
        console.print(
            "No MCP servers configured — see [bold]nvh mcp servers list[/bold] for the format."
        )
        raise typer.Exit(1)
    try:
        import mcp as _mcp  # noqa: F401
    except ImportError:
        console.print(f"[red]{MISSING_SDK_HINT}[/red]")
        raise typer.Exit(1)

    console.print("[bold]Refreshing MCP server tools...[/bold]")
    cache = _asyncio.run(refresh_all_tools())
    failed = 0
    for name, entry in cache.items():
        if entry.get("ok"):
            console.print(
                f"  [green]✓[/green] {name}: {len(entry.get('tools', []))} tools"
            )
        else:
            failed += 1
            console.print(f"  [red]✗[/red] {name}: {entry.get('error')}")
    if failed:
        raise typer.Exit(1)


# Pre-0.41.1 spellings (`nvh mcp list|refresh`); hidden for one release.
mcp_app.command("list", hidden=True)(mcp_list)
mcp_app.command("refresh", hidden=True)(mcp_refresh_cmd)


def _services_render_console(snap: Any) -> None:
    """Render a service snapshot using the project's Rich console."""
    from nvh.cli.services import render_status_table  # noqa: WPS433

    console.print(render_status_table(snap))


@services_app.callback()
def _services_root(
    ctx: typer.Context,
    api_port: int = typer.Option(8000, "--api-port", help="API server port"),
    webui_port: int = typer.Option(3000, "--webui-port", help="WebUI port"),
) -> None:
    """When ``nvh services`` is invoked with no subcommand, print status.

    Typer's idiomatic way to give a subapp a default action is to use
    ``invoke_without_command=True`` and check ``ctx.invoked_subcommand``
    inside the root callback. We keep this small — the heavy lifting is
    in nvh.cli.services so the test suite can exercise it without going
    through typer.
    """
    ctx.obj = {"api_port": api_port, "webui_port": webui_port}
    if ctx.invoked_subcommand is None:
        from nvh.cli.services import snapshot

        _services_render_console(snapshot(api_port=api_port, webui_port=webui_port))


@services_app.command("status", hidden=True)
def services_status(ctx: typer.Context) -> None:
    """(alias) nvh services / nvh status — status table of Ollama, the API, and the WebUI."""
    from nvh.cli.services import snapshot

    ports = ctx.obj or {}
    _services_render_console(
        snapshot(
            api_port=ports.get("api_port", 8000),
            webui_port=ports.get("webui_port", 3000),
        )
    )


@services_app.command("start")
def services_start(
    ctx: typer.Context,
    open_browser: bool = typer.Option(
        True, "--open/--no-open",
        help="Open http://localhost:<webui_port>/setup when WebUI is ready",
    ),
) -> None:
    """Boot the whole pipeline in order, with real health gates.

    Ollama → API → WebUI. If any step fails the rest are skipped and
    the failing step + reason is printed.

    Rich Live progress table (2026-05-22): each service shows
    "waiting → starting → healthy/failed" in place. The browser only
    opens when every row reaches healthy — the user's first sight of
    the WebUI is a fully working state, never a red API-offline banner.
    """
    from rich.live import Live
    from rich.table import Table

    from nvh.cli.services import snapshot, start_pipeline
    from nvh.integrations.workspace.storage import storage_layout

    ports = ctx.obj or {}
    api_port = ports.get("api_port", 8000)
    webui_port = ports.get("webui_port", 3000)

    layout = storage_layout()
    log_dir = str(layout.logs_dir)
    layout.logs_dir.mkdir(parents=True, exist_ok=True)

    # State for the Live table — four rows, mutated as the pipeline
    # progresses. Statuses: "waiting", "starting", "healthy", "failed",
    # "skipped". The 4th row is the end-to-end Wizard smoke test that
    # closes the loop on "the user can actually use this" (not just
    # "three ports are listening").
    #
    # Row labels (2026-05-22 audit Agent C): the previous labels
    # "Ollama / API / WebUI / Smoke test" spoke engineer. First-time
    # users had no model for what those names mean. New labels lead
    # with the OUTCOME ("Local AI brain") and keep the technical name
    # parenthetical for engineers who still want to see it.
    STATE: dict[str, dict[str, str]] = {
        "Ollama":     {"label": "Local AI brain (Ollama)",  "port": "11434",            "status": "waiting", "detail": "queued"},
        "API":        {"label": "nvHive backend (API)",     "port": str(api_port),      "status": "waiting", "detail": "queued"},
        "WebUI":      {"label": "Web dashboard (WebUI)",    "port": str(webui_port),    "status": "waiting", "detail": "queued"},
        "Smoke test": {"label": "End-to-end test",          "port": "—",                "status": "waiting", "detail": "queued"},
    }

    def _render() -> Table:
        table = Table(
            title="nvHive bring-up",
            title_style="bold",
            show_header=True,
            header_style="bold dim",
        )
        table.add_column("Service", style="bold")
        table.add_column("Port")
        table.add_column("Status", min_width=10)
        table.add_column("Detail", overflow="fold")
        for _name, row in STATE.items():
            status = row["status"]
            if status == "healthy":
                status_cell = "[green]✓ ready[/green]"
            elif status == "degraded":
                status_cell = "[yellow]⚠ degraded[/yellow]"
            elif status == "starting":
                status_cell = "[cyan]⟳ starting[/cyan]"
            elif status == "failed":
                status_cell = "[red]✗ failed[/red]"
            elif status == "skipped":
                status_cell = "[yellow]– skipped[/yellow]"
            else:
                status_cell = "[dim]· waiting[/dim]"
            # Outcome-oriented label (e.g. "Local AI brain (Ollama)")
            # with the technical name parenthetical so engineers can
            # still find their bearings. See STATE init for the contract.
            table.add_row(row["label"], row["port"], status_cell, row["detail"])
        return table

    console.print("[bold]Starting service pipeline...[/bold]")
    console.print(
        f"[dim]Logs: {log_dir} · "
        "Browser opens only when every service is verified healthy.[/dim]"
    )

    with Live(_render(), console=console, refresh_per_second=4) as live:
        def _on_begin(label: str) -> None:
            STATE[label]["status"] = "starting"
            STATE[label]["detail"] = "spawning + health probe…"
            live.update(_render())

        def _on_step(label: str, ok: bool, reason: str) -> None:
            # "degraded" state: ok=True but the reason starts with
            # "degraded:" — surfaced by wizard_smoke_test when the
            # Wizard answered via the deterministic fallback (no LLM
            # loaded). The browser still opens, but the user sees
            # yellow + the fallback_reason so they know what's missing.
            if ok and reason.startswith("degraded:"):
                STATE[label]["status"] = "degraded"
            else:
                STATE[label]["status"] = "healthy" if ok else "failed"
            STATE[label]["detail"] = reason
            # Anything not yet attempted after a failure becomes "skipped".
            if not ok:
                order = ["Ollama", "API", "WebUI", "Smoke test"]
                idx = order.index(label)
                for later in order[idx + 1:]:
                    if STATE[later]["status"] == "waiting":
                        STATE[later]["status"] = "skipped"
                        STATE[later]["detail"] = "aborted (earlier step failed)"
            live.update(_render())

        result = start_pipeline(
            api_port=api_port,
            webui_port=webui_port,
            log_dir=log_dir,
            open_browser=open_browser,
            on_step=_on_step,
            on_step_begin=_on_begin,
        )

    if not result.ok:
        skipped = ", ".join(result.skipped) if result.skipped else "(nothing)"
        console.print(
            f"\n[red]Aborted at {result.failed}:[/red] {result.reason}\n"
            f"  Skipped: {skipped}"
        )
        # 2026-05-22 audit fix: every previously-failed retest cycle
        # was a user squinting at "did not become healthy in 20s" with
        # no further context. Dump the last 25 lines of the relevant
        # service log inline so the actual error is visible without
        # the user having to know the path.
        if result.log_path:
            console.print(f"  Log:     [bold]{result.log_path}[/bold]")
            if result.log_tail:
                console.print(
                    f"\n  [dim]--- {result.log_path.split('/')[-1]} "
                    f"tail (last {len(result.log_tail)} lines) ---[/dim]"
                )
                for _line in result.log_tail:
                    console.print(f"  [dim]{_line}[/dim]")
                console.print("  [dim]--- end tail ---[/dim]")
        else:
            console.print(f"  Logs:    {log_dir}")
        raise typer.Exit(1)

    # Success — final snapshot for the receipt.
    console.print()
    _services_render_console(snapshot(api_port=api_port, webui_port=webui_port))
    # If the smoke test ended in degraded mode (deterministic fallback,
    # no local LLM), surface that explicitly so the user knows the
    # WebUI works but the Wizard isn't using a local model yet. The
    # 2026-05-22 audit found the previous code printed "All services
    # healthy" even in this state — misleading.
    smoke_state = STATE.get("Smoke test", {}).get("status", "")
    smoke_detail = STATE.get("Smoke test", {}).get("detail", "")
    # Print the degraded/healthy summary unconditionally (2026-06-10
    # audit: it was gated on open_browser, so `--no-open` — the exact
    # flag scripted/SSH runs use — silently dropped the degraded-mode
    # warning). Only the trailing destination line varies.
    if open_browser:
        dest = f"Browser → http://localhost:{webui_port}/setup"
    else:
        dest = f"WebUI at http://localhost:{webui_port}/setup"
    if smoke_state == "degraded":
        console.print(
            f"\n[yellow]Services up, Wizard running in fallback mode.[/yellow]\n"
            f"  Detail:  {smoke_detail}\n"
            f"  {dest}"
        )
    else:
        console.print(
            f"\n[green]All services healthy.[/green] "
            f"{dest}"
        )


@services_app.command("restart")
def services_restart(
    ctx: typer.Context,
    open_browser: bool = typer.Option(
        True, "--open/--no-open",
        help="Open the WebUI in your browser once it's ready",
    ),
) -> None:
    """Graceful kill of all three services, then ``services start``.

    Sends SIGTERM to whatever's listening on the API + WebUI ports
    (Ollama is preserved so its model cache stays warm), waits 1s
    for the OS to settle, then re-runs the start pipeline.
    """
    from nvh.cli.services import restart_pipeline, snapshot
    from nvh.integrations.workspace.storage import storage_layout

    ports = ctx.obj or {}
    api_port = ports.get("api_port", 8000)
    webui_port = ports.get("webui_port", 3000)

    layout = storage_layout()
    log_dir = str(layout.logs_dir)
    layout.logs_dir.mkdir(parents=True, exist_ok=True)

    def _on_step(label: str, ok: bool, reason: str) -> None:
        if ok:
            console.print(f"  [green]✓[/green] {label}: {reason}")
        else:
            console.print(f"  [red]✗[/red] {label}: {reason}")

    console.print("[bold]Restarting service pipeline...[/bold]")
    result = restart_pipeline(
        api_port=api_port,
        webui_port=webui_port,
        log_dir=log_dir,
        open_browser=open_browser,
        on_step=_on_step,
    )

    console.print()
    _services_render_console(snapshot(api_port=api_port, webui_port=webui_port))

    if not result.ok:
        skipped = ", ".join(result.skipped) if result.skipped else "(nothing)"
        console.print(
            f"\n[red]Aborted at {result.failed}:[/red] {result.reason}\n"
            f"  Skipped: {skipped}\n"
            f"  Logs:    {log_dir}"
        )
        raise typer.Exit(1)


@services_app.command("stop")
def services_stop(
    ctx: typer.Context,
    ollama: bool = typer.Option(
        False, "--ollama/--no-ollama",
        help="Also stop Ollama (default: leave it running so the warmed model cache stays in RAM)",
    ),
) -> None:
    """Stop the nvHive service stack — WebUI + API (+ optional Ollama).

    Reverse-dependency order: WebUI first (so the API isn't briefly
    orphaned serving panels), then API. Ollama is preserved by default
    because killing it discards the warmed-up model in RAM — the
    user's next chat would cold-load the model again.

    Added 2026-05-22: the daemon message printed by `nvh webui` and
    the post-install console copy referenced `nvh services stop` but
    the command was never implemented — users running it got typer's
    "No such command" error after install told them to use it.
    """
    from nvh.cli.services import (
        OLLAMA_PORT,
        kill_stale_api,
        kill_stale_port,
        pids_listening_on_port,
        port_listening,
    )

    ports = ctx.obj or {}
    api_port = ports.get("api_port", 8000)
    webui_port = ports.get("webui_port", 3000)

    def _verify_released(port: int) -> bool:
        # 2026-06-10 audit: the ✓ used to print unconditionally right
        # after the kill call — which is best-effort and a silent no-op
        # when fuser/lsof are missing. Re-probe the port (up to ~3s for
        # a graceful SIGTERM shutdown) so "stopped" means stopped.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not port_listening(port):
                return True
            time.sleep(0.25)
        return False

    def _still_listening_warning(name: str, port: int) -> None:
        pids = pids_listening_on_port(port)
        pid_hint = f" (pid {', '.join(str(p) for p in pids)})" if pids else ""
        console.print(
            f"  [yellow]![/yellow] {name} on :{port} is still "
            f"listening{pid_hint} — kill it manually "
            f"(`kill <pid>`, or find it via `lsof -i :{port}` / `fuser {port}/tcp`)"
        )

    console.print("[bold]Stopping nvHive services...[/bold]")
    # WebUI first (reverse dependency order).
    if port_listening(webui_port):
        kill_stale_port(webui_port)
        if _verify_released(webui_port):
            console.print(f"  [green]✓[/green] WebUI on :{webui_port} stopped")
        else:
            _still_listening_warning("WebUI", webui_port)
    else:
        console.print(f"  [dim]·[/dim] WebUI on :{webui_port} was not running")
    # Then API.
    if port_listening(api_port):
        kill_stale_api(api_port)
        if _verify_released(api_port):
            console.print(f"  [green]✓[/green] API on :{api_port} stopped")
        else:
            _still_listening_warning("API", api_port)
    else:
        console.print(f"  [dim]·[/dim] API on :{api_port} was not running")
    # Ollama only if explicitly requested.
    if ollama:
        if port_listening(OLLAMA_PORT):
            kill_stale_port(OLLAMA_PORT)
            if _verify_released(OLLAMA_PORT):
                console.print(f"  [green]✓[/green] Ollama on :{OLLAMA_PORT} stopped")
            else:
                _still_listening_warning("Ollama", OLLAMA_PORT)
        else:
            console.print(f"  [dim]·[/dim] Ollama on :{OLLAMA_PORT} was not running")
    else:
        if port_listening(OLLAMA_PORT):
            console.print(
                f"  [dim]·[/dim] Ollama on :{OLLAMA_PORT} left running "
                "(use --ollama to stop it; preserves warmed model cache)"
            )


@services_app.command("smoke-test")
def services_smoke_test(
    ctx: typer.Context,
    # 90s default (2026-06-10 audit): aligned with wizard_smoke_test's
    # own default — the manual command used to allow only 45s while the
    # identical pipeline check allowed 90s, so a cold first run could
    # pass bring-up yet fail the manual re-check.
    timeout: float = typer.Option(
        90.0, "--timeout",
        help="Seconds to wait for the Wizard to answer (cold model load can run 30s+)",
    ),
) -> None:
    """End-to-end "can the Wizard actually answer?" check.

    POSTs a small chat request to ``/v1/wizard/chat`` and verifies a
    non-empty answer comes back. Use this any time you suspect the
    stack is half-working — e.g. all ports listening but the Wizard
    is silent in the WebUI.

    Exits 0 on success (non-empty answer, any mode including fallback),
    1 on hard failure (timeout, endpoint error, empty answer).
    """
    from nvh.cli.services import wizard_smoke_test

    ports = ctx.obj or {}
    api_port = ports.get("api_port", 8000)

    console.print(f"[bold]Wizard smoke test on :{api_port}...[/bold]")
    console.print(f"[dim]Timeout: {timeout:.0f}s · POST /v1/wizard/chat[/dim]")
    ok, reason = wizard_smoke_test(api_port=api_port, timeout=timeout)
    if ok:
        console.print(f"  [green]✓[/green] {reason}")
        return
    console.print(f"  [red]✗[/red] {reason}")
    console.print(
        "\nTo diagnose:\n"
        "  [bold]nvh status[/bold]                show per-service health\n"
        "  [bold]nvh status --deep --json[/bold]  run the full diagnostic\n"
        "  [bold]nvh services restart[/bold]  recycle the API\n"
    )
    raise typer.Exit(1)


if __name__ == "__main__":
    main()
