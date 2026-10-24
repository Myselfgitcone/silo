"""What the agent can do, as plain functions over the lake.

The MCP server is a thin wrapper around this class, so everything here is
testable without a protocol in the way.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from silo.guard import QueryRejected, check
from silo.lake import Lake, LakeUnavailable
from silo.semantic import Metric, compile_metric, load_metrics
from silo.settings import Settings

SAMPLE_ROWS = 5

log = logging.getLogger(__name__)


class Toolbox:
    def __init__(self, lake: Lake, settings: Settings, metrics: dict[str, Metric] | None = None):
        self.lake = lake
        self.settings = settings
        self.metrics = metrics if metrics is not None else load_metrics()
        self._next_check = 0.0
        self._refresh_lock = threading.Lock()

    def refresh_if_due(self) -> None:
        """Pick up a newer Grainflow build, at most once per refresh interval."""
        if time.monotonic() < self._next_check or not self._refresh_lock.acquire(blocking=False):
            return
        try:
            self.lake.load()
        except LakeUnavailable as error:
            # Keep answering from the last good build rather than going dark.
            log.warning("lake refresh failed, serving the build from %s: %s", self.lake.generated_at, error)
        finally:
            self._next_check = time.monotonic() + self.settings.refresh_s
            self._refresh_lock.release()

    def list_tables(self) -> dict[str, Any]:
        return {
            "lake_built_at": self.lake.generated_at,
            "tables": [
                {"name": t.name, "rows": t.rows, "description": t.description}
                for t in self.lake.tables.values()
            ],
        }

    def describe_table(self, table: str) -> dict[str, Any]:
        found = self.lake.tables.get(table)
        if found is None:
            raise QueryRejected(f"There's no table named {table}. Tables: {', '.join(self.lake.tables)}.")
        sample = self.lake.execute(
            f'select * from "{found.name}" limit {SAMPLE_ROWS}', self.settings.query_timeout_s
        )
        return {
            "name": found.name,
            "description": found.description,
            "rows": found.rows,
            "columns": [
                {"name": c.name, "type": c.type, "description": c.description} for c in found.columns
            ],
            "sample": {"columns": sample.columns, "rows": sample.rows},
        }

    def run_query(self, sql: str) -> dict[str, Any]:
        checked = check(sql, set(self.lake.tables), self.settings.max_rows)
        result = self.lake.execute(checked.sql, self.settings.query_timeout_s)
        truncated = len(result.rows) > self.settings.max_rows
        return {
            "sql": sql.strip(),
            "columns": result.columns,
            "rows": result.rows[: self.settings.max_rows],
            "row_count": min(len(result.rows), self.settings.max_rows),
            "truncated": truncated,
            "elapsed_ms": result.elapsed_ms,
        }

    def list_metrics(self) -> dict[str, Any]:
        return {"metrics": [m.describe() for m in self.metrics.values()]}

    def query_metric(
        self,
        metric: str,
        group_by: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        start: str | None = None,
        end: str | None = None,
        grain: str | None = None,
        order: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        found = self.metrics.get(metric)
        if found is None:
            raise QueryRejected(f"There's no metric named {metric}. Metrics: {', '.join(self.metrics)}.")
        sql = compile_metric(found, group_by, filters, start, end, grain, order, limit)
        return {"metric": found.describe(), **self.run_query(sql)}

    def data_freshness(self) -> dict[str, Any]:
        """When each table's data ends, and what the contracts rejected lately."""
        latest = {}
        for metric in self.metrics.values():
            if metric.table in latest or metric.table not in self.lake.tables:
                continue
            result = self.lake.execute(
                f'select max("{metric.time}") from "{metric.table}"', self.settings.query_timeout_s
            )
            latest[metric.table] = result.rows[0][0]

        quarantined = []
        if "rpt_quarantine_daily" in self.lake.tables:
            quarantined = self.lake.execute(
                "select contract, reason, sum(rejected_records) as records "
                "from rpt_quarantine_daily "
                "where ingested_date >= (select max(ingested_date) from rpt_quarantine_daily) - 30 "
                "group by all order by records desc",
                self.settings.query_timeout_s,
            ).rows
        return {
            "lake_built_at": self.lake.generated_at,
            "latest_data": latest,
            "quarantined_last_30_days": [
                {"contract": c, "reason": r, "records": n} for c, r, n in quarantined
            ],
        }
