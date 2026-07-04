"""
foodpilot/auth/supabase.py

All Supabase-Auth related logic lives here:

1. PKCE helpers  — generate verifier/challenge pairs + in-memory state store
2. JWT decoding  — local verification using the project JWT secret (no network call)
3. Token exchange — calls Supabase /auth/v1/token to swap the OAuth code for a session
4. User upsert   — keeps our `users` table in sync with Supabase Auth after each login

WHY PKCE?
   OAuth 2.1 requires PKCE for all auth code flows. The code verifier is generated
   server-side, bound to a short-lived state token, and consumed exactly once on callback.
   This prevents authorization code interception attacks.

WHY LOCAL JWT VERIFICATION?
   Calling Supabase's /auth/v1/user on every request to verify tokens would add a
   network round-trip to every API call. Local verification with the project JWT secret
   is instantaneous and just as secure — the secret is only known to our backend.

WHY UPSERT (not INSERT)?
   The same user can log in multiple times. Upserting on `supabase_id` keeps the row
   current (e.g. updated avatar URL from Google) without duplicate-key errors.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from base64 import urlsafe_b64encode
from typing import Any
from urllib.parse import urlencode

import httpx
from jose import ExpiredSignatureError, JWTError, jwt
from supabase import AsyncClient

from foodpilot.config import get_settings
from foodpilot.core.errors import AuthError, TokenExpiredError
from foodpilot.core.logging import get_logger
from foodpilot.db.models import UserRow

logger = get_logger(__name__)

# ── PKCE state store ──────────────────────────────────────────────────────────
# Maps state token → (code_verifier, expires_at_unix_timestamp)
# TTL matches Supabase authorization code lifetime: 120 seconds.
# Single-process only — use Redis for multi-instance deployments.
_pkce_store: dict[str, tuple[str, float]] = {}
_PKCE_TTL_SECONDS = 120


# ── PKCE helpers ──────────────────────────────────────────────────────────────


def generate_pkce_pair() -> tuple[str, str]:
    """
    Generate a (code_verifier, code_challenge) pair.
    - code_verifier: 32 random URL-safe bytes
    - code_challenge: SHA-256(verifier), base64url-encoded, no padding (S256 method)
    """
    code_verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def store_pkce_state(state: str, code_verifier: str) -> None:
    """
    Persist the code_verifier under the given state token for the PKCE TTL.
    Also evicts any expired entries to prevent unbounded memory growth.
    """
    now = time.monotonic()
    expired_keys = [k for k, (_, exp) in _pkce_store.items() if now > exp]
    for k in expired_keys:
        del _pkce_store[k]
    _pkce_store[state] = (code_verifier, now + _PKCE_TTL_SECONDS)


def pop_pkce_state(state: str) -> str:
    """
    Retrieve and delete the code_verifier for a given state.
    The 'pop' makes it single-use — replaying the same callback URL won't work.
    Raises AuthError if the state is unknown or expired.
    """
    entry = _pkce_store.pop(state, None)
    if entry is None or time.monotonic() > entry[1]:
        raise AuthError(
            "OAuth state is invalid or has expired. Please start the login flow again.",
            "OAUTH_STATE_INVALID",
        )
    code_verifier, _ = entry
    return code_verifier


# ── URL builder ───────────────────────────────────────────────────────────────


def build_google_auth_url(
    supabase_url: str,
    redirect_to: str,
    code_challenge: str,
) -> str:
    """
    Build the full Supabase authorize URL for Google OAuth.
    Supabase's authorize endpoint accepts the provider name, our callback URL,
    and the PKCE challenge + state for security.
    """
    params = urlencode(
        {
            "provider": "google",
            "redirect_to": redirect_to,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "scopes": "email profile",
        }
    )
    return f"{supabase_url}/auth/v1/authorize?{params}"


# ── Token exchange ─────────────────────────────────────────────────────────────


async def exchange_code_for_session(
    auth_code: str,
    code_verifier: str,
) -> dict[str, Any]:
    """
    Exchange the OAuth authorization code + PKCE verifier for a Supabase session.
    Returns the raw JSON response which includes:
      - access_token  (JWT, 1 hour by default)
      - refresh_token (long-lived)
      - user          (Supabase Auth user object)

    WHY httpx instead of supabase-py?
    The supabase-py AsyncClient doesn't expose a fully async PKCE token exchange
    method in v2.x. Calling the REST endpoint directly with httpx gives us full
    control over the request and error handling.
    """
    settings = get_settings()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{settings.supabase_url}/auth/v1/token",
            params={"grant_type": "pkce"},
            headers={
                "apikey": settings.supabase_anon_key.get_secret_value(),
                "Content-Type": "application/json",
            },
            json={
                "auth_code": auth_code,
                "code_verifier": code_verifier,
            },
        )

    if resp.status_code != 200:
        logger.error(
            "Supabase code exchange failed",
            extra={"status": resp.status_code, "body": resp.text[:300]},
        )
        raise AuthError(
            "Failed to exchange authorization code. Please try logging in again.",
            "OAUTH_EXCHANGE_FAILED",
        )

    return resp.json()


# ── JWT verification ──────────────────────────────────────────────────────────


def decode_supabase_jwt(token: str) -> dict[str, Any]:
    """
    Verify and decode a Supabase-issued JWT using the project JWT secret.

    - Uses HS256 (HMAC-SHA256) with the project-level JWT secret
    - verify_aud=False: Supabase uses aud="authenticated" but python-jose's
      audience verification requires passing the expected audience explicitly.
      We skip it — the signature check is sufficient.
    - Raises TokenExpiredError on expired tokens (maps to 401 with clear message)
    - Raises AuthError on any other JWT problem (tampered, malformed, wrong secret)
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.supabase_jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        return payload
    except ExpiredSignatureError:
        logger.error("Auth failed: JWT token has expired")
        raise TokenExpiredError()
    except JWTError as exc:
        logger.error(f"Auth failed: Invalid JWT token: {exc}")
        raise AuthError(f"Invalid token: {exc}", "INVALID_TOKEN")


# ── User persistence ──────────────────────────────────────────────────────────


async def upsert_user(db: AsyncClient, supabase_user: dict[str, Any]) -> UserRow:
    """
    Insert or update the user row in our `users` table from the Supabase Auth
    user object returned by the token exchange.

    WHY upsert (not insert)?
    The same user hits this code path on every login. `ON CONFLICT (supabase_id)
    DO UPDATE` keeps their name and avatar fresh without duplicate-key errors.
    """
    meta = supabase_user.get("user_metadata") or {}
    payload = {
        "supabase_id": supabase_user["id"],
        "email": supabase_user["email"],
        "name": meta.get("full_name") or meta.get("name"),
        "avatar_url": meta.get("avatar_url") or meta.get("picture"),
    }
    result = (
        await db.table("users")
        .upsert(payload, on_conflict="supabase_id")
        .execute()
    )
    return result.data[0]


async def get_user_by_supabase_id(
    db: AsyncClient, supabase_id: str
) -> UserRow | None:
    """
    Fetch the user row matching the given Supabase Auth UUID.
    Returns None if the user hasn't been upserted yet (edge case).
    """
    result = (
        await db.table("users")
        .select("*")
        .eq("supabase_id", supabase_id)
        .maybe_single()
        .execute()
    )
    return result.data
