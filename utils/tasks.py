"""
Background tasks for maintenance and cleanup.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discord.ext import tasks

if TYPE_CHECKING:
    from bot import StarzaiBot

logger = logging.getLogger(__name__)


class BackgroundTasks:
    """Scheduled background tasks for the bot."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot
        self.cleanup_task.start()

    @tasks.loop(hours=24)
    async def cleanup_task(self):
        """Clean up old messages daily (runs every 24 hours)."""
        if not self.bot.database.is_ready:
            logger.debug("Skipping cleanup — database not available yet")
            return
        try:
            deleted_count = await self.bot.database.cleanup_old_messages(days=30)
            logger.info("🧹 Cleaned up %d old messages (>30 days)", deleted_count)
        except Exception as e:
            logger.error("Error during cleanup task: %s", e, exc_info=True)

    @cleanup_task.before_loop
    async def before_cleanup(self):
        """Wait until bot is ready before starting cleanup task."""
        await self.bot.wait_until_ready()
        logger.info("🕐 Background cleanup task started (runs every 24 hours)")

    def stop(self):
        """Stop all background tasks."""
        self.cleanup_task.cancel()
        logger.info("Background tasks stopped")

