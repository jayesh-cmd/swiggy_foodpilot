"""
foodpilot/auth/swiggy_router.py

Four endpoints for the Swiggy account linking lifecycle.

  GET  /auth/swiggy/connect      → start Swiggy PKCE flow (user must be logged in)
  GET  /auth/swiggy/callback     → Swiggy redirects here after phone+OTP
  GET  /auth/swiggy/status       → is Swiggy linked? when does the token expire?
  DELETE /auth/swiggy/disconnect → unlink Swiggy account

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHY IS /auth/swiggy/connect PROTECTED (requires Google login)?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
We need to know WHO is connecting Swiggy so we can save the token against
the right user_id in swiggy_tokens. Requiring a logged-in user at /connect
time lets us embed user_id in the PKCE state, which survives the browser
redirect to Swiggy and back.

WHY IS /auth/swiggy/callback UNPROTECTED?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Because it's a browser redirect from Swiggy — the browser does not send
our Authorization header. The user_id is recovered from the PKCE state
store instead (it was embedded there at /connect time).
"""
import secrets

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, RedirectResponse
from supabase import AsyncClient

from foodpilot.auth.supabase import generate_pkce_pair
from foodpilot.auth.swiggy import (
    build_swiggy_auth_url,
    exchange_swiggy_code,
    get_or_register_client,
    pop_swiggy_pkce_state,
    revoke_swiggy_token,
    store_swiggy_pkce_state,
)
from foodpilot.core.logging import get_logger
from foodpilot.db.models import UserRow
from foodpilot.dependencies import get_current_user, get_database
from foodpilot.swiggy.token_store import (
    delete_swiggy_token,
    get_decrypted_token,
    get_swiggy_token_row,
    get_token_expiry_status,
    save_swiggy_token,
)

logger = get_logger(__name__)
router = APIRouter(tags=["Swiggy Auth"])


# ── 1. Start Swiggy OAuth ─────────────────────────────────────────────────────


@router.get(
    "/connect",
    summary="Link your Swiggy account",
    description=(
        "Starts the Swiggy OAuth 2.1 + PKCE flow. "
        "Redirects to Swiggy's consent screen (phone + OTP). "
        "Requires an active FoodPilot session (Google login first)."
    ),
)
async def swiggy_connect(
    current_user: UserRow = Depends(get_current_user),
) -> RedirectResponse:
    # Get or register our OAuth client_id with Swiggy
    client_id = await get_or_register_client()

    # Generate fresh PKCE pair and state for this login attempt
    state = secrets.token_urlsafe(16)
    code_verifier, code_challenge = generate_pkce_pair()

    # Embed user_id in state store — recovered in the callback
    store_swiggy_pkce_state(state, code_verifier, current_user["id"])

    auth_url = build_swiggy_auth_url(
        client_id=client_id,
        code_challenge=code_challenge,
        state=state,
    )

    logger.info(
        "Swiggy OAuth flow started",
        extra={"user_id": current_user["id"], "state_prefix": state[:8]},
    )
    return RedirectResponse(url=auth_url, status_code=302)


# ── 2. OAuth callback ─────────────────────────────────────────────────────────


@router.get(
    "/callback",
    summary="Swiggy OAuth callback (internal)",
    description=(
        "Swiggy redirects here after phone + OTP. "
        "Exchanges the code, encrypts the token, and saves it to the database. "
        "NOT called by users directly."
    ),
)
async def swiggy_callback(
    code: str = Query(..., description="Authorization code from Swiggy"),
    state: str = Query(..., description="CSRF state token from /connect"),
    db: AsyncClient = Depends(get_database),
) -> JSONResponse:
    # Recover code_verifier and user_id from the PKCE state store
    # This also validates and consumes the state (single-use)
    code_verifier, user_id = pop_swiggy_pkce_state(state)

    # Get our registered client_id (needed for token exchange)
    client_id = await get_or_register_client()

    # Exchange the authorization code for a Swiggy access token
    access_token, scope, expires_at = await exchange_swiggy_code(
        auth_code=code,
        code_verifier=code_verifier,
        client_id=client_id,
    )

    # Encrypt and persist the token in the database
    # From this point on, Claude can call Swiggy on behalf of this user
    await save_swiggy_token(
        db=db,
        user_id=user_id,
        access_token=access_token,
        expires_at=expires_at,
        scope=scope,
    )

    logger.info("Swiggy account linked", extra={"user_id": user_id})

    # Redirect back to the frontend so the user can continue using the app
    return RedirectResponse(url="http://localhost:8080", status_code=302)


# ── 3. Status ─────────────────────────────────────────────────────────────────


@router.get(
    "/status",
    summary="Check Swiggy account link status",
    description="Returns whether the current user has a linked Swiggy account and when the token expires.",
)
async def swiggy_status(
    current_user: UserRow = Depends(get_current_user),
    db: AsyncClient = Depends(get_database),
) -> JSONResponse:
    row = await get_swiggy_token_row(db, current_user["id"])

    if row is None:
        return JSONResponse(
            content={
                "linked": False,
                "message": "No Swiggy account linked. Visit /auth/swiggy/connect to get started.",
            }
        )

    expires_at_str = row["expires_at"]
    return JSONResponse(
        content={
            "linked": True,
            "expires_at": str(expires_at_str),
            "scope": row.get("scope"),
        }
    )


# ── 4. Disconnect ─────────────────────────────────────────────────────────────


@router.delete(
    "/disconnect",
    summary="Unlink Swiggy account",
    description=(
        "Revokes the Swiggy session server-side and deletes the stored token. "
        "After this, Claude cannot place orders until you reconnect via /auth/swiggy/connect."
    ),
)
async def swiggy_disconnect(
    current_user: UserRow = Depends(get_current_user),
    db: AsyncClient = Depends(get_database),
) -> JSONResponse:
    # Try to get the token so we can revoke it server-side
    row = await get_swiggy_token_row(db, current_user["id"])

    if row:
        try:
            from foodpilot.swiggy.token_store import decrypt_token
            plaintext_token = decrypt_token(row["access_token"])
            await revoke_swiggy_token(plaintext_token)
        except Exception as exc:
            # Revocation failure is non-fatal — we still delete locally
            logger.warning("Could not revoke Swiggy token server-side", extra={"error": str(exc)})

        await delete_swiggy_token(db, current_user["id"])

    logger.info("Swiggy account unlinked", extra={"user_id": current_user["id"]})
    return JSONResponse(
        content={"unlinked": True, "message": "Swiggy account disconnected."}
    )


@router.get(
    "/health",
    summary="Swiggy token health check",
    description=(
        "Returns the current Swiggy token status: linked, expired, or expiring soon. "
        "Use this to proactively show 'Reconnect Swiggy' banners in the UI. "
        "Does NOT make any calls to Swiggy — pure DB lookup."
    ),
)
async def swiggy_token_health(
    current_user: UserRow = Depends(get_current_user),
    db: AsyncClient = Depends(get_database),
) -> JSONResponse:
    """
    Proactive token health check — fast, DB-only.

    RESPONSE EXAMPLES:

    Token healthy (4.5 days left):
      {"linked": true, "expired": false, "expiring_soon": false,
       "hours_remaining": 108.0, "expires_at": "2026-07-07T10:00:00+00:00",
       "status": "ok"}

    Token expiring soon (< 24h left):
      {"linked": true, "expired": false, "expiring_soon": true,
       "hours_remaining": 6.5, "expires_at": "...",
       "status": "expiring_soon", "action": "Visit /auth/swiggy/connect to reconnect."}

    Token not linked:
      {"linked": false, "status": "not_linked",
       "action": "Visit /auth/swiggy/connect to link your Swiggy account."}

    Token expired:
      {"linked": true, "expired": true, "status": "expired",
       "action": "Visit /auth/swiggy/connect to reconnect."}
    """
    status = await get_token_expiry_status(db, current_user["id"])

    if not status["linked"]:
        return JSONResponse(content={
            **status,
            "status": "not_linked",
            "action": "Visit /auth/swiggy/connect to link your Swiggy account.",
        })

    if status["expired"]:
        return JSONResponse(content={
            **status,
            "status": "expired",
            "action": "Visit /auth/swiggy/connect to reconnect.",
        })

    if status["expiring_soon"]:
        return JSONResponse(content={
            **status,
            "status": "expiring_soon",
            "action": (
                f"Your session expires in {status['hours_remaining']:.0f}h. "
                "Visit /auth/swiggy/connect to reconnect before it expires."
            ),
        })

    return JSONResponse(content={**status, "status": "ok"})
