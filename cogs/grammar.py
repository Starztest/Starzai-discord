"""
Grammar & Vocabulary Editor cog — Text improvement and grammar checking.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from config.constants import TEXT_STYLES
from utils.embedder import Embedder
from utils.llm_client import LLMClientError

if TYPE_CHECKING:
    from bot import StarzaiBot

logger = logging.getLogger(__name__)


class GrammarCog(commands.Cog, name="Grammar"):
    """Advanced grammar checking and text improvement."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot

    # ── /check-grammar ───────────────────────────────────────────────

    @app_commands.command(
        name="check-grammar", description="Check text for grammar and spelling errors"
    )
    @app_commands.describe(text="The text to check")
    async def check_grammar_cmd(
        self, interaction: discord.Interaction, text: str
    ) -> None:
        result = self.bot.rate_limiter.check(interaction.user.id, interaction.guild_id)
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return

        await interaction.response.defer()

        prompt = (
            f"Analyze the following text for grammar, spelling, and punctuation errors.\n\n"
            f"Text: \"{text}\"\n\n"
            "Provide:\n"
            "1. **Corrected Text** — the fixed version\n"
            "2. **Errors Found** — list each error with an explanation\n"
            "3. **Score** — rate the original text's quality from 1-10\n"
            "4. **Tips** — 1-2 general writing tips based on the errors found\n\n"
            "If the text is already correct, say so and give a high score."
        )

        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system="You are an expert English editor and proofreader. Be thorough but encouraging.",
                max_tokens=4096,  # Maximum tokens for detailed grammar analysis
            )

            embed = Embedder.standard(
                "✏️ Grammar Check",
                resp.content,
                fields=[("Original Text", text[:1024], False)],
            )
            await interaction.followup.send(embed=embed)
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="check-grammar",
                guild_id=interaction.guild_id,
                tokens_used=resp.total_tokens,
                latency_ms=resp.latency_ms,
            )

        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Grammar Check Failed", str(exc))
            )

    # ── /improve-text ────────────────────────────────────────────────

    @app_commands.command(
        name="improve-text",
        description="Improve text with a specific writing style",
    )
    @app_commands.describe(
        text="The text to improve",
        style="Writing style to apply",
    )
    @app_commands.choices(
        style=[
            app_commands.Choice(name=s.title(), value=s)
            for s in TEXT_STYLES
        ]
    )
    async def improve_text_cmd(
        self,
        interaction: discord.Interaction,
        text: str,
        style: str = "formal",
    ) -> None:
        result = self.bot.rate_limiter.check(interaction.user.id, interaction.guild_id)
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return

        await interaction.response.defer()

        style_descriptions = {
            "formal": "professional, polished, suitable for business or academic contexts",
            "casual": "relaxed, conversational, friendly tone",
            "academic": "scholarly, precise, with appropriate terminology",
            "creative": "vivid, expressive, engaging and unique",
            "concise": "brief, to-the-point, removing all unnecessary words",
            "professional": "clear, authoritative, suitable for workplace communication",
        }
        style_desc = style_descriptions.get(style, style)

        prompt = (
            f"Rewrite the following text in a {style} style ({style_desc}).\n\n"
            f"Original text: \"{text}\"\n\n"
            "Provide:\n"
            "1. **Improved Text** — the rewritten version\n"
            "2. **Changes Made** — briefly explain the key changes\n"
            "3. **Style Notes** — 1-2 tips for writing in this style"
        )

        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system=f"You are an expert writing coach specializing in {style} writing.",
                max_tokens=4096,  # Maximum tokens for detailed text improvement
            )

            embed = Embedder.standard(
                f"📝 Text Improvement — {style.title()}",
                resp.content,
                fields=[("Original Text", text[:1024], False)],
            )
            await interaction.followup.send(embed=embed)
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="improve-text",
                guild_id=interaction.guild_id,
                tokens_used=resp.total_tokens,
                latency_ms=resp.latency_ms,
            )

        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Improvement Failed", str(exc))
            )


async def setup(bot: StarzaiBot) -> None:
    await bot.add_cog(GrammarCog(bot))
