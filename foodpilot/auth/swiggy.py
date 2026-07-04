"""
foodpilot/auth/swiggy.py

Swiggy OAuth 2.1 + PKCE logic.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW SWIGGY OAUTH DIFFERS FROM GOOGLE OAUTH (M2)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- No Supabase in the middle — we talk directly to mcp.swiggy.com
- Dynamic Client Registration (DCR, RFC 7591) — Swiggy auto-issues us
  a client_id on first use. We cache it in-memory; set SWIGGY_CLIENT_ID
  in .env after first run to skip re-registration on server restart.
- The token is NOT returned to the client — it's stored encrypted in DB
  and injected server-side into every Claude MCP call.
- No refresh tokens in Swiggy v1.0 — when the 5-day token expires,
  the user must re-run the full PKCE flow (/auth/swiggy/connect again).

PKCE STATE STORE FOR SWIGGY:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
state → (code_verifier, user_id, expires_at)

We carry user_id in the state because the Swiggy callback is a browser
redirect — it has no Authorization header. We need to know which FoodPilot
user to associate the Swiggy token with.
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from foodpilot.config import get_settings
from foodpilot.auth.supabase import generate_pkce_pair  # same PKCE math
from foodpilot.core.errors import AuthError, SwiggyAuthError
from foodpilot.core.logging import get_logger

logger = get_logger(__name__)

# ── Swiggy PKCE state store ───────────────────────────────────────────────────
# state → (code_verifier, user_id, expires_at_monotonic)
# Separate from the Google PKCE store — each OAuth flow has its own state space.
_swiggy_state: dict[str, tuple[str, str, float]] = {}
_PKCE_TTL_SECONDS = 120  # matches Swiggy auth code lifetime

# ── Dynamic Client Registration cache ────────────────────────────────────────
# Swiggy client_id obtained via DCR. Cached in memory across requests.
# If SWIGGY_CLIENT_ID is set in .env, DCR is skipped entirely.
_cached_client_id: str | None = None

SWIGGY_BASE_URL = "https://mcp.swiggy.com"


# ── Dynamic Client Registration ───────────────────────────────────────────────


async def get_or_register_client() -> str:
    """
    Return the Swiggy OAuth client_id.

    Order of precedence:
    1. SWIGGY_CLIENT_ID env var (set this after first run to skip DCR)
    2. In-memory cache from a previous DCR in this process
    3. Fresh DCR call to mcp.swiggy.com/auth/register

    WHY DCR?
    Swiggy uses RFC 7591 Dynamic Client Registration — there is no developer
    portal where you manually create an OAuth app. Your MCP client registers
    itself automatically. This is the same mechanism Claude Desktop, Cursor,
    and ChatGPT use when they add Swiggy MCP.
    """
    global _cached_client_id

    # Priority 1: env var (stable across restarts)
    settings = get_settings()
    if settings.swiggy_client_id:
        return settings.swiggy_client_id

    # Priority 2: in-memory cache
    if _cached_client_id:
        return _cached_client_id

    # Priority 3: register fresh
    logger.info("Performing Swiggy Dynamic Client Registration")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{SWIGGY_BASE_URL}/auth/register",
            json={
                "client_name": "FoodPilot AI",
                "redirect_uris": [settings.swiggy_redirect_uri],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",  # public client — PKCE handles security
            },
        )

    if resp.status_code not in (200, 201):
        logger.error(
            "Swiggy DCR failed",
            extra={"status": resp.status_code, "body": resp.text[:300]},
        )
        raise SwiggyAuthError(
            f"Failed to register with Swiggy OAuth server (HTTP {resp.status_code}). "
            "Please check that the server is reachable."
        )

    data = resp.json()
    client_id = data.get("client_id")
    if not client_id:
        raise SwiggyAuthError("Swiggy DCR response missing client_id.")

    _cached_client_id = client_id
    logger.info(
        "Swiggy client registered — add SWIGGY_CLIENT_ID to .env to skip DCR on restart",
        extra={"client_id": client_id},
    )
    return client_id


# ── PKCE state management ─────────────────────────────────────────────────────


def store_swiggy_pkce_state(state: str, code_verifier: str, user_id: str) -> None:
    """
    Save the PKCE verifier + the FoodPilot user_id under the state token.
    The user_id is needed in the callback (which has no auth header) to know
    which user's swiggy_tokens row to create.
    """
    now = time.monotonic()
    # Purge expired entries
    expired = [k for k, (_, _, exp) in _swiggy_state.items() if now > exp]
    for k in expired:
        del _swiggy_state[k]
    _swiggy_state[state] = (code_verifier, user_id, now + _PKCE_TTL_SECONDS)


def pop_swiggy_pkce_state(state: str) -> tuple[str, str]:
    """
    Retrieve and delete (code_verifier, user_id) for a given state.
    Single-use — replaying the callback URL fails after the first use.
    Raises AuthError if state is unknown or expired.
    """
    entry = _swiggy_state.pop(state, None)
    if entry is None or time.monotonic() > entry[2]:
        raise AuthError(
            "Swiggy OAuth state is invalid or expired. Please start the connection flow again.",
            "SWIGGY_OAUTH_STATE_INVALID",
        )
    code_verifier, user_id, _ = entry
    return code_verifier, user_id


# ── URL builder ───────────────────────────────────────────────────────────────


def build_swiggy_auth_url(client_id: str, code_challenge: str, state: str) -> str:
    """
    Build the Swiggy authorization URL that the user's browser will be redirected to.
    The user will see a Swiggy phone + OTP login screen in the browser.
    """
    settings = get_settings()
    params = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": settings.swiggy_redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": "mcp:tools",
        }
    )
    return f"{SWIGGY_BASE_URL}/auth/authorize?{params}"


# ── Token exchange ─────────────────────────────────────────────────────────────


async def exchange_swiggy_code(
    auth_code: str,
    code_verifier: str,
    client_id: str,
) -> tuple[str, str | None, datetime]:
    """
    Exchange the Swiggy authorization code + PKCE verifier for an access token.

    Returns: (access_token, scope, expires_at)

    WHY NOT SUPABASE HERE?
    Swiggy is a completely independent OAuth server — Supabase has no role.
    We call mcp.swiggy.com/auth/token directly with httpx.

    TOKEN LIFETIME:
    Swiggy docs say expires_in = 432000 seconds = 5 days.
    We calculate expires_at = now + expires_in and store it in the DB
    so we can proactively check expiry before injecting into MCP calls.
    """
    settings = get_settings()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{SWIGGY_BASE_URL}/auth/token",
            headers={"Content-Type": "application/json"},
            json={
                "grant_type": "authorization_code",
                "code": auth_code,
                "code_verifier": code_verifier,
                "client_id": client_id,
                "redirect_uri": settings.swiggy_redirect_uri,
            },
        )

    if resp.status_code != 200:
        logger.error(
            "Swiggy token exchange failed",
            extra={"status": resp.status_code, "body": resp.text[:300]},
        )
        raise SwiggyAuthError(
            "Failed to exchange Swiggy authorization code. Please try connecting again."
        )

    data = resp.json()
    access_token = data.get("access_token")
    if not access_token:
        raise SwiggyAuthError("Swiggy token exchange response missing access_token.")

    expires_in = data.get("expires_in", 432000)  # default 5 days per docs
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    scope = data.get("scope")

    logger.info("Swiggy token obtained", extra={"expires_at": expires_at.isoformat()})
    return access_token, scope, expires_at


# ── Token revocation ──────────────────────────────────────────────────────────


async def revoke_swiggy_token(access_token: str) -> None:
    """
    Revoke the Swiggy session server-side.
    Called when the user clicks 'disconnect Swiggy' in the app.
    Non-fatal if Swiggy returns an error — we delete our local record either way.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{SWIGGY_BASE_URL}/auth/logout",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        logger.info("Swiggy session revoked server-side")
    except Exception as exc:
        # Log but don't raise — local token deletion still proceeds
        logger.warning("Swiggy logout call failed (non-fatal)", extra={"error": str(exc)})
