"""
foodpilot/swiggy/client.py

Builds the mcp_servers config that tells Claude which Swiggy MCP servers
to connect to and how to authenticate against them.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT IS mcp_servers?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
When we send mcp_servers to the Anthropic API (via the beta flag
"mcp-client-2025-11-20"), Claude connects to each server, reads
its tool catalogue, and autonomously decides which tools to call.

We pass TWO servers:
  - swiggy-food  → food delivery tools (search_restaurants, update_food_cart, place_food_order…)
  - swiggy-instamart → grocery tools  (search_products, update_cart, checkout…)

The same user access token authenticates against both servers.

OFFICIAL URLS (verified from https://mcp.swiggy.com/builders/docs):
  Food:       https://mcp.swiggy.com/food
  Instamart:  https://mcp.swiggy.com/im

HOW THE TOKEN IS INJECTED:
━━━━━━━━━━━━━━━━━━━━━━━━━━━
The Anthropic MCP connector sends the authorization_token as a
"Authorization: Bearer <token>" header on every MCP call to Swiggy.
This is exactly how the Swiggy docs say to authenticate:
  "Anthropic hosted MCP connector — Bearer-header only"

We decrypt the token from our DB here, just before handing it to Claude,
so the plaintext token is only in memory for the duration of the API call.
"""
from __future__ import annotations


# Official Swiggy MCP server URLs per Builders Club documentation
# Source: https://mcp.swiggy.com/builders/docs/start/developer/build-an-agent.md
SWIGGY_FOOD_MCP_URL = "https://mcp.swiggy.com/food"
SWIGGY_INSTAMART_MCP_URL = "https://mcp.swiggy.com/im"


def build_mcp_servers(swiggy_access_token: str) -> list[dict]:
    """
    Build the mcp_servers list for the Anthropic API call.

    Args:
        swiggy_access_token: Plaintext Swiggy Bearer token (decrypted from DB
                             by get_decrypted_token() just before this call).

    Returns:
        List of MCP server configs — one for Food, one for Instamart.
        These are passed directly to the Anthropic beta messages API.

    Swiggy excludes Dineout from scope per user decision ("leave dineout for now").
    """
    return [
        {
            "type": "url",
            "url": SWIGGY_FOOD_MCP_URL,
            "name": "swiggy-food",
            "authorization_token": swiggy_access_token,
        },
        {
            "type": "url",
            "url": SWIGGY_INSTAMART_MCP_URL,
            "name": "swiggy-instamart",
            "authorization_token": swiggy_access_token,
        },
    ]


def build_mcp_tools(mcp_servers: list[dict]) -> list[dict]:
    """
    Build the tools list that tells Claude which MCP toolsets are available.

    Each mcp_toolset entry corresponds to one server in mcp_servers.
    Claude reads the tool catalogue from each server and knows which
    tool belongs to which server by the mcp_server_name reference.

    Source: Anthropic beta docs + Swiggy build-an-agent examples.
    """
    return [
        {"type": "mcp_toolset", "mcp_server_name": server["name"]}
        for server in mcp_servers
    ]
