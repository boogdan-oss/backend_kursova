"""Клієнт Claude (Anthropic API) для AI-асистента."""

from functools import lru_cache

from anthropic import AsyncAnthropic

from app.core.config import get_settings


class AssistantDisabled(Exception):
    """ANTHROPIC_API_KEY не заданий."""


@lru_cache
def _client(api_key: str) -> AsyncAnthropic:
    return AsyncAnthropic(api_key=api_key, max_retries=2)


def get_claude() -> AsyncAnthropic:
    api_key = get_settings().anthropic_api_key
    if not api_key:
        raise AssistantDisabled("AI-асистент не налаштований: задайте ANTHROPIC_API_KEY")
    return _client(api_key)
