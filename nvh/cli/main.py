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
import webbrowser
from decimal import Decimal
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from nvh import __version__

# The callback handles `nvh "question"` with no subcommand — smart default mode
app = typer.Typer(
    name="nvh",
    help="NVHive — Multi-LLM orchestration. Just type: nvh \"your question\"",
    no_args_is_help=False,
)
console = Console()


async def _smart_default(prompt: str):
    """Smart default handler — detects actions vs questions, routes accordingly.

    Flow:
    1. Check if the prompt is a SYSTEM ACTION (install, open, find, kill, etc.)
       → If yes, execute the action directly via tools (no LLM needed)
    2. If it's a QUESTION, route based on profile mode setting
       → ask, convene, poll, or throwdown
    """
    from nvh.config.settings import load_config

    # --- Step 1: Check if this is a system action, not a question ---
    from nvh.core.action_detector import detect_action
    from nvh.core.engine import Engine
    action = detect_action(prompt)
    if action:
        await _execute_action(action)
        return

    # --- Step 2: It's a question — route to LLM ---
    config = load_config()
    engine = Engine(config=config)
    await engine.initialize()

    # Determine default mode from config (fallback to "ask")
    default_mode = getattr(config.defaults, "mode", "ask")

    if default_mode == "convene":
        console.print("[dim][convene → auto-agents][/dim]\n")
        try:
            result = await engine.run_council(
                prompt=prompt,
                auto_agents=True,
                synthesize=True,
            )
            # Display synthesis
            if result.synthesis:
                console.print(result.synthesis.content)
                console.print(f"\n[dim]Agents: {', '.join(result.agents_used)} | Cost: ${result.total_cost_usd:.4f} | {result.total_latency_ms}ms[/dim]")
            else:
                for label, resp in result.member_responses.items():
                    console.print(Panel(resp.content, title=label, border_style="blue"))
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    elif default_mode == "poll":
        console.print("[dim][poll → all advisors][/dim]\n")
        try:
            results = await engine.compare(prompt=prompt)
            for pname, resp in results.items():
                header = f"{pname}/{resp.model}  {resp.latency_ms}ms  ${resp.cost_usd:.4f}"
                console.print(Panel(resp.content, title=header, border_style="cyan"))
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    elif default_mode == "throwdown":
        console.print("[dim][throwdown → two-pass deep analysis][/dim]\n")
        try:
            pass1 = await engine.run_council(
                prompt=prompt,
                auto_agents=True,
                synthesize=True,
            )
            critique_prompt = (
                f"Original question: {prompt}\n\n"
                f"A council of AI experts produced this initial analysis:\n\n"
                f"{pass1.synthesis.content if pass1.synthesis else 'No synthesis available'}\n\n"
                "Now critique this analysis. What did the experts miss? "
                "What assumptions are wrong? Provide a refined, improved answer."
            )
            pass2 = await engine.run_council(
                prompt=critique_prompt,
                auto_agents=True,
                synthesize=True,
            )
            final_prompt = (
                f"Original question: {prompt}\n\n"
                f"Pass 1 analysis:\n{pass1.synthesis.content if pass1.synthesis else ''}\n\n"
                f"Pass 2 critique:\n{pass2.synthesis.content if pass2.synthesis else ''}\n\n"
                "Produce a definitive final answer integrating the best insights from both passes."
            )
            final = await engine.query(prompt=final_prompt, stream=False)
            console.print(Panel(
                final.content,
                title="[bold green]THROWDOWN RESULT[/bold green]",
                border_style="green",
            ))
            total_cost = pass1.total_cost_usd + pass2.total_cost_usd + (final.cost_usd if final else Decimal("0"))
            console.print(f"\n[dim]Total cost: ${total_cost:.4f}[/dim]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    else:
        # Default: ask — smart route to best advisor
        try:
            # Get routing decision first so we can show the mode indicator
            decision = engine.router.route(prompt)
            console.print(f"[dim][ask → {decision.provider}/{decision.model}][/dim]\n")
            resp = await engine.query(prompt=prompt, stream=False)
            console.print(resp.content)
            console.print(f"\n[dim]Advisor: {resp.provider} | Model: {resp.model} | "
                         f"Tokens: {resp.usage.input_tokens}/{resp.usage.output_tokens} | "
                         f"Cost: ${resp.cost_usd:.4f} | {resp.latency_ms}ms[/dim]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

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
        console.print(f"[red]Error: {e}[/red]")


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
        console.print("  [bold]nvh setup[/bold]    — configure free AI providers (recommended)")
        console.print("  [bold]nvh ollama[/bold]   — set up local AI on your GPU")
        console.print("  [bold]nvh openai[/bold]   — add your OpenAI API key")
        console.print("  [bold]nvh groq[/bold]     — add Groq (free, ultra-fast)\n")

        # Check if Ollama is available even without config
        try:
            import httpx
            resp = httpx.get("http://localhost:11434/api/tags", timeout=2)
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
    "anthropic": {"name": "Anthropic", "url": "https://console.anthropic.com/settings/keys", "free_tier": False},
    "google": {"name": "Google Gemini", "url": "https://aistudio.google.com/apikey", "free_tier": True, "free_info": "15 req/min free"},
    "groq": {"name": "Groq", "url": "https://console.groq.com/keys", "free_tier": True, "free_info": "Free tier: 30 req/min, 14.4K tok/min"},
    "grok": {"name": "Grok (xAI)", "url": "https://console.x.ai", "free_tier": False},
    "mistral": {"name": "Mistral", "url": "https://console.mistral.ai/api-keys", "free_tier": True, "free_info": "Free Experiment plan: 2 RPM"},
    "cohere": {"name": "Cohere", "url": "https://dashboard.cohere.com/api-keys", "free_tier": True, "free_info": "Trial API key included on signup"},
    "deepseek": {"name": "DeepSeek", "url": "https://platform.deepseek.com", "free_tier": False, "free_info": "Very cheap: $0.07/M tokens"},
    "ollama": {"name": "Ollama (Local)", "url": "https://ollama.com/download", "free_tier": True, "free_info": "Unlimited, free, runs on your GPU"},
    "mock": {"name": "Mock (Testing)", "url": "", "free_tier": True, "free_info": "Testing only, no real API calls"},
    "perplexity": {"name": "Perplexity", "url": "https://www.perplexity.ai/settings/api", "free_tier": False, "free_info": "Search-augmented responses with citations"},
    "together": {"name": "Together AI", "url": "https://api.together.xyz/settings/api-keys", "free_tier": False, "free_info": "Requires $5 minimum purchase"},
    "fireworks": {"name": "Fireworks AI", "url": "https://fireworks.ai/account/api-keys", "free_tier": True, "free_info": "Free tier available"},
    "openrouter": {"name": "OpenRouter", "url": "https://openrouter.ai/keys", "free_tier": False, "free_info": "Routes to best available provider"},
    "cerebras": {"name": "Cerebras", "url": "https://cloud.cerebras.ai", "free_tier": True, "free_info": "Free tier: 30 req/min"},
    "sambanova": {"name": "SambaNova", "url": "https://cloud.sambanova.ai", "free_tier": True, "free_info": "Free tier available"},
    "huggingface": {"name": "Hugging Face", "url": "https://huggingface.co/settings/tokens", "free_tier": True, "free_info": "Free Inference API"},
    "ai21": {"name": "AI21 Labs", "url": "https://studio.ai21.com/account/api-key", "free_tier": True, "free_info": "Free tier available"},
    "github": {"name": "GitHub Models", "url": "https://github.com/marketplace/models", "free_tier": True, "free_info": "Free for all GitHub users: 50-150 req/day, frontier models"},
    "nvidia": {"name": "NVIDIA NIM", "url": "https://build.nvidia.com", "free_tier": True, "free_info": "1000+ free API credits, 40 RPM, NVIDIA Developer Program"},
    "siliconflow": {"name": "SiliconFlow", "url": "https://cloud.siliconflow.cn", "free_tier": True, "free_info": "Permanently free models at 1000 RPM"},
    "llm7": {"name": "LLM7", "url": "https://llm7.io", "free_tier": True, "free_info": "Anonymous access: 30 RPM, no signup required"},
}


def _make_advisor_cmd(advisor_name: str):
    """Factory: create a Typer command for an advisor."""
    info = KNOWN_ADVISORS[advisor_name]

    def cmd(
        question: str | None = typer.Argument(None, help="Question to ask this advisor"),
        model: str | None = typer.Option(None, "-m", "--model", help="Specific model"),
        system: str | None = typer.Option(None, "-s", "--system", help="System prompt"),
        raw: bool = typer.Option(False, "--raw", help="Output just the answer, no metadata"),
    ):
        if question:
            # nvh openai "What is ML?" → ask this advisor
            async def _ask():
                from nvh.config.settings import load_config
                from nvh.core.engine import Engine
                config = load_config()
                engine = Engine(config=config)
                await engine.initialize()
                try:
                    resp = await engine.query(
                        prompt=question,
                        provider=advisor_name,
                        model=model,
                        system_prompt=system,
                        stream=False,
                    )
                    if raw:
                        print(resp.content, end="")
                    else:
                        console.print(resp.content)
                        console.print(f"\n[dim]{resp.provider}/{resp.model} | "
                                     f"{resp.usage.input_tokens}/{resp.usage.output_tokens} tokens | "
                                     f"${resp.cost_usd:.4f} | {resp.latency_ms}ms[/dim]")
                except Exception as e:
                    console.print(f"[red]Error: {e}[/red]")
            _run(_ask())
        else:
            # nvh openai → setup/login
            if advisor_name == "ollama":
                console.print(f"[bold]{info['name']}[/bold] — {info.get('free_info', '')}")
                console.print("Checking Ollama connectivity...")
                try:
                    import httpx
                    resp = httpx.get("http://localhost:11434/api/tags", timeout=3)
                    if resp.status_code == 200:
                        models = resp.json().get("models", [])
                        console.print(f"[green]Connected! {len(models)} models available.[/green]")
                        for m in models[:5]:
                            console.print(f"  - {m.get('name', '?')}")
                    else:
                        console.print("[yellow]Ollama returned an error.[/yellow]")
                except Exception:
                    console.print("[red]Ollama not reachable at localhost:11434[/red]")
                    console.print("Install: curl -fsSL https://ollama.com/install.sh | sh")
                return

            console.print(f"[bold]{info['name']}[/bold] — Setup")
            if info.get("free_tier"):
                console.print(f"[green]Free tier available: {info.get('free_info', '')}[/green]")
            if info["url"]:
                console.print(f"Get your API key: [link={info['url']}]{info['url']}[/link]")
                if typer.confirm("Open in browser?", default=True):
                    webbrowser.open(info["url"])
            key = typer.prompt(f"Paste your {info['name']} API key", hide_input=True, default="")
            if key:
                try:
                    import keyring
                    keyring.set_password("nvhive", f"{advisor_name}_api_key", key)
                    console.print("[green]Key stored securely.[/green]")
                except Exception:
                    console.print(f"[yellow]Set {advisor_name.upper()}_API_KEY in your environment.[/yellow]")

    cmd.__name__ = f"{advisor_name}_cmd"
    cmd.__doc__ = f"Ask {info['name']}, or set up API key if no question given."
    return cmd


# Register all advisor names as commands
for _adv_name in KNOWN_ADVISORS:
    if _adv_name != "mock":  # skip mock from top-level
        app.command(_adv_name)(_make_advisor_cmd(_adv_name))


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
    parts = []
    if resp.provider:
        parts.append(f"Provider: {resp.provider}")
    if resp.model:
        parts.append(f"Model: {resp.model}")
    if resp.usage.total_tokens:
        parts.append(f"Tokens: {resp.usage.input_tokens} in / {resp.usage.output_tokens} out")
    if resp.cost_usd:
        parts.append(f"Cost: ${resp.cost_usd:.4f}")
    if resp.latency_ms:
        parts.append(f"Latency: {resp.latency_ms}ms")
    if resp.cache_hit:
        parts.append("(cached)")
    if resp.fallback_from:
        parts.append(f"(fallback from {resp.fallback_from})")

    if parts:
        console.print(f"\n[dim]{' | '.join(parts)}[/dim]")


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


def _read_stdin() -> str:
    """Read from stdin if piped."""
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


# ---------------------------------------------------------------------------
# hive ask
# ---------------------------------------------------------------------------

@app.command()
def ask(
    prompt: str | None = typer.Argument(None, help="The prompt to send to the LLM"),
    provider: str | None = typer.Option(None, "-p", "--advisor", help="Advisor to use"),
    model: str | None = typer.Option(None, "-m", "--model", help="Model to use"),
    system: str | None = typer.Option(None, "-s", "--system", help="System prompt"),
    output: str = typer.Option("text", "-o", "--output", help="Output format: text, json, markdown, raw"),
    stream: bool = typer.Option(True, "--stream/--no-stream", help="Stream output"),
    max_tokens: int | None = typer.Option(None, "--max-tokens", help="Max output tokens"),
    temperature: float | None = typer.Option(None, "-t", "--temperature", help="Temperature"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass cache"),
    strategy: str = typer.Option("best", "--strategy", help="Routing: best, cheapest, fastest, best-for-task"),
    continue_: bool = typer.Option(False, "-c", "--continue", help="Continue last conversation"),
    conversation: str | None = typer.Option(None, "--conversation", help="Continue a specific conversation"),
    profile: str | None = typer.Option(None, "--profile", help="Config profile to use"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Show routing details"),
    quiet: bool = typer.Option(False, "-q", "--quiet", help="Suppress metadata"),
    privacy: bool = typer.Option(False, "--privacy", help="Privacy mode: disable logging, caching, and conversation persistence"),
    template: str | None = typer.Option(None, "--template", help="Prompt template name to use"),
    var: list[str] | None = typer.Option(None, "--var", help="Template variable as key=value (repeatable)"),
    file: str | None = typer.Option(None, "-f", "--file", help="Include a file's contents in the prompt"),
    output_json: bool = typer.Option(False, "--json", help="Shorthand for --output json"),
    output_raw: bool = typer.Option(False, "--raw", help="Shorthand for --output raw (no metadata, just the answer)"),
    knowledge: bool = typer.Option(False, "--knowledge", "-k", help="Augment prompt with your knowledge base (RAG)"),
):
    """Ask a single LLM advisor a question."""
    # Apply shorthand output flags
    if output_json:
        output = "json"
    elif output_raw:
        output = "raw"
        quiet = True
    # Handle template rendering before stdin processing
    template_system: str | None = None
    if template:
        from nvh.core.templates import render_template
        # Parse --var key=value pairs
        template_vars: dict[str, str] = {}
        for item in (var or []):
            if "=" in item:
                k, _, v = item.partition("=")
                template_vars[k.strip()] = v.strip()
            else:
                console.print(f"[red]Error: --var '{item}' must be in key=value format.[/red]")
                raise typer.Exit(1)
        # If a positional prompt was given, use it as the primary variable if no explicit var mapping
        if prompt and "text" not in template_vars and "code" not in template_vars:
            template_vars.setdefault("text", prompt)
            template_vars.setdefault("code", prompt)
        try:
            rendered_prompt, template_system = render_template(template, template_vars)
            prompt = rendered_prompt
            if template_system and not system:
                system = template_system
        except (FileNotFoundError, ValueError) as e:
            console.print(f"[red]Template error: {e}[/red]")
            raise typer.Exit(1)

    # Read from --file if provided
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

    # Read from stdin if no prompt provided
    stdin_content = _read_stdin()
    if not prompt and not stdin_content and not file_content:
        console.print("[red]Error: No prompt provided. Pass a prompt or pipe input via stdin.[/red]")
        raise typer.Exit(1)

    full_prompt = ""
    parts_to_join = []
    if prompt:
        parts_to_join.append(prompt)
    if file_content:
        parts_to_join.append(f"```\n{file_content}\n```")
    if stdin_content:
        parts_to_join.append(stdin_content)
    full_prompt = "\n\n".join(parts_to_join) if parts_to_join else (prompt or "")  # type: ignore

    # RAG: prepend knowledge base context if requested
    if knowledge:
        from nvh.core.knowledge import get_knowledge_base
        kb = get_knowledge_base()
        kb_context = kb.get_context(full_prompt)
        if kb_context:
            full_prompt = kb_context + "\n\n" + full_prompt
            if not quiet:
                console.print("[dim][knowledge base context injected][/dim]")
        else:
            console.print("[dim][knowledge base: no relevant documents found — run 'nvh learn <file>' to add some][/dim]")

    async def _run_query():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config(profile=profile)
        engine = Engine(config=config)
        await engine.initialize()

        if privacy:
            console.print("[dim][privacy mode — no data stored][/dim]")

        if verbose:
            from nvh.core.router import classify_task
            classification = classify_task(full_prompt)
            console.print(f"[dim]Task type: {classification.task_type.value} (confidence: {classification.confidence:.2f})[/dim]")

        if stream and output == "text":
            # Stream the response
            decision = engine.router.route(full_prompt, provider_override=provider, model_override=model, strategy=strategy)

            if verbose:
                console.print(f"[dim]Routed to: {decision.provider}/{decision.model} ({decision.reason})[/dim]")

            prov = engine.registry.get(decision.provider)
            pconfig = config.providers.get(decision.provider)
            pmodel = model or decision.model or (pconfig.default_model if pconfig else "")

            from nvh.providers.base import Message
            msgs = [Message(role="user", content=full_prompt)]
            if system:
                msgs.insert(0, Message(role="system", content=system))

            import time
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
                        parts = [f"Provider: {decision.provider}", f"Model: {pmodel}"]
                        if chunk.usage:
                            parts.append(f"Tokens: {chunk.usage.input_tokens} in / {chunk.usage.output_tokens} out")
                        if chunk.cost_usd:
                            parts.append(f"Cost: ${chunk.cost_usd:.4f}")
                        parts.append(f"Latency: {elapsed}ms")
                        console.print(f"\n[dim]{' | '.join(parts)}[/dim]")
            except Exception as e:
                console.print(f"\n[red]Error: {e}[/red]")
                raise typer.Exit(1)
        else:
            # Non-streaming
            try:
                resp = await engine.query(
                    prompt=full_prompt,
                    provider=provider,
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
                )
                _format_output(resp.content, output)
                _print_metadata(resp, show=not quiet)
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")
                raise typer.Exit(1)

    _run(_run_query())


# ---------------------------------------------------------------------------
# Focus Modes
# ---------------------------------------------------------------------------

@app.command()
def code(
    prompt: str | None = typer.Argument(None),
    file: str | None = typer.Option(None, "-f", "--file"),
    advisor: str | None = typer.Option(None, "-a"),
):
    """Coding focus — optimized for code tasks.

    Auto-selects the best coding advisor, enables code-specific system prompt,
    and formats output for code.

    Examples:
        nvh code "Write a binary search in Python"
        nvh code -f main.py "Fix the bug on line 42"
        nvh code "Explain this regex: ^[a-z]+@[a-z]+\\.[a-z]{2,}$"
    """
    system_prompt = (
        "You are an expert software engineer. Provide clear, correct, well-structured code. "
        "When writing code, include brief explanations of key decisions. "
        "Prefer idiomatic solutions. Highlight any edge cases or caveats."
    )

    full_prompt = prompt or ""

    if file:
        file_path_obj = Path(file)
        if not file_path_obj.exists():
            console.print(f"[red]Error: File not found: {file}[/red]")
            raise typer.Exit(1)
        try:
            file_content = file_path_obj.read_text()
            file_block = f"File: {file}\n```\n{file_content}\n```"
            full_prompt = f"{file_block}\n\n{full_prompt}".strip() if full_prompt else file_block
        except Exception as e:
            console.print(f"[red]Error reading file {file}: {e}[/red]")
            raise typer.Exit(1)

    stdin_content = _read_stdin()
    if stdin_content:
        full_prompt = f"{full_prompt}\n\n{stdin_content}".strip() if full_prompt else stdin_content

    if not full_prompt:
        console.print("[red]Error: No prompt provided.[/red]")
        raise typer.Exit(1)

    async def _run_code():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()

        # Route to coding-capable advisor; prefer anthropic/openai/groq for code
        coding_advisors = ["anthropic", "openai", "groq", "github", "google", "deepseek"]
        enabled = engine.registry.list_enabled()
        chosen_provider = advisor
        if not chosen_provider:
            for pref in coding_advisors:
                if pref in enabled:
                    chosen_provider = pref
                    break

        console.print(f"[dim][code → {chosen_provider or 'auto'}][/dim]\n")
        try:
            resp = await engine.query(
                prompt=full_prompt,
                provider=chosen_provider,
                system_prompt=system_prompt,
                stream=False,
            )
            console.print(Markdown(resp.content))
            console.print(
                f"\n[dim]Advisor: {resp.provider} | Model: {resp.model} | "
                f"Cost: ${resp.cost_usd:.4f} | {resp.latency_ms}ms[/dim]"
            )
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    _run(_run_code())


@app.command()
def write(
    prompt: str | None = typer.Argument(None),
    tone: str = typer.Option("professional", help="Tone: casual, professional, academic, creative"),
):
    """Writing focus — optimized for text composition.

    Auto-selects the best writing advisor (Claude preferred).

    Examples:
        nvh write "Draft an email declining a meeting"
        nvh write "Write a blog post about AI" --tone casual
        nvh write "Create a cover letter for a software engineer position"
    """
    system_prompt = (
        f"You are a skilled writer. Write with a {tone} tone. "
        "Produce clear, engaging, well-structured text. "
        "Match the format to the request (email, essay, blog post, etc.)."
    )

    full_prompt = prompt or _read_stdin().strip()
    if not full_prompt:
        console.print("[red]Error: No prompt provided.[/red]")
        raise typer.Exit(1)

    async def _run_write():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()

        # Claude is best for writing; fall back to openai, google
        writing_advisors = ["anthropic", "openai", "google", "groq", "github"]
        enabled = engine.registry.list_enabled()
        chosen_provider = None
        for pref in writing_advisors:
            if pref in enabled:
                chosen_provider = pref
                break

        console.print(f"[dim][write → {chosen_provider or 'auto'} | tone: {tone}][/dim]\n")
        try:
            resp = await engine.query(
                prompt=full_prompt,
                provider=chosen_provider,
                system_prompt=system_prompt,
                stream=False,
            )
            console.print(resp.content)
            console.print(
                f"\n[dim]Advisor: {resp.provider} | Model: {resp.model} | "
                f"Cost: ${resp.cost_usd:.4f} | {resp.latency_ms}ms[/dim]"
            )
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    _run(_run_write())


@app.command()
def research(
    prompt: str | None = typer.Argument(None),
):
    """Research focus — web search + multi-source synthesis.

    Automatically searches the web, fetches relevant pages,
    and synthesizes findings.

    Examples:
        nvh research "Latest developments in quantum computing"
        nvh research "Compare React vs Vue vs Svelte in 2026"
    """
    system_prompt = (
        "You are a thorough research assistant. Synthesize information from multiple sources. "
        "Always cite your sources. Highlight areas of consensus and disagreement. "
        "Provide a balanced, well-structured summary."
    )

    full_prompt = prompt or _read_stdin().strip()
    if not full_prompt:
        console.print("[red]Error: No prompt provided.[/red]")
        raise typer.Exit(1)

    async def _run_research():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()

        console.print("[dim][research → web search + synthesis][/dim]\n")

        # Step 1: web search via Perplexity if available, else council synthesis
        enabled = engine.registry.list_enabled()
        if "perplexity" in enabled:
            console.print("[dim]Searching with Perplexity...[/dim]")
            try:
                search_resp = await engine.query(
                    prompt=full_prompt,
                    provider="perplexity",
                    system_prompt=system_prompt,
                    stream=False,
                )
                console.print(Markdown(search_resp.content))
                console.print(
                    f"\n[dim]Advisor: {search_resp.provider} | Model: {search_resp.model} | "
                    f"Cost: ${search_resp.cost_usd:.4f} | {search_resp.latency_ms}ms[/dim]"
                )
                return
            except Exception:
                pass  # fall through to council synthesis

        # Fall back: council synthesis with research system prompt
        console.print("[dim]Synthesizing from multiple advisors...[/dim]\n")
        try:
            result = await engine.run_council(
                prompt=full_prompt,
                system_prompt=system_prompt,
                auto_agents=True,
                synthesize=True,
            )
            if result.synthesis:
                console.print(Markdown(result.synthesis.content))
                console.print(
                    f"\n[dim]Agents: {', '.join(result.agents_used)} | "
                    f"Cost: ${result.total_cost_usd:.4f} | {result.total_latency_ms}ms[/dim]"
                )
            else:
                for label, resp in result.member_responses.items():
                    console.print(Panel(resp.content, title=label, border_style="blue"))
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    _run(_run_research())


@app.command()
def math(
    prompt: str | None = typer.Argument(None),
):
    """Math focus — optimized for math and calculations.

    Routes to reasoning-focused advisors (o3, DeepSeek-R1).
    Enables step-by-step problem solving.

    Examples:
        nvh math "Solve: integral of x^2 * sin(x) dx"
        nvh math "Prove that sqrt(2) is irrational"
    """
    system_prompt = (
        "You are an expert mathematician. Solve problems step by step, showing all work. "
        "Use clear notation. Verify your answer when possible. "
        "If there are multiple approaches, briefly mention alternatives after the main solution."
    )

    full_prompt = prompt or _read_stdin().strip()
    if not full_prompt:
        console.print("[red]Error: No prompt provided.[/red]")
        raise typer.Exit(1)

    async def _run_math():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()

        # Route to reasoning-focused advisors: o3, DeepSeek-R1, then general fallback
        math_advisors = ["openai", "deepseek", "anthropic", "google", "groq"]
        enabled = engine.registry.list_enabled()
        chosen_provider = None
        for pref in math_advisors:
            if pref in enabled:
                chosen_provider = pref
                break

        # For OpenAI, prefer o3/o1 reasoning models
        chosen_model = None
        if chosen_provider == "openai":
            chosen_model = "o3-mini"
        elif chosen_provider == "deepseek":
            chosen_model = "deepseek-reasoner"

        console.print(
            f"[dim][math → {chosen_provider or 'auto'}"
            f"{f'/{chosen_model}' if chosen_model else ''} | step-by-step][/dim]\n"
        )
        try:
            resp = await engine.query(
                prompt=full_prompt,
                provider=chosen_provider,
                model=chosen_model,
                system_prompt=system_prompt,
                stream=False,
            )
            console.print(Markdown(resp.content))
            console.print(
                f"\n[dim]Advisor: {resp.provider} | Model: {resp.model} | "
                f"Cost: ${resp.cost_usd:.4f} | {resp.latency_ms}ms[/dim]"
            )
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    _run(_run_math())


# ---------------------------------------------------------------------------
# Clipboard Integration
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
    "ask": "Answer any questions about the following content, or describe it if no question is obvious:",
    "explain": "Explain the following clearly and concisely:",
    "fix": (
        "Fix any bugs, errors, or issues in the following code. "
        "Return the corrected version with a brief explanation of what was changed:"
    ),
    "summarize": "Summarize the following in a few sentences:",
    "translate": "Translate the following text to English (or if already English, to Spanish):",
}


@app.command()
def clip(
    action: str = typer.Argument("ask", help="What to do: ask, explain, fix, summarize, translate"),
    advisor: str | None = typer.Option(None, "-a"),
    copy: bool = typer.Option(False, "--copy", "-c", help="Copy the result back to clipboard"),
):
    """Process clipboard contents with AI.

    Reads your clipboard and applies an action to it.

    Examples:
        nvh clip              # ask about clipboard contents
        nvh clip explain      # explain the clipboard contents
        nvh clip fix          # fix code from clipboard
        nvh clip summarize    # summarize clipboard text
        nvh clip translate    # translate clipboard text to English
    """
    valid_actions = list(_CLIP_ACTIONS.keys())
    if action not in valid_actions:
        console.print(f"[red]Unknown action '{action}'. Choose from: {', '.join(valid_actions)}[/red]")
        raise typer.Exit(1)

    try:
        clipboard_text = _read_clipboard()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if not clipboard_text.strip():
        console.print("[yellow]Clipboard is empty.[/yellow]")
        raise typer.Exit(1)

    action_instruction = _CLIP_ACTIONS[action]
    full_prompt = f"{action_instruction}\n\n{clipboard_text}"

    # Show a preview of what's being processed
    preview = clipboard_text[:80].replace("\n", " ")
    if len(clipboard_text) > 80:
        preview += "..."
    console.print(f"[dim]Clipboard ({len(clipboard_text)} chars): {preview}[/dim]\n")

    async def _run_clip():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()

        console.print(f"[dim][clip:{action} → {advisor or 'auto'}][/dim]\n")
        try:
            resp = await engine.query(
                prompt=full_prompt,
                provider=advisor,
                stream=False,
            )
            console.print(resp.content)
            console.print(
                f"\n[dim]Advisor: {resp.provider} | Model: {resp.model} | "
                f"Cost: ${resp.cost_usd:.4f} | {resp.latency_ms}ms[/dim]"
            )
            if copy:
                try:
                    _write_clipboard(resp.content)
                    console.print("[dim]Result copied to clipboard.[/dim]")
                except RuntimeError as e:
                    console.print(f"[yellow]Could not copy to clipboard: {e}[/yellow]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    _run(_run_clip())


# ---------------------------------------------------------------------------
# hive convene (hive mode)
# ---------------------------------------------------------------------------

@app.command("convene")
def convene_cmd(
    prompt: str = typer.Argument(..., help="The prompt to send to the hive"),
    members: str | None = typer.Option(None, "--members", help="Comma-separated advisor list"),
    weights: str | None = typer.Option(None, "--weights", help="Advisor weights, e.g. openai=0.4,anthropic=0.6"),
    strategy: str | None = typer.Option(None, "--strategy", help="Consensus: weighted_consensus, majority_vote, best_of"),
    system: str | None = typer.Option(None, "-s", "--system", help="System prompt"),
    output: str = typer.Option("text", "-o", "--output", help="Output format: text, json, table"),
    max_tokens: int | None = typer.Option(None, "--max-tokens"),
    temperature: float | None = typer.Option(None, "-t", "--temperature"),
    no_synthesize: bool = typer.Option(False, "--no-synthesize", help="Skip synthesis, show raw responses"),
    auto_agents: bool = typer.Option(False, "--auto-agents", "-a", help="Auto-generate expert personas based on query content"),
    preset: str | None = typer.Option(None, "--cabinet", help="Agent cabinet: executive, engineering, security_review, code_review, product, data, full_board"),
    num_agents: int | None = typer.Option(None, "--num-agents", "-n", help="Number of agent personas to generate"),
    profile: str | None = typer.Option(None, "--profile"),
    quiet: bool = typer.Option(False, "-q", "--quiet"),
    privacy: bool = typer.Option(False, "--privacy", help="Privacy mode: disable logging, caching, and conversation persistence"),
    output_raw: bool = typer.Option(False, "--raw", help="Output just the synthesis text, no panels"),
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
                personas = generate_agents(prompt, num_agents=num_agents or len(member_list or engine.registry.list_enabled()))

            console.print("[bold]Hive Mode[/bold] — auto-generated expert advisors:\n")
            for p in personas:
                console.print(f"  [bold cyan]{p.role}[/bold cyan] — {p.expertise}")
            console.print()
        else:
            console.print(f"[bold]Hive Mode[/bold] — querying {len(member_list or engine.registry.list_enabled())} advisors...\n")

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
            console.print(f"[red]Error: {e}[/red]")
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
                    p: {"content": r.content, "model": r.model, "cost_usd": str(r.cost_usd), "latency_ms": r.latency_ms}
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
                header = f"{persona} ({resp.provider}) [weight: {weight:.0%}]  {resp.latency_ms}ms  ${resp.cost_usd:.4f}"
                console.print(Panel(resp.content, title=header, border_style="blue"))
            else:
                header = f"{label} [weight: {weight:.0%}]  {resp.latency_ms}ms  ${resp.cost_usd:.4f}"
                console.print(Panel(resp.content, title=header, border_style="blue"))

        # Display failures
        for label, error in result.failed_members.items():
            if label != "_synthesis":
                console.print(Panel(f"[red]{error}[/red]", title=f"{label} (FAILED)", border_style="red"))

        # Display synthesis
        if result.synthesis:
            console.print()
            console.print(Panel(
                result.synthesis.content,
                title=f"SYNTHESIS ({result.strategy})",
                border_style="green",
            ))

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

@app.command()
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
            console.print(f"[red]Error: {e}[/red]")
            raise typer.Exit(1)

        if output == "json":
            import json
            data = {
                p: {"content": r.content, "model": r.model, "cost_usd": str(r.cost_usd), "latency_ms": r.latency_ms}
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
# nvh throwdown — two-pass deep analysis with all APIs and agents
# ---------------------------------------------------------------------------

@app.command()
def throwdown(
    prompt: str = typer.Argument(..., help="The question for the throwdown"),
    cabinet: str | None = typer.Option(None, "--cabinet", "-c", help="Agent cabinet to use"),
    num_agents: int | None = typer.Option(None, "-n", "--num-agents", help="Number of agents"),
    profile: str | None = typer.Option(None, "--profile"),
    quiet: bool = typer.Option(False, "-q", "--quiet"),
    quick: bool = typer.Option(False, "--quick", help="Single pass instead of two (cheaper throwdown)"),
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
                f"Pass 2 critique and refinement:\n{pass2.synthesis.content if pass2.synthesis else ''}\n\n"
                f"Produce a definitive final answer that integrates the best insights from both passes. "
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
            total_cost = pass1.total_cost_usd + pass2.total_cost_usd + (final.cost_usd if final else 0)
            total_time = pass1.total_latency_ms + pass2.total_latency_ms + (final.latency_ms if final else 0)
            console.print(f"\n[dim]Throwdown complete | Total cost: ${total_cost:.4f} | "
                         f"Total time: {total_time}ms | "
                         f"Agents used: {', '.join(pass1.agents_used)}[/dim]")

    _run(_run_throwdown())


# ---------------------------------------------------------------------------
# nvh status
# ---------------------------------------------------------------------------

@app.command()
def status():
    """Quick system status — advisors, GPU, budget, and models at a glance."""
    from rich.rule import Rule

    async def _run_status():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config()
        engine = Engine(config=config)
        enabled_providers = await engine.initialize()

        console.print(f"[bold]NVHive v{__version__}[/bold]")
        console.print(Rule(style="dim"))

        # GPU
        try:
            from nvh.utils.gpu import detect_gpus
            gpus = detect_gpus()
            if gpus:
                gpu_parts = []
                for g in gpus:
                    gpu_parts.append(f"{g.name} ({g.vram_gb:.0f} GB) — {g.utilization_pct}% utilized")
                gpu_line = " | ".join(gpu_parts)
            else:
                gpu_line = "no NVIDIA GPU detected (CPU mode)"
        except Exception:
            gpu_line = "unavailable"
        console.print(f"[bold]GPU:[/bold]      {gpu_line}")

        # cloud session info
        try:
            from nvh.integrations.cloud_session import detect_cloud_session, format_cloud_status
            cloud = detect_cloud_session()
            if cloud.is_cloud:
                console.print(f"  [bold green]Cloud:[/bold green]     {format_cloud_status(cloud)}")
        except Exception:
            pass

        # Local models from Ollama
        try:
            import httpx
            resp = httpx.get("http://localhost:11434/api/tags", timeout=3)
            if resp.status_code == 200:
                ollama_models = [m.get("name", "") for m in resp.json().get("models", [])]
                if ollama_models:
                    loaded_names = ", ".join(f"{m} (loaded)" for m in ollama_models[:3])
                    if len(ollama_models) > 3:
                        loaded_names += f" +{len(ollama_models) - 3} more"
                    models_line = loaded_names
                else:
                    models_line = "none loaded (run: ollama pull llama3.1)"
            else:
                models_line = "Ollama not reachable"
        except Exception:
            models_line = "Ollama not reachable"
        console.print(f"[bold]Models:[/bold]   {models_line}")

        # Advisor health
        advisor_parts = []
        for pname in enabled_providers:
            try:
                provider = engine.registry.get(pname)
                health = await provider.health_check()
                mark = "[green]✓[/green]" if health.healthy else "[red]✗[/red]"
                advisor_parts.append(f"{pname} {mark}")
            except Exception:
                advisor_parts.append(f"{pname} [red]✗[/red]")

        if advisor_parts:
            advisors_line = f"{len(enabled_providers)} online — {', '.join(advisor_parts)}"
        else:
            advisors_line = "none configured (run: nvh config init)"
        console.print(f"[bold]Advisors:[/bold] {advisors_line}")

        # Budget
        try:
            budget_data = await engine.get_budget_status()
            daily_spend = budget_data["daily_spend"]
            daily_limit = budget_data["daily_limit"]
            monthly_spend = budget_data["monthly_spend"]
            monthly_limit = budget_data["monthly_limit"]

            daily_str = f"${daily_spend:.2f} / ${daily_limit:.2f} daily" if daily_limit > 0 else f"${daily_spend:.2f} spent today"
            monthly_str = f"${monthly_spend:.2f} / ${monthly_limit:.2f} monthly" if monthly_limit > 0 else f"${monthly_spend:.2f} spent this month"
            console.print(f"[bold]Budget:[/bold]   {daily_str} | {monthly_str}")

            # Savings: queries handled by local (Ollama) vs cloud
            local_queries = budget_data.get("local_queries", 0)
            monthly_queries = budget_data.get("monthly_queries", 0)
            if monthly_queries > 0 and local_queries > 0:
                # Rough savings estimate: average cloud query cost * local query count
                avg_cloud_cost = float(monthly_spend) / max(monthly_queries - local_queries, 1) if monthly_queries > local_queries else 0.002
                saved_usd = avg_cloud_cost * local_queries
                console.print(f"[bold]Savings:[/bold]  ${saved_usd:.2f} saved this month ({local_queries} local queries)")
        except Exception:
            console.print("[bold]Budget:[/bold]   unavailable")

        # Default mode
        default_mode = getattr(config.defaults, "mode", "ask")
        console.print(f"[bold]Mode:[/bold]     {default_mode} (default) — change with: nvh config set defaults.mode convene")

        console.print(Rule(style="dim"))

    _run(_run_status())


# ---------------------------------------------------------------------------
# nvh quick
# ---------------------------------------------------------------------------

@app.command()
def quick(
    prompt: str = typer.Argument(..., help="Question to answer quickly"),
):
    """Quick answer from the fastest/cheapest advisor. No frills.

    Routes to the cheapest available advisor: Groq > DeepSeek > Ollama > cheapest cloud.
    Outputs just the answer with no metadata — for when you need a fast answer.
    """
    async def _run_quick():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config()
        engine = Engine(config=config)
        enabled = await engine.initialize()

        # Priority order: groq (fastest free tier) > deepseek (cheapest cloud) > ollama (local free)
        # then fall back to cheapest available
        cheap_priority = ["groq", "deepseek", "ollama"]
        chosen_provider = None
        for pname in cheap_priority:
            if pname in enabled:
                chosen_provider = pname
                break

        if chosen_provider is None:
            # Fall back to cheapest routing strategy
            chosen_provider = None  # let the router decide with "cheapest" strategy

        try:
            resp = await engine.query(
                prompt=prompt,
                provider=chosen_provider,
                stream=False,
                strategy="cheapest",
                use_cache=True,
            )
            # Raw output — no metadata
            print(resp.content, end="")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
            raise typer.Exit(1)

    _run(_run_quick())


# ---------------------------------------------------------------------------
# nvh safe — local-only private mode
# ---------------------------------------------------------------------------

@app.command()
def safe(
    prompt: str = typer.Argument(..., help="Question to answer privately using local models only"),
    model: str | None = typer.Option(None, "-m", "--model", help="Local model to use"),
    raw: bool = typer.Option(False, "--raw", help="Output just the answer"),
):
    """Private mode — your data never leaves your machine.

    Routes ONLY to local Ollama models. No cloud APIs are called.
    No data is logged, cached, or stored. No data is used to train other AI models.

    Perfect for: confidential documents, salary data, medical info, legal matters,
    proprietary code, personal questions.

    Examples:
        nvh safe "Analyze my salary negotiation strategy"
        nvh safe "Review this NDA" -f contract.pdf
        nvh safe "Debug this proprietary algorithm"
    """
    async def _run_safe():
        from nvh.config.settings import load_config
        from nvh.core.engine import Engine

        config = load_config()
        engine = Engine(config=config)
        await engine.initialize()

        if not engine.registry.has("ollama"):
            console.print("[red]Safe mode requires Ollama (local AI).[/red]")
            console.print("Set up: nvh ollama")
            raise typer.Exit(1)

        console.print("[dim][safe mode — local only, no data leaves your machine][/dim]\n")

        try:
            resp = await engine.query(
                prompt=prompt,
                provider="ollama",
                model=model,
                stream=False,
                privacy=True,  # no logging, no caching, no persistence
            )
            if raw:
                print(resp.content, end="")
            else:
                console.print(resp.content)
                console.print(f"\n[dim]{resp.model} (local) | {resp.usage.total_tokens} tokens | FREE | {resp.latency_ms}ms | [green]no data transmitted[/green][/dim]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
            raise typer.Exit(1)

    _run(_run_safe())


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
7. Local AI processing (nvh safe) keeps all data on your device
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
    ("google", "Google Gemini", "https://aistudio.google.com/apikey", "Google account, 15 RPM free"),
    ("github", "GitHub Models", "https://github.com/settings/tokens", "GitHub account, GPT-4o free"),
    ("nvidia", "NVIDIA NIM", "https://build.nvidia.com/", "NVIDIA Dev account, 1000 credits"),
    ("mistral", "Mistral", "https://console.mistral.ai/api-keys", "Phone verify, 2 RPM free"),
]


@app.command()
def setup(
    email: str | None = typer.Option(None, "--email", "-e", help="Your email for provider signups"),
    all_providers: bool = typer.Option(False, "--all", help="Set up ALL free providers (opens many browser tabs)"),
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
        console.print(Panel(EULA_TEXT, title="[bold]NVHive — Terms of Use[/bold]", border_style="green"))
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
    console.print("[bold green]Step 1/3: Zero-signup providers[/bold green] (enabled immediately)\n")
    for name, display, desc in ZERO_SIGNUP:
        if name == "ollama":
            try:
                import httpx
                resp = httpx.get("http://localhost:11434/api/tags", timeout=3)
                if resp.status_code == 200:
                    models = resp.json().get("models", [])
                    console.print(f"  [green]✓[/green] {display} — {desc} ({len(models)} models ready)")
                else:
                    console.print(f"  [yellow]![/yellow] {display} — not running. Start with: ollama serve")
            except Exception:
                console.print(f"  [yellow]![/yellow] {display} — not detected. Install: curl -fsSL https://ollama.com/install.sh | sh")
        else:
            console.print(f"  [green]✓[/green] {display} — {desc}")

    console.print()

    # Step 4: Email-signup providers
    console.print("[bold green]Step 2/3: Email-signup providers[/bold green] (free, just need a key)\n")

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
            import os
            env_names = [f"{name.upper()}_API_KEY", f"HIVE_{name.upper()}_API_KEY"]
            for env_name in env_names:
                if os.environ.get(env_name):
                    has_key = True
                    break

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

        key = typer.prompt(f"  Paste your {display} API key (or Enter to skip)", default="", hide_input=True)
        if key:
            try:
                import keyring
                keyring.set_password("nvhive", f"{name}_api_key", key)
                console.print(f"  [green]✓ {display} configured![/green]")
                configured += 1
            except Exception:
                console.print(f"  [yellow]Keychain unavailable. Set {name.upper()}_API_KEY in your environment.[/yellow]")
        else:
            console.print(f"  [dim]Skipped {display}[/dim]")
            skipped += 1

    # Step 5: Account-signup providers (if not already covered by --all)
    if not all_providers and ACCOUNT_SIGNUP:
        console.print("\n[bold green]Step 3/3: Account-based providers[/bold green] (need existing account)\n")
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

    # Summary
    total_free = len(ZERO_SIGNUP) + configured
    console.print("\n[bold green]Setup complete![/bold green]")
    console.print(f"  {total_free} free advisors ready, {skipped} skipped")
    console.print("\n  Try it: [bold]nvh \"What is the meaning of life?\"[/bold]")
    console.print("  Or just: [bold]nvh[/bold] (launches interactive chat)")
    if skipped > 0:
        console.print("  Set up more later: [bold]nvh setup --all[/bold]")
    console.print()


# ---------------------------------------------------------------------------
# nvh conversation
# ---------------------------------------------------------------------------

from datetime import UTC  # noqa: E402

from nvh.cli.conversations import conversation_app  # noqa: E402

app.add_typer(conversation_app, name="conversation")


# ---------------------------------------------------------------------------
# hive config
# ---------------------------------------------------------------------------

config_app = typer.Typer(help="Manage configuration")
app.add_typer(config_app, name="config")


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
                    console.print("  [yellow]Keychain unavailable. Key will be read from env var.[/yellow]")
                providers_to_enable.append(name)

    # Check for Ollama
    console.print("\nChecking for local Ollama...")
    try:
        import httpx
        resp = httpx.get("http://localhost:11434/api/tags", timeout=3)
        if resp.status_code == 200:
            models = resp.json().get("models", [])
            console.print(f"  [green]Ollama detected! {len(models)} models available.[/green]")
            providers_to_enable.append("ollama")
    except Exception:
        console.print("  [dim]Ollama not detected (not running or not installed)[/dim]")

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
            f"  {name}:\n    enabled: true\n    api_key: ${{{name.upper()}_API_KEY}}\n    default_model:",
        )

    # Set default provider
    if providers_to_enable:
        default = providers_to_enable[0]
        config_content = config_content.replace('  provider: ""', f'  provider: {default}')

    DEFAULT_CONFIG_PATH.write_text(config_content)
    console.print(f"\n[green]Config written to {DEFAULT_CONFIG_PATH}[/green]")
    console.print(f"Default advisor: [bold]{providers_to_enable[0] if providers_to_enable else 'none'}[/bold]")
    console.print("\nRun [bold]hive ask \"Hello, world!\"[/bold] to test your setup!")


@config_app.command("get")
def config_get(key: str = typer.Argument(..., help="Config key (dot notation, e.g. defaults.provider)")):
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
    output: str | None = typer.Option(None, "--output", "-o", help="Output file path (default: stdout)"),
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


# ---------------------------------------------------------------------------
# hive advisor
# ---------------------------------------------------------------------------

advisor_app = typer.Typer(help="Manage LLM advisors")
app.add_typer(advisor_app, name="advisor")


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

    for name, pconfig in config.providers.items():
        # Check for key
        has_key = bool(pconfig.api_key and not pconfig.api_key.startswith("${"))
        if not has_key:
            import os
            has_key = bool(os.environ.get(f"{name.upper()}_API_KEY") or os.environ.get(f"HIVE_{name.upper()}_API_KEY"))
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
    console.print(f"Cost tier: {profile.cost_tier} | Free tier: {'Yes' if profile.has_free_tier else 'No'}")
    if profile.free_tier_limits:
        console.print(f"[green]{profile.free_tier_limits}[/green]")

    # Scores
    console.print(f"\n[dim]Quality: {profile.quality_weight:.0%} | Speed: {profile.speed_weight:.0%} | "
                 f"Cost efficiency: {profile.cost_weight:.0%} | Reliability: {profile.reliability_weight:.0%}[/dim]")

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
        console.print(f"[yellow]Keychain unavailable. Set {name.upper()}_API_KEY environment variable instead.[/yellow]")


@advisor_app.command("remove")
def advisor_remove(name: str = typer.Argument(..., help="Advisor name")):
    """Remove an advisor's API key."""
    try:
        import keyring
        keyring.delete_password("nvhive", f"{name}_api_key")
        console.print(f"[green]API key for {name} removed.[/green]")
    except Exception as e:
        console.print(f"[yellow]Could not remove key: {e}[/yellow]")


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
    urls = {
        "openai": "https://platform.openai.com/api-keys",
        "anthropic": "https://console.anthropic.com/settings/keys",
        "google": "https://aistudio.google.com/apikey",
        "mistral": "https://console.mistral.ai/api-keys",
        "cohere": "https://dashboard.cohere.com/api-keys",
        "groq": "https://console.groq.com/keys",
        "huggingface": "https://huggingface.co/settings/tokens",
    }

    if name == "ollama":
        console.print("Ollama doesn't require authentication. Checking connectivity...")
        try:
            import httpx
            resp = httpx.get("http://localhost:11434/api/tags", timeout=3)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                console.print(f"[green]✓ Ollama detected! {len(models)} models available.[/green]")
            else:
                console.print("[red]Ollama returned an error.[/red]")
        except Exception:
            console.print("[red]Ollama not reachable at localhost:11434.[/red]")
        return

    if name in ("google", "aws", "azure"):
        # Check for cloud CLI tools
        import shutil
        cli_tools = {"google": "gcloud", "aws": "aws", "azure": "az"}
        tool = cli_tools.get(name)
        if tool and shutil.which(tool):
            console.print(f"[green]Detected {tool} CLI. You can authenticate via: [bold]{tool} auth login[/bold][/green]")
            console.print("Or paste an API key manually below.")

    url = urls.get(name, "")
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
app.add_typer(budget_app, name="budget")


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
        monthly_limit = f"${status['monthly_limit']:.2f}" if status['monthly_limit'] > 0 else "unlimited"

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


@app.command()
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
            lines.append("Run queries with a local model (Ollama, LM Studio, etc.) to start saving.")
        else:
            lines.append(f"[bold green]Money saved this month:[/bold green]       [bold]${saved:.2f}[/bold]")
            lines.append(f"[dim]If you'd used cloud for everything:   ${hypothetical:.2f}[/dim]")
            lines.append(f"[dim]Your actual cloud spend:              ${actual_spend:.2f}[/dim]")
            lines.append(f"[bold]Savings percentage:[/bold]               [bold cyan]{pct:.1f}%[/bold cyan]")
            lines.append("")
            if pct >= 80:
                lines.append("[bold green]Outstanding.[/bold green] You're running almost everything locally. "
                             "Every dollar counts — keep it up!")
            elif pct >= 50:
                lines.append("[green]Great work.[/green] Over half your queries run free on local hardware. "
                             "You're making your budget go further.")
            elif pct >= 20:
                lines.append("[yellow]Good start.[/yellow] You're saving real money. "
                             "Try routing more queries to local models to stretch your budget further.")
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

@app.command("plugins")
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

@app.command()
def bench(
    model: str | None = typer.Option(None, "-m", "--model", help="Model to benchmark (default: current local model)"),
    quick_mode: bool = typer.Option(False, "--quick", help="Run only 2 tests instead of 4"),
    all_models: bool = typer.Option(False, "--all", help="Benchmark all loaded local models"),
):
    """Benchmark your GPU — measure AI inference speed (tokens/second).

    Like 3DMark but for AI. See how fast your GPU can generate text
    and compare with community averages.

    Examples:
        nvh bench                    # benchmark default local model
        nvh bench -m nemotron-small  # benchmark specific model
        nvh bench --quick            # fast 2-test benchmark
        nvh bench --all              # benchmark all local models
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
            resp = httpx.get("http://localhost:11434/api/tags", timeout=5)
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
            # Normalise — allow short names (e.g. "nemotron-small" matches "nemotron-small:latest")
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
                console.print(f"Community average for {baseline_label}: ~{baseline_tps} tok/s [dim](7B Q4_K_M)[/dim]")
                # Note if the model is larger than the baseline 7B
                model_short = bench_model.split(":")[0]
                is_larger = any(x in model_short for x in ["70b", "34b", "22b", "13b", "small", "medium", "large"])
                size_note = f" ({model_short} is larger than the baseline 7B model)" if is_larger else ""
                console.print(f"Your result: [bold cyan]{avg_tps:.1f} tok/s[/bold cyan]{size_note}")
                console.print()

                # Star rating: ratio of result vs baseline (adjusted: larger models expected to be slower)
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
                console.print(f"[bold cyan]Result: {avg_tps:.1f} tok/s[/bold cyan] [dim](no community baseline for this GPU)[/dim]")

            console.print()

    _run(_run_bench())


# ---------------------------------------------------------------------------
# hive model
# ---------------------------------------------------------------------------

model_app = typer.Typer(help="Browse available models")
app.add_typer(model_app, name="model")


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

agent_app = typer.Typer(help="Manage auto-generated agent personas")
app.add_typer(agent_app, name="agent")


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

@app.command()
def repl(
    provider: str | None = typer.Option(None, "-p", "--advisor", help="Starting advisor"),
    model: str | None = typer.Option(None, "-m", "--model", help="Starting model"),
    council_mode: bool = typer.Option(False, "--convene", help="Start in hive mode"),
    auto_agents: bool = typer.Option(False, "-a", "--auto-agents", help="Enable auto-agent generation"),
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
app.add_typer(webhook_app, name="webhook")


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
        console.print(f"[yellow]Webhook with URL '{url}' already exists. Updating events/secret.[/yellow]")
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
app.add_typer(auth_app, name="auth")


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
            user = await create_user(username=username, password=password, role=effective_role, email=email)
        except ValueError as exc:
            console.print(f"[red]Error: {exc}[/red]")
            raise typer.Exit(1)

        console.print(f"[green]User created:[/green] {user.username} (role: {user.role}, id: {user.id})")
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

        console.print(f"\n[green]API token created:[/green] {token_record.name} (id: {token_record.id})")
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

@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address"),
    port: int = typer.Option(8000, "--port", help="Port number"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (dev mode)"),
    daemon: bool = typer.Option(False, "--daemon", help="Install as system service (auto-start on boot)"),
):
    """Start the REST API server.

    Use --daemon to install as a persistent service that starts on boot.
    """
    if daemon:
        import sys as _sys

        from nvh.integrations.service import install_launchd_service, install_systemd_service
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

    from nvh.api.server import run_server
    from nvh.integrations.hostname import is_hostname_configured
    console.print(f"[bold]Hive API Server[/bold] starting on http://{host}:{port}")
    if is_hostname_configured():
        console.print(f"  Also available at: http://nvhive:{port}")
    console.print(f"API docs: http://{host}:{port}/docs")
    console.print("  Tip: run [bold]nvh hostname[/bold] to enable http://nvhive")
    console.print()
    run_server(host=host, port=port, reload=reload)


@app.command()
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
    from nvh.integrations.service import service_status, uninstall_service

    if action == "status":
        running, msg = service_status()
        if running:
            console.print(f"  [green]✓[/green] nvHive proxy service: [bold green]{msg}[/bold green]")
        else:
            console.print(f"  [yellow]○[/yellow] nvHive proxy service: [bold]{msg}[/bold]")
            if msg == "Not installed":
                console.print("  Install with: [bold]nvh serve --daemon[/bold]")

    elif action == "stop":
        import subprocess
        import sys as _sys
        if _sys.platform == "darwin":
            subprocess.run(["launchctl", "unload",
                           str(Path.home() / "Library" / "LaunchAgents" / "com.nvhive.proxy.plist")],
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
# hive hostname — local DNS setup
# ---------------------------------------------------------------------------

@app.command()
def hostname(
    remove: bool = typer.Option(False, "--remove", help="Remove nvhive hostname"),
):
    """Set up 'nvhive' as a local hostname for easy browser access.

    After setup, access the WebUI at http://nvhive:3000 and the API at
    http://nvhive:8000 instead of http://localhost.

    Examples:
        nvh hostname           Add nvhive to /etc/hosts
        nvh hostname --remove  Remove it
    """
    from nvh.integrations.hostname import (
        add_hostname,
        is_hostname_configured,
        remove_hostname,
    )

    if remove:
        ok, msg = remove_hostname()
        console.print(f"  {'[green]✓[/green]' if ok else '[yellow]![/yellow]'} {msg}")
        return

    if is_hostname_configured():
        console.print("  [green]✓[/green] [bold]nvhive[/bold] hostname is already configured")
        console.print("  WebUI: http://nvhive:3000")
        console.print("  API:   http://nvhive:8000")
        console.print()
        console.print("  [dim]Want http://nvhive with no port? Run:[/dim]")
        console.print("  [dim]  sudo nvh webui --port 80[/dim]")
        return

    ok, msg = add_hostname()
    if ok:
        console.print(f"  [green]✓[/green] {msg}")
    else:
        console.print(f"  [yellow]![/yellow] {msg}")
    console.print()
    console.print("  [dim]Want http://nvhive with no port? Run:[/dim]")
    console.print("  [dim]  sudo nvh webui --port 80[/dim]")


# ---------------------------------------------------------------------------
# hive integrate — auto-detect and configure all platforms
# ---------------------------------------------------------------------------

@app.command()
def integrate(
    auto: bool = typer.Option(False, "--auto", "-y", help="Auto-configure all detected platforms without prompting"),
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

    from nvh.integrations.detector import (
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
        integ_table.add_row("NemoClaw", "pip install nemoclaw", "nvh nemoclaw")
        integ_table.add_row("OpenClaw", "pip install openclaw", "nvh openclaw")
        integ_table.add_row("Claude Code", "npm i -g @anthropic/claude-code", "nvh integrate --auto")
        integ_table.add_row("Cursor", "https://cursor.com", "nvh integrate --auto")
        console.print(integ_table)
        console.print()
        console.print("  After installing, run [bold]nvh integrate[/bold] again to auto-configure.")
        console.print()
        return

    for p in detected:
        status = "[green]✓ configured[/green]" if p.already_configured else "[yellow]○ not configured[/yellow]"
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
        mcp_platforms = [p for p in to_configure if p.integration_type == "mcp" and not p.already_configured]
        proxy_platforms = [p for p in to_configure if p.integration_type == "inference" and not p.already_configured]

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

@app.command()
def openclaw(
    test: bool = typer.Option(False, "--test", help="Test if the MCP server is reachable"),
    start: bool = typer.Option(False, "--start", help="Start the MCP server for OpenClaw"),
    config: bool = typer.Option(False, "--config", help="Generate openclaw.json config file"),
    output: str | None = typer.Option(None, "-o", "--output", help="Output path for openclaw.json"),
    http: bool = typer.Option(False, "--http", help="Use HTTP transport instead of stdio"),
    port: int = typer.Option(8080, "--port", help="Port for HTTP transport"),
):
    """OpenClaw integration — use NVHive tools in any OpenClaw agent.

    Registers nvHive's multi-LLM tools (ask, council, throwdown, etc.)
    as an MCP server that any OpenClaw agent can call.

    Examples:
        nvh openclaw              Show setup instructions
        nvh openclaw --test       Test MCP server connectivity
        nvh openclaw --start      Start the MCP server
        nvh openclaw --config     Generate openclaw.json
    """
    from rich.rule import Rule

    console.print()
    console.print(Panel(
        "[bold green]NVHive ↔ OpenClaw Integration[/bold green]\n"
        "Give any OpenClaw agent access to nvHive's multi-LLM tools:\n"
        "smart routing, council consensus, and throwdown analysis.",
        border_style="green",
    ))

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
                console.print("  [green]✓[/green] MCP HTTP server is [bold green]reachable[/bold green]")
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
                console.print("  [green]✓[/green] MCP server module loads [bold green]OK[/bold green]")
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

        from nvh.integrations.openclaw import write_openclaw_config
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

    tool_table.add_row("ask", "Smart-routed LLM query", "Ask any question across 22 providers")
    tool_table.add_row("ask_safe", "Local-only query", "Privacy-sensitive queries via Ollama")
    tool_table.add_row("council", "Multi-model consensus", "Get 3-5 LLMs to debate and synthesize")
    tool_table.add_row("throwdown", "Two-pass deep analysis", "Complex questions with critique loop")
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
    console.print("        │  Ollama  │ │   Groq   │ │Anthropic │ ...22 providers")
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
    console.print("  [bold]nvh openclaw --test[/bold]     Test MCP server availability")
    console.print("  [bold]nvh openclaw --start[/bold]    Start MCP server manually")
    console.print("  [bold]nvh openclaw --config[/bold]   Generate openclaw.json")
    console.print("  [bold]nvh openclaw --http[/bold]     Use HTTP transport (with --start or --test)")
    console.print()


# ---------------------------------------------------------------------------
# hive mcp — MCP server for Claude Code, Cursor, OpenClaw
# ---------------------------------------------------------------------------

@app.command()
def mcp(
    transport: str = typer.Option("stdio", "--transport", "-t", help="Transport: stdio or streamable-http"),
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
    """
    try:
        from nvh.mcp_server import create_server
    except ImportError:
        console.print("[red]MCP SDK not installed.[/red]")
        console.print('Install with: [bold]pip install "nvhive[mcp]"[/bold]')
        console.print('  or: [bold]pip install "mcp[cli]"[/bold]')
        raise typer.Exit(1)

    server = create_server()

    if transport == "stdio":
        console.print("[bold]NVHive MCP Server[/bold] starting (stdio transport)")
        console.print("Register with Claude Code:")
        console.print("  [dim]$[/dim] claude mcp add nvhive nvh mcp")
        console.print()
        server.run(transport="stdio")
    elif transport in ("streamable-http", "http", "sse"):
        console.print(f"[bold]NVHive MCP Server[/bold] starting on port {port} (HTTP transport)")
        console.print(f"Connect clients to: http://localhost:{port}/mcp")
        console.print()
        server.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        console.print(f"[red]Unknown transport: {transport}[/red]")
        console.print("Use: stdio, streamable-http")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# hive nemoclaw — NemoClaw integration setup
# ---------------------------------------------------------------------------

@app.command()
def nemoclaw(
    host: str = typer.Option("127.0.0.1", "--host", help="NVHive proxy bind address"),
    port: int = typer.Option(8000, "--port", help="NVHive proxy port"),
    test: bool = typer.Option(False, "--test", help="Test connectivity to a running nvHive proxy"),
    start: bool = typer.Option(False, "--start", help="Start the proxy server for NemoClaw"),
):
    """NemoClaw integration — use NVHive as your NemoClaw inference provider.

    Registers NVHive's smart router, council consensus, and throwdown analysis
    as an OpenAI-compatible inference endpoint inside NemoClaw's OpenShell sandbox.

    Examples:
        nvh nemoclaw              Show setup instructions
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
                console.print(f"  [green]✓[/green] Engine initialized: {data.get('engine_initialized', '?')}")
                console.print(f"  [green]✓[/green] Providers enabled: {data.get('providers_enabled', '?')}")
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
        from nvh.api.server import run_server
        run_server(host=host, port=port, reload=False)
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

    model_table.add_row("auto", "Smart routing", "Best available provider based on query type, cost, and speed")
    model_table.add_row("safe", "Local only", "Routes to Ollama — nothing leaves your machine")
    model_table.add_row("council", "Consensus", "3-model council with synthesis (default)")
    model_table.add_row("council:N", "Consensus", "N-model council (2-10 members)")
    model_table.add_row("throwdown", "Deep analysis", "Two-pass analysis with critique and refinement")
    model_table.add_row("<model-id>", "Direct", "Route to a specific model (gpt-4o, claude-sonnet-4, etc.)")

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
    console.print("        │  Ollama  │ │   Groq   │ │Anthropic │ ...22 providers")
    console.print("        │ Nemotron │ │          │ │          │")
    console.print("        └──────────┘ └──────────┘ └──────────┘")
    console.print()

    console.print(Rule("Commands"))
    console.print()
    console.print("  [bold]nvh nemoclaw[/bold]           Show this setup guide")
    console.print("  [bold]nvh nemoclaw --test[/bold]    Test proxy connectivity")
    console.print("  [bold]nvh nemoclaw --start[/bold]   Start the proxy server")
    console.print()


def _print_openshell_commands(host: str, port: int):
    """Print the openshell provider create command."""
    # Use host.openshell.internal for sandbox-to-host communication
    endpoint_host = "host.openshell.internal" if host in ("127.0.0.1", "0.0.0.0", "localhost") else host
    console.print("  [dim]$[/dim] openshell provider create \\")
    console.print("      --name nvhive \\")
    console.print("      --type openai \\")
    console.print("      --credential OPENAI_API_KEY=nvhive \\")
    console.print(f"      --config OPENAI_BASE_URL=http://{endpoint_host}:{port}/v1/proxy")


# ---------------------------------------------------------------------------
# hive version
# ---------------------------------------------------------------------------

@app.command()
def version():
    """Show NVHive version."""
    console.print(f"NVHive v{__version__}")


@app.command()
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
        ("GitHub Models", "https://github.com/settings/tokens", "Free GPT-4o — just need a GitHub account", "github"),
        ("Google Gemini", "https://aistudio.google.com/apikey", "15 req/min free — 1M token context", "google"),
        ("Cerebras", "https://cloud.cerebras.ai/", "30 req/min free — wafer-scale speed", "cerebras"),
        ("NVIDIA NIM", "https://build.nvidia.com/", "1000 free credits — 100+ models", "nvidia"),
        ("SiliconFlow", "https://cloud.siliconflow.cn/", "1000 req/min free — best rate limits", "siliconflow"),
        ("Fireworks AI", "https://fireworks.ai/", "10 req/min free", "fireworks"),
        ("Mistral", "https://console.mistral.ai/api-keys", "2 req/min free — multilingual", "mistral"),
        ("SambaNova", "https://cloud.sambanova.ai/", "200K tokens/day free — Llama 405B", "sambanova"),
        ("Hugging Face", "https://huggingface.co/settings/tokens", "Free inference API", "huggingface"),
        ("AI21 Labs", "https://studio.ai21.com/", "$10 free credit — 256K context", "ai21"),
        ("Cohere", "https://dashboard.cohere.com/api-keys", "1K calls/month free — RAG specialist", "cohere"),
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


@app.command()
def update():
    """Update NVHive to the latest version from GitHub."""
    import os
    import subprocess

    nvh_home = os.environ.get("NVH_HOME", os.path.expanduser("~/nvh"))
    repo_dir = os.path.join(nvh_home, "repo")

    if os.path.isdir(os.path.join(repo_dir, ".git")):
        console.print("[bold]Updating NVHive...[/bold]")
        try:
            subprocess.run(["git", "-C", repo_dir, "pull", "--quiet"], check=True)
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "-e", repo_dir],
                check=True,
            )
            console.print("[green]Updated to latest version.[/green]")
        except Exception as e:
            console.print(f"[red]Update failed: {e}[/red]")
    else:
        console.print("[dim]Not installed from git. Reinstall with:[/dim]")
        console.print("curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install.sh | bash")


# ---------------------------------------------------------------------------
# nvh webui — install and launch the web interface
# ---------------------------------------------------------------------------

@app.command()
def webui(
    install_only: bool = typer.Option(False, "--install", help="Install without launching"),
    port: int = typer.Option(3000, "--port", help="Port for the web UI"),
):
    """Install and launch the nvHive web UI.

    The web UI is optional — nvHive works fully from the CLI.
    This command installs Node.js dependencies and starts the Next.js dev server.

    First run installs dependencies (~30 seconds).
    Subsequent runs start instantly.

    Examples:
        nvh webui              # install (if needed) and launch on port 3000
        nvh webui --install    # install dependencies only
        nvh webui --port 8080  # launch on a different port
    """
    import shutil
    import subprocess

    # Find the web directory
    web_dir = None
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "web"),
        os.path.expanduser("~/nvh/repo/web"),
        os.path.join(os.getcwd(), "web"),
    ]
    for candidate in candidates:
        if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "package.json")):
            web_dir = candidate
            break

    if not web_dir:
        console.print("[red]Web UI not found.[/red]")
        console.print("Make sure you installed from source (git clone), not just pip.")
        console.print("[dim]The web/ directory should be in the repo root.[/dim]")
        raise typer.Exit(1)

    # Check for Node.js
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        console.print("[red]Node.js not found.[/red]")
        console.print("Install Node.js 18+:")
        if sys.platform == "darwin":
            console.print("  brew install node")
        elif sys.platform == "win32":
            console.print("  winget install OpenJS.NodeJS")
        else:
            console.print("  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -")
            console.print("  sudo apt install -y nodejs")
        raise typer.Exit(1)

    # Install dependencies if needed
    node_modules = os.path.join(web_dir, "node_modules")
    if not os.path.isdir(node_modules):
        console.print("[bold]Installing web UI dependencies...[/bold]")
        result = subprocess.run(
            ["npm", "ci"],
            cwd=web_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # Try npm install as fallback
            result = subprocess.run(["npm", "install"], cwd=web_dir)
        if result.returncode != 0:
            console.print("[red]npm install failed.[/red]")
            raise typer.Exit(1)
        console.print("[green]Dependencies installed.[/green]")

    if install_only:
        console.print("[green]Web UI ready. Run 'nvh webui' to launch.[/green]")
        return

    # Smart port selection — check for conflicts
    import socket

    def _port_in_use(p: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", p)) == 0

    chosen_port = port
    if _port_in_use(port):
        if port == 3000:
            # Default port occupied — try alternatives
            for fallback in [3001, 3002, 8080, 8081]:
                if not _port_in_use(fallback):
                    console.print(
                        f"[yellow]![/yellow] Port {port} is in use. "
                        f"Switching to port {fallback}."
                    )
                    chosen_port = fallback
                    break
            else:
                console.print(f"[red]Port {port} and fallbacks are all in use.[/red]")
                console.print("Specify a free port: [bold]nvh webui --port 9000[/bold]")
                raise typer.Exit(1)
        else:
            console.print(f"[red]Port {port} is already in use.[/red]")
            console.print("Choose a different port: [bold]nvh webui --port 3001[/bold]")
            raise typer.Exit(1)

    # Check hostname setup
    from nvh.integrations.hostname import is_hostname_configured
    host_label = "nvhive" if is_hostname_configured() else "localhost"

    console.print(f"[bold]Starting nvHive Web UI on port {chosen_port}...[/bold]")
    console.print(f"  WebUI: http://{host_label}:{chosen_port}")
    console.print("  [dim]API server must be running: nvh serve (in another terminal)[/dim]")
    console.print("  [dim]Press Ctrl+C to stop[/dim]")
    console.print()

    try:
        subprocess.run(
            ["npm", "run", "dev", "--", "-p", str(chosen_port)],
            cwd=web_dir,
        )
    except KeyboardInterrupt:
        console.print("\n[dim]Web UI stopped.[/dim]")


# ---------------------------------------------------------------------------
# nvh debug — full diagnostic dump for troubleshooting
# ---------------------------------------------------------------------------

@app.command()
def debug(
    output: str | None = typer.Option(None, "-o", "--output", help="Save to file instead of printing"),
    send: bool = typer.Option(False, "--send", help="Copy to clipboard for sharing"),
    nvidia_report: bool = typer.Option(False, "--nvidia-report", help="Also run nvidia-bug-report.sh for NVIDIA support"),
):
    """Full diagnostic dump — captures everything needed to troubleshoot issues.

    Collects: system info, GPU, Python, config, advisors, Ollama, cloud session status,
    disk, memory, network, recent errors, and a test query result.

    Use --nvidia-report to also generate NVIDIA's official bug report
    (runs nvidia-bug-report.sh and packages the output).

    Share the output when reporting issues.

    Examples:
        nvh debug                    # print to terminal
        nvh debug -o debug.txt       # save to file
        nvh debug --send             # copy to clipboard
        nvh debug --nvidia-report    # include NVIDIA driver/GPU diagnostics
    """
    import os
    import platform
    import subprocess
    import sys
    from datetime import datetime

    lines: list[str] = []

    def log(text: str = ""):
        lines.append(text)
        if not output and not send:
            console.print(text)

    log(f"NVHive Debug Report — {datetime.now(UTC).isoformat()}")
    log("=" * 60)

    # --- System ---
    log("\n[SYSTEM]")
    log(f"  Platform:    {platform.platform()}")
    log(f"  Python:      {sys.version}")
    log(f"  Executable:  {sys.executable}")
    log(f"  NVHive:      v{__version__}")
    log(f"  CWD:         {os.getcwd()}")
    log(f"  HOME:        {os.path.expanduser('~')}")
    log(f"  User:        {os.environ.get('USER', 'unknown')}")
    log(f"  Shell:       {os.environ.get('SHELL', 'unknown')}")

    # --- Environment ---
    log("\n[ENVIRONMENT]")
    try:
        from nvh.utils.environment import detect_environment
        env = detect_environment()
        log(f"  Docker:      {env.is_docker}")
        log(f"  Cloud:       {env.is_cloud} ({env.cloud_provider})")
        log(f"  Has root:    {env.has_root}")
        log(f"  GPU access:  {env.gpu_accessible}")
    except Exception as e:
        log(f"  Error: {e}")

    # --- Cloud Session ---
    log("\n[CLOUD SESSION]")
    try:
        from nvh.integrations.cloud_session import detect_cloud_session
        cloud = detect_cloud_session()
        log(f"  Detected:    {cloud.is_cloud}")
        if cloud.is_cloud:
            log(f"  Tier:        {cloud.tier}")
            log(f"  GPU class:   {cloud.gpu_class}")
            log(f"  Session ID:  {cloud.session_id[:12]}..." if cloud.session_id else "  Session ID:  none")
            log(f"  Storage:     {cloud.persistent_storage}")
    except Exception as e:
        log(f"  Error: {e}")

    # --- GPU ---
    log("\n[GPU]")
    try:
        from nvh.utils.gpu import detect_gpus, detect_system_memory, get_ollama_optimizations
        gpus = detect_gpus()
        if gpus:
            for g in gpus:
                log(f"  GPU {g.index}: {g.name}")
                log(f"    VRAM:      {g.vram_gb:.1f} GB total, {g.memory_free_mb} MB free")
                log(f"    Util:      {g.utilization_pct}%")
                log(f"    Driver:    {g.driver_version}")
                log(f"    CUDA:      {g.cuda_version}")
            opts = get_ollama_optimizations(gpus)
            log(f"  Architecture: {opts.architecture} (CC {opts.compute_capability})")
            log(f"  Flash Attn:  {opts.flash_attention}")
            log(f"  Rec. quant:  {opts.recommended_quant}")
            log(f"  Rec. ctx:    {opts.recommended_ctx}")
        else:
            log("  No NVIDIA GPU detected")

        mem = detect_system_memory()
        log(f"\n  System RAM:  {mem.total_ram_gb} GB total, {mem.available_ram_gb} GB free")
    except Exception as e:
        log(f"  Error: {e}")

    # --- Drivers & Dependencies ---
    log("\n[DRIVERS & DEPENDENCIES]")
    try:
        # NVIDIA driver
        result = subprocess.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            log(f"  NVIDIA driver: {result.stdout.strip()}")
        else:
            log("  NVIDIA driver: NOT FOUND")

        # CUDA
        cuda_paths = ["/usr/local/cuda/version.txt", "/usr/local/cuda/bin/nvcc"]
        cuda_found = False
        for p in cuda_paths:
            if os.path.exists(p):
                if p.endswith("nvcc"):
                    r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=5)
                    ver = [ln for ln in r.stdout.splitlines() if "release" in ln.lower()]
                    log(f"  CUDA toolkit: {ver[0].strip() if ver else 'found'}")
                else:
                    log(f"  CUDA toolkit: {open(p).read().strip()}")
                cuda_found = True
                break
        if not cuda_found:
            log("  CUDA toolkit: not found (OK — Ollama bundles its own)")

        # Docker
        r = subprocess.run(["docker", "--version"], capture_output=True, text=True, timeout=5)
        log(f"  Docker:       {r.stdout.strip() if r.returncode == 0 else 'not found'}")

        # Docker GPU support
        if r.returncode == 0:
            r2 = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=10)
            if "nvidia" in r2.stdout.lower():
                log("  Docker GPU:   NVIDIA runtime detected")
            else:
                log("  Docker GPU:   NVIDIA runtime NOT found (install nvidia-container-toolkit)")

        # Git
        r = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=5)
        log(f"  Git:          {r.stdout.strip() if r.returncode == 0 else 'not found'}")

        # Ollama binary
        ollama_bin = os.path.expanduser("~/nvh/ollama")
        if os.path.exists(ollama_bin):
            log(f"  Ollama bin:   {ollama_bin} (local)")
        elif subprocess.run(["which", "ollama"], capture_output=True).returncode == 0:
            log("  Ollama bin:   system install")
        else:
            log("  Ollama bin:   not found")

        # Key Python packages
        import importlib
        for pkg, name in [("litellm", "LiteLLM"), ("fastapi", "FastAPI"), ("rich", "Rich"),
                          ("typer", "Typer"), ("pydantic", "Pydantic"), ("httpx", "httpx"),
                          ("keyring", "Keyring"), ("tiktoken", "tiktoken")]:
            try:
                mod = importlib.import_module(pkg)
                ver = getattr(mod, "__version__", "?")
                log(f"  {name:12s}  {ver}")
            except ImportError:
                log(f"  {name:12s}  MISSING — pip install {pkg}")

        # Audio tools (for voice)
        for tool in ["sox", "arecord", "espeak", "edge-tts"]:
            found = subprocess.run(["which", tool], capture_output=True).returncode == 0
            log(f"  {tool:12s}  {'found' if found else 'not found (optional, for nvh voice)'}")

        # Screenshot tools
        for tool in ["scrot", "gnome-screenshot", "import"]:
            found = subprocess.run(["which", tool], capture_output=True).returncode == 0
            log(f"  {tool:12s}  {'found' if found else 'not found (optional, for nvh screenshot)'}")

        # PDF tools (for RAG)
        for tool in ["pdftotext"]:
            found = subprocess.run(["which", tool], capture_output=True).returncode == 0
            log(f"  {tool:12s}  {'found' if found else 'not found (optional, for PDF ingestion)'}")

    except Exception as e:
        log(f"  Error: {e}")

    # --- Disk ---
    log("\n[DISK]")
    try:
        import shutil
        home = os.path.expanduser("~")
        usage = shutil.disk_usage(home)
        free_gb = usage.free / (1024**3)
        total_gb = usage.total / (1024**3)
        log(f"  Home dir:    {free_gb:.1f} GB free / {total_gb:.1f} GB total")
        nvh_dir = os.path.expanduser("~/nvh")
        if os.path.isdir(nvh_dir):
            nvh_size = sum(
                os.path.getsize(os.path.join(dp, f))
                for dp, _, fns in os.walk(nvh_dir) for f in fns
            ) / (1024**3)
            log(f"  ~/nvh size:  {nvh_size:.2f} GB")
        hive_dir = os.path.expanduser("~/.hive")
        if os.path.isdir(hive_dir):
            log("  ~/.hive:     exists")
    except Exception as e:
        log(f"  Error: {e}")

    # --- Config ---
    log("\n[CONFIG]")
    try:
        from nvh.config.settings import DEFAULT_CONFIG_PATH, load_config
        log(f"  Config path: {DEFAULT_CONFIG_PATH}")
        log(f"  Exists:      {DEFAULT_CONFIG_PATH.exists()}")
        config = load_config()
        log(f"  Default mode:    {config.defaults.mode}")
        log(f"  Default advisor: {config.defaults.provider or '(auto)'}")
        log(f"  Budget daily:    ${config.budget.daily_limit_usd}")
        log(f"  Budget monthly:  ${config.budget.monthly_limit_usd}")
        log(f"  Cache enabled:   {config.cache.enabled}")
        log(f"  Hooks:           {len(config.hooks)}")
        log(f"  Webhooks:        {len(config.webhooks)}")

        enabled_advisors = [n for n, p in config.providers.items() if p.enabled]
        disabled_advisors = [n for n, p in config.providers.items() if not p.enabled]
        log(f"  Advisors enabled:  {enabled_advisors}")
        log(f"  Advisors disabled: {disabled_advisors}")
    except Exception as e:
        log(f"  Error: {e}")

    # --- API Keys (masked) ---
    log("\n[API KEYS]")
    try:
        import keyring as kr
        for name in ["openai", "anthropic", "google", "groq", "github", "ollama",
                      "grok", "mistral", "cohere", "deepseek", "nvidia",
                      "cerebras", "sambanova", "huggingface", "ai21",
                      "perplexity", "together", "fireworks", "openrouter",
                      "siliconflow", "llm7"]:
            key = ""
            try:
                key = kr.get_password("nvhive", f"{name}_api_key") or ""
            except Exception:
                pass
            if not key:
                key = os.environ.get(f"{name.upper()}_API_KEY", "")
            status = f"set ({key[:4]}...{key[-4:]})" if len(key) > 8 else ("set" if key else "not set")
            log(f"  {name:15s} {status}")
    except Exception as e:
        log(f"  Keyring error: {e}")

    # --- Ollama ---
    log("\n[OLLAMA]")
    try:
        import httpx
        resp = httpx.get("http://localhost:11434/api/tags", timeout=3)
        if resp.status_code == 200:
            models = resp.json().get("models", [])
            log("  Status:      running")
            log(f"  Models:      {len(models)}")
            for m in models[:10]:
                name = m.get("name", "?")
                size_gb = m.get("size", 0) / (1024**3)
                log(f"    - {name} ({size_gb:.1f} GB)")
        else:
            log(f"  Status:      error ({resp.status_code})")
    except Exception as e:
        log(f"  Status:      not reachable ({type(e).__name__})")

    # --- Free Tier ---
    log("\n[FREE TIER DETECTION]")
    try:
        from nvh.core.free_tier import detect_available_free_advisors
        available = detect_available_free_advisors()
        log(f"  Available:   {[a.name for a in available]}")
    except Exception as e:
        log(f"  Error: {e}")

    # --- Knowledge Base ---
    log("\n[KNOWLEDGE BASE]")
    try:
        from nvh.core.knowledge import get_knowledge_base
        kb = get_knowledge_base()
        docs = kb.list_documents()
        log(f"  Documents:   {len(docs)}")
        for d in docs[:5]:
            log(f"    - {d.filename} ({d.num_chunks} chunks)")
    except Exception as e:
        log(f"  Error: {e}")

    # --- Memory ---
    log("\n[MEMORY STORE]")
    try:
        from nvh.core.memory import get_memory_store
        store = get_memory_store()
        memories = store.get_all()
        log(f"  Memories:    {len(memories)}")
    except Exception as e:
        log(f"  Error: {e}")

    # --- Test Query (mock) ---
    log("\n[TEST QUERY]")
    try:
        async def _test():
            from nvh.core.engine import Engine
            engine = Engine()
            enabled = await engine.initialize()
            log(f"  Initialized: {len(enabled)} advisors")
            if enabled:
                decision = engine.router.route("test query hello world")
                log(f"  Route test:  {decision.provider}/{decision.model}")
                log(f"  Task type:   {decision.task_type.value}")
                log(f"  Confidence:  {decision.confidence:.2f}")
                log(f"  Reason:      {decision.reason}")
            else:
                log("  No advisors — cannot test routing")
        _run(_test())
    except Exception as e:
        log(f"  Error: {e}")

    # --- Tools ---
    log("\n[TOOLS]")
    try:
        from nvh.core.tools import ToolRegistry
        tools = ToolRegistry()
        log(f"  Registered:  {[t.name for t in tools.list_tools()]}")
    except Exception as e:
        log(f"  Error: {e}")

    # --- Scheduled Tasks ---
    log("\n[SCHEDULER]")
    try:
        from nvh.core.scheduler import Scheduler
        sched = Scheduler()
        tasks = sched.list_tasks()
        log(f"  Tasks:       {len(tasks)}")
    except Exception as e:
        log(f"  Error: {e}")

    # --- Network ---
    log("\n[NETWORK]")
    try:
        import httpx
        for url, name in [
            ("https://api.groq.com", "Groq API"),
            ("https://html.duckduckgo.com", "DuckDuckGo"),
            ("https://ollama.com", "Ollama.com"),
        ]:
            try:
                resp = httpx.head(url, timeout=5, follow_redirects=True)
                log(f"  {name:15s} reachable ({resp.status_code})")
            except Exception:
                log(f"  {name:15s} UNREACHABLE")
    except Exception as e:
        log(f"  Error: {e}")

    # --- NVIDIA Bug Report (only when issues detected or --nvidia-report) ---
    gpu_issues = any("NOT FOUND" in ln or "UNREACHABLE" in ln or "not found" in ln.lower()
                     for ln in lines if "[GPU]" in "".join(lines[:lines.index(ln)+1]) or "NVIDIA" in ln)

    if nvidia_report or gpu_issues:
        log("\n[NVIDIA BUG REPORT]")
        nvidia_report_path = os.path.expanduser("~/nvh/nvidia-bug-report.log.gz")
        try:
            # nvidia-bug-report.sh generates nvidia-bug-report.log.gz
            result = subprocess.run(
                ["nvidia-bug-report.sh", "--output-file", nvidia_report_path],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0 and os.path.exists(nvidia_report_path):
                size_kb = os.path.getsize(nvidia_report_path) / 1024
                log(f"  Generated:   {nvidia_report_path} ({size_kb:.0f} KB)")
                log("  Send to NVIDIA support or attach to bug reports")
                if gpu_issues:
                    log("  [!] GPU issues detected — this report may help diagnose the problem")
            else:
                log(f"  nvidia-bug-report.sh failed (exit {result.returncode})")
                if result.stderr:
                    log(f"  {result.stderr.strip()[:200]}")
        except FileNotFoundError:
            log("  nvidia-bug-report.sh not found (NVIDIA driver may not be installed)")
            log("  Install: https://www.nvidia.com/drivers")
        except subprocess.TimeoutExpired:
            log("  nvidia-bug-report.sh timed out (60s)")
        except Exception as e:
            log(f"  Error: {e}")
    elif not nvidia_report:
        log("\n[NVIDIA BUG REPORT]")
        log("  Skipped — no GPU issues detected. Use --nvidia-report to force.")

    log("\n" + "=" * 60)
    log("End of debug report")

    full_report = "\n".join(lines)

    # Save to file
    if output:
        from pathlib import Path
        Path(output).write_text(full_report)
        console.print(f"[green]Debug report saved to {output}[/green]")

    # Copy to clipboard
    if send:
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=full_report.encode(), timeout=5,
            )
            console.print("[green]Debug report copied to clipboard[/green]")
        except FileNotFoundError:
            try:
                subprocess.run(
                    ["pbcopy"],
                    input=full_report.encode(), timeout=5,
                )
                console.print("[green]Debug report copied to clipboard[/green]")
            except FileNotFoundError:
                console.print("[yellow]Clipboard tools not found. Use -o to save to file.[/yellow]")


# ---------------------------------------------------------------------------
# hive doctor
# ---------------------------------------------------------------------------

@app.command()
def doctor():
    """Run comprehensive system diagnostic."""
    import os

    rows: list[tuple[str, str, str]] = []  # (check, status, detail)

    passed = 0
    warned = 0
    failed = 0
    fixes: list[str] = []

    def _pass(check: str, detail: str = "") -> None:
        nonlocal passed
        passed += 1
        rows.append((check, "[green]PASS[/green]", detail))

    def _warn(check: str, detail: str = "", fix: str = "") -> None:
        nonlocal warned
        warned += 1
        rows.append((check, "[yellow]WARN[/yellow]", detail))
        if fix:
            fixes.append(fix)

    def _fail(check: str, detail: str = "", fix: str = "") -> None:
        nonlocal failed
        failed += 1
        rows.append((check, "[red]FAIL[/red]", detail))
        if fix:
            fixes.append(fix)

    console.print("[bold]Hive Doctor[/bold] — running diagnostics...\n")

    # 1. Python version
    py_version = sys.version_info
    if py_version >= (3, 12):
        _pass("Python version", f"{py_version.major}.{py_version.minor}.{py_version.micro}")
    else:
        _fail(
            "Python version",
            f"{py_version.major}.{py_version.minor}.{py_version.micro} (need >= 3.12)",
            "Upgrade Python to 3.12+: https://python.org/downloads",
        )

    # 2. Config file exists and is valid YAML
    from nvh.config.settings import DEFAULT_CONFIG_PATH
    if not DEFAULT_CONFIG_PATH.exists():
        _fail(
            "Config file exists",
            str(DEFAULT_CONFIG_PATH),
            "Run `nvh config init` to create a configuration file.",
        )
        raw_yaml_ok = False
    else:
        try:
            import yaml
            yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
            _pass("Config file (YAML)", str(DEFAULT_CONFIG_PATH))
            raw_yaml_ok = True
        except Exception as e:
            _fail("Config file (YAML)", str(e), f"Fix YAML syntax in {DEFAULT_CONFIG_PATH}")
            raw_yaml_ok = False

    # 3. Config parses into HiveConfig
    config = None
    if raw_yaml_ok:
        try:
            from nvh.config.settings import load_config
            config = load_config()
            _pass("Config schema (Pydantic)", "HiveConfig validated successfully")
        except Exception as e:
            _fail("Config schema (Pydantic)", str(e), f"Fix config errors in {DEFAULT_CONFIG_PATH}")

    # 4. Database accessible
    async def _check_db() -> tuple[bool, str]:
        try:
            from nvh.storage import repository as repo
            await repo.init_db()
            return True, ""
        except Exception as e:
            return False, str(e)

    db_ok, db_err = asyncio.run(_check_db())
    if db_ok:
        _pass("Database", "init_db succeeded")
    else:
        _fail("Database", db_err, "Check storage permissions or reinstall: pip install hive-ai")

    # 5 & 6. Per-provider checks (API key + health check)
    if config is not None:
        async def _check_providers() -> list[tuple[str, bool, bool, str]]:
            """Returns list of (name, has_key, health_ok, detail)."""
            results = []
            for name, pconfig in config.providers.items():
                if not pconfig.enabled:
                    continue

                # Check for API key
                has_key = bool(pconfig.api_key and not pconfig.api_key.startswith("${"))
                if not has_key:
                    has_key = bool(
                        os.environ.get(f"{name.upper()}_API_KEY")
                        or os.environ.get(f"HIVE_{name.upper()}_API_KEY")
                    )
                if not has_key:
                    try:
                        import keyring
                        has_key = bool(keyring.get_password("nvhive", f"{name}_api_key"))
                    except Exception:
                        pass
                if name == "ollama":
                    has_key = True  # no key needed

                # Health check
                health_ok = False
                detail = ""
                try:
                    from nvh.core.engine import Engine
                    engine = Engine(config=config)
                    await engine.initialize()
                    if engine.registry.has(name):
                        import asyncio as _aio
                        health = await _aio.wait_for(
                            engine.registry.get(name).health_check(),
                            timeout=10,
                        )
                        health_ok = health.healthy
                        detail = f"{health.latency_ms}ms" if health_ok else (health.error or "failed")
                    else:
                        detail = "not registered (check API key)"
                except Exception as e:
                    detail = str(e)

                results.append((name, has_key, health_ok, detail))
            return results

        provider_results = asyncio.run(_check_providers())
        for name, has_key, health_ok, detail in provider_results:
            if not has_key:
                _fail(
                    f"Advisor {name}: API key",
                    "missing",
                    f"Run `hive advisor login {name}` or set {name.upper()}_API_KEY",
                )
            else:
                _pass(f"Advisor {name}: API key", "found")

            if health_ok:
                _pass(f"Advisor {name}: health check", detail)
            else:
                _warn(
                    f"Advisor {name}: health check",
                    detail or "failed",
                    f"Check your {name} API key and network access.",
                )

    # 6. Ollama detection
    ollama_models: list[str] = []
    try:
        import httpx
        resp = httpx.get("http://localhost:11434/api/tags", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            ollama_models = [m.get("name", "") for m in data.get("models", [])]
            _pass("Ollama", f"detected, {len(ollama_models)} model(s)")
        else:
            _warn("Ollama", f"HTTP {resp.status_code}", "Start Ollama: `ollama serve`")
    except Exception:
        _warn("Ollama", "not reachable at localhost:11434", "Install from https://ollama.ai or start with `ollama serve`")

    # 7. Cache status
    if config is not None:
        try:
            from nvh.core.engine import Engine
            engine_for_cache = Engine(config=config)
            stats = engine_for_cache.cache.stats
            cache_detail = f"{stats['entries']} entries / max {stats['max_size']}"
            if config.cache.enabled:
                _pass("Cache", cache_detail)
            else:
                _warn("Cache", "disabled in config", "Set cache.enabled: true in config to improve performance.")
        except Exception as e:
            _warn("Cache", str(e))

    # 8. Disk space
    try:
        import shutil as _shutil
        home_path = Path.home()
        usage = _shutil.disk_usage(home_path)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 1.0:
            _fail(
                "Disk space",
                f"{free_gb:.1f}GB free",
                "Free up disk space — less than 1GB available.",
            )
        elif free_gb < 5.0:
            _warn("Disk space", f"{free_gb:.1f}GB free", "Disk space is low (< 5GB).")
        else:
            _pass("Disk space", f"{free_gb:.1f}GB free")
    except Exception as e:
        _warn("Disk space", str(e))

    # 9. GPU detection
    try:
        from nvh.utils.gpu import detect_gpus, get_gpu_summary, recommend_models
        gpus = detect_gpus()
        if gpus:
            summary = get_gpu_summary()
            _pass("GPU (nvidia-smi)", summary)
            # Show per-GPU detail for multi-GPU systems
            if len(gpus) > 1:
                for g in gpus:
                    _pass(
                        f"  GPU {g.index}: {g.name}",
                        f"{g.vram_gb:.1f} GB VRAM, driver {g.driver_version}",
                    )
            # Show model recommendations
            recs = recommend_models(gpus)
            rec_str = ", ".join(r.model for r in recs)
            _pass("GPU model recommendations", rec_str + f" — {recs[0].reason}" if recs else "none")
        else:
            _warn(
                "GPU (nvidia-smi)",
                "no NVIDIA GPU detected — Ollama will run in CPU mode",
                "Install NVIDIA drivers and nvidia-smi to enable GPU acceleration.",
            )
    except Exception as e:
        _warn("GPU (nvidia-smi)", str(e))

    # 9b. Linux Desktop detection
    try:
        from nvh.integrations.cloud_session import detect_cloud_session
        cloud = detect_cloud_session()
        if cloud.is_cloud:
            tier_label = cloud.tier.capitalize() if cloud.tier else "Unknown"
            _pass(
                "Linux Desktop",
                f"{tier_label} tier — {cloud.gpu_class}" + (f" | Session: {cloud.session_id[:8]}..." if cloud.session_id else ""),
            )
        else:
            _pass("Linux Desktop", "not detected (local / native)")
    except Exception as e:
        _warn("Linux Desktop", str(e))

    # 10. Local models from Ollama
    if ollama_models:
        _pass("Ollama local models", ", ".join(ollama_models[:5]) + (" ..." if len(ollama_models) > 5 else ""))
    else:
        _warn(
            "Ollama local models",
            "none found",
            "Pull a model: `ollama pull llama3.1`",
        )

    # 11. Deployment environment detection
    try:
        from nvh.utils.environment import detect_environment, get_environment_summary
        env_info = detect_environment()

        # Platform
        _pass("Environment: platform", env_info.platform)

        # Docker
        if env_info.is_docker:
            _pass("Environment: container", "running inside Docker")
        else:
            _pass("Environment: container", "not in Docker (native)")

        # Cloud
        if env_info.is_cloud:
            cloud_detail = env_info.cloud_provider
            if env_info.instance_type and env_info.instance_type != "unknown":
                cloud_detail += f" / {env_info.instance_type}"
            if env_info.public_ip:
                cloud_detail += f" / {env_info.public_ip}"
            _pass("Environment: cloud", cloud_detail)
        else:
            _pass("Environment: cloud", "not detected (local / on-prem)")

        # GPU accessibility (separate from nvidia-smi check above)
        if env_info.gpu_accessible:
            _pass("Environment: GPU accessible", f"{env_info.gpu_count} GPU(s) accessible from this process")
        elif env_info.has_gpu:
            _warn(
                "Environment: GPU accessible",
                "GPU detected but not accessible (container config?)",
                "Add --gpus all to docker run, or configure NVIDIA Container Toolkit.",
            )
        else:
            _pass("Environment: GPU accessible", "no GPU present (CPU mode)")

        # Root access
        if env_info.has_root:
            _warn(
                "Environment: root access",
                "running as root",
                "Consider running as a non-root user for improved security.",
            )
        else:
            _pass("Environment: root access", "non-root user (good)")

        # Print a compact environment summary line
        summary = get_environment_summary(env_info)
        console.print(f"\n[dim]Environment summary: {summary}[/dim]")

    except Exception as e:
        _warn("Environment detection", str(e))

    # -----------------------------------------------------------------------
    # Render results table
    # -----------------------------------------------------------------------
    table = Table(title="Diagnostic Results", show_lines=False)
    table.add_column("Check", style="bold", min_width=35)
    table.add_column("Status", justify="center", min_width=8)
    table.add_column("Detail")

    for check, status, detail in rows:
        table.add_row(check, status, detail)

    console.print(table)

    # Summary
    total = passed + warned + failed
    summary_parts = []
    if passed:
        summary_parts.append(f"[green]{passed} passed[/green]")
    if warned:
        summary_parts.append(f"[yellow]{warned} warnings[/yellow]")
    if failed:
        summary_parts.append(f"[red]{failed} failures[/red]")
    console.print(f"\nResults: {', '.join(summary_parts)} ({total} checks total)")

    if fixes:
        console.print("\n[bold]Suggested fixes:[/bold]")
        for i, fix in enumerate(fixes, 1):
            console.print(f"  {i}. {fix}")

    if failed:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# hive template
# ---------------------------------------------------------------------------

template_app = typer.Typer(help="Manage prompt templates")
app.add_typer(template_app, name="template")


@template_app.command("list")
def template_list():
    """List all available prompt templates."""
    from nvh.core.templates import list_templates

    templates = list_templates()
    if not templates:
        console.print("[dim]No templates found.[/dim]")
        return

    table = Table(title="Available Templates")
    table.add_column("Name", style="bold cyan")
    table.add_column("Description")
    table.add_column("Required vars", style="dim")
    table.add_column("Optional vars", style="dim")

    for t in templates:
        req = ", ".join(t.required_vars) if t.required_vars else "[dim]—[/dim]"
        opt = ", ".join(t.optional_vars.keys()) if t.optional_vars else "[dim]—[/dim]"
        table.add_row(t.name, t.description or "[dim]—[/dim]", req, opt)

    console.print(table)
    console.print("\n[dim]Use: hive ask --template <name> --var key=value[/dim]")


@template_app.command("show")
def template_show(
    name: str = typer.Argument(..., help="Template name"),
):
    """Display template content and variable information."""
    from nvh.core.templates import load_template

    try:
        t = load_template(name)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold cyan]{t.name}[/bold cyan]")
    if t.description:
        console.print(f"[dim]{t.description}[/dim]")
    console.print()

    if t.required_vars:
        console.print("[bold]Required variables:[/bold]")
        for v in t.required_vars:
            console.print(f"  [yellow]{{{{[/yellow]{v}[yellow]}}}}[/yellow]  (required)")

    if t.optional_vars:
        console.print("[bold]Optional variables:[/bold]")
        for v, default in t.optional_vars.items():
            default_str = f"  default: {default!r}" if default != "" else ""
            console.print(f"  [green]{{{{[/green]{v}[green]}}}}[/green]{default_str}")

    if t.system:
        console.print(f"\n[bold]System prompt:[/bold]\n{t.system}")

    console.print("\n[bold]Body:[/bold]")
    console.print(Panel(t.body, border_style="dim"))

    console.print(
        f"\n[dim]Example: hive ask --template {t.name}"
        + "".join(f" --var {v}=..." for v in t.required_vars)
        + "[/dim]"
    )


@template_app.command("create")
def template_create(
    name: str = typer.Argument(..., help="Template name (alphanumeric, underscores)"),
):
    """Create a new prompt template interactively."""
    import re as _re

    from nvh.core.templates import TEMPLATES_DIR

    if not _re.match(r"^[a-zA-Z0-9_]+$", name):
        console.print("[red]Template name must be alphanumeric with underscores only.[/red]")
        raise typer.Exit(1)

    dest = TEMPLATES_DIR / f"{name}.yaml"
    if dest.exists():
        if not typer.confirm(f"Template '{name}' already exists. Overwrite?", default=False):
            raise typer.Exit(0)

    console.print(f"[bold]Creating template:[/bold] {name}\n")

    description = typer.prompt("Description", default="")
    system_prompt = typer.prompt("System prompt (leave blank for none)", default="")

    req_vars_raw = typer.prompt(
        "Required variables (comma-separated, e.g. code,text)", default=""
    )
    required_vars = [v.strip() for v in req_vars_raw.split(",") if v.strip()]

    opt_vars_raw = typer.prompt(
        "Optional variables with defaults (e.g. length=medium,format=prose)", default=""
    )
    optional_vars: dict[str, str] = {}
    for item in opt_vars_raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            k, _, v = item.partition("=")
            optional_vars[k.strip()] = v.strip()
        else:
            optional_vars[item] = ""

    console.print(
        "\nEnter the template body. "
        "Use {{variable}} for placeholders. "
        "Type END on a new line when done."
    )
    body_lines = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        body_lines.append(line)
    body = "\n".join(body_lines)

    # Build YAML frontmatter
    import yaml as _yaml
    frontmatter: dict = {"name": name}
    if description:
        frontmatter["description"] = description
    if system_prompt:
        frontmatter["system"] = system_prompt
    if required_vars:
        frontmatter["required_vars"] = required_vars
    if optional_vars:
        frontmatter["optional_vars"] = optional_vars

    content = f"---\n{_yaml.dump(frontmatter, default_flow_style=False)}---\n{body}\n"

    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    console.print(f"\n[green]Template '{name}' saved to {dest}[/green]")
    console.print(
        f"[dim]Use: hive ask --template {name}"
        + "".join(f" --var {v}=..." for v in required_vars)
        + "[/dim]"
    )


# ---------------------------------------------------------------------------
# hive workflow
# ---------------------------------------------------------------------------

workflow_app = typer.Typer(help="Manage and run workflow pipelines")
app.add_typer(workflow_app, name="workflow")


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
        icons = {"running": "[yellow]...[/yellow]", "done": "[green]OK[/green]", "skipped": "[dim]SKIP[/dim]", "error": "[red]ERR[/red]"}
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
            console.print(f"[green]Workflow complete.[/green] ({result.steps_completed}/{result.steps_total} steps)")
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
        console.print(f"\n  [bold]{i}. {step.name}[/bold]  [{action_style}]{step.action}[/{action_style}]")
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
# hive completions
# ---------------------------------------------------------------------------

@app.command()
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
            f"\n[dim]Tip: run `hive completions {shell} --install` to install automatically.[/dim]"
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

@app.command("do")
def do_task(
    task: str = typer.Argument(..., help="Task for the agent to complete"),
    advisor: str | None = typer.Option(None, "-a", "--advisor", help="Specific advisor to use"),
    model: str | None = typer.Option(None, "-m", "--model", help="Specific model to use"),
    max_steps: int = typer.Option(15, "--max-steps", help="Maximum agent iterations"),
    auto: bool = typer.Option(True, "--auto/--confirm", help="Auto-approve safe tools (default: yes)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be done without executing"),
    profile: str | None = typer.Option(None, "--profile", help="Config profile to use"),
):
    """Hands-free mode — give NVHive a task and it completes it autonomously.

    The agent can read/write files, search the web, run code, and more.
    Safe tools (read, search) run automatically. Unsafe tools (write, execute)
    ask for confirmation unless --auto is set.

    Examples:

      nvh do "Find all TODO comments in this project and create a summary"

      nvh do "Search the web for Python FastAPI tutorials and summarize the top 3"

      nvh do "Read README.md and suggest improvements"

      nvh do "Create a Python script that sorts CSV files by column"
    """
    import time as _time

    from nvh.config.settings import load_config
    from nvh.core.agent_loop import AgentStep, run_agent_loop
    from nvh.core.engine import Engine
    from nvh.core.tools import ToolRegistry

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
            console.print(f"[bold]Routing:[/bold]  advisor=[cyan]{effective_advisor}[/cyan]  model=[cyan]{effective_model}[/cyan]")
            console.print(f"[bold]Max steps:[/bold] {max_steps}")
            console.print(f"[bold]Auto-approve safe tools:[/bold] {'yes' if auto else 'no'}")
            console.print()

            # Show available tools
            tool_list = tools.list_tools()
            safe_tools = [t.name for t in tool_list if t.safe]
            unsafe_tools = [t.name for t in tool_list if not t.safe]
            console.print(f"[bold]Available tools ({len(tool_list)} total):[/bold]")
            console.print(f"  [green]Safe (auto-run):[/green] {', '.join(safe_tools) or 'none'}")
            console.print(f"  [yellow]Unsafe (require approval):[/yellow] {', '.join(unsafe_tools) or 'none'}")
            console.print()

            # Show spending cap
            budget = config.budget
            console.print(f"[bold]Spending caps:[/bold]  daily=${budget.daily_limit_usd}  monthly=${budget.monthly_limit_usd}  hard_stop={'yes' if budget.hard_stop else 'no'}")
            console.print()

            console.print("[bold yellow]Dry run complete. Remove --dry-run to execute.[/bold yellow]")
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
                    console.print(f"  [green]✓[/green] [dim]{preview}{'...' if len(result.output) > 60 else ''}[/dim]")
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
            confirm_unsafe=None if auto else confirm_unsafe,
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
# nvh voice — speak your question, hear the answer
# ---------------------------------------------------------------------------

@app.command()
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
                    console.print("[yellow]TTS produced no output — is edge-tts installed?[/yellow]")
                    console.print("[dim]Install: pip install edge-tts[/dim]")
            except Exception as e:
                console.print(f"[yellow]TTS failed (answer shown above): {e}[/yellow]")

    _run(_run_voice())


# ---------------------------------------------------------------------------
# nvh imagine — generate an image from a text description
# ---------------------------------------------------------------------------

@app.command()
def imagine(
    prompt: str = typer.Argument(..., help="Text description of the image to generate"),
    output: str = typer.Option("", "-o", "--output", help="Output path for the image (default: auto temp file)"),
    provider: str = typer.Option("auto", "--provider", help="Provider: auto, openai, stability, pollinations"),
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

@app.command()
def screenshot(
    advisor: str | None = typer.Option(None, "-a", "--advisor", help="Advisor to use for analysis"),
    model: str | None = typer.Option(None, "-m", "--model", help="Model to use"),
    save: str | None = typer.Option(None, "--save", help="Save screenshot to this path"),
    no_analysis: bool = typer.Option(False, "--no-analysis", help="Just take the screenshot, skip LLM analysis"),
    question: str = typer.Option("Describe this screenshot in detail.", "-q", "--question", help="Question to ask about the screenshot"),
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
                "Linux: sudo apt install scrot[/dim]"
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

@app.command()
def learn(
    path: str = typer.Argument(..., help="File or directory to ingest into the knowledge base"),
):
    """Add documents to your knowledge base for RAG queries.

    Supports: PDF, TXT, MD, RST, CSV, PY, JS, TS, JSON, YAML, and more.

    Examples:

      nvh learn README.md

      nvh learn docs/

      nvh ask "What does the spec say about authentication?" --knowledge
    """
    from pathlib import Path as _Path

    from nvh.core.knowledge import get_knowledge_base

    kb = get_knowledge_base()
    target = _Path(path)

    if not target.exists():
        console.print(f"[red]Path not found: {path}[/red]")
        raise typer.Exit(1)

    files: list[_Path] = []
    if target.is_dir():
        # Ingest all readable files in the directory (non-recursive for safety)
        supported_exts = {
            ".txt", ".md", ".rst", ".csv", ".log",
            ".py", ".js", ".ts", ".java", ".c", ".cpp", ".go", ".rs",
            ".json", ".yaml", ".yml", ".toml", ".pdf",
        }
        files = [f for f in sorted(target.iterdir()) if f.is_file() and f.suffix.lower() in supported_exts]
        if not files:
            console.print(f"[yellow]No supported files found in {path}[/yellow]")
            raise typer.Exit(1)
    else:
        files = [target]

    ingested = 0
    skipped = 0
    for f in files:
        try:
            doc = kb.ingest(str(f))
            if doc.num_chunks == 0:
                console.print(f"[yellow]Skipped (empty):[/yellow] {f.name}")
                skipped += 1
            else:
                console.print(
                    f"[green]Ingested:[/green] {doc.filename} "
                    f"[dim]({doc.num_chunks} chunks, {doc.size_bytes:,} bytes, id={doc.id})[/dim]"
                )
                ingested += 1
        except Exception as e:
            console.print(f"[red]Error ingesting {f.name}: {e}[/red]")
            skipped += 1

    console.print(
        f"\n[bold]{ingested} file(s) ingested[/bold]"
        + (f", {skipped} skipped" if skipped else "")
        + "."
    )
    console.print("[dim]Use: nvh ask \"your question\" --knowledge[/dim]")


# ---------------------------------------------------------------------------
# nvh knowledge — manage the knowledge base
# ---------------------------------------------------------------------------

knowledge_app = typer.Typer(help="Manage the RAG knowledge base")
app.add_typer(knowledge_app, name="knowledge")


@knowledge_app.command("list")
def knowledge_list():
    """List all ingested documents."""
    from nvh.core.knowledge import get_knowledge_base

    kb = get_knowledge_base()
    docs = kb.list_documents()

    if not docs:
        console.print("[dim]No documents in the knowledge base.[/dim]")
        console.print("[dim]Add some with: nvh learn path/to/file[/dim]")
        return

    table = Table(title="Knowledge Base Documents")
    table.add_column("ID", style="dim cyan")
    table.add_column("Filename", style="bold")
    table.add_column("Type", style="dim")
    table.add_column("Chunks", justify="right")
    table.add_column("Size", justify="right", style="dim")
    table.add_column("Ingested", style="dim")

    for doc in docs:
        size_str = f"{doc.size_bytes:,} B" if doc.size_bytes < 1024 else f"{doc.size_bytes // 1024:,} KB"
        ingested_at = doc.ingested_at[:10] if doc.ingested_at else "—"
        table.add_row(doc.id, doc.filename, doc.doc_type, str(doc.num_chunks), size_str, ingested_at)

    console.print(table)
    console.print(f"\n[dim]{len(docs)} document(s) | Use 'nvh knowledge remove <id>' to remove one[/dim]")


@knowledge_app.command("search")
def knowledge_search(
    query: str = typer.Argument(..., help="Search query"),
    max_results: int = typer.Option(5, "-n", "--max", help="Maximum results to return"),
):
    """Search the knowledge base for relevant chunks."""
    from nvh.core.knowledge import get_knowledge_base

    kb = get_knowledge_base()
    chunks = kb.search(query, max_results=max_results)

    if not chunks:
        console.print("[dim]No results found.[/dim]")
        return

    console.print(f"[bold]Top {len(chunks)} result(s) for:[/bold] {query}\n")
    for i, chunk in enumerate(chunks, 1):
        source = chunk.metadata.get("filename", "unknown")
        total = chunk.metadata.get("total", "?")
        console.print(
            f"[bold cyan]{i}.[/bold cyan] [dim]{source} — chunk {chunk.chunk_index}/{total}[/dim]"
        )
        preview = chunk.content[:300].replace("\n", " ")
        if len(chunk.content) > 300:
            preview += "..."
        console.print(f"   {preview}\n")


@knowledge_app.command("remove")
def knowledge_remove(
    doc_id: str = typer.Argument(..., help="Document ID (or prefix) to remove"),
):
    """Remove a document and its chunks from the knowledge base."""
    from nvh.core.knowledge import get_knowledge_base

    kb = get_knowledge_base()
    removed = kb.remove_document(doc_id)
    if removed:
        console.print(f"[green]Removed document:[/green] {doc_id}")
    else:
        console.print(f"[red]Document not found:[/red] {doc_id}")
        console.print("[dim]Run 'nvh knowledge list' to see available document IDs.[/dim]")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# nvh schedule — recurring AI tasks
# ---------------------------------------------------------------------------

schedule_app = typer.Typer(help="Schedule recurring AI tasks")
app.add_typer(schedule_app, name="schedule")


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
    task = scheduler.add(prompt=prompt, interval_seconds=interval_seconds, advisor=advisor, mode=mode)

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
    console.print(f"\n[dim]{len(tasks)} task(s) | Run 'nvh schedule start' to execute due tasks[/dim]")


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
app.add_typer(git_app, name="git")


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
        diff_out = diff_out[:max_diff_chars] + f"\n\n[... diff truncated at {max_diff_chars} chars ...]"

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
        console.print(Panel(explanation, title="[bold]Git History Explained[/bold]", border_style="green"))


# ---------------------------------------------------------------------------
# nvh scan — AI analysis of a codebase
# ---------------------------------------------------------------------------

@app.command()
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
            ("hardcoded secret", ["password =", "secret =", "api_key =", "token =", "private_key ="]),
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

    elif focus == "dependencies":
        dep_files_content: list[str] = []
        for df in ["requirements.txt", "package.json", "Cargo.toml", "go.mod", "pyproject.toml", "pom.xml"]:
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

    console.print(f"[dim]Analysing with focus=[bold]{focus}[/bold] ({total_files} files, {total_lines:,} lines)…[/dim]")

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
# Entry point — catches unknown commands and treats them as prompts
# ---------------------------------------------------------------------------

def main():
    """Entry point — routes between subcommands and bare prompts.

    nvh                     → REPL
    nvh version             → subcommand
    nvh status              → subcommand
    nvh "what is AI?"       → bare prompt → LLM
    nvh ask "question"      → subcommand
    """
    args = sys.argv[1:]

    if not args:
        # No arguments → launch REPL
        _run(_launch_default_repl())
        return

    # Flags like --help, --version should go to Typer directly
    if args[0].startswith("-"):
        app()
        return

    # Check if the first arg is a known subcommand or advisor
    first = args[0].lower().replace("-", "_")

    # Get all registered command names from Typer
    known_commands = set()
    for cmd_info in app.registered_commands:
        if hasattr(cmd_info, "name") and cmd_info.name:
            known_commands.add(cmd_info.name)
        if hasattr(cmd_info, "callback") and cmd_info.callback:
            known_commands.add(cmd_info.callback.__name__)

    # Also add sub-typer group names
    for group in app.registered_groups:
        if hasattr(group, "name") and group.name:
            known_commands.add(group.name)

    # Add advisor names
    known_commands.update(k.lower() for k in KNOWN_ADVISORS.keys())

    # Common command aliases
    known_commands.update({
        "ask", "convene", "poll", "throwdown", "quick", "safe", "do",
        "code", "write", "research", "math", "clip",
        "voice", "imagine", "screenshot", "bench", "scan", "learn",
        "setup", "status", "savings", "debug", "doctor", "update", "version",
        "serve", "repl", "completions", "plugins", "nemoclaw", "mcp", "openclaw", "integrate", "service", "hostname",
        "advisor", "agent", "config", "conversation", "budget", "model",
        "template", "workflow", "knowledge", "schedule", "webhook", "auth",
        "git", "webui", "keys",
    })

    if first in known_commands:
        # It's a subcommand — let Typer handle it
        app()
    else:
        # It's a bare prompt — route to smart default
        prompt = " ".join(args)

        # Check for system actions first
        from nvh.core.action_detector import detect_action
        action = detect_action(prompt)
        if action:
            _run(_execute_action(action))
        else:
            _run(_smart_default(prompt))


if __name__ == "__main__":
    main()
