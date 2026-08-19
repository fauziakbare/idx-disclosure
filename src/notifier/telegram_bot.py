"""Telegram Command Center: bot for manual triggers and notification delivery."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Callable, Coroutine

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"

_MAX_RETRIES = 8
_RETRY_BASE_DELAY = 0.8  # seconds
_RETRY_JITTER = 0.75  # seconds, +/- random jitter on backoff

# Transient network errors worth retrying
_RETRYABLE_EXC = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.PoolTimeout,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.TooManyRedirects,
)


def _is_transient_status(resp: httpx.Response) -> bool:
    """True for status codes that warrant a retry (5xx, 429)."""
    return resp.status_code == 429 or resp.status_code >= 500


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int = _MAX_RETRIES,
    **kwargs: Any,
) -> httpx.Response:
    """Execute HTTP request with exponential backoff retry on transient failures.

    Retries connection errors, timeouts, pool saturation, and transient
    HTTP status codes (429 rate-limit, 5xx). Does NOT retry 409 conflict
    (getUpdates) — that carries a Telegram message body to surface.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = await client.request(method, url, **kwargs)
            if _is_transient_status(resp):
                last_exc = httpx.HTTPStatusError(
                    f"Transient status {resp.status_code} for {url}",
                    request=resp.request,
                    response=resp,
                )
                if attempt < max_retries - 1:
                    delay = _RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, _RETRY_JITTER)
                    logger.warning(
                        "Telegram API transient status %d (attempt %d/%d). Retrying in %.1fs...",
                        resp.status_code, attempt + 1, max_retries, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
            resp.raise_for_status()
            return resp
        except _RETRYABLE_EXC as exc:
            last_exc = exc
            exc_name = type(exc).__name__
            if attempt < max_retries - 1:
                delay = _RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, _RETRY_JITTER)
                logger.warning(
                    "Telegram API request failed (attempt %d/%d): [%s] %s. Retrying in %.1fs...",
                    attempt + 1, max_retries, exc_name, exc, delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "Telegram API request failed after %d attempts: [%s] %s",
                    max_retries, exc_name, exc,
                )
    raise last_exc  # type: ignore[misc]


class TelegramNotifier:
    """Send notifications via Telegram Bot API."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._base_url = TELEGRAM_API_BASE.format(token=bot_token)
        self._chat_id = chat_id

    async def send_message(
        self,
        text: str,
        parse_mode: str = "Markdown",
        disable_preview: bool = True,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a message to configured chat.

        Args:
            text: Message content (supports Markdown).
            parse_mode: Parse mode (Markdown, HTML, or None).
            disable_preview: Disable link previews.
            reply_markup: Optional inline keyboard markup dict.

        Returns:
            Telegram API response dict.
        """
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=12.0, read=60.0, write=60.0, pool=15.0)) as client:
            resp = await _request_with_retry(
                client, "POST", f"{self._base_url}/sendMessage", json=payload,
            )
            data = resp.json()

        logger.info("Telegram message sent to %s (%d chars)", self._chat_id, len(text))
        return data

class TelegramCommandBot:
    """Simple command bot for manual pipeline triggers and inline button callbacks."""

    def __init__(
        self,
        bot_token: str,
        allowed_chat_ids: list[str],
    ) -> None:
        self._base_url = TELEGRAM_API_BASE.format(token=bot_token)
        self._allowed_chats = set(allowed_chat_ids)
        self._handlers: dict[str, Callable[..., Coroutine]] = {}
        self._callback_handlers: dict[str, Callable[..., Coroutine]] = {}
        self._running = False
        self._poll_interval = 2.0
        self._poll_timeout = 30.0
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Lazily create a persistent AsyncClient with sane limits."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=8.0,
                    read=self._poll_timeout + 15.0,
                    write=60.0,
                    pool=12.0,
                ),
                limits=httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                ),
                follow_redirects=True,
            )
        return self._client

    def _handle_conflict(self, body: str) -> bool:
        """Handle getUpdates 409 conflict. Returns True if offset should reset.

        Telegram conflict bodies:
          - "Conflict: can't use getUpdates method while webhook is active"
            -> a webhook is registered; polling impossible. False.
          - "Conflict: terminated by other getUpdates request"
            -> another poller runs; recover by resetting offset.
        """
        if "terminated by other getUpdates" in body:
            logger.warning("getUpdates conflict: terminated by other poller. Resetting offset.")
            return True
        if "webhook is active" in body:
            logger.error("getUpdates conflict: webhook active. Use deleteWebhook.")
            return False
        return True

    def register_command(
        self,
        command: str,
        handler: Callable[..., Coroutine],
    ) -> None:
        """Register a command handler.

        Args:
            command: Command string without slash (e.g., "run", "status").
            handler: Async callable(chat_id, args) -> str response.
        """
        self._handlers[command.lower()] = handler

    def register_callback_handler(
        self,
        prefix: str,
        handler: Callable[..., Coroutine],
    ) -> None:
        """Register an inline button callback handler.

        Args:
            prefix: Callback data prefix (e.g., "draft_x", "detail").
            handler: Async callable(chat_id, payload) -> str response.
                     payload is the part after 'prefix:'.
        """
        self._callback_handlers[prefix] = handler

    async def _process_update(self, update: dict[str, Any]) -> None:
        """Process single Telegram update (commands and callback queries)."""
        # Handle inline button presses
        if "callback_query" in update:
            await self._process_callback_query(update["callback_query"])
            return

        message = update.get("message", {})
        text = message.get("text", "")
        chat_id = str(message.get("chat", {}).get("id", ""))

        if not text.startswith("/"):
            return

        if chat_id not in self._allowed_chats:
            logger.warning("Unauthorized command from chat %s", chat_id)
            return

        parts = text.strip().split(maxsplit=1)
        command = parts[0][1:].lower()  # Remove leading /
        args = parts[1] if len(parts) > 1 else ""

        handler = self._handlers.get(command)
        if handler is None:
            reply = f"Unknown command: /{command}. Available: {', '.join('/' + c for c in self._handlers)}"
        else:
            try:
                reply = await handler(chat_id, args)
            except Exception as e:
                logger.error("Handler error for /%s: %s", command, e)
                reply = f"Error executing /{command}: {e}"

        await self._send_message(chat_id, reply)

    async def _process_callback_query(self, callback: dict[str, Any]) -> None:
        """Process inline button callback query."""
        import time

        chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        callback_data = callback.get("data", "")
        callback_id = callback.get("id", "")

        if chat_id not in self._allowed_chats:
            logger.warning("Unauthorized callback from chat %s", chat_id)
            await self._answer_callback_safe(callback_id, "Unauthorized")
            return

        # Parse prefix:payload format (e.g., "draft_x:BBCA")
        if ":" not in callback_data:
            await self._answer_callback_safe(callback_id, "Invalid action")
            return

        prefix, payload = callback_data.split(":", 1)
        handler = self._callback_handlers.get(prefix)

        if handler is None:
            logger.warning("No handler for callback prefix: %s", prefix)
            await self._answer_callback_safe(callback_id, "Unknown action")
            return

        # Telegram callback_query_id expires after ~30 seconds.
        # If handler exceeds this budget, skip answerCallbackQuery entirely.
        _CALLBACK_TTL = 25.0
        start = time.monotonic()
        try:
            reply = await asyncio.wait_for(handler(chat_id, payload), timeout=_CALLBACK_TTL)
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            logger.warning(
                "Callback handler for %s timed out after %.1fs (TTL %ds). Skipping answerCallbackQuery.",
                prefix, elapsed, int(_CALLBACK_TTL),
            )
            await self._send_message(chat_id, "⏳ Permintaan diproses terlalu lama. Silakan coba lagi.", parse_mode="")
            return
        except Exception as e:
            elapsed = time.monotonic() - start
            logger.error("Callback handler error for %s (%.1fs): %s", prefix, elapsed, e)
            await self._answer_callback_safe(callback_id, "Error processing request")
            await self._send_message(chat_id, f"Error: {e}", parse_mode="")
            return

        elapsed = time.monotonic() - start
        if elapsed > _CALLBACK_TTL:
            logger.warning(
                "Callback handler for %s completed in %.1fs but exceeds TTL %ds. Skipping answerCallbackQuery.",
                prefix, elapsed, int(_CALLBACK_TTL),
            )
            await self._send_message(chat_id, reply, parse_mode="")
        else:
            await self._answer_callback_safe(callback_id)
            await self._send_message(chat_id, reply, parse_mode="")

    async def _answer_callback(self, callback_id: str, text: str = "") -> None:
        """Answer callback query to remove loading state on button."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=8.0, read=30.0, write=30.0, pool=12.0)) as client:
            await _request_with_retry(
                client, "POST", f"{self._base_url}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
            )

    async def _answer_callback_safe(self, callback_id: str, text: str = "") -> None:
        """Answer callback query, silently ignoring errors (e.g. expired ID → 400)."""
        try:
            await self._answer_callback(callback_id, text)
        except Exception as e:
            logger.warning("answerCallbackQuery failed (safe): %s", e)

    @staticmethod
    def _split_message(text: str, max_len: int = 4000) -> list[str]:
        """Split long text into chunks respecting Telegram 4096 char limit."""
        if len(text) <= max_len:
            return [text]
        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            # Try split at newline near limit
            split_at = text.rfind("\n", 0, max_len)
            if split_at == -1:
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        return chunks

    async def _send_message(self, chat_id: str, text: str, parse_mode: str = "Markdown") -> None:
        """Send text message to chat. Auto-chunks if >4000 chars."""
        chunks = self._split_message(text)
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=8.0, read=60.0, write=60.0, pool=12.0)) as client:
            for chunk in chunks:
                payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                try:
                    await _request_with_retry(
                        client, "POST", f"{self._base_url}/sendMessage",
                        json=payload,
                    )
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 400:
                        body = e.response.text
                        logger.error(
                            "Telegram sendMessage 400 for chat %s (parse_mode=%s, len=%d): %s",
                            chat_id, parse_mode, len(chunk), body,
                        )
                        # Retry without parse_mode if formatting caused the issue
                        if parse_mode:
                            logger.info("Retrying sendMessage without parse_mode")
                            await _request_with_retry(
                                client, "POST", f"{self._base_url}/sendMessage",
                                json={"chat_id": chat_id, "text": chunk},
                            )
                        else:
                            raise
                    else:
                        raise

    async def poll_updates(self, interval: float = 2.0) -> None:
        """Long-poll Telegram updates.

        Args:
            interval: Polling interval in seconds.
        """
        self._running = True
        offset = 0

        logger.info("Telegram command bot started polling")

        client = self._get_client()
        consecutive_failures = 0

        while self._running:
            try:
                resp = await _request_with_retry(
                    client, "POST", f"{self._base_url}/getUpdates",
                    json={"offset": offset, "timeout": self._poll_timeout},
                )
                data = resp.json()

                # getUpdates returns 200 with empty result on long-poll timeout.
                results = data.get("result", [])
                for update in results:
                    offset = max(offset, update["update_id"] + 1)
                    await self._process_update(update)

                consecutive_failures = 0
            except httpx.HTTPStatusError as e:
                # 409 conflict handling
                if e.response.status_code == 409:
                    body = e.response.text or ""
                    if not self._handle_conflict(body):
                        await asyncio.sleep(interval)
                        continue
                    # Reset offset to re-sync, drop the conflicting poller.
                    offset = 0
                    logger.info("Resetting getUpdates offset to 0 after conflict")
                else:
                    logger.error("Poll HTTP error %d: %s", e.response.status_code, e)
                consecutive_failures += 1
            except Exception as e:
                logger.error("Poll error: %s", e)
                consecutive_failures += 1

            # Backoff on repeated failures to avoid hammering a dead link.
            if consecutive_failures >= 3:
                wait = min(30.0, interval * (2 ** min(consecutive_failures - 3, 4)))
                logger.warning(
                    "Polling failing repeatedly (%d consecutive). Backing off %.1fs",
                    consecutive_failures, wait,
                )
                await asyncio.sleep(wait)
            else:
                await asyncio.sleep(interval)

    async def close(self) -> None:
        """Close underlying HTTP client and stop polling."""
        self._running = False
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def stop(self) -> None:
        """Stop polling loop."""
        self._running = False
        logger.info("Telegram command bot stopping")
