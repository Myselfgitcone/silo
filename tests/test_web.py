import pytest
from starlette.testclient import TestClient

from silo.budget import Budget
from silo.server import build_server
from silo.web import PRESETS, RateLimiter, Web
from tests.test_agent import ScriptedModel, reply, text


@pytest.fixture
def make_client(toolbox, settings, tmp_path):
    settings.state_dir = str(tmp_path)

    def make(model=None, **overrides):
        for key, value in overrides.items():
            setattr(settings, key, value)
        budget = Budget(tmp_path / "spend.json", settings.daily_budget_usd)
        server = build_server(toolbox)
        web = Web(toolbox, settings, budget, model)
        web.register(server)
        client = TestClient(server.streamable_http_app(stateless_http=True, json_response=True))
        client.budget = budget
        return client

    return make


def test_page_and_config(make_client):
    client = make_client(ScriptedModel())
    assert "Ask the grain lakehouse" in client.get("/").text
    assert "/api/ask" in client.get("/app.js").text
    config = client.get("/api/config").json()
    assert [p["id"] for p in config["presets"]] == list(PRESETS)
    assert config["analyst_online"] is True
    assert config["lake_built_at"]


def test_presets_are_answered_once_per_lake_build(make_client):
    model = ScriptedModel(reply(text("Corn tended to rise.")))
    client = make_client(model)

    first = client.post("/api/ask", json={"preset": "signal"}).json()
    second = client.post("/api/ask", json={"preset": "signal"}).json()

    assert first["text"] == second["text"] == "Corn tended to rise."
    assert (first["cached"], second["cached"]) == (False, True)
    assert len(model.requests) == 1


def test_free_questions_are_rate_limited_per_visitor(make_client):
    client = make_client(ScriptedModel(reply(text("One."))), asks_per_hour=1)
    headers = {"x-forwarded-for": "203.0.113.7, 10.0.0.1"}
    assert client.post("/api/ask", json={"question": "First?"}, headers=headers).status_code == 200
    second = client.post("/api/ask", json={"question": "Second?"}, headers=headers)
    assert second.status_code == 429
    assert "1 questions this hour" in second.json()["error"]


def test_presets_do_not_count_against_the_visitor_limit(make_client):
    model = ScriptedModel(reply(text("A.")), reply(text("B.")))
    client = make_client(model, asks_per_hour=1)
    assert client.post("/api/ask", json={"preset": "signal"}).status_code == 200
    assert client.post("/api/ask", json={"question": "Mine?"}).status_code == 200


@pytest.mark.parametrize(
    ("body", "status", "message"),
    [
        ({}, 400, "Ask a question first"),
        ({"question": "x" * 301}, 400, "under 300"),
        ({"preset": "nope"}, 400, "Unknown preset"),
    ],
)
def test_bad_requests(make_client, body, status, message):
    response = make_client(ScriptedModel()).post("/api/ask", json=body)
    assert response.status_code == status
    assert message in response.json()["error"]


def test_without_a_key_the_page_says_offline(make_client):
    client = make_client(None)
    assert client.get("/api/config").json()["analyst_online"] is False
    assert client.post("/api/ask", json={"question": "Hi?"}).status_code == 503


def test_spent_budget_returns_503_without_calling_the_model(make_client, settings):
    model = ScriptedModel()
    client = make_client(model)
    client.budget.record(settings.daily_budget_usd)
    response = client.post("/api/ask", json={"question": "Anything?"})
    assert response.status_code == 503
    assert "budget" in response.json()["error"]
    assert model.requests == []


def test_rate_limiter_window():
    limiter = RateLimiter(limit=2, window_s=60)
    assert limiter.allow("a", now=0) and limiter.allow("a", now=10)
    assert not limiter.allow("a", now=20)
    assert limiter.retry_after("a", now=20) == 40
    assert limiter.allow("a", now=61)
    assert limiter.allow("b", now=20)


def test_unexpected_errors_return_a_readable_message(make_client):
    class Broken(ScriptedModel):
        async def create(self, **request):
            raise PermissionError("state folder is read-only")

    response = make_client(Broken()).post("/api/ask", json={"question": "Anything?"})
    assert response.status_code == 500
    assert response.json()["error"] == "Something went wrong on the server. It's been logged."


def test_serve_refuses_to_start_without_a_writable_state_folder(tmp_path):
    import typer

    from silo.cli import _check_writable

    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    with pytest.raises(typer.Exit):
        _check_writable(blocked / "state")
    _check_writable(tmp_path / "ok")  # creates it
    assert (tmp_path / "ok").is_dir()
