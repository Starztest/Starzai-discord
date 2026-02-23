"""
Games cog — Trivia, word games, and riddles.
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config.constants import TRIVIA_CATEGORIES
from utils.embedder import Embedder
from utils.llm_client import LLMClientError

if TYPE_CHECKING:
    from bot import StarzaiBot

logger = logging.getLogger(__name__)


class GamesCog(commands.Cog, name="Games"):
    """Interactive games — trivia, word games, and riddles."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot

    # ── /trivia ──────────────────────────────────────────────────────

    @app_commands.command(name="trivia", description="Get a trivia question")
    @app_commands.describe(category="Trivia category")
    @app_commands.choices(
        category=[
            app_commands.Choice(name=c.title(), value=c)
            for c in TRIVIA_CATEGORIES
        ]
    )
    async def trivia_cmd(
        self,
        interaction: discord.Interaction,
        category: str = "",
    ) -> None:
        result = self.bot.rate_limiter.check(interaction.user.id, interaction.guild_id)
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return

        await interaction.response.defer()

        cat = category or random.choice(TRIVIA_CATEGORIES)
        prompt = (
            f"Create a trivia question about {cat}.\n\n"
            "Format:\n"
            "**Question:** <the question>\n\n"
            "**Options:**\n"
            "A) <option>\n"
            "B) <option>\n"
            "C) <option>\n"
            "D) <option>\n\n"
            "||**Answer:** <correct letter and explanation>||\n\n"
            "Make the question interesting, challenging but fair. "
            "Use Discord spoiler tags (||) around the answer so users can try first."
        )

        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system="You are a trivia game host. Create fun, accurate trivia questions.",
            )

            embed = Embedder.standard(
                f"🧠 Trivia — {cat.title()}",
                resp.content,
                footer="Click the spoiler to reveal the answer!",
            )
            await interaction.followup.send(embed=embed)
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="trivia",
                guild_id=interaction.guild_id,
                tokens_used=resp.total_tokens,
            )

        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Trivia Error", str(exc))
            )

    # ── /word-game ───────────────────────────────────────────────────

    @app_commands.command(
        name="word-game", description="Play a fun word game challenge"
    )
    async def word_game_cmd(self, interaction: discord.Interaction) -> None:
        result = self.bot.rate_limiter.check(interaction.user.id, interaction.guild_id)
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return

        await interaction.response.defer()

        game_types = [
            "Create an anagram puzzle. Give a scrambled word and ask the user to unscramble it.",
            "Create a 'word chain' challenge. Give a word and ask the user to reply with a word that starts with the last letter.",
            "Create a 'fill in the blank' challenge with a well-known phrase or idiom.",
            "Create a word association game. Give 4 words that share a hidden connection.",
            "Create a vocabulary challenge. Define a rare or unusual word and give 4 possible meanings.",
        ]

        prompt = (
            f"{random.choice(game_types)}\n\n"
            "Format the game clearly with:\n"
            "- Clear instructions\n"
            "- The challenge/puzzle\n"
            "- ||The answer in spoiler tags||\n\n"
            "Make it fun and engaging!"
        )

        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system="You are a creative word game designer. Make games fun and engaging.",
            )

            embed = Embedder.standard("🔤 Word Game", resp.content)
            await interaction.followup.send(embed=embed)
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="word-game",
                guild_id=interaction.guild_id,
                tokens_used=resp.total_tokens,
            )

        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Game Error", str(exc))
            )

    # ── /riddle ──────────────────────────────────────────────────────

    @app_commands.command(name="riddle", description="Get a brain-teasing riddle")
    async def riddle_cmd(self, interaction: discord.Interaction) -> None:
        result = self.bot.rate_limiter.check(interaction.user.id, interaction.guild_id)
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return

        await interaction.response.defer()

        difficulty = random.choice(["easy", "medium", "hard"])
        prompt = (
            f"Create an original {difficulty} riddle.\n\n"
            "Format:\n"
            "**Riddle:**\n<the riddle in a poetic/mysterious style>\n\n"
            f"**Difficulty:** {difficulty}\n\n"
            "**Hint:** ||<a subtle hint>||\n\n"
            "**Answer:** ||<the answer with explanation>||\n\n"
            "Make it creative and thought-provoking!"
        )

        try:
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system="You are a master riddler. Create clever, original riddles.",
            )

            embed = Embedder.standard(
                f"🧩 Riddle — {difficulty.title()}",
                resp.content,
                footer="Use the spoiler tags to check hints and answer!",
            )
            await interaction.followup.send(embed=embed)
            await self.bot.database.log_usage(
                user_id=interaction.user.id,
                command="riddle",
                guild_id=interaction.guild_id,
                tokens_used=resp.total_tokens,
            )

        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Riddle Error", str(exc))
            )


async def setup(bot: StarzaiBot) -> None:
    await bot.add_cog(GamesCog(bot))
