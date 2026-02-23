"""
MegaLLM API client with streaming, retries, and error handling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Dict, List, Optional

import aiohttp

from config.constants import API_MAX_RETRIES, API_RETRY_BASE_DELAY, API_TIMEOUT

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """Structured response from the LLM API."""

    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    finish_reason: str = "stop"
    latency_ms: float = 0.0


class LLMClientError(Exception):
    """Base exception for LLM client errors."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class LLMClient:
    """Async client for the MegaLLM API with retries and streaming."""

    def __init__(self, api_key: str, base_url: str, default_model: str = "gpt-4"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self._session: Optional[aiohttp.ClientSession] = None

    # ── Session Management ───────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Core Chat Completion ─────────────────────────────────────────

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """Send a chat completion request with retry logic."""
        model = model or self.default_model
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        last_error: Optional[Exception] = None

        for attempt in range(API_MAX_RETRIES):
            try:
                start = time.monotonic()
                session = await self._get_session()

                async with session.post(
                    f"{self.base_url}/chat/completions", json=payload
                ) as resp:
                    latency = (time.monotonic() - start) * 1000

                    if resp.status == 200:
                        data = await resp.json()
                        choice = data["choices"][0]
                        usage = data.get("usage", {})

                        return LLMResponse(
                            content=choice["message"]["content"],
                            model=data.get("model", model),
                            prompt_tokens=usage.get("prompt_tokens", 0),
                            completion_tokens=usage.get("completion_tokens", 0),
                            total_tokens=usage.get("total_tokens", 0),
                            finish_reason=choice.get("finish_reason", "stop"),
                            latency_ms=latency,
                        )

                    body = await resp.text()
                    if resp.status == 429:
                        wait = API_RETRY_BASE_DELAY * (2**attempt)
                        logger.warning(
                            "Rate limited (attempt %d/%d), waiting %.1fs",
                            attempt + 1, API_MAX_RETRIES, wait,
                        )
                        await asyncio.sleep(wait)
                        continue

                    if resp.status >= 500:
                        wait = API_RETRY_BASE_DELAY * (2**attempt)
                        logger.warning(
                            "Server error %d (attempt %d/%d): %s",
                            resp.status, attempt + 1, API_MAX_RETRIES, body[:200],
                        )
                        await asyncio.sleep(wait)
                        continue

                    raise LLMClientError(
                        f"API error {resp.status}: {body[:200]}",
                        status_code=resp.status,
                    )

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                wait = API_RETRY_BASE_DELAY * (2**attempt)
                logger.warning(
                    "Network error (attempt %d/%d): %s",
                    attempt + 1, API_MAX_RETRIES, exc,
                )
                if attempt < API_MAX_RETRIES - 1:
                    await asyncio.sleep(wait)

        raise LLMClientError(
            f"All {API_MAX_RETRIES} retries failed: {last_error}"
        )

    # ── Streaming Chat ───────────────────────────────────────────────

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        """Stream chat completion tokens. Yields content chunks."""
        model = model or self.default_model
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT * 2)

        async with session.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            timeout=timeout,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise LLMClientError(
                    f"Stream error {resp.status}: {body[:200]}",
                    status_code=resp.status,
                )

            # Buffer for incomplete lines across chunks
            buffer = b""
            async for chunk in resp.content.iter_any():
                buffer += chunk
                # Process complete lines
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    decoded = line.decode("utf-8").strip()
                    if not decoded or not decoded.startswith("data: "):
                        continue
                    data_str = decoded[6:]
                    if data_str == "[DONE]":
                        return
                    try:
                        data = json.loads(data_str)
                        delta = data["choices"][0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    # ── Convenience Methods ──────────────────────────────────────────

    async def simple_prompt(
        self,
        prompt: str,
        system: str = "You are a helpful AI assistant.",
        model: Optional[str] = None,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """Send a simple prompt with a system message."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        return await self.chat(messages, model=model, max_tokens=max_tokens)

    async def list_models(self) -> List[str]:
        """List available models from the API (or fallback to config)."""
        try:
            session = await self._get_session()
            async with session.get(f"{self.base_url}/models") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return [m["id"] for m in data.get("data", [])]
        except Exception as exc:
            logger.debug("Could not fetch models from API: %s", exc)
        return []
