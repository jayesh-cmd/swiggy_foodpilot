"""
foodpilot/dependencies.py

FastAPI dependency injection wiring.

These functions are injected via Depends() in route handlers.
They abstract infrastructure concerns (DB, auth) so route code only
deals with business logic.

get_database       → shared Supabase client
get_current_user   → verified UserRow (raises 401 if unauthenticated)

WHY A SEPARATE dependencies.py?
  Route modules (auth/router.py, chat/router.py) all need the same
  dependencies. Centralising them here avoids circular imports and
  keeps route files focused on request/response logic only.

HOW get_current_user WORKS:
  1. Extracts the Bearer token from the Authorization header
  2. Decodes the JWT locally using the Supabase JWT secret (no network call)
  3. Reads the Supabase user UUID (sub claim) from the payload
  4. Queries our `users` table to get the full UserRow
  5. Injects the UserRow into the route handler

  Any route that declares `current_user: UserRow = Depends(get_current_user)`
  is automatically protected — no token → 401, expired token → 401, unknown
  user → 401. The route handler never sees raw JWT claims.
"""
from __future__ import annotations

from fastapi import Depends, Header, Cookie
from supabase import AsyncClient

from foodpilot.auth.supabase import get_user_by_supabase_id
from foodpilot.core.errors import AuthError
from foodpilot.core.logging import get_logger
from foodpilot.db.client import get_db
from foodpilot.db.models import UserRow

logger = get_logger(__name__)


async def get_database(db: AsyncClient = Depends(get_db)) -> AsyncClient:
    """Provide the Supabase AsyncClient to route handlers."""
    return db


async def get_current_user(
    authorization: str | None = Header(None, description="Bearer <supabase_access_token>"),
    access_token: str | None = Cookie(None, description="Fallback cookie for browser clients"),
    db: AsyncClient = Depends(get_database),
) -> UserRow:
    """
    Authenticate the request and return the verified user.

    Raises AuthError (→ HTTP 401) if:
    - Authorization header is missing or malformed
    - JWT signature is invalid
    - JWT is expired
    - User is not found in our database
    """
    # ── Parse token from header or cookie ─────────────────────────────────────
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
    elif access_token:
        token = access_token.strip()

    if not token:
        logger.error(f"Auth failed: Missing token. Header: {authorization}, Cookie: {access_token}")
        raise AuthError("Not authenticated. Missing Bearer token or cookie.", "EMPTY_TOKEN")

    # ── Verify JWT via Supabase API ───────────────────────────────────────────
    try:
        user_resp = await db.auth.get_user(token)
        if not user_resp or not user_resp.user:
            raise ValueError("No user returned")
        supabase_id = user_resp.user.id
    except Exception as exc:
        logger.error(f"Auth failed: Invalid JWT token or expired: {exc}")
        raise AuthError(f"Invalid token: {exc}", "INVALID_TOKEN")

    # ── Look up user in our database ──────────────────────────────────────────
    user = await get_user_by_supabase_id(db, supabase_id)
    if user is None:
        logger.error(f"Auth failed: User {supabase_id} not found in database")
        raise AuthError(
            "Authenticated user not found in the application database. "
            "Please complete sign-in via /auth/google/login.",
            "USER_NOT_FOUND",
        )

    return user
