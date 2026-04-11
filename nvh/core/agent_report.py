"""Explainability dashboard that summarizes an agent session.

Produces Rich-markup formatted reports for agentic coding sessions.
"""

from __future__ import annotations


def assess_multi_model_value(
    phases: list[dict],
    verification: str,
) -> str:
    """Analyze whether multi-model routing provided value.

    Returns a human-readable assessment string.
    """
    unique_models = {p.get("model", "") for p in phases}
    unique_models.discard("")
    lines: list[str] = []

    if len(unique_models) > 1:
        lines.append(
            f"Different models provided diverse perspectives "
            f"({', '.join(sorted(unique_models))})"
        )

    # Look for reviewer phases that caught issues
    for p in phases:
        note = p.get("quality_note", "")
        if "issue" in note.lower() or "fix" in note.lower():
            lines.append(f"Reviewer caught issues: {note}")

    if verification and "pass" in verification.lower():
        lines.append("Verification passed after multi-model pipeline")
    elif verification:
        lines.append(f"Verification status: {verification}")

    if not lines:
        lines.append("Single-model execution; multi-model routing was not needed")

    return "; ".join(lines)


def format_session_report(
    task: str,
    tier: str,
    duration_ms: int,
    phases: list[dict],
    files_modified: list[str],
    files_created: list[str],
    files_read: list[str],
    commands_run: list[str],
    verification: str,
    cost_local_tokens: int,
    cost_cloud_tokens: int,
    cost_cloud_usd: float,
    multi_model_value: str,
    mode: str,
) -> str:
    """Format a complete session report with Rich markup."""
    total_secs = duration_ms / 1000

    lines = [
        "",
        "[bold]Agent Session Report[/bold]",
        f"[bold]Task:[/bold] {task}",
        f"[bold]Tier:[/bold] {tier}  [bold]Mode:[/bold] {mode}",
        f"[bold]Duration:[/bold] {total_secs:.1f}s",
        "",
        "[bold]Phases[/bold]",
    ]

    for p in phases:
        dur = p.get("duration_ms", 0) / 1000
        model = p.get("model", "?")
        tokens = p.get("tokens", 0)
        note = p.get("quality_note", "")
        note_part = f"  [dim]({note})[/dim]" if note else ""
        lines.append(
            f"  {p.get('name', '?'):12s}  {model:20s}  "
            f"{dur:5.1f}s  {tokens:6d} tok{note_part}"
        )

    lines.append("")
    lines.append("[bold]File Changes[/bold]")
    if files_modified:
        lines.append(f"  [yellow]Modified:[/yellow] {', '.join(files_modified)}")
    if files_created:
        lines.append(f"  [green]Created:[/green]  {', '.join(files_created)}")
    if files_read:
        lines.append(f"  [dim]Read:[/dim]     {', '.join(files_read)}")

    lines.append("")
    lines.append("[bold]Cost Breakdown[/bold]")
    lines.append(f"  Local tokens:  {cost_local_tokens:,}")
    lines.append(f"  Cloud tokens:  {cost_cloud_tokens:,}")
    lines.append(f"  Cloud cost:    ${float(cost_cloud_usd):.4f}")

    lines.append("")
    lines.append(f"[bold]Multi-Model Value:[/bold] {multi_model_value}")
    lines.append("")

    return "\n".join(lines)
