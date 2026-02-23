"""
Starzai Discord Bot — Main entry point.
Loads configuration, initializes services, and starts the bot.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

import discord
from aiohttp import web
from discord.ext import commands

from config.settings import Settings
from utils.db_manager import DatabaseManager
from utils.embedder import Embedder
from utils.llm_client import LLMClient
from utils.rate_limiter import RateLimiter
from utils.tasks import BackgroundTasks

# ── Logging ──────────────────────────────────────────────────────────
settings = Settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s │ %(levelname)-8s │ %(name)-20s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("starzai")

# ── Cog list ─────────────────────────────────────────────────────────
COGS = [
    "cogs.chat",
    "cogs.translator",
    "cogs.etymology",
    "cogs.grammar",
    "cogs.astrology",
    "cogs.personality",
    "cogs.files",
    "cogs.games",
    "cogs.admin",
    "cogs.privacy",
]


# ── Bot subclass ─────────────────────────────────────────────────────
class StarzaiBot(commands.Bot):
    """Custom Bot with shared services attached."""

    def __init__(self, settings: Settings):
        # Enable all intents for full functionality
        intents = discord.Intents.default()
        
        # PRIVILEGED INTENTS (must be enabled in Discord Developer Portal)
        intents.message_content = True  # Read message content for personalization
        intents.members = True          # Read member list and usernames
        intents.presences = True        # See user status and activity
        
        # Additional useful intents
        intents.guilds = True           # Guild (server) events
        intents.messages = True         # Message events
        intents.reactions = True        # Reaction events
        intents.typing = True           # Typing indicators

        super().__init__(
            command_prefix="!",  # Slash commands are primary
            intents=intents,
            application_id=settings.application_id,
        )

        self.settings = settings
        self.llm = LLMClient(
            api_key=settings.megallm_api_key,
            base_url=settings.megallm_base_url,
            default_model=settings.default_model,
        )
        self.rate_limiter = RateLimiter(
            user_limit=settings.rate_limit_per_user,
            global_limit=settings.rate_limit_global,
            daily_token_limit_user=settings.daily_token_limit_user,
            daily_token_limit_server=settings.daily_token_limit_server,
        )
        self.database = DatabaseManager()
        self.background_tasks = None  # Will be initialized after setup
        self._health_runner: Optional[web.AppRunner] = None

    # ── Startup ──────────────────────────────────────────────────────

    async def setup_hook(self) -> None:
        """Called once when the bot starts. Load cogs and init services."""
        logger.info("Running setup_hook…")

        # Database
        await self.database.initialize()

        # Load cogs
        for cog_path in COGS:
            try:
                await self.load_extension(cog_path)
                logger.info("Loaded cog: %s", cog_path)
            except Exception as exc:
                logger.error("Failed to load cog %s: %s", cog_path, exc)

        # Sync slash commands
        try:
            synced = await self.tree.sync()
            logger.info("Synced %d slash commands", len(synced))
        except Exception as exc:
            logger.error("Failed to sync commands: %s", exc)

        # Start health-check HTTP server for Railway
        await self._start_health_server()
        
        # Start background tasks
        self.background_tasks = BackgroundTasks(self)

    async def on_ready(self) -> None:
        logger.info("✨ %s is online! Guilds: %d", self.user, len(self.guilds))
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="/chat • AI powered ✨",
            )
        )

    async def on_message(self, message: discord.Message) -> None:
        """Track user messages for personalization."""
        # Ignore bot messages
        if message.author.bot:
            return

        # Ignore DMs (no guild)
        if not message.guild:
            return

        # Store message for context
        try:
            await self.database.store_user_message(
                user_id=str(message.author.id),
                guild_id=str(message.guild.id),
                channel_id=str(message.channel.id),
                content=message.content,
            )

            # Update user context every 5 messages
            if message.author.id % 5 == 0:  # Simple modulo check
                recent = await self.database.get_recent_messages(
                    str(message.author.id), str(message.guild.id), limit=20
                )
                await self.database.update_user_context(
                    str(message.author.id), str(message.guild.id), recent
                )
        except Exception as e:
            logger.error(f"Error storing message: {e}", exc_info=True)

        # Process commands (important for prefix commands if any)
        await self.process_commands(message)

    # ── Shutdown ─────────────────────────────────────────────────────

    async def close(self) -> None:
        logger.info("Shutting down…")
        if self.background_tasks:
            self.background_tasks.stop()
        await self.llm.close()
        await self.database.close()
        if self._health_runner:
            await self._health_runner.cleanup()
        await super().close()

    # ── Global Error Handler ─────────────────────────────────────────

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                embed=Embedder.error("Permission Denied", str(error))
            )
            return
        logger.error("Unhandled command error: %s", error, exc_info=error)
        await ctx.send(
            embed=Embedder.error(
                "Something went wrong",
                "An unexpected error occurred. Please try again later.",
            )
        )

    # ── Health Check Server ──────────────────────────────────────────

    async def _start_health_server(self) -> None:
        """Start a tiny HTTP server so Railway knows the bot is alive."""
        app = web.Application()
        app.router.add_get("/", self._health_handler)
        app.router.add_get("/health", self._health_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.settings.port)
        await site.start()
        self._health_runner = runner
        logger.info("Health-check server listening on port %d", self.settings.port)

    async def _health_handler(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "bot": str(self.user),
                "guilds": len(self.guilds),
                "latency_ms": round(self.latency * 1000, 2),
            }
        )


# ── Entry Point ──────────────────────────────────────────────────────
def main() -> None:
    errors = settings.validate()
    if errors:
        for e in errors:
            logger.critical("CONFIG ERROR: %s", e)
        sys.exit(1)

    bot = StarzaiBot(settings)

    try:
        bot.run(settings.discord_token, log_handler=None)
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as exc:
        logger.critical("Bot crashed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
