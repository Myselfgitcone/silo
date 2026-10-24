def test_run_query_reports_truncation(toolbox):
    result = toolbox.run_query("select * from fct_price_daily")
    assert result["row_count"] == 50
    assert result["truncated"] is True
    assert isinstance(result["rows"][0][2], str)  # dates come back as ISO strings


def test_describe_table_includes_samples(toolbox):
    table = toolbox.describe_table("fct_crop_condition_weekly")
    assert table["rows"] == 52
    assert len(table["sample"]["rows"]) == 5
    assert "condition_index" in [c["name"] for c in table["columns"]]


def test_query_metric_returns_the_sql_it_ran(toolbox):
    result = toolbox.query_metric("futures_close", group_by=["symbol"], grain="year")
    assert result["metric"]["unit"] == "cents per bushel"
    assert result["sql"].startswith("SELECT CAST(DATE_TRUNC('YEAR'")
    assert {row[1] for row in result["rows"]} == {"ZC=F", "ZS=F", "ZW=F"}


def test_data_freshness(toolbox):
    freshness = toolbox.data_freshness()
    assert freshness["latest_data"]["fct_price_daily"] == "2026-09-11"
    assert freshness["quarantined_last_30_days"]


def test_a_failed_refresh_keeps_serving_the_last_build(toolbox, tmp_path, monkeypatch):
    built = toolbox.lake.generated_at
    monkeypatch.setattr(toolbox.lake, "source", str(tmp_path))  # nothing published here
    toolbox.refresh_if_due()
    assert toolbox.lake.generated_at == built
    assert toolbox.run_query("select count(*) from ref_symbols")["rows"] == [[4]]
