import json

import httpx
import pytest

from api.ai_provider import (
    AICompletion,
    AIProviderClient,
    AIProviderError,
    get_provider_config,
    get_comparison_provider_config,
    comparison_unit_prices,
    provider_status,
)
from api import ai_service
from api.routers import ai as ai_router


def test_default_groq_uses_supported_replacement_without_exposing_key():
    env = {"GROQ_API_KEY": "secret-value", "PACHI_AI_ALLOW_EXTERNAL": "true"}
    config = get_provider_config(env)
    status = provider_status(env)

    assert config is not None
    assert config.model == "qwen/qwen3.6-27b"
    assert status["available"] is True
    assert status["provider"] == "groq"
    assert status["personal_history_enabled"] is False
    assert "secret-value" not in json.dumps(status)


def test_provider_requires_its_own_key_and_respects_external_switch():
    missing = provider_status({"PACHI_AI_PROVIDER": "kimi", "PACHI_AI_ALLOW_EXTERNAL": "true"})
    blocked = provider_status({
        "PACHI_AI_PROVIDER": "deepseek",
        "DEEPSEEK_API_KEY": "secret",
        "PACHI_AI_ALLOW_EXTERNAL": "false",
    })

    assert missing["available"] is False
    assert "MOONSHOT_API_KEY" in missing["reason"]
    assert blocked["available"] is False
    assert blocked["configured"] is True
    assert "送信が無効" in blocked["reason"]


def test_openai_compatible_request_uses_selected_model():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "回答"}}]})

    config = get_provider_config({
        "PACHI_AI_PROVIDER": "qwen",
        "PACHI_AI_ALLOW_EXTERNAL": "true",
        "DASHSCOPE_API_KEY": "qwen-secret",
        "PACHI_AI_MODEL": "custom-qwen",
    })
    assert config is not None
    client = AIProviderClient(config, transport=httpx.MockTransport(handler))

    result = client.complete([{"role": "user", "content": "質問"}], max_tokens=123)

    assert result == "回答"
    assert seen["url"].endswith("/compatible-mode/v1/chat/completions")
    assert seen["auth"] == "Bearer qwen-secret"
    assert seen["body"]["model"] == "custom-qwen"
    assert seen["body"]["max_tokens"] == 123


def test_detailed_completion_retains_reported_usage_without_guessing():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "回答"}}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 45}})
    config = get_provider_config({"PACHI_AI_PROVIDER": "qwen", "PACHI_AI_ALLOW_EXTERNAL": "true",
                                  "DASHSCOPE_API_KEY": "secret"})
    result = AIProviderClient(config, transport=httpx.MockTransport(handler)).complete_detailed(
        [{"role": "user", "content": "質問"}], max_tokens=50)
    assert result == AICompletion("回答", 123, 45)


def test_comparison_config_needs_second_switch_and_uses_provider_specific_values():
    env = {"PACHI_AI_ALLOW_EXTERNAL": "true", "PACHI_AI_ALLOW_COMPARISON_EXTERNAL": "true",
           "DEEPSEEK_API_KEY": "secret", "PACHI_AI_DEEPSEEK_MODEL": "fixed-model",
           "PACHI_AI_DEEPSEEK_INPUT_USD_PER_MTOK": "1.25",
           "PACHI_AI_DEEPSEEK_OUTPUT_USD_PER_MTOK": "bad"}
    config = get_comparison_provider_config("deepseek", env)
    assert config.available and config.model == "fixed-model"
    assert config.allow_personal_history is False
    assert comparison_unit_prices("deepseek", env) == (1.25, None)
    assert not get_comparison_provider_config("deepseek", {**env, "PACHI_AI_ALLOW_COMPARISON_EXTERNAL": "false"}).available


def test_claude_request_separates_system_message():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "説明"}]})

    config = get_provider_config({
        "PACHI_AI_PROVIDER": "claude",
        "PACHI_AI_ALLOW_EXTERNAL": "true",
        "ANTHROPIC_API_KEY": "claude-secret",
    })
    assert config is not None
    client = AIProviderClient(config, transport=httpx.MockTransport(handler))
    result = client.complete([
        {"role": "system", "content": "規則"},
        {"role": "user", "content": "質問"},
    ], max_tokens=80)

    assert result == "説明"
    assert seen["headers"]["x-api-key"] == "claude-secret"
    assert seen["body"]["system"] == "規則"
    assert seen["body"]["messages"] == [{"role": "user", "content": "質問"}]


def test_provider_error_is_classified_without_returning_credentials():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad credentials"}})

    config = get_provider_config({"PACHI_AI_PROVIDER": "kimi", "MOONSHOT_API_KEY": "do-not-leak", "PACHI_AI_ALLOW_EXTERNAL": "true"})
    assert config is not None
    client = AIProviderClient(config, transport=httpx.MockTransport(handler))

    with pytest.raises(AIProviderError) as exc_info:
        client.complete([{"role": "user", "content": "質問"}], max_tokens=50)

    assert exc_info.value.kind == "authentication"
    assert "do-not-leak" not in str(exc_info.value)


def test_status_endpoint_reports_selected_provider_without_key(monkeypatch):
    monkeypatch.setenv("PACHI_AI_ALLOW_EXTERNAL", "true")
    monkeypatch.setenv("PACHI_AI_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "provider-secret")

    result = ai_router.api_ai_status()

    assert result["available"] is True
    assert result["provider"] == "deepseek"
    assert result["model"] == "deepseek-flash"
    assert "provider-secret" not in json.dumps(result)


def test_existing_key_does_not_enable_external_calls():
    config = get_provider_config({"GROQ_API_KEY": "existing-key"})
    assert config is not None
    assert not config.available
    def unexpected_request(request):
        pytest.fail("A disabled provider must not send a request")
    client = AIProviderClient(config, transport=httpx.MockTransport(unexpected_request))
    with pytest.raises(AIProviderError, match="有効化"):
        client.complete([{"role": "user", "content": "質問"}], max_tokens=50)
