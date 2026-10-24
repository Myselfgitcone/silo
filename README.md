# Silo

An MCP server that lets AI agents answer questions about grain markets from the
[Grainflow](https://github.com/Myselfgitcone/grainflow) lakehouse, safely.

Ask *"How did Iowa's corn ratings move this season, and what did corn do after each drop?"*
and the agent picks a named metric or writes one read-only query, runs it
against Grainflow's gold tables, and answers with the numbers and the SQL it ran.

```
Grainflow daily build ──► gold tables as Parquet + manifest.json (served by Vercel)
                                         │  downloaded on start, refreshed every 15 min
                                         ▼
                         Silo: in-memory DuckDB, locked down
                                         │
          list_tables · describe_table · list_metrics · query_metric
                         run_query · data_freshness
                                         │  MCP (stdio or Streamable HTTP)
                                         ▼
              any MCP client, the Silo agent, or the public page
```

## Why it's built this way

**Metrics before SQL.** `src/silo/metrics.yml` defines what can be asked by name:
the table, the aggregate, the time column and the dimensions it can be cut by.
The agent is told to try `query_metric` first, so it can't average the wrong
column or invent a join. Metrics also carry the rules an analyst would apply:
crop ratings must name a commodity (corn and soybeans never get averaged
together) and stay inside the 18 states USDA uses for its national number, and
price metrics must name a contract. Identifiers come from the YAML and values are quoted by
sqlglot, so the only free text that reaches SQL is a literal.

**Two locks on free-form SQL.** `run_query` exists for questions a metric can't express.
1. `guard.py` parses the SQL with sqlglot and allows exactly one `SELECT` over
   the published tables: no writes, no `ATTACH`/`INSTALL`/`COPY`/`PRAGMA`, no
   table functions such as `read_csv`, no file paths, no schema-qualified names.
   Every rejection says what was wrong, so the agent can fix its query.
2. The engine itself runs with `enable_external_access = false` and a locked
   configuration. Even a query that slipped past the guard can't read a file or
   reach the network; there's a test that proves it.

Results are capped (`truncated: true` tells the agent it saw a slice) and
queries are interrupted after a timeout.

**Docs come from dbt.** Grainflow's lake manifest carries the table and column
descriptions written in its dbt models, so the agent reads the same
documentation an analyst would, with no second copy to drift.

**No database to run.** The lake is a few MB of Parquet. Silo downloads it, loads
it into memory and checks for a newer build every 15 minutes.

## The analyst

`silo ask` is an agent built as an ordinary MCP client of the server above. It
lists the server's tools, gives them to the model (Haiku, the cheapest tier),
runs what the model calls and feeds results back until it answers. Rejections
go back too, so the model reads why its SQL was refused and fixes it.

```bash
uv run silo ask "Which corn belt states lost the most good/excellent rating since July?" --trace
```

Each answer prints the SQL behind it and its cost. Cost control is built in:

- **Daily budget.** A ledger in `.silo/spend.json` stops all model calls once
  `SILO_DAILY_BUDGET_USD` (default $0.50) is spent for the UTC day.
- **Step cap.** At most `SILO_MAX_STEPS` tool calls per question.
- **Prompt caching.** The system prompt and tool list are the same on every
  call and are marked cacheable.
- **Small results.** Rows are capped server-side and oversized results are cut
  before they reach the prompt.

Every answer is appended to `.silo/traces.jsonl`: question, each tool call with
its arguments, latency and outcome, the SQL, tokens and cost. That file is
what the eval suite and the admin view will read.

Tests drive the loop with a scripted model, so CI never calls a paid API.

## Evals

`evals/cases.yml` holds questions with known answers: the worst and best corn
states this week, the latest corn settle, 2022's high, wheat volatility, the
signal Grainflow measures, plus a request to delete data and a question the
lake can't answer. Each case carries the SQL that computes the right answer,
run against the lake at eval time, so expectations follow the data as it
refreshes.

Grading needs no second model: an answer passes when it mentions every
expected value. Numbers match within the rounding the answer uses, state codes
match their names, dates match ISO or "October 18" form.

```bash
uv run silo eval                     # all cases, exits 1 below 85%
uv run silo eval --only worst_corn_state_latest_week
```

The `evals` workflow runs them on any PR that touches the prompt, tools or
metrics, weekly, and on demand, with the pass table in the job summary. The
first real run caught the agent calling Iowa the top corn state when four
states were higher; the fix was to let the database rank (`order` and `limit`
on `query_metric`) instead of the model reading rows by eye.

## The public page

`silo serve --http` serves three things from one process: the MCP endpoint at
`/mcp`, a chat page at `/`, and `/api/ask` behind it. The model key stays on
the server. Spend has three limits:

- the five example questions are answered once per lake build and cached,
- free-text questions are limited per visitor (`SILO_ASKS_PER_HOUR`, default 6),
- the daily budget stops the model for everyone once it's spent.

Every answer on the page shows each tool call, any refusal with its reason, the
SQL, tokens, cost and latency.

### Deploy on Railway

The Dockerfile runs `silo serve --http` as an unprivileged user that can write
only `/app/.silo`. In Railway: new project from this repo, add
`ANTHROPIC_API_KEY` as a variable, and generate a public domain pointed at the
port the deploy log shows Uvicorn listening on (Railway's `PORT`, usually
8080). Silo also reads `RAILWAY_PUBLIC_DOMAIN` for the MCP host check.

The spend ledger lives in the container, so a redeploy resets the day's count;
mount a volume at `/app/.silo` to keep it. The server refuses to start if it
can't write there, since the budget can't be enforced without its ledger.

## Run it

```bash
uv sync
uv run silo sql "select symbol, max(trade_date) from fct_price_daily group by 1"
uv run silo metric pct_good_excellent --where commodity=CORN --by state_alpha --grain month
uv run silo serve                    # stdio, for desktop MCP clients
uv run silo serve --http --port 8000 # Streamable HTTP at /mcp
```

Point `SILO_LAKE_URL` at a local folder (for example `tests/fixtures/lake`) to
work offline. Settings are in `.env.example`. Add `--json` for raw output.

To use it from an MCP client such as Cursor or VS Code:

```json
{
  "mcpServers": {
    "silo": { "command": "uv", "args": ["--directory", "/path/to/silo", "run", "silo", "serve"] }
  }
}
```

## Tests

```bash
uv run pytest -q
```

The fixture lake in `tests/fixtures/lake` is real output of `grainflow publish lake`,
built from Grainflow's dbt fixtures. Tests cover the guard (a table of unsafe
queries and the reason each is refused), the engine lockdown and timeout, every
metric compiling and running, and the tools end to end over an in-process MCP client.

## Roadmap

- [x] Lake export in Grainflow, metrics layer, SQL guard, MCP server
- [x] Agent loop with a per-day cost budget and tracing
- [x] Eval suite in CI: questions with known answers, fail the PR if accuracy drops
- [x] Public page and MCP endpoint on Railway
