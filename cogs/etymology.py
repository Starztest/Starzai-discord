"""
Etymology cog — Word origins and linguistic analysis.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from utils.embedder import Embedder
from utils.llm_client import LLMClientError

if TYPE_CHECKING:
    from bot import StarzaiBot

logger = logging.getLogger(__name__)


class EtymologyCog(commands.Cog, name="Etymology"):
    """Explore word origins, history, and linguistic roots."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot

    # ── /etymology ───────────────────────────────────────────────────

    @app_commands.command(
        name="etymology", description="Discover the origin and roots of a word"
    )
    @app_commands.describe(word="The word to look up")
    async def etymology_cmd(
        self, interaction: discord.Interaction, word: str
    ) -> None:
        result = self.bot.rate_limiter.check(interaction.user.id, interaction.guild_id)
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return

        await interaction.response.defer()

        prompt = (
            f"Provide a detailed etymology of the word \"{word}\".\n\n"
            "Include:\n"
            "1. **Language of Origin** — the original language and root word\n"
            "2. **Root Meaning** — what the original word meant\n"
            "3. **Evolution** — how the word changed over time through different languages\n"
            "4. **First Known Use** — approximate date of first recorded use in English\n"
            "5. **Related Words** — 3-5 words that share the same root\n\n"
            "Format your response with clear sections using markdown."
        )

        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system="You are an expert etymologist and historical linguist. Provide accurate, detailed word origin analysis.",
            )

            embed = Embedder.standard(
                f"📜 Etymology: *{word}*",
                resp.content,
                footer=f"Tokens used: {resp.total_tokens:,}",
            )
            await interaction.followup.send(embed=embed)
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="etymology",
                guild_id=interaction.guild_id,
                tokens_used=resp.total_tokens,
                latency_ms=resp.latency_ms,
            )

        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Etymology Error", str(exc))
            )

    # ── /word-history ────────────────────────────────────────────────

    @app_commands.command(
        name="word-history",
        description="Explore the historical journey of a word through time",
    )
    @app_commands.describe(word="The word to trace through history")
    async def word_history_cmd(
        self, interaction: discord.Interaction, word: str
    ) -> None:
        result = self.bot.rate_limiter.check(interaction.user.id, interaction.guild_id)
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return

        await interaction.response.defer()

        prompt = (
            f"Create a historical timeline for the word \"{word}\".\n\n"
            "Present it as a timeline with approximate dates, showing:\n"
            "- The earliest known form and language\n"
            "- Key transformations as it passed through different languages\n"
            "- How its meaning shifted over centuries\n"
            "- Notable historical contexts where the word was significant\n"
            "- Its modern usage and any recent meaning changes\n\n"
            "Use a timeline format with dates/periods on the left."
        )

        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system="You are a historical linguist. Create engaging, accurate word history timelines.",
            )

            embed = Embedder.standard(
                f"📖 Word History: *{word}*",
                resp.content,
            )
            await interaction.followup.send(embed=embed)
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="word-history",
                guild_id=interaction.guild_id,
                tokens_used=resp.total_tokens,
                latency_ms=resp.latency_ms,
            )

        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Word History Error", str(exc))
            )


async def setup(bot: StarzaiBot) -> None:
    await bot.add_cog(EtymologyCog(bot))

