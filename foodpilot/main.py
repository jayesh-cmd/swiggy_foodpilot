"""
foodpilot/main.py

FastAPI application factory.

- App is constructed via create_app() so it can be imported in tests
  without side effects.
- Lifespan handles startup (Supabase init) and shutdown (graceful close).
- Global exception handlers translate FoodPilotError subclasses to
  structured JSON responses.
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.responses import JSONResponse

from foodpilot.config import get_settings
from foodpilot.core.errors import (
    AIProviderError,
    AuthError,
    ConversationNotFoundError,
    FoodPilotError,
    SwiggyNotLinkedError,
    SwiggySessionRevokedError,
    SwiggyTokenExpiredError,
    TokenExpiredError,
)
from foodpilot.core.logging import configure_logging, get_logger
from foodpilot.core.middleware import RateLimitMiddleware, RequestIDMiddleware
from foodpilot.db.client import close_db, get_db

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup → yield → shutdown."""
    settings = get_settings()

    # Configure logging first so every subsequent log is structured
    configure_logging(settings.log_level)

    logger.info(
        "FoodPilot AI starting",
        extra={"env": settings.app_env, "log_level": settings.log_level},
    )

    # Warm up the Supabase connection (fail fast if credentials are wrong)
    await get_db()
    logger.info("Supabase connection established")

    yield

    # Graceful shutdown
    await close_db()
    logger.info("FoodPilot AI shut down cleanly")


def create_app() -> FastAPI:
    """Construct and configure the FastAPI application."""
    settings = get_settings()

    if settings.sentry_dsn:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.app_env,
            traces_sample_rate=1.0 if settings.app_env == "development" else 0.1,
            _experiments={"continuous_profiling_auto_start": True},
        )
        logger.info("Sentry initialized")

    app = FastAPI(
        title="FoodPilot AI",
        description=(
            "AI-powered food concierge — order food delivery and groceries "
            "through a conversational interface powered by Claude Sonnet 4.6 "
            "and Swiggy MCP."
        ),
        version="0.1.0",
        # Disable interactive docs in production
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url="/redoc" if settings.app_env != "production" else None,
        lifespan=lifespan,
    )

    # ── Middleware ────────────────────────────────────────────────────────────
    if settings.app_env == "production":
        # M9: Force HTTPS in production
        app.add_middleware(HTTPSRedirectMiddleware)

    # Order matters: RequestIDMiddleware should run first so rate limit logs have request IDs
    app.add_middleware(RateLimitMiddleware, requests_per_minute=60)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:8080", "http://localhost:8081", "http://localhost:5173", "http://127.0.0.1:8080", "http://127.0.0.1:8081", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Exception handlers ────────────────────────────────────────────────────
    _register_exception_handlers(app)

    # ── System routes (always present) ───────────────────────────────────────
    app.include_router(_system_router())

    # ── Feature routers ─────────────────────────────────────────────────────
    # M2: Supabase Auth (Google OAuth)
    from foodpilot.auth.router import router as auth_router  # noqa: PLC0415
    app.include_router(auth_router, prefix="/auth")

    # M3: Swiggy account linking
    from foodpilot.auth.swiggy_router import router as swiggy_auth_router  # noqa: PLC0415
    app.include_router(swiggy_auth_router, prefix="/auth/swiggy")

    # M5: Chat (Claude ↔ Swiggy MCP)
    from foodpilot.chat.router import router as chat_router  # noqa: PLC0415
    app.include_router(chat_router, prefix="/chat")

    # M5: app.include_router(chat_router, prefix="/chat", tags=["Chat"])

    return app


def _system_router() -> APIRouter:
    router = APIRouter(tags=["System"])

    @router.get("/health", summary="Health check")
    async def health_check() -> dict:
        """
        Returns service status. Used by load balancers and uptime monitors.
        Does NOT check downstream dependencies (Supabase, Swiggy) — this endpoint
        must always be fast and dependency-free.
        """
        return {
            "status": "ok",
            "service": "foodpilot-ai",
            "version": "0.1.0",
        }

    return router


def _register_exception_handlers(app: FastAPI) -> None:
    """Map FoodPilotError subclasses to HTTP status codes + structured JSON."""

    @app.exception_handler(ConversationNotFoundError)
    async def conversation_not_found_handler(
        request: Request, exc: ConversationNotFoundError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"error": exc.code, "message": exc.message},
        )

    @app.exception_handler(TokenExpiredError)
    async def token_expired_handler(request: Request, exc: TokenExpiredError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"error": exc.code, "message": exc.message},
        )

    @app.exception_handler(AuthError)
    async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"error": exc.code, "message": exc.message},
        )

    @app.exception_handler(SwiggyNotLinkedError)
    async def swiggy_not_linked_handler(
        request: Request, exc: SwiggyNotLinkedError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={
                "error": exc.code,
                "message": exc.message,
                "action": "Visit /auth/swiggy/connect to link your Swiggy account.",
            },
        )

    @app.exception_handler(SwiggyTokenExpiredError)
    async def swiggy_token_expired_handler(
        request: Request, exc: SwiggyTokenExpiredError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={
                "error": exc.code,
                "message": exc.message,
                "reauth_required": True,
                "connect_url": "/auth/swiggy/connect",
            },
        )

    @app.exception_handler(SwiggySessionRevokedError)
    async def swiggy_session_revoked_handler(
        request: Request, exc: SwiggySessionRevokedError
    ) -> JSONResponse:
        # HTTP 403 (not 419) — 419 is non-standard and some clients ignore it.
        # The body's "error" code (SWIGGY_SESSION_REVOKED) is the actionable signal.
        # full_reauth=True tells the frontend to show phone+OTP flow, not just a token refresh.
        return JSONResponse(
            status_code=403,
            content={
                "error": exc.code,
                "message": exc.message,
                "full_reauth_required": True,
                "connect_url": "/auth/swiggy/connect",
            },
        )

    @app.exception_handler(AIProviderError)
    async def ai_provider_error_handler(
        request: Request, exc: AIProviderError
    ) -> JSONResponse:
        # HTTP 503 = Service Unavailable — the correct code when a dependency is down.
        # The client should retry after a delay, not immediately.
        logger.error(
            "AI provider failed",
            extra={"provider": exc.provider, "message": exc.message},
        )
        return JSONResponse(
            status_code=503,
            content={
                "error": exc.code,
                "message": exc.message,
                "retry_after_seconds": 30,
            },
        )

    @app.exception_handler(FoodPilotError)
    async def generic_foodpilot_error_handler(
        request: Request, exc: FoodPilotError
    ) -> JSONResponse:
        logger.error(
            "Unhandled FoodPilotError",
            extra={
                "code": exc.code,
                "message": exc.message,
                "request_id": getattr(request.state, "request_id", None),
            },
        )
        return JSONResponse(
            status_code=500,
            content={"error": exc.code, "message": exc.message},
        )


# Instantiate the app (imported by uvicorn as `foodpilot.main:app`)
app = create_app()
