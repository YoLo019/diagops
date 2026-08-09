import pytest

import backend.diagnosis.openai_model as openai_model


@pytest.mark.parametrize(
    ("overall", "expected"),
    [(60.0, 55.0), (10.0, 5.0), (5.0, 1.0), (1.0, 1.0)],
)
def test_transport_timeout_finishes_inside_overall_budget(overall, expected):
    assert openai_model.transport_timeout_seconds(overall) == expected


@pytest.mark.anyio
async def test_openai_responses_model_owns_bounded_retry_client(
    monkeypatch,
):
    captured = {}
    clients = []

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.closed = False
            clients.append(self)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(openai_model, "AsyncOpenAI", FakeClient)

    async with openai_model.openai_responses_model("gpt-test", 60) as model:
        assert model.model == "gpt-test"
        assert captured == {
            "base_url": "https://api.openai.com/v1",
            "timeout": 55.0,
            "max_retries": 2,
        }
        assert not clients[0].closed

    assert clients[0].closed


def test_official_openai_ignores_ambient_base_url(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def close(self):
            pass

    monkeypatch.setattr(openai_model, "AsyncOpenAI", FakeClient)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://rogue-endpoint.example/v1")

    async def create():
        async with openai_model.openai_responses_model("gpt-test", 60):
            pass

    import asyncio

    asyncio.run(create())

    # 官方 endpoint 钉死，绝不继承环境变量路由（spec 7.8）。
    assert captured["base_url"] == "https://api.openai.com/v1"
