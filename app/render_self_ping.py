"""Run an opt-in liveness probe through the service's public Render URL."""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

import httpx


async def run_self_ping(url):
    # Give the new instance time to become reachable before the first probe.
    await asyncio.sleep(60)
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        while True:
            ping_id = uuid4().hex
            result = {"event": "render_self_ping", "id": ping_id, "ok": False}
            try:
                response = await client.get(url, params={"render_self_ping": ping_id})
                result["status"] = response.status_code
                result["ok"] = response.status_code == 200 and response.json() == {"status": "ok"}
            except (httpx.HTTPError, ValueError) as exc:
                result["error"] = type(exc).__name__
            print(json.dumps(result), flush=True)
            await asyncio.sleep(300)


@asynccontextmanager
async def self_ping_lifespan():
    base = os.getenv("RENDER_EXTERNAL_URL", "")
    if not re.fullmatch(r"https://[a-z0-9][a-z0-9-]*\.onrender\.com/?", base):
        raise RuntimeError("Render self-ping requires the public RENDER_EXTERNAL_URL.")
    task = asyncio.create_task(run_self_ping(base.rstrip("/") + "/healthz"))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
