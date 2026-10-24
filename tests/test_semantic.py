import pytest

from silo.guard import QueryRejected
from silo.semantic import compile_metric, load_metrics

METRICS = load_metrics()


def test_every_metric_compiles_and_runs(lake):
    for metric in METRICS.values():
        assert metric.table in lake.tables, metric.name
        lake.execute(compile_metric(metric, group_by=list(metric.required), grain="month"), timeout_s=5)


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


def test_metrics_that_would_mix_things_refuse_to_guess():
    with pytest.raises(QueryRejected, match="mixes different symbol values"):
        compile_metric(METRICS["futures_close"])
    compile_metric(METRICS["futures_close"], filters={"symbol": "ZC=F"})
    compile_metric(METRICS["futures_close"], group_by=["symbol"])


def test_crop_ratings_stay_inside_the_crop_progress_states(lake):
    sql = compile_metric(
        METRICS["pct_good_excellent"], group_by=["state_alpha"], filters={"commodity": "CORN"}
    )
    assert "ref_crop_progress_states" in sql
    states = {row[0] for row in lake.execute(sql, timeout_s=5).rows}
    allowed = lake.execute(
        "select state_alpha from ref_crop_progress_states where commodity = 'CORN'", timeout_s=5
    ).rows
    assert states <= {row[0] for row in allowed}


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
        compile_metric(METRICS["pct_good_excellent"], **{"filters": {"commodity": "CORN"}, **kwargs})


def test_rankings_are_sorted_by_the_database(lake):
    sql = compile_metric(
        METRICS["pct_good_excellent"],
        group_by=["state_alpha"],
        filters={"commodity": "CORN"},
        order="asc",
        limit=1,
    )
    assert sql.endswith("GROUP BY 1 ORDER BY pct_good_excellent ASC LIMIT 1")
    worst = lake.execute(sql, timeout_s=5).rows
    every = lake.execute(sql.replace(" LIMIT 1", ""), timeout_s=5).rows
    assert worst == every[:1]
    assert worst[0][1] == min(row[1] for row in every)


def test_bad_order_and_limit_are_rejected():
    with pytest.raises(QueryRejected, match="asc or desc"):
        compile_metric(METRICS["corn_close"], order="up")
    with pytest.raises(QueryRejected, match="at least 1"):
        compile_metric(METRICS["corn_close"], limit=0)
