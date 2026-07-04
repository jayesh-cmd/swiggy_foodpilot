"""
foodpilot/swiggy/token_store.py

Encrypted storage and retrieval of Swiggy access tokens.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHY ENCRYPT THE TOKEN?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A Swiggy access token can place real food orders, check saved addresses,
and access personal order history. If the database is ever breached, a
plaintext token would be immediately exploitable.

Fernet (AES-128-CBC + HMAC-SHA256) encrypts the token so that:
- Even if someone reads the database row, they see random bytes
- They also need the TOKEN_ENCRYPTION_KEY (in env, not in DB)
- The HMAC prevents silent tampering — if someone modifies the ciphertext,
  decryption fails loudly rather than returning garbage

WHY FERNET (not bcrypt/argon2)?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
bcrypt/argon2 are ONE-WAY — you can verify but never recover the original.
We need to RECOVER the original token to inject it into every Claude MCP call.
Fernet is symmetric — encrypt with key → store → decrypt with same key → use.

TOKEN LIFECYCLE:
━━━━━━━━━━━━━━━━
- Swiggy tokens live 5 days (432,000 seconds) per official docs
- We store expires_at in the DB so we can proactively detect expiry
  before making an MCP call (instead of failing mid-conversation)
- On expiry, we delete the row and ask the user to reconnect
- There are NO refresh tokens in Swiggy v1.0 — re-auth = full PKCE flow again
"""
from __future__ import annotations

from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from supabase import AsyncClient

from foodpilot.config import get_settings
from foodpilot.core.errors import SwiggyNotLinkedError, SwiggyTokenExpiredError
from foodpilot.core.logging import get_logger
from foodpilot.db.models import SwiggyTokenRow

logger = get_logger(__name__)


# ── Encryption helpers ────────────────────────────────────────────────────────


def _get_fernet() -> Fernet:
    """
    Build a Fernet instance from the TOKEN_ENCRYPTION_KEY in settings.
    Fernet is instantiated fresh each call (cheap) so we never cache
    a stale key reference after a config reload.
    """
    settings = get_settings()
    return Fernet(settings.token_encryption_key.get_secret_value().encode())


def encrypt_token(plaintext_token: str) -> str:
    """
    Encrypt a Swiggy access token before writing to the database.
    Returns a URL-safe base64-encoded ciphertext string.
    """
    return _get_fernet().encrypt(plaintext_token.encode()).decode()


def decrypt_token(encrypted_token: str) -> str:
    """
    Decrypt a stored Swiggy access token for use in an MCP call.
    Raises SwiggyNotLinkedError if the ciphertext is corrupt/tampered.
    """
    try:
        return _get_fernet().decrypt(encrypted_token.encode()).decode()
    except InvalidToken as exc:
        # InvalidToken = wrong key, corrupted ciphertext, or tampered data
        logger.error("Failed to decrypt Swiggy token — possible data corruption", extra={"error": str(exc)})
        raise SwiggyNotLinkedError()


# ── Database CRUD ─────────────────────────────────────────────────────────────


async def save_swiggy_token(
    db: AsyncClient,
    user_id: str,
    access_token: str,
    expires_at: datetime,
    scope: str | None = None,
) -> None:
    """
    Encrypt the token and upsert it into the swiggy_tokens table.

    Uses ON CONFLICT (user_id) → UPDATE so re-linking Swiggy (after expiry)
    replaces the old token row rather than creating a duplicate.
    """
    encrypted = encrypt_token(access_token)
    payload = {
        "user_id": user_id,
        "access_token": encrypted,
        "expires_at": expires_at.isoformat(),
        "scope": scope,
    }
    await (
        db.table("swiggy_tokens")
        .upsert(payload, on_conflict="user_id")
        .execute()
    )
    logger.info("Swiggy token saved", extra={"user_id": user_id, "expires_at": expires_at.isoformat()})


async def get_swiggy_token_row(
    db: AsyncClient,
    user_id: str,
) -> SwiggyTokenRow | None:
    """
    Fetch the raw swiggy_tokens row for a user.
    Returns None if not linked. Does NOT decrypt — use get_decrypted_token for that.
    """
    result = (
        await db.table("swiggy_tokens")
        .select("*")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    return result.data


async def get_decrypted_token(db: AsyncClient, user_id: str) -> str:
    """
    Fetch, validate, and decrypt the Swiggy access token for a user.

    This is the function called before every Claude MCP call. It:
    1. Checks the token exists (raises SwiggyNotLinkedError if not)
    2. Checks the token hasn't expired (raises SwiggyTokenExpiredError if so)
    3. Decrypts and returns the plaintext Bearer token

    Proactively checking expiry here (instead of waiting for a 401 from Swiggy)
    means the user sees a clear "reconnect Swiggy" message before the AI call
    fails mid-conversation.
    """
    row = await get_swiggy_token_row(db, user_id)

    if row is None:
        raise SwiggyNotLinkedError()

    # Parse expires_at — Supabase returns ISO 8601 strings
    expires_at_str = row["expires_at"]
    if isinstance(expires_at_str, str):
        expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
    else:
        expires_at = expires_at_str

    now = datetime.now(timezone.utc)
    if now >= expires_at:
        logger.warning("Swiggy token expired — deleting row", extra={"user_id": user_id})
        await delete_swiggy_token(db, user_id)
        raise SwiggyTokenExpiredError()

    return decrypt_token(row["access_token"])


async def get_token_expiry_status(db: AsyncClient, user_id: str) -> dict:
    """
    Return the Swiggy token's remaining lifetime without raising exceptions.
    Used for proactive health checks and pre-chat warnings.

    Returns a dict with:
      linked: bool            — is Swiggy connected at all?
      expired: bool           — has the token passed expires_at?
      expiring_soon: bool     — less than 24h remaining (warn user to reconnect)
      hours_remaining: float  — hours until expiry (negative if already expired)
      expires_at: str | None  — ISO 8601 timestamp

    WHY NOT JUST USE get_decrypted_token (which raises on expiry)?
      Because we want to emit a non-fatal SSE warning at the START of a stream
      ("Your Swiggy session expires in 6 hours — consider reconnecting after this
       order"), not abruptly fail the stream mid-way. This function gives the
      service the data to make that decision without raising.
    """
    row = await get_swiggy_token_row(db, user_id)

    if row is None:
        return {"linked": False, "expired": False, "expiring_soon": False,
                "hours_remaining": 0.0, "expires_at": None}

    expires_at_str = row["expires_at"]
    if isinstance(expires_at_str, str):
        expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
    else:
        expires_at = expires_at_str

    now = datetime.now(timezone.utc)
    hours_remaining = (expires_at - now).total_seconds() / 3600

    return {
        "linked": True,
        "expired": hours_remaining <= 0,
        "expiring_soon": 0 < hours_remaining < 24,   # warn if < 24h left
        "hours_remaining": round(hours_remaining, 1),
        "expires_at": expires_at.isoformat(),
    }


async def delete_swiggy_token(db: AsyncClient, user_id: str) -> None:
    """Remove the Swiggy token row. Called on disconnect or expiry detection."""
    await (
        db.table("swiggy_tokens")
        .delete()
        .eq("user_id", user_id)
        .execute()
    )
    logger.info("Swiggy token deleted", extra={"user_id": user_id})
