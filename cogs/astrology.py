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

        # Generate comprehensive birth chart in one go, then split intelligently
        full_prompt = (
            f"Create an EXTREMELY DETAILED and comprehensive birth chart reading for someone born:\n"
            f"📅 Date: {date}\n"
            f"🕐 Time: {time} (24-hour format)\n"
            f"📍 Location: {location}\n\n"
            
            f"Provide a complete, in-depth birth chart analysis covering ALL of the following sections. "
            f"Be as detailed and thorough as possible for each section:\n\n"
            
            f"**PART 1: CORE IDENTITY**\n"
            f"1. **Sun Sign** — Core identity, ego, life purpose, strengths, shadow side (be extremely detailed)\n"
            f"2. **Moon Sign** — Emotional nature, inner world, subconscious patterns, needs, childhood (very thorough)\n"
            f"3. **Rising Sign (Ascendant)** — Outer personality, first impressions, life approach, physical appearance tendencies\n"
            f"4. **Chart Ruler** — The planet ruling the rising sign and its profound significance\n\n"
            
            f"**PART 2: PERSONAL PLANETS**\n"
            f"5. **Mercury Placement** — Communication style, thinking patterns, learning style, mental processes, decision-making\n"
            f"6. **Venus Placement** — Love language, relationships, values, aesthetics, pleasure, money attitudes\n"
            f"7. **Mars Placement** — Drive, passion, action style, anger expression, sexual energy, ambition\n"
            f"8. **Jupiter Placement** — Growth, expansion, luck, philosophy, optimism, where they thrive\n"
            f"9. **Saturn Placement** — Discipline, responsibility, challenges, life lessons, karmic themes\n\n"
            
            f"**PART 3: OUTER PLANETS & ASPECTS**\n"
            f"10. **Uranus, Neptune, Pluto** — Generational influences and personal manifestations\n"
            f"11. **Major Planetary Aspects** — Conjunctions, oppositions, trines, squares, and their meanings\n"
            f"12. **Dominant Elements** — Fire, Earth, Air, Water balance and what it means\n"
            f"13. **Dominant Modalities** — Cardinal, Fixed, Mutable balance and implications\n\n"
            
            f"**PART 4: HOUSES & LIFE AREAS**\n"
            f"14. **House Placements** — Which life areas are emphasized (career, relationships, home, spirituality, etc.)\n"
            f"15. **Angular Houses** — 1st, 4th, 7th, 10th house emphasis and significance\n"
            f"16. **Nodal Axis** — North Node and South Node (karmic path and past life themes)\n\n"
            
            f"**PART 5: SYNTHESIS & INTEGRATION**\n"
            f"17. **Personality Synthesis** — Integrated personality overview combining all placements\n"
            f"18. **Life Path & Soul Purpose** — Strengths, challenges, karmic lessons, life mission\n"
            f"19. **Compatibility** — Which signs, elements, and chart types harmonize well\n"
            f"20. **Practical Insights** — Actionable advice and guidance for personal growth\n\n"
            
            f"**BONUS: PERSONALITY TYPOLOGY ANALYSIS**\n"
            f"21. **MBTI Type** — Most likely Myers-Briggs type with detailed reasoning (I/E, N/S, T/F, J/P)\n"
            f"22. **Enneagram Complete Analysis**:\n"
            f"    - Core Type (1-9) with detailed explanation\n"
            f"    - Wing (e.g., 4w5 or 4w3)\n"
            f"    - Tritype (e.g., 459, 468, etc.)\n"
            f"    - Instinctual Variant (Self-Preservation, Social, Sexual/One-to-One)\n"
            f"    - Integration and Disintegration arrows\n"
            f"    - How the chart supports this Enneagram profile\n"
            f"23. **Big Five Personality Traits** with scores (1-10) and explanations:\n"
            f"    - Openness to Experience\n"
            f"    - Conscientiousness\n"
            f"    - Extraversion\n"
            f"    - Agreeableness\n"
            f"    - Neuroticism (Emotional Stability)\n"
            f"24. **Typology Integration** — How MBTI, Enneagram, and Big Five align with the astrological profile\n\n"
            
            f"Note: This is an AI-generated estimate based on astrological principles. "
            f"For a precise chart, exact birth time and professional ephemeris data are needed.\n\n"
            
            f"BE EXTREMELY DETAILED AND COMPREHENSIVE. This should be a complete, professional-level birth chart reading."
        )

        try:
            # Generate the complete birth chart in one comprehensive call
            resp = await self.bot.llm.simple_prompt(
                full_prompt,
                system=(
                    "You are a master astrologer with deep expertise in natal chart analysis and personality psychology. "
                    "Provide an extremely detailed, comprehensive, professional-level birth chart reading. "
                    "Structure your response clearly with each section labeled and numbered. "
                    "Be thorough, insightful, and specific. This should be the most complete reading possible."
                ),
                max_tokens=8192,  # Maximum tokens for full comprehensive analysis
            )
            
            # Smart chunking: Split the response into multiple embeds
            full_content = resp.content
            chunk_size = 3900  # Safe limit below Discord's 4096
            
            # Split content intelligently by sections (look for **PART markers)
            chunks = []
            current_chunk = ""
            
            lines = full_content.split('\n')
            for line in lines:
                # Check if adding this line would exceed the limit
                if len(current_chunk) + len(line) + 1 > chunk_size:
                    # Save current chunk and start a new one
                    if current_chunk:
                        chunks.append(current_chunk.strip())
                    current_chunk = line + '\n'
                else:
                    current_chunk += line + '\n'
            
            # Add the last chunk
            if current_chunk:
                chunks.append(current_chunk.strip())
            
            # Send all chunks as separate embeds
            for i, chunk in enumerate(chunks, 1):
                if i == 1:
                    # First embed includes birth info
                    embed = Embedder.standard(
                        f"🌟 Birth Chart Reading — Part {i}/{len(chunks)}",
                        chunk,
                        fields=[
                            ("Date", date, True),
                            ("Time", time, True),
                            ("Location", location, True),
                        ],
                    )
                else:
                    # Subsequent embeds
                    # Check if this is the typology section (contains MBTI or Enneagram)
                    is_typology = "MBTI" in chunk or "Enneagram" in chunk or "Big Five" in chunk
                    
                    if is_typology:
                        embed = discord.Embed(
                            title=f"🎁 Birth Chart Reading — Part {i}/{len(chunks)} (Personality Typology)",
                            description=chunk,
                            color=discord.Color.purple(),
                        )
                        embed.set_footer(text="Based on astrological chart • For entertainment and insight")
                    else:
                        embed = Embedder.standard(
                            f"🌟 Birth Chart Reading — Part {i}/{len(chunks)}",
                            chunk,
                        )
                
                await interaction.followup.send(embed=embed)
            
            # Log usage
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="birth-chart",
                guild_id=interaction.guild_id,
                tokens_used=resp.total_tokens,
                latency_ms=resp.latency_ms,
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
