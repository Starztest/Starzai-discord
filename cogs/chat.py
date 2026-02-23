"""
Core LLM Chat cog — /chat, /ask, /conversation, /set-model, /models, @mention conversations
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import TYPE_CHECKING, Dict, Optional

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
    "Be helpful, concise, and engaging. Use Discord markdown formatting: "
    "**bold**, *italic*, __underline__, ~~strikethrough~~, `code`, ```code blocks```. "
    "Keep responses natural and conversational. If you don't know something, say so honestly."
)

# Auto-expiry for @mention conversations (10 minutes of inactivity)
MENTION_CONVERSATION_TIMEOUT = 600  # seconds


def _truncate(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    return text[:limit] + "…" if len(text) > limit else text


def _estimate_tokens(text: str) -> int:
    """Rough token estimation: ~4 chars per token for English text."""
    return max(1, len(text) // 4)


class ChatCog(commands.Cog, name="Chat"):
    """Core AI chat features powered by MegaLLM."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot
        # Track @mention conversations: {user_id: {"messages": [...], "last_activity": timestamp}}
        self.mention_conversations: Dict[int, Dict] = {}

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
                # Estimate tokens for streaming response
                estimated_tokens = _estimate_tokens(message + collected)
                await msg.edit(
                    embed=Embedder.chat_response(collected, model, estimated_tokens)
                )
                await self._log(interaction, "chat", model, tokens=estimated_tokens, success=True)
            else:
                # Fallback if stream yielded nothing
                resp = await self.bot.llm.simple_prompt(message, model=model)
                collected = resp.content
                await interaction.followup.send(
                    embed=Embedder.chat_response(
                        resp.content, model, resp.total_tokens, resp.latency_ms
                    )
                )
                await self._log(interaction, "chat", model, tokens=resp.total_tokens, success=True)

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
                # Estimate tokens for streaming response
                estimated_tokens = _estimate_tokens(message + collected)
                await reply.edit(embed=Embedder.chat_response(collected, model, estimated_tokens))
                await self._log(interaction, "say", model, tokens=estimated_tokens, success=True)
            else:
                resp = await self.bot.llm.simple_prompt(message, model=model)
                collected = resp.content
                await interaction.followup.send(
                    embed=Embedder.chat_response(resp.content, model, resp.total_tokens, resp.latency_ms)
                )
                await self._log(interaction, "say", model, tokens=resp.total_tokens, success=True)

            # Save messages to conversation
            await self.bot.database.append_message(
                conv["id"], "user", message, MAX_CONVERSATION_MESSAGES
            )
            await self.bot.database.append_message(
                conv["id"], "assistant", collected, MAX_CONVERSATION_MESSAGES
            )

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
        # Validate explicitly: check if input is a valid model or alias
        settings = self.bot.settings
        model_lower = model.strip().lower()
        
        if model in settings.available_models:
            resolved = model
        elif model_lower in settings.model_aliases:
            resolved = settings.model_aliases[model_lower]
        else:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Unknown Model",
                    f"`{model}` is not available.\nUse `/models` to see available models and aliases.",
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
        view = ModelSelectorView(self.bot, interaction.user.id, current)
        await interaction.response.send_message(
            embed=Embedder.model_list(self.bot.settings.available_models, current),
            view=view,
            ephemeral=True,
        )

    # ── /stop ────────────────────────────────────────────────────────

    @app_commands.command(name="stop", description="Stop your current conversation")
    async def stop_cmd(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        
        # Check both conversation types
        db_conv = await self.bot.database.get_conversation(user_id)
        mention_conv = self.mention_conversations.get(user_id)
        
        if not db_conv and not mention_conv:
            await interaction.response.send_message(
                embed=Embedder.warning(
                    "No Active Conversation",
                    "You don't have any active conversations to stop."
                ),
                ephemeral=True,
            )
            return
        
        # Clear both types
        if db_conv:
            await self.bot.database.clear_conversation(user_id)
        if mention_conv:
            del self.mention_conversations[user_id]
        
        await interaction.response.send_message(
            embed=Embedder.conversation_status(
                "stop",
                "Your conversation has been stopped and cleared."
            ),
            ephemeral=True,
        )

    # ── @mention handler ─────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Handle @mentions for natural conversations."""
        # Ignore bots and DMs
        if message.author.bot or not message.guild:
            return
        
        # Check if bot was mentioned
        if self.bot.user not in message.mentions:
            return
        
        # Remove the mention from the message
        content = message.content
        for mention in message.mentions:
            content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
        content = content.strip()
        
        if not content:
            await message.reply("Hey! You mentioned me but didn't say anything. How can I help? 😊")
            return
        
        user_id = message.author.id
        
        # Rate limiting
        result = self.bot.rate_limiter.check(user_id, message.guild.id, expensive=True)
        if not result.allowed:
            await message.reply(
                embed=Embedder.rate_limited(result.retry_after),
                delete_after=10,
            )
            return
        
        token_result = self.bot.rate_limiter.check_token_budget(user_id, message.guild.id)
        if not token_result.allowed:
            await message.reply(
                embed=Embedder.warning("Token Limit", token_result.reason),
                delete_after=15,
            )
            return
        
        # Clean up expired conversations
        self._cleanup_expired_conversations()
        
        # Get or create conversation
        if user_id not in self.mention_conversations:
            self.mention_conversations[user_id] = {
                "messages": [],
                "last_activity": time.time(),
            }
        
        conv = self.mention_conversations[user_id]
        conv["last_activity"] = time.time()
        
        # Add user message to history
        conv["messages"].append({"role": "user", "content": content})
        
        # Keep only last 10 messages (5 exchanges)
        if len(conv["messages"]) > MAX_CONVERSATION_MESSAGES:
            conv["messages"] = conv["messages"][-MAX_CONVERSATION_MESSAGES:]
        
        # Get user's preferred model
        model = await self._resolve_model(user_id)
        
        # Build messages for API
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(conv["messages"])
        
        # Show typing indicator
        try:
            async with message.channel.typing():
                # Stream the response
                collected = ""
                reply_msg = None
                last_edit = 0
                
                async for chunk in self.bot.llm.chat_stream(messages, model=model):
                    collected += chunk
                    now = time.time()
                    
                    # Only send/edit if we have actual content (not just whitespace)
                    if collected.strip() and now - last_edit >= STREAMING_EDIT_INTERVAL:
                        if not reply_msg:
                            # Send initial message
                            reply_msg = await message.reply(collected[:2000])
                            last_edit = now
                        else:
                            # Update existing message (ignore errors)
                            try:
                                await reply_msg.edit(content=collected[:2000])
                                last_edit = now
                            except discord.HTTPException:
                                # Edit failed, but don't crash - just skip this update
                                pass
                
                # Final update - ensure we send the complete response
                if collected.strip():
                    if reply_msg:
                        # Try to do final edit, if it fails just leave the last successful edit
                        try:
                            await reply_msg.edit(content=collected[:2000])
                        except discord.HTTPException:
                            pass
                    else:
                        # No message sent yet, send it now
                        reply_msg = await message.reply(collected[:2000])
                    
                    # Add assistant response to history
                    conv["messages"].append({"role": "assistant", "content": collected})
                    
                    # Estimate tokens and log
                    estimated_tokens = _estimate_tokens(content + collected)
                    self.bot.rate_limiter.record_tokens(user_id, message.guild.id, estimated_tokens)
                    await self.bot.database.log_usage(
                        user_id=user_id,
                        command="mention",
                        guild_id=message.guild.id,
                        tokens_used=estimated_tokens,
                    )
                else:
                    # If no content was generated, send a fallback message
                    await message.reply("I couldn't generate a response. Please try again.")
                
        except LLMClientError as exc:
            await message.reply(
                embed=Embedder.error("Chat Error", str(exc)),
                delete_after=15,
            )
        except Exception as exc:
            logger.error("Unexpected error in mention handler: %s", exc, exc_info=True)
            await message.reply(
                embed=Embedder.error("Unexpected Error", "Something went wrong. Please try again."),
                delete_after=15,
            )

    def _cleanup_expired_conversations(self) -> None:
        """Remove conversations that have been inactive for too long."""
        now = time.time()
        expired = [
            user_id
            for user_id, conv in self.mention_conversations.items()
            if now - conv["last_activity"] > MENTION_CONVERSATION_TIMEOUT
        ]
        for user_id in expired:
            del self.mention_conversations[user_id]
            logger.info("Expired mention conversation for user %s", user_id)


# ── Model Selector View ──────────────────────────────────────────────

class ModelSelectorView(discord.ui.View):
    """Interactive button view for selecting AI models."""
    
    def __init__(self, bot: StarzaiBot, user_id: int, current_model: str):
        super().__init__(timeout=180)  # 3 minute timeout
        self.bot = bot
        self.user_id = user_id
        self.current_model = current_model
        
        # Add buttons for each model (max 5 per row, max 25 total)
        models = bot.settings.available_models[:25]  # Discord limit
        for i, model in enumerate(models):
            button = discord.ui.Button(
                label=model,
                style=discord.ButtonStyle.primary if model == current_model else discord.ButtonStyle.secondary,
                custom_id=f"model_{model}",
                row=i // 5,  # 5 buttons per row
            )
            button.callback = self._make_callback(model)
            self.add_item(button)
    
    def _make_callback(self, model: str):
        """Create a callback for a specific model button."""
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    "These buttons aren't for you! Use `/models` to get your own.",
                    ephemeral=True,
                )
                return
            
            resolved = self.bot.settings.resolve_model(model)
            await self.bot.database.set_user_model(self.user_id, resolved)
            
            # Update the view to show new selection
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    if item.label == model:
                        item.style = discord.ButtonStyle.primary
                    else:
                        item.style = discord.ButtonStyle.secondary
            
            await interaction.response.edit_message(
                embed=Embedder.success(
                    "Model Updated",
                    f"Your preferred model is now **{resolved}**.\nAll future requests will use this model."
                ),
                view=self,
            )
        
        return callback


async def setup(bot: StarzaiBot) -> None:
    await bot.add_cog(ChatCog(bot))
