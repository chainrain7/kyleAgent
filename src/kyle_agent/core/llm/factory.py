"""LLM Provider 工厂:按配置构造 Anthropic 或 OpenAI 兼容 provider(改造 C)。"""
from __future__ import annotations

from kyle_agent.core.config import LlmConfig
from kyle_agent.core.llm.base import LLMProvider


# 按 llm.provider 配置构造默认 provider;未支持值启动期报错
def build_provider(config: LlmConfig) -> LLMProvider:
    if config.provider == "anthropic":
        from kyle_agent.core.llm.provider import AnthropicProvider

        return AnthropicProvider(config.default_model)
    if config.provider == "openai":
        from kyle_agent.core.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(config.default_model)
    raise SystemExit(
        f"Config error: llm.provider={config.provider!r} "
        f"(supported: anthropic, openai)"
    )
