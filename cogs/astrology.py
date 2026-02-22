"""
Astrology & Zodiac cog — Horoscopes and birth chart analysis.
"""

from __future__ import annotations

import logging
from datetime import datetime
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
        current_date = datetime.now().strftime("%B %d, %Y")  # e.g., "February 23, 2026"

        prompt = (
            f"Create a {period} horoscope for {sign.title()} ({emoji}) "
            f"for today, {current_date}.\n\n"
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

        # Split into two parts to avoid Discord's 4096 character limit
        prompt_part1 = (
            f"Create a detailed birth chart reading for someone born:\n"
            f"📅 Date: {date}\n"
            f"🕐 Time: {time} (24-hour format)\n"
            f"📍 Location: {location}\n\n"
            
            f"Provide PART 1 of the birth chart analysis (Core Placements):\n\n"
            
            f"1. **Sun Sign** — their core identity, ego, and life purpose\n"
            f"2. **Moon Sign** — emotional nature and inner world (estimate from date)\n"
            f"3. **Rising Sign** — how they appear to others (estimate from time and location)\n"
            f"4. **Mercury Placement** — communication style and thinking patterns\n"
            f"5. **Venus Placement** — love language and relationships\n"
            f"6. **Mars Placement** — drive, passion, and action style\n\n"
            
            f"Be detailed and insightful. Use accessible language while maintaining depth."
        )
        
        prompt_part2 = (
            f"Continue the birth chart reading for someone born on {date} at {time} in {location}.\n\n"
            
            f"Provide PART 2 of the birth chart analysis (Synthesis & Insights):\n\n"
            
            f"7. **Key Planetary Aspects** — important planetary relationships\n"
            f"8. **House Placements** — life areas affected (simplified)\n"
            f"9. **Personality Synthesis** — integrated personality overview from all placements\n"
            f"10. **Life Path & Potential** — strengths, challenges, and life purpose\n"
            f"11. **Compatibility** — which signs harmonize well with this birth chart\n"
            f"12. **Practical Insights** — actionable advice based on the chart\n\n"
            
            f"Note: This is an AI-generated estimate. For a precise chart, an exact birth time "
            f"and professional ephemeris data are needed."
        )

        try:
            # Generate Part 1
            resp1 = await self.bot.llm.simple_prompt(
                prompt_part1,
                system=(
                    "You are an experienced astrologer. Provide detailed, insightful readings. "
                    "Structure your response clearly with each section labeled."
                ),
                max_tokens=2048,
            )
            
            # Generate Part 2
            resp2 = await self.bot.llm.simple_prompt(
                prompt_part2,
                system=(
                    "You are an experienced astrologer. Provide detailed, insightful readings. "
                    "Structure your response clearly with each section labeled."
                ),
                max_tokens=2048,
            )

            # Send Part 1
            embed1 = Embedder.standard(
                "🌟 Birth Chart Reading — Part 1: Core Placements",
                resp1.content[:4000],  # Safety limit
                fields=[
                    ("Date", date, True),
                    ("Time", time, True),
                    ("Location", location, True),
                ],
            )
            await interaction.followup.send(embed=embed1)
            
            # Send Part 2
            embed2 = Embedder.standard(
                "🌟 Birth Chart Reading — Part 2: Synthesis & Insights",
                resp2.content[:4000],  # Safety limit
            )
            await interaction.followup.send(embed=embed2)
            
            # Log usage for both parts
            total_tokens = resp1.total_tokens + resp2.total_tokens
            avg_latency = (resp1.latency_ms + resp2.latency_ms) / 2
            
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="birth-chart",
                guild_id=interaction.guild_id,
                tokens_used=total_tokens,
                latency_ms=avg_latency,
            )

        except LLMClientError as exc:
            error_msg = str(exc)
            
            # Specific error mapping
            if "timeout" in error_msg.lower():
                user_msg = (
                    "⏱️ The request took too long. Birth charts are complex - please try again in a moment. "
                    "If the problem persists, make sure your birth information is complete."
                )
            elif "token" in error_msg.lower():
                user_msg = (
                    "📝 The response was too long for the current API configuration. "
                    "Try with a simpler location name or try again."
                )
            elif "rate" in error_msg.lower():
                user_msg = (
                    "⏸️ Too many requests to the API. Please wait a moment before trying again. "
                    "Birth charts use a lot of processing power!"
                )
            elif "invalid" in error_msg.lower() or "400" in error_msg.lower():
                user_msg = (
                    "❌ Invalid birth information provided. Make sure:\n"
                    "- Date is in YYYY-MM-DD format (e.g., 1990-01-15)\n"
                    "- Time is in HH:MM format (e.g., 14:30)\n"
                    "- Location is a valid city name"
                )
            else:
                user_msg = (
                    f"🌙 Birth chart generation encountered an issue: {error_msg}\n\n"
                    "Please try again with complete information."
                )
            
            await interaction.followup.send(
                embed=Embedder.error("Birth Chart Error", user_msg)
            )
        except Exception as exc:
            logger.error("Unexpected birth chart error: %s", exc, exc_info=True)
            error_details = f"{type(exc).__name__}: {str(exc)}"
            await interaction.followup.send(
                embed=Embedder.error(
                    "Birth Chart Error",
                    f"🌙 An unexpected error occurred:\n```\n{error_details[:1000]}\n```\n\n"
                    "Please try again with valid birth information (YYYY-MM-DD, HH:MM format)."
                )
            )


async def setup(bot: StarzaiBot) -> None:
    await bot.add_cog(AstrologyCog(bot))
