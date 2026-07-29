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
        assert captured == {"timeout": 55.0, "max_retries": 2}
        assert not clients[0].closed

    assert clients[0].closed
