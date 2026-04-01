"""NVHive Workflows — chain multiple AI operations into pipelines.

Workflows are defined as YAML files in ~/.hive/workflows/ or inline.

Example workflow (code_review.yaml):
  name: Code Review Pipeline
  description: Full code review with multiple passes
  steps:
    - name: initial_review
      action: ask
      prompt: "Review this code for bugs and security issues: {{input}}"
      advisor: anthropic
      save_as: review

    - name: test_suggestions
      action: ask
      prompt: "Based on this review, suggest unit tests:\n{{review}}"
      advisor: openai
      save_as: tests

    - name: final_summary
      action: convene
      prompt: "Summarize the code review and test suggestions:\n\nReview: {{review}}\n\nTests: {{tests}}"
      cabinet: code_review
      save_as: summary

Usage:
  nvh workflow run code_review --input "$(cat main.py)"
  nvh workflow list
  nvh workflow show code_review
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class WorkflowStep:
    name: str
    action: str          # "ask", "convene", "poll", "safe", "shell"
    prompt: str          # supports {{var}} templates
    advisor: str = ""    # for "ask" action
    cabinet: str = ""    # for "convene" action
    save_as: str = ""    # variable name to store result
    condition: str = ""  # optional: only run if condition is truthy


@dataclass
class Workflow:
    name: str
    description: str = ""
    steps: list[WorkflowStep] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)  # default variable values


@dataclass
class WorkflowResult:
    workflow_name: str
    steps_completed: int
    steps_total: int
    variables: dict[str, str]  # all saved variables including step outputs
    success: bool
    error: str = ""


def load_workflow(path: Path) -> Workflow:
    """Load a workflow from a YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)

    steps = []
    for s in data.get("steps", []):
        steps.append(WorkflowStep(
            name=s.get("name", f"step_{len(steps)+1}"),
            action=s.get("action", "ask"),
            prompt=s.get("prompt", ""),
            advisor=s.get("advisor", ""),
            cabinet=s.get("cabinet", ""),
            save_as=s.get("save_as", ""),
            condition=s.get("condition", ""),
        ))

    return Workflow(
        name=data.get("name", path.stem),
        description=data.get("description", ""),
        steps=steps,
        variables=data.get("variables", {}),
    )


def discover_workflows(dirs: list[Path] | None = None) -> dict[str, Path]:
    """Find all workflow YAML files."""
    if dirs is None:
        dirs = [
            Path.home() / ".hive" / "workflows",
            Path.cwd() / ".hive" / "workflows",
        ]

    # Also include the built-in workflow templates shipped with the package
    _builtin = Path(__file__).parent.parent / "workflows"
    if _builtin.is_dir():
        dirs = [_builtin] + list(dirs)

    workflows = {}
    for d in dirs:
        if d.is_dir():
            for f in d.glob("*.yaml"):
                workflows[f.stem] = f
            for f in d.glob("*.yml"):
                workflows[f.stem] = f

    return workflows


def _render_template(template: str, variables: dict[str, str]) -> str:
    """Replace {{var}} placeholders with variable values."""
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{{{key}}}}}", str(value))
    return result


async def run_workflow(
    workflow: Workflow,
    engine,
    initial_vars: dict[str, str] | None = None,
    on_step: Any = None,  # callback(step_name, status, result)
) -> WorkflowResult:
    """Execute a workflow step by step."""
    variables = dict(workflow.variables)
    if initial_vars:
        variables.update(initial_vars)

    completed = 0

    for step in workflow.steps:
        # Check condition
        if step.condition:
            cond_value = variables.get(step.condition, "")
            if not cond_value:
                if on_step:
                    on_step(step.name, "skipped", "Condition not met")
                continue

        # Render prompt with current variables
        prompt = _render_template(step.prompt, variables)

        if on_step:
            on_step(step.name, "running", "")

        try:
            if step.action == "ask":
                resp = await engine.query(
                    prompt=prompt,
                    provider=step.advisor or None,
                    stream=False,
                )
                result_text = resp.content

            elif step.action == "convene":
                result = await engine.run_council(
                    prompt=prompt,
                    agent_preset=step.cabinet or None,
                    auto_agents=True,
                    synthesize=True,
                )
                result_text = result.synthesis.content if result.synthesis else ""

            elif step.action == "poll":
                results = await engine.compare(prompt=prompt)
                result_text = "\n\n".join(
                    f"--- {name} ---\n{r.content}" for name, r in results.items()
                )

            elif step.action == "safe":
                resp = await engine.query(
                    prompt=prompt,
                    provider="ollama",
                    privacy=True,
                    stream=False,
                )
                result_text = resp.content

            elif step.action == "shell":
                from nvh.sandbox.executor import SandboxExecutor
                sandbox = SandboxExecutor()
                exec_result = await sandbox.execute(code=prompt, language="bash")
                result_text = exec_result.stdout
                if exec_result.stderr:
                    result_text += f"\nSTDERR: {exec_result.stderr}"
                if exec_result.timed_out:
                    result_text += "\n(timed out)"

            else:
                result_text = f"Unknown action: {step.action}"

            # Save result as variable
            if step.save_as:
                variables[step.save_as] = result_text

            completed += 1
            if on_step:
                on_step(step.name, "done", result_text[:200])

        except Exception as e:
            if on_step:
                on_step(step.name, "error", str(e))
            return WorkflowResult(
                workflow_name=workflow.name,
                steps_completed=completed,
                steps_total=len(workflow.steps),
                variables=variables,
                success=False,
                error=f"Step '{step.name}' failed: {e}",
            )

    return WorkflowResult(
        workflow_name=workflow.name,
        steps_completed=completed,
        steps_total=len(workflow.steps),
        variables=variables,
        success=True,
    )
