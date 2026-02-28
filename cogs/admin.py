"""
Admin cog — Owner-only bot management commands.
"""

from __future__ import annotations

import logging
import platform
import sys
from typing import TYPE_CHECKING, Optional

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

    # ── /allow ───────────────────────────────────────────────────────

    @app_commands.command(
        name="allow",
        description="🔒 Allow a guild to use the bot (owner only)",
    )
    @app_commands.describe(guild_id="The guild/server ID to authorise (defaults to the current server)")
    @_is_owner()
    async def allow_cmd(
        self, interaction: discord.Interaction, guild_id: Optional[str] = None
    ) -> None:
        resolved = int(guild_id) if guild_id else (interaction.guild_id if interaction.guild_id else None)
        if resolved is None:
            await interaction.response.send_message(
                embed=Embedder.error("No Guild", "Provide a guild ID or run this in a server."),
                ephemeral=True,
            )
            return

        self.bot.allowed_guilds.add(resolved)
        self.bot.save_allowed_guilds()

        guild_obj = self.bot.get_guild(resolved)
        name = guild_obj.name if guild_obj else str(resolved)
        await interaction.response.send_message(
            embed=Embedder.success(
                "Guild Allowed",
                f"**{name}** (`{resolved}`) can now use the bot.",
            ),
            ephemeral=True,
        )
        logger.info("Guild %s allowed by %s", resolved, interaction.user)

    # ── /disallow ────────────────────────────────────────────────────

    @app_commands.command(
        name="disallow",
        description="🔒 Revoke a guild's access to the bot (owner only)",
    )
    @app_commands.describe(guild_id="The guild/server ID to revoke (defaults to current server)")
    @_is_owner()
    async def disallow_cmd(
        self, interaction: discord.Interaction, guild_id: Optional[str] = None
    ) -> None:
        resolved = int(guild_id) if guild_id else (interaction.guild_id if interaction.guild_id else None)
        if resolved is None:
            await interaction.response.send_message(
                embed=Embedder.error("No Guild", "Provide a guild ID or run this in a server."),
                ephemeral=True,
            )
            return

        self.bot.allowed_guilds.discard(resolved)
        self.bot.save_allowed_guilds()

        guild_obj = self.bot.get_guild(resolved)
        name = guild_obj.name if guild_obj else str(resolved)
        await interaction.response.send_message(
            embed=Embedder.success(
                "Guild Removed",
                f"**{name}** (`{resolved}`) has been removed from the allowlist.",
            ),
            ephemeral=True,
        )
        logger.info("Guild %s disallowed by %s", resolved, interaction.user)

    # ── /allowlist ───────────────────────────────────────────────────

    @app_commands.command(
        name="allowlist",
        description="🔒 Show all allowed guilds (owner only)",
    )
    @_is_owner()
    async def allowlist_cmd(self, interaction: discord.Interaction) -> None:
        if not self.bot.allowed_guilds:
            await interaction.response.send_message(
                embed=Embedder.info(
                    "Allowlist Empty",
                    "No guilds are currently allowed. Use `/allow` to add one.",
                ),
                ephemeral=True,
            )
            return

        lines = []
        for gid in sorted(self.bot.allowed_guilds):
            guild_obj = self.bot.get_guild(gid)
            name = guild_obj.name if guild_obj else "Unknown"
            lines.append(f"• **{name}** (`{gid}`)")

        await interaction.response.send_message(
            embed=Embedder.standard(
                "📝 Allowed Guilds",
                "\n".join(lines),
            ),
            ephemeral=True,
        )

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
                "`/transits` - Current transit forecast for your chart\n"
                "`/compatibility` - Compatibility analysis between two people\n"
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
            name="\U0001f3b5 Music Dashboard",
            value=(
                "`/dashboard` - **All-in-one music control panel** with buttons\n"
                "> _Play, queue, volume, filters, playlists, favorites & more — no commands needed!_"
            ),
            inline=False,
        )
        
        embed.add_field(
            name="\U0001f3b6 Music Commands",
            value=(
                "`/music` - Search and download a song (MP3)\n"
                "`/play` - Play a song in your voice channel\n"
                "`/queue` - View the music queue (paginated)\n"
                "`/nowplaying` - Show current song with progress bar\n"
                "`/skip` / `/voteskip` - Skip or vote-skip the current song\n"
                "`/pause` / `/resume` - Pause or resume playback\n"
                "`/shuffle` - Shuffle the queue\n"
                "`/loop` - Set loop mode (off / track / queue)\n"
                "`/seek` - Jump to a position in the song\n"
                "`/volume` - Set playback volume (0-100)\n"
                "`/lyrics` - Search for song lyrics\n"
                "`/music-stop` - Stop playback and leave VC"
            ),
            inline=False,
        )
        
        embed.add_field(
            name="\U0001f3bc Advanced Music",
            value=(
                "`/skipto` - Skip to a specific queue position\n"
                "`/move` - Move a song to a different position\n"
                "`/swap` - Swap two songs in the queue\n"
                "`/remove` / `/clear` - Remove songs or clear the queue\n"
                "`/duplicates` - Remove duplicate songs\n"
                "`/replay` / `/previous` - Restart or go back a song\n"
                "`/grab` - Save current song info to your DMs\n"
                "`/history` - Show recently played songs\n"
                "`/filter` - Apply audio filters (bass, nightcore, etc.)\n"
                "`/autoplay` - Auto-queue similar songs when queue ends\n"
                "`/247` - Stay in VC even when idle\n"
                "`/djrole` - Set a DJ role for queue management"
            ),
            inline=False,
        )
        
        embed.add_field(
            name="\U0001f4bf Playlists & Favorites",
            value=(
                "`/playlist create` / `delete` / `rename` - Manage playlists\n"
                "`/playlist add` / `remove` - Add or remove songs\n"
                "`/playlist list` / `view` - Browse your playlists\n"
                "`/playlist play` - Load a playlist into the queue\n"
                "`/playlist save-queue` - Save current queue as a playlist\n"
                "`/favorite` - Toggle favorite on current song\n"
                "`/favorites` - View all your favorite songs\n"
                "`/musicprofile` - View your or another user's music profile\n"
                "`/sleeptimer` - Auto-disconnect after a set time"
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
                "✅ **Music Dashboard** - Full button-driven control panel (`/dashboard`)\n"
                "✅ **Music Player** - VC playback with progress bar, loop, shuffle, seek\n"
                "✅ **Playlists & Favorites** - Save, organize, and replay your music\n"
                "✅ **MP3 Downloads** - High-quality single-file downloads\n"
                "✅ **7 Music Platforms** - Spotify, YouTube, SoundCloud, Tidal & more\n"
                "✅ **Audio Filters** - Bass boost, nightcore, vaporwave & more\n"
                "✅ **DJ Role System** - Control who manages the queue\n"
                "✅ **Auto-Resume** - Resumes playback on reconnect\n"
                "✅ **Personalization** - Remembers your preferences\n"
                "✅ **Privacy-First** - GDPR compliant with data controls\n"
                "✅ **Multiple AI Models** - Choose your preferred model\n"
                "✅ **Real Astronomy** - Swiss Ephemeris calculations"
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
