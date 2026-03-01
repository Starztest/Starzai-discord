"""
Dodo — Gamified todo system with XP, streaks, MVP, anti-abuse, and BSD reminders.
Only one slash command: /dodo — everything else is GUI-driven via buttons, dropdowns, and modals.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import re
from datetime import datetime, timedelta, date, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config.constants import (
    BOT_COLOR,
    BOT_ERROR_COLOR,
    BOT_SUCCESS_COLOR,
    DODO_BSD_CHARACTERS,
    DODO_COOK_TIMES,
    DODO_DAILY_XP_CAP,
    DODO_MAX_ACTIVE,
    DODO_MIN_SERVER_AGE_DAYS,
    DODO_PRIORITY_EMOJIS,
    DODO_RED_EXPIRE_PENALTY,
    DODO_RED_MAX_TIMER_HOURS,
    DODO_STREAK_DECAY,
    DODO_STREAK_MILESTONES,
    DODO_STREAK_MULTIPLIER_CAP,
    DODO_STRIKE_COLORS,
    DODO_STRIKE_RULES,
    DODO_XP_VALUES,
)
from utils.embedder import Embedder

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

if TYPE_CHECKING:
    from bot import StarzaiBot

logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _today() -> str:
    return _now().strftime("%Y-%m-%d")

def _week_start() -> str:
    d = _now()
    monday = d - timedelta(days=d.weekday())
    return monday.strftime("%Y-%m-%d")

def _parse_timer(raw: str) -> Optional[timedelta]:
    """Parse timer string like '2h', '30m', '1h30m' into timedelta."""
    if not raw or not raw.strip():
        return None
    raw = raw.strip().lower()
    hours = 0
    minutes = 0
    h_match = re.search(r"(\d+)\s*h", raw)
    m_match = re.search(r"(\d+)\s*m", raw)
    if h_match:
        hours = int(h_match.group(1))
    if m_match:
        minutes = int(m_match.group(1))
    if hours == 0 and minutes == 0:
        try:
            minutes = int(raw)
        except ValueError:
            return None
    return timedelta(hours=hours, minutes=minutes)

def _parse_remind_intervals(raw: str) -> List[str]:
    """Parse comma-separated reminder intervals."""
    if not raw or not raw.strip():
        return []
    return [i.strip() for i in raw.split(",") if i.strip()]

def _format_duration(td: timedelta) -> str:
    total_mins = int(td.total_seconds() / 60)
    if total_mins < 60:
        return f"{total_mins}m"
    hours = total_mins // 60
    mins = total_mins % 60
    return f"{hours}h{mins}m" if mins else f"{hours}h"



# ══════════════════════════════════════════════════════════════════════
#  PERSISTENT VIEWS & COMPONENTS
# ══════════════════════════════════════════════════════════════════════


class TaskThreadView(discord.ui.View):
    """Persistent view attached to each user's task thread embed."""

    def __init__(self, bot: StarzaiBot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Add Task", style=discord.ButtonStyle.green,
        emoji="➕", custom_id="dodo:add_task", row=0,
    )
    async def add_task_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
        if not cog:
            return
        ok, msg = await cog._check_user_eligible(interaction)
        if not ok:
            await interaction.response.send_message(embed=Embedder.error("Not Eligible", msg), ephemeral=True)
            return
        await interaction.response.send_modal(AddTaskModal(self.bot))

    @discord.ui.button(
        label="Check Task", style=discord.ButtonStyle.blurple,
        emoji="✅", custom_id="dodo:check_task", row=0,
    )
    async def check_task_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
        if not cog:
            return
        ok, msg = await cog._check_user_eligible(interaction)
        if not ok:
            await interaction.response.send_message(embed=Embedder.error("Not Eligible", msg), ephemeral=True)
            return
        tasks = await cog._get_user_tasks(interaction.user.id, interaction.guild_id, completed=False)
        if not tasks:
            await interaction.response.send_message(
                embed=Embedder.info("No Tasks", "You don't have any active tasks to check off."), ephemeral=True,
            )
            return
        options = []
        for t in tasks[:25]:
            emoji = DODO_PRIORITY_EMOJIS.get(t["priority"], "⬜")
            label = t["task_text"][:100]
            options.append(discord.SelectOption(label=label, value=str(t["id"]), emoji=emoji))
        view = discord.ui.View(timeout=60)
        dropdown = CheckTaskDropdown(self.bot, options)
        view.add_item(dropdown)
        await interaction.response.send_message("Select a task to check off:", view=view, ephemeral=True)

    @discord.ui.button(
        label="Delete Task", style=discord.ButtonStyle.red,
        emoji="🗑️", custom_id="dodo:delete_task", row=0,
    )
    async def delete_task_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
        if not cog:
            return
        ok, msg = await cog._check_user_eligible(interaction)
        if not ok:
            await interaction.response.send_message(embed=Embedder.error("Not Eligible", msg), ephemeral=True)
            return
        tasks = await cog._get_user_tasks(interaction.user.id, interaction.guild_id, completed=False)
        deletable = [t for t in tasks if t["priority"] != "yellow"]
        if not deletable:
            await interaction.response.send_message(
                embed=Embedder.info("No Deletable Tasks", "No tasks available for deletion. Yellow tasks cannot be deleted."),
                ephemeral=True,
            )
            return
        options = []
        for t in deletable[:25]:
            emoji = DODO_PRIORITY_EMOJIS.get(t["priority"], "⬜")
            label = t["task_text"][:100]
            options.append(discord.SelectOption(label=label, value=str(t["id"]), emoji=emoji))
        view = discord.ui.View(timeout=60)
        dropdown = DeleteTaskDropdown(self.bot, options)
        view.add_item(dropdown)
        await interaction.response.send_message("Select a task to delete:", view=view, ephemeral=True)

    @discord.ui.button(
        label="Summon", style=discord.ButtonStyle.grey,
        emoji="📊", custom_id="dodo:summon", row=1,
    )
    async def summon_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
        if not cog:
            return
        await interaction.response.defer(ephemeral=True)
        img = await cog._generate_leaderboard_image(interaction.guild_id, "daily")
        if img:
            await interaction.followup.send(file=discord.File(img, "leaderboard.png"), ephemeral=True)
        else:
            embed = await cog._build_leaderboard_embed(interaction.guild_id, "daily")
            await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Profile", style=discord.ButtonStyle.grey,
        emoji="🦤", custom_id="dodo:profile", row=1,
    )
    async def profile_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
        if not cog:
            return
        await interaction.response.defer(ephemeral=True)
        img = await cog._generate_profile_card(interaction.user.id, interaction.guild_id)
        if img:
            await interaction.followup.send(file=discord.File(img, "profile.png"), ephemeral=True)
        else:
            embed = await cog._build_profile_embed(interaction.user.id, interaction.guild_id)
            await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Help", style=discord.ButtonStyle.grey,
        emoji="❓", custom_id="dodo:help", row=1,
    )
    async def help_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🦤 Dodo — Help",
            description=(
                "**Dodo** is a gamified todo system!\n\n"
                "**Getting Started**\n"
                "Use `/dodo` to open your task dashboard.\n\n"
                "**Buttons**\n"
                "➕ **Add Task** — Create a new task with priority, timer, and reminders\n"
                "✅ **Check Task** — Complete a task and earn XP\n"
                "🗑️ **Delete Task** — Remove a task (yellow tasks can't be deleted)\n"
                "📊 **Summon** — View the server leaderboard\n"
                "🦤 **Profile** — View your stats card\n\n"
                "**Priority System**\n"
                "🔴 **Red** — Urgent | 30 XP | 60min cook | max 3 | timer required (max 12h)\n"
                "🟡 **Yellow** — Medium | 20 XP | 45min cook | max 10 | can't delete\n"
                "🟢 **Green** — Chill | 10 XP | 20min cook | unlimited\n\n"
                "**Anti-Abuse**\n"
                "• Tasks have a minimum cook time before you can check them\n"
                "• Trying to check too early = public callout + strike\n"
                "• Daily cap of 10 tasks counting toward XP\n"
                "• Must be in server 7+ days to use\n\n"
                "**Streaks & MVP**\n"
                "• Complete tasks daily to build your streak 🔥\n"
                "• XP multiplier grows with streak (caps at 3x)\n"
                "• Daily MVP gets 👑 role + steal shield\n"
                "• Weekly MVP gets perks: XP boost or steal"
            ),
            color=BOT_COLOR,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class MVPPerkView(discord.ui.View):
    """Persistent view DMed to weekly MVP for perk selection."""

    def __init__(self, bot: StarzaiBot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Use XP Boost", style=discord.ButtonStyle.green,
        emoji="⚡", custom_id="dodo:mvp_boost",
    )
    async def boost_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
        if not cog:
            return
        success = await cog._use_mvp_boost(interaction.user.id)
        if success:
            await interaction.response.send_message(
                embed=Embedder.success("XP Boost Activated! ⚡", "Your next completed task will earn **double XP**!"),
            )
        else:
            await interaction.response.send_message(
                embed=Embedder.error("Boost Unavailable", "You don't have a boost available or it has expired."),
                ephemeral=True,
            )

    @discord.ui.button(
        label="Steal XP", style=discord.ButtonStyle.red,
        emoji="💀", custom_id="dodo:mvp_steal",
    )
    async def steal_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
        if not cog:
            return
        # Check if steal is available
        available = await cog._check_steal_available(interaction.user.id)
        if not available:
            await interaction.response.send_message(
                embed=Embedder.error("Steal Unavailable", "You don't have a steal available or it has expired."),
                ephemeral=True,
            )
            return
        # Build target dropdown from active guild members
        guild_id = await cog._get_user_guild(interaction.user.id)
        if not guild_id:
            await interaction.response.send_message(
                embed=Embedder.error("Error", "Could not determine your server."), ephemeral=True,
            )
            return
        targets = await cog._get_steal_targets(guild_id, interaction.user.id)
        if not targets:
            await interaction.response.send_message(
                embed=Embedder.info("No Targets", "No eligible steal targets found."), ephemeral=True,
            )
            return
        options = [
            discord.SelectOption(label=name[:100], value=str(uid))
            for uid, name in targets[:25]
        ]
        view = discord.ui.View(timeout=120)
        view.add_item(StealTargetDropdown(self.bot, options))
        await interaction.response.send_message("Select a target to steal from:", view=view, ephemeral=True)


class ShieldView(discord.ui.View):
    """Persistent view DMed to steal victim if they have a shield."""

    def __init__(self, bot: StarzaiBot, steal_log_id: int = 0):
        super().__init__(timeout=None)
        self.bot = bot
        self.steal_log_id = steal_log_id

    @discord.ui.button(
        label="Use Shield 🛡️", style=discord.ButtonStyle.green,
        custom_id="dodo:use_shield",
    )
    async def use_shield_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
        if not cog:
            return
        success = await cog._use_shield(interaction.user.id)
        if success:
            await interaction.response.send_message(
                embed=Embedder.success("Shield Used! 🛡️", "The steal has been blocked! Your XP is safe."),
            )
        else:
            await interaction.response.send_message(
                embed=Embedder.error("No Shield", "You don't have a shield to use."), ephemeral=True,
            )



# ── Dropdowns ────────────────────────────────────────────────────────


class CheckTaskDropdown(discord.ui.Select):
    """Dropdown for selecting a task to mark complete."""

    def __init__(self, bot: StarzaiBot, options: list):
        super().__init__(
            placeholder="Select a task to check off...",
            options=options,
            custom_id="dodo:check_select",
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
        if not cog:
            return
        task_id = int(self.values[0])
        await cog._handle_check_task(interaction, task_id)


class DeleteTaskDropdown(discord.ui.Select):
    """Dropdown for selecting a task to delete."""

    def __init__(self, bot: StarzaiBot, options: list):
        super().__init__(
            placeholder="Select a task to delete...",
            options=options,
            custom_id="dodo:delete_select",
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
        if not cog:
            return
        task_id = int(self.values[0])
        await cog._handle_delete_task(interaction, task_id)


class StealTargetDropdown(discord.ui.Select):
    """Dropdown for selecting a steal target."""

    def __init__(self, bot: StarzaiBot, options: list):
        super().__init__(
            placeholder="Select a target to steal from...",
            options=options,
            custom_id="dodo:steal_select",
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
        if not cog:
            return
        target_id = int(self.values[0])
        await cog._handle_steal(interaction, target_id)


# ── Modal ────────────────────────────────────────────────────────────


class AddTaskModal(discord.ui.Modal, title="🦤 Add New Task"):
    """Modal for adding a new task."""

    task_name = discord.ui.TextInput(
        label="Task Name",
        placeholder="What do you need to do?",
        max_length=200,
        style=discord.TextStyle.short,
    )
    priority = discord.ui.TextInput(
        label="Priority (r = red, y = yellow, g = green)",
        placeholder="r / y / g",
        max_length=6,
        style=discord.TextStyle.short,
    )
    timer = discord.ui.TextInput(
        label="Timer (required for red, e.g. 2h, 30m)",
        placeholder="e.g. 2h, 1h30m, 45m (optional for yellow/green)",
        required=False,
        max_length=10,
        style=discord.TextStyle.short,
    )
    hide_task = discord.ui.TextInput(
        label="Hide task from others? (y / n)",
        placeholder="y / n",
        default="n",
        max_length=3,
        style=discord.TextStyle.short,
    )
    reminders = discord.ui.TextInput(
        label="Reminder intervals (optional, comma separated)",
        placeholder="e.g. 30m, 1h, 2h",
        required=False,
        max_length=100,
        style=discord.TextStyle.short,
    )

    def __init__(self, bot: StarzaiBot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
        if not cog:
            return

        # Parse priority
        raw_p = self.priority.value.strip().lower()
        priority_map = {"r": "red", "y": "yellow", "g": "green", "red": "red", "yellow": "yellow", "green": "green"}
        priority = priority_map.get(raw_p)
        if not priority:
            await interaction.response.send_message(
                embed=Embedder.error("Invalid Priority", "Use `r` (red), `y` (yellow), or `g` (green)."),
                ephemeral=True,
            )
            return

        # Parse timer
        timer_td = _parse_timer(self.timer.value) if self.timer.value else None
        if priority == "red" and not timer_td:
            await interaction.response.send_message(
                embed=Embedder.error("Timer Required", "Red tasks require a timer (max 12 hours). Example: `2h`, `1h30m`"),
                ephemeral=True,
            )
            return
        if timer_td and timer_td > timedelta(hours=DODO_RED_MAX_TIMER_HOURS):
            await interaction.response.send_message(
                embed=Embedder.error("Timer Too Long", f"Maximum timer is {DODO_RED_MAX_TIMER_HOURS} hours."),
                ephemeral=True,
            )
            return

        # Parse hidden
        is_hidden = self.hide_task.value.strip().lower() in ("y", "yes", "true", "1")

        # Parse reminders
        remind_intervals = _parse_remind_intervals(self.reminders.value) if self.reminders.value else []

        await cog._handle_add_task(
            interaction=interaction,
            task_text=self.task_name.value.strip(),
            priority=priority,
            timer_td=timer_td,
            is_hidden=is_hidden,
            remind_intervals=remind_intervals,
        )


# ══════════════════════════════════════════════════════════════════════
#  MAIN COG
# ══════════════════════════════════════════════════════════════════════


class DodoCog(commands.Cog, name="Dodo"):
    """Gamified todo system — /dodo is the only command, everything else is GUI."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot
        self._boost_cache: dict[int, bool] = {}  # user_id -> has active boost
        self.check_expirations.start()

    def cog_unload(self):
        self.check_expirations.cancel()

    # ── /dodo entry point ────────────────────────────────────────────

    dodo_group = app_commands.Group(name="dodo", description="Dodo todo system")

    @dodo_group.command(name="start", description="Open your Dodo task dashboard")
    async def dodo_cmd(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                embed=Embedder.error("Server Only", "Dodo can only be used in a server."), ephemeral=True,
            )
            return

        ok, msg = await self._check_user_eligible(interaction)
        if not ok:
            await interaction.response.send_message(embed=Embedder.error("Not Eligible", msg), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # Ensure user exists in dodo_users
        await self._ensure_dodo_user(interaction.user.id, interaction.guild_id)

        # Get or create thread + embed
        thread, message = await self._get_or_create_thread(interaction)
        await self._update_thread_embed(interaction.user.id, interaction.guild_id, thread, message)

        await interaction.followup.send(
            embed=Embedder.success("Dashboard Ready! 🦤", f"Your task thread is ready: {thread.mention}"),
            ephemeral=True,
        )

    @dodo_group.command(name="profile", description="View your or another user's Dodo profile")
    @app_commands.describe(user="The user to view (leave empty for yourself)")
    async def profile_cmd(self, interaction: discord.Interaction, user: Optional[discord.Member] = None) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                embed=Embedder.error("Server Only", "Dodo can only be used in a server."), ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        target = user or interaction.user
        img = await self._generate_profile_card(target.id, interaction.guild_id)
        if img:
            await interaction.followup.send(file=discord.File(img, "profile.png"), ephemeral=True)
        else:
            embed = await self._build_profile_embed(target.id, interaction.guild_id)
            await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Eligibility Check ────────────────────────────────────────────

    async def _check_user_eligible(self, interaction: discord.Interaction) -> Tuple[bool, str]:
        """Check if user meets requirements to use Dodo."""
        if not interaction.guild:
            return False, "Dodo can only be used in a server."
        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            return False, "Could not find you in this server."
        if member.joined_at:
            age = (_now() - member.joined_at.replace(tzinfo=timezone.utc)).days
            if age < DODO_MIN_SERVER_AGE_DAYS:
                remaining = DODO_MIN_SERVER_AGE_DAYS - age
                return False, f"You need to be in this server for at least {DODO_MIN_SERVER_AGE_DAYS} days. {remaining} day(s) remaining."
        return True, ""

    # ── Database Helpers ─────────────────────────────────────────────

    async def _ensure_dodo_user(self, user_id: int, guild_id: int) -> None:
        db = self.bot.database.db
        await db.execute(
            "INSERT OR IGNORE INTO dodo_users (user_id, guild_id) VALUES (?, ?)",
            (user_id, guild_id),
        )
        await db.commit()

    async def _get_dodo_user(self, user_id: int, guild_id: int) -> Optional[Dict[str, Any]]:
        db = self.bot.database.db
        async with db.execute(
            "SELECT * FROM dodo_users WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def _get_user_tasks(
        self, user_id: int, guild_id: int, completed: Optional[bool] = None
    ) -> List[Dict[str, Any]]:
        db = self.bot.database.db
        query = "SELECT * FROM dodo_tasks WHERE user_id = ? AND guild_id = ? AND is_expired = 0"
        params: list = [user_id, guild_id]
        if completed is not None:
            query += " AND is_completed = ?"
            params.append(int(completed))
        query += " ORDER BY created_at DESC"
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def _get_task_by_id(self, task_id: int) -> Optional[Dict[str, Any]]:
        db = self.bot.database.db
        async with db.execute("SELECT * FROM dodo_tasks WHERE id = ?", (task_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def _count_active_by_priority(self, user_id: int, guild_id: int, priority: str) -> int:
        db = self.bot.database.db
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM dodo_tasks WHERE user_id = ? AND guild_id = ? AND priority = ? AND is_completed = 0 AND is_expired = 0",
            (user_id, guild_id, priority),
        ) as cur:
            row = await cur.fetchone()
            return row["cnt"] if row else 0

    async def _count_completed_today(self, user_id: int, guild_id: int) -> int:
        db = self.bot.database.db
        today = _today()
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM dodo_tasks WHERE user_id = ? AND guild_id = ? AND is_completed = 1 AND date(completed_at) = ?",
            (user_id, guild_id, today),
        ) as cur:
            row = await cur.fetchone()
            return row["cnt"] if row else 0

    async def _get_strike_count(self, user_id: int, guild_id: int) -> int:
        db = self.bot.database.db
        today = _today()
        async with db.execute(
            "SELECT strike_count FROM dodo_strikes WHERE user_id = ? AND guild_id = ? AND strike_date = ?",
            (user_id, guild_id, today),
        ) as cur:
            row = await cur.fetchone()
            return row["strike_count"] if row else 0


    # ── Thread & Embed System ────────────────────────────────────────

    async def _get_or_create_thread(
        self, interaction: discord.Interaction
    ) -> Tuple[discord.Thread, discord.Message]:
        """Get or create a thread for the user in the dodo-tasks channel."""
        db = self.bot.database.db
        guild_id = interaction.guild_id
        user_id = interaction.user.id

        # Check for existing thread record
        async with db.execute(
            "SELECT thread_id, message_id FROM dodo_threads WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        ) as cur:
            row = await cur.fetchone()

        # Try to use the configured channel, fall back to current channel
        channel_id = self.bot.settings.dodo_tasks_channel_id
        channel = None
        if channel_id:
            channel = self.bot.get_channel(channel_id)
        if not channel:
            channel = interaction.channel

        if row:
            try:
                thread = self.bot.get_channel(row["thread_id"])
                if thread is None:
                    thread = await self.bot.fetch_channel(row["thread_id"])
                if isinstance(thread, discord.Thread):
                    # Unarchive if needed
                    if thread.archived:
                        await thread.edit(archived=False)
                    try:
                        message = await thread.fetch_message(row["message_id"])
                        return thread, message
                    except discord.NotFound:
                        # Message deleted, create new one
                        view = TaskThreadView(self.bot)
                        message = await thread.send(embed=discord.Embed(title="Loading..."), view=view)
                        await db.execute(
                            "UPDATE dodo_threads SET message_id = ? WHERE user_id = ? AND guild_id = ?",
                            (message.id, user_id, guild_id),
                        )
                        await db.commit()
                        return thread, message
            except (discord.NotFound, discord.Forbidden):
                pass  # Thread gone, create new

        # Create new thread
        thread_name = f"🦤 {interaction.user.display_name}'s Tasks"
        if isinstance(channel, discord.TextChannel):
            thread = await channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440,  # 24 hours
            )
        else:
            # Fallback — shouldn't normally happen
            thread = await channel.create_thread(
                name=thread_name,
                auto_archive_duration=1440,
            )

        view = TaskThreadView(self.bot)
        embed = discord.Embed(title="Loading your dashboard...", color=BOT_COLOR)
        message = await thread.send(embed=embed, view=view)

        # Store thread info
        await db.execute(
            "INSERT OR REPLACE INTO dodo_threads (user_id, guild_id, thread_id, message_id) VALUES (?, ?, ?, ?)",
            (user_id, guild_id, thread.id, message.id),
        )
        await db.commit()
        return thread, message

    async def _update_thread_embed(
        self,
        user_id: int,
        guild_id: int,
        thread: discord.Thread,
        message: discord.Message,
    ) -> None:
        """Update the user's thread embed with current task list and stats."""
        user_data = await self._get_dodo_user(user_id, guild_id)
        if not user_data:
            return

        active_tasks = await self._get_user_tasks(user_id, guild_id, completed=False)
        completed_today = await self._count_completed_today(user_id, guild_id)

        xp = user_data["xp"]
        streak = user_data["streak"]
        multiplier = min(1 + (streak * 0.01), DODO_STREAK_MULTIPLIER_CAP)

        # Build rank
        rank = await self._get_user_rank(user_id, guild_id)

        # Build task list
        task_lines = []
        for t in active_tasks:
            emoji = DODO_PRIORITY_EMOJIS.get(t["priority"], "⬜")
            text = t["task_text"] if not t["is_hidden"] else "🔒 Personal Task"
            cook_ready = self._is_cook_time_met(t)
            status = "🍳" if not cook_ready else "✅"
            line = f"{emoji} {status} {text}"
            if t["priority"] == "red" and t["timer_expires"]:
                try:
                    expires = datetime.fromisoformat(t["timer_expires"])
                    remaining = expires - _now()
                    if remaining.total_seconds() > 0:
                        line += f" ⏰ {_format_duration(remaining)}"
                    else:
                        line += " ⏰ **EXPIRED**"
                except (ValueError, TypeError):
                    pass
            task_lines.append(line)

        if not task_lines:
            task_list = "*No active tasks — hit ➕ to add one!*"
        else:
            task_list = "\n".join(task_lines)

        embed = discord.Embed(
            title=f"🦤 Dodo Dashboard",
            description=task_list,
            color=BOT_COLOR,
        )
        embed.add_field(name="⚡ XP", value=f"`{xp:,}`", inline=True)
        embed.add_field(name="🔥 Streak", value=f"`{streak}` days", inline=True)
        embed.add_field(name="📈 Multiplier", value=f"`{multiplier:.2f}x`", inline=True)
        embed.add_field(name="🏆 Rank", value=f"`#{rank}`", inline=True)
        embed.add_field(name="📋 Today", value=f"`{completed_today}/{DODO_DAILY_XP_CAP}` tasks", inline=True)
        embed.set_footer(text=f"🦤 Dodo • Last updated")
        embed.timestamp = _now()

        try:
            view = TaskThreadView(self.bot)
            await message.edit(embed=embed, view=view)
        except discord.HTTPException as e:
            logger.warning("Failed to update thread embed: %s", e)

    async def _get_user_rank(self, user_id: int, guild_id: int) -> int:
        db = self.bot.database.db
        async with db.execute(
            "SELECT COUNT(*) + 1 as rank FROM dodo_users WHERE guild_id = ? AND xp > (SELECT COALESCE(xp, 0) FROM dodo_users WHERE user_id = ? AND guild_id = ?)",
            (guild_id, user_id, guild_id),
        ) as cur:
            row = await cur.fetchone()
            return row["rank"] if row else 1

    def _is_cook_time_met(self, task: Dict[str, Any]) -> bool:
        """Check if the task's cook time has elapsed."""
        try:
            created = datetime.fromisoformat(task["created_at"]).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return True
        cook_mins = task.get("cook_time_mins") or DODO_COOK_TIMES.get(task["priority"], 20)
        return _now() >= created + timedelta(minutes=cook_mins)


    # ── Task Handlers ────────────────────────────────────────────────

    async def _handle_add_task(
        self,
        interaction: discord.Interaction,
        task_text: str,
        priority: str,
        timer_td: Optional[timedelta],
        is_hidden: bool,
        remind_intervals: List[str],
    ) -> None:
        """Handle adding a new task from the modal."""
        user_id = interaction.user.id
        guild_id = interaction.guild_id

        await self._ensure_dodo_user(user_id, guild_id)

        # Check priority limits
        active_count = await self._count_active_by_priority(user_id, guild_id, priority)
        max_allowed = DODO_MAX_ACTIVE.get(priority, 999)
        if active_count >= max_allowed:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Task Limit Reached",
                    f"You already have **{active_count}/{max_allowed}** active {DODO_PRIORITY_EMOJIS[priority]} {priority} tasks."
                ),
                ephemeral=True,
            )
            return

        now = _now()
        cook_time = DODO_COOK_TIMES[priority]
        timer_expires = None
        if timer_td:
            timer_expires = (now + timer_td).isoformat()

        # Calculate first reminder time
        next_remind = None
        remind_enabled = bool(remind_intervals)
        if remind_intervals:
            first_td = _parse_timer(remind_intervals[0])
            if first_td:
                next_remind = (now + first_td).isoformat()

        db = self.bot.database.db
        await db.execute(
            """INSERT INTO dodo_tasks
                (user_id, guild_id, task_text, priority, is_hidden, cook_time_mins,
                 timer_expires, remind_enabled, remind_intervals, next_remind_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id, guild_id, task_text, priority, int(is_hidden), cook_time,
                timer_expires, int(remind_enabled), json.dumps(remind_intervals), next_remind,
            ),
        )
        await db.commit()

        emoji = DODO_PRIORITY_EMOJIS[priority]
        await interaction.response.send_message(
            embed=Embedder.success(
                "Task Added! 🦤",
                f"{emoji} **{task_text}**\n\n"
                f"Priority: **{priority.title()}** | Cook time: **{cook_time}min**"
                + (f" | Timer: **{_format_duration(timer_td)}**" if timer_td else "")
                + (f"\nReminders: {', '.join(remind_intervals)}" if remind_intervals else "")
            ),
            ephemeral=True,
        )

        # Update the thread embed
        await self._refresh_user_embed(user_id, guild_id)

    async def _handle_check_task(self, interaction: discord.Interaction, task_id: int) -> None:
        """Handle checking off a task."""
        task = await self._get_task_by_id(task_id)
        if not task:
            await interaction.response.send_message(
                embed=Embedder.error("Task Not Found", "This task no longer exists."), ephemeral=True,
            )
            return

        if task["user_id"] != interaction.user.id:
            await interaction.response.send_message(
                embed=Embedder.error("Not Your Task", "You can only check off your own tasks."), ephemeral=True,
            )
            return

        if task["is_completed"]:
            await interaction.response.send_message(
                embed=Embedder.info("Already Done", "This task is already completed."), ephemeral=True,
            )
            return

        # Cook time check
        if not self._is_cook_time_met(task):
            # Strike + public callout
            await self._apply_strike(interaction, task)
            return

        # Complete the task
        user_id = interaction.user.id
        guild_id = interaction.guild_id

        db = self.bot.database.db
        now = _now()
        await db.execute(
            "UPDATE dodo_tasks SET is_completed = 1, completed_at = ? WHERE id = ?",
            (now.isoformat(), task_id),
        )
        await db.commit()

        # Grant XP
        xp_earned = await self._grant_xp(user_id, guild_id, task["priority"])

        # Update streak
        await self._update_streak(user_id, guild_id)

        await interaction.response.send_message(
            embed=Embedder.success(
                "Task Complete! ✅",
                f"**{task['task_text']}** is done!\n"
                f"XP earned: **+{xp_earned}** {DODO_PRIORITY_EMOJIS[task['priority']]}"
            ),
            ephemeral=True,
        )

        # Update thread embed
        await self._refresh_user_embed(user_id, guild_id)

        # Check streak milestones
        user_data = await self._get_dodo_user(user_id, guild_id)
        if user_data and user_data["streak"] in DODO_STREAK_MILESTONES:
            await self._announce_streak_milestone(user_id, guild_id, user_data["streak"])

    async def _handle_delete_task(self, interaction: discord.Interaction, task_id: int) -> None:
        """Handle deleting a task."""
        task = await self._get_task_by_id(task_id)
        if not task:
            await interaction.response.send_message(
                embed=Embedder.error("Task Not Found", "This task no longer exists."), ephemeral=True,
            )
            return

        if task["user_id"] != interaction.user.id:
            await interaction.response.send_message(
                embed=Embedder.error("Not Your Task", "You can only delete your own tasks."), ephemeral=True,
            )
            return

        if task["priority"] == "yellow":
            await interaction.response.send_message(
                embed=Embedder.error("Can't Delete Yellow", "🟡 Yellow tasks cannot be deleted once added."),
                ephemeral=True,
            )
            return

        db = self.bot.database.db
        await db.execute("DELETE FROM dodo_tasks WHERE id = ?", (task_id,))
        await db.commit()

        await interaction.response.send_message(
            embed=Embedder.success("Task Deleted 🗑️", f"**{task['task_text']}** has been removed."),
            ephemeral=True,
        )
        await self._refresh_user_embed(interaction.user.id, interaction.guild_id)

    async def _refresh_user_embed(self, user_id: int, guild_id: int) -> None:
        """Refresh the user's thread embed."""
        db = self.bot.database.db
        async with db.execute(
            "SELECT thread_id, message_id FROM dodo_threads WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return

        try:
            thread = self.bot.get_channel(row["thread_id"])
            if thread is None:
                thread = await self.bot.fetch_channel(row["thread_id"])
            if isinstance(thread, discord.Thread):
                if thread.archived:
                    await thread.edit(archived=False)
                message = await thread.fetch_message(row["message_id"])
                await self._update_thread_embed(user_id, guild_id, thread, message)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logger.warning("Failed to refresh embed for user %s: %s", user_id, e)


    # ── Game Logic ───────────────────────────────────────────────────

    async def _grant_xp(self, user_id: int, guild_id: int, priority: str) -> int:
        """Grant XP for completing a task. Returns XP earned."""
        # Daily cap check
        completed_today = await self._count_completed_today(user_id, guild_id)
        if completed_today > DODO_DAILY_XP_CAP:
            return 0

        # Strike penalty check
        strikes = await self._get_strike_count(user_id, guild_id)
        if strikes >= 4:
            return 0
        half_xp = strikes >= 3

        # Base XP
        base_xp = DODO_XP_VALUES.get(priority, 10)

        # Streak multiplier
        user_data = await self._get_dodo_user(user_id, guild_id)
        streak = user_data["streak"] if user_data else 0
        multiplier = min(1 + (streak * 0.01), DODO_STREAK_MULTIPLIER_CAP)

        xp = int(base_xp * multiplier)

        # Boost check
        if self._boost_cache.get(user_id):
            xp *= 2
            self._boost_cache.pop(user_id, None)
            # Mark boost as used in DB
            db = self.bot.database.db
            await db.execute(
                "UPDATE dodo_mvp SET boost_used = 1 WHERE user_id = ? AND boost_available = 1 AND boost_used = 0",
                (user_id,),
            )
            await db.commit()

        if half_xp:
            xp = xp // 2

        # Apply XP
        db = self.bot.database.db
        await db.execute(
            "UPDATE dodo_users SET xp = xp + ? WHERE user_id = ? AND guild_id = ?",
            (xp, user_id, guild_id),
        )
        await db.commit()
        return xp

    async def _update_streak(self, user_id: int, guild_id: int) -> None:
        """Update streak on task completion."""
        db = self.bot.database.db
        user_data = await self._get_dodo_user(user_id, guild_id)
        if not user_data:
            return

        today = _today()
        last_active = user_data.get("last_active")

        if last_active == today:
            return  # Already active today

        if last_active:
            try:
                last_date = datetime.strptime(last_active, "%Y-%m-%d").date()
                today_date = datetime.strptime(today, "%Y-%m-%d").date()
                diff = (today_date - last_date).days
            except ValueError:
                diff = 999

            if diff == 1:
                # Consecutive day — increment streak
                new_streak = user_data["streak"] + 1
            elif diff > 1:
                # Missed day(s) — lose 50% (round down)
                new_streak = max(0, int(user_data["streak"] * DODO_STREAK_DECAY))
            else:
                new_streak = user_data["streak"]
        else:
            new_streak = 1

        await db.execute(
            "UPDATE dodo_users SET streak = ?, last_active = ? WHERE user_id = ? AND guild_id = ?",
            (new_streak, today, user_id, guild_id),
        )
        await db.commit()

    async def _apply_strike(self, interaction: discord.Interaction, task: Dict[str, Any]) -> None:
        """Apply a strike for trying to check a task before cook time."""
        user_id = interaction.user.id
        guild_id = interaction.guild_id
        db = self.bot.database.db
        today = _today()

        # Increment strike
        await db.execute(
            """INSERT INTO dodo_strikes (user_id, guild_id, strike_date, strike_count)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(user_id, guild_id, strike_date)
               DO UPDATE SET strike_count = strike_count + 1""",
            (user_id, guild_id, today),
        )
        await db.commit()

        strike_count = await self._get_strike_count(user_id, guild_id)

        # Calculate remaining cook time
        try:
            created = datetime.fromisoformat(task["created_at"]).replace(tzinfo=timezone.utc)
            cook_mins = task.get("cook_time_mins") or DODO_COOK_TIMES.get(task["priority"], 20)
            ready_at = created + timedelta(minutes=cook_mins)
            remaining = ready_at - _now()
            time_left = _format_duration(remaining) if remaining.total_seconds() > 0 else "soon"
        except (ValueError, TypeError):
            time_left = "unknown"

        # Ephemeral block message
        rule = DODO_STRIKE_RULES.get(strike_count, "zero_xp")
        if rule == "funny":
            desc = f"🍳 Whoa there, speed demon! That task still needs **{time_left}** to cook!\nStrike **{strike_count}/4** today."
        elif rule == "serious":
            desc = f"⚠️ Seriously? Still not ready. **{time_left}** remaining.\nStrike **{strike_count}/4** — next one halves your XP!"
        elif rule == "half_xp":
            desc = f"🔥 That's strike **{strike_count}**. Your XP is **halved** for today.\nTask needs **{time_left}** more."
        else:
            desc = f"💀 Strike **{strike_count}**. **ZERO XP** for you today.\nTask needs **{time_left}** more."

        color = DODO_STRIKE_COLORS.get(min(strike_count, 4), 0x000000)
        embed = discord.Embed(title="🚫 Not So Fast!", description=desc, color=color)
        await interaction.response.send_message(embed=embed, ephemeral=True)

        # Public callout in GC
        gc_channel_id = self.bot.settings.dodo_gc_channel_id
        if gc_channel_id:
            gc_channel = self.bot.get_channel(gc_channel_id)
            if gc_channel:
                callout = discord.Embed(
                    title="🚨 Couch Potato Alert!",
                    description=f"**{interaction.user.display_name}** tried to speed-run their tasks! 🐌\nStrike **{strike_count}** today.",
                    color=color,
                )
                callout.set_footer(text="🦤 Dodo Anti-Abuse")
                try:
                    await gc_channel.send(embed=callout)
                except discord.HTTPException:
                    pass


    # ── MVP & Perk Logic ─────────────────────────────────────────────

    async def _calculate_daily_score(self, user_id: int, guild_id: int) -> int:
        """Calculate daily MVP score: (tasks_completed_today * 10) + xp_earned_today."""
        completed = await self._count_completed_today(user_id, guild_id)
        # XP earned today = sum of XP from tasks completed today
        db = self.bot.database.db
        today = _today()
        async with db.execute(
            """SELECT COUNT(*) as cnt FROM dodo_tasks
               WHERE user_id = ? AND guild_id = ? AND is_completed = 1 AND date(completed_at) = ?""",
            (user_id, guild_id, today),
        ) as cur:
            row = await cur.fetchone()
            tasks_done = row["cnt"] if row else 0
        # Approximate XP earned today
        user_data = await self._get_dodo_user(user_id, guild_id)
        xp = user_data["xp"] if user_data else 0
        return (tasks_done * 10) + xp

    async def _announce_daily_mvp(self) -> None:
        """Announce the daily MVP for each guild."""
        db = self.bot.database.db
        today = _today()

        # Get all guilds with active dodo users
        async with db.execute("SELECT DISTINCT guild_id FROM dodo_users") as cur:
            guilds = [row["guild_id"] for row in await cur.fetchall()]

        for guild_id in guilds:
            try:
                # Find user with highest score today
                async with db.execute(
                    """SELECT user_id, COUNT(*) as tasks_done
                       FROM dodo_tasks
                       WHERE guild_id = ? AND is_completed = 1 AND date(completed_at) = ?
                       GROUP BY user_id
                       ORDER BY tasks_done DESC
                       LIMIT 1""",
                    (guild_id, today),
                ) as cur:
                    row = await cur.fetchone()

                if not row:
                    continue

                winner_id = row["user_id"]
                score = row["tasks_done"]

                # Record MVP
                await db.execute(
                    """INSERT INTO dodo_mvp (guild_id, user_id, mvp_type, expires_at)
                       VALUES (?, ?, 'daily', datetime('now', '+1 day'))""",
                    (guild_id, winner_id),
                )

                # Grant steal shield
                await db.execute(
                    "UPDATE dodo_users SET steal_shield = 1 WHERE user_id = ? AND guild_id = ?",
                    (winner_id, guild_id),
                )
                await db.commit()

                # Assign role
                guild = self.bot.get_guild(guild_id)
                if guild and self.bot.settings.dodo_daily_mvp_role_id:
                    role = guild.get_role(self.bot.settings.dodo_daily_mvp_role_id)
                    if role:
                        # Remove from previous holder
                        for member in role.members:
                            try:
                                await member.remove_roles(role)
                            except discord.HTTPException:
                                pass
                        # Add to winner
                        winner_member = guild.get_member(winner_id)
                        if winner_member:
                            try:
                                await winner_member.add_roles(role)
                            except discord.HTTPException:
                                pass

                # Announce in GC
                gc_channel_id = self.bot.settings.dodo_gc_channel_id
                if gc_channel_id:
                    gc_channel = self.bot.get_channel(gc_channel_id)
                    if gc_channel:
                        img = await self._generate_mvp_announcement_image(winner_id, "daily", score)
                        embed = discord.Embed(
                            title="🦤 Daily MVP — 🏆 of the Day!",
                            description=f"<@{winner_id}> is today's Dodo MVP with **{score}** tasks completed! 👑\nThey earn a **steal shield** 🛡️",
                            color=0xFFD700,
                        )
                        if img:
                            await gc_channel.send(embed=embed, file=discord.File(img, "mvp_daily.png"))
                        else:
                            await gc_channel.send(embed=embed)

            except Exception as e:
                logger.error("Error announcing daily MVP for guild %s: %s", guild_id, e)

    async def _announce_weekly_mvp(self) -> None:
        """Announce the weekly MVP for each guild."""
        db = self.bot.database.db

        async with db.execute("SELECT DISTINCT guild_id FROM dodo_users") as cur:
            guilds = [row["guild_id"] for row in await cur.fetchall()]

        for guild_id in guilds:
            try:
                # User with most XP
                async with db.execute(
                    "SELECT user_id, xp FROM dodo_users WHERE guild_id = ? ORDER BY xp DESC LIMIT 1",
                    (guild_id,),
                ) as cur:
                    row = await cur.fetchone()

                if not row:
                    continue

                winner_id = row["user_id"]
                xp = row["xp"]

                # Record MVP with perks
                now = _now()
                next_sunday = now + timedelta(days=(6 - now.weekday()) % 7 + 7)
                await db.execute(
                    """INSERT INTO dodo_mvp (guild_id, user_id, mvp_type, boost_available, steal_available, expires_at)
                       VALUES (?, ?, 'weekly', 1, 1, ?)""",
                    (guild_id, winner_id, next_sunday.isoformat()),
                )
                await db.commit()

                # Assign role
                guild = self.bot.get_guild(guild_id)
                if guild and self.bot.settings.dodo_weekly_mvp_role_id:
                    role = guild.get_role(self.bot.settings.dodo_weekly_mvp_role_id)
                    if role:
                        for member in role.members:
                            try:
                                await member.remove_roles(role)
                            except discord.HTTPException:
                                pass
                        winner_member = guild.get_member(winner_id)
                        if winner_member:
                            try:
                                await winner_member.add_roles(role)
                            except discord.HTTPException:
                                pass

                # Announce in GC
                gc_channel_id = self.bot.settings.dodo_gc_channel_id
                if gc_channel_id:
                    gc_channel = self.bot.get_channel(gc_channel_id)
                    if gc_channel:
                        img = await self._generate_mvp_announcement_image(winner_id, "weekly", xp)
                        embed = discord.Embed(
                            title="🦤 Weekly MVP — 🏆 of the Week!",
                            description=(
                                f"<@{winner_id}> is this week's Dodo MVP with **{xp:,} XP**! 👑\n\n"
                                "**Perks unlocked:**\n"
                                "⚡ **XP Boost** — Double XP on your next task\n"
                                "💀 **Steal** — Take 30% of someone's daily XP"
                            ),
                            color=0xFFD700,
                        )
                        files = []
                        if img:
                            files.append(discord.File(img, "mvp_weekly.png"))
                        await gc_channel.send(embed=embed, files=files)

                # DM winner with perk view
                try:
                    winner_member = guild.get_member(winner_id) if guild else None
                    if winner_member:
                        view = MVPPerkView(self.bot)
                        dm_embed = discord.Embed(
                            title="🦤 You're the Weekly MVP! 🏆",
                            description=(
                                "Congratulations! You've earned two perks:\n\n"
                                "⚡ **XP Boost** — Double XP on your next completed task\n"
                                "💀 **Steal** — Take 30% of someone's daily XP\n\n"
                                "Use the buttons below before next Sunday!"
                            ),
                            color=0xFFD700,
                        )
                        await winner_member.send(embed=dm_embed, view=view)
                except discord.HTTPException:
                    pass

            except Exception as e:
                logger.error("Error announcing weekly MVP for guild %s: %s", guild_id, e)

    async def _announce_streak_milestone(self, user_id: int, guild_id: int, streak: int) -> None:
        """Announce a streak milestone publicly."""
        gc_channel_id = self.bot.settings.dodo_gc_channel_id
        if not gc_channel_id:
            return
        gc_channel = self.bot.get_channel(gc_channel_id)
        if not gc_channel:
            return

        img = await self._generate_streak_milestone_image(user_id, streak)
        embed = discord.Embed(
            title=f"🔥 Streak Milestone — {streak} Days!",
            description=f"<@{user_id}> has hit a **{streak}-day** streak! 🦤🔥",
            color=0xFF4500,
        )
        try:
            if img:
                await gc_channel.send(embed=embed, file=discord.File(img, "streak.png"))
            else:
                await gc_channel.send(embed=embed)
        except discord.HTTPException:
            pass

    async def _use_mvp_boost(self, user_id: int) -> bool:
        """Activate XP boost for the user."""
        db = self.bot.database.db
        now = _now().isoformat()
        async with db.execute(
            """SELECT id FROM dodo_mvp
               WHERE user_id = ? AND boost_available = 1 AND boost_used = 0
               AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY awarded_at DESC LIMIT 1""",
            (user_id, now),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        self._boost_cache[user_id] = True
        return True

    async def _check_steal_available(self, user_id: int) -> bool:
        db = self.bot.database.db
        now = _now().isoformat()
        async with db.execute(
            """SELECT id FROM dodo_mvp
               WHERE user_id = ? AND steal_available = 1 AND steal_used = 0
               AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY awarded_at DESC LIMIT 1""",
            (user_id, now),
        ) as cur:
            row = await cur.fetchone()
        return row is not None

    async def _get_user_guild(self, user_id: int) -> Optional[int]:
        db = self.bot.database.db
        async with db.execute(
            "SELECT guild_id FROM dodo_users WHERE user_id = ? LIMIT 1", (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return row["guild_id"] if row else None

    async def _get_steal_targets(self, guild_id: int, stealer_id: int) -> List[Tuple[int, str]]:
        """Get eligible steal targets (active dodo users, not the stealer, not already stolen this week)."""
        db = self.bot.database.db
        week = _week_start()
        async with db.execute(
            """SELECT du.user_id FROM dodo_users du
               WHERE du.guild_id = ? AND du.user_id != ?
               AND du.user_id NOT IN (
                   SELECT target_id FROM dodo_steal_log
                   WHERE stealer_id = ? AND week_start = ?
               )
               ORDER BY du.xp DESC LIMIT 25""",
            (guild_id, stealer_id, stealer_id, week),
        ) as cur:
            rows = await cur.fetchall()

        targets = []
        guild = self.bot.get_guild(guild_id)
        for row in rows:
            uid = row["user_id"]
            if guild:
                member = guild.get_member(uid)
                name = member.display_name if member else f"User {uid}"
            else:
                name = f"User {uid}"
            targets.append((uid, name))
        return targets

    async def _handle_steal(self, interaction: discord.Interaction, target_id: int) -> None:
        """Execute steal: take 30% of target's daily XP."""
        stealer_id = interaction.user.id
        db = self.bot.database.db
        now = _now()
        week = _week_start()

        # Validate steal
        available = await self._check_steal_available(stealer_id)
        if not available:
            await interaction.response.send_message(
                embed=Embedder.error("Steal Unavailable", "No steal perk available."), ephemeral=True,
            )
            return

        # Check one-per-target-per-week
        async with db.execute(
            "SELECT id FROM dodo_steal_log WHERE stealer_id = ? AND target_id = ? AND week_start = ?",
            (stealer_id, target_id, week),
        ) as cur:
            if await cur.fetchone():
                await interaction.response.send_message(
                    embed=Embedder.error("Already Stolen", "You've already stolen from this user this week."),
                    ephemeral=True,
                )
                return

        # Get guild_id
        guild_id = await self._get_user_guild(stealer_id)
        if not guild_id:
            await interaction.response.send_message(
                embed=Embedder.error("Error", "Could not determine guild."), ephemeral=True,
            )
            return

        # Check target's shield
        target_data = await self._get_dodo_user(target_id, guild_id)
        if not target_data:
            await interaction.response.send_message(
                embed=Embedder.error("Target Not Found", "This user doesn't exist in Dodo."), ephemeral=True,
            )
            return

        # Calculate 30% of target's XP
        stolen_xp = int(target_data["xp"] * 0.30)
        if stolen_xp <= 0:
            await interaction.response.send_message(
                embed=Embedder.info("Nothing to Steal", "This user has no XP to steal."), ephemeral=True,
            )
            return

        # Execute steal
        await db.execute(
            "UPDATE dodo_users SET xp = xp - ? WHERE user_id = ? AND guild_id = ?",
            (stolen_xp, target_id, guild_id),
        )
        await db.execute(
            "UPDATE dodo_users SET xp = xp + ? WHERE user_id = ? AND guild_id = ?",
            (stolen_xp, stealer_id, guild_id),
        )
        await db.execute(
            "INSERT INTO dodo_steal_log (guild_id, stealer_id, target_id, week_start, stolen_xp) VALUES (?, ?, ?, ?, ?)",
            (guild_id, stealer_id, target_id, week, stolen_xp),
        )
        # Mark steal as used
        await db.execute(
            "UPDATE dodo_mvp SET steal_used = 1, steal_target_id = ? WHERE user_id = ? AND steal_available = 1 AND steal_used = 0",
            (target_id, stealer_id),
        )
        # Using a perk makes you steal-eligible
        await db.execute(
            "UPDATE dodo_users SET steal_shield = 0 WHERE user_id = ? AND guild_id = ?",
            (stealer_id, guild_id),
        )
        await db.commit()

        await interaction.response.send_message(
            embed=Embedder.success(
                "Steal Successful! 💀",
                f"You stole **{stolen_xp:,} XP** from <@{target_id}>!"
            ),
            ephemeral=True,
        )

        # Public announcement
        gc_channel_id = self.bot.settings.dodo_gc_channel_id
        if gc_channel_id:
            gc_channel = self.bot.get_channel(gc_channel_id)
            if gc_channel:
                embed = discord.Embed(
                    title="💀 XP Heist!",
                    description=f"<@{stealer_id}> stole **{stolen_xp:,} XP** from <@{target_id}>!",
                    color=0x8B0000,
                )
                embed.set_footer(text="🦤 Dodo Steal System")
                try:
                    await gc_channel.send(embed=embed)
                except discord.HTTPException:
                    pass

        # DM victim
        guild = self.bot.get_guild(guild_id)
        if guild:
            victim = guild.get_member(target_id)
            if victim:
                try:
                    has_shield = target_data.get("steal_shield", 0)
                    dm_embed = discord.Embed(
                        title="💀 You've Been Robbed!",
                        description=f"<@{stealer_id}> stole **{stolen_xp:,} XP** from you!",
                        color=0x8B0000,
                    )
                    if has_shield:
                        dm_embed.add_field(
                            name="🛡️ Shield Available",
                            value="You have a steal shield! Use the button below to block this steal.",
                        )
                        await victim.send(embed=dm_embed, view=ShieldView(self.bot))
                    else:
                        await victim.send(embed=dm_embed)
                except discord.HTTPException:
                    pass

    async def _use_shield(self, user_id: int) -> bool:
        """Use steal shield to block a steal."""
        db = self.bot.database.db
        # Check shield
        async with db.execute(
            "SELECT steal_shield FROM dodo_users WHERE user_id = ? AND steal_shield = 1",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False

        # Reverse the most recent steal against this user
        async with db.execute(
            "SELECT id, stealer_id, guild_id, stolen_xp FROM dodo_steal_log WHERE target_id = ? ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ) as cur:
            steal = await cur.fetchone()

        if steal:
            # Reverse XP transfer
            await db.execute(
                "UPDATE dodo_users SET xp = xp + ? WHERE user_id = ? AND guild_id = ?",
                (steal["stolen_xp"], user_id, steal["guild_id"]),
            )
            await db.execute(
                "UPDATE dodo_users SET xp = MAX(0, xp - ?) WHERE user_id = ? AND guild_id = ?",
                (steal["stolen_xp"], steal["stealer_id"], steal["guild_id"]),
            )
            await db.execute("DELETE FROM dodo_steal_log WHERE id = ?", (steal["id"],))

        # Consume shield
        await db.execute(
            "UPDATE dodo_users SET steal_shield = 0 WHERE user_id = ?", (user_id,),
        )
        await db.commit()

        # Public announcement
        if steal:
            gc_channel_id = self.bot.settings.dodo_gc_channel_id
            if gc_channel_id:
                gc_channel = self.bot.get_channel(gc_channel_id)
                if gc_channel:
                    embed = discord.Embed(
                        title="🛡️ Shield Activated!",
                        description=f"<@{user_id}> blocked <@{steal['stealer_id']}>'s steal! XP has been returned.",
                        color=0x00FF00,
                    )
                    try:
                        await gc_channel.send(embed=embed)
                    except discord.HTTPException:
                        pass

        return True


    # ── BSD Reminder System ──────────────────────────────────────────

    async def _is_user_active(self, guild_id: int, user_id: int) -> bool:
        """Check if user is currently active in the server."""
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return False
        member = guild.get_member(user_id)
        if not member:
            return False
        return member.status in (discord.Status.online, discord.Status.idle)

    async def _send_bsd_reminder(self, task: Dict[str, Any]) -> None:
        """Send a BSD character reminder for a task."""
        user_id = task["user_id"]
        guild_id = task["guild_id"]
        stage = min(task.get("remind_stage", 0), len(DODO_BSD_CHARACTERS) - 1)
        char = DODO_BSD_CHARACTERS[stage]

        # Generate character message via LLM
        try:
            prompt = (
                f"You are {char['name']} from Bungo Stray Dogs. "
                f"Your tone is: {char['tone']}. "
                f"Write a very short (1-2 sentences) reminder message to someone about their task: \"{task['task_text']}\". "
                f"Stay in character. Don't break the fourth wall. Be creative."
            )
            resp = await self.bot.llm.simple_prompt(
                prompt,
                system=f"You are {char['name']}. Speak in character with the tone: {char['tone']}. Keep it to 1-2 sentences max.",
                max_tokens=150,
            )
            message_text = resp.content.strip()
        except Exception as e:
            logger.warning("LLM failed for BSD reminder: %s", e)
            message_text = f"Hey! Don't forget about your task: **{task['task_text']}**"

        embed = discord.Embed(
            title=f"{char['name']} reminds you...",
            description=message_text,
            color=char["color"],
        )
        embed.set_footer(text=f"🦤 Dodo Reminder • Task: {task['task_text'][:50]}")

        is_active = await self._is_user_active(guild_id, user_id)

        if is_active:
            # Public ping in GC
            gc_channel_id = self.bot.settings.dodo_gc_channel_id
            if gc_channel_id:
                gc_channel = self.bot.get_channel(gc_channel_id)
                if gc_channel:
                    try:
                        msg = await gc_channel.send(
                            content=f"<@{user_id}>",
                            embed=embed,
                            delete_after=10,
                        )
                    except discord.HTTPException:
                        pass
        else:
            # DM
            guild = self.bot.get_guild(guild_id)
            if guild:
                member = guild.get_member(user_id)
                if member:
                    try:
                        await member.send(embed=embed, delete_after=10)
                    except discord.HTTPException:
                        pass

    # ── Background Loop ──────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def check_expirations(self):
        """Background task: red task expiry, reminders, MVP, strike reset."""
        try:
            db = self.bot.database.db
            now = _now()
            now_iso = now.isoformat()

            # 1. Red task expiry
            async with db.execute(
                """SELECT id, user_id, guild_id FROM dodo_tasks
                   WHERE priority = 'red' AND is_completed = 0 AND is_expired = 0
                   AND timer_expires IS NOT NULL AND timer_expires < ?""",
                (now_iso,),
            ) as cur:
                expired_tasks = await cur.fetchall()

            for task in expired_tasks:
                await db.execute(
                    "UPDATE dodo_tasks SET is_expired = 1 WHERE id = ?", (task["id"],),
                )
                # Deduct XP
                await db.execute(
                    "UPDATE dodo_users SET xp = MAX(0, xp - ?) WHERE user_id = ? AND guild_id = ?",
                    (DODO_RED_EXPIRE_PENALTY, task["user_id"], task["guild_id"]),
                )
                await self._refresh_user_embed(task["user_id"], task["guild_id"])
            if expired_tasks:
                await db.commit()

            # 2. BSD reminders
            async with db.execute(
                """SELECT * FROM dodo_tasks
                   WHERE remind_enabled = 1 AND is_completed = 0 AND is_expired = 0
                   AND next_remind_at IS NOT NULL AND next_remind_at <= ?""",
                (now_iso,),
            ) as cur:
                reminder_tasks = [dict(r) for r in await cur.fetchall()]

            for task in reminder_tasks:
                try:
                    await self._send_bsd_reminder(task)
                except Exception as e:
                    logger.error("Error sending BSD reminder for task %s: %s", task["id"], e)

                # Increment stage and set next remind time
                new_stage = task.get("remind_stage", 0) + 1
                intervals = json.loads(task.get("remind_intervals", "[]"))
                if new_stage < len(intervals):
                    next_td = _parse_timer(intervals[new_stage])
                    next_remind = (now + next_td).isoformat() if next_td else None
                else:
                    next_remind = None  # No more reminders

                await db.execute(
                    "UPDATE dodo_tasks SET remind_stage = ?, next_remind_at = ? WHERE id = ?",
                    (new_stage, next_remind, task["id"]),
                )
            if reminder_tasks:
                await db.commit()

            # 3. Daily MVP at midnight UTC
            if now.hour == 0 and now.minute == 0:
                await self._announce_daily_mvp()

            # 4. Weekly MVP on Sunday midnight UTC
            if now.weekday() == 6 and now.hour == 0 and now.minute == 0:
                await self._announce_weekly_mvp()

        except Exception as e:
            logger.error("Error in check_expirations loop: %s", e, exc_info=True)

    @check_expirations.before_loop
    async def before_check_expirations(self):
        await self.bot.wait_until_ready()
        logger.info("🦤 Dodo background task started")


    # ── Pillow Image Generators ──────────────────────────────────────

    def _get_font(self, size: int = 20):
        """Get a font, falling back to default if custom not available."""
        if not HAS_PILLOW:
            return None
        try:
            return ImageFont.truetype("assets/fonts/font.ttf", size)
        except (OSError, IOError):
            try:
                # Try system fonts
                for name in ["DejaVuSans.ttf", "Arial.ttf", "Helvetica.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
                    try:
                        return ImageFont.truetype(name, size)
                    except (OSError, IOError):
                        continue
            except Exception:
                pass
            return ImageFont.load_default()

    async def _generate_profile_card(self, user_id: int, guild_id: int) -> Optional[io.BytesIO]:
        """Generate a profile card image."""
        if not HAS_PILLOW:
            return None

        try:
            user_data = await self._get_dodo_user(user_id, guild_id)
            if not user_data:
                return None

            rank = await self._get_user_rank(user_id, guild_id)
            active_tasks = await self._get_user_tasks(user_id, guild_id, completed=False)
            completed_today = await self._count_completed_today(user_id, guild_id)

            xp = user_data["xp"]
            streak = user_data["streak"]
            multiplier = min(1 + (streak * 0.01), DODO_STREAK_MULTIPLIER_CAP)

            # Get username
            guild = self.bot.get_guild(guild_id)
            member = guild.get_member(user_id) if guild else None
            username = member.display_name if member else f"User {user_id}"

            # Create image
            width, height = 500, 300
            img = Image.new("RGB", (width, height), (30, 30, 46))
            draw = ImageDraw.Draw(img)

            font_large = self._get_font(28)
            font_med = self._get_font(18)
            font_small = self._get_font(14)

            # Title
            draw.text((20, 15), f"🦤 {username}", fill=(255, 255, 255), font=font_large)

            # Stats
            y = 65
            stats = [
                (f"⚡ XP: {xp:,}", (255, 215, 0)),
                (f"🔥 Streak: {streak} days", (255, 100, 50)),
                (f"📈 Multiplier: {multiplier:.2f}x", (100, 200, 255)),
                (f"🏆 Rank: #{rank}", (200, 200, 200)),
                (f"📋 Today: {completed_today}/{DODO_DAILY_XP_CAP} tasks", (150, 255, 150)),
                (f"📝 Active: {len(active_tasks)} tasks", (200, 180, 255)),
            ]
            for text, color in stats:
                draw.text((30, y), text, fill=color, font=font_med)
                y += 32

            # XP Bar
            bar_y = y + 10
            bar_width = 440
            bar_height = 20
            draw.rectangle([(30, bar_y), (30 + bar_width, bar_y + bar_height)], fill=(60, 60, 80))
            level = max(1, xp // 100)
            progress = (xp % 100) / 100.0
            fill_width = int(bar_width * progress)
            if fill_width > 0:
                draw.rectangle([(30, bar_y), (30 + fill_width, bar_y + bar_height)], fill=(155, 89, 182))
            draw.text((30, bar_y + bar_height + 5), f"Level {level} • {xp % 100}/100 to next", fill=(180, 180, 180), font=font_small)

            # Footer
            draw.text((20, height - 25), "🦤 Dodo Todo System", fill=(100, 100, 120), font=font_small)

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return buf

        except Exception as e:
            logger.error("Error generating profile card: %s", e)
            return None

    async def _generate_leaderboard_image(self, guild_id: int, filter_type: str) -> Optional[io.BytesIO]:
        """Generate a leaderboard image."""
        if not HAS_PILLOW:
            return None

        try:
            db = self.bot.database.db
            async with db.execute(
                "SELECT user_id, xp, streak FROM dodo_users WHERE guild_id = ? ORDER BY xp DESC LIMIT 5",
                (guild_id,),
            ) as cur:
                rows = await cur.fetchall()

            if not rows:
                return None

            guild = self.bot.get_guild(guild_id)

            width, height = 500, 60 + len(rows) * 55
            img = Image.new("RGB", (width, height), (30, 30, 46))
            draw = ImageDraw.Draw(img)

            font_large = self._get_font(24)
            font_med = self._get_font(16)
            font_small = self._get_font(13)

            draw.text((20, 10), "🦤 Dodo Leaderboard", fill=(255, 255, 255), font=font_large)

            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
            y = 50
            for i, row in enumerate(rows):
                uid = row["user_id"]
                xp = row["xp"]
                streak = row["streak"]
                member = guild.get_member(uid) if guild else None
                name = member.display_name if member else f"User {uid}"

                medal = medals[i] if i < len(medals) else f"#{i+1}"
                draw.text((20, y), f"{medal} {name}", fill=(255, 255, 255), font=font_med)
                draw.text((20, y + 22), f"   ⚡ {xp:,} XP • 🔥 {streak}d streak", fill=(180, 180, 200), font=font_small)

                # XP bar
                bar_x = 350
                bar_w = 130
                bar_h = 12
                max_xp = rows[0]["xp"] or 1
                fill = max(1, int(bar_w * (xp / max_xp)))
                colors = [(255, 215, 0), (192, 192, 192), (205, 127, 50), (155, 89, 182), (100, 149, 237)]
                color = colors[i] if i < len(colors) else (150, 150, 150)
                draw.rectangle([(bar_x, y + 5), (bar_x + bar_w, y + 5 + bar_h)], fill=(60, 60, 80))
                draw.rectangle([(bar_x, y + 5), (bar_x + fill, y + 5 + bar_h)], fill=color)

                y += 55

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return buf

        except Exception as e:
            logger.error("Error generating leaderboard: %s", e)
            return None

    async def _generate_mvp_announcement_image(self, user_id: int, mvp_type: str, score: int) -> Optional[io.BytesIO]:
        """Generate an MVP announcement image."""
        if not HAS_PILLOW:
            return None

        try:
            guild_id = await self._get_user_guild(user_id)
            guild = self.bot.get_guild(guild_id) if guild_id else None
            member = guild.get_member(user_id) if guild else None
            username = member.display_name if member else f"User {user_id}"

            width, height = 500, 200
            img = Image.new("RGB", (width, height), (40, 20, 10))
            draw = ImageDraw.Draw(img)

            font_large = self._get_font(32)
            font_med = self._get_font(20)
            font_small = self._get_font(14)

            label = "Daily" if mvp_type == "daily" else "Weekly"
            draw.text((20, 15), f"🦤 {label} MVP 🏆", fill=(255, 215, 0), font=font_large)
            draw.text((20, 60), f"👑 {username}", fill=(255, 255, 255), font=font_med)

            metric = "tasks" if mvp_type == "daily" else "XP"
            draw.text((20, 95), f"Score: {score:,} {metric}", fill=(255, 200, 100), font=font_med)

            draw.text((20, height - 30), "🦤 Dodo Todo System", fill=(100, 80, 60), font=font_small)

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return buf

        except Exception as e:
            logger.error("Error generating MVP image: %s", e)
            return None

    async def _generate_streak_milestone_image(self, user_id: int, streak_days: int) -> Optional[io.BytesIO]:
        """Generate a streak milestone celebration image."""
        if not HAS_PILLOW:
            return None

        try:
            guild_id = await self._get_user_guild(user_id)
            guild = self.bot.get_guild(guild_id) if guild_id else None
            member = guild.get_member(user_id) if guild else None
            username = member.display_name if member else f"User {user_id}"

            width, height = 500, 180
            img = Image.new("RGB", (width, height), (50, 20, 0))
            draw = ImageDraw.Draw(img)

            font_large = self._get_font(36)
            font_med = self._get_font(22)
            font_small = self._get_font(14)

            draw.text((20, 15), f"🔥 {streak_days} Day Streak!", fill=(255, 100, 0), font=font_large)
            draw.text((20, 70), f"🦤 {username}", fill=(255, 255, 255), font=font_med)
            draw.text((20, 105), "Keep the flame alive! 🔥🔥🔥", fill=(255, 180, 100), font=font_med)
            draw.text((20, height - 25), "🦤 Dodo Todo System", fill=(100, 60, 30), font=font_small)

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return buf

        except Exception as e:
            logger.error("Error generating streak image: %s", e)
            return None

    # ── Embed Fallbacks ──────────────────────────────────────────────

    async def _build_profile_embed(self, user_id: int, guild_id: int) -> discord.Embed:
        """Build a text-based profile embed (fallback if Pillow unavailable)."""
        user_data = await self._get_dodo_user(user_id, guild_id)
        if not user_data:
            return Embedder.info("No Profile", "This user hasn't used Dodo yet.")

        rank = await self._get_user_rank(user_id, guild_id)
        active_tasks = await self._get_user_tasks(user_id, guild_id, completed=False)
        completed_today = await self._count_completed_today(user_id, guild_id)

        xp = user_data["xp"]
        streak = user_data["streak"]
        multiplier = min(1 + (streak * 0.01), DODO_STREAK_MULTIPLIER_CAP)

        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        username = member.display_name if member else f"User {user_id}"

        embed = discord.Embed(
            title=f"🦤 {username}'s Dodo Profile",
            color=BOT_COLOR,
        )
        embed.add_field(name="⚡ XP", value=f"`{xp:,}`", inline=True)
        embed.add_field(name="🔥 Streak", value=f"`{streak}` days", inline=True)
        embed.add_field(name="📈 Multiplier", value=f"`{multiplier:.2f}x`", inline=True)
        embed.add_field(name="🏆 Rank", value=f"`#{rank}`", inline=True)
        embed.add_field(name="📋 Today", value=f"`{completed_today}/{DODO_DAILY_XP_CAP}`", inline=True)
        embed.add_field(name="📝 Active", value=f"`{len(active_tasks)}`", inline=True)
        if user_data.get("steal_shield"):
            embed.add_field(name="🛡️ Shield", value="Active", inline=True)
        return embed

    async def _build_leaderboard_embed(self, guild_id: int, filter_type: str) -> discord.Embed:
        """Build a text-based leaderboard embed (fallback)."""
        db = self.bot.database.db
        async with db.execute(
            "SELECT user_id, xp, streak FROM dodo_users WHERE guild_id = ? ORDER BY xp DESC LIMIT 10",
            (guild_id,),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return Embedder.info("Empty Leaderboard", "No one has used Dodo in this server yet!")

        guild = self.bot.get_guild(guild_id)
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(rows):
            member = guild.get_member(row["user_id"]) if guild else None
            name = member.display_name if member else f"User {row['user_id']}"
            medal = medals[i] if i < len(medals) else f"`#{i+1}`"
            lines.append(f"{medal} **{name}** — ⚡ {row['xp']:,} XP • 🔥 {row['streak']}d")

        embed = discord.Embed(
            title="🦤 Dodo Leaderboard",
            description="\n".join(lines),
            color=BOT_COLOR,
        )
        return embed


# ══════════════════════════════════════════════════════════════════════
#  SETUP
# ══════════════════════════════════════════════════════════════════════


async def setup(bot: StarzaiBot) -> None:
    await bot.add_cog(DodoCog(bot))
