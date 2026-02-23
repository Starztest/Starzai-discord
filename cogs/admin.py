"""
Admin cog — Owner-only bot management commands.
"""

from __future__ import annotations

import logging
import platform
import sys
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from utils.embedder import Embedder

if TYPE_CHECKING:
    from bot import StarzaiBot

logger = logging.getLogger(__name__)


def _is_owner():
    """Check decorator: only allow configured bot owners."""

    async def predicate(interaction: discord.Interaction) -> bool:
        bot: StarzaiBot = interaction.client  # type: ignore
        if interaction.user.id not in bot.settings.owner_ids:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Access Denied",
                    "This command is restricted to bot owners.",
                ),
                ephemeral=True,
            )
            return False
        return True

    return app_commands.check(predicate)


class AdminCog(commands.Cog, name="Admin"):
    """Bot administration — owner-only management commands."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot

    # ── /reload ──────────────────────────────────────────────────────

    @app_commands.command(name="reload", description="🔒 Reload a cog (owner only)")
    @app_commands.describe(cog="Cog name to reload (e.g., 'cogs.chat')")
    @_is_owner()
    async def reload_cmd(
        self, interaction: discord.Interaction, cog: str
    ) -> None:
        # Normalize: allow both "chat" and "cogs.chat"
        if not cog.startswith("cogs."):
            cog = f"cogs.{cog}"

        try:
            await self.bot.reload_extension(cog)
            await interaction.response.send_message(
                embed=Embedder.success("Cog Reloaded", f"`{cog}` has been reloaded successfully.")
            )
            logger.info("Cog reloaded by %s: %s", interaction.user, cog)
        except commands.ExtensionNotLoaded:
            await interaction.response.send_message(
                embed=Embedder.error("Not Loaded", f"`{cog}` is not currently loaded."),
                ephemeral=True,
            )
        except commands.ExtensionNotFound:
            await interaction.response.send_message(
                embed=Embedder.error("Not Found", f"`{cog}` was not found."),
                ephemeral=True,
            )
        except Exception as exc:
            logger.error("Failed to reload %s: %s", cog, exc, exc_info=True)
            await interaction.response.send_message(
                embed=Embedder.error("Reload Failed", f"```\n{exc}\n```"),
                ephemeral=True,
            )

    # ── /stats ───────────────────────────────────────────────────────

    @app_commands.command(name="stats", description="🔒 View bot statistics (owner only)")
    @_is_owner()
    async def stats_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        db_stats = await self.bot.database.get_global_stats()

        embed = Embedder.standard(
            "📊 Bot Statistics",
            "",
            fields=[
                ("Guilds", str(len(self.bot.guilds)), True),
                ("Users (DB)", f"{db_stats['total_users']:,}", True),
                ("Latency", f"{self.bot.latency * 1000:.1f}ms", True),
                ("Commands Run", f"{db_stats['total_commands']:,}", True),
                ("Total Tokens", f"{db_stats['total_tokens']:,}", True),
                ("Active Convos", f"{db_stats['active_conversations']:,}", True),
                ("Python", platform.python_version(), True),
                ("discord.py", discord.__version__, True),
                ("Platform", platform.system(), True),
            ],
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /sync ────────────────────────────────────────────────────────

    @app_commands.command(
        name="sync", description="🔒 Sync slash commands globally (owner only)"
    )
    @_is_owner()
    async def sync_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            synced = await self.bot.tree.sync()
            await interaction.followup.send(
                embed=Embedder.success(
                    "Commands Synced",
                    f"Successfully synced **{len(synced)}** commands globally.",
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(
                embed=Embedder.error("Sync Failed", f"```\n{exc}\n```"),
                ephemeral=True,
            )

    # ── /shutdown ────────────────────────────────────────────────────

    @app_commands.command(
        name="shutdown", description="🔒 Gracefully shut down the bot (owner only)"
    )
    @_is_owner()
    async def shutdown_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=Embedder.warning("Shutting Down", "Bot is shutting down gracefully…"),
            ephemeral=True,
        )
        logger.info("Shutdown initiated by %s", interaction.user)
        await self.bot.close()

    # ── /usage ───────────────────────────────────────────────────────

    @app_commands.command(
        name="usage", description="Check your personal usage statistics"
    )
    async def usage_cmd(self, interaction: discord.Interaction) -> None:
        stats = await self.bot.database.get_user_stats(interaction.user.id)
        rl_usage = self.bot.rate_limiter.get_user_usage(interaction.user.id)

        embed = Embedder.standard(
            "📈 Your Usage",
            "",
            fields=[
                ("Total Tokens Used", f"{stats['total_tokens']:,}", True),
                ("Preferred Model", stats.get("preferred_model") or "Default", True),
                ("Tokens Today", f"{rl_usage['tokens_today']:,} / {rl_usage['token_limit']:,}", True),
                ("Member Since", str(stats.get("created_at", "Unknown")), False),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /help ────────────────────────────────────────────────────────

    @app_commands.command(name="help", description="View all available commands and bot information")
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        """Comprehensive help command showing all bot features."""
        
        # Main help embed
        embed = discord.Embed(
            title="✨ Starzai Bot - Command Guide",
            description=(
                "**Starzai** is an AI-powered Discord bot with conversational abilities, "
                "utility features, and personalization.\n\n"
                "Use the buttons below to explore different command categories!"
            ),
            color=discord.Color.blue(),
        )
        
        embed.add_field(
            name="🤖 AI Chat Commands",
            value=(
                "`/chat` - Send a message to the AI\n"
                "`/ask` - Ask with a specific model\n"
                "`/conversation start` - Begin a persistent conversation\n"
                "`/conversation end` - End current conversation\n"
                "`/conversation clear` - Clear conversation history\n"
                "`/set-model` - Set your preferred AI model\n"
                "`/models` - List all available models"
            ),
            inline=False,
        )
        
        embed.add_field(
            name="🌍 Language & Text Tools",
            value=(
                "`/translate` - Translate text to another language\n"
                "`/detect-language` - Detect the language of text\n"
                "`/etymology` - Learn word origins and history\n"
                "`/word-history` - Detailed word etymology\n"
                "`/check-grammar` - Check grammar and spelling\n"
                "`/improve-text` - Improve text style and clarity"
            ),
            inline=False,
        )
        
        embed.add_field(
            name="⭐ Astrology & Personality",
            value=(
                "`/horoscope` - Get your daily/weekly/monthly horoscope\n"
                "`/birth-chart` - Generate detailed birth chart\n"
                "`/synastry` - Compatibility analysis between two people\n"
                "`/analyze-personality` - Analyze personality from text"
            ),
            inline=False,
        )
        
        embed.add_field(
            name="📁 File Analysis",
            value=(
                "`/analyze-file` - Analyze uploaded documents\n"
                "`/summarize-file` - Summarize document content"
            ),
            inline=False,
        )
        
        embed.add_field(
            name="🎮 Games & Fun",
            value=(
                "`/trivia` - Play trivia with different categories\n"
                "`/word-game` - Interactive word games\n"
                "`/riddle` - Solve riddles and puzzles"
            ),
            inline=False,
        )
        
        embed.add_field(
            name="🔒 Privacy & Data",
            value=(
                "`/privacy` - View privacy policy\n"
                "`/my-data` - See what data we store about you\n"
                "`/forget-me` - Delete all your data (GDPR)"
            ),
            inline=False,
        )
        
        embed.add_field(
            name="📊 User Commands",
            value=(
                "`/usage` - Check your usage statistics\n"
                "`/help` - Show this help message"
            ),
            inline=False,
        )
        
        embed.add_field(
            name="💡 Features",
            value=(
                "✅ **Multi-turn Conversations** - Context-aware AI chat\n"
                "✅ **Personalization** - Remembers your preferences\n"
                "✅ **Privacy-First** - GDPR compliant with data controls\n"
                "✅ **Multiple AI Models** - Choose your preferred model\n"
                "✅ **Real Astronomy** - Swiss Ephemeris calculations\n"
                "✅ **30-Day Data Retention** - Automatic cleanup"
            ),
            inline=False,
        )
        
        embed.add_field(
            name="🔗 Links & Support",
            value=(
                "• **Privacy Policy**: Use `/privacy` to view\n"
                "• **Data Management**: Use `/my-data` to see your data\n"
                "• **Support**: Contact bot owner for help"
            ),
            inline=False,
        )
        
        embed.set_footer(
            text="Starzai • AI-Powered Discord Bot • Use /privacy for data policy"
        )
        
        embed.set_thumbnail(url=self.bot.user.avatar.url if self.bot.user.avatar else None)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: StarzaiBot) -> None:
    await bot.add_cog(AdminCog(bot))
