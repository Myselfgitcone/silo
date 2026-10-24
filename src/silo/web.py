"""The public face of Silo: a chat page and a small JSON API beside /mcp.

Everything runs in the one process that serves the MCP endpoint, so the model
key never leaves the server. Spend is bounded three ways: preset questions are
answered once per lake build and cached, free-text questions are rate limited
per visitor, and the daily budget stops the model for everyone once it's spent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from anthropic import APIError
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from silo.agent import Answer, answer
from silo.budget import Budget, BudgetExceeded
from silo.server import build_server
from silo.settings import Settings
from silo.tools import Toolbox

log = logging.getLogger(__name__)

STATIC = Path(__file__).with_name("static")
MAX_QUESTION_CHARS = 300

PRESETS = {
    "signal": "When corn crop ratings fall, does corn rise the next week?",
    "worst_state": "Which of the 18 corn states had the worst rating last week?",
    "iowa_season": "How has Iowa's corn rating moved over the latest season?",
    "high_2022": "What was the highest corn futures close in 2022, and when?",
    "wheat_vol": "How volatile are wheat futures right now?",
}


class RateLimiter:
    """At most `limit` hits per key in any rolling hour."""

    def __init__(self, limit: int, window_s: float = 3600):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        while hits and now - hits[0] > self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def retry_after(self, key: str, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        return max(1, round(self.window_s - (now - hits[0]))) if hits else 0


class Web:
    def __init__(self, toolbox: Toolbox, settings: Settings, budget: Budget, llm):
        self.toolbox = toolbox
        self.settings = settings
        self.budget = budget
        self.llm = llm
        self.limiter = RateLimiter(settings.asks_per_hour)
        self.busy = asyncio.Semaphore(settings.max_concurrent_asks)
        # preset id -> (lake build it was answered from, payload)
        self.cache: dict[str, tuple[str | None, dict[str, Any]]] = {}
        self.preset_locks = {key: asyncio.Lock() for key in PRESETS}

    def register(self, server: MCPServer) -> None:
        server.custom_route("/", methods=["GET"])(self.page)
        server.custom_route("/app.js", methods=["GET"])(self.script)
        server.custom_route("/api/config", methods=["GET"])(self.config)
        server.custom_route("/api/ask", methods=["POST"])(self.ask)
        server.custom_route("/api/health", methods=["GET"])(self.health)

    async def page(self, _: Request) -> Response:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def script(self, _: Request) -> Response:
        return FileResponse(STATIC / "app.js", headers={"Cache-Control": "no-cache"})

    async def health(self, _: Request) -> Response:
        return JSONResponse({"ok": True, "lake_built_at": self.toolbox.lake.generated_at})

    async def config(self, _: Request) -> Response:
        return JSONResponse(
            {
                "presets": [{"id": key, "question": q} for key, q in PRESETS.items()],
                "model": self.settings.model,
                "analyst_online": self.llm is not None,
                "spent_today": round(self.budget.spent_today(), 4),
                "daily_budget": self.settings.daily_budget_usd,
                "asks_per_hour": self.settings.asks_per_hour,
                "lake_built_at": self.toolbox.lake.generated_at,
                "grainflow_url": self.settings.grainflow_url,
                "portfolio_url": self.settings.portfolio_url or None,
                "source_url": "https://github.com/Myselfgitcone/silo",
            }
        )

    async def ask(self, request: Request) -> Response:
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return _error(400, 'Send JSON like {"question": "..."}.')
        if self.llm is None:
            return _error(503, "The analyst is offline: no model key is configured on the server.")

        preset = body.get("preset")
        if preset is not None:
            if preset not in PRESETS:
                return _error(400, f"Unknown preset {preset!r}.")
            return await self._preset(preset)

        question = " ".join(str(body.get("question", "")).split())
        if not question:
            return _error(400, "Ask a question first.")
        if len(question) > MAX_QUESTION_CHARS:
            return _error(400, f"Keep questions under {MAX_QUESTION_CHARS} characters.")
        visitor = _visitor(request)
        if not self.limiter.allow(visitor):
            minutes = -(-self.limiter.retry_after(visitor) // 60)
            return _error(
                429,
                f"That's {self.settings.asks_per_hour} questions this hour. "
                f"Try again in {minutes} min, or pick one of the examples.",
            )
        return await self._run(question, cached=False)

    async def _preset(self, key: str) -> Response:
        built = self.toolbox.lake.generated_at
        async with self.preset_locks[key]:
            hit = self.cache.get(key)
            if hit and hit[0] == built:
                return JSONResponse({**hit[1], "cached": True})
            response = await self._run(PRESETS[key], cached=False)
            if response.status_code == 200:
                self.cache[key] = (built, json.loads(response.body))
            return response

    async def _run(self, question: str, cached: bool) -> Response:
        async with self.busy:
            try:
                reply = await answer(
                    question, build_server(self.toolbox), self.llm, self.settings, self.budget
                )
            except BudgetExceeded as error:
                return _error(503, str(error))
            except APIError as error:
                log.warning("model API error: %s", error)
                return _error(502, "The model API had a problem. Try again in a minute.")
        return JSONResponse(self._payload(reply, cached))

    def _payload(self, reply: Answer, cached: bool) -> dict[str, Any]:
        u = reply.usage
        return {
            "question": reply.question,
            "text": reply.text,
            "steps": [
                {"tool": s.tool, "ok": s.ok, "ms": s.ms, "summary": s.summary, "sql": s.sql}
                for s in reply.steps
            ],
            "cost_usd": round(reply.cost_usd, 5),
            "tokens_in": u.input_tokens + u.cache_read_tokens + u.cache_write_tokens,
            "tokens_out": u.output_tokens,
            "latency_ms": reply.latency_ms,
            "model": reply.model,
            "lake_built_at": self.toolbox.lake.generated_at,
            "spent_today": round(self.budget.spent_today(), 4),
            "cached": cached,
        }


def _visitor(request: Request) -> str:
    # Railway's proxy puts the caller first in X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)
