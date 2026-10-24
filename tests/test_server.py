import json

import anyio
from mcp import Client

from silo.server import build_server


def call(server, tool, arguments=None):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, arguments or {})

    return anyio.run(go)


def test_tools_are_listed_as_read_only(toolbox):
    async def go():
        async with Client(build_server(toolbox)) as client:
            return (await client.list_tools()).tools

    tools = anyio.run(go)
    assert {t.name for t in tools} == {
        "list_tables",
        "describe_table",
        "run_query",
        "list_metrics",
        "query_metric",
        "data_freshness",
    }
    assert all(t.annotations.read_only_hint for t in tools)


def test_query_metric_over_mcp(toolbox):
    result = call(
        build_server(toolbox),
        "query_metric",
        {"metric": "corn_close", "grain": "month", "start": "2026-06-01"},
    )
    assert not result.is_error
    payload = json.loads(result.content[0].text)
    assert payload["columns"] == ["period", "corn_close"]


def test_rejections_reach_the_agent_with_their_reason(toolbox):
    result = call(build_server(toolbox), "run_query", {"sql": "drop table fct_price_daily"})
    assert result.is_error
    assert "read-only" in result.content[0].text


def test_sql_errors_reach_the_agent_with_the_fix(toolbox):
    result = call(build_server(toolbox), "run_query", {"sql": "select close from fct_price_daily"})
    assert result.is_error
    assert "close_price" in result.content[0].text
