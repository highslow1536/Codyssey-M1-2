"""Read-only SDK subprocess check; --live collects real provider observations."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from app.config import Settings
from mcp_adapter.tools import TOOLS


async def check(env_file, live=False):
    # Explicitly pass configured shell overrides, without printing credentials.
    env = {
        name.upper(): os.environ[name.upper()]
        for name in Settings.model_fields
        if name.upper() in os.environ
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_adapter.server", "--env-file", str(env_file)],
        env=env,
        cwd=tempfile.gettempdir(),
    )
    report = {"transport": "stdio", "live": live, "instruments": []}
    async with Client(params, mode="legacy", read_timeout_seconds=180) as client:
        tools = await client.list_tools()
        if {tool.name for tool in tools.tools} != set(TOOLS):
            raise RuntimeError("tool_catalog_mismatch")
        report["tools"] = len(tools.tools)
        catalog = await client.call_tool("list_instruments", {})
        if catalog.is_error or len(catalog.structured_content["items"]) != 9:
            raise RuntimeError("instrument_catalog_failed")
        denied = await client.call_tool("list_alerts", {"owner_uid": "forbidden"})
        if not denied.is_error:
            raise RuntimeError("unexpected_owner_argument_accepted")
        if live:
            for instrument_id in ("KRX_005930", "NASDAQ_AAPL"):
                item = {"instrument_id": instrument_id}
                for name in ("get_quote", "get_price_history", "calculate_indicators"):
                    response = await client.call_tool(name, {"instrument_id": instrument_id})
                    payload = response.structured_content
                    if response.is_error:
                        raise RuntimeError(f"{name}:{payload['error']['code']}")
                    if name == "get_quote" and payload.get("quote") is None:
                        raise RuntimeError("quote_observation_missing")
                    if name != "get_quote" and not payload.get("observation_count"):
                        raise RuntimeError("daily_observations_missing")
                    item[name] = payload
                report["instruments"].append(item)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.env_file.is_absolute() or not args.env_file.is_file():
        parser.error("--env-file requires an existing absolute path")
    try:
        report = asyncio.run(check(args.env_file, args.live))
    except Exception:
        print("MCP smoke failed; check server stderr and docs/mcp.md", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
