"""
Core LLM Chat cog — /chat, /ask, /conversation, /set-model, /models, @mention conversations
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
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
from utils.llm_client import LLMClient, LLMClientError
from utils.analysis_view import AnalysisView, create_analysis_embeds, format_full_report
from utils.analysis_helpers import multi_agent_analysis

if TYPE_CHECKING:
    from bot import StarzaiBot

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are Starzai, a friendly and knowledgeable AI assistant on Discord. "
    "Be helpful, concise, and engaging. Use Discord markdown formatting: "
    "**bold**, *italic*, __underline__, ~~strikethrough~~, `code`, ```code blocks```. "
    "Keep responses natural and conversational. If you don't know something, say so honestly."
)

# Extended system prompt template for @mention conversations with server context
MENTION_SYSTEM_PROMPT = (
    "You are {bot_name}, {relationship} to {owner_name}. "
    "Be helpful, concise, and engaging. Use Discord markdown formatting: "
    "**bold**, *italic*, __underline__, ~~strikethrough~~, `code`, ```code blocks```. "
    "Keep responses natural and conversational. If you don't know something, say so honestly.\n\n"
    "Context: You are chatting in the Discord server \"{server_name}\" in the #{channel_name} channel. "
    "The user talking to you is \"{user_display_name}\". "
    "When the user mentions other people by name in their messages, those are real users in the server — "
    "acknowledge them naturally. If recent message context is provided for mentioned users, use it to give "
    "informed responses about their activity, personality, or recent topics they've discussed. "
    "If the user is replying to a message, that context will be provided to help you understand what they're responding to."
)

# Auto-expiry for @mention conversations (10 minutes of inactivity)
MENTION_CONVERSATION_TIMEOUT = 600  # seconds


def _truncate(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    return text[:limit] + "…" if len(text) > limit else text


def _estimate_tokens(text: str) -> int:
    """Rough token estimation: ~4 chars per token for English text."""
    return max(1, len(text) // 4)


class AnalysisOptInView(discord.ui.View):
    """Interactive view for analysis opt-in/opt-out."""
    
    def __init__(self, db_manager, user_id: str, guild_id: str):
        super().__init__(timeout=300)  # 5 minute timeout
        self.database = db_manager
        self.user_id = user_id
        self.guild_id = guild_id
    
    @discord.ui.button(label="✅ Allow Analysis", style=discord.ButtonStyle.success)
    async def allow_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle opt-in button click."""
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "❌ This button is not for you! Use `/allow-analysis` to manage your own settings.",
                ephemeral=True
            )
            return
        
        await self.database.set_analysis_opt_in(self.user_id, self.guild_id, True)
        await interaction.response.edit_message(
            content="✅ **Analysis Enabled!**\n\n"
                    "You can now be analyzed with `/analyze` and `/compare` commands.\n"
                    "Use `/allow-analysis` again anytime to disable.",
            view=None
        )
    
    @discord.ui.button(label="❌ Disable Analysis", style=discord.ButtonStyle.danger)
    async def disable_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle opt-out button click."""
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "❌ This button is not for you! Use `/allow-analysis` to manage your own settings.",
                ephemeral=True
            )
            return
        
        await self.database.set_analysis_opt_in(self.user_id, self.guild_id, False)
        await interaction.response.edit_message(
            content="❌ **Analysis Disabled**\n\n"
                    "You won't be analyzed by `/analyze` or `/compare` commands.\n"
                    "Use `/allow-analysis` again anytime to re-enable.",
            view=None
        )


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
        db_conv = await self.bot.database.get_active_conversation(
            user_id, interaction.guild_id
        )
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
            await self.bot.database.clear_conversation(db_conv["id"])
            await self.bot.database.end_conversation(user_id, interaction.guild_id)
        if mention_conv:
            del self.mention_conversations[user_id]
        
        await interaction.response.send_message(
            embed=Embedder.conversation_status(
                "stop",
                "Your conversation has been stopped and cleared."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="name-bot",
        description="Give your bot a personalized name and relationship"
    )
    @app_commands.describe(
        bot_name="What do you want to call your bot?",
        relationship="What is your bot to you? (e.g., 'my assistant', 'my friend', 'my mentor')"
    )
    async def name_bot(
        self,
        interaction: discord.Interaction,
        bot_name: str,
        relationship: str = "my assistant"
    ) -> None:
        """Set a personalized name and relationship for your bot instance."""
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id) if interaction.guild_id else "0"
        
        # Validate inputs
        if len(bot_name) > 50:
            await interaction.response.send_message(
                "❌ Bot name must be 50 characters or less!",
                ephemeral=True
            )
            return
        
        if len(relationship) > 100:
            await interaction.response.send_message(
                "❌ Relationship description must be 100 characters or less!",
                ephemeral=True
            )
            return
        
        # Store the bot identity
        await self.bot.database.set_bot_identity(user_id, guild_id, bot_name, relationship)
        
        await interaction.response.send_message(
            f"✅ **Bot identity set!**\n\n"
            f"Your bot is now **{bot_name}**, {relationship}.\n"
            f"They'll remember this when you talk to them! 🎭",
            ephemeral=True
        )

    @app_commands.command(
        name="analyze",
        description="🔥 ULTIMATE personality analysis with 5000+ messages & interactive pages"
    )
    @app_commands.describe(
        user="The user to analyze",
        analysis_type="Psychology framework: trait_theory, freudian, jungian, humanistic, cognitive_behavioral, mbti"
    )
    @app_commands.choices(analysis_type=[
        app_commands.Choice(name="🎨 Big Five Traits (Default)", value="trait_theory"),
        app_commands.Choice(name="🔬 Freudian Psychoanalysis", value="freudian"),
        app_commands.Choice(name="🎭 Jungian Psychology", value="jungian"),
        app_commands.Choice(name="🌟 Humanistic (Maslow/Rogers)", value="humanistic"),
        app_commands.Choice(name="🧩 Cognitive-Behavioral", value="cognitive_behavioral"),
        app_commands.Choice(name="💼 MBTI-Style", value="mbti"),
    ])
    async def analyze_user(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        analysis_type: str = "trait_theory"
    ) -> None:
        """Create comprehensive personality analysis with deep message search and pagination."""
        await interaction.response.defer(ephemeral=True)
        
        if user.bot:
            await interaction.followup.send(
                "❌ I can't analyze bots!",
                ephemeral=True
            )
            return
        
        target_user_id = str(user.id)
        guild_id = str(interaction.guild_id) if interaction.guild_id else "0"
        analyzer_id = str(interaction.user.id)
        
        # Check if analyzing self (always allowed) or if target user has opted in
        is_self_analysis = target_user_id == analyzer_id
        if not is_self_analysis:
            has_opted_in = await self.bot.database.get_analysis_opt_in(target_user_id, guild_id)
            if not has_opted_in:
                await interaction.followup.send(
                    f"❌ {user.mention} hasn't enabled analysis yet. "
                    f"They can use `/allow-analysis` to opt in!",
                    ephemeral=True
                )
                return
        
        try:
            # Send initial progress message
            progress_msg = await interaction.followup.send(
                f"🔍 **Starting deep analysis of {user.display_name}...**\n"
                f"⏳ This may take a minute. Searching up to 5000 messages...",
                ephemeral=True,
                wait=True
            )
            
            # Try database first (fast path)
            db_messages = await self.bot.database.search_user_messages(
                target_user_id, guild_id, limit=5000
            )
            
            # If not enough cached messages, search Discord history
            if len(db_messages) < 100:
                messages = await self._deep_message_search(
                    user, interaction.guild, progress_msg, max_messages=5000
                )
            else:
                messages = db_messages
                await progress_msg.edit(
                    content=f"✅ **Found {len(messages):,} cached messages!**\n🧠 Analyzing now..."
                )
            
            if not messages:
                await progress_msg.edit(
                    content=f"❌ No message history found for {user.display_name}. "
                            "They might not have sent many messages yet!"
                )
                return
            
            # Prepare comprehensive analysis prompt
            # Sample messages from different time periods
            # Get model
            model = await self._resolve_model(interaction.user.id)
            
            # Progress callback for multi-agent pipeline
            async def update_progress(status: str):
                try:
                    await progress_msg.edit(content=status)
                except:
                    pass
            
            # Run MULTI-AGENT ANALYSIS PIPELINE
            # This ensures NO EMPTY RESPONSES and high quality for each section
            analysis_data = await multi_agent_analysis(
                self.bot.llm,
                messages,
                user.display_name,
                model,
                analysis_type,
                progress_callback=update_progress
            )
            
            # BONUS: Analyze top 3 channels for channel-specific insights
            await update_progress("🎯 Analyzing top 3 channels...")
            top_channels = self._get_top_channels(messages, top_n=3, guild=interaction.guild)
            if top_channels:
                channel_insights = await self._analyze_top_channels(
                    top_channels, user.display_name, model
                )
                analysis_data["channel_insights"] = channel_insights
            
            # Store the analysis
            date_range = f"Last {len(messages):,} messages"
            await self.bot.database.store_user_analysis(
                target_user_id,
                guild_id,
                analyzer_id,
                analysis_data,
                len(messages),
                date_range
            )
            
            # Create paginated embeds
            pages = create_analysis_embeds(analysis_data, user.display_name, len(messages))
            
            # Create full text report
            full_report = format_full_report(analysis_data, user.display_name, len(messages))
            
            # Create interactive view with re-analysis capability
            async def reanalyze_with_model(inter, new_model, msgs, uname):
                await self._reanalyze_with_model(inter, new_model, msgs, uname, target_user_id, guild_id, analyzer_id)
            
            view = AnalysisView(
                pages, 
                full_report, 
                user.display_name, 
                len(messages),
                analysis_data,
                messages,
                reanalyze_callback=reanalyze_with_model
            )
            
            # Send first page with view
            await progress_msg.edit(
                content=None,
                embed=pages[0],
                view=view
            )
            
            # Store message reference for timeout handling
            view.message = progress_msg
            
        except Exception as e:
            logger.error(f"Error analyzing user: {e}", exc_info=True)
            try:
                await progress_msg.edit(
                    content=f"❌ An error occurred while analyzing {user.display_name}: {str(e)}"
                )
            except:
                await interaction.followup.send(
                    f"❌ An error occurred while analyzing {user.display_name}: {str(e)}",
                    ephemeral=True
                )
    
    def _parse_analysis_response(self, response: str) -> Dict[str, str]:
        """Parse LLM response into structured analysis data."""
        import json
        import re
        
        # Try to extract JSON if present
        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass
        
        # Fallback: parse by section headers
        sections = {
            "overview": "",
            "communication_style": "",
            "personality_traits": "",
            "interests": "",
            "behavioral_patterns": "",
            "activity_patterns": "",
            "social_dynamics": "",
            "vocabulary": "",
            "unique_insights": ""
        }
        
        # Try to extract sections by headers
        for key in sections.keys():
            pattern = rf'"{key}":\s*"([^"]*)"'
            match = re.search(pattern, response, re.IGNORECASE)
            if match:
                sections[key] = match.group(1)
            else:
                # Try without quotes
                pattern = rf'{key}:\s*([^\n]+)'
                match = re.search(pattern, response, re.IGNORECASE)
                if match:
                    sections[key] = match.group(1)
        
        # If still empty, just use the whole response as overview
        if not any(sections.values()):
            sections["overview"] = response[:1000]
        
        return sections

    def _get_top_channels(self, messages: List[Dict[str, Any]], top_n: int = 3, guild: Optional[discord.Guild] = None) -> Dict[str, List[Dict[str, Any]]]:
        """Identify top N most active channels and group messages by channel."""
        from collections import Counter
        
        # Handle both channel_name (from live search) and channel_id (from database)
        channel_counts = Counter()
        for msg in messages:
            if 'channel_name' in msg:
                channel_counts[msg['channel_name']] += 1
            elif 'channel_id' in msg and guild:
                # Resolve channel_id to channel_name
                channel = guild.get_channel(int(msg['channel_id']))
                if channel:
                    channel_counts[channel.name] += 1
                    # Add channel_name to message for consistency
                    msg['channel_name'] = channel.name
        
        top_channels = channel_counts.most_common(top_n)
        
        channel_messages = {}
        for channel_name, count in top_channels:
            channel_messages[channel_name] = [
                msg for msg in messages if msg.get('channel_name') == channel_name
            ]
        
        return channel_messages


    async def _analyze_top_channels(
        self,
        channel_messages: Dict[str, List[Dict[str, Any]]],
        user_name: str,
        model: str
    ) -> str:
        """Run secondary analysis on top channels to get channel-specific insights."""
        channel_insights = []
        
        for channel_name, messages in channel_messages.items():
            sample = "\n".join([f"- {m['content'][:150]}" for m in messages[:20]])
            
            prompt = f"""Analyze {user_name}'s behavior specifically in #{channel_name} based on {len(messages)} messages:

Sample messages from #{channel_name}:
{sample}

Provide a brief 2-3 sentence insight about:
- What topics they discuss in this channel
- How their behavior differs here vs. elsewhere
- Any unique patterns specific to this channel

Be concise and insightful."""

            analysis = ""
            async for chunk in self.bot.llm.chat_stream(
                [{"role": "user", "content": prompt}],
                model=model
            ):
                analysis += chunk
            
            channel_insights.append(f"**#{channel_name}** ({len(messages)} messages)\n{analysis}")
        
        return "\n\n".join(channel_insights)

    async def _reanalyze_with_model(
        self,
        interaction: discord.Interaction,
        model: str,
        messages: List[Dict[str, Any]],
        user_name: str,
        target_user_id: str,
        guild_id: str,
        analyzer_id: str
    ) -> None:
        """Re-analyze user with a different AI model."""
        try:
            sample_size = min(100, len(messages))
            step = max(1, len(messages) // sample_size)
            message_samples = "\n".join([
                f"- {messages[i]['content'][:200]}" 
                for i in range(0, len(messages), step)
            ][:100])
            
            analysis_prompt = f"""Analyze this Discord user comprehensively based on {len(messages):,} messages.

User: {user_name}
Messages: {len(messages):,}

Sample messages (chronologically distributed):
{message_samples}

Provide a DETAILED analysis in the following JSON-like structure:

{{
  "overview": "2-3 sentence high-level summary of who they are",
  "communication_style": "How they express themselves (tone, length, emoji use, formatting). Use bullet points.",
  "personality_traits": "Key personality characteristics observed. Use bullet points with emojis.",
  "interests": "Main topics and interests they discuss. Use bullet points with emojis.",
  "behavioral_patterns": "Notable habits, patterns, or quirks. Use bullet points.",
  "activity_patterns": "When/how often they post, engagement level. Use bullet points.",
  "social_dynamics": "How they interact with others, social role in server. Use bullet points.",
  "vocabulary": "Unique phrases, favorite words, linguistic style. Use bullet points.",
  "unique_insights": "Interesting observations that stand out. Use bullet points with emojis."
}}

Make it insightful, respectful, and engaging. Use Discord markdown formatting (**bold**, *italic*, `code`).
Focus on observable patterns, not judgments. Be specific with examples when possible."""

            analysis_text = ""
            async for chunk in self.bot.llm.chat_stream(
                [{"role": "user", "content": analysis_prompt}],
                model=model
            ):
                analysis_text += chunk
            
            analysis_data = self._parse_analysis_response(analysis_text)
            
            top_channels = self._get_top_channels(messages, top_n=3, guild=interaction.guild)
            if top_channels:
                channel_insights = await self._analyze_top_channels(
                    top_channels, user_name, model
                )
                analysis_data["channel_insights"] = channel_insights
            
            date_range = f"Last {len(messages):,} messages"
            await self.bot.database.store_user_analysis(
                target_user_id,
                guild_id,
                analyzer_id,
                analysis_data,
                len(messages),
                date_range
            )
            
            pages = create_analysis_embeds(analysis_data, user_name, len(messages))
            full_report = format_full_report(analysis_data, user_name, len(messages))
            
            async def reanalyze_again(inter, new_model, msgs, uname):
                await self._reanalyze_with_model(inter, new_model, msgs, uname, target_user_id, guild_id, analyzer_id)
            
            view = AnalysisView(
                pages,
                full_report,
                user_name,
                len(messages),
                analysis_data,
                messages,
                reanalyze_callback=reanalyze_again
            )
            
            await interaction.followup.send(
                content=f"✅ **Re-analyzed with {model}!**",
                embed=pages[0],
                view=view,
                ephemeral=True
            )
            view.message = await interaction.original_response()
            
        except Exception as e:
            logger.error(f"Error re-analyzing: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Error re-analyzing with {model}: {str(e)}",
                ephemeral=True
            )
    @app_commands.guild_only()

    @app_commands.command(
        name="my-stats",
        description="📊 View your own personality analysis and statistics"
    )
    async def my_stats(self, interaction: discord.Interaction) -> None:
        """Quick access to analyze yourself."""
        # Just call analyze_user with the interaction user
        await self.analyze_user(interaction, interaction.user)


    @app_commands.command(
        name="allow-analysis",
        description="🔐 Manage your analysis privacy settings"
    )
    @app_commands.guild_only()
    async def allow_analysis(self, interaction: discord.Interaction) -> None:
        """Allow or disable personality analysis features."""
        # Check current opt-in status
        current_status = await self.bot.database.get_analysis_opt_in(
            str(interaction.user.id),
            str(interaction.guild.id)
        )
        
        status_text = "✅ **Currently Enabled**" if current_status else "❌ **Currently Disabled**"
        
        disclaimer = f"""✨ **Personality Insights**

{status_text}

Want to discover fun insights about your communication style? I can analyze your messages to create a personality profile just for you!

**What I look at:**
• How you express yourself
• Your interests and conversation topics
• When you're most active
• Your unique vocabulary and phrases

**Your control:**
• Completely optional - you choose!
• Toggle on/off whenever you want
• Your messages stay private in this server
• Fun, respectful insights only

**Privacy Note:** This feature uses message content for personality analysis (an approved Discord use case). Only you and those you chat with can be analyzed, and only if you opt in. No data leaves this server.

Curious what I'll discover? 🎨"""
        
        view = AnalysisOptInView(
            self.bot.database,
            str(interaction.user.id),
            str(interaction.guild.id)
        )
        
        await interaction.response.send_message(
            disclaimer,
            view=view,
            ephemeral=True
        )

    @app_commands.command(
        name="compare",
        description="⚖️ Compare two users side-by-side"
    )
    @app_commands.describe(
        user1="First user to compare",
        user2="Second user to compare"
    )
    async def compare_users(
        self,
        interaction: discord.Interaction,
        user1: discord.Member,
        user2: discord.Member
    ) -> None:
        """Compare two users' communication styles and personalities."""
        await interaction.response.defer(ephemeral=True)
        
        if user1.bot or user2.bot:
            await interaction.followup.send(
                "❌ I can't compare bots!",
                ephemeral=True
            )
            return
        
        if user1.id == user2.id:
            await interaction.followup.send(
                "❌ Please select two different users to compare!",
                ephemeral=True
            )
            return
        
        # Check if both users have opted in (unless comparing with self)
        guild_id = str(interaction.guild_id) if interaction.guild_id else "0"
        analyzer_id = str(interaction.user.id)
        
        # Check user1 opt-in (unless it's the analyzer themselves)
        if str(user1.id) != analyzer_id:
            has_opted_in = await self.bot.database.get_analysis_opt_in(str(user1.id), guild_id)
            if not has_opted_in:
                await interaction.followup.send(
                    f"❌ {user1.mention} hasn't enabled analysis yet. "
                    f"They can use `/allow-analysis` to opt in!",
                    ephemeral=True
                )
                return
        
        # Check user2 opt-in (unless it's the analyzer themselves)
        if str(user2.id) != analyzer_id:
            has_opted_in = await self.bot.database.get_analysis_opt_in(str(user2.id), guild_id)
            if not has_opted_in:
                await interaction.followup.send(
                    f"❌ {user2.mention} hasn't enabled analysis yet. "
                    f"They can use `/allow-analysis` to opt in!",
                    ephemeral=True
                )
                return
        
        try:
            progress_msg = await interaction.followup.send(
                f"⚖️ **Comparing {user1.display_name} vs {user2.display_name}...**\n"
                f"⏳ Gathering data from both users...",
                ephemeral=True,
                wait=True
            )
            
            # Get messages for both users
            guild_id = str(interaction.guild_id)
            
            messages1 = await self.bot.database.search_user_messages(
                str(user1.id), guild_id, limit=1000
            )
            messages2 = await self.bot.database.search_user_messages(
                str(user2.id), guild_id, limit=1000
            )
            
            # If not enough cached, search Discord
            if len(messages1) < 50:
                messages1 = await self._deep_message_search(
                    user1, interaction.guild, progress_msg, max_messages=1000
                )
            if len(messages2) < 50:
                await progress_msg.edit(
                    content=f"⚖️ **Comparing {user1.display_name} vs {user2.display_name}...**\n"
                            f"⏳ Gathering data from {user2.display_name}..."
                )
                messages2 = await self._deep_message_search(
                    user2, interaction.guild, progress_msg, max_messages=1000
                )
            
            if not messages1 or not messages2:
                await progress_msg.edit(
                    content=f"❌ Not enough message history for comparison. "
                            f"Need at least 50 messages from each user."
                )
                return
            
            # Prepare comparison prompt
            sample1 = "\n".join([f"- {m['content'][:150]}" for m in messages1[:30]])
            sample2 = "\n".join([f"- {m['content'][:150]}" for m in messages2[:30]])
            
            comparison_prompt = f"""Compare these two Discord users side-by-side:

**User 1: {user1.display_name}** ({len(messages1)} messages)
Sample messages:
{sample1}

**User 2: {user2.display_name}** ({len(messages2)} messages)
Sample messages:
{sample2}

Provide a comparative analysis covering:
1. **Communication Styles**: How do they differ in expression?
2. **Personality Differences**: Key contrasts in personality
3. **Similarities**: What do they have in common?
4. **Activity Levels**: Who's more active/engaged?
5. **Unique Traits**: What makes each one stand out?

Format with bullet points and emojis. Be insightful and respectful."""

            await progress_msg.edit(
                content=f"⚖️ **Comparing {user1.display_name} vs {user2.display_name}...**\n"
                        f"🧠 Analyzing differences and similarities..."
            )
            
            # Get comparison from LLM
            model = await self._resolve_model(interaction.user.id)
            comparison_text = ""
            
            async for chunk in self.bot.llm.chat_stream(
                [{"role": "user", "content": comparison_prompt}],
                model=model
            ):
                comparison_text += chunk
            
            # Create comparison embed
            embed = discord.Embed(
                title=f"⚖️ Comparison: {user1.display_name} vs {user2.display_name}",
                description=comparison_text[:4000],
                color=discord.Color.gold()
            )
            embed.add_field(
                name=f"📊 {user1.display_name}",
                value=f"{len(messages1):,} messages analyzed",
                inline=True
            )
            embed.add_field(
                name=f"📊 {user2.display_name}",
                value=f"{len(messages2):,} messages analyzed",
                inline=True
            )
            embed.set_footer(text="Comparative analysis • Based on message history")
            
            await progress_msg.edit(content=None, embed=embed)
            
        except Exception as e:
            logger.error(f"Error comparing users: {e}", exc_info=True)
            try:
                await progress_msg.edit(
                    content=f"❌ An error occurred during comparison: {str(e)}"
                )
            except:
                await interaction.followup.send(
                    f"❌ An error occurred during comparison: {str(e)}",
                    ephemeral=True
                )

    # ── Helper: deep message search with progress ─────────────────────

    async def _deep_message_search(
        self,
        user: discord.Member,
        guild: discord.Guild,
        progress_message: discord.InteractionMessage,
        max_messages: int = 5000
    ) -> List[Dict[str, Any]]:
        """
        Deep search for user messages across all channels with progress updates.
        
        Args:
            user: The user to search for
            guild: The guild to search in
            progress_message: Message to update with progress
            max_messages: Maximum messages to collect
        
        Returns:
            List of message dictionaries with content, channel_id, timestamp
        """
        messages_found = []
        channels_searched = 0
        total_channels = len([c for c in guild.text_channels if c.permissions_for(guild.me).read_message_history])
        
        for channel in guild.text_channels:
            if len(messages_found) >= max_messages:
                break
            
            try:
                permissions = channel.permissions_for(guild.me)
                if not permissions.read_message_history:
                    continue
                
                channels_searched += 1
                
                # Update progress every 3 channels
                if channels_searched % 3 == 0:
                    try:
                        await progress_message.edit(
                            content=f"🔍 **Searching Discord history...**\n"
                                    f"📊 Progress: {channels_searched}/{total_channels} channels\n"
                                    f"💬 Found: {len(messages_found):,} messages so far..."
                        )
                    except:
                        pass  # Ignore edit failures
                
                # Search this channel
                async for msg in channel.history(limit=1000):
                    if msg.author.id == user.id and msg.content.strip():
                        messages_found.append({
                            'content': msg.content,
                            'channel_id': str(channel.id),
                            'timestamp': msg.created_at.isoformat(),
                            'channel_name': channel.name
                        })
                        if len(messages_found) >= max_messages:
                            break
            
            except (discord.Forbidden, discord.HTTPException):
                continue
        
        # Final progress update
        try:
            await progress_message.edit(
                content=f"✅ **Search complete!**\n"
                        f"📊 Searched: {channels_searched}/{total_channels} channels\n"
                        f"💬 Found: {len(messages_found):,} messages\n"
                        f"🧠 Analyzing now..."
            )
        except:
            pass
        
        return messages_found

    # ── Helper: search messages across server ────────────────────────

    async def _search_user_messages_in_server(
        self, guild: discord.Guild, user_id: int, limit: int = 5
    ) -> list[str]:
        """
        Search for recent messages from a user across all accessible channels in the server.
        Simulates Discord's "from:user" search across the entire server.
        """
        found_messages = []
        
        # Iterate through text channels the bot can read
        for channel in guild.text_channels:
            if len(found_messages) >= limit:
                break
                
            try:
                # Check if bot has permission to read this channel
                permissions = channel.permissions_for(guild.me)
                if not permissions.read_message_history:
                    continue
                
                # Search this channel for user's messages
                async for msg in channel.history(limit=50):
                    if msg.author.id == user_id and not msg.author.bot:
                        content = msg.content.strip()
                        if content and len(content) < 500:
                            found_messages.append(f"[#{channel.name}] {content}")
                            if len(found_messages) >= limit:
                                break
            except (discord.Forbidden, discord.HTTPException):
                continue
        
        return found_messages

    # ── Helper: fetch recent messages from mentioned users ───────────

    async def _get_mentioned_users_context(
        self, message: discord.Message, limit: int = 5, search_server: bool = False
    ) -> str:
        """
        Fetch recent messages from users mentioned in the message using optimized filtering.
        Returns a formatted string with their recent activity.
        
        This simulates Discord's search functionality:
        - search_server=False: "from:user in:channel" (current channel only)
        - search_server=True: "from:user" (across all accessible channels)
        """
        if not message.mentions:
            return ""
        
        # Filter out the bot from mentions
        mentioned_users = [m for m in message.mentions if m.id != self.bot.user.id]
        if not mentioned_users:
            return ""
        
        context_parts = []
        
        # Detect if user is asking for server-wide search based on message content
        content_lower = message.content.lower()
        search_keywords = ["server", "everywhere", "all channels", "anywhere", "overall", "generally"]
        if not search_server and any(keyword in content_lower for keyword in search_keywords):
            search_server = True
        
        try:
            if search_server:
                # Server-wide search: "from:user" across all channels
                for user in mentioned_users[:3]:
                    messages_list = await self._search_user_messages_in_server(
                        message.guild, user.id, limit
                    )
                    if messages_list:
                        context_parts.append(
                            f"\n@{user.display_name}'s recent messages across the server:\n"
                            + "\n".join(f"  - {msg}" for msg in messages_list)
                        )
            else:
                # Channel-only search: "from:user in:channel"
                user_ids = {user.id for user in mentioned_users[:3]}
                
                # Optimized: Fetch messages in bulk and filter in memory
                history_messages = []
                async for msg in message.channel.history(limit=200, before=message):
                    # Filter: only messages from mentioned users, not bots, with content
                    if (msg.author.id in user_ids and 
                        not msg.author.bot and 
                        msg.content.strip() and 
                        len(msg.content) < 500):
                        history_messages.append(msg)
                        # Early exit if we have enough messages for all users
                        if len(history_messages) >= limit * len(user_ids):
                            break
                
                # Group messages by user (simulates "from:user" filtering)
                user_messages = {user_id: [] for user_id in user_ids}
                for msg in history_messages:
                    if len(user_messages[msg.author.id]) < limit:
                        user_messages[msg.author.id].append(msg.content.strip())
                
                # Build context for each user
                for user in mentioned_users[:3]:
                    if user.id in user_messages and user_messages[user.id]:
                        messages_list = user_messages[user.id]
                        context_parts.append(
                            f"\n@{user.display_name}'s recent messages in #{message.channel.name}:\n"
                            + "\n".join(f"  - \"{msg}\"" for msg in messages_list)
                        )
                    
        except discord.Forbidden:
            # No permission to read message history
            logger.warning(f"No permission to read history in channel {message.channel.id}")
            return ""
        except Exception as e:
            logger.error(f"Error fetching message context: {e}", exc_info=True)
            return ""
        
        search_scope = "server-wide" if search_server else f"#{message.channel.name}"
        if context_parts:
            return f"\n\n[Recent activity context - searched {search_scope}]" + "".join(context_parts)
        return ""

    # ── Helper: extract reply context ─────────────────────────────────

    def _get_reply_context(self, message: discord.Message) -> str:
        """
        Extract context from the message being replied to.
        Returns formatted context string if message is a reply.
        """
        if not message.reference or not message.reference.resolved:
            return ""
        
        replied_msg = message.reference.resolved
        if isinstance(replied_msg, discord.DeletedReferencedMessage):
            return ""
        
        # Get the author and content of the replied message
        author_name = replied_msg.author.display_name
        content = replied_msg.content[:200]  # Limit to 200 chars
        
        if not content:
            content = "[attachment or embed]"
        
        return f"\n\n[Replying to @{author_name}: \"{content}\"]"

    # ── Helper: resolve mentions to display names ─────────────────────

    def _resolve_message_mentions(self, message: discord.Message) -> str:
        """
        Process message content to properly handle mentions:
        - Remove the bot's own mention (it's just the trigger)
        - Replace other user mentions with their display names
        - Replace role mentions with role names
        - Replace channel mentions with channel names
        """
        content = message.content
        bot_id = self.bot.user.id

        # Step 1: Replace OTHER users' mentions with their display names
        for mention in message.mentions:
            if mention.id == bot_id:
                continue  # Skip the bot mention — we'll remove it separately
            display_name = mention.display_name
            content = content.replace(f"<@{mention.id}>", f"@{display_name}")
            content = content.replace(f"<@!{mention.id}>", f"@{display_name}")

        # Step 2: Remove the bot's own mention (the conversation trigger)
        content = content.replace(f"<@{bot_id}>", "")
        content = content.replace(f"<@!{bot_id}>", "")

        # Step 3: Resolve role mentions to role names
        for role in message.role_mentions:
            content = content.replace(f"<@&{role.id}>", f"@{role.name}")

        # Step 4: Resolve channel mentions to channel names
        if message.guild:
            channel_pattern = re.compile(r"<#(\d+)>")
            for match in channel_pattern.finditer(content):
                channel_id = int(match.group(1))
                channel = message.guild.get_channel(channel_id)
                if channel:
                    content = content.replace(match.group(0), f"#{channel.name}")

        return content.strip()

    # ── @mention handler ─────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Handle @mentions for natural conversations and log messages for analysis."""
        # Ignore DMs
        if not message.guild:
            return

        # Ignore bots for conversation handling
        if message.author.bot:
            return
        
        # Check if bot was mentioned
        if self.bot.user not in message.mentions:
            return
        
        # Resolve mentions: strip bot mention, convert user/role/channel mentions to names
        content = self._resolve_message_mentions(message)
        
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
        
        # Get bot identity for this user (personalized name/relationship)
        bot_identity = await self.bot.database.get_bot_identity(
            str(user_id), str(message.guild.id)
        )
        bot_name = bot_identity["bot_name"] if bot_identity else "Starzai"
        relationship = bot_identity["relationship"] if bot_identity else "a friendly AI assistant"
        
        # Fetch reply context if this is a reply
        reply_context = self._get_reply_context(message)
        
        # Fetch recent messages from mentioned users for context
        mentioned_context = await self._get_mentioned_users_context(message, limit=5)
        
        # Add user message to history (with all context if available)
        user_message_content = content
        if reply_context:
            user_message_content += reply_context
        if mentioned_context:
            user_message_content += mentioned_context
        
        conv["messages"].append({"role": "user", "content": user_message_content})
        
        # Keep only last 10 messages (5 exchanges)
        if len(conv["messages"]) > MAX_CONVERSATION_MESSAGES:
            conv["messages"] = conv["messages"][-MAX_CONVERSATION_MESSAGES:]
        
        # Get user's preferred model
        model = await self._resolve_model(user_id)
        
        # Build messages for API with server context and bot identity
        system_prompt = MENTION_SYSTEM_PROMPT.format(
            bot_name=bot_name,
            relationship=relationship,
            owner_name=message.author.display_name,
            server_name=message.guild.name,
            channel_name=message.channel.name if hasattr(message.channel, 'name') else "DM",
            user_display_name=message.author.display_name,
        )
        messages = [{"role": "system", "content": system_prompt}]
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
                    self.bot.rate_limiter.record_tokens(user_id, estimated_tokens, message.guild.id)
                    await self.bot.database.add_user_tokens(user_id, estimated_tokens)
                    await self.bot.database.log_usage(
                        user_id=user_id,
                        command="mention",
                        guild_id=message.guild.id,
                        model=model,
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
