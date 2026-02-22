"""
Core LLM Chat cog — /chat, /ask, /conversation, /set-model, /models
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config.constants import (
    EXPENSIVE_COMMANDS,
    MAX_CONVERSATION_MESSAGES,
    MAX_CONTEXT_CHARS,
    STREAMING_EDIT_INTERVAL,
)
from utils.embedder import Embedder
from utils.llm_client import LLMClientError

if TYPE_CHECKING:
    from bot import StarzaiBot

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are Starzai, a friendly and knowledgeable AI assistant on Discord. "
    "Be helpful, concise, and engaging. Use markdown formatting when appropriate. "
    "If you don't know something, say so honestly."
)


def _truncate(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    return text[:limit] + "…" if len(text) > limit else text


class ChatCog(commands.Cog, name="Chat"):
    """Core AI chat features powered by MegaLLM."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot

    # ── Helper: rate-limit gate ──────────────────────────────────────

    async def _gate(
        self, interaction: discord.Interaction, expensive: bool = False
    ) -> bool:
        """Check rate limits. Returns True if allowed, else sends error."""
        result = self.bot.rate_limiter.check(
            user_id=interaction.user.id,
            server_id=interaction.guild_id,
            expensive=expensive,
        )
        if not result.allowed:
            await interaction.response.send_message(
                embed=Embedder.rate_limited(result.retry_after), ephemeral=True
            )
            return False

        token_result = self.bot.rate_limiter.check_token_budget(
            user_id=interaction.user.id, server_id=interaction.guild_id
        )
        if not token_result.allowed:
            await interaction.response.send_message(
                embed=Embedder.warning("Token Limit", token_result.reason),
                ephemeral=True,
            )
            return False

        return True

    # ── Helper: resolve model ────────────────────────────────────────

    async def _resolve_model(
        self, user_id: int, explicit: Optional[str] = None
    ) -> str:
        if explicit:
            return self.bot.settings.resolve_model(explicit)
        saved = await self.bot.database.get_user_model(user_id)
        if saved:
            return saved
        return self.bot.settings.default_model

    # ── Helper: log usage ────────────────────────────────────────────

    async def _log(
        self,
        interaction: discord.Interaction,
        command: str,
        model: str,
        tokens: int = 0,
        latency_ms: float = 0.0,
        success: bool = True,
        error_message: Optional[str] = None,
    ) -> None:
        await self.bot.database.log_usage(
            user_id=interaction.user.id,
            command=command,
            guild_id=interaction.guild_id,
            model=model,
            tokens_used=tokens,
            latency_ms=latency_ms,
            success=success,
            error_message=error_message,
        )
        if tokens:
            await self.bot.database.add_user_tokens(interaction.user.id, tokens)
            self.bot.rate_limiter.record_tokens(
                interaction.user.id, tokens, interaction.guild_id
            )

    # ── /chat ────────────────────────────────────────────────────────

    @app_commands.command(name="chat", description="Send a message to Starzai AI")
    @app_commands.describe(message="Your message to the AI")
    async def chat_cmd(
        self, interaction: discord.Interaction, message: str
    ) -> None:
        if not await self._gate(interaction, expensive=True):
            return

        await interaction.response.defer()

        model = await self._resolve_model(interaction.user.id)

        try:
            # Stream the response
            collected = ""
            msg: Optional[discord.WebhookMessage] = None
            last_edit = 0.0

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _truncate(message)},
            ]

            async for chunk in self.bot.llm.chat_stream(messages, model=model):
                collected += chunk
                now = asyncio.get_event_loop().time()

                if msg is None:
                    msg = await interaction.followup.send(
                        embed=Embedder.streaming(collected), wait=True
                    )
                    last_edit = now
                elif now - last_edit >= STREAMING_EDIT_INTERVAL:
                    await msg.edit(embed=Embedder.streaming(collected))
                    last_edit = now

            # Final edit with full response
            if msg:
                await msg.edit(
                    embed=Embedder.chat_response(collected, model)
                )
            else:
                # Fallback if stream yielded nothing
                resp = await self.bot.llm.simple_prompt(message, model=model)
                collected = resp.content
                await interaction.followup.send(
                    embed=Embedder.chat_response(
                        resp.content, model, resp.total_tokens, resp.latency_ms
                    )
                )

            await self._log(interaction, "chat", model, success=True)

        except LLMClientError as exc:
            logger.error("LLM error in /chat: %s", exc)
            await interaction.followup.send(
                embed=Embedder.error("AI Error", f"The AI service returned an error: {exc}")
            )
            await self._log(interaction, "chat", model, success=False, error_message=str(exc))

        except Exception as exc:
            logger.error("Unexpected error in /chat: %s", exc, exc_info=True)
            await interaction.followup.send(
                embed=Embedder.error("Error", "Something went wrong. Please try again.")
            )
            await self._log(interaction, "chat", model, success=False, error_message=str(exc))

    # ── /ask ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="ask", description="Ask a question using a specific model"
    )
    @app_commands.describe(message="Your question", model="AI model to use")
    async def ask_cmd(
        self, interaction: discord.Interaction, message: str, model: str = ""
    ) -> None:
        if not await self._gate(interaction, expensive=True):
            return

        await interaction.response.defer()
        resolved = await self._resolve_model(interaction.user.id, model or None)

        try:
            resp = await self.bot.llm.simple_prompt(message, model=resolved)
            await interaction.followup.send(
                embed=Embedder.chat_response(
                    resp.content, resolved, resp.total_tokens, resp.latency_ms
                )
            )
            await self._log(
                interaction, "ask", resolved, resp.total_tokens, resp.latency_ms
            )
        except LLMClientError as exc:
            await interaction.followup.send(
                embed=Embedder.error("AI Error", str(exc))
            )
            await self._log(interaction, "ask", resolved, success=False, error_message=str(exc))

    # ── /conversation ────────────────────────────────────────────────

    conversation_group = app_commands.Group(
        name="conversation",
        description="Manage persistent AI conversations",
    )

    @conversation_group.command(
        name="start", description="Start a new conversation with memory"
    )
    async def conv_start(self, interaction: discord.Interaction) -> None:
        model = await self._resolve_model(interaction.user.id)
        conv_id = await self.bot.database.start_conversation(
            interaction.user.id, interaction.guild_id, model
        )
        await interaction.response.send_message(
            embed=Embedder.conversation_status(
                "start",
                f"New conversation started (ID: `{conv_id}`)\n"
                f"Model: `{model}`\n"
                f"The bot will remember the last {MAX_CONVERSATION_MESSAGES} messages.\n"
                f"Use `/conversation end` when you're done.",
            )
        )

    @conversation_group.command(
        name="end", description="End your current conversation"
    )
    async def conv_end(self, interaction: discord.Interaction) -> None:
        conv = await self.bot.database.get_active_conversation(
            interaction.user.id, interaction.guild_id
        )
        if not conv:
            await interaction.response.send_message(
                embed=Embedder.warning("No Conversation", "You don't have an active conversation."),
                ephemeral=True,
            )
            return
        await self.bot.database.end_conversation(interaction.user.id, interaction.guild_id)
        await interaction.response.send_message(
            embed=Embedder.conversation_status("end", "Your conversation has been ended.")
        )

    @conversation_group.command(
        name="clear", description="Clear the current conversation history"
    )
    async def conv_clear(self, interaction: discord.Interaction) -> None:
        conv = await self.bot.database.get_active_conversation(
            interaction.user.id, interaction.guild_id
        )
        if not conv:
            await interaction.response.send_message(
                embed=Embedder.warning("No Conversation", "You don't have an active conversation."),
                ephemeral=True,
            )
            return
        await self.bot.database.clear_conversation(conv["id"])
        await interaction.response.send_message(
            embed=Embedder.conversation_status("clear", "Conversation history cleared. Context reset.")
        )

    @conversation_group.command(
        name="export", description="Export your conversation as a text file"
    )
    async def conv_export(self, interaction: discord.Interaction) -> None:
        conv = await self.bot.database.get_active_conversation(
            interaction.user.id, interaction.guild_id
        )
        if not conv:
            await interaction.response.send_message(
                embed=Embedder.warning("No Conversation", "You don't have an active conversation."),
                ephemeral=True,
            )
            return

        from utils.file_handler import FileHandler

        transcript = await self.bot.database.get_conversation_export(conv["id"])
        file = FileHandler.make_text_file(transcript, "starzai_conversation.txt")
        await interaction.response.send_message(
            embed=Embedder.success("Conversation Exported", "Here's your conversation transcript:"),
            file=file,
        )

    # ── /say (conversation message) ──────────────────────────────────

    @app_commands.command(
        name="say", description="Send a message in your active conversation"
    )
    @app_commands.describe(message="Your message")
    async def say_cmd(
        self, interaction: discord.Interaction, message: str
    ) -> None:
        if not await self._gate(interaction, expensive=True):
            return

        conv = await self.bot.database.get_active_conversation(
            interaction.user.id, interaction.guild_id
        )
        if not conv:
            await interaction.response.send_message(
                embed=Embedder.warning(
                    "No Conversation",
                    "Start a conversation first with `/conversation start`.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        model = conv["model_used"] or await self._resolve_model(interaction.user.id)

        # Build messages with context
        context_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for msg in conv["messages"]:
            context_messages.append({
                "role": msg["role"],
                "content": _truncate(msg["content"]),
            })
        context_messages.append({"role": "user", "content": _truncate(message)})

        try:
            # Stream response
            collected = ""
            reply: Optional[discord.WebhookMessage] = None
            last_edit = 0.0

            async for chunk in self.bot.llm.chat_stream(context_messages, model=model):
                collected += chunk
                now = asyncio.get_event_loop().time()
                if reply is None:
                    reply = await interaction.followup.send(
                        embed=Embedder.streaming(collected), wait=True
                    )
                    last_edit = now
                elif now - last_edit >= STREAMING_EDIT_INTERVAL:
                    await reply.edit(embed=Embedder.streaming(collected))
                    last_edit = now

            if reply:
                await reply.edit(embed=Embedder.chat_response(collected, model))
            else:
                resp = await self.bot.llm.simple_prompt(message, model=model)
                collected = resp.content
                await interaction.followup.send(
                    embed=Embedder.chat_response(resp.content, model, resp.total_tokens, resp.latency_ms)
                )

            # Save messages to conversation
            await self.bot.database.append_message(
                conv["id"], "user", message, MAX_CONVERSATION_MESSAGES
            )
            await self.bot.database.append_message(
                conv["id"], "assistant", collected, MAX_CONVERSATION_MESSAGES
            )
            await self._log(interaction, "say", model, success=True)

        except LLMClientError as exc:
            await interaction.followup.send(embed=Embedder.error("AI Error", str(exc)))
            await self._log(interaction, "say", model, success=False, error_message=str(exc))

    # ── /set-model ───────────────────────────────────────────────────

    @app_commands.command(
        name="set-model", description="Set your preferred AI model"
    )
    @app_commands.describe(model="The model to use by default")
    async def set_model_cmd(
        self, interaction: discord.Interaction, model: str
    ) -> None:
        resolved = self.bot.settings.resolve_model(model)
        if resolved not in self.bot.settings.available_models:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Unknown Model",
                    f"`{model}` is not available.\nUse `/models` to see available models.",
                ),
                ephemeral=True,
            )
            return

        await self.bot.database.set_user_model(interaction.user.id, resolved)
        await interaction.response.send_message(
            embed=Embedder.success(
                "Model Updated",
                f"Your preferred model is now `{resolved}`.\nAll future requests will use this model.",
            )
        )

    # ── /models ──────────────────────────────────────────────────────

    @app_commands.command(name="models", description="List available AI models")
    async def models_cmd(self, interaction: discord.Interaction) -> None:
        current = await self._resolve_model(interaction.user.id)
        await interaction.response.send_message(
            embed=Embedder.model_list(self.bot.settings.available_models, current)
        )


async def setup(bot: StarzaiBot) -> None:
    await bot.add_cog(ChatCog(bot))

