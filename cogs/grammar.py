"""
Grammar & Vocabulary Editor cog — Text improvement and grammar checking.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from config.constants import TEXT_STYLES
from utils.embedder import Embedder
from utils.llm_client import LLMClientError

if TYPE_CHECKING:
    from bot import StarzaiBot

logger = logging.getLogger(__name__)


class StyleSelectorView(discord.ui.View):
    """Interactive button view for selecting text improvement styles."""
    
    def __init__(self, bot: StarzaiBot, user_id: int, original_text: str, original_message: discord.Message):
        super().__init__(timeout=300)  # 5 minute timeout
        self.bot = bot
        self.user_id = user_id
        self.original_text = original_text
        self.original_message = original_message
        
        # Add buttons for each style
        styles = [
            ("Formal", "formal", discord.ButtonStyle.primary),
            ("Casual", "casual", discord.ButtonStyle.secondary),
            ("Academic", "academic", discord.ButtonStyle.primary),
            ("Creative", "creative", discord.ButtonStyle.secondary),
            ("Concise", "concise", discord.ButtonStyle.primary),
            ("Professional", "professional", discord.ButtonStyle.secondary),
        ]
        
        for i, (label, style, button_style) in enumerate(styles):
            button = discord.ui.Button(
                label=label,
                style=button_style,
                custom_id=f"style_{style}",
                row=i // 3,  # 3 buttons per row
            )
            button.callback = self._make_callback(style)
            self.add_item(button)
    
    def _make_callback(self, style: str):
        """Create a callback for a specific style button."""
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    "These buttons aren't for you! Use `/improve-text` to get your own.",
                    ephemeral=True,
                )
                return
            
            await interaction.response.defer()
            
            style_descriptions = {
                "formal": "professional, polished, suitable for business or academic contexts",
                "casual": "relaxed, conversational, friendly tone",
                "academic": "scholarly, precise, with appropriate terminology",
                "creative": "vivid, expressive, engaging and unique",
                "concise": "brief, to-the-point, removing all unnecessary words",
                "professional": "clear, authoritative, suitable for workplace communication",
            }
            style_desc = style_descriptions.get(style, style)
            
            prompt = (
                f"Rewrite the following text in a {style} style ({style_desc}).\n\n"
                f"Original text: \"{self.original_text}\"\n\n"
                "Provide ONLY the improved text in your response. "
                "Do not include explanations, labels, or any other text. "
                "Just output the rewritten version directly."
            )
            
            try:
                resp = await self.bot.llm.simple_prompt(
                    prompt,
                    system=f"You are an expert writing coach. Output ONLY the rewritten text, nothing else.",
                    max_tokens=4096,
                )
                
                improved_text = resp.content.strip()
                
                # Create embed with copyable code block
                embed = discord.Embed(
                    title=f"📝 Text Improved — {style.title()} Style",
                    description=f"**Improved Text:**\n```\n{improved_text[:3900]}\n```",
                    color=discord.Color.green(),
                )
                embed.add_field(
                    name="Original Text",
                    value=f"```\n{self.original_text[:1000]}\n```",
                    inline=False,
                )
                embed.set_footer(text=f"Style: {style.title()} | Click another button to try a different style")
                
                # Edit the original message instead of sending a new one
                await self.original_message.edit(embed=embed, view=self)
                await interaction.followup.send("✅ Text improved!", ephemeral=True)
                
                await self.bot.database.log_usage(
                    user_id=interaction.user.id,
                    command="improve-text",
                    guild_id=interaction.guild_id,
                    tokens_used=resp.total_tokens,
                    latency_ms=resp.latency_ms,
                )
                
            except LLMClientError as exc:
                await interaction.followup.send(
                    embed=Embedder.error("Improvement Failed", str(exc))
                )
        
        return callback


class GrammarCog(commands.Cog, name="Grammar"):
    """Advanced grammar checking and text improvement."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot

    # ── /check-grammar ───────────────────────────────────────────────

    @app_commands.command(
        name="check-grammar", description="Check text for grammar and spelling errors"
    )
    @app_commands.describe(text="The text to check")
    async def check_grammar_cmd(
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
            f"Analyze the following text for grammar, spelling, and punctuation errors.\n\n"
            f"Text: \"{text}\"\n\n"
            "Provide:\n"
            "1. **Corrected Text** — the fixed version\n"
            "2. **Errors Found** — list each error with an explanation\n"
            "3. **Score** — rate the original text's quality from 1-10\n"
            "4. **Tips** — 1-2 general writing tips based on the errors found\n\n"
            "If the text is already correct, say so and give a high score."
        )

        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system="You are an expert English editor and proofreader. Be thorough but encouraging.",
                max_tokens=4096,  # Maximum tokens for detailed grammar analysis
            )

            embed = Embedder.standard(
                "✏️ Grammar Check",
                resp.content,
                fields=[("Original Text", text[:1024], False)],
            )
            await interaction.followup.send(embed=embed)
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="check-grammar",
                guild_id=interaction.guild_id,
                tokens_used=resp.total_tokens,
                latency_ms=resp.latency_ms,
            )

        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Grammar Check Failed", str(exc))
            )

    # ── /improve-text ────────────────────────────────────────────────

    @app_commands.command(
        name="improve-text",
        description="Improve text with interactive style selection",
    )
    @app_commands.describe(
        text="The text to improve",
    )
    async def improve_text_cmd(
        self,
        interaction: discord.Interaction,
        text: str,
    ) -> None:
        result = self.bot.rate_limiter.check(interaction.user.id, interaction.guild_id)
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return

        embed = discord.Embed(
            title="📝 Text Improvement",
            description=(
                "**Select a style to improve your text:**\n\n"
                "Click any button below to see your text rewritten in that style. "
                "The improved text will be in a copyable code block for easy copying!\n\n"
                f"**Your Original Text:**\n```\n{text[:1000]}\n```"
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(text="Buttons expire in 5 minutes")
        
        # Send the message first, then create the view with the message reference
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()
        
        # Now create the view with the message reference and edit to add buttons
        view = StyleSelectorView(self.bot, interaction.user.id, text, message)
        await message.edit(view=view)


async def setup(bot: StarzaiBot) -> None:
    await bot.add_cog(GrammarCog(bot))
