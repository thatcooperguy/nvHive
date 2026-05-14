"""API service layer — domain logic decoupled from HTTP.

Route handlers in ``nvh/api/server.py`` historically wrapped business logic
inline: parameter validation, engine setup, provider routing, error mapping,
response envelope building. That makes the same logic hard to reuse from the
CLI, agent loops, or background jobs, and bloats ``server.py`` past 5000 LoC.

The service layer holds the actual domain operations. Routes become thin
adapters that translate the HTTP request into a service call and the result
into an HTTP response. Other callers (CLI, agents) bypass the HTTP layer and
talk to the same service directly.

Currently exported services:
  - QueryService    — single-provider completions (``/v1/query``)

Planned (not in this PR):
  - CouncilService  — multi-LLM consensus (``/v1/council``)
  - CompareService  — side-by-side comparison (``/v1/compare``)

See :class:`nvh.api.services.query_service.QueryService` for the canonical
shape: domain exceptions on errors (no ``HTTPException`` leakage), simple
dataclasses/dicts for I/O, no FastAPI imports.
"""

from __future__ import annotations

from nvh.api.services.query_service import QueryService

__all__ = ["QueryService"]
