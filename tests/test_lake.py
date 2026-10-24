import duckdb
import pytest

from silo.lake import Lake, QueryTimeout


def test_tables_and_dbt_docs_load(lake):
    price = lake.tables["fct_price_daily"]
    assert price.rows == 420
    assert price.description.startswith("Daily OHLCV")
    assert any(c.description for c in price.columns)


def test_reloading_an_unchanged_lake_is_a_no_op(lake):
    assert lake.load() is False


def test_engine_cannot_reach_files_even_if_the_guard_is_bypassed(lake):
    with pytest.raises(duckdb.Error, match="disabled"):
        lake.execute("select * from read_csv('/etc/passwd')", timeout_s=5)
    with pytest.raises(duckdb.Error):
        lake.execute("set enable_external_access = true", timeout_s=5)


def test_long_queries_are_stopped(lake):
    with pytest.raises(QueryTimeout, match=r"0\.2s"):
        lake.execute("select count(*) from range(10000000000) a, range(1000) b", timeout_s=0.2)


def test_a_missing_lake_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        Lake(str(tmp_path)).load()
