"""
Standardized Discord embed builder for consistent bot responses.
"""

from __future__ import annotations

import datetime
from typing import List, Optional, Tuple

import discord

from config.constants import (
    BOT_COLOR,
    BOT_ERROR_COLOR,
    BOT_INFO_COLOR,
    BOT_NAME,
    BOT_SUCCESS_COLOR,
    BOT_WARN_COLOR,
)


class Embedder:
    """Factory for creating consistent, branded Discord embeds."""

    @staticmethod
    def _base(
        title: str,
        description: str,
        color: int,
        *,
        footer: Optional[str] = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_footer(text=footer or f"✨ {BOT_NAME}")
        return embed

    # ── Standard Embed Types ─────────────────────────────────────────

    @classmethod
    def standard(
        cls,
        title: str,
        description: str,
        *,
        fields: Optional[List[Tuple[str, str, bool]]] = None,
        footer: Optional[str] = None,
        thumbnail: Optional[str] = None,
    ) -> discord.Embed:
        """Create a standard themed embed."""
        embed = cls._base(title, description, BOT_COLOR, footer=footer)
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        for name, value, inline in (fields or []):
            embed.add_field(name=name, value=value, inline=inline)
        return embed

    @classmethod
    def success(cls, title: str, description: str) -> discord.Embed:
        return cls._base(f"✅ {title}", description, BOT_SUCCESS_COLOR)

    @classmethod
    def error(cls, title: str, description: str) -> discord.Embed:
        return cls._base(f"❌ {title}", description, BOT_ERROR_COLOR)

    @classmethod
    def warning(cls, title: str, description: str) -> discord.Embed:
        return cls._base(f"⚠️ {title}", description, BOT_WARN_COLOR)

    @classmethod
    def info(cls, title: str, description: str) -> discord.Embed:
        return cls._base(f"ℹ️ {title}", description, BOT_INFO_COLOR)

    # ── Specialized Embeds ───────────────────────────────────────────

    @classmethod
    def chat_response(
        cls,
        content: str,
        model: str,
        tokens: int = 0,
        latency_ms: float = 0.0,
    ) -> discord.Embed:
        """Create an embed for an AI chat response."""
        # Truncate to embed description limit
        if len(content) > 4096:
            content = content[:4090] + "\n…"

        embed = cls._base("💬 Starzai", content, BOT_COLOR)
        parts = []
        if model:
            parts.append(f"Model: {model}")
        if tokens:
            parts.append(f"Tokens: {tokens:,}")
        if latency_ms:
            parts.append(f"Latency: {latency_ms:.0f}ms")
        embed.set_footer(text=" • ".join(parts) if parts else f"✨ {BOT_NAME}")
        return embed

    @classmethod
    def streaming(cls, partial: str = "⏳ Thinking...") -> discord.Embed:
        """Create an embed used during streaming updates."""
        if len(partial) > 4096:
            partial = partial[:4090] + "\n…"
        return cls._base("💬 Starzai", partial, BOT_COLOR, footer="⏳ Streaming…")

    @classmethod
    def rate_limited(cls, retry_after: float) -> discord.Embed:
        """Create a rate-limit warning embed."""
        return cls.warning(
            "Slow Down",
            f"You're sending requests too fast.\nPlease wait **{retry_after:.1f}s** before trying again.",
        )

    @classmethod
    def model_list(cls, models: List[str], current: str) -> discord.Embed:
        """Create an embed listing available models."""
        lines = []
        for m in models:
            marker = " ◀ *current*" if m == current else ""
            lines.append(f"• `{m}`{marker}")
        return cls.standard(
            "🤖 Available Models",
            "\n".join(lines) or "No models configured.",
        )

    @classmethod
    def conversation_status(cls, action: str, detail: str = "") -> discord.Embed:
        """Create an embed for conversation lifecycle events."""
        icons = {"start": "🟢", "end": "🔴", "clear": "🧹"}
        icon = icons.get(action, "💬")
        return cls.info(
            f"{icon} Conversation {action.title()}",
            detail or f"Conversation {action}ed successfully.",
        )

    @classmethod
    def searching(cls, query: str = "") -> discord.Embed:
        """Create a loading embed while web search is in progress."""
        desc = f"🔍 Searching the web for: **{query}**\n\n⏳ Analyzing results…" if query else "🔍 Searching the web…"
        return cls._base("🌐 Web Search", desc, BOT_INFO_COLOR, footer="⏳ Searching…")

    @classmethod
    def search_response(
        cls,
        content: str,
        query: str,
        sources: str = "",
        search_type: str = "web",
        cached: bool = False,
        provider: str = "",
        image_url: str = "",
        video_text: str = "",
    ) -> discord.Embed:
        """Create a rich embed for an AI-synthesized web search answer."""
        if len(content) > 3500:
            content = content[:3494] + "\n…"

        icon = "📰" if search_type == "news" else "🌐"
        embed = cls._base(
            f"{icon} {query[:80]}",
            content,
            BOT_INFO_COLOR,
        )
        if sources:
            embed.add_field(name="📎 Sources", value=sources[:1024], inline=False)
        if video_text:
            embed.add_field(name="🎬 Related Video", value=video_text[:1024], inline=False)
        if image_url:
            embed.set_image(url=image_url)
        footer_parts = [f"Search: {search_type}"]
        if provider:
            footer_parts.append(f"via {provider}")
        if cached:
            footer_parts.append("cached")
        footer_parts.append(f"✨ {BOT_NAME}")
        embed.set_footer(text=" • ".join(footer_parts))
        return embed

    @classmethod
    def auto_news(
        cls,
        content: str,
        topic: str,
        sources: str = "",
        image_url: str = "",
        next_update: str = "",
    ) -> discord.Embed:
        """Create an embed for automatic news channel updates."""
        if len(content) > 3500:
            content = content[:3494] + "\n…"

        embed = cls._base(
            f"📡 Auto-News: {topic[:60]}",
            content,
            BOT_INFO_COLOR,
        )
        if sources:
            embed.add_field(name="📎 Sources", value=sources[:1024], inline=False)
        if image_url:
            embed.set_image(url=image_url)
        footer_parts = ["📡 Auto-News"]
        if next_update:
            footer_parts.append(f"Next update: {next_update}")
        footer_parts.append(f"✨ {BOT_NAME}")
        embed.set_footer(text=" • ".join(footer_parts))
        return embed

    @classmethod
    def paginated(
        cls,
        title: str,
        content: str,
        page: int,
        total_pages: int,
    ) -> discord.Embed:
        """Create a paginated embed with page info in the footer."""
        embed = cls._base(title, content, BOT_COLOR)
        embed.set_footer(text=f"Page {page}/{total_pages} • ✨ {BOT_NAME}")
        return embed
