#!/usr/bin/env python3
"""
Fetch the OpenAPI spec from production, filter to SDK-facing endpoints,
inject x-mint.content annotations, and write to openapi.json.

Usage:
    python scripts/sync-openapi.py          # writes openapi.json
    python scripts/sync-openapi.py --dry-run # prints stats only
"""

import json
import re
import sys
import urllib.request

PRODUCTION_URL = "https://api.simmer.markets/openapi.json"
OUTPUT_PATH = "openapi.json"

# Endpoints to include (public, agent-facing via SDK API key or no auth)
INCLUDE = [
    # Agents
    "POST /api/sdk/agents/register",
    "GET /api/sdk/agents/me",
    "PATCH /api/sdk/agents/me/settings",
    "GET /api/sdk/agents/claim/{claim_code}",
    # Markets
    "GET /api/sdk/markets",
    "GET /api/sdk/markets/{market_id}",
    "GET /api/sdk/markets/{market_id}/history",
    "GET /api/sdk/markets/opportunities",
    "GET /api/sdk/markets/check",
    "GET /api/sdk/markets/importable",
    "POST /api/sdk/markets/import",
    "POST /api/sdk/markets/import/kalshi",
    "GET /api/sdk/fast-markets",
    # Trading
    "POST /api/sdk/trade",
    "POST /api/sdk/trades/batch",
    "POST /api/sdk/redeem",
    "POST /api/sdk/redeem/report",
    "POST /api/sdk/copytrading/execute",
    # Positions & Portfolio
    "GET /api/sdk/positions",
    "GET /api/sdk/positions/expiring",
    "GET /api/sdk/portfolio",
    # Orders
    "GET /api/sdk/orders/open",
    "DELETE /api/sdk/orders/{order_id}",
    "DELETE /api/sdk/markets/{market_id}/orders",
    "DELETE /api/sdk/orders",
    # Context & Briefing
    "GET /api/sdk/context/{market_id}",
    "GET /api/sdk/briefing",
    # Risk
    "POST /api/sdk/positions/{market_id}/monitor",
    "GET /api/sdk/positions/monitors",
    "DELETE /api/sdk/positions/{market_id}/monitor",
    "GET /api/sdk/risk-alerts",
    "DELETE /api/sdk/risk-alerts/{market_id}/{side}",
    # Settings
    "GET /api/sdk/settings",
    "POST /api/sdk/settings",
    # Alerts
    "POST /api/sdk/alerts",
    "GET /api/sdk/alerts",
    "DELETE /api/sdk/alerts/{alert_id}",
    "GET /api/sdk/alerts/triggered",
    # Webhooks
    "POST /api/sdk/webhooks",
    "GET /api/sdk/webhooks",
    "DELETE /api/sdk/webhooks/{webhook_id}",
    "POST /api/sdk/webhooks/test",
    # Wallet
    "GET /api/sdk/wallet/link/challenge",
    "POST /api/sdk/wallet/link",
    "POST /api/sdk/wallet/unlink",
    "POST /api/sdk/wallet/broadcast-tx",
    "GET /api/sdk/wallet/{wallet_address}/positions",
    "GET /api/sdk/wallet/credentials/check",
    # Skills
    "GET /api/sdk/skills",
    "POST /api/sdk/skills",
    "GET /api/sdk/skills/mine",
    # Trades history
    "GET /api/sdk/trades",
    # Kalshi
    "POST /api/sdk/trade/kalshi/quote",
    "POST /api/sdk/trade/kalshi/submit",
    # Leaderboard
    "GET /api/leaderboard/all",
    "GET /api/leaderboard/sdk-agents",
    "GET /api/leaderboard/{venue}",
    # Utilities
    "POST /api/sdk/troubleshoot",
    "GET /api/sdk/health",
]

# Custom tips/warnings injected into auto-generated pages
CONTENT_INJECTIONS = {
    "/api/sdk/agents/register": {
        "post": '<Warning>Save your `api_key` immediately \u2014 it is only shown once.</Warning>',
    },
    "/api/sdk/trade": {
        "post": (
            '<Note>Multi-outcome markets (e.g., "Who will win the election?") use a different contract type on Polymarket. '
            "This is auto-detected and handled server-side \u2014 no extra parameters needed.</Note>\n\n"
            "<Warning>\n**Before selling, verify:**\n"
            "1. Market is active \u2014 resolved markets cannot be sold, use `/redeem` instead\n"
            "2. Shares >= 5 \u2014 Polymarket minimum per sell order\n"
            "3. Position exists on-chain \u2014 call `GET /positions` fresh before selling\n"
            "4. Use `shares` (not `amount`) for sells\n</Warning>\n\n"
            '<Note>The `source` tag groups trades for P&L tracking and prevents accidental re-buys on markets you already hold. '
            "Use a consistent prefix like `sdk:strategy-name`.</Note>"
        ),
    },
    "/api/sdk/markets": {
        "get": (
            "<Tip>Need `time_to_resolution`, slippage, or flip-flop detection? "
            "Use the [context endpoint](/api/context) \u2014 those fields are not on `/markets`.</Tip>"
        ),
    },
    "/api/sdk/markets/import": {
        "post": (
            "<Warning>The `market_id` values in import responses are Simmer-specific UUIDs \u2014 "
            "different from Polymarket condition IDs. Use these Simmer IDs for all subsequent API calls.</Warning>\n\n"
            "Response headers: `X-Imports-Remaining`, `X-Imports-Limit`. Re-importing an existing market does not consume quota.\n\n"
            "**Need more than 100/day?** When you hit the daily limit, the `429` response includes an `x402_url` field. "
            "Pay $0.005/import with USDC on Base for unlimited overflow."
        ),
    },
    "/api/sdk/markets/import/kalshi": {
        "post": (
            "<Warning>The `market_id` is a Simmer-specific UUID \u2014 different from the Kalshi ticker. "
            "Use this Simmer ID for all subsequent API calls.</Warning>"
        ),
    },
    "/api/sdk/redeem": {
        "post": (
            "**Managed wallet:** Server signs and submits, returns `tx_hash`.\n\n"
            "**External wallet:** Server returns `unsigned_tx` for you to sign. The Python SDK handles this automatically "
            "with `client.redeem()`.\n\n"
            '<Tip>Use `GET /api/sdk/positions` and look for `"redeemable": true` to find positions ready to redeem.</Tip>'
        ),
    },
    "/api/sdk/positions": {
        "get": "<Tip>Filter by `source` to see positions from a specific skill or strategy.</Tip>",
    },
    "/api/sdk/orders/open": {
        "get": (
            "<Note>External wallet users who also place orders directly on the Polymarket CLOB (outside Simmer) "
            "should query the CLOB directly for a complete picture of open orders.</Note>"
        ),
    },
    "/api/sdk/positions/{market_id}/monitor": {
        "post": (
            "**Stop-loss is on by default** \u2014 every buy gets a 50% stop-loss automatically. "
            "Take-profit is off by default (prediction markets resolve naturally). "
            "Use this endpoint to set or override thresholds for a specific position."
        ),
    },
    "/api/sdk/risk-alerts": {
        "get": (
            "<Note>The Python SDK handles risk alerts automatically via `get_briefing()`. "
            "You typically do not need to call this directly.</Note>"
        ),
    },
    "/api/sdk/briefing": {
        "get": "<Tip>This is the recommended single-call check-in for agent heartbeat loops. See the [Heartbeat Pattern](/heartbeat) guide.</Tip>",
    },
    "/api/sdk/markets/opportunities": {
        "get": "<Tip>This is a convenience wrapper around `/markets?sort=opportunity`. Use it when you want pre-filtered, ranked opportunities.</Tip>",
    },
    "/api/sdk/redeem/report": {
        "post": "<Note>The Python SDK calls this automatically after signing a redeem transaction. You only need this if you are building your own signing flow.</Note>",
    },
    "/api/sdk/copytrading/execute": {
        "post": "<Warning>This endpoint executes real trades. Always test with `venue=sim` first.</Warning>",
    },
}


def find_refs(obj, spec, found=None):
    """Recursively find all $ref schema names."""
    if found is None:
        found = set()
    if isinstance(obj, dict):
        if "$ref" in obj:
            ref = obj["$ref"]
            if ref.startswith("#/components/schemas/"):
                name = ref.split("/")[-1]
                if name not in found:
                    found.add(name)
                    schema = spec.get("components", {}).get("schemas", {}).get(name, {})
                    find_refs(schema, spec, found)
        for v in obj.values():
            find_refs(v, spec, found)
    elif isinstance(obj, list):
        for item in obj:
            find_refs(item, spec, found)
    return found


def sync():
    dry_run = "--dry-run" in sys.argv

    # Fetch
    print(f"Fetching {PRODUCTION_URL}...")
    with urllib.request.urlopen(PRODUCTION_URL) as resp:
        spec = json.loads(resp.read())
    print(f"  Total paths: {len(spec.get('paths', {}))}")

    # Filter paths
    filtered_paths = {}
    for entry in INCLUDE:
        method, path = entry.split(" ", 1)
        method = method.lower()
        if path in spec["paths"] and method in spec["paths"][path]:
            if path not in filtered_paths:
                filtered_paths[path] = {}
            filtered_paths[path][method] = spec["paths"][path][method]

    ops = sum(len(v) for v in filtered_paths.values())
    print(f"  Filtered: {ops} operations across {len(filtered_paths)} paths")

    # Clean summaries
    for path, methods in filtered_paths.items():
        for method, op in methods.items():
            if "summary" in op:
                op["summary"] = op["summary"].replace("Api Sdk ", "").replace("Api ", "")

    # Inject x-mint.content
    injected = 0
    for path, methods in CONTENT_INJECTIONS.items():
        if path in filtered_paths:
            for method, content in methods.items():
                if method in filtered_paths[path]:
                    if "x-mint" not in filtered_paths[path][method]:
                        filtered_paths[path][method]["x-mint"] = {}
                    filtered_paths[path][method]["x-mint"]["content"] = content
                    injected += 1
    print(f"  Injected x-mint.content: {injected} endpoints")

    # Build filtered spec
    filtered_spec = {
        "openapi": spec["openapi"],
        "info": {
            "title": "Simmer API",
            "description": "Agent-native trading infrastructure for prediction markets.",
            "version": "1.0.0",
        },
        "servers": [{"url": "https://api.simmer.markets"}],
        "paths": filtered_paths,
    }

    # Trim schemas to only referenced ones
    used = find_refs(filtered_paths, spec)
    for name in list(used):
        schema = spec.get("components", {}).get("schemas", {}).get(name, {})
        find_refs(schema, spec, used)

    all_schemas = spec.get("components", {}).get("schemas", {})
    filtered_schemas = {k: v for k, v in all_schemas.items() if k in used}
    filtered_spec["components"] = {"schemas": filtered_schemas}
    print(f"  Schemas: {len(filtered_schemas)} (trimmed from {len(all_schemas)})")

    size_kb = len(json.dumps(filtered_spec)) / 1024
    print(f"  Output size: {size_kb:.0f} KB")

    if dry_run:
        print("\n  --dry-run: not writing file")
    else:
        with open(OUTPUT_PATH, "w") as f:
            json.dump(filtered_spec, f, indent=2)
            f.write("\n")
        print(f"\n  Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    sync()
