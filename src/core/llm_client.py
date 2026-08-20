"""LLM Client Factory: unified interface via litellm for Role-1 and Role-2."""

from __future__ import annotations

import os
from typing import Any

import litellm

from src.core.logger import logger


def _is_gemini_3(model: str) -> bool:
    """Check if model is Gemini 3 / 3.5 family requiring temperature=1.0."""
    return "gemini-3" in model.lower()


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
        # Gemini 3/3.5 requires temperature=1.0 to avoid warning & infinite loop
        effective_temp = 1.0 if _is_gemini_3(self._model) else temperature

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": effective_temp,
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
        # Gemini 3/3.5 requires temperature=1.0 to avoid warning & infinite loop
        effective_temp = 1.0 if _is_gemini_3(self._model) else temperature

        resp = await litellm.acompletion(
            model=self._model,
            messages=messages,
            temperature=effective_temp,
            max_tokens=max_tokens,
            api_base=self._base_url,
            api_key=self._api_key,
        )
        content = resp.choices[0].message.content or ""
        logger.debug("Vision LLM response (%s): %d chars", self._model, len(content))
        return content


def _ensure_gemini_prefix(model_name: str) -> str:
    """Ensure model name has gemini/ provider prefix for litellm."""
    if not model_name.startswith("gemini/"):
        return f"gemini/{model_name}"
    return model_name


def _inject_gemini_env(api_key: str) -> None:
    """Set GEMINI_API_KEY and GOOGLE_API_KEY env vars for litellm fallback."""
    if api_key:
        os.environ["GEMINI_API_KEY"] = api_key
        os.environ["GOOGLE_API_KEY"] = api_key


def create_triage_client(settings: Any) -> LLMClient:
    """Create LLM client for Role-1 Triage from app settings."""
    model = _ensure_gemini_prefix(settings.TRIAGE_MODEL_NAME or "gemini/gemini-3.5-flash-lite")
    api_key = settings.get_triage_api_key()
    _inject_gemini_env(api_key)
    return LLMClient(
        base_url=settings.TRIAGE_BASE_URL,
        api_key=api_key,
        model=model,
    )


def create_vision_client(settings: Any) -> LLMClient:
    """Create LLM client for PDF Vision fallback from app settings."""
    model = _ensure_gemini_prefix(settings.TRIAGE_VISION_MODEL_NAME or "gemini/gemini-3.5-flash-lite")
    api_key = settings.get_triage_api_key()
    _inject_gemini_env(api_key)
    return LLMClient(
        base_url=settings.TRIAGE_BASE_URL,
        api_key=api_key,
        model=model,
    )


def create_reasoner_client(settings: Any) -> LLMClient:
    """Create LLM client for Role-2 Reasoner from app settings."""
    model = _ensure_gemini_prefix(settings.REASONER_MODEL_NAME or "gemini/gemini-3.5-flash-lite")
    api_key = settings.get_reasoner_api_key()
    _inject_gemini_env(api_key)
    return LLMClient(
        base_url=settings.REASONER_BASE_URL,
        api_key=api_key,
        model=model,
    )
