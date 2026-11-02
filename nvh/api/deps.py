"""FastAPI dependencies shared by every nvHive route.

``require_auth``, the bearer scheme it reads, the response envelope and the
engine accessor lived at the top of ``nvh/api/server.py`` until 0.46; they
moved here so routers outside the monolith can depend on them without
importing the whole server. ``nvh.api.server`` re-exports every name, so
``from nvh.api.server import require_auth`` and ``server.get_engine`` keep
working, and a monkeypatch on either module's attribute still takes effect
where that module's routes look the name up.

The engine itself stays on the server module (``nvh.api.server._engine``):
its lifespan assigns it there and the test-suite swaps it there, so
:func:`get_engine` reads it from the server module at call time instead of
keeping a copy that those writers would miss.
"""

from __future__ import annotations

import hmac
import os
import sys
from typing import TYPE_CHECKING, Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

if TYPE_CHECKING:
    from nvh.core.engine import Engine

__all__ = [
    "_bearer_scheme",
    "_get_council_api_key",
    "_get_current_user_optional",
    "_response_envelope",
    "auth_mode",
    "get_engine",
    "key_required",
    "require_auth",
]

# ---------------------------------------------------------------------------
# Optional API key authentication
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer(auto_error=False)


def _get_council_api_key() -> str | None:
    """Return the HIVE_API_KEY env var if set, otherwise None (open mode)."""
    return os.environ.get("HIVE_API_KEY") or None


async def _users_registered() -> bool:
    """Whether the users table has rows — the first tier of ``require_auth``.

    A database that is not initialised, or any other lookup failure, counts
    as "no users", exactly as ``_get_current_user_optional`` treats it.
    """
    from nvh.auth.auth import get_user_count

    try:
        return await get_user_count() > 0
    except Exception:
        return False


async def _get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None,
    x_key: str | None,
) -> Any | None:
    """Try to resolve a User from a bearer token if user auth is enabled.

    Returns the User object on success, None if user auth is not active or the
    token does not resolve to a user-based token.
    """
    from nvh.auth.auth import get_user_by_token

    if not await _users_registered():
        return None  # no users registered — fall through to API key mode

    token: str | None = None
    if credentials:
        token = credentials.credentials
    elif x_key:
        token = x_key

    if token is None:
        return None

    return await get_user_by_token(token)


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> Any:
    """FastAPI dependency that enforces authentication.

    Priority:
    1. User-based auth (when users table has entries): validates API tokens
       created via the /v1/auth/* endpoints.
    2. Simple HIVE_API_KEY env var (single-user, legacy).
    3. Open/local mode — whenever HIVE_API_KEY is unset: a request without a
       valid user token is admitted even when users exist (users add a
       credential; only the key closes the API). SECURITY.md documents this
       and ``_auth_open_mode`` must keep mirroring it.

    Accepts credentials via:
    - ``Authorization: Bearer <token>`` header
    - ``X-Hive-API-Key: <key>`` header
    """
    x_key = request.headers.get("X-Hive-API-Key")

    # Try user-based auth first
    user = await _get_current_user_optional(credentials, x_key)
    if user is not None:
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User account is disabled.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    # Fall back to simple API key
    expected = _get_council_api_key()
    if expected is None:
        # Open mode — no auth required
        return None

    # Check Bearer token
    if credentials and hmac.compare_digest(credentials.credentials, expected):
        return None

    # Check X-Hive-API-Key header
    if x_key and hmac.compare_digest(x_key, expected):
        return None

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "Authentication required. Provide a valid API key via "
            "'Authorization: Bearer <key>' or 'X-Hive-API-Key: <key>' header."
        ),
        headers={"WWW-Authenticate": "Bearer"},
    )


async def auth_mode() -> str:
    """Which tier of :func:`require_auth` applies first: ``users``, ``key`` or ``open``.

    ``users`` when the users table has rows (per-user tokens are checked
    first), else ``key`` when ``HIVE_API_KEY`` is set, else ``open``. The
    predicates are the ones ``require_auth`` itself runs, in its order, so a
    client (``GET /v1/health``, ``nvh launch``) can tell which credential it
    must present. Note SECURITY.md's rule: ``users`` without the key is still
    open mode — a token-less request is admitted — because only the key closes
    the API.
    """
    if await _users_registered():
        return "users"
    return "key" if _get_council_api_key() is not None else "open"


def key_required() -> bool:
    """Whether a token-less request is refused: exactly "``HIVE_API_KEY`` is set".

    The inverse of open mode as SECURITY.md defines it, and the one fact
    :func:`auth_mode` cannot carry — it answers ``users`` whether or not the
    key is also set, and ``users`` without the key still admits a bare
    request — so ``GET /v1/health`` discloses both, and the server's
    ``_auth_open_mode`` is ``not key_required()``.
    """
    return _get_council_api_key() is not None


# ---------------------------------------------------------------------------
# Response envelope
# ---------------------------------------------------------------------------

def _response_envelope(data: Any) -> dict[str, Any]:
    return {"status": "success", "data": data}


# ---------------------------------------------------------------------------
# Shared engine instance
# ---------------------------------------------------------------------------

def get_engine() -> Engine:
    """The Engine the server lifespan built (``nvh.api.server._engine``).

    Read from the server module at call time: its lifespan assigns the global
    there (``global _engine``) and tests swap it there
    (``server_module._engine = engine``), so a copy held here would miss both.
    A server module that was never imported has never built an engine.
    """
    server = sys.modules.get("nvh.api.server")
    engine = getattr(server, "_engine", None) if server is not None else None
    if engine is None:
        raise RuntimeError("Engine not initialized. Server startup failed.")
    return engine
