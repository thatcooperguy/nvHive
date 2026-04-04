"""Authentication and authorization logic."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

from passlib.context import CryptContext
from sqlalchemy import func, select

from nvh.auth.models import APIToken, User
from nvh.storage.repository import get_session

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Token prefix for easy identification
_TOKEN_PREFIX = "hive_"


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    return pwd_context.hash(password)


def verify_password(password: str, hash: str) -> bool:
    """Verify a plaintext password against its stored hash."""
    return pwd_context.verify(password, hash)


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def create_api_token() -> tuple[str, str]:
    """Generate a new API token. Returns (raw_token, hashed_token).

    Token format: hive_<32 random url-safe chars>
    The raw token is shown once; only the SHA-256 hash is stored.
    """
    raw = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of a token string."""
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

_VALID_ROLES = {"admin", "user", "viewer"}


async def create_user(
    username: str,
    password: str,
    role: str = "user",
    email: str | None = None,
) -> User:
    """Create and persist a new user. Raises ValueError on invalid input."""
    # Input validation
    username = username.strip()
    if not username or len(username) < 2:
        raise ValueError("Username must be at least 2 characters.")
    if len(username) > 64:
        raise ValueError("Username must be 64 characters or fewer.")
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if role not in _VALID_ROLES:
        raise ValueError(
            f"Invalid role '{role}'. Must be one of: "
            f"{', '.join(sorted(_VALID_ROLES))}"
        )

    async with get_session() as session:
        existing = await session.execute(
            select(User).where(User.username == username)
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError(f"Username '{username}' is already taken.")

        user = User(
            username=username,
            email=email,
            password_hash=hash_password(password),
            role=role,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def get_user_count() -> int:
    """Return the total number of users in the database."""
    async with get_session() as session:
        result = await session.execute(select(func.count(User.id)))
        return result.scalar_one() or 0


async def authenticate_user(username: str, password: str) -> User | None:
    """Verify credentials and return the User on success, or None on failure.

    Uses constant-time comparison to prevent timing-based username enumeration.
    """
    # Dummy hash for constant-time comparison when user is not found.
    # This prevents attackers from determining if a username exists
    # by measuring response time differences.
    _dummy_hash = (
        "$2b$12$LJ3m4ys3Rl0t3XEbLUMgruY5bBIxmU3MpY5sDGOXF/hdsCP3Tqo.i"
    )

    async with get_session() as session:
        result = await session.execute(
            select(User).where(
                User.username == username,
                User.is_active == True,  # noqa: E712
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            # Perform a dummy verify to equalize timing
            pwd_context.verify("dummy", _dummy_hash)
            return None
        if not verify_password(password, user.password_hash):
            return None

        # Update last_login
        user.last_login = datetime.now(UTC)
        await session.commit()
        return user


async def get_user_by_id(user_id: str) -> User | None:
    """Fetch a user by their UUID."""
    async with get_session() as session:
        return await session.get(User, user_id)


async def get_user_by_token(token: str) -> User | None:
    """Look up the user that owns the given raw API token, if it is valid."""
    token_hash = hash_token(token)
    async with get_session() as session:
        result = await session.execute(
            select(APIToken).where(
                APIToken.token_hash == token_hash,
                APIToken.is_active == True,  # noqa: E712
            )
        )
        api_token = result.scalar_one_or_none()
        if api_token is None:
            return None

        # Check expiry
        if api_token.expires_at is not None and api_token.expires_at < datetime.now(UTC):
            return None

        # Update last_used timestamp
        api_token.last_used = datetime.now(UTC)
        await session.commit()

        # Return owning user
        return await session.get(User, api_token.user_id)


# ---------------------------------------------------------------------------
# API token management
# ---------------------------------------------------------------------------

async def create_token_for_user(
    user_id: str,
    name: str,
    scopes: str = "query,council,compare",
    expires_at: datetime | None = None,
) -> tuple[str, APIToken]:
    """Create an API token for a user.

    Returns (raw_token, token_record). The raw token is only returned once
    and must be shown to the user immediately; only the hash is stored.
    """
    raw, hashed = create_api_token()
    async with get_session() as session:
        token = APIToken(
            user_id=user_id,
            name=name,
            token_hash=hashed,
            scopes=scopes,
            expires_at=expires_at,
        )
        session.add(token)
        await session.commit()
        await session.refresh(token)
        return raw, token


async def list_user_tokens(user_id: str) -> list[APIToken]:
    """Return all active API tokens for the given user."""
    async with get_session() as session:
        result = await session.execute(
            select(APIToken)
            .where(APIToken.user_id == user_id, APIToken.is_active == True)  # noqa: E712
            .order_by(APIToken.created_at.desc())
        )
        return list(result.scalars().all())


async def revoke_token(token_id: str) -> bool:
    """Deactivate an API token. Returns True if found and revoked."""
    async with get_session() as session:
        token = await session.get(APIToken, token_id)
        if token is None:
            return False
        token.is_active = False
        await session.commit()
        return True
