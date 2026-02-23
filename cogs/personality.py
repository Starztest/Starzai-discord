"""
Human Calculator / Personality Analysis cog — Analyze personality from text.
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


class PersonalityCog(commands.Cog, name="Personality"):
    """Personality and behavioral analysis from text samples."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot

    # ── /analyze-personality ─────────────────────────────────────────

    @app_commands.command(
        name="analyze-personality",
        description="Analyze personality traits from a text sample",
    )
    @app_commands.describe(
        text="A text sample to analyze (the more text, the better the analysis)"
    )
    async def analyze_personality_cmd(
        self, interaction: discord.Interaction, text: str
    ) -> None:
        result = self.bot.rate_limiter.check(
            interaction.user.id, interaction.guild_id, expensive=True
        )
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return

        if len(text) < 50:
            await interaction.response.send_message(
                embed=Embedder.warning(
                    "Text Too Short",
                    "Please provide at least 50 characters for a meaningful analysis.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        prompt = (
            "Analyze the personality and behavioral traits reflected in the following text.\n\n"
            f"Text: \"{text}\"\n\n"
            "Provide a detailed analysis including:\n"
            "1. **Big Five Personality Traits** (rate each 1-10):\n"
            "   - Openness to Experience\n"
            "   - Conscientiousness\n"
            "   - Extraversion\n"
            "   - Agreeableness\n"
            "   - Emotional Stability\n\n"
            "2. **Communication Style** — how they express themselves\n"
            "3. **Emotional Tone** — dominant emotions detected\n"
            "4. **Thinking Pattern** — analytical vs. intuitive, detail vs. big-picture\n"
            "5. **Key Strengths** — 3 personality strengths evident\n"
            "6. **Growth Areas** — 2 areas for personal development\n"
            "7. **Overall Profile** — a brief, engaging personality summary\n\n"
            "Note: This is an AI analysis for entertainment. It should not be used as a psychological assessment."
        )

        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system=(
                    "You are an expert behavioral psychologist and personality analyst. "
                    "Provide insightful, balanced personality assessments based on text analysis. "
                    "Always include a disclaimer that this is for entertainment purposes."
                ),
            )

            embed = Embedder.standard(
                "🧠 Personality Analysis",
                resp.content,
                fields=[
                    ("Text Analyzed", text[:500] + ("…" if len(text) > 500 else ""), False),
                ],
            )
            await interaction.followup.send(embed=embed)
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="analyze-personality",
                guild_id=interaction.guild_id,
                tokens_used=resp.total_tokens,
                latency_ms=resp.latency_ms,
            )

        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Analysis Failed", str(exc))
            )


async def setup(bot: StarzaiBot) -> None:
    await bot.add_cog(PersonalityCog(bot))

