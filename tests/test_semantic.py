import pytest

from silo.guard import QueryRejected
from silo.semantic import compile_metric, load_metrics

METRICS = load_metrics()


def test_every_metric_compiles_and_runs(lake):
    for metric in METRICS.values():
        assert metric.table in lake.tables, metric.name
        lake.execute(compile_metric(metric, grain="month"), timeout_s=5)


def test_grain_group_and_filters():
    sql = compile_metric(
        METRICS["pct_good_excellent"],
        group_by=["state_alpha"],
        filters={"state_alpha": ["IA", "IL"], "commodity": "CORN"},
        start="2024-01-01",
        grain="month",
    )
    assert "DATE_TRUNC('MONTH', week_ending)" in sql
    assert "state_alpha IN ('IA', 'IL')" in sql
    assert "commodity IN ('CORN')" in sql
    assert sql.endswith("GROUP BY 1, 2 ORDER BY 1, 2")


def test_filter_values_are_quoted_not_injected():
    sql = compile_metric(METRICS["precipitation"], filters={"state_alpha": "IA') or 1=1 --"})
    assert "IN ('IA'') or 1=1 --')" in sql


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"group_by": ["symbol"]}, "can't be cut by symbol"),
        ({"filters": {"county": "Story"}}, "can't be cut by county"),
        ({"grain": "fortnight"}, "Unknown grain"),
        ({"start": "last tuesday"}, "YYYY-MM-DD"),
    ],
)
def test_bad_requests_explain_themselves(kwargs, reason):
    with pytest.raises(QueryRejected, match=reason):
        compile_metric(METRICS["pct_good_excellent"], **kwargs)
