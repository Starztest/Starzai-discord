"""
Web Search cog — /search, /news, and /setnews commands + auto-news background task.

Features:
  • Multi-provider web search with fallback chain (DDG → Google RSS → GNews → Currents)
  • Rich media embeds with images and video links
  • /setnews command to configure per-guild automatic news delivery
  • Background task that sends periodic news briefings to configured channels
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config.constants import (
    AUTO_NEWS_CHECK_INTERVAL,
    AUTO_NEWS_DEFAULT_INTERVAL,
    AUTO_NEWS_MAX_GUILDS_PER_TICK,
    AUTO_NEWS_MAX_INTERVAL,
    AUTO_NEWS_MIN_INTERVAL,
)
from utils.embedder import Embedder
from utils.llm_client import LLMClientError
from utils.web_search import WebSearcher

if TYPE_CHECKING:
    from bot import StarzaiBot

logger = logging.getLogger(__name__)

# ── System Prompts ────────────────────────────────────────────────

SEARCH_SYSTEM_PROMPT = (
    "You are Starzai, an AI assistant with real-time web search capabilities. "
    "The user asked a question and web search results have been provided below. "
    "Your job is to synthesize a clear, accurate, and informative answer using "
    "ONLY the information from the search results provided. "
    "Rules:\n"
    "- Be factual and cite information from the sources\n"
    "- If the search results don't contain enough info, say so honestly\n"
    "- Use Discord markdown formatting: **bold**, *italic*, `code`\n"
    "- Keep the response concise but comprehensive (aim for 200-400 words)\n"
    "- When mentioning specific claims, reference which source it came from\n"
    "- Include relevant dates, numbers, and specifics from the results\n"
    "- Do NOT make up information that isn't in the search results\n"
)

NEWS_SYSTEM_PROMPT = (
    "You are Starzai, an AI assistant reporting on current events and breaking news. "
    "Recent news search results have been provided below. "
    "Your job is to synthesize a clear, well-organized news briefing using "
    "ONLY the information from the search results provided. "
    "Rules:\n"
    "- Present the most important/recent developments first\n"
    "- Be factual — cite which source reported what\n"
    "- Include dates, casualty numbers, and concrete details when available\n"
    "- Use Discord markdown: **bold** for key facts, headers for sections\n"
    "- Present multiple perspectives if the sources show different viewpoints\n"
    "- Keep the briefing concise but thorough (aim for 300-500 words)\n"
    "- If covering a conflict/war, include the latest status and key developments\n"
    "- Do NOT speculate beyond what the sources report\n"
)

AUTO_NEWS_SYSTEM_PROMPT = (
    "You are Starzai, delivering an automatic news update for a Discord channel. "
    "Summarize the most important and NEWEST developments from the search results below. "
    "Rules:\n"
    "- Lead with breaking/most recent news first\n"
    "- Be concise — aim for 200-350 words\n"
    "- Use Discord markdown: **bold** for key facts, bullet points for clarity\n"
    "- Include specific dates, numbers, and names\n"
    "- Cite sources inline (e.g., 'according to Reuters...')\n"
    "- End with a brief 1-sentence outlook if the sources suggest one\n"
    "- Do NOT repeat old news or speculate\n"
)


class SearchCog(commands.Cog, name="Search"):
    """Real-time web search with multi-provider fallback, rich media, and auto-news."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot
        self.searcher = WebSearcher()
        # Cache for news channel configs: guild_id -> config dict or None
        self._news_channel_cache: Dict[int, Optional[dict]] = {}

    async def cog_load(self) -> None:
        """Start the auto-news background task when cog loads."""
        self.auto_news_task.start()

    async def cog_unload(self) -> None:
        """Stop the auto-news background task when cog unloads."""
        self.auto_news_task.cancel()

    # ── Helpers ───────────────────────────────────────────────────

    async def _gate(
        self, interaction: discord.Interaction, expensive: bool = False
    ) -> bool:
        """Check rate limits. Returns True if allowed."""
        result = self.bot.rate_limiter.check(
            user_id=interaction.user.id,
            server_id=interaction.guild_id,
            expensive=expensive,
        )
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return False

        token_result = self.bot.rate_limiter.check_token_budget(
            user_id=interaction.user.id, server_id=interaction.guild_id
        )
        if not token_result.allowed:
            await interaction.response.send_message(
                embed=Embedder.warning("Token Limit", token_result.reason),
                ephemeral=True,
            )
            return False

        return True

    async def _resolve_model(
        self, user_id: int, explicit: Optional[str] = None
    ) -> str:
        if explicit:
            return self.bot.settings.resolve_model(explicit)
        saved = await self.bot.database.get_user_model(user_id)
        if saved:
            return saved
        return self.bot.settings.default_model

    async def _log(
        self,
        interaction: discord.Interaction,
        command: str,
        model: str,
        tokens: int = 0,
        latency_ms: float = 0.0,
        success: bool = True,
        error_message: Optional[str] = None,
    ) -> None:
        await self.bot.database.log_usage(
            user_id=interaction.user.id,
            command=command,
            guild_id=interaction.guild_id,
            model=model,
            tokens_used=tokens,
            latency_ms=latency_ms,
            success=success,
            error_message=error_message,
        )
        if tokens:
            await self.bot.database.add_user_tokens(interaction.user.id, tokens)
            self.bot.rate_limiter.record_tokens(
                interaction.user.id, tokens, interaction.guild_id
            )

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)

    # ══════════════════════════════════════════════════════════════
    #  /search — General web search with rich media
    # ══════════════════════════════════════════════════════════════

    @app_commands.command(
        name="search",
        description="Search the web and get an AI-synthesized answer with images & sources",
    )
    @app_commands.describe(
        query="What to search for (e.g. 'latest Ukraine war updates')",
        model="AI model to use (optional)",
    )
    async def search_cmd(
        self,
        interaction: discord.Interaction,
        query: str,
        model: str = "",
    ) -> None:
        if not await self._gate(interaction, expensive=True):
            return

        await interaction.response.defer()
        searching_msg = await interaction.followup.send(
            embed=Embedder.searching(query), wait=True
        )

        resolved_model = await self._resolve_model(interaction.user.id, model or None)
        start = time.monotonic()

        try:
            # Run search + media fetch concurrently
            search_task = self.searcher.search(query)
            media_task = self.searcher.search_media(query, max_images=2, max_videos=1)
            search_response, (images, videos) = await asyncio.gather(
                search_task, media_task
            )

            if search_response.error:
                await searching_msg.edit(
                    embed=Embedder.error("Search Failed", search_response.error)
                )
                await self._log(
                    interaction, "search", resolved_model,
                    success=False, error_message=search_response.error,
                )
                return

            if not search_response.has_results:
                await searching_msg.edit(
                    embed=Embedder.warning(
                        "No Results",
                        f"No web results found for: **{query}**\n\n"
                        "Try rephrasing your search or using different keywords.",
                    )
                )
                return

            # Attach media to the response
            search_response.images = images
            search_response.videos = videos

            # Format results as LLM context
            search_context = WebSearcher.format_results_for_llm(search_response)
            messages = [
                {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
                {"role": "user", "content": f"Question: {query}\n\n{search_context}"},
            ]

            resp = await self.bot.llm.chat(messages, model=resolved_model, max_tokens=2048)
            latency = (time.monotonic() - start) * 1000

            # Build rich embed
            sources = WebSearcher.format_sources_for_embed(search_response)
            image_url = search_response.best_image or ""
            video = search_response.best_video
            video_text = WebSearcher.format_video_for_embed(video) if video else ""

            await searching_msg.edit(
                embed=Embedder.search_response(
                    content=resp.content,
                    query=query,
                    sources=sources,
                    search_type="web",
                    cached=search_response.cached,
                    provider=search_response.provider,
                    image_url=image_url,
                    video_text=video_text,
                )
            )

            await self._log(
                interaction, "search", resolved_model,
                tokens=resp.total_tokens or self._estimate_tokens(query + resp.content),
                latency_ms=latency,
            )

        except LLMClientError as exc:
            logger.error("LLM error in /search: %s", exc)
            await searching_msg.edit(
                embed=Embedder.error("AI Error", f"Search completed but AI synthesis failed: {exc}")
            )
            await self._log(
                interaction, "search", resolved_model,
                success=False, error_message=str(exc),
            )
        except Exception as exc:
            logger.error("Unexpected error in /search: %s", exc, exc_info=True)
            await searching_msg.edit(
                embed=Embedder.error("Error", "Something went wrong. Please try again.")
            )
            await self._log(
                interaction, "search", resolved_model,
                success=False, error_message=str(exc),
            )

    # ══════════════════════════════════════════════════════════════
    #  /news — Breaking news briefing with rich media
    # ══════════════════════════════════════════════════════════════

    @app_commands.command(
        name="news",
        description="Get the latest news on any topic with AI-powered briefing, images & sources",
    )
    @app_commands.describe(
        topic="News topic (e.g. 'Israel Hamas war', 'AI regulation', 'stock market')",
        model="AI model to use (optional)",
    )
    async def news_cmd(
        self,
        interaction: discord.Interaction,
        topic: str,
        model: str = "",
    ) -> None:
        if not await self._gate(interaction, expensive=True):
            return

        await interaction.response.defer()
        searching_msg = await interaction.followup.send(
            embed=Embedder.searching(f"📰 {topic}"), wait=True
        )

        resolved_model = await self._resolve_model(interaction.user.id, model or None)
        start = time.monotonic()

        try:
            # Run news search + media fetch concurrently
            news_task = self.searcher.search_news(topic)
            media_task = self.searcher.search_media(f"{topic} news", max_images=2, max_videos=1)
            news_response, (images, videos) = await asyncio.gather(
                news_task, media_task
            )

            if news_response.error:
                await searching_msg.edit(
                    embed=Embedder.error("News Search Failed", news_response.error)
                )
                await self._log(
                    interaction, "news", resolved_model,
                    success=False, error_message=news_response.error,
                )
                return

            if not news_response.has_results:
                await searching_msg.edit(
                    embed=Embedder.warning(
                        "No News Found",
                        f"No recent news found for: **{topic}**\n\n"
                        "Try broadening your topic or checking the spelling.",
                    )
                )
                return

            news_response.images = images
            news_response.videos = videos

            news_context = WebSearcher.format_results_for_llm(news_response)
            messages = [
                {"role": "system", "content": NEWS_SYSTEM_PROMPT},
                {"role": "user", "content": f"Topic: {topic}\n\n{news_context}"},
            ]

            resp = await self.bot.llm.chat(messages, model=resolved_model, max_tokens=2048)
            latency = (time.monotonic() - start) * 1000

            sources = WebSearcher.format_sources_for_embed(news_response)
            image_url = news_response.best_image or ""
            video = news_response.best_video
            video_text = WebSearcher.format_video_for_embed(video) if video else ""

            await searching_msg.edit(
                embed=Embedder.search_response(
                    content=resp.content,
                    query=topic,
                    sources=sources,
                    search_type="news",
                    cached=news_response.cached,
                    provider=news_response.provider,
                    image_url=image_url,
                    video_text=video_text,
                )
            )

            await self._log(
                interaction, "news", resolved_model,
                tokens=resp.total_tokens or self._estimate_tokens(topic + resp.content),
                latency_ms=latency,
            )

        except LLMClientError as exc:
            logger.error("LLM error in /news: %s", exc)
            await searching_msg.edit(
                embed=Embedder.error("AI Error", f"News search completed but AI synthesis failed: {exc}")
            )
            await self._log(
                interaction, "news", resolved_model,
                success=False, error_message=str(exc),
            )
        except Exception as exc:
            logger.error("Unexpected error in /news: %s", exc, exc_info=True)
            await searching_msg.edit(
                embed=Embedder.error("Error", "Something went wrong. Please try again.")
            )
            await self._log(
                interaction, "news", resolved_model,
                success=False, error_message=str(exc),
            )

    # ══════════════════════════════════════════════════════════════
    #  /setnews — Configure auto-news for a channel
    # ══════════════════════════════════════════════════════════════

    @app_commands.command(
        name="setnews",
        description="Set up automatic news updates in a channel (admin only)",
    )
    @app_commands.describe(
        channel="Channel to send auto-news to (leave empty to show/remove config)",
        topic="News topic to track (e.g. 'world war', 'AI news', 'crypto')",
        interval="Minutes between updates (15–1440, default 30)",
        action="Enable, disable, or remove the auto-news config",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Enable", value="enable"),
            app_commands.Choice(name="Disable", value="disable"),
            app_commands.Choice(name="Remove", value="remove"),
            app_commands.Choice(name="Status", value="status"),
        ]
    )
    async def setnews_cmd(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        topic: str = "",
        interval: int = AUTO_NEWS_DEFAULT_INTERVAL,
        action: str = "enable",
    ) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        # Permission check: admin or bot owner
        is_admin = (
            interaction.user.guild_permissions.manage_guild
            if hasattr(interaction.user, "guild_permissions")
            else False
        )
        is_owner = interaction.user.id in self.bot.settings.owner_ids
        if not is_admin and not is_owner:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Permission Denied",
                    "You need **Manage Server** permission to configure auto-news.",
                ),
                ephemeral=True,
            )
            return

        db = self.bot.database
        gid = str(interaction.guild_id)

        # ── Status ────────────────────────────────────────────────
        if action == "status":
            config = await db.get_news_channel(gid)
            if not config:
                await interaction.response.send_message(
                    embed=Embedder.warning(
                        "No Auto-News",
                        "No auto-news channel configured for this server.\n\n"
                        "Use `/setnews channel:#channel topic:\"your topic\"` to set one up!",
                    ),
                    ephemeral=True,
                )
            else:
                status_icon = "🟢" if config["enabled"] else "🔴"
                last = config.get("last_sent_at") or "Never"
                await interaction.response.send_message(
                    embed=Embedder.standard(
                        "📡 Auto-News Config",
                        "",
                        fields=[
                            ("Status", f"{status_icon} {'Enabled' if config['enabled'] else 'Disabled'}", True),
                            ("Channel", f"<#{config['channel_id']}>", True),
                            ("Topic", config["topic"], True),
                            ("Interval", f"Every {config['interval_minutes']} minutes", True),
                            ("Last Sent", last, True),
                            ("Configured By", f"<@{config['configured_by']}>", True),
                        ],
                    ),
                    ephemeral=True,
                )
            return

        # ── Remove ────────────────────────────────────────────────
        if action == "remove":
            removed = await db.remove_news_channel(gid)
            self._news_channel_cache.pop(interaction.guild_id, None)
            if removed:
                await interaction.response.send_message(
                    embed=Embedder.success(
                        "Auto-News Removed",
                        "🗑️ Auto-news has been removed for this server.",
                    )
                )
            else:
                await interaction.response.send_message(
                    embed=Embedder.warning("No Config", "There's no auto-news configured to remove."),
                    ephemeral=True,
                )
            return

        # ── Disable ───────────────────────────────────────────────
        if action == "disable":
            toggled = await db.toggle_news_channel(gid, enabled=False)
            self._news_channel_cache.pop(interaction.guild_id, None)
            if toggled:
                await interaction.response.send_message(
                    embed=Embedder.success(
                        "Auto-News Paused",
                        "⏸️ Auto-news has been **paused**. Use `/setnews action:Enable` to resume.",
                    )
                )
            else:
                await interaction.response.send_message(
                    embed=Embedder.warning("No Config", "No auto-news is configured to disable."),
                    ephemeral=True,
                )
            return

        # ── Enable (new or update) ────────────────────────────────
        if action == "enable":
            # Check if re-enabling existing config (no channel/topic needed)
            existing = await db.get_news_channel(gid)
            if existing and not channel and not topic:
                await db.toggle_news_channel(gid, enabled=True)
                self._news_channel_cache.pop(interaction.guild_id, None)
                await interaction.response.send_message(
                    embed=Embedder.success(
                        "Auto-News Resumed",
                        f"▶️ Auto-news for **{existing['topic']}** in <#{existing['channel_id']}> "
                        f"has been **resumed** (every {existing['interval_minutes']} min).",
                    )
                )
                return

            # Need channel + topic for new config
            if not channel or not topic:
                await interaction.response.send_message(
                    embed=Embedder.warning(
                        "Missing Info",
                        "To set up auto-news, provide both a **channel** and a **topic**.\n\n"
                        "Example: `/setnews channel:#news topic:\"world war\" interval:30`",
                    ),
                    ephemeral=True,
                )
                return

            # Validate interval
            interval = max(AUTO_NEWS_MIN_INTERVAL, min(interval, AUTO_NEWS_MAX_INTERVAL))

            await db.set_news_channel(
                guild_id=gid,
                channel_id=str(channel.id),
                topic=topic,
                interval_minutes=interval,
                configured_by=str(interaction.user.id),
            )
            self._news_channel_cache.pop(interaction.guild_id, None)

            await interaction.response.send_message(
                embed=Embedder.success(
                    "📡 Auto-News Configured!",
                    f"✅ {channel.mention} will now receive **auto-news** updates!\n\n"
                    f"📰 **Topic:** {topic}\n"
                    f"⏱️ **Interval:** Every {interval} minutes\n"
                    f"🔄 **Providers:** DuckDuckGo → Google News → GNews → Currents\n\n"
                    f"Use `/setnews action:Status` to check, or `/setnews action:Disable` to pause.",
                )
            )

    # ══════════════════════════════════════════════════════════════
    #  Auto-News Background Task
    # ══════════════════════════════════════════════════════════════

    @tasks.loop(minutes=AUTO_NEWS_CHECK_INTERVAL)
    async def auto_news_task(self) -> None:
        """Periodic task that sends news to configured channels."""
        try:
            configs = await self.bot.database.get_all_active_news_channels()
            if not configs:
                return

            now = datetime.now(timezone.utc)
            processed = 0

            for config in configs:
                if processed >= AUTO_NEWS_MAX_GUILDS_PER_TICK:
                    break

                # Check if interval has elapsed
                last_sent = config.get("last_sent_at")
                if last_sent:
                    try:
                        last_dt = datetime.fromisoformat(last_sent).replace(tzinfo=timezone.utc)
                        interval_td = timedelta(minutes=config["interval_minutes"])
                        if now - last_dt < interval_td:
                            continue
                    except (ValueError, TypeError):
                        pass  # Invalid date, proceed to send

                # Try to send news
                try:
                    await self._send_auto_news(config)
                    processed += 1
                except Exception as exc:
                    logger.error(
                        "Auto-news failed for guild %s: %s",
                        config["guild_id"], exc, exc_info=True,
                    )

        except Exception as exc:
            logger.error("Auto-news task error: %s", exc, exc_info=True)

    @auto_news_task.before_loop
    async def before_auto_news(self) -> None:
        await self.bot.wait_until_ready()
        logger.info("📡 Auto-news background task started (checks every %d min)", AUTO_NEWS_CHECK_INTERVAL)

    async def _send_auto_news(self, config: dict) -> None:
        """Fetch and send an auto-news update to a configured channel."""
        guild_id = int(config["guild_id"])
        channel_id = int(config["channel_id"])
        topic = config["topic"]

        # Get the channel
        channel = self.bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                logger.warning("Auto-news: channel %d not accessible, skipping", channel_id)
                return

        # Load previous URLs for dedup
        try:
            prev_urls = set(json.loads(config.get("last_sent_urls", "[]")))
        except (json.JSONDecodeError, TypeError):
            prev_urls = set()

        # Search for news + media concurrently
        news_task = self.searcher.search_news(topic)
        media_task = self.searcher.search_media(f"{topic} news", max_images=1, max_videos=0)

        try:
            news_response, (images, _) = await asyncio.wait_for(
                asyncio.gather(news_task, media_task),
                timeout=20,
            )
        except asyncio.TimeoutError:
            logger.warning("Auto-news search timed out for topic '%s'", topic)
            return

        if not news_response.has_results:
            return

        # Filter out previously sent articles
        new_results = [r for r in news_response.results if r.url not in prev_urls]
        if not new_results:
            # All results are old — update timestamp but don't send
            await self.bot.database.update_news_last_sent(str(guild_id))
            return

        news_response.results = new_results
        news_response.images = images

        # Get LLM synthesis
        news_context = WebSearcher.format_results_for_llm(news_response)
        messages = [
            {"role": "system", "content": AUTO_NEWS_SYSTEM_PROMPT},
            {"role": "user", "content": f"Topic: {topic}\n\n{news_context}"},
        ]

        try:
            model = self.bot.settings.default_model
            resp = await self.bot.llm.chat(messages, model=model, max_tokens=1500)
        except Exception as exc:
            logger.error("Auto-news LLM failed for '%s': %s", topic, exc)
            return

        # Build and send embed
        sources = WebSearcher.format_sources_for_embed(news_response, max_sources=4)
        image_url = news_response.best_image or ""
        next_mins = config["interval_minutes"]
        next_time = (datetime.now(timezone.utc) + timedelta(minutes=next_mins)).strftime("%H:%M UTC")

        embed = Embedder.auto_news(
            content=resp.content,
            topic=topic,
            sources=sources,
            image_url=image_url,
            next_update=next_time,
        )

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Auto-news: no permission to send in channel %d", channel_id)
            return
        except discord.HTTPException as exc:
            logger.error("Auto-news: failed to send to channel %d: %s", channel_id, exc)
            return

        # Update dedup state
        all_urls = list(prev_urls) + [r.url for r in new_results]
        await self.bot.database.update_news_last_sent(str(guild_id), all_urls)
        logger.info(
            "📡 Auto-news sent to guild %s (#%s) — topic: %s, %d new articles",
            guild_id, channel_id, topic, len(new_results),
        )


async def setup(bot: StarzaiBot) -> None:
    await bot.add_cog(SearchCog(bot))

