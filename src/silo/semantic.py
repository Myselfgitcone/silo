"""Named metrics compiled to SQL.

The agent asks for "pct_good_excellent by state_alpha, monthly, for IA and IL
since 2024" and gets back rows plus the SQL that produced them. Every
identifier comes from metrics.yml and every value is quoted by sqlglot, so the
only free text that reaches SQL is a literal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from sqlglot import exp

from silo.guard import QueryRejected

METRICS_FILE = Path(__file__).with_name("metrics.yml")
GRAINS = ("day", "week", "month", "quarter", "year")


@dataclass(frozen=True)
class Metric:
    name: str
    description: str
    unit: str
    table: str
    time: str
    expression: str
    dimensions: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "unit": self.unit,
            "table": self.table,
            "time_column": self.time,
            "dimensions": list(self.dimensions),
        }


def load_metrics(path: Path = METRICS_FILE) -> dict[str, Metric]:
    raw = yaml.safe_load(path.read_text())["metrics"]
    return {
        name: Metric(
            name=name,
            description=" ".join(spec["description"].split()),
            unit=spec["unit"],
            table=spec["table"],
            time=spec["time"],
            expression=spec["expression"],
            dimensions=tuple(spec.get("dimensions", [])),
        )
        for name, spec in raw.items()
    }


def compile_metric(
    metric: Metric,
    group_by: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    start: str | None = None,
    end: str | None = None,
    grain: str | None = None,
) -> str:
    """Build the SQL for one metric. Raises QueryRejected on a bad request."""
    group_by = group_by or []
    filters = filters or {}

    for name in [*group_by, *filters]:
        if name not in metric.dimensions:
            allowed = ", ".join(metric.dimensions) or "none"
            raise QueryRejected(f"{metric.name} can't be cut by {name}. Dimensions: {allowed}.")
    if grain is not None and grain not in GRAINS:
        raise QueryRejected(f"Unknown grain {grain}. Use one of: {', '.join(GRAINS)}.")

    time = exp.column(metric.time)
    selects: list[exp.Expression] = []
    if grain:
        period = exp.cast(exp.func("date_trunc", exp.Literal.string(grain), time), exp.DataType.build("date"))
        selects.append(period.as_("period"))
    selects += [exp.column(name) for name in group_by]
    # Group and sort by position so the SQL shown to people stays readable.
    groups = [exp.Literal.number(i + 1) for i in range(len(selects))]
    value = exp.maybe_parse(metric.expression, dialect="duckdb")
    selects.append(value.as_(metric.name))

    query = exp.select(*selects).from_(metric.table)
    for name, wanted in filters.items():
        values = wanted if isinstance(wanted, list) else [wanted]
        query = query.where(exp.column(name).isin(*[_literal(v) for v in values]))
    if start:
        query = query.where(time >= exp.cast(exp.Literal.string(_iso(start)), "date"))
    if end:
        query = query.where(time <= exp.cast(exp.Literal.string(_iso(end)), "date"))
    if groups:
        query = query.group_by(*groups).order_by(*groups)
    return query.sql(dialect="duckdb")


def _literal(value: Any) -> exp.Expression:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return exp.Literal.string(str(value))
    return exp.Literal.number(value)


def _iso(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise QueryRejected(f"Dates must be YYYY-MM-DD, got {value!r}.") from None
