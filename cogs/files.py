"""
File Analysis cog — Process and analyze uploaded documents.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from utils.embedder import Embedder
from utils.file_handler import FileHandler
from utils.llm_client import LLMClientError

if TYPE_CHECKING:
    from bot import StarzaiBot

logger = logging.getLogger(__name__)


class FilesCog(commands.Cog, name="Files"):
    """Analyze and summarize uploaded documents."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot

    # ── /analyze-file ────────────────────────────────────────────────

    @app_commands.command(
        name="analyze-file",
        description="Analyze the contents of an uploaded file",
    )
    @app_commands.describe(file="The file to analyze")
    async def analyze_file_cmd(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
    ) -> None:
        result = self.bot.rate_limiter.check(
            interaction.user.id, interaction.guild_id, expensive=True
        )
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return

        await interaction.response.defer()

        content, error = await FileHandler.extract_text(
            file, self.bot.settings.max_file_size_mb
        )
        if error:
            await interaction.followup.send(
                embed=Embedder.error("File Error", error)
            )
            return

        prompt = (
            f"Analyze the following file content from '{file.filename}'.\n\n"
            "Provide:\n"
            "1. **File Type & Purpose** — what kind of file this is and its likely purpose\n"
            "2. **Content Summary** — a concise summary of the contents\n"
            "3. **Key Insights** — important patterns, data points, or notable elements\n"
            "4. **Structure** — how the content is organized\n"
            "5. **Quality Assessment** — any issues, inconsistencies, or areas for improvement\n"
            "6. **Recommendations** — suggestions based on the content\n\n"
            f"File content:\n```\n{content[:6000]}\n```"
        )

        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system="You are an expert document analyst. Provide thorough, actionable file analysis.",
            )

            embed = Embedder.standard(
                f"📄 File Analysis: {file.filename}",
                resp.content,
                fields=[
                    ("File Size", f"{file.size / 1024:.1f} KB", True),
                    ("Type", file.content_type or "Unknown", True),
                ],
            )
            await interaction.followup.send(embed=embed)
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="analyze-file",
                guild_id=interaction.guild_id,
                tokens_used=resp.total_tokens,
                latency_ms=resp.latency_ms,
            )

        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Analysis Failed", str(exc))
            )

    # ── /summarize-file ──────────────────────────────────────────────

    @app_commands.command(
        name="summarize-file",
        description="Get a concise summary of an uploaded file",
    )
    @app_commands.describe(file="The file to summarize")
    async def summarize_file_cmd(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
    ) -> None:
        result = self.bot.rate_limiter.check(
            interaction.user.id, interaction.guild_id, expensive=True
        )
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return

        await interaction.response.defer()

        content, error = await FileHandler.extract_text(
            file, self.bot.settings.max_file_size_mb
        )
        if error:
            await interaction.followup.send(
                embed=Embedder.error("File Error", error)
            )
            return

        prompt = (
            f"Summarize the following file content from '{file.filename}'.\n\n"
            "Create a concise summary that captures:\n"
            "- The main topic or purpose\n"
            "- Key points (bullet list)\n"
            "- Any important data or conclusions\n\n"
            "Keep it brief but comprehensive.\n\n"
            f"File content:\n```\n{content[:6000]}\n```"
        )

        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system="You are an expert at summarizing documents. Be concise and accurate.",
            )

            embed = Embedder.standard(
                f"📋 Summary: {file.filename}",
                resp.content,
                fields=[
                    ("File Size", f"{file.size / 1024:.1f} KB", True),
                ],
            )
            await interaction.followup.send(embed=embed)
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="summarize-file",
                guild_id=interaction.guild_id,
                tokens_used=resp.total_tokens,
                latency_ms=resp.latency_ms,
            )

        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Summary Failed", str(exc))
            )


async def setup(bot: StarzaiBot) -> None:
    await bot.add_cog(FilesCog(bot))

