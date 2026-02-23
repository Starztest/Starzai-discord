"""
Privacy & Data Management cog — GDPR compliance and user data control.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from utils.embedder import Embedder

if TYPE_CHECKING:
    from bot import StarzaiBot

logger = logging.getLogger(__name__)


class Privacy(commands.Cog):
    """Privacy and data management commands."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot

    @app_commands.command(name="privacy")
    async def privacy_policy(self, interaction: discord.Interaction):
        """View Starzai's privacy policy and data collection practices."""
        embed = discord.Embed(
            title="🔒 Starzai Privacy Policy",
            description=(
                "Your privacy matters to us. Here's what you need to know about "
                "how Starzai collects and uses your data."
            ),
            color=discord.Color.blue(),
        )

        embed.add_field(
            name="📊 What We Collect",
            value=(
                "• **Messages**: We store your recent messages (last 20) to provide personalized responses\n"
                "• **Usage Data**: Command usage, tokens consumed, and timestamps\n"
                "• **Preferences**: Your preferred AI model and settings\n"
                "• **Context**: AI-generated summaries of your interests and personality"
            ),
            inline=False,
        )

        embed.add_field(
            name="🎯 Why We Collect It",
            value=(
                "• **Personalization**: To remember your preferences and provide contextual responses\n"
                "• **Improvement**: To understand usage patterns and improve the bot\n"
                "• **Analytics**: To track token usage and optimize performance"
            ),
            inline=False,
        )

        embed.add_field(
            name="🔐 How We Protect It",
            value=(
                "• **Encryption**: All data is stored securely in an encrypted database\n"
                "• **Retention**: Messages older than 30 days are automatically deleted\n"
                "• **Access**: Only you and the bot can access your data\n"
                "• **No Sharing**: We never sell or share your data with third parties"
            ),
            inline=False,
        )

        embed.add_field(
            name="✅ Your Rights",
            value=(
                "• **Access**: View what data we have about you\n"
                "• **Deletion**: Use `/forget-me` to delete all your data\n"
                "• **Opt-Out**: Stop data collection at any time\n"
                "• **Portability**: Request a copy of your data"
            ),
            inline=False,
        )

        embed.add_field(
            name="📝 Data Retention",
            value=(
                "• **Messages**: Automatically deleted after 30 days\n"
                "• **Usage Logs**: Kept for analytics (anonymized after 90 days)\n"
                "• **Preferences**: Kept until you delete them or leave all servers"
            ),
            inline=False,
        )

        embed.add_field(
            name="🌍 GDPR Compliance",
            value=(
                "Starzai is fully compliant with GDPR and other privacy regulations. "
                "You have the right to access, modify, or delete your data at any time."
            ),
            inline=False,
        )

        embed.set_footer(
            text="Use /forget-me to delete all your data • Last updated: 2024"
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="forget-me")
    async def forget_me(self, interaction: discord.Interaction):
        """Delete all your data from Starzai's database (GDPR right to be forgotten)."""
        # Create confirmation embed
        embed = discord.Embed(
            title="⚠️ Delete All Your Data?",
            description=(
                "This will **permanently delete**:\n"
                "• All your stored messages\n"
                "• Your conversation history\n"
                "• Your personality summary and interests\n"
                "• Your preferences and settings\n\n"
                "**This action cannot be undone!**"
            ),
            color=discord.Color.red(),
        )

        # Create confirmation buttons
        class ConfirmView(discord.ui.View):
            def __init__(self, cog: Privacy):
                super().__init__(timeout=60)
                self.cog = cog
                self.value = None

            @discord.ui.button(
                label="Yes, Delete Everything",
                style=discord.ButtonStyle.danger,
                emoji="🗑️",
            )
            async def confirm(
                self, button_interaction: discord.Interaction, button: discord.ui.Button
            ):
                if button_interaction.user.id != interaction.user.id:
                    await button_interaction.response.send_message(
                        "❌ Only the command user can confirm this action!",
                        ephemeral=True,
                    )
                    return

                # Delete all user data
                try:
                    await self.cog.bot.database.delete_user_data(
                        str(button_interaction.user.id)
                    )

                    success_embed = discord.Embed(
                        title="✅ Data Deleted Successfully",
                        description=(
                            "All your data has been permanently deleted from Starzai's database.\n\n"
                            "• Messages: Deleted\n"
                            "• Context: Deleted\n"
                            "• Preferences: Deleted\n"
                            "• Privacy: Opted out\n\n"
                            "You can continue using Starzai, but personalization features will be disabled. "
                            "If you change your mind, just start using the bot again and data collection will resume."
                        ),
                        color=discord.Color.green(),
                    )

                    await button_interaction.response.edit_message(
                        embed=success_embed, view=None
                    )
                    logger.info(
                        f"User {button_interaction.user.id} deleted all their data"
                    )

                except Exception as e:
                    logger.error(f"Error deleting user data: {e}", exc_info=True)
                    error_embed = Embedder.error(
                        "Deletion Failed",
                        "An error occurred while deleting your data. Please try again later.",
                    )
                    await button_interaction.response.edit_message(
                        embed=error_embed, view=None
                    )

                self.value = True
                self.stop()

            @discord.ui.button(
                label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌"
            )
            async def cancel(
                self, button_interaction: discord.Interaction, button: discord.ui.Button
            ):
                if button_interaction.user.id != interaction.user.id:
                    await button_interaction.response.send_message(
                        "❌ Only the command user can cancel this action!",
                        ephemeral=True,
                    )
                    return

                cancel_embed = discord.Embed(
                    title="✅ Cancelled",
                    description="Your data has not been deleted. Everything remains as it was.",
                    color=discord.Color.blue(),
                )

                await button_interaction.response.edit_message(
                    embed=cancel_embed, view=None
                )
                self.value = False
                self.stop()

        view = ConfirmView(self)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="my-data")
    async def my_data(self, interaction: discord.Interaction):
        """View what data Starzai has stored about you."""
        await interaction.response.defer(ephemeral=True)

        try:
            user_id = str(interaction.user.id)
            guild_id = str(interaction.guild.id) if interaction.guild else None

            # Get user context
            context = None
            if guild_id:
                context = await self.bot.database.get_user_context(user_id, guild_id)

            # Get message count
            message_count = 0
            if guild_id:
                messages = await self.bot.database.get_recent_messages(
                    user_id, guild_id, limit=1000
                )
                message_count = len(messages)

            # Get user preferences
            preferred_model = await self.bot.database.get_user_model(
                int(interaction.user.id)
            )

            # Build embed
            embed = discord.Embed(
                title="📊 Your Data Summary",
                description=f"Here's what Starzai knows about you in this server:",
                color=discord.Color.blue(),
            )

            embed.add_field(
                name="💬 Messages Stored",
                value=f"{message_count} messages" if message_count > 0 else "No messages stored",
                inline=True,
            )

            embed.add_field(
                name="🤖 Preferred Model",
                value=preferred_model or "Not set (using default)",
                inline=True,
            )

            if context:
                recent_count = len(context.get("recent_messages", []))
                embed.add_field(
                    name="📝 Recent Context",
                    value=f"{recent_count} recent messages in context",
                    inline=True,
                )

                if context.get("personality_summary"):
                    embed.add_field(
                        name="🎭 Personality Summary",
                        value=context["personality_summary"][:200] + "..."
                        if len(context["personality_summary"]) > 200
                        else context["personality_summary"],
                        inline=False,
                    )

                if context.get("interests"):
                    interests = context["interests"][:5]  # Show first 5
                    embed.add_field(
                        name="🎯 Detected Interests",
                        value=", ".join(interests) if interests else "None detected yet",
                        inline=False,
                    )

            embed.add_field(
                name="🔒 Privacy",
                value=(
                    "Your data is encrypted and automatically deleted after 30 days.\n"
                    "Use `/forget-me` to delete everything immediately."
                ),
                inline=False,
            )

            embed.set_footer(text="Data is stored per-server and never shared")

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error fetching user data: {e}", exc_info=True)
            await interaction.followup.send(
                embed=Embedder.error(
                    "Error",
                    "Could not retrieve your data. Please try again later.",
                ),
                ephemeral=True,
            )


async def setup(bot: StarzaiBot):
    await bot.add_cog(Privacy(bot))

