"""
foodpilot/core/errors.py

Custom exception hierarchy. All business errors subclass FoodPilotError.
FastAPI exception handlers (registered in main.py) convert these to HTTP responses.
"""


class FoodPilotError(Exception):
    """Base for all FoodPilot application errors."""

    def __init__(self, message: str, code: str = "INTERNAL_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(code={self.code!r}, message={self.message!r})"


# ── Auth errors ───────────────────────────────────────────────────────────────


class AuthError(FoodPilotError):
    """Raised when a request cannot be authenticated via Supabase JWT."""

    def __init__(
        self,
        message: str = "Authentication required",
        code: str = "AUTH_ERROR",
    ) -> None:
        super().__init__(message, code)


class TokenExpiredError(AuthError):
    def __init__(self) -> None:
        super().__init__("Session token has expired. Please log in again.", "TOKEN_EXPIRED")


# ── Swiggy errors ─────────────────────────────────────────────────────────────


class SwiggyNotLinkedError(FoodPilotError):
    """Raised when a user tries to chat without linking their Swiggy account."""

    def __init__(self) -> None:
        super().__init__(
            "Swiggy account not linked. Please connect your Swiggy account first.",
            "SWIGGY_NOT_LINKED",
        )


class SwiggyTokenExpiredError(FoodPilotError):
    """
    Raised when the stored Swiggy access token has passed its expires_at timestamp.
    Detected proactively in get_decrypted_token() BEFORE calling Claude/Swiggy.

    Recovery: user re-runs the Swiggy PKCE flow at /auth/swiggy/connect.
    Swiggy docs: access tokens live 5 days; no refresh token in v1.0.
    The underlying Swiggy session lives 30 days (sliding), so re-auth is
    usually silent (no phone+OTP prompt) if the session is still valid.
    """

    def __init__(self) -> None:
        super().__init__(
            "Your Swiggy session has expired. Please reconnect your account at /auth/swiggy/connect.",
            "SWIGGY_TOKEN_EXPIRED",
        )


class SwiggySessionRevokedError(FoodPilotError):
    """
    Raised when Swiggy returns HTTP 419 — the session was revoked server-side.

    WHY 419 IS DIFFERENT FROM 401:
      - HTTP 401 (token expired): The 5-day access token timed out.
        Silent re-auth is likely (Swiggy session still valid → no phone+OTP prompt).
      - HTTP 419 (session revoked): Swiggy invalidated the session before expiry.
        Causes: user logged out of Swiggy app, account security event, policy revoke.
        Full re-auth required (phone + OTP) — cannot be silent.

    Our API maps 419 → HTTP 403 (Forbidden) because 419 is a non-standard code
    that HTTP clients may not handle correctly. The JSON body contains the real signal.

    Recovery: full Swiggy re-auth at /auth/swiggy/connect (phone + OTP required).
    """

    def __init__(self) -> None:
        super().__init__(
            "Your Swiggy session was revoked. Please reconnect your account "
            "at /auth/swiggy/connect (phone + OTP required).",
            "SWIGGY_SESSION_REVOKED",
        )


class SwiggyAuthError(FoodPilotError):
    """Raised during the Swiggy PKCE OAuth flow if DCR or token exchange fails."""

    def __init__(self, message: str = "Swiggy authorization failed") -> None:
        super().__init__(message, "SWIGGY_AUTH_ERROR")


# ── AI Provider errors ────────────────────────────────────────────────────────


class AIProviderError(FoodPilotError):
    """Raised when the primary AI provider fails and no fallback is available."""

    def __init__(self, message: str = "AI provider unavailable", provider: str = "unknown") -> None:
        super().__init__(message, "AI_PROVIDER_ERROR")
        self.provider = provider


# ── Conversation errors ───────────────────────────────────────────────────────


class ConversationNotFoundError(FoodPilotError):
    def __init__(self, conversation_id: str) -> None:
        super().__init__(
            f"Conversation '{conversation_id}' not found or has expired.",
            "CONVERSATION_NOT_FOUND",
        )
