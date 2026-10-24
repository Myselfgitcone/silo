from __future__ import annotations

import json
import logging
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from silo.guard import QueryRejected
from silo.lake import Lake, LakeUnavailable, QueryTimeout
from silo.settings import get_settings
from silo.tools import Toolbox

console = Console()
err = Console(stderr=True)

app = typer.Typer(no_args_is_help=True, help="Silo: an MCP server over the Grainflow lake.")


def _toolbox() -> Toolbox:
    settings = get_settings()
    lake = Lake(settings.lake_url)
    try:
        lake.load()
    except LakeUnavailable as error:
        _fail(str(error))
    return Toolbox(lake, settings)


def _fail(message: str) -> None:
    err.print(f"[bold red]Error:[/] {message}")
    raise typer.Exit(1)


def _show(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(result, indent=2, default=str))
        return
    table = Table(*result["columns"], header_style="bold", box=None, pad_edge=False)
    for row in result["rows"]:
        table.add_row(*(f"{v:,.4g}" if isinstance(v, float) else str(v) for v in row))
    console.print(f"[dim]{result['sql']}[/]\n")
    console.print(table)
    more = " (truncated; narrow the query)" if result["truncated"] else ""
    console.print(f"\n[dim]{result['row_count']} rows in {result['elapsed_ms']} ms{more}[/]")


@app.command()
def serve(
    http: bool = typer.Option(False, help="Serve Streamable HTTP instead of stdio."),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000, envvar="PORT"),
) -> None:
    """Run the MCP server. stdio for desktop clients, --http for a deployed server."""
    from mcp.server.transport_security import TransportSecuritySettings

    from silo.server import build_server

    # stdout belongs to the protocol on stdio, so logs go to stderr.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    server = build_server(_toolbox())
    if not http:
        server.run()
        return

    hosts = [h.strip() for h in get_settings().allowed_hosts.split(",") if h.strip()]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*hosts, f"localhost:{port}", f"127.0.0.1:{port}"],
        allowed_origins=[f"https://{h}" for h in hosts],
    )
    server.run(
        "streamable-http",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )


@app.command()
def sql(
    query: str,
    as_json: bool = typer.Option(False, "--json", help="Print raw JSON instead of a table."),
) -> None:
    """Run one query through the same guard the agent uses."""
    toolbox = _toolbox()
    try:
        _show(toolbox.run_query(query), as_json)
    except (QueryRejected, QueryTimeout) as error:
        _fail(str(error))


@app.command()
def metric(
    name: str,
    by: list[str] = typer.Option([], "--by", help="Dimension to group by; repeatable."),
    grain: str | None = typer.Option(None),
    where: list[str] = typer.Option([], "--where", help="Filter as dimension=value; repeatable."),
    start: str | None = typer.Option(None),
    end: str | None = typer.Option(None),
    as_json: bool = typer.Option(False, "--json", help="Print raw JSON instead of a table."),
) -> None:
    """Compute a named metric from the command line."""
    filters: dict[str, list[str]] = {}
    for item in where:
        key, _, value = item.partition("=")
        if not value:
            _fail(f"--where takes dimension=value, got {item!r}.")
        filters.setdefault(key, []).append(value)
    toolbox = _toolbox()
    try:
        result = toolbox.query_metric(name, by, filters, start, end, grain)
    except (QueryRejected, QueryTimeout) as error:
        _fail(str(error))
    _show(result, as_json)


@app.command()
def ask(
    question: str,
    show_trace: bool = typer.Option(False, "--trace", help="Show each tool call."),
) -> None:
    """Ask the analyst a question in plain English. Needs ANTHROPIC_API_KEY."""
    import os
    from pathlib import Path

    import anyio
    from anthropic import AsyncAnthropic
    from dotenv import load_dotenv
    from rich.markdown import Markdown

    from silo.agent import answer as run_agent
    from silo.budget import Budget, BudgetExceeded
    from silo.server import build_server

    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _fail("Set ANTHROPIC_API_KEY in your shell or .env first.")
    settings = get_settings()
    toolbox = _toolbox()
    budget = Budget(Path(settings.state_dir) / "spend.json", settings.daily_budget_usd)

    try:
        answer = anyio.run(run_agent, question, build_server(toolbox), AsyncAnthropic(), settings, budget)
    except BudgetExceeded as error:
        _fail(str(error))

    console.print(Markdown(answer.text))
    if show_trace:
        console.print()
        for i, step in enumerate(answer.steps, 1):
            mark = "[green]ok[/]" if step.ok else "[red]rejected[/]"
            console.print(f"[dim]{i}.[/] {step.tool} {mark} [dim]{step.ms} ms · {step.summary}[/]")
    for sql in answer.sql:
        console.print(f"\n[dim]{sql}[/]")
    u = answer.usage
    console.print(
        f"\n[dim]{len(answer.steps)} tool calls · {u.input_tokens + u.cache_read_tokens:,} in / "
        f"{u.output_tokens:,} out tokens · ${answer.cost_usd:.4f} · {answer.latency_ms / 1000:.1f}s · "
        f"${budget.spent_today():.4f} of ${settings.daily_budget_usd:.2f} used today[/]"
    )


if __name__ == "__main__":
    app()
