from __future__ import annotations

import json
import logging

import typer

from silo.lake import Lake
from silo.settings import get_settings
from silo.tools import Toolbox

app = typer.Typer(no_args_is_help=True, help="Silo: an MCP server over the Grainflow lake.")


def _toolbox() -> Toolbox:
    settings = get_settings()
    lake = Lake(settings.lake_url)
    lake.load()
    return Toolbox(lake, settings)


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
def sql(query: str) -> None:
    """Run one query through the same guard the agent uses."""
    typer.echo(json.dumps(_toolbox().run_query(query), indent=2, default=str))


@app.command()
def metric(
    name: str,
    by: list[str] = typer.Option([], "--by", help="Dimension to group by; repeatable."),
    grain: str | None = typer.Option(None),
    start: str | None = typer.Option(None),
    end: str | None = typer.Option(None),
) -> None:
    """Compute a named metric from the command line."""
    result = _toolbox().query_metric(name, group_by=by, start=start, end=end, grain=grain)
    typer.echo(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    app()
