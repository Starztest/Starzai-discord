"""
Astrology & Zodiac cog — Horoscopes and birth chart analysis.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, List

import discord
from discord import app_commands
from discord.ext import commands

from config.constants import ZODIAC_EMOJIS, ZODIAC_SIGNS
from utils.embedder import Embedder
from utils.llm_client import LLMClientError

# Optional PDF generation (gracefully handle if reportlab not installed)
try:
    from utils.pdf_generator import create_transit_pdf, create_compatibility_pdf
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False
    create_transit_pdf = None
    create_compatibility_pdf = None

# Real astronomical calculations
try:
    from utils.astro_calculator import AstroCalculator, ASTRO_AVAILABLE
except ImportError:
    ASTRO_AVAILABLE = False
    AstroCalculator = None

if TYPE_CHECKING:
    from bot import StarzaiBot


class BirthChartPaginationView(discord.ui.View):
    """Interactive pagination view for birth chart readings."""
    
    def __init__(
        self,
        pages: List[str],
        date: str,
        time: str,
        location: str,
        user_id: int,
    ):
        super().__init__(timeout=600)  # 10 minute timeout
        self.pages = pages
        self.date = date
        self.time = time
        self.location = location
        self.user_id = user_id
        self.current_page = 0
        
        # Update button states
        self._update_buttons()
    
    def _update_buttons(self):
        """Update button states based on current page."""
        # Disable previous button on first page
        self.previous_button.disabled = self.current_page == 0
        # Disable next button on last page
        self.next_button.disabled = self.current_page == len(self.pages) - 1
    
    def _create_embed(self) -> discord.Embed:
        """Create embed for current page."""
        page_content = self.pages[self.current_page]
        
        # Check if this is the typology section
        is_typology = "MBTI" in page_content or "Enneagram" in page_content or "Big Five" in page_content
        
        if is_typology:
            embed = discord.Embed(
                title=f"🎁 Birth Chart Reading — Page {self.current_page + 1}/{len(self.pages)} (Personality Typology)",
                description=page_content,
                color=discord.Color.purple(),
            )
            embed.set_footer(text="Based on astrological chart • For entertainment and insight")
        else:
            if self.current_page == 0:
                # First page includes birth info
                embed = discord.Embed(
                    title=f"🌟 Birth Chart Reading — Page {self.current_page + 1}/{len(self.pages)}",
                    description=page_content,
                    color=discord.Color.blue(),
                )
                embed.add_field(name="Date", value=self.date, inline=True)
                embed.add_field(name="Time", value=self.time, inline=True)
                embed.add_field(name="Location", value=self.location, inline=True)
            else:
                embed = discord.Embed(
                    title=f"🌟 Birth Chart Reading — Page {self.current_page + 1}/{len(self.pages)}",
                    description=page_content,
                    color=discord.Color.blue(),
                )
        
        embed.set_footer(text=f"Page {self.current_page + 1}/{len(self.pages)} • Use buttons to navigate • Download full report below")
        return embed
    
    @discord.ui.button(label="◀️ Previous", style=discord.ButtonStyle.secondary, custom_id="previous")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to previous page."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "These buttons aren't for you! Use `/birth-chart` to get your own reading.",
                ephemeral=True,
            )
            return
        
        self.current_page = max(0, self.current_page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._create_embed(), view=self)
    
    @discord.ui.button(label="Next ▶️", style=discord.ButtonStyle.primary, custom_id="next")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to next page."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "These buttons aren't for you! Use `/birth-chart` to get your own reading.",
                ephemeral=True,
            )
            return
        
        self.current_page = min(len(self.pages) - 1, self.current_page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._create_embed(), view=self)

logger = logging.getLogger(__name__)

PERIODS = ["daily", "weekly", "monthly"]


class AstrologyCog(commands.Cog, name="Astrology"):
    """Personalized astrological insights and zodiac readings."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot
        self.astro_calc = (
            AstroCalculator() if ASTRO_AVAILABLE and AstroCalculator is not None else None
        )
        if self.astro_calc:
            logger.info("AstroCalculator enabled (Swiss Ephemeris)")
        else:
            logger.info("AstroCalculator disabled (dependencies not available)")

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

        # Calculate real birth chart if available
        logger.info(f"🔵 Starting birth chart calculation...")
        chart_data = None
        if self.astro_calc and ASTRO_AVAILABLE:
            logger.info(f"🔵 AstroCalculator available, calculating...")
            try:
                chart_data = await self.astro_calc.calculate_birth_chart(date, time, location)
                logger.info(f"🔵 Calculation complete, chart_data: {chart_data is not None}")
                if chart_data:
                    logger.info(f"Calculated real birth chart for {date} {time} {location}")
                else:
                    logger.warning(f"Could not calculate chart - likely geocoding failed for '{location}'")
            except asyncio.TimeoutError:
                logger.error(f"Timeout calculating birth chart for {location}")
            except Exception as e:
                logger.error(f"Birth chart calculation error: {e}", exc_info=True)
        
        # Build prompt with real data if available
        if chart_data:
            # Use REAL astronomical data
            chart_text = self.astro_calc.format_chart_data(chart_data)
            full_prompt = (
                f"Interpret this REAL birth chart for someone born:\n"
                f"Date: {date}\n"
                f"Time: {time}\n"
                f"Location: {location}\n\n"
                f"**ACTUAL ASTRONOMICAL DATA:**\n"
                f"{chart_text}\n\n"
                f"Provide an in-depth interpretation covering:\n\n"
                f"**CORE IDENTITY**\n"
                f"1. Sun Sign — Core identity, ego, life purpose, strengths, shadow side\n"
                f"2. Moon Sign — Emotional nature, inner world, subconscious patterns, needs\n"
                f"3. Rising Sign — Outer personality, first impressions, life approach\n"
                f"4. Chart Ruler — Ruling planet and its profound significance\n\n"
                f"**PERSONAL PLANETS**\n"
                f"5. Mercury — Communication style, thinking patterns, learning style\n"
                f"6. Venus — Love language, relationships, values, aesthetics\n"
                f"7. Mars — Drive, passion, action style, ambition\n"
                f"8. Jupiter — Growth, expansion, luck, philosophy\n"
                f"9. Saturn — Discipline, responsibility, challenges, life lessons\n\n"
                f"**ASPECTS & ELEMENTS**\n"
                f"10. Major Planetary Aspects — Interpret the aspects listed above\n"
                f"11. Dominant Elements — Fire/Earth/Air/Water balance and meaning\n"
                f"12. House Placements — Life areas emphasized based on house positions\n\n"
                f"**LIFE PATH**\n"
                f"13. Personality Synthesis — Integrated overview combining all placements\n"
                f"14. Life Purpose and Strengths — Karmic lessons, life mission\n"
                f"15. Compatibility — Which signs and elements harmonize well\n\n"
                f"**PERSONALITY TYPOLOGY**\n"
                f"16. MBTI Type — Most likely type with detailed reasoning (I/E, N/S, T/F, J/P)\n"
                f"17. Enneagram — Core Type (1-9), Wing, Tritype, Instinctual Variant (SP/SO/SX)\n"
                f"18. Big Five Traits — Openness, Conscientiousness, Extraversion, Agreeableness, Neuroticism (scores 1-10)\n\n"
                f"IMPORTANT: Use the EXACT planetary positions and aspects provided above. Do not make up different positions."
            )
        else:
            # Fallback to AI-only mode
            full_prompt = (
                f"Create a detailed birth chart reading for someone born:\n"
                f"Date: {date}\n"
                f"Time: {time}\n"
                f"Location: {location}\n\n"
                f"Provide an in-depth analysis covering all sections as usual.\n"
                f"Note: Real astronomical calculations unavailable, provide general interpretation."
            )

        try:
            # Generate the complete birth chart interpretation
            resp = await self.bot.llm.simple_prompt(
                full_prompt,
                system=(
                    "You are a master astrologer with deep expertise in natal chart analysis and personality psychology. "
                    "When provided with REAL astronomical data, interpret those EXACT positions accurately. "
                    "Do not make up different planetary positions - use only what is provided. "
                    "Provide a detailed, comprehensive birth chart reading. "
                    "Structure your response clearly with each section labeled and numbered. "
                    "Be thorough, insightful, and specific."
                ),
                max_tokens=5000,
            )
            
            # Smart chunking: Split the response into max 5 pages
            full_content = resp.content
            chunk_size = 3900  # Safe limit below Discord's 4096
            max_pages = 5  # Maximum number of pages
            
            # Split content intelligently by sections
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
            
            # Limit to max 7 pages (merge if needed)
            if len(chunks) > max_pages:
                # Merge chunks to fit within max_pages
                merged_chunks = []
                chunks_per_page = len(chunks) // max_pages + 1
                for i in range(0, len(chunks), chunks_per_page):
                    merged = '\n\n'.join(chunks[i:i+chunks_per_page])
                    merged_chunks.append(merged[:3900])  # Safety limit
                chunks = merged_chunks[:max_pages]
            
            # Create and send a downloadable .txt file with the complete report
            # Strip Markdown formatting for clean plain text
            import re
            
            def strip_markdown(text: str) -> str:
                """Remove Discord/Markdown formatting from text."""
                # Remove bold (**text** or __text__)
                text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
                text = re.sub(r'__(.+?)__', r'\1', text)
                # Remove italic (*text* or _text_)
                text = re.sub(r'\*(.+?)\*', r'\1', text)
                text = re.sub(r'_(.+?)_', r'\1', text)
                # Remove strikethrough (~~text~~)
                text = re.sub(r'~~(.+?)~~', r'\1', text)
                # Remove inline code (`text`)
                text = re.sub(r'`(.+?)`', r'\1', text)
                # Remove code blocks (```text```)
                text = re.sub(r'```(.+?)```', r'\1', text, flags=re.DOTALL)
                return text
            
            clean_content = strip_markdown(full_content)
            
            report_header = (
                f"═══════════════════════════════════════════════════════════\n"
                f"           COMPLETE BIRTH CHART READING REPORT\n"
                f"═══════════════════════════════════════════════════════════\n\n"
                f"Birth Information:\n"
                f"  Date: {date}\n"
                f"  Time: {time}\n"
                f"  Location: {location}\n\n"
                f"Generated by Starzai Discord Bot\n"
                f"═══════════════════════════════════════════════════════════\n\n"
            )
            
            report_footer = (
                f"\n\n═══════════════════════════════════════════════════════════\n"
                f"Note: This is an AI-generated astrological analysis based on\n"
                f"the provided birth information. For a precise professional\n"
                f"chart, exact birth time and ephemeris data are recommended.\n"
                f"═══════════════════════════════════════════════════════════\n"
            )
            
            full_report = report_header + clean_content + report_footer
            
            # Create a temporary file
            import tempfile
            import os
            
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
                f.write(full_report)
                temp_path = f.name
            
            try:
                # Create pagination view
                view = BirthChartPaginationView(
                    pages=chunks,
                    date=date,
                    time=time,
                    location=location,
                    user_id=interaction.user.id,
                )
                
                # Send the file with the first page embed and pagination buttons
                file = discord.File(temp_path, filename=f"birth_chart_{date.replace('-', '')}_{interaction.user.name}.txt")
                await interaction.followup.send(
                    content="📄 **Complete Birth Chart Report** — Use buttons to navigate pages, download full report below!",
                    embed=view._create_embed(),
                    view=view,
                    file=file
                )
            finally:
                # Clean up the temporary file
                os.unlink(temp_path)
            
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


    @app_commands.command(name="transits", description="Get current planetary transits for your birth chart")
    @app_commands.describe(
        date="Your birth date (YYYY-MM-DD, e.g., 1990-01-15)",
        time="Your birth time (HH:MM, e.g., 14:30)",
        location="Your birth location (city name)",
        period="Time period for transit forecast (daily, weekly, or monthly)"
    )
    @app_commands.choices(period=[
        app_commands.Choice(name="Daily (Today)", value="daily"),
        app_commands.Choice(name="Weekly (This Week)", value="weekly"),
        app_commands.Choice(name="Monthly (This Month)", value="monthly"),
    ])
    async def transits(
        self,
        interaction: discord.Interaction,
        date: str,
        time: str,
        location: str,
        period: str = "weekly"
    ):
        """Analyze current planetary transits affecting your birth chart."""
        await interaction.response.defer()
        
        from datetime import datetime as dt
        current_date = dt.now().strftime("%Y-%m-%d")
        
        period_labels = {
            "daily": "Today",
            "weekly": "This Week",
            "monthly": "This Month"
        }
        
        # Calculate real natal chart and transits if available
        natal_chart = None
        transit_data = None
        if self.astro_calc and ASTRO_AVAILABLE:
            try:
                natal_chart = await self.astro_calc.calculate_birth_chart(date, time, location)
                if natal_chart:
                    transit_data = self.astro_calc.calculate_transits(natal_chart, current_date)
                    logger.info(f"Calculated real transits for {date}")
            except Exception as e:
                logger.error(f"Transit calculation error: {e}")
        
        # Build prompt with real data if available
        if natal_chart and transit_data:
            # Format transit positions
            transit_text = "\n".join([f"{p.name}: {p.degree:.1f}° {p.sign}" + (" ℞" if p.retrograde else "") 
                                      for p in transit_data.values()])
            
            prompt = (
                f"Analyze the current planetary transits for someone born:\n"
                f"Birth Date: {date}\n"
                f"Birth Time: {time}\n"
                f"Birth Location: {location}\n\n"
                f"Current Date: {current_date}\n"
                f"Forecast Period: {period_labels[period]}\n\n"
                f"**ACTUAL TRANSITING POSITIONS:**\n"
                f"{transit_text}\n\n"
                f"**NATAL CHART (for reference):**\n"
                f"{self.astro_calc.format_chart_data(natal_chart)}\n\n"
                f"Provide a detailed transit forecast covering:\n\n"
                f"**CURRENT TRANSITS**\n"
                f"1. Major Planetary Transits — Interpret the transiting positions above\n"
                f"2. Most Significant Aspects — Key transit-to-natal aspects\n"
                f"3. Transit Themes — Overall energy and themes for this period\n\n"
                f"**LIFE AREAS ACTIVATED**\n"
                f"4. Career & Ambition — Professional opportunities and challenges\n"
                f"5. Relationships & Love — Romantic and social dynamics\n"
                f"6. Personal Growth — Inner development and spiritual themes\n"
                f"7. Health & Vitality — Physical and emotional well-being\n\n"
                f"**TIMING & GUIDANCE**\n"
                f"8. Best Days — Most favorable days for important activities\n"
                f"9. Challenging Days — Days requiring extra caution or patience\n"
                f"10. Opportunities — What to focus on and take advantage of\n"
                f"11. Warnings — What to avoid or be mindful of\n\n"
                f"**PRACTICAL ADVICE**\n"
                f"12. Action Steps — Specific recommendations for this period\n"
                f"13. Affirmations — Supportive mantras aligned with current energy\n\n"
                f"IMPORTANT: Use the EXACT transiting positions provided above."
            )
        else:
            # Fallback to AI-only mode
            prompt = (
                f"Analyze transits for someone born {date} at {time} in {location}.\n"
                f"Current Date: {current_date}\n"
                f"Period: {period_labels[period]}\n\n"
                f"Provide a general transit forecast. Note: Real calculations unavailable."
            )
        
        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system=(
                    "You are an expert astrologer specializing in transit analysis. "
                    "When provided with REAL transit positions, interpret those EXACT positions accurately. "
                    "Provide practical, insightful forecasts that help people navigate current energies. "
                    "Be specific about timing and actionable in your guidance."
                ),
                max_tokens=3000,
            )
            
            content = resp.content
            
            # Create embed
            embed = discord.Embed(
                title=f"🔮 Transit Forecast — {period_labels[period]}",
                description=content[:4000],  # Discord limit
                color=discord.Color.purple(),
            )
            embed.add_field(name="Birth Date", value=date, inline=True)
            embed.add_field(name="Birth Time", value=time, inline=True)
            embed.add_field(name="Location", value=location, inline=True)
            embed.set_footer(text=f"Current transits for {current_date} • AI-generated forecast")
            
            # Create beautiful downloadable files
            import tempfile
            import os
            
            # Create .txt file
            txt_content = (
                f"═══════════════════════════════════════════════════════════\n"
                f"           🔮 TRANSIT FORECAST — {period_labels[period].upper()}\n"
                f"═══════════════════════════════════════════════════════════\n\n"
                f"BIRTH INFORMATION\n"
                f"─────────────────\n"
                f"Date: {date}\n"
                f"Time: {time}\n"
                f"Location: {location}\n"
                f"Forecast Date: {current_date}\n\n"
                f"{content}\n\n"
                f"───────────────────────────────────────────────────────────\n"
                f"Generated by Starzai • AI-Powered Astrological Analysis\n"
                f"For entertainment and personal insight purposes\n"
            )
            
            # Save txt to temp file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as txt_file:
                txt_file.write(txt_content)
                txt_path = txt_file.name
            
            files_to_send = [discord.File(txt_path, filename=f"transit_forecast_{period}_{date}.txt")]
            temp_files = [txt_path]
            
            if PDF_AVAILABLE:
                pdf_bytes = create_transit_pdf(
                    content, date, time, location, current_date, period_labels[period]
                )
                with tempfile.NamedTemporaryFile(mode='wb', suffix='.pdf', delete=False) as pdf_file:
                    pdf_file.write(pdf_bytes)
                    pdf_path = pdf_file.name
                files_to_send.append(discord.File(pdf_path, filename=f"transit_forecast_{period}_{date}.pdf"))
                temp_files.append(pdf_path)
            
            try:
                # Send embed with files
                await interaction.followup.send(embed=embed, files=files_to_send)
            finally:
                # Clean up temp files
                for temp_file in temp_files:
                    os.unlink(temp_file)
            
        except LLMClientError as e:
            logger.error("Transit forecast LLM error: %s", e)
            await interaction.followup.send(
                embed=Embedder.error(
                    "Transit Forecast Error",
                    f"🔮 Could not generate transit forecast: {str(e)}\n\n"
                    "Please try again with valid birth information."
                )
            )
        except Exception as exc:
            logger.error("Unexpected transit error: %s", exc, exc_info=True)
            await interaction.followup.send(
                embed=Embedder.error(
                    "Transit Forecast Error",
                    f"🔮 An unexpected error occurred: {type(exc).__name__}\n\n"
                    "Please try again."
                )
            )
    
    @app_commands.command(name="compatibility", description="Analyze astrological compatibility between two people")
    @app_commands.describe(
        your_date="Your birth date (YYYY-MM-DD)",
        your_time="Your birth time (HH:MM)",
        your_location="Your birth location",
        partner_date="Partner's birth date (YYYY-MM-DD)",
        partner_time="Partner's birth time (HH:MM)",
        partner_location="Partner's birth location"
    )
    async def compatibility(
        self,
        interaction: discord.Interaction,
        your_date: str,
        your_time: str,
        your_location: str,
        partner_date: str,
        partner_time: str,
        partner_location: str
    ):
        """Analyze synastry and compatibility between two birth charts."""
        await interaction.response.defer()
        
        # Calculate real birth charts and synastry if available
        chart1 = None
        chart2 = None
        synastry_aspects = None
        
        if self.astro_calc and ASTRO_AVAILABLE:
            try:
                chart1 = await self.astro_calc.calculate_birth_chart(your_date, your_time, your_location)
                chart2 = await self.astro_calc.calculate_birth_chart(partner_date, partner_time, partner_location)
                
                if chart1 and chart2:
                    synastry_aspects = self.astro_calc.calculate_synastry(chart1, chart2)
                    logger.info(f"Calculated real synastry between {your_date} and {partner_date}")
            except Exception as e:
                logger.error(f"Synastry calculation error: {e}")
        
        # Build prompt with real data if available
        if chart1 and chart2 and synastry_aspects:
            # Format both charts
            chart1_text = self.astro_calc.format_chart_data(chart1)
            chart2_text = self.astro_calc.format_chart_data(chart2)
            
            # Format synastry aspects
            synastry_text = "\n".join([str(aspect) for aspect in synastry_aspects[:20]])  # Top 20 aspects
            
            prompt = (
                f"Perform a detailed synastry and compatibility analysis between:\n\n"
                f"**Person 1:**\n"
                f"Birth Date: {your_date}\n"
                f"Birth Time: {your_time}\n"
                f"Birth Location: {your_location}\n\n"
                f"**PERSON 1 CHART:**\n"
                f"{chart1_text}\n\n"
                f"**Person 2:**\n"
                f"Birth Date: {partner_date}\n"
                f"Birth Time: {partner_time}\n"
                f"Birth Location: {partner_location}\n\n"
                f"**PERSON 2 CHART:**\n"
                f"{chart2_text}\n\n"
                f"**ACTUAL SYNASTRY ASPECTS:**\n"
                f"{synastry_text}\n\n"
                f"Provide a comprehensive compatibility analysis covering:\n\n"
                f"**OVERALL COMPATIBILITY**\n"
                f"1. Compatibility Score — Overall rating (1-10) based on the aspects above\n"
                f"2. Relationship Dynamic — Core energy and interaction style\n"
                f"3. Soul Connection — Karmic ties and spiritual bond\n\n"
                f"**SYNASTRY ASPECTS**\n"
                f"4. Sun-Moon Connections — Interpret actual Sun-Moon aspects above\n"
                f"5. Venus-Mars Aspects — Interpret actual Venus-Mars aspects above\n"
                f"6. Mercury Connections — Interpret actual Mercury aspects above\n"
                f"7. Major Challenging Aspects — Interpret squares and oppositions above\n"
                f"8. Harmonious Aspects — Interpret trines and sextiles above\n\n"
                f"**RELATIONSHIP AREAS**\n"
                f"9. Communication — How you understand each other\n"
                f"10. Emotional Connection — Feelings and nurturing\n"
                f"11. Romance & Passion — Love language and attraction\n"
                f"12. Shared Values — What you both care about\n"
                f"13. Long-term Potential — Sustainability and growth\n\n"
                f"**STRENGTHS & CHALLENGES**\n"
                f"14. Relationship Strengths — What works naturally\n"
                f"15. Growth Areas — Where effort is needed\n"
                f"16. Advice for Harmony — How to nurture the connection\n\n"
                f"IMPORTANT: Use the EXACT synastry aspects provided above. Base your analysis on these real connections."
            )
        else:
            # Fallback to AI-only mode
            prompt = (
                f"Perform a general compatibility analysis between:\n"
                f"Person 1: {your_date} at {your_time} in {your_location}\n"
                f"Person 2: {partner_date} at {partner_time} in {partner_location}\n\n"
                f"Provide a comprehensive analysis. Note: Real calculations unavailable."
            )
        
        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system=(
                    "You are an expert relationship astrologer specializing in synastry analysis. "
                    "When provided with REAL synastry aspects, interpret those EXACT aspects accurately. "
                    "Provide balanced, honest assessments that highlight both strengths and challenges. "
                    "Be constructive and focus on how the relationship can thrive."
                ),
                max_tokens=4000,
            )
            
            content = resp.content
            
            # Split into multiple embeds if needed
            chunk_size = 3900
            embed_chunks = []
            current_chunk = ""
            
            lines = content.split('\n')
            for line in lines:
                if len(current_chunk) + len(line) + 1 > chunk_size:
                    if current_chunk:
                        embed_chunks.append(current_chunk.strip())
                    current_chunk = line + '\n'
                else:
                    current_chunk += line + '\n'
            
            if current_chunk:
                embed_chunks.append(current_chunk.strip())
            
            # Build embeds
            embeds = []
            for i, chunk in enumerate(embed_chunks, 1):
                if i == 1:
                    embed = discord.Embed(
                        title=f"💕 Compatibility Analysis — Part {i}/{len(embed_chunks)}",
                        description=chunk,
                        color=discord.Color.from_rgb(255, 105, 180),  # Hot pink
                    )
                    embed.add_field(name="Person 1", value=f"{your_date} • {your_location}", inline=True)
                    embed.add_field(name="Person 2", value=f"{partner_date} • {partner_location}", inline=True)
                else:
                    embed = discord.Embed(
                        title=f"💕 Compatibility Analysis — Part {i}/{len(embed_chunks)}",
                        description=chunk,
                        color=discord.Color.from_rgb(255, 105, 180),
                    )
                
                embed.set_footer(text="Synastry analysis • AI-generated compatibility reading")
                embeds.append(embed)
            
            # Create beautiful downloadable files
            import tempfile
            import os
            
            # Create .txt file
            txt_content = (
                f"♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥\n"
                f"           💕 COMPATIBILITY ANALYSIS 💕\n"
                f"♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥♥\n\n"
                f"PERSON 1\n"
                f"────────\n"
                f"Birth Date: {your_date}\n"
                f"Birth Time: {your_time}\n"
                f"Birth Location: {your_location}\n\n"
                f"PERSON 2\n"
                f"────────\n"
                f"Birth Date: {partner_date}\n"
                f"Birth Time: {partner_time}\n"
                f"Birth Location: {partner_location}\n\n"
                f"{content}\n\n"
                f"───────────────────────────────────────────────────────────\n"
                f"Generated by Starzai • AI-Powered Synastry Analysis\n"
                f"For entertainment and relationship insight purposes\n"
            )
            
            # Save to temp files
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as txt_file:
                txt_file.write(txt_content)
                txt_path = txt_file.name
            
            files_to_send = [discord.File(txt_path, filename=f"compatibility_{your_date}_{partner_date}.txt")]
            temp_files = [txt_path]
            
            if PDF_AVAILABLE:
                pdf_bytes = create_compatibility_pdf(
                    content,
                    your_date, your_time, your_location,
                    partner_date, partner_time, partner_location
                )
                with tempfile.NamedTemporaryFile(mode='wb', suffix='.pdf', delete=False) as pdf_file:
                    pdf_file.write(pdf_bytes)
                    pdf_path = pdf_file.name
                files_to_send.append(discord.File(pdf_path, filename=f"compatibility_{your_date}_{partner_date}.pdf"))
                temp_files.append(pdf_path)
            
            try:
                # Send first embed with files
                await interaction.followup.send(embed=embeds[0], files=files_to_send)
                
                # Send remaining embeds if any
                for i in range(1, len(embeds)):
                    await interaction.followup.send(embed=embeds[i])
            finally:
                # Clean up temp files
                for temp_file in temp_files:
                    os.unlink(temp_file)
            
        except LLMClientError as e:
            logger.error("Compatibility LLM error: %s", e)
            await interaction.followup.send(
                embed=Embedder.error(
                    "Compatibility Error",
                    f"💕 Could not generate compatibility analysis: {str(e)}\n\n"
                    "Please try again with valid birth information for both people."
                )
            )
        except Exception as exc:
            logger.error("Unexpected compatibility error: %s", exc, exc_info=True)
            await interaction.followup.send(
                embed=Embedder.error(
                    "Compatibility Error",
                    f"💕 An unexpected error occurred: {type(exc).__name__}\n\n"
                    "Please try again."
                )
            )


async def setup(bot: StarzaiBot) -> None:
    await bot.add_cog(AstrologyCog(bot))
