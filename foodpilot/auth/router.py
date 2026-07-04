"""
foodpilot/auth/router.py

Four endpoints that form the complete Google OAuth + session lifecycle:

  GET  /auth/google/login   → kick off the OAuth flow
  GET  /auth/callback       → Supabase redirects here after Google consent
  GET  /auth/me             → return the logged-in user's profile
  POST /auth/logout         → revoke the Supabase session

WHY THESE FOUR?
  They cover the entire auth lifecycle: start → complete → inspect → end.
  All other routes just need the `get_current_user` dependency — they never
  touch raw tokens or OAuth state.

FLOW DIAGRAM:
  Browser → GET /auth/google/login
              ↓ (302 redirect)
          Supabase → Google consent
              ↓ (Google redirects to Supabase)
          Supabase → GET /auth/callback?code=...&state=...
              ↓ (exchange code + PKCE verifier for session)
          Response: { access_token, refresh_token, user }
              ↓
  Browser stores tokens, sends Authorization: Bearer <access_token> on all future requests
"""
import secrets

from fastapi import APIRouter, Depends, Query, Cookie
from fastapi.responses import JSONResponse, RedirectResponse
from supabase import AsyncClient

from foodpilot.auth.supabase import (
    build_google_auth_url,
    decode_supabase_jwt,
    exchange_code_for_session,
    generate_pkce_pair,
    pop_pkce_state,
    store_pkce_state,
    upsert_user,
)
from foodpilot.config import get_settings
from foodpilot.core.errors import AuthError
from foodpilot.core.logging import get_logger
from foodpilot.db.models import UserRow
from foodpilot.dependencies import get_current_user, get_database

logger = get_logger(__name__)
router = APIRouter(tags=["Auth"])


# ── 1. Start Google OAuth ─────────────────────────────────────────────────────


@router.get(
    "/google/login",
    summary="Start Google OAuth login",
    description=(
        "Generates a PKCE verifier+challenge pair, sets the verifier as an HttpOnly cookie, "
        "and redirects the user to Supabase's Google consent screen."
    ),
)
async def google_login() -> RedirectResponse:
    settings = get_settings()

    # Generate PKCE pair
    code_verifier, code_challenge = generate_pkce_pair()

    auth_url = build_google_auth_url(
        supabase_url=settings.supabase_url,
        redirect_to=settings.supabase_auth_redirect_url,
        code_challenge=code_challenge,
    )

    logger.info("Google OAuth flow started")
    response = RedirectResponse(url=auth_url, status_code=302)
    response.set_cookie(
        key="pkce_verifier",
        value=code_verifier,
        httponly=True,
        samesite="lax",
        max_age=300, # 5 minutes
    )
    return response


# ── 2. OAuth callback ─────────────────────────────────────────────────────────


@router.get(
    "/callback",
    summary="Supabase OAuth callback",
    description=(
        "Supabase redirects here after Google consent. "
        "Exchanges the authorization code + stored PKCE verifier for a session, "
        "upserts the user into our database, and returns the access + refresh tokens."
    ),
)
async def auth_callback(
    code: str = Query(..., description="Authorization code from Supabase"),
    pkce_verifier: str | None = Cookie(None, description="PKCE verifier from cookie"),
    db: AsyncClient = Depends(get_database),
) -> RedirectResponse:
    if not pkce_verifier:
        raise AuthError("OAuth session expired or cookies blocked. Please try again.", "OAUTH_NO_VERIFIER")
    
    code_verifier = pkce_verifier

    # Exchange code for a Supabase session
    session = await exchange_code_for_session(
        auth_code=code,
        code_verifier=code_verifier,
    )

    # Supabase returns the full user object inside the session
    supabase_user = session.get("user")
    if not supabase_user:
        raise AuthError("No user returned from Supabase session exchange.", "OAUTH_NO_USER")

    # Sync user into our application database
    user = await upsert_user(db, supabase_user)

    logger.info(
        "User logged in via Google",
        extra={"user_id": user["id"], "email": user["email"]},
    )

    # Redirect back to the Lovable frontend
    # Lovable usually runs on 8080 or 8081
    frontend_url = "http://localhost:8080"
    response = RedirectResponse(url=frontend_url, status_code=302)
    response.set_cookie(
        key="access_token",
        value=session["access_token"],
        httponly=True,  # Protect against XSS
        secure=False,   # Fine for localhost development
        samesite="lax",
        max_age=3600 * 24 * 7, # 7 days
    )
    response.delete_cookie("pkce_verifier")
    return response


# ── 3. Current user profile ───────────────────────────────────────────────────


@router.get(
    "/me",
    summary="Get current user profile",
    description=(
        "Returns the profile of the currently authenticated user. "
        "Requires a valid Supabase access token in the Authorization header. "
        "This is the canonical endpoint to verify a token is working."
    ),
)
async def get_me(current_user: UserRow = Depends(get_current_user)) -> JSONResponse:
    return JSONResponse(
        content={
            "id": current_user["id"],
            "email": current_user["email"],
            "name": current_user["name"],
            "avatar_url": current_user["avatar_url"],
            "created_at": str(current_user["created_at"]),
        }
    )


# ── 4. Logout ─────────────────────────────────────────────────────────────────


@router.post(
    "/logout",
    summary="Log out current user",
    description=(
        "Revokes the current Supabase session server-side. "
        "The client should discard the access + refresh tokens after calling this."
    ),
)
async def logout(
    current_user: UserRow = Depends(get_current_user),
    db: AsyncClient = Depends(get_database),
) -> JSONResponse:
    # Supabase sign-out via the auth client
    # This invalidates the session on Supabase's side
    try:
        await db.auth.sign_out()
    except Exception as exc:
        # Sign-out failure is non-fatal — the token will expire naturally
        logger.warning("Supabase sign-out error (non-fatal)", extra={"error": str(exc)})

    logger.info("User logged out", extra={"user_id": current_user["id"]})
    
    response = JSONResponse(content={"message": "Logged out successfully."})
    response.delete_cookie("access_token", httponly=True, secure=False, samesite="lax")
    return response
