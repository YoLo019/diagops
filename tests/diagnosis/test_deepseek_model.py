import asyncio
from dataclasses import replace

import pytest

import backend.diagnosis.deepseek_model as deepseek_model


def test_deepseek_capability_profile_matches_official_stable_v4_contract():
    profile = deepseek_model.DEEPSEEK_CAPABILITIES

    assert deepseek_model.DEEPSEEK_BASE_URL == "https://api.deepseek.com"
    assert profile.official_models == ("deepseek-v4-flash", "deepseek-v4-pro")
    assert profile.chat_completions is True
    assert profile.tool_calls is True
    assert profile.json_object is True
    assert profile.local_schema_validation is True
    assert profile.strict_function_schemas is False
    assert deepseek_model.implementation_status() == "implemented"


def test_create_deepseek_model_requires_model_and_key(monkeypatch):
    def forbidden_client(**_kwargs):
        raise AssertionError("client must not be constructed")

    monkeypatch.setattr(deepseek_model, "AsyncOpenAI", forbidden_client)

    assert deepseek_model.create_deepseek_model(None, "secret") is None
    assert deepseek_model.create_deepseek_model("deepseek-v4-pro", "") is None


def test_create_deepseek_model_defers_client_and_hides_key(monkeypatch):
    def forbidden_client(**_kwargs):
        raise AssertionError("client must be created inside an active request loop")

    monkeypatch.setattr(deepseek_model, "AsyncOpenAI", forbidden_client)

    model = deepseek_model.create_deepseek_model(
        " deepseek-v4-pro ", " local-secret "
    )

    assert isinstance(model, deepseek_model.DeepSeekChatCompletionsModel)
    assert model.model == "deepseek-v4-pro"
    assert "local-secret" not in repr(model)


@pytest.mark.anyio
@pytest.mark.parametrize("raises", [False, True])
async def test_deepseek_model_closes_request_client(monkeypatch, raises):
    clients = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.loop = asyncio.get_running_loop()
            self.closed = False
            clients.append(self)

        async def close(self):
            assert asyncio.get_running_loop() is self.loop
            self.closed = True

    class FakeDelegate:
        def __init__(self, *, model, openai_client, strict_feature_validation):
            assert model == "deepseek-v4-pro"
            assert openai_client is clients[-1]
            assert strict_feature_validation is True

        async def get_response(self, *_args, **_kwargs):
            if raises:
                raise RuntimeError("provider failed")
            return "response"

    monkeypatch.setattr(deepseek_model, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(deepseek_model, "OpenAIChatCompletionsModel", FakeDelegate)
    model = deepseek_model.create_deepseek_model("deepseek-v4-pro", "local-secret")

    async def invoke():
        return await model.get_response(
            None,
            "input",
            object(),
            [],
            None,
            [],
            object(),
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )

    if raises:
        with pytest.raises(RuntimeError, match="provider failed"):
            await invoke()
    else:
        assert await invoke() == "response"

    assert len(clients) == 1
    assert clients[0].kwargs == {
        "api_key": "local-secret",
        "base_url": deepseek_model.DEEPSEEK_BASE_URL,
    }
    assert clients[0].closed is True


def test_deepseek_model_supports_sequential_event_loops(monkeypatch):
    clients = []

    class FakeClient:
        def __init__(self, **_kwargs):
            self.loop = asyncio.get_running_loop()
            self.closed = False
            clients.append(self)

        async def close(self):
            assert asyncio.get_running_loop() is self.loop
            self.closed = True

    class FakeDelegate:
        def __init__(self, **_kwargs):
            pass

        async def get_response(self, *_args, **_kwargs):
            return asyncio.get_running_loop()

    monkeypatch.setattr(deepseek_model, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(deepseek_model, "OpenAIChatCompletionsModel", FakeDelegate)
    model = deepseek_model.create_deepseek_model("deepseek-v4-pro", "local-secret")

    async def invoke():
        return await model.get_response(
            None,
            "input",
            object(),
            [],
            None,
            [],
            object(),
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )

    first_loop = asyncio.run(invoke())
    second_loop = asyncio.run(invoke())

    assert first_loop is not second_loop
    assert len(clients) == 2
    assert all(client.closed for client in clients)


def test_v11_deepseek_clone_explicitly_disables_provider_retry(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(deepseek_model, "AsyncOpenAI", FakeClient)
    model = deepseek_model.create_deepseek_model("deepseek-v4-pro", "local-secret")
    v11_model = model.clone_for_model("deepseek-v4-pro", max_retries=0)

    v11_model._create_client()

    assert captured["max_retries"] == 0


def test_unsupported_capability_profile_constructs_no_client(monkeypatch):
    unsupported = replace(deepseek_model.DEEPSEEK_CAPABILITIES, json_object=False)
    monkeypatch.setattr(deepseek_model, "DEEPSEEK_CAPABILITIES", unsupported)

    def forbidden_client(**_kwargs):
        raise AssertionError("unsupported Provider must not construct a client")

    monkeypatch.setattr(deepseek_model, "AsyncOpenAI", forbidden_client)

    assert deepseek_model.implementation_status() == "unsupported"
    assert (
        deepseek_model.create_deepseek_model(
            "deepseek-v4-pro", "local-secret"
        )
        is None
    )
