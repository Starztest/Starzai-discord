"""
Translator cog — Multi-language translation with auto-detection.
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

LANGUAGES = [
    "english", "spanish", "french", "german", "italian", "portuguese",
    "russian", "japanese", "chinese", "korean", "arabic", "hindi",
    "dutch", "swedish", "polish", "turkish", "vietnamese", "thai",
    "indonesian", "greek", "hebrew", "czech", "romanian", "hungarian",
]


class TranslatorCog(commands.Cog, name="Translator"):
    """Multi-language translation powered by AI."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot

    # ── /translate ───────────────────────────────────────────────────

    @app_commands.command(name="translate", description="Translate text to another language")
    @app_commands.describe(
        text="The text to translate",
        to="Target language (e.g., spanish, french, japanese)",
        source="Source language (auto-detected if omitted)",
    )
    async def translate_cmd(
        self,
        interaction: discord.Interaction,
        text: str,
        to: str,
        source: str = "",
    ) -> None:
        # Rate limit check
        result = self.bot.rate_limiter.check(interaction.user.id, interaction.guild_id)
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return

        await interaction.response.defer()

        source_part = f" from {source}" if source else ""
        prompt = (
            f"Translate the following text{source_part} to {to}.\n"
            f"Only output the translation, nothing else.\n\n"
            f"Text: {text}"
        )

        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system="You are an expert multilingual translator. Provide accurate, natural translations.",
            )

            embed = Embedder.standard(
                "🌐 Translation",
                resp.content,
                fields=[
                    ("Original", text[:1024], False),
                    ("Target Language", to.title(), True),
                ],
            )
            if source:
                embed.insert_field_at(1, name="Source Language", value=source.title(), inline=True)

            await interaction.followup.send(embed=embed)
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="translate",
                guild_id=interaction.guild_id,
                tokens_used=resp.total_tokens,
                latency_ms=resp.latency_ms,
            )

        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Translation Failed", str(exc))
            )

    # ── /detect-language ─────────────────────────────────────────────

    @app_commands.command(
        name="detect-language", description="Detect the language of a text"
    )
    @app_commands.describe(text="The text to analyze")
    async def detect_language_cmd(
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
            "Identify the language of the following text. "
            "Respond with ONLY:\n"
            "Language: <language name>\n"
            "Confidence: <high/medium/low>\n"
            "Script: <writing system used>\n\n"
            f"Text: {text}"
        )

        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system="You are a linguistics expert specializing in language identification.",
            )

            embed = Embedder.standard(
                "🔍 Language Detection",
                resp.content,
                fields=[("Analyzed Text", text[:1024], False)],
            )
            await interaction.followup.send(embed=embed)

        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Detection Failed", str(exc))
            )

    @translate_cmd.autocomplete("to")
    async def translate_to_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=lang.title(), value=lang)
            for lang in LANGUAGES
            if current.lower() in lang
        ][:25]

    @translate_cmd.autocomplete("source")
    async def translate_source_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=lang.title(), value=lang)
            for lang in LANGUAGES
            if current.lower() in lang
        ][:25]


async def setup(bot: StarzaiBot) -> None:
    await bot.add_cog(TranslatorCog(bot))

