from pathlib import Path

import anyio
import pytest

from silo.evals import Case, expected_values, grade, load_cases, run_cases, summary_markdown
from tests.test_agent import ScriptedModel, reply, text, tool

CASES = Path(__file__).parents[1] / "evals" / "cases.yml"


def test_every_truth_query_runs_against_the_lake(lake):
    for case in load_cases(CASES):
        if case.truth:
            lake.execute(case.truth, timeout_s=5)  # raises on a broken query
        else:
            assert case.mentions_any, case.id


@pytest.mark.parametrize(
    ("answer", "expected", "ok"),
    [
        ("North Carolina was worst at 14%.", ["NC", "14"], True),
        ("NC was worst at 14.2%.", ["NC", "14"], True),
        ("Iowa was worst at 14%.", ["NC", "14"], False),
        ("NC was worst at 41%.", ["NC", "14"], False),
        ("The correlation was \u22120.16.", ["-0.16"], True),
        ("Corn gained about 0.9% the next week.", ["0.93"], True),
        ("Corn gained about 1% the next week.", ["0.93"], False),
        ("It settled at $4.12 a bushel.", ["412.25|4.12"], True),
        ("Ratings fell 9.2 points.", ["-9.2"], True),
        ("The latest report is for October 18.", ["2026-10-18|October 18"], True),
        ("Data runs through 2026-10-14.", ["2026-10-18|October 18"], False),
    ],
)
def test_grading(answer, expected, ok):
    assert (grade(answer, expected, Case("c", "q")) == []) is ok


def test_mentions_any():
    case = Case("c", "q", mentions_any=("read-only", "can't"))
    assert grade("I have read-only access.", [], case) == []
    assert grade("Done, all rows deleted.", [], case) == ["none of ['read-only', \"can't\"]"]


def test_expected_values_come_from_the_lake(lake):
    case = Case(
        "c",
        "q",
        truth="select symbol, max(trade_date) as last from fct_price_daily group by 1 order by 1 limit 1",
        mentions=("{symbol}", "{last}"),
    )
    assert expected_values(case, lake) == ["ZC=F", "2026-09-11"]


def test_run_cases_grades_each_answer(toolbox, settings, tmp_path):
    from silo.budget import Budget

    settings.state_dir = str(tmp_path)
    case = Case("rows", "How many symbols?", truth="select count(*) as n from ref_symbols", mentions=("{n}",))
    model = ScriptedModel(
        reply(tool("run_query", sql="select count(*) from ref_symbols"), stop="tool_use"),
        reply(text("There are 4 symbols.")),
    )
    budget = Budget(tmp_path / "spend.json", 1.0)
    results = anyio.run(run_cases, [case], toolbox, model, settings, budget)
    assert results[0].passed
    assert "1/1 passed" in summary_markdown(results)
