"""The agent loop against a scripted model, so CI never calls a paid API."""

import json

import anyio
import pytest
from anthropic.types import Message

from silo.agent import answer
from silo.budget import Budget, BudgetExceeded
from silo.server import build_server


def reply(*blocks, stop="end_turn", tokens=(1000, 100)):
    return Message.model_validate(
        {
            "id": "msg",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": list(blocks),
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {
                "input_tokens": tokens[0],
                "output_tokens": tokens[1],
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
    )


def tool(name, **arguments):
    return {"type": "tool_use", "id": f"call-{name}", "name": name, "input": arguments}


def text(value):
    return {"type": "text", "text": value}


class ScriptedModel:
    """Plays back replies in order and keeps every request it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.messages = self

    async def create(self, **request):
        self.requests.append(json.loads(json.dumps(request)))
        return self.replies.pop(0)


@pytest.fixture
def run(toolbox, settings, tmp_path):
    settings.state_dir = str(tmp_path)
    budget = Budget(tmp_path / "spend.json", settings.daily_budget_usd)

    def go(question, model):
        return anyio.run(answer, question, build_server(toolbox), model, settings, budget)

    go.budget = budget
    go.trace = tmp_path / "traces.jsonl"
    return go


def test_tool_call_then_answer(run, settings):
    model = ScriptedModel(
        reply(tool("query_metric", metric="corn_close", grain="month"), stop="tool_use"),
        reply(text("Corn closed lower in August than in June.")),
    )
    answer = run("How did corn prices move this summer?", model)

    assert answer.text == "Corn closed lower in August than in June."
    assert [s.tool for s in answer.steps] == ["query_metric"]
    assert answer.sql[0].startswith("SELECT CAST(DATE_TRUNC('MONTH', week_start)")

    # The model saw the real rows, and the tools came with a cache marker.
    result = model.requests[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and not result["is_error"]
    assert "corn_close" in result["content"]
    assert model.requests[0]["tools"][-1]["cache_control"] == {"type": "ephemeral"}

    # 2 calls x (1000 in, 100 out) at $0.10 / $0.50 per million tokens.
    assert answer.cost_usd == pytest.approx(2 * (1000 * 0.10 + 100 * 0.50) / 1e6)
    assert run.budget.spent_today() == pytest.approx(answer.cost_usd)
    traced = json.loads(run.trace.read_text().splitlines()[-1])
    assert traced["question"] == "How did corn prices move this summer?"
    assert traced["sql"] == answer.sql


def test_thinking_blocks_go_back_unchanged(run):
    thinking = {"type": "thinking", "thinking": "Check the metric list first.", "signature": "sig-123"}
    model = ScriptedModel(
        reply(thinking, tool("list_metrics"), stop="tool_use"),
        reply(thinking, text("There are 11 metrics.")),
    )
    answer = run("What can you measure?", model)

    echoed = model.requests[1]["messages"][1]["content"]
    assert echoed[0] == thinking
    assert echoed[1]["type"] == "tool_use"
    assert answer.text == "There are 11 metrics."


def test_unexpected_errors_are_not_buried_in_exception_groups(run):
    class Broken:
        messages = None

        @staticmethod
        async def create(**_):
            raise ConnectionError("network down")

    Broken.messages = Broken
    with pytest.raises(ConnectionError, match="network down"):
        run("Anything", Broken)


def test_a_rejected_query_goes_back_to_the_model_to_fix(run):
    model = ScriptedModel(
        reply(tool("run_query", sql="delete from fct_price_daily"), stop="tool_use"),
        reply(tool("run_query", sql="select count(*) as n from fct_price_daily"), stop="tool_use"),
        reply(text("There are 420 daily price rows.")),
    )
    answer = run("How many price rows are there?", model)

    rejection = model.requests[1]["messages"][-1]["content"][0]
    assert rejection["is_error"] is True
    assert "read-only" in rejection["content"]
    assert [s.ok for s in answer.steps] == [False, True]
    assert answer.sql == ["select count(*) as n from fct_price_daily"]


def test_the_loop_stops_at_max_steps(run, settings):
    settings.max_steps = 2
    looping = [reply(tool("list_tables"), stop="tool_use") for _ in range(2)]
    answer = run("Keep going", ScriptedModel(*looping))
    assert answer.stopped_early
    assert "stopped after 2 tool calls" in answer.text


def test_no_model_call_once_the_daily_budget_is_spent(run, settings):
    run.budget.record(settings.daily_budget_usd)
    model = ScriptedModel()
    with pytest.raises(BudgetExceeded, match=r"budget of \$0\.50 is used up"):
        run("Anything", model)
    assert model.requests == []


def test_an_answer_cut_off_at_the_output_limit_says_so(run):
    answer = run("Explain everything", ScriptedModel(reply(text("Corn rose because"), stop="max_tokens")))
    assert answer.cut_off
    assert answer.text.endswith("*(Answer cut off at the output limit.)*")
