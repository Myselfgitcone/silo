import pytest

from silo.guard import QueryRejected, check

TABLES = {"fct_price_daily", "fct_crop_condition_weekly"}


def test_a_plain_select_is_capped():
    checked = check("select symbol from fct_price_daily;", TABLES, max_rows=100)
    assert checked.sql.endswith("limit 101")
    assert checked.tables == {"fct_price_daily"}


def test_ctes_joins_and_unions_are_fine():
    sql = """
        with corn as (select * from fct_price_daily where symbol = 'ZC=F')
        select c.trade_date, w.pct_good_excellent
        from corn as c join fct_crop_condition_weekly as w on w.week_ending = c.trade_date
        union all
        select trade_date, null from fct_price_daily
    """
    assert check(sql, TABLES, 10).tables == TABLES


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("drop table fct_price_daily", "Only SELECT"),
        ("insert into fct_price_daily values (1)", "Only SELECT"),
        ("select 1; select 2", "one statement"),
        ("attach 'other.db'", "Only SELECT"),
        ("install httpfs", "Only SELECT"),
        ("load httpfs", "Only SELECT"),
        ("copy fct_price_daily to 'out.csv'", "Only SELECT"),
        ("pragma database_list", "Only SELECT"),
        ("select * from read_csv('/etc/passwd')", "read_csv"),
        ("select * from 'secrets.parquet'", "no table named"),
        ("select getenv('HOME')", "getenv"),
        ("select * from main.fct_price_daily", "bare table names"),
        ("select * from users", "no table named users"),
        ("select * from (select 1) as t, glob('*')", "glob"),
        ("selec 1", "parse"),
        ("", "empty"),
    ],
)
def test_unsafe_or_broken_sql_is_rejected_with_a_reason(sql, reason):
    with pytest.raises(QueryRejected, match=reason):
        check(sql, TABLES, 10)
