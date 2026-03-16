"""
Multi-provider LLM client with automatic failover, streaming, retries, and
provider-aware model routing.

Supports: Requesty.ai, Featherless AI, ModelsLab, Chutes.ai, Puter (free),
          and the legacy MegaLLM provider.

Every provider speaks the OpenAI chat-completions protocol, so the core
request/response logic is shared.  Provider-specific differences (base URL,
auth header, model-name format) are captured in ``ProviderConfig``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    AsyncIterator,
    Dict,
    List,
    Optional,
    Sequence,
)

import aiohttp

from config.constants import API_MAX_RETRIES, API_RETRY_BASE_DELAY, API_TIMEOUT

logger = logging.getLogger(__name__)


# ── Data Classes ──────────────────────────────────────────────────────


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
    provider: str = ""  # which provider fulfilled the request


class LLMClientError(Exception):
    """Base exception for LLM client errors."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


# ── Provider Definitions ─────────────────────────────────────────────


class ProviderName(str, Enum):
    """Canonical names for supported LLM providers."""

    REQUESTY = "requesty"
    FEATHERLESS = "featherless"
    MODELSLAB = "modelslab"
    CHUTES = "chutes"
    PUTER = "puter"
    MEGALLM = "megallm"       # legacy / custom


# Provider metadata used at config time.
PROVIDER_DEFAULTS: Dict[str, Dict[str, str]] = {
    ProviderName.REQUESTY: {
        "base_url": "https://router.requesty.ai/v1",
        "env_key": "REQUESTY_API_KEY",
        "default_model": "openai/gpt-4o-mini",
    },
    ProviderName.FEATHERLESS: {
        "base_url": "https://api.featherless.ai/v1",
        "env_key": "FEATHERLESS_API_KEY",
        "default_model": "Qwen/Qwen2.5-7B-Instruct",
    },
    ProviderName.MODELSLAB: {
        "base_url": "https://modelslab.com/api/uncensored-chat/v1",
        "env_key": "MODELSLAB_API_KEY",
        "default_model": "gpt-4o",
    },
    ProviderName.CHUTES: {
        "base_url": "https://llm.chutes.ai/v1",
        "env_key": "CHUTES_API_KEY",
        "default_model": "deepseek-ai/DeepSeek-V3-0324",
    },
    ProviderName.PUTER: {
        "base_url": "https://api.puter.com/puterai/openai/v1",
        "env_key": "PUTER_AUTH_TOKEN",
        "default_model": "gpt-4o-mini",
    },
    ProviderName.MEGALLM: {
        "base_url": "https://ai.megallm.io/v1",
        "env_key": "MEGALLM_API_KEY",
        "default_model": "gpt-4",
    },
}


@dataclass
class ProviderConfig:
    """Configuration for a single LLM provider."""

    name: str                         # e.g. "requesty"
    base_url: str                     # e.g. "https://router.requesty.ai/v1"
    api_key: str                      # Bearer token / API key
    default_model: str = "gpt-4"     # model to use when none is specified
    priority: int = 0                 # lower = tried first
    enabled: bool = True
    # Map of canonical model names → provider-specific model IDs.
    # E.g. {"gpt-4": "openai/gpt-4"} for Requesty.
    model_map: Dict[str, str] = field(default_factory=dict)
    # Extra headers sent with every request (e.g. Referer, X-Title).
    extra_headers: Dict[str, str] = field(default_factory=dict)
    # Maximum consecutive failures before the provider is temporarily
    # bypassed in the fallback chain.  Resets on success.
    max_consecutive_failures: int = 3
    # Seconds to keep a provider in "cool-down" after max failures.
    cooldown_seconds: float = 60.0

    def resolve_model(self, canonical: str) -> str:
        """Return the provider-specific model ID for *canonical*, or pass through."""
        return self.model_map.get(canonical, canonical)


# ── Single-Provider Client ───────────────────────────────────────────


class _ProviderClient:
    """Low-level async client for ONE OpenAI-compatible provider."""

    def __init__(self, config: ProviderConfig):
        self.cfg = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        # Failure tracking for circuit-breaker
        self._consecutive_failures: int = 0
        self._cooldown_until: float = 0.0

    @property
    def is_available(self) -> bool:
        if not self.cfg.enabled:
            return False
        if self._consecutive_failures >= self.cfg.max_consecutive_failures:
            if time.monotonic() < self._cooldown_until:
                return False
            # Cooldown expired → give it another chance
            self._consecutive_failures = 0
        return True

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.cfg.max_consecutive_failures:
            self._cooldown_until = time.monotonic() + self.cfg.cooldown_seconds
            logger.warning(
                "Provider %s hit %d consecutive failures — cooling down for %.0fs",
                self.cfg.name,
                self._consecutive_failures,
                self.cfg.cooldown_seconds,
            )

    # ── Session ───────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    headers = {
                        "Authorization": f"Bearer {self.cfg.api_key}",
                        "Content-Type": "application/json",
                    }
                    headers.update(self.cfg.extra_headers)
                    self._session = aiohttp.ClientSession(
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                    )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Chat Completion ───────────────────────────────────────────

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        resolved_model = self.cfg.resolve_model(model)
        payload = {
            "model": resolved_model,
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
                    f"{self.cfg.base_url}/chat/completions", json=payload
                ) as resp:
                    latency = (time.monotonic() - start) * 1000

                    if resp.status == 200:
                        try:
                            data = await resp.json(content_type=None)
                            choice = data["choices"][0]
                            usage = data.get("usage", {})
                            content = choice["message"]["content"]
                        except (
                            aiohttp.ContentTypeError,
                            json.JSONDecodeError,
                            KeyError,
                            IndexError,
                            TypeError,
                        ) as exc:
                            raise LLMClientError(
                                f"[{self.cfg.name}] Malformed API response: {exc}"
                            ) from exc

                        self.record_success()
                        return LLMResponse(
                            content=content,
                            model=data.get("model", resolved_model),
                            prompt_tokens=usage.get("prompt_tokens", 0),
                            completion_tokens=usage.get("completion_tokens", 0),
                            total_tokens=usage.get("total_tokens", 0),
                            finish_reason=choice.get("finish_reason", "stop"),
                            latency_ms=latency,
                            provider=self.cfg.name,
                        )

                    body = await resp.text()

                    # Retriable server / rate-limit errors
                    if resp.status == 429 or resp.status >= 500:
                        last_error = LLMClientError(
                            f"[{self.cfg.name}] API error {resp.status}: {body[:200]}",
                            status_code=resp.status,
                        )
                        wait = _retry_wait(resp, attempt)
                        if attempt < API_MAX_RETRIES - 1:
                            logger.warning(
                                "[%s] %s (attempt %d/%d), waiting %.1fs",
                                self.cfg.name,
                                "Rate limited" if resp.status == 429 else f"Error {resp.status}",
                                attempt + 1,
                                API_MAX_RETRIES,
                                wait,
                            )
                            await asyncio.sleep(wait)
                            continue
                        raise last_error

                    # Non-retriable client error (4xx except 429)
                    raise LLMClientError(
                        f"[{self.cfg.name}] API error {resp.status}: {body[:200]}",
                        status_code=resp.status,
                    )

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                wait = API_RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "[%s] Network error (attempt %d/%d): %s",
                    self.cfg.name, attempt + 1, API_MAX_RETRIES, exc,
                )
                if attempt < API_MAX_RETRIES - 1:
                    await asyncio.sleep(wait)

        self.record_failure()
        raise LLMClientError(
            f"[{self.cfg.name}] All {API_MAX_RETRIES} retries failed: {last_error}"
        )

    # ── Streaming Chat ────────────────────────────────────────────

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        resolved_model = self.cfg.resolve_model(model)
        payload = {
            "model": resolved_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        last_error: Optional[Exception] = None
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT * 2)
        content_yielded = False

        for attempt in range(API_MAX_RETRIES):
            try:
                session = await self._get_session()
                async with session.post(
                    f"{self.cfg.base_url}/chat/completions",
                    json=payload,
                    timeout=timeout,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        if resp.status == 429 or resp.status >= 500:
                            last_error = LLMClientError(
                                f"[{self.cfg.name}] Stream error {resp.status}: {body[:200]}",
                                status_code=resp.status,
                            )
                            wait = _retry_wait(resp, attempt)
                            if attempt < API_MAX_RETRIES - 1:
                                logger.warning(
                                    "[%s] Stream %s (attempt %d/%d), waiting %.1fs",
                                    self.cfg.name,
                                    "rate limited" if resp.status == 429 else f"error {resp.status}",
                                    attempt + 1,
                                    API_MAX_RETRIES,
                                    wait,
                                )
                                await asyncio.sleep(wait)
                                continue
                            raise last_error
                        raise LLMClientError(
                            f"[{self.cfg.name}] Stream error {resp.status}: {body[:200]}",
                            status_code=resp.status,
                        )

                    buffer = b""
                    async for chunk in resp.content.iter_any():
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            try:
                                decoded = line.decode("utf-8").strip()
                            except UnicodeDecodeError:
                                continue
                            if not decoded or not decoded.startswith("data:"):
                                continue
                            data_str = decoded[5:].strip()
                            if data_str == "[DONE]":
                                self.record_success()
                                return
                            try:
                                data = json.loads(data_str)
                                delta = data["choices"][0].get("delta", {})
                                content = delta.get("content")
                                if content:
                                    content_yielded = True
                                    yield content
                            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                                continue

                    self.record_success()
                    return

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if content_yielded:
                    raise LLMClientError(
                        f"[{self.cfg.name}] Stream interrupted after partial content: {exc}"
                    ) from exc
                last_error = exc
                wait = API_RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "[%s] Stream network error (attempt %d/%d): %s",
                    self.cfg.name, attempt + 1, API_MAX_RETRIES, exc,
                )
                if attempt < API_MAX_RETRIES - 1:
                    await asyncio.sleep(wait)
                    continue
                raise LLMClientError(
                    f"[{self.cfg.name}] All {API_MAX_RETRIES} stream retries failed: {exc}"
                ) from exc

        self.record_failure()

    # ── List models ───────────────────────────────────────────────

    async def list_models(self) -> List[str]:
        try:
            session = await self._get_session()
            async with session.get(f"{self.cfg.base_url}/models") as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    return [m["id"] for m in data.get("data", [])]
        except Exception as exc:
            logger.debug("[%s] Could not fetch models: %s", self.cfg.name, exc)
        return []


# ── Helpers ───────────────────────────────────────────────────────────


def _retry_wait(resp: aiohttp.ClientResponse, attempt: int) -> float:
    """Calculate retry wait from Retry-After header or exponential backoff."""
    if resp.status == 429:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
    return API_RETRY_BASE_DELAY * (2 ** attempt)


# ── Multi-Provider Orchestrator (the public interface) ────────────────


class LLMClient:
    """
    Drop-in replacement for the old single-provider ``LLMClient``.

    Holds one ``_ProviderClient`` per configured provider and routes
    requests through them in priority order, falling back automatically
    on failures.

    **Public API** (unchanged from the original):
      - ``chat(messages, model, temperature, max_tokens) → LLMResponse``
      - ``chat_stream(messages, model, temperature, max_tokens) → AsyncIterator[str]``
      - ``simple_prompt(prompt, system, model, max_tokens) → LLMResponse``
      - ``list_models() → List[str]``
      - ``close() → None``
    """

    def __init__(
        self,
        # Legacy single-provider args (backward compat with bot.py)
        api_key: str = "",
        base_url: str = "",
        default_model: str = "gpt-4",
        # New multi-provider args
        providers: Optional[Sequence[ProviderConfig]] = None,
    ):
        self.default_model = default_model
        self._providers: List[_ProviderClient] = []

        if providers:
            # New multi-provider path
            for cfg in sorted(providers, key=lambda c: c.priority):
                if cfg.enabled and cfg.api_key:
                    self._providers.append(_ProviderClient(cfg))
            logger.info(
                "LLMClient initialised with %d provider(s): %s",
                len(self._providers),
                ", ".join(p.cfg.name for p in self._providers),
            )
        elif api_key:
            # Legacy single-provider path (MegaLLM)
            legacy_cfg = ProviderConfig(
                name="megallm",
                base_url=base_url.rstrip("/") if base_url else "https://ai.megallm.io/v1",
                api_key=api_key,
                default_model=default_model,
                priority=0,
            )
            self._providers.append(_ProviderClient(legacy_cfg))
            logger.info("LLMClient initialised with legacy single provider (megallm)")

        if not self._providers:
            logger.error(
                "LLMClient has NO configured providers — all LLM calls will fail!"
            )

    # ── Provider Selection ────────────────────────────────────────

    def _available_providers(self) -> List[_ProviderClient]:
        """Return providers that are currently available (not in cooldown)."""
        return [p for p in self._providers if p.is_available]

    # ── Core Chat Completion (with fallback) ──────────────────────

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """Send a chat completion request, falling back across providers."""
        model = model or self.default_model
        available = self._available_providers()

        if not available:
            # All providers in cooldown — forcibly try the first enabled one
            available = [p for p in self._providers if p.cfg.enabled and p.cfg.api_key]
            if not available:
                raise LLMClientError("No LLM providers configured or all are disabled")

        last_error: Optional[Exception] = None

        for provider in available:
            try:
                resp = await provider.chat(messages, model, temperature, max_tokens)
                logger.debug(
                    "Chat fulfilled by %s (%.0fms)", provider.cfg.name, resp.latency_ms
                )
                return resp
            except LLMClientError as exc:
                last_error = exc
                provider.record_failure()
                # Non-retriable auth errors → skip to next immediately
                if exc.status_code and 400 <= exc.status_code < 500 and exc.status_code != 429:
                    logger.warning(
                        "Provider %s returned %d — skipping to next",
                        provider.cfg.name, exc.status_code,
                    )
                else:
                    logger.warning(
                        "Provider %s failed: %s — trying next", provider.cfg.name, exc
                    )
            except Exception as exc:
                last_error = exc
                provider.record_failure()
                logger.warning(
                    "Provider %s unexpected error: %s — trying next",
                    provider.cfg.name, exc,
                )

        raise LLMClientError(
            f"All {len(available)} providers failed. Last error: {last_error}"
        )

    # ── Streaming Chat (with fallback) ────────────────────────────

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        """Stream chat tokens, falling back across providers on pre-stream errors."""
        model = model or self.default_model
        available = self._available_providers()

        if not available:
            available = [p for p in self._providers if p.cfg.enabled and p.cfg.api_key]
            if not available:
                raise LLMClientError("No LLM providers configured or all are disabled")

        last_error: Optional[Exception] = None

        for provider in available:
            try:
                async for chunk in provider.chat_stream(
                    messages, model, temperature, max_tokens
                ):
                    yield chunk
                # If we get here the stream completed successfully
                logger.debug("Stream fulfilled by %s", provider.cfg.name)
                return
            except LLMClientError as exc:
                # If content was already yielded, we can't retry — propagate
                if "partial content" in str(exc):
                    raise
                last_error = exc
                provider.record_failure()
                logger.warning(
                    "Provider %s stream failed: %s — trying next",
                    provider.cfg.name, exc,
                )
            except Exception as exc:
                last_error = exc
                provider.record_failure()
                logger.warning(
                    "Provider %s stream unexpected error: %s — trying next",
                    provider.cfg.name, exc,
                )

        raise LLMClientError(
            f"All {len(available)} providers failed streaming. Last error: {last_error}"
        )

    # ── Convenience Methods ───────────────────────────────────────

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
        """Aggregate available models from ALL active providers."""
        all_models: List[str] = []
        for provider in self._providers:
            if provider.is_available:
                models = await provider.list_models()
                # Prefix with provider name to avoid collisions
                for m in models:
                    prefixed = f"[{provider.cfg.name}] {m}"
                    all_models.append(prefixed)
        return all_models

    async def close(self) -> None:
        """Close all provider sessions."""
        for provider in self._providers:
            await provider.close()

    # ── Status / Debug ────────────────────────────────────────────

    def provider_status(self) -> List[Dict[str, object]]:
        """Return a summary of each provider's health (for /admin or health endpoint)."""
        return [
            {
                "name": p.cfg.name,
                "enabled": p.cfg.enabled,
                "available": p.is_available,
                "consecutive_failures": p._consecutive_failures,
                "base_url": p.cfg.base_url,
                "default_model": p.cfg.default_model,
            }
            for p in self._providers
        ]
