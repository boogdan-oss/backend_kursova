"""Клієнти LLM для AI-асистента: Gemini (Google) або Claude (Anthropic)."""

from functools import lru_cache
from typing import Literal

from anthropic import AsyncAnthropic
from google import genai
from google.genai import types

from app.core.config import get_settings

Provider = Literal["gemini", "claude"]

RETRY_STATUSES = [500, 502, 504]


class AssistantDisabled(Exception):
    """Немає ключа для жодного провайдера."""


@lru_cache
def _claude(api_key: str) -> AsyncAnthropic:
    return AsyncAnthropic(api_key=api_key, max_retries=2)


@lru_cache
def _gemini(api_key: str) -> genai.Client:
    retry = types.HttpRetryOptions(attempts=3, http_status_codes=RETRY_STATUSES)
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(retry_options=retry))


def assistant_provider() -> Provider:
    """Хто відповідає: ASSISTANT_PROVIDER або, для auto, Gemini за наявності ключа."""
    settings = get_settings()
    choice = settings.assistant_provider
    if choice == "auto":
        if settings.gemini_api_key:
            return "gemini"
        if settings.anthropic_api_key:
            return "claude"
        raise AssistantDisabled(
            "AI-асистент не налаштований: задайте GEMINI_API_KEY або ANTHROPIC_API_KEY"
        )
    key = settings.gemini_api_key if choice == "gemini" else settings.anthropic_api_key
    if not key:
        name = "GEMINI_API_KEY" if choice == "gemini" else "ANTHROPIC_API_KEY"
        raise AssistantDisabled(f"AI-асистент не налаштований: задайте {name}")
    return choice


def get_claude() -> AsyncAnthropic:
    api_key = get_settings().anthropic_api_key
    if not api_key:
        raise AssistantDisabled("AI-асистент не налаштований: задайте ANTHROPIC_API_KEY")
    return _claude(api_key)


def get_gemini() -> genai.Client:
    api_key = get_settings().gemini_api_key
    if not api_key:
        raise AssistantDisabled("AI-асистент не налаштований: задайте GEMINI_API_KEY")
    return _gemini(api_key)
