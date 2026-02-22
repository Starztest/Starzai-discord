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

        # Split into three parts + bonus typology for maximum detail
        prompt_part1 = (
            f"Create a detailed birth chart reading for someone born:\n"
            f"📅 Date: {date}\n"
            f"🕐 Time: {time} (24-hour format)\n"
            f"📍 Location: {location}\n\n"
            
            f"Provide PART 1 of the birth chart analysis (Core Identity):\n\n"
            
            f"1. **Sun Sign** — their core identity, ego, and life purpose (be very detailed)\n"
            f"2. **Moon Sign** — emotional nature, inner world, and subconscious patterns (estimate from date, be thorough)\n"
            f"3. **Rising Sign (Ascendant)** — how they appear to others, first impressions, and outer personality (estimate from time and location)\n"
            f"4. **Chart Ruler** — the planet ruling their rising sign and its significance\n\n"
            
            f"Be extremely detailed and insightful. Provide deep analysis for each placement."
        )
        
        prompt_part2 = (
            f"Continue the birth chart reading for someone born on {date} at {time} in {location}.\n\n"
            
            f"Provide PART 2 of the birth chart analysis (Personal Planets & Communication):\n\n"
            
            f"5. **Mercury Placement** — communication style, thinking patterns, learning style, and mental processes (be very detailed)\n"
            f"6. **Venus Placement** — love language, relationships, values, aesthetics, and what brings pleasure (be thorough)\n"
            f"7. **Mars Placement** — drive, passion, action style, anger expression, and sexual energy (be comprehensive)\n"
            f"8. **Key Planetary Aspects** — important relationships between planets and how they interact\n\n"
            
            f"Provide deep, insightful analysis for each placement and aspect."
        )
        
        prompt_part3 = (
            f"Continue the birth chart reading for someone born on {date} at {time} in {location}.\n\n"
            
            f"Provide PART 3 of the birth chart analysis (Life Path & Integration):\n\n"
            
            f"9. **House Placements** — which life areas are most emphasized (career, relationships, home, etc.)\n"
            f"10. **Personality Synthesis** — integrated personality overview combining all placements\n"
            f"11. **Life Path & Soul Purpose** — strengths, challenges, karmic lessons, and life mission\n"
            f"12. **Compatibility** — which signs and elements harmonize well with this birth chart\n"
            f"13. **Practical Insights** — actionable advice and guidance based on the chart\n\n"
            
            f"Note: This is an AI-generated estimate. For a precise chart, exact birth time and professional ephemeris data are needed."
        )
        
        prompt_typology = (
            f"Based on the birth chart for someone born on {date} at {time} in {location}, "
            f"provide a BONUS personality typology analysis:\n\n"
            
            f"**🧠 Personality Typology Predictions (Based on Astrological Chart)**\n\n"
            
            f"1. **MBTI Type** — Most likely Myers-Briggs type based on Sun, Moon, Mercury, and Rising signs. "
            f"Explain the reasoning (e.g., Fire Sun = Extroversion, Water Moon = Feeling, etc.)\n\n"
            
            f"2. **Enneagram Type** — Most likely Enneagram type and wing based on core motivations shown in the chart. "
            f"Explain which planetary placements suggest this type.\n\n"
            
            f"3. **Big Five Traits** — Estimate their Big Five personality scores:\n"
            f"   - Openness (1-10)\n"
            f"   - Conscientiousness (1-10)\n"
            f"   - Extraversion (1-10)\n"
            f"   - Agreeableness (1-10)\n"
            f"   - Neuroticism (1-10)\n"
            f"   Explain how the astrological placements suggest these scores.\n\n"
            
            f"4. **Integration** — How these typologies align with the astrological profile.\n\n"
            
            f"Be specific and explain your reasoning. This is a fun, insightful bonus analysis!"
        )

        try:
            # Generate Part 1 - Core Identity
            resp1 = await self.bot.llm.simple_prompt(
                prompt_part1,
                system=(
                    "You are an experienced astrologer. Provide extremely detailed, insightful readings. "
                    "Structure your response clearly with each section labeled. Be thorough and comprehensive."
                ),
                max_tokens=2048,
            )
            
            # Generate Part 2 - Personal Planets
            resp2 = await self.bot.llm.simple_prompt(
                prompt_part2,
                system=(
                    "You are an experienced astrologer. Provide extremely detailed, insightful readings. "
                    "Structure your response clearly with each section labeled. Be thorough and comprehensive."
                ),
                max_tokens=2048,
            )
            
            # Generate Part 3 - Life Path & Integration
            resp3 = await self.bot.llm.simple_prompt(
                prompt_part3,
                system=(
                    "You are an experienced astrologer. Provide extremely detailed, insightful readings. "
                    "Structure your response clearly with each section labeled. Be thorough and comprehensive."
                ),
                max_tokens=2048,
            )
            
            # Generate Bonus - Personality Typology
            resp4 = await self.bot.llm.simple_prompt(
                prompt_typology,
                system=(
                    "You are an expert in both astrology and personality psychology. "
                    "Provide insightful connections between astrological placements and personality typologies. "
                    "Be specific and explain your reasoning clearly."
                ),
                max_tokens=2048,
            )

            # Send Part 1
            embed1 = Embedder.standard(
                "🌟 Birth Chart Reading — Part 1: Core Identity",
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
                "🌟 Birth Chart Reading — Part 2: Personal Planets & Communication",
                resp2.content[:4000],  # Safety limit
            )
            await interaction.followup.send(embed=embed2)
            
            # Send Part 3
            embed3 = Embedder.standard(
                "🌟 Birth Chart Reading — Part 3: Life Path & Integration",
                resp3.content[:4000],  # Safety limit
            )
            await interaction.followup.send(embed=embed3)
            
            # Send Bonus Typology
            embed4 = discord.Embed(
                title="🎁 BONUS: Personality Typology Analysis",
                description=resp4.content[:4000],  # Safety limit
                color=discord.Color.purple(),  # Purple for bonus content
            )
            embed4.set_footer(text="Based on astrological chart analysis • For entertainment and insight")
            await interaction.followup.send(embed=embed4)
            
            # Log usage for all four parts
            total_tokens = resp1.total_tokens + resp2.total_tokens + resp3.total_tokens + resp4.total_tokens
            avg_latency = (resp1.latency_ms + resp2.latency_ms + resp3.latency_ms + resp4.latency_ms) / 4
            
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
