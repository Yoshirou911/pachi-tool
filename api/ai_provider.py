"""v3.40: provider-neutral AI transport.

AI providers are selected only by server-side environment variables. API keys are
never returned by the status API, written to the database, or stored in a browser.
This module only transports prompts; it does not calculate or change predictions.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
import math
from typing import Mapping

import httpx


@dataclass(frozen=True)
class ProviderDefinition:
    name: str
    label: str
    api_style: str
    base_url: str
    default_model: str
    key_env: str


@dataclass(frozen=True)
class ProviderConfig:
    definition: ProviderDefinition
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float
    allow_external: bool
    allow_personal_history: bool

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.allow_external)


@dataclass(frozen=True)
class AICompletion:
    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None


PROVIDERS: dict[str, ProviderDefinition] = {
    "groq": ProviderDefinition(
        name="groq",
        label="Groq",
        api_style="openai",
        base_url="https://api.groq.com/openai/v1",
        default_model="qwen/qwen3.6-27b",
        key_env="GROQ_API_KEY",
    ),
    "deepseek": ProviderDefinition(
        name="deepseek",
        label="DeepSeek",
        api_style="openai",
        base_url="https://api.deepseek.com",
        default_model="deepseek-flash",
        key_env="DEEPSEEK_API_KEY",
    ),
    "qwen": ProviderDefinition(
        name="qwen",
        label="Qwen",
        api_style="openai",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        default_model="qwen3.5-plus",
        key_env="DASHSCOPE_API_KEY",
    ),
    "kimi": ProviderDefinition(
        name="kimi",
        label="Kimi",
        api_style="openai",
        base_url="https://api.moonshot.ai/v1",
        default_model="kimi-k2.6",
        key_env="MOONSHOT_API_KEY",
    ),
    "claude": ProviderDefinition(
        name="claude",
        label="Claude",
        api_style="anthropic",
        base_url="https://api.anthropic.com",
        default_model="claude-sonnet-5",
        key_env="ANTHROPIC_API_KEY",
    ),
}

_ALIASES = {"anthropic": "claude", "moonshot": "kimi", "dashscope": "qwen"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


class AIProviderError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _enabled(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in _FALSE_VALUES


def get_provider_config(environ: Mapping[str, str] | None = None) -> ProviderConfig | None:
    env = os.environ if environ is None else environ
    requested = (env.get("PACHI_AI_PROVIDER") or "groq").strip().lower()
    requested = _ALIASES.get(requested, requested)
    if requested in {"", "none", "off", "disabled"}:
        return None
    definition = PROVIDERS.get(requested)
    if definition is None:
        return None
    try:
        timeout = float(env.get("PACHI_AI_TIMEOUT_SECONDS", "45"))
    except (TypeError, ValueError):
        timeout = 45.0
    timeout = min(120.0, max(5.0, timeout))
    return ProviderConfig(
        definition=definition,
        model=(env.get("PACHI_AI_MODEL") or definition.default_model).strip(),
        base_url=(env.get("PACHI_AI_BASE_URL") or definition.base_url).strip().rstrip("/"),
        api_key=(env.get(definition.key_env) or "").strip(),
        timeout_seconds=timeout,
        allow_external=_enabled(env.get("PACHI_AI_ALLOW_EXTERNAL"), default=False),
        # v3.48: personal play history is never eligible for external transmission.
        allow_personal_history=False,
    )


def get_comparison_provider_config(name: str, environ: Mapping[str, str] | None = None) -> ProviderConfig | None:
    """Return one provider's benchmark config without changing the active provider.

    Comparison traffic needs both external switches. Models/base URLs are provider-
    specific so one provider's override cannot silently affect the others.
    """
    env = os.environ if environ is None else environ
    canonical = _ALIASES.get((name or "").strip().lower(), (name or "").strip().lower())
    definition = PROVIDERS.get(canonical)
    if definition is None:
        return None
    prefix = f"PACHI_AI_{canonical.upper()}"
    try:
        timeout = float(env.get("PACHI_AI_TIMEOUT_SECONDS", "45"))
    except (TypeError, ValueError):
        timeout = 45.0
    return ProviderConfig(
        definition=definition,
        model=(env.get(f"{prefix}_MODEL") or definition.default_model).strip(),
        base_url=(env.get(f"{prefix}_BASE_URL") or definition.base_url).strip().rstrip("/"),
        api_key=(env.get(definition.key_env) or "").strip(),
        timeout_seconds=min(120.0, max(5.0, timeout)),
        allow_external=(
            _enabled(env.get("PACHI_AI_ALLOW_EXTERNAL"), default=False)
            and _enabled(env.get("PACHI_AI_ALLOW_COMPARISON_EXTERNAL"), default=False)
        ),
        allow_personal_history=False,
    )


def comparison_unit_prices(name: str, environ: Mapping[str, str] | None = None) -> tuple[float | None, float | None]:
    """Read administrator-supplied USD / million-token prices; never guess prices."""
    env = os.environ if environ is None else environ
    canonical = _ALIASES.get((name or "").strip().lower(), (name or "").strip().lower())
    prefix = f"PACHI_AI_{canonical.upper()}"
    values = []
    for suffix in ("INPUT_USD_PER_MTOK", "OUTPUT_USD_PER_MTOK"):
        try:
            value = float(env.get(f"{prefix}_{suffix}", ""))
            values.append(value if math.isfinite(value) and value >= 0 else None)
        except (TypeError, ValueError):
            values.append(None)
    return values[0], values[1]


def provider_status(environ: Mapping[str, str] | None = None) -> dict:
    env = os.environ if environ is None else environ
    requested = (env.get("PACHI_AI_PROVIDER") or "groq").strip().lower()
    requested = _ALIASES.get(requested, requested)
    config = get_provider_config(env)
    if requested in {"", "none", "off", "disabled"}:
        reason = "AI接続は無効です"
    elif requested not in PROVIDERS:
        reason = f"未対応のAI接続先です: {requested}"
    elif config and not config.allow_external:
        reason = "外部AIへの送信が無効です"
    elif config and not config.api_key:
        reason = f"{config.definition.key_env} が設定されていません"
    else:
        reason = ""
    return {
        "available": bool(config and config.available),
        "provider": config.definition.name if config else requested or "disabled",
        "provider_label": config.definition.label if config else "統計エンジン",
        "model": config.model if config else "",
        "configured": bool(config and config.api_key),
        "external_data_enabled": bool(config and config.allow_external),
        "personal_history_enabled": bool(config and config.allow_personal_history),
        "reason": reason,
        "fallback": "統計エンジン",
        "supported_providers": [
            {
                "provider": item.name,
                "label": item.label,
                "default_model": item.default_model,
                "key_env": item.key_env,
            }
            for item in PROVIDERS.values()
        ],
    }


class AIProviderClient:
    def __init__(self, config: ProviderConfig, *, transport: httpx.BaseTransport | None = None):
        self.config = config
        self._transport = transport

    @property
    def display_name(self) -> str:
        return self.config.definition.label

    @property
    def model(self) -> str:
        return self.config.model

    @property
    def allow_personal_history(self) -> bool:
        return self.config.allow_personal_history

    def complete(self, messages: list[dict], *, max_tokens: int) -> str:
        return self.complete_detailed(messages, max_tokens=max_tokens).content

    def complete_detailed(self, messages: list[dict], *, max_tokens: int) -> AICompletion:
        if not self.config.available:
            raise AIProviderError("disabled", "外部AIへの送信は有効化されていません")
        try:
            if self.config.definition.api_style == "anthropic":
                return self._complete_anthropic(messages, max_tokens=max_tokens)
            return self._complete_openai(messages, max_tokens=max_tokens)
        except AIProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise AIProviderError("timeout", "外部AIの応答が時間内に返りませんでした") from exc
        except httpx.RequestError as exc:
            raise AIProviderError("network", "外部AIへ接続できませんでした") from exc

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.config.timeout_seconds, transport=self._transport)

    def _complete_openai(self, messages: list[dict], *, max_tokens: int) -> AICompletion:
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        with self._client() as client:
            response = client.post(
                f"{self.config.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
        self._raise_for_status(response)
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AIProviderError("invalid_response", "AIの応答形式が不正です") from exc
        if not isinstance(content, str) or not content.strip():
            raise AIProviderError("invalid_response", "AIの回答が空です")
        usage = body.get("usage") if isinstance(body, dict) else None
        return AICompletion(content.strip(), *_usage_tokens(usage, "prompt_tokens", "completion_tokens"))

    def _complete_anthropic(self, messages: list[dict], *, max_tokens: int) -> AICompletion:
        system_parts = [str(item.get("content") or "") for item in messages if item.get("role") == "system"]
        conversation = [
            {"role": item.get("role"), "content": item.get("content")}
            for item in messages
            if item.get("role") in {"user", "assistant"}
        ]
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "system": "\n\n".join(part for part in system_parts if part),
            "messages": conversation,
        }
        with self._client() as client:
            response = client.post(
                f"{self.config.base_url}/v1/messages",
                headers=headers,
                json=payload,
            )
        self._raise_for_status(response)
        try:
            body = response.json()
            blocks = body["content"]
            content = "\n".join(
                str(block.get("text") or "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise AIProviderError("invalid_response", "AIの応答形式が不正です") from exc
        if not content:
            raise AIProviderError("invalid_response", "AIの回答が空です")
        usage = body.get("usage") if isinstance(body, dict) else None
        return AICompletion(content, *_usage_tokens(usage, "input_tokens", "output_tokens"))

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        if response.status_code in {401, 403}:
            kind = "authentication"
        elif response.status_code == 429:
            kind = "rate_limit"
        elif response.status_code >= 500:
            kind = "provider_unavailable"
        else:
            kind = "request"
        message = f"AI接続先がHTTP {response.status_code}を返しました"
        try:
            body = response.json()
            detail = body.get("error") if isinstance(body, dict) else None
            if isinstance(detail, dict):
                detail = detail.get("message")
            if isinstance(detail, str) and detail.strip():
                message = detail.strip()[:160]
        except ValueError:
            pass
        raise AIProviderError(kind, message)


def _usage_tokens(usage, input_key: str, output_key: str) -> tuple[int | None, int | None]:
    if not isinstance(usage, dict):
        return None, None
    def clean(key):
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    return clean(input_key), clean(output_key)


def get_ai_client(environ: Mapping[str, str] | None = None) -> AIProviderClient | None:
    config = get_provider_config(environ)
    if not config or not config.available:
        return None
    return AIProviderClient(config)
