"""LLM Client Factory: unified interface via litellm for Role-1 and Role-2."""

from __future__ import annotations

from typing import Any

import litellm

from src.core.logger import logger


class LLMClient:
    """Thin wrapper around litellm for structured LLM calls."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._model = model

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Send chat completion request.

        Args:
            messages: OpenAI-format message list.
            temperature: Sampling temperature.
            max_tokens: Max output tokens.
            response_format: Optional JSON schema for structured output.

        Returns:
            Assistant response content string.
        """
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "api_base": self._base_url,
            "api_key": self._api_key,
        }
        if response_format:
            kwargs["response_format"] = response_format

        resp = await litellm.acompletion(**kwargs)
        content = resp.choices[0].message.content or ""
        logger.debug("LLM response (%s): %d chars", self._model, len(content))
        return content

    async def vision_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send vision/multimodal chat completion request.

        Args:
            messages: Messages with image_url content blocks.
            temperature: Sampling temperature.
            max_tokens: Max output tokens.

        Returns:
            Assistant response content string.
        """
        resp = await litellm.acompletion(
            model=self._model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            api_base=self._base_url,
            api_key=self._api_key,
        )
        content = resp.choices[0].message.content or ""
        logger.debug("Vision LLM response (%s): %d chars", self._model, len(content))
        return content


def create_triage_client(settings: Any) -> LLMClient:
    """Create LLM client for Role-1 Triage from app settings."""
    return LLMClient(
        base_url=settings.TRIAGE_BASE_URL,
        api_key=settings.TRIAGE_API_KEY,
        model=settings.TRIAGE_MODEL_NAME,
    )


def create_vision_client(settings: Any) -> LLMClient:
    """Create LLM client for PDF Vision fallback from app settings."""
    return LLMClient(
        base_url=settings.TRIAGE_BASE_URL,
        api_key=settings.TRIAGE_API_KEY,
        model=settings.TRIAGE_VISION_MODEL_NAME,
    )


def create_reasoner_client(settings: Any) -> LLMClient:
    """Create LLM client for Role-2 Reasoner from app settings."""
    return LLMClient(
        base_url=settings.REASONER_BASE_URL,
        api_key=settings.REASONER_API_KEY,
        model=settings.REASONER_MODEL_NAME,
    )
