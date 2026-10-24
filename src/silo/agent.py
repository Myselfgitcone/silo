"""The analyst: a tool-use loop over Silo's own MCP server.

The agent is an ordinary MCP client. It lists the server's tools, hands them to
the model, runs whatever the model calls, and feeds results (and rejections)
back until the model answers. Nothing here knows SQL or the lake; the server's
guardrails apply to the agent exactly as they do to any other client.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import Client

from silo.budget import Budget, Usage
from silo.settings import Settings

SYSTEM = """\
You are Silo, an analyst for US grain markets. You answer from the Grainflow
lakehouse through your tools, and only from what they return.

- Never state a number you didn't get from a tool result.
- Prefer query_metric. Use run_query only when no metric fits.
- If a tool rejects your request, read why, fix it and try again.
- If the data can't answer the question, say so and say what is available.
- Answer in a few sentences: the finding first, with numbers and units, then
  any caveat that changes how to read it. No preamble.
- Don't repeat SQL, table or tool names; the queries are shown to the reader
  separately.
- If asked to change or delete data, say in one sentence that you have
  read-only access, and stop.
"""

# Tool results go back into the prompt; keep a runaway result from eating it.
MAX_RESULT_CHARS = 12_000


@dataclass
class Step:
    tool: str
    arguments: dict[str, Any]
    ok: bool
    ms: int
    summary: str
    sql: str | None = None


@dataclass
class Answer:
    question: str
    text: str
    model: str
    steps: list[Step] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    latency_ms: int = 0
    stopped_early: bool = False
    cut_off: bool = False

    @property
    def sql(self) -> list[str]:
        return [s.sql for s in self.steps if s.ok and s.sql]

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "sql": self.sql}


async def answer(question: str, server: Any, llm, settings: Settings, budget: Budget) -> Answer:
    """Connect to an MCP server (an in-process MCPServer or a URL) and answer one question."""
    async with Client(server) as mcp:
        try:
            return await ask(question, mcp, llm, settings, budget)
        except Exception as error:
            failed = error
    # Raised after the client closes, so callers get the real error rather than
    # the ExceptionGroup the client's task group would wrap it in.
    raise failed


async def ask(question: str, mcp: Client, llm, settings: Settings, budget: Budget) -> Answer:
    """Answer one question. `llm` is an AsyncAnthropic client (or a stand-in in tests)."""
    started = time.perf_counter()
    answer = Answer(question=question, text="", model=settings.model)
    tools = await _tool_specs(mcp)
    system = [{"type": "text", "text": SYSTEM + "\n" + (mcp.instructions or "")}]
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]

    for _ in range(settings.max_steps):
        budget.check()
        response = await llm.messages.create(
            model=settings.model,
            max_tokens=settings.max_output_tokens,
            system=system,
            tools=tools,
            messages=messages,
        )
        call = Usage()
        call.add(response.usage)
        answer.usage.add(response.usage)
        budget.record(call.cost(settings))

        messages.append({"role": "assistant", "content": [_as_param(b) for b in response.content]})
        calls = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not calls:
            answer.text = "".join(b.text for b in response.content if b.type == "text").strip()
            if response.stop_reason == "max_tokens":
                answer.cut_off = True
                answer.text += "\n\n*(Answer cut off at the output limit.)*"
            break

        results = []
        for block in calls:
            step, content, is_error = await _run_tool(mcp, block.name, block.input)
            answer.steps.append(step)
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error}
            )
        messages.append({"role": "user", "content": results})
    else:
        answer.stopped_early = True
        answer.text = (
            f"I stopped after {settings.max_steps} tool calls without a final answer. "
            "Try a narrower question."
        )

    answer.cost_usd = answer.usage.cost(settings)
    answer.latency_ms = round((time.perf_counter() - started) * 1000)
    _write_trace(Path(settings.state_dir) / "traces.jsonl", answer)
    return answer


async def _tool_specs(mcp: Client) -> list[dict[str, Any]]:
    listed = (await mcp.list_tools()).tools
    specs = [
        {"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in listed
    ]
    # The tool list and system prompt are identical on every call; cache them.
    specs[-1]["cache_control"] = {"type": "ephemeral"}
    return specs


async def _run_tool(mcp: Client, name: str, arguments: dict[str, Any]) -> tuple[Step, str, bool]:
    started = time.perf_counter()
    result = await mcp.call_tool(name, arguments)
    ms = round((time.perf_counter() - started) * 1000)
    text = "".join(getattr(c, "text", "") for c in result.content)

    if result.is_error:
        return Step(name, arguments, ok=False, ms=ms, summary=text), text, True

    payload = _json(text)
    summary, sql = name, None
    # Query results carry a list of rows; describe_table's "rows" is a count.
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        sql = payload.get("sql")
        cut = " (truncated)" if payload.get("truncated") else ""
        summary = f"{payload.get('row_count', len(payload['rows']))} rows{cut}"
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n[result cut short; ask for fewer rows or aggregate]"
    return Step(name, arguments, ok=True, ms=ms, summary=summary, sql=sql), text, False


def _as_param(block) -> dict[str, Any]:
    """Echo a response block back in request form, without response-only fields.

    Thinking blocks must go back unchanged, signature included, or the API
    rejects the next turn of a tool-use conversation.
    """
    match block.type:
        case "text":
            return {"type": "text", "text": block.text}
        case "tool_use":
            return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
        case "thinking":
            return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
        case "redacted_thinking":
            return {"type": "redacted_thinking", "data": block.data}
        case _:
            return block.model_dump(mode="json", exclude_none=True)


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _write_trace(path: Path, answer: Answer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": datetime.now(UTC).isoformat(timespec="seconds"), **answer.to_json()}
    with path.open("a") as file:
        file.write(json.dumps(record, default=str) + "\n")
