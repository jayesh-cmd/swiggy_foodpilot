"""
foodpilot/ai/claude.py

Claude Sonnet 4.6 implementation of AIProvider.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW CLAUDE CALLS SWIGGY TOOLS AUTOMATICALLY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
This is the most important part of the whole project.

When we pass `mcp_servers` to the Anthropic API with the beta flag
"mcp-client-2025-11-20", something magical happens:

1. Claude asks Swiggy's MCP server: "what tools do you have?"
2. Swiggy responds: "I have search_restaurants, update_food_cart, place_food_order..."
3. Claude reads the user's message ("order biryani to my home")
4. Claude AUTONOMOUSLY decides which tools to call and in what order
5. Claude calls get_addresses, then search_restaurants, then update_food_cart
6. Claude reads each tool response and decides what to do next
7. Claude writes the final natural-language response to the user

WE WRITE ZERO TOOL-DISPATCH CODE. Claude does all of it.
Our system prompt (prompt.py) just tells Claude the rules.

WHY THE BETA FLAG "mcp-client-2025-11-20"?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The Anthropic SDK's native MCP connector is still in beta as of mid-2026.
The flag tells the API to enable MCP server support for this request.
This is the correct approach per Swiggy's own documentation.

WHY STREAMING?
━━━━━━━━━━━━━━━
We use stream=True so the user sees the response token by token,
not waiting 10 seconds for a complete response. This is the standard
for all modern AI interfaces.
"""
from __future__ import annotations

import anthropic
from anthropic import AsyncAnthropic

from foodpilot.config import get_settings
from foodpilot.core.errors import (
    AIProviderError,
    SwiggySessionRevokedError,
    SwiggyTokenExpiredError,
)
from foodpilot.core.logging import get_logger

logger = get_logger(__name__)

# Anthropic beta flag that enables native MCP server support
_MCP_BETA = "mcp-client-2025-11-20"
_MODEL = "claude-sonnet-4-6"


class ClaudeProvider:
    """
    Claude Sonnet 4.6 AI provider with native Swiggy MCP integration.

    Satisfies the AIProvider Protocol — has the correct chat() signature
    so it can be used anywhere an AIProvider is expected.

    Usage:
        provider = ClaudeProvider()
        async for chunk in provider.chat(messages, system, mcp_servers):
            print(chunk, end="", flush=True)
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._client = AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            max_retries=3,  # Native exponential backoff for API/MCP failures
        )

    async def chat(
        self,
        messages: list[dict],
        system: str,
        mcp_servers: list[dict] | None = None,
    ):
        """
        Stream a response from Claude Sonnet 4.6.

        If mcp_servers is provided (Swiggy Food + Instamart configs with the
        user's decrypted Bearer token), Claude will autonomously call Swiggy
        tools as needed to fulfil the user's request.

        Yields str chunks as Claude generates them.
        Raises AIProviderError if the Anthropic API call fails.
        """
        try:
            # Build the tool list — one mcp_toolset entry per MCP server
            # This tells Claude "you have access to all tools on these servers"
            tools = []
            if mcp_servers:
                for server in mcp_servers:
                    tools.append({
                        "type": "mcp_toolset",
                        "mcp_server_name": server["name"],
                    })

            logger.info(
                "Claude chat started",
                extra={
                    "model": _MODEL,
                    "mcp_servers": [s["name"] for s in (mcp_servers or [])],
                    "message_count": len(messages),
                },
            )


            # Stream the response using the native MCP beta AND prompt caching
            async with self._client.beta.messages.stream(
                model=_MODEL,
                max_tokens=4096,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
                betas=[_MCP_BETA, "prompt-caching-2024-07-31"],
                **({"mcp_servers": mcp_servers} if mcp_servers else {}),
                **({"tools": tools} if tools else {}),
            ) as stream:
                async for text_chunk in stream.text_stream:
                    yield text_chunk
                
                final_msg = await stream.get_final_message()
                logger.info(
                    "claude_token_usage",
                    extra={
                        "input_tokens": final_msg.usage.input_tokens,
                        "output_tokens": final_msg.usage.output_tokens,
                        "cache_creation_input_tokens": getattr(final_msg.usage, "cache_creation_input_tokens", None),
                        "cache_read_input_tokens": getattr(final_msg.usage, "cache_read_input_tokens", None),
                        "stop_reason": final_msg.stop_reason,
                    }
                )
                
                # Check for tool results in the final message content
                for block in final_msg.content:
                    if block.type == "tool_result" or block.type == "mcp_tool_result":
                        logger.info("mcp_tool_result_size", extra={
                            "tool_name": getattr(block, "tool_use_id", "unknown"), # Note: tool_result blocks usually link back via tool_use_id, not name directly, but we'll try to find any identifying info
                            "content_length_chars": len(str(block.content)),
                        })

        except anthropic.AuthenticationError as exc:
            raise AIProviderError(
                f"Anthropic API key is invalid or expired: {exc}",
                provider="claude",
            )
        except anthropic.RateLimitError as exc:
            raise AIProviderError(
                "Anthropic rate limit reached. Please try again in a moment.",
                provider="claude",
            )
        except anthropic.APIStatusError as exc:
            # ── M8: Swiggy MCP Error Code Mapping ──────────────────────────────
            # If Anthropic's backend fails to talk to Swiggy's MCP server, it
            # returns an APIStatusError (often 502 Bad Gateway or 400 Bad Request).
            # The body contains the upstream error details from Swiggy.
            err_text = str(exc).lower()
            if "401" in err_text or "unauthorized" in err_text:
                logger.warning("Claude received 401 from Swiggy MCP server")
                raise SwiggyTokenExpiredError()
            
            if "419" in err_text or "session_revoked" in err_text:
                logger.warning("Claude received 419 from Swiggy MCP server")
                raise SwiggySessionRevokedError()
            
            if "403" in err_text or "forbidden" in err_text:
                logger.warning("Claude received 403 from Swiggy MCP server")
                raise AIProviderError(
                    "Swiggy blocked this request (403 Forbidden).",
                    provider="claude",
                )
            
            if "429" in err_text or "too many requests" in err_text:
                logger.warning("Claude received 429 Rate Limit from Swiggy MCP server")
                raise AIProviderError(
                    "Swiggy rate limit reached. Please wait a moment and try again.",
                    provider="claude",
                )
            
            # Unhandled APIStatusError
            raise AIProviderError(
                f"Claude API status error: {exc}",
                provider="claude",
            )
        except anthropic.APIError as exc:
            raise AIProviderError(
                f"Claude API error: {exc}",
                provider="claude",
            )
