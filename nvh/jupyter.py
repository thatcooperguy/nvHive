"""IPython/Jupyter magic commands for nvHive.

Usage in a Jupyter notebook::

    %load_ext nvh.jupyter

    %nvh What is quicksort?
    %nvh_council Should we use Redis or Postgres?
    %nvh_safe Analyze this private data
    %nvh_why
"""

from __future__ import annotations

import json
from pathlib import Path


def _display(obj):
    """Pretty-print a response or council result in a notebook cell."""
    try:
        from IPython.display import Markdown, display

        if hasattr(obj, "synthesis"):
            # Council result
            parts = [f"## Council Synthesis\n\n{obj.synthesis.content}\n"]
            for i, resp in enumerate(getattr(obj, "member_responses", []), 1):
                label = getattr(resp, "provider", f"member-{i}")
                parts.append(f"### {label}\n\n{resp.content}\n")
            display(Markdown("\n---\n".join(parts)))
        elif hasattr(obj, "content"):
            meta = []
            if getattr(obj, "provider", None):
                meta.append(f"**Provider:** {obj.provider}")
            if getattr(obj, "model", None):
                meta.append(f"**Model:** {obj.model}")
            cost = getattr(obj, "cost_usd", None)
            if cost is not None:
                meta.append(f"**Cost:** ${cost:.4f}")
            latency = getattr(obj, "latency_ms", None)
            if latency:
                meta.append(f"**Latency:** {latency}ms")
            header = " | ".join(meta)
            display(Markdown(f"{header}\n\n---\n\n{obj.content}"))
        else:
            print(obj)
    except Exception:
        print(obj)


def _register_magics(ipython):
    """Register all nvHive magic commands."""
    from IPython.core.magic import register_line_magic

    @register_line_magic
    def nvh(line):  # noqa: F811
        """Ask nvHive a question: %nvh What is quicksort?"""
        from nvh.sdk import ask_sync

        resp = ask_sync(line)
        _display(resp)

    @register_line_magic
    def nvh_council(line):
        """Convene an nvHive council: %nvh_council Should we use Redis?"""
        from nvh.sdk import convene_sync

        result = convene_sync(line)
        _display(result)

    @register_line_magic
    def nvh_safe(line):
        """Safe (local-only) query: %nvh_safe Analyze this private data"""
        from nvh.sdk import safe_sync

        resp = safe_sync(line)
        _display(resp)

    @register_line_magic
    def nvh_why(line):  # noqa: ARG001
        """Show routing details for the last query."""
        try:
            from IPython.display import JSON, display

            path = Path.home() / ".hive" / "last_query.json"
            if not path.exists():
                print("No last query found. Run %nvh first.")
                return
            data = json.loads(path.read_text())
            display(JSON(data))
        except ImportError:
            path = Path.home() / ".hive" / "last_query.json"
            if not path.exists():
                print("No last query found. Run %nvh first.")
                return
            print(json.dumps(json.loads(path.read_text()), indent=2))


def load_ipython_extension(ipython):
    """Entry point for ``%load_ext nvh.jupyter``."""
    _register_magics(ipython)
