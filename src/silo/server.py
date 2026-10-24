"""The MCP server: Silo's tools, for any agent that speaks MCP."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from silo.guard import QueryRejected
from silo.lake import QueryTimeout
from silo.tools import Toolbox

INSTRUCTIONS = """\
Silo answers questions about grain markets from the Grainflow lakehouse: CBOT
futures prices, NOAA weather for the corn belt and USDA crop condition ratings.

How to work:
1. Prefer query_metric. Call list_metrics to see what's defined.
2. For anything a metric can't express, call describe_table, then run_query
   with one read-only SELECT over the lake tables.
3. Check data_freshness when the question is about "now" or "this week".
4. In your answer, give the numbers, their units and the SQL you ran.

Prices are in cents per bushel. Crop ratings are only published during the
growing season, so gaps outside roughly April to November are expected.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)


def build_server(toolbox: Toolbox) -> MCPServer:
    server = MCPServer("silo", instructions=INSTRUCTIONS, website_url="https://github.com/Myselfgitcone/silo")

    def call(fn, *args: Any, **kwargs: Any) -> dict[str, Any]:
        toolbox.refresh_if_due()
        try:
            return fn(*args, **kwargs)
        except (QueryRejected, QueryTimeout) as error:
            # These messages are written for the agent, so pass them through.
            raise ToolError(str(error)) from None

    @server.tool(annotations=READ_ONLY)
    def list_tables() -> dict[str, Any]:
        """List the lake tables with row counts and descriptions."""
        return call(toolbox.list_tables)

    @server.tool(annotations=READ_ONLY)
    def describe_table(
        table: Annotated[str, Field(description="Table name from list_tables.")],
    ) -> dict[str, Any]:
        """Columns, types, descriptions and a few sample rows for one table."""
        return call(toolbox.describe_table, table)

    @server.tool(annotations=READ_ONLY)
    def run_query(
        sql: Annotated[str, Field(description="One read-only SELECT over the lake tables.")],
    ) -> dict[str, Any]:
        """Run one read-only SQL query (DuckDB dialect). Results are capped; check `truncated`."""
        return call(toolbox.run_query, sql)

    @server.tool(annotations=READ_ONLY)
    def list_metrics() -> dict[str, Any]:
        """List the named metrics with their units and the dimensions they can be cut by."""
        return call(toolbox.list_metrics)

    @server.tool(annotations=READ_ONLY)
    def query_metric(
        metric: Annotated[str, Field(description="Metric name from list_metrics.")],
        group_by: Annotated[list[str] | None, Field(description="Dimensions to split by.")] = None,
        filters: Annotated[
            dict[str, Any] | None,
            Field(description='Dimension values to keep, e.g. {"state_alpha": ["IA", "IL"]}.'),
        ] = None,
        start: Annotated[str | None, Field(description="First date, YYYY-MM-DD.")] = None,
        end: Annotated[str | None, Field(description="Last date, YYYY-MM-DD.")] = None,
        grain: Annotated[
            str | None, Field(description="Time bucket: day, week, month, quarter or year.")
        ] = None,
    ) -> dict[str, Any]:
        """Compute a named metric, optionally by time bucket and dimensions. Returns the SQL used."""
        return call(toolbox.query_metric, metric, group_by, filters, start, end, grain)

    @server.tool(annotations=READ_ONLY)
    def data_freshness() -> dict[str, Any]:
        """When the lake was built, the latest date in each table, and recent quarantined records."""
        return call(toolbox.data_freshness)

    return server
