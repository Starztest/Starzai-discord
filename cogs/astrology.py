"""
Astrology & Zodiac cog — Horoscopes and birth chart analysis.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from config.constants import ZODIAC_EMOJIS, ZODIAC_SIGNS
from utils.embedder import Embedder
from utils.llm_client import LLMClientError

if TYPE_CHECKING:
    from bot import StarzaiBot

logger = logging.getLogger(__name__)

PERIODS = ["daily", "weekly", "monthly"]


class AstrologyCog(commands.Cog, name="Astrology"):
    """Personalized astrological insights and zodiac readings."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot

    # ── /horoscope ───────────────────────────────────────────────────

    @app_commands.command(
        name="horoscope", description="Get your personalized horoscope"
    )
    @app_commands.describe(
        sign="Your zodiac sign",
        period="Time period for the reading",
    )
    @app_commands.choices(
        sign=[
            app_commands.Choice(
                name=f"{ZODIAC_EMOJIS.get(s, '')} {s.title()}", value=s
            )
            for s in ZODIAC_SIGNS
        ],
        period=[
            app_commands.Choice(name=p.title(), value=p) for p in PERIODS
        ],
    )
    async def horoscope_cmd(
        self,
        interaction: discord.Interaction,
        sign: str,
        period: str = "daily",
    ) -> None:
        result = self.bot.rate_limiter.check(interaction.user.id, interaction.guild_id)
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return

        await interaction.response.defer()

        emoji = ZODIAC_EMOJIS.get(sign.lower(), "⭐")

        prompt = (
            f"Create a {period} horoscope for {sign.title()} ({emoji}).\n\n"
            "Include:\n"
            "1. **General Overview** — the overall energy and theme\n"
            "2. **Love & Relationships** — romantic and social insights\n"
            "3. **Career & Finance** — professional and financial guidance\n"
            "4. **Health & Wellness** — physical and mental well-being tips\n"
            "5. **Lucky Elements** — lucky number, color, and day\n"
            "6. **Affirmation** — a positive affirmation for the period\n\n"
            "Make it feel personal, insightful, and encouraging. Use mystical but accessible language."
        )

        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system="You are a wise and insightful astrologer. Create personalized, engaging horoscopes.",
            )

            embed = Embedder.standard(
                f"{emoji} {sign.title()} — {period.title()} Horoscope",
                resp.content,
            )
            await interaction.followup.send(embed=embed)
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="horoscope",
                guild_id=interaction.guild_id,
                tokens_used=resp.total_tokens,
                latency_ms=resp.latency_ms,
            )

        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Horoscope Error", str(exc))
            )

    # ── /birth-chart ─────────────────────────────────────────────────

    @app_commands.command(
        name="birth-chart",
        description="Get a personalized birth chart reading",
    )
    @app_commands.describe(
        date="Birth date (YYYY-MM-DD)",
        time="Birth time (HH:MM, 24h format)",
        location="Birth location (city name)",
    )
    async def birth_chart_cmd(
        self,
        interaction: discord.Interaction,
        date: str,
        time: str = "12:00",
        location: str = "Unknown",
    ) -> None:
        result = self.bot.rate_limiter.check(interaction.user.id, interaction.guild_id)
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return

        await interaction.response.defer()

        prompt = (
            f"Create a birth chart reading for someone born on {date} at {time} in {location}.\n\n"
            "Include:\n"
            "1. **Sun Sign** — their core identity and ego\n"
            "2. **Moon Sign** — their emotional nature (estimate based on date)\n"
            "3. **Rising Sign** — how they appear to others (estimate based on time)\n"
            "4. **Key Planetary Placements** — significant planet positions\n"
            "5. **Personality Profile** — a synthesized personality overview\n"
            "6. **Life Path Insights** — potential strengths and challenges\n"
            "7. **Compatibility** — which signs they tend to harmonize with\n\n"
            "Note: This is an AI-generated estimate. For a precise chart, "
            "an exact birth time and ephemeris are needed.\n"
            "Make the reading feel personal and insightful."
        )

        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system="You are an experienced astrologer creating detailed, personalized birth chart readings.",
            )

            embed = Embedder.standard(
                "🌟 Birth Chart Reading",
                resp.content,
                fields=[
                    ("Date", date, True),
                    ("Time", time, True),
                    ("Location", location, True),
                ],
            )
            await interaction.followup.send(embed=embed)
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="birth-chart",
                guild_id=interaction.guild_id,
                tokens_used=resp.total_tokens,
                latency_ms=resp.latency_ms,
            )

        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Birth Chart Error", str(exc))
            )


async def setup(bot: StarzaiBot) -> None:
    await bot.add_cog(AstrologyCog(bot))

