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
    DODO_COOLDOWN_MINUTES,
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


def _sql_dt(dt: datetime) -> str:
    """Format a datetime for SQLite comparison — matches datetime('now') format."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def _sql_now() -> str:
    """Current UTC time in SQLite-compatible format."""
    return _sql_dt(_now())

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
#  INTERACTION ERROR HELPERS
# ══════════════════════════════════════════════════════════════════════

_COG_MISSING_MSG = "Dodo module is reloading — try again in a moment."


async def _safe_error_response(interaction: discord.Interaction, title: str, msg: str) -> None:
    """Send an ephemeral error, handling both fresh and already-acked interactions."""
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=Embedder.error(title, msg), ephemeral=True)
        else:
            await interaction.followup.send(embed=Embedder.error(title, msg), ephemeral=True)
    except discord.HTTPException:
        pass  # interaction expired, nothing we can do


# ══════════════════════════════════════════════════════════════════════
#  PERSISTENT VIEWS & COMPONENTS
# ══════════════════════════════════════════════════════════════════════


class TaskThreadView(discord.ui.View):
    """Persistent view attached to each user's task thread embed."""

    def __init__(self, bot: StarzaiBot):
        super().__init__(timeout=None)
        self.bot = bot

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception("TaskThreadView error on %s", getattr(item, "custom_id", item))
        await _safe_error_response(interaction, "Something Went Wrong", "An unexpected error occurred. Please try again.")

    @discord.ui.button(
        label="Add Task", style=discord.ButtonStyle.green,
        emoji="➕", custom_id="dodo:add_task", row=0,
    )
    async def add_task_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
            if not cog:
                await _safe_error_response(interaction, "Unavailable", _COG_MISSING_MSG)
                return
            ok, msg = await cog._check_user_eligible(interaction)
            if not ok:
                await interaction.response.send_message(embed=Embedder.error("Not Eligible", msg), ephemeral=True)
                return
            # Thread-owner access control
            if interaction.channel and interaction.guild:
                thread_owner = await cog._get_thread_owner(interaction.channel_id, interaction.guild_id)
                if thread_owner is not None and interaction.user.id != thread_owner:
                    await interaction.response.send_message(
                        embed=Embedder.error(
                            "Not Your Dashboard",
                            "This isn't your task dashboard! Use `/dodo start` to open yours."
                        ),
                        ephemeral=True,
                    )
                    return
            await interaction.response.send_modal(AddTaskModal(self.bot))
        except Exception as exc:
            logger.exception("add_task_btn failed: %s", exc)
            await _safe_error_response(interaction, "Something Went Wrong", "Could not open the task form. Please try again.")

    @discord.ui.button(
        label="Check Task", style=discord.ButtonStyle.blurple,
        emoji="✅", custom_id="dodo:check_task", row=0,
    )
    async def check_task_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
            if not cog:
                await _safe_error_response(interaction, "Unavailable", _COG_MISSING_MSG)
                return
            ok, msg = await cog._check_user_eligible(interaction)
            if not ok:
                await interaction.response.send_message(embed=Embedder.error("Not Eligible", msg), ephemeral=True)
                return
            # Thread-owner access control
            if interaction.channel and interaction.guild:
                thread_owner = await cog._get_thread_owner(interaction.channel_id, interaction.guild_id)
                if thread_owner is not None and interaction.user.id != thread_owner:
                    await interaction.response.send_message(
                        embed=Embedder.error(
                            "Not Your Dashboard",
                            "This isn't your task dashboard! Use `/dodo start` to open yours."
                        ),
                        ephemeral=True,
                    )
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
        except Exception as exc:
            logger.exception("check_task_btn failed: %s", exc)
            await _safe_error_response(interaction, "Something Went Wrong", "Could not load tasks. Please try again.")

    @discord.ui.button(
        label="Delete Task", style=discord.ButtonStyle.red,
        emoji="🗑️", custom_id="dodo:delete_task", row=0,
    )
    async def delete_task_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
            if not cog:
                await _safe_error_response(interaction, "Unavailable", _COG_MISSING_MSG)
                return
            ok, msg = await cog._check_user_eligible(interaction)
            if not ok:
                await interaction.response.send_message(embed=Embedder.error("Not Eligible", msg), ephemeral=True)
                return
            # Thread-owner access control
            if interaction.channel and interaction.guild:
                thread_owner = await cog._get_thread_owner(interaction.channel_id, interaction.guild_id)
                if thread_owner is not None and interaction.user.id != thread_owner:
                    await interaction.response.send_message(
                        embed=Embedder.error(
                            "Not Your Dashboard",
                            "This isn't your task dashboard! Use `/dodo start` to open yours."
                        ),
                        ephemeral=True,
                    )
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
        except Exception as exc:
            logger.exception("delete_task_btn failed: %s", exc)
            await _safe_error_response(interaction, "Something Went Wrong", "Could not load tasks. Please try again.")

    @discord.ui.button(
        label="Summon", style=discord.ButtonStyle.grey,
        emoji="📊", custom_id="dodo:summon", row=1,
    )
    async def summon_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
            if not cog:
                await _safe_error_response(interaction, "Unavailable", _COG_MISSING_MSG)
                return
            await interaction.response.defer(ephemeral=True)
            img = await cog._generate_leaderboard_image(interaction.guild_id, "daily")
            if img:
                await interaction.followup.send(file=discord.File(img, "leaderboard.png"), ephemeral=True)
            else:
                embed = await cog._build_leaderboard_embed(interaction.guild_id, "daily")
                await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.exception("summon_btn failed: %s", exc)
            await _safe_error_response(interaction, "Something Went Wrong", "Could not load leaderboard. Please try again.")

    @discord.ui.button(
        label="Profile", style=discord.ButtonStyle.grey,
        emoji="🦤", custom_id="dodo:profile", row=1,
    )
    async def profile_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
            if not cog:
                await _safe_error_response(interaction, "Unavailable", _COG_MISSING_MSG)
                return
            await interaction.response.defer(ephemeral=True)
            img = await cog._generate_profile_card(interaction.user.id, interaction.guild_id)
            if img:
                await interaction.followup.send(file=discord.File(img, "profile.png"), ephemeral=True)
            else:
                embed = await cog._build_profile_embed(interaction.user.id, interaction.guild_id)
                await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.exception("profile_btn failed: %s", exc)
            await _safe_error_response(interaction, "Something Went Wrong", "Could not load profile. Please try again.")

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
                "🔴 **Red** — Urgent | 30 XP | max 3 | timer required (max 12h)\n"
                "🟡 **Yellow** — Medium | 20 XP | max 10 | can't delete\n"
                "🟢 **Green** — Chill | 10 XP | unlimited\n\n"
                "**Cooldown System**\n"
                "• Each task unlocks one check permission after **5 min**\n"
                "• No checks available = soft block (no strikes)\n"
                "• Daily cap of 10 tasks counting toward XP\n"
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

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception("MVPPerkView error on %s", getattr(item, "custom_id", item))
        await _safe_error_response(interaction, "Something Went Wrong", "An unexpected error occurred. Please try again.")

    @discord.ui.button(
        label="Use XP Boost", style=discord.ButtonStyle.green,
        emoji="⚡", custom_id="dodo:mvp_boost",
    )
    async def boost_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
            if not cog:
                await _safe_error_response(interaction, "Unavailable", _COG_MISSING_MSG)
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
        except Exception as exc:
            logger.exception("boost_btn failed: %s", exc)
            await _safe_error_response(interaction, "Something Went Wrong", "Could not activate boost. Please try again.")

    @discord.ui.button(
        label="Steal XP", style=discord.ButtonStyle.red,
        emoji="💀", custom_id="dodo:mvp_steal",
    )
    async def steal_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
            if not cog:
                await _safe_error_response(interaction, "Unavailable", _COG_MISSING_MSG)
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
        except Exception as exc:
            logger.exception("steal_btn failed: %s", exc)
            await _safe_error_response(interaction, "Something Went Wrong", "Could not initiate steal. Please try again.")


class ShieldView(discord.ui.View):
    """Persistent view DMed to steal victim if they have a shield."""

    def __init__(self, bot: StarzaiBot, steal_log_id: int = 0):
        super().__init__(timeout=None)
        self.bot = bot
        self.steal_log_id = steal_log_id

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception("ShieldView error on %s", getattr(item, "custom_id", item))
        await _safe_error_response(interaction, "Something Went Wrong", "An unexpected error occurred. Please try again.")

    @discord.ui.button(
        label="Use Shield 🛡️", style=discord.ButtonStyle.green,
        custom_id="dodo:use_shield",
    )
    async def use_shield_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
            if not cog:
                await _safe_error_response(interaction, "Unavailable", _COG_MISSING_MSG)
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
        except Exception as exc:
            logger.exception("use_shield_btn failed: %s", exc)
            await _safe_error_response(interaction, "Something Went Wrong", "Could not use shield. Please try again.")



# ── Dropdowns ────────────────────────────────────────────────────────


class CheckTaskDropdown(discord.ui.Select):
    """Dropdown for selecting a task to mark complete."""

    def __init__(self, bot: StarzaiBot, options: list):
        super().__init__(
            placeholder="Select a task to check off...",
            options=options,
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        try:
            cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
            if not cog:
                await _safe_error_response(interaction, "Unavailable", _COG_MISSING_MSG)
                return
            task_id = int(self.values[0])
            await cog._handle_check_task(interaction, task_id)
        except Exception as exc:
            logger.exception("CheckTaskDropdown callback failed: %s", exc)
            await _safe_error_response(interaction, "Something Went Wrong", "Could not check off task. Please try again.")


class DeleteTaskDropdown(discord.ui.Select):
    """Dropdown for selecting a task to delete."""

    def __init__(self, bot: StarzaiBot, options: list):
        super().__init__(
            placeholder="Select a task to delete...",
            options=options,
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        try:
            cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
            if not cog:
                await _safe_error_response(interaction, "Unavailable", _COG_MISSING_MSG)
                return
            task_id = int(self.values[0])
            await cog._handle_delete_task(interaction, task_id)
        except Exception as exc:
            logger.exception("DeleteTaskDropdown callback failed: %s", exc)
            await _safe_error_response(interaction, "Something Went Wrong", "Could not delete task. Please try again.")


class StealTargetDropdown(discord.ui.Select):
    """Dropdown for selecting a steal target."""

    def __init__(self, bot: StarzaiBot, options: list):
        super().__init__(
            placeholder="Select a target to steal from...",
            options=options,
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        try:
            cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
            if not cog:
                await _safe_error_response(interaction, "Unavailable", _COG_MISSING_MSG)
                return
            target_id = int(self.values[0])
            await cog._handle_steal(interaction, target_id)
        except Exception as exc:
            logger.exception("StealTargetDropdown callback failed: %s", exc)
            await _safe_error_response(interaction, "Something Went Wrong", "Could not execute steal. Please try again.")


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
        label="Reminders (optional, comma-separated)",
        placeholder="e.g. 30m, 1h, 2h | Dazai",
        required=False,
        max_length=100,
        style=discord.TextStyle.short,
    )

    def __init__(self, bot: StarzaiBot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        try:
            cog: Optional[DodoCog] = self.bot.get_cog("Dodo")
            if not cog:
                await _safe_error_response(interaction, "Unavailable", _COG_MISSING_MSG)
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

            # Parse reminders and optional character choice
            remind_intervals = []
            remind_character = None
            if self.reminders.value:
                raw_remind = self.reminders.value.strip()
                if "|" in raw_remind:
                    parts = raw_remind.split("|", 1)
                    interval_part = parts[0].strip()
                    char_part = parts[1].strip().lower()
                    remind_intervals = _parse_remind_intervals(interval_part) if interval_part else []
                    # Fuzzy match against BSD characters
                    for char_entry in DODO_BSD_CHARACTERS:
                        char_name = char_entry["name"].split(" ")[0].lower()  # e.g. "atsushi"
                        if char_part == char_name or char_part in char_entry["name"].lower():
                            remind_character = char_entry["name"]
                            break
                else:
                    remind_intervals = _parse_remind_intervals(raw_remind)

            await cog._handle_add_task(
                interaction=interaction,
                task_text=self.task_name.value.strip(),
                priority=priority,
                timer_td=timer_td,
                is_hidden=is_hidden,
                remind_intervals=remind_intervals,
                remind_character=remind_character,
            )
        except Exception as exc:
            logger.exception("AddTaskModal on_submit failed: %s", exc)
            await _safe_error_response(interaction, "Something Went Wrong", "Could not add your task. Please try again.")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("AddTaskModal error: %s", error)
        await _safe_error_response(interaction, "Something Went Wrong", "An unexpected error occurred. Please try again.")


# ══════════════════════════════════════════════════════════════════════
#  MAIN COG
# ══════════════════════════════════════════════════════════════════════


class DodoCog(commands.Cog, name="Dodo"):
    """Gamified todo system — /dodo is the only command, everything else is GUI."""

    def __init__(self, bot: StarzaiBot):
        self.bot = bot
        self._boost_cache: dict[int, bool] = {}  # user_id -> has active boost
        self._dodo_config_cache: dict[int, dict] = {}  # guild_id -> {tasks_channel_id, gc_channel_id}
        self.check_expirations.start()

    def cog_unload(self):
        self.check_expirations.cancel()

    # ── Channel config helpers (DB → env var fallback) ───────────────

    async def _get_dodo_config(self, guild_id: int) -> dict:
        """Fetch Dodo channel config for a guild. Checks cache → DB → env var fallback."""
        if guild_id in self._dodo_config_cache:
            return self._dodo_config_cache[guild_id]

        row = await self.bot.database.get_dodo_config(guild_id)
        config = {
            "tasks_channel_id": (row["tasks_channel_id"] if row and row["tasks_channel_id"] else None)
                                or self.bot.settings.dodo_tasks_channel_id,
            "gc_channel_id":    (row["gc_channel_id"] if row and row["gc_channel_id"] else None)
                                or self.bot.settings.dodo_gc_channel_id,
        }
        self._dodo_config_cache[guild_id] = config
        return config

    async def _get_tasks_channel_id(self, guild_id: int) -> Optional[int]:
        return (await self._get_dodo_config(guild_id))["tasks_channel_id"]

    async def _get_gc_channel_id(self, guild_id: int) -> Optional[int]:
        return (await self._get_dodo_config(guild_id))["gc_channel_id"]

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
        try:
            thread, message = await self._get_or_create_thread(interaction)
        except RuntimeError as exc:
            await interaction.followup.send(
                embed=Embedder.error("Channel Error", str(exc)), ephemeral=True,
            )
            return
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

    @dodo_group.command(name="setchannel", description="Set the Dodo tasks and/or announcements channel")
    @app_commands.describe(
        tasks_channel="Channel where task threads will be created",
        gc_channel="Channel for public callouts and announcements",
    )
    async def setchannel_cmd(
        self,
        interaction: discord.Interaction,
        tasks_channel: Optional[discord.TextChannel] = None,
        gc_channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                embed=Embedder.error("Server Only", "Dodo can only be used in a server."), ephemeral=True,
            )
            return

        guild_id = interaction.guild_id

        # If no args, show current config
        if tasks_channel is None and gc_channel is None:
            config = await self._get_dodo_config(guild_id)
            lines = []
            if config["tasks_channel_id"]:
                lines.append(f"📋 Tasks channel: <#{config['tasks_channel_id']}>")
            else:
                lines.append("📋 Tasks channel: *not set*")
            if config["gc_channel_id"]:
                lines.append(f"📢 Announcements channel: <#{config['gc_channel_id']}>")
            else:
                lines.append("📢 Announcements channel: *not set*")
            lines.append("")
            lines.append("Use `/dodo setchannel tasks_channel:#channel gc_channel:#channel` to configure.")
            embed = discord.Embed(
                title="🦤 Dodo Channel Config",
                description="\n".join(lines),
                color=BOT_COLOR,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Save to DB
        await self.bot.database.set_dodo_config(
            guild_id=guild_id,
            tasks_channel_id=tasks_channel.id if tasks_channel else None,
            gc_channel_id=gc_channel.id if gc_channel else None,
            configured_by=str(interaction.user.id),
        )

        # Invalidate cache
        self._dodo_config_cache.pop(guild_id, None)

        # Build confirmation
        parts = []
        if tasks_channel:
            parts.append(f"📋 Tasks → {tasks_channel.mention}")
        if gc_channel:
            parts.append(f"📢 Announcements → {gc_channel.mention}")

        embed = discord.Embed(
            title="✅ Dodo Channels Updated!",
            description="\n".join(parts),
            color=BOT_SUCCESS_COLOR,
        )
        embed.set_footer(text="Run /dodo setchannel with no args to see current config")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Eligibility Check ────────────────────────────────────────────

    async def _check_user_eligible(self, interaction: discord.Interaction) -> Tuple[bool, str]:
        """Check if user meets requirements to use Dodo."""
        if not interaction.guild:
            return False, "Dodo can only be used in a server."
        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            return False, "Could not find you in this server."
        # 7-day server age requirement
        if member.joined_at:
            days_in_server = (_now() - member.joined_at).days
            if days_in_server < DODO_MIN_SERVER_AGE_DAYS:
                remaining = DODO_MIN_SERVER_AGE_DAYS - days_in_server
                return False, f"You must be in the server for at least {DODO_MIN_SERVER_AGE_DAYS} days to use Dodo. ({remaining} day(s) remaining)"
        return True, ""

    # ── Database Helpers ─────────────────────────────────────────────

    async def _ensure_dodo_user(self, user_id: int, guild_id: int) -> None:
        pool = self.bot.database.pool
        await pool.execute(
            "INSERT INTO dodo_users (user_id, guild_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            user_id, guild_id,
        )

    async def _get_dodo_user(self, user_id: int, guild_id: int) -> Optional[Dict[str, Any]]:
        pool = self.bot.database.pool
        row = await pool.fetchrow(
            "SELECT * FROM dodo_users WHERE user_id = $1 AND guild_id = $2",
            user_id, guild_id,
        )
        return dict(row) if row else None

    async def _get_user_tasks(
        self, user_id: int, guild_id: int, completed: Optional[bool] = None
    ) -> List[Dict[str, Any]]:
        pool = self.bot.database.pool
        if completed is not None:
            rows = await pool.fetch(
                "SELECT * FROM dodo_tasks WHERE user_id = $1 AND guild_id = $2 AND is_expired = 0 AND is_completed = $3 ORDER BY created_at DESC",
                user_id, guild_id, int(completed),
            )
        else:
            rows = await pool.fetch(
                "SELECT * FROM dodo_tasks WHERE user_id = $1 AND guild_id = $2 AND is_expired = 0 ORDER BY created_at DESC",
                user_id, guild_id,
            )
        return [dict(r) for r in rows]

    async def _get_task_by_id(self, task_id: int) -> Optional[Dict[str, Any]]:
        pool = self.bot.database.pool
        row = await pool.fetchrow("SELECT * FROM dodo_tasks WHERE id = $1", task_id)
        return dict(row) if row else None

    async def _get_thread_owner(self, thread_id: int, guild_id: int) -> Optional[int]:
        """Look up the user_id that owns a given dodo thread."""
        pool = self.bot.database.pool
        row = await pool.fetchrow(
            "SELECT user_id FROM dodo_threads WHERE thread_id = $1 AND guild_id = $2",
            thread_id, guild_id,
        )
        return row["user_id"] if row else None

    async def _count_active_by_priority(self, user_id: int, guild_id: int, priority: str) -> int:
        pool = self.bot.database.pool
        row = await pool.fetchrow(
            "SELECT COUNT(*) as cnt FROM dodo_tasks WHERE user_id = $1 AND guild_id = $2 AND priority = $3 AND is_completed = 0 AND is_expired = 0",
            user_id, guild_id, priority,
        )
        return row["cnt"] if row else 0

    async def _count_completed_today(self, user_id: int, guild_id: int) -> int:
        pool = self.bot.database.pool
        today = _today()
        row = await pool.fetchrow(
            "SELECT COUNT(*) as cnt FROM dodo_tasks WHERE user_id = $1 AND guild_id = $2 AND is_completed = 1 AND CAST(completed_at AS DATE) = CAST($3 AS DATE)",
            user_id, guild_id, today,
        )
        return row["cnt"] if row else 0

    async def _get_strike_count(self, user_id: int, guild_id: int) -> int:
        pool = self.bot.database.pool
        today = _today()
        row = await pool.fetchrow(
            "SELECT strike_count FROM dodo_strikes WHERE user_id = $1 AND guild_id = $2 AND strike_date = $3",
            user_id, guild_id, today,
        )
        return row["strike_count"] if row else 0


    # ── Thread & Embed System ────────────────────────────────────────

    async def _get_or_create_thread(
        self, interaction: discord.Interaction
    ) -> Tuple[discord.Thread, discord.Message]:
        """Get or create a thread for the user in the dodo-tasks channel."""
        pool = self.bot.database.pool
        guild_id = interaction.guild_id
        user_id = interaction.user.id

        # Check for existing thread record
        row = await pool.fetchrow(
            "SELECT thread_id, message_id FROM dodo_threads WHERE user_id = $1 AND guild_id = $2",
            user_id, guild_id,
        )

        # Try to use the configured channel, fall back to current channel
        channel_id = await self._get_tasks_channel_id(guild_id)
        channel = None
        if channel_id:
            channel = self.bot.get_channel(channel_id)
        if not channel:
            channel = interaction.channel

        # Resolve threads to their parent TextChannel — can't nest threads
        if isinstance(channel, discord.Thread):
            channel = channel.parent
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(
                "Could not find a valid text channel. "
                "Set DODO_TASKS_CHANNEL_ID or run /dodo from a text channel."
            )

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
                        await pool.execute(
                            "UPDATE dodo_threads SET message_id = $1 WHERE user_id = $2 AND guild_id = $3",
                            message.id, user_id, guild_id,
                        )
                        return thread, message
            except (discord.NotFound, discord.Forbidden):
                pass  # Thread gone, create new

        # Create new thread in the resolved TextChannel
        thread_name = f"🦤 {interaction.user.display_name}'s Tasks"
        thread = await channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.public_thread,
            auto_archive_duration=1440,  # 24 hours
        )

        view = TaskThreadView(self.bot)
        embed = discord.Embed(title="Loading your dashboard...", color=BOT_COLOR)
        message = await thread.send(embed=embed, view=view)

        # Store thread info
        await pool.execute(
            """INSERT INTO dodo_threads (user_id, guild_id, thread_id, message_id) VALUES ($1, $2, $3, $4)
               ON CONFLICT (user_id, guild_id) DO UPDATE SET thread_id = EXCLUDED.thread_id, message_id = EXCLUDED.message_id""",
            user_id, guild_id, thread.id, message.id,
        )
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

        # Check permissions for display
        available_checks, next_unlock = await self._get_check_permissions(user_id, guild_id)

        # Build task list
        task_lines = []
        for t in active_tasks:
            emoji = DODO_PRIORITY_EMOJIS.get(t["priority"], "⬜")
            text = t["task_text"] if not t["is_hidden"] else "🔒 Personal Task"
            line = f"{emoji} {text}"
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
        perm_text = f"`{available_checks}` available"
        if next_unlock and available_checks == 0:
            perm_text += f" (next in {_format_duration(next_unlock)})"
        embed.add_field(name="🔓 Checks", value=perm_text, inline=True)
        embed.set_footer(text=f"🦤 Dodo • Last updated")
        embed.timestamp = _now()

        try:
            view = TaskThreadView(self.bot)
            await message.edit(embed=embed, view=view)
        except discord.HTTPException as e:
            logger.warning("Failed to update thread embed: %s", e)

    async def _get_user_rank(self, user_id: int, guild_id: int) -> int:
        pool = self.bot.database.pool
        row = await pool.fetchrow(
            "SELECT COUNT(*) + 1 as rank FROM dodo_users WHERE guild_id = $1 AND xp > (SELECT COALESCE(xp, 0) FROM dodo_users WHERE user_id = $2 AND guild_id = $3)",
            guild_id, user_id, guild_id,
        )
        return row["rank"] if row else 1

    async def _get_check_permissions(self, user_id: int, guild_id: int) -> Tuple[int, Optional[timedelta]]:
        """Return (available_checks, time_until_next_unlock).

        Permission stack system:
        - Every task created pushes a 5-min timer.
        - After 5 min, if the task still exists (not deleted/expired early), one check permission unlocks.
        - available = unlocked - checks_used_in_last_24h
        """
        pool = self.bot.database.pool
        now = _now()
        cutoff = _sql_dt(now - timedelta(minutes=DODO_COOLDOWN_MINUTES))
        day_ago = _sql_dt(now - timedelta(hours=24))

        # Unlocked: tasks created >= 5 min ago that still exist in DB and weren't expired early
        row = await pool.fetchrow(
            """SELECT COUNT(*) as cnt FROM dodo_tasks
               WHERE user_id = $1 AND guild_id = $2 AND created_at <= $3
               AND NOT (is_expired = 1 AND timer_expires IS NOT NULL AND timer_expires < $4)""",
            user_id, guild_id, cutoff, cutoff,
        )
        unlocked = row["cnt"] if row else 0

        # Used: tasks checked in the last 24 hours
        row = await pool.fetchrow(
            """SELECT COUNT(*) as cnt FROM dodo_tasks
               WHERE user_id = $1 AND guild_id = $2 AND is_completed = 1 AND completed_at >= $3""",
            user_id, guild_id, day_ago,
        )
        used = row["cnt"] if row else 0

        available = max(0, unlocked - used)

        # Time until next unlock: earliest task created < 5 min ago
        next_unlock: Optional[timedelta] = None
        if available == 0:
            row = await pool.fetchrow(
                """SELECT MIN(created_at) as earliest FROM dodo_tasks
                   WHERE user_id = $1 AND guild_id = $2 AND created_at > $3
                   AND is_expired = 0""",
                user_id, guild_id, cutoff,
            )
            if row and row["earliest"]:
                try:
                    earliest = datetime.fromisoformat(row["earliest"]).replace(tzinfo=timezone.utc)
                    unlock_at = earliest + timedelta(minutes=DODO_COOLDOWN_MINUTES)
                    remaining = unlock_at - now
                    if remaining.total_seconds() > 0:
                        next_unlock = remaining
                except (ValueError, TypeError):
                    pass

        return available, next_unlock


    # ── Task Handlers ────────────────────────────────────────────────

    async def _handle_add_task(
        self,
        interaction: discord.Interaction,
        task_text: str,
        priority: str,
        timer_td: Optional[timedelta],
        is_hidden: bool,
        remind_intervals: List[str],
        remind_character: Optional[str] = None,
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
        timer_expires = None
        if timer_td:
            timer_expires = _sql_dt(now + timer_td)

        # Calculate first reminder time
        next_remind = None
        remind_enabled = bool(remind_intervals)
        if remind_intervals:
            first_td = _parse_timer(remind_intervals[0])
            if first_td:
                next_remind = _sql_dt(now + first_td)

        pool = self.bot.database.pool
        await pool.execute(
            """INSERT INTO dodo_tasks
                (user_id, guild_id, task_text, priority, is_hidden,
                 timer_expires, remind_enabled, remind_intervals, next_remind_at, remind_character)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            user_id, guild_id, task_text, priority, int(is_hidden),
            timer_expires, int(remind_enabled), json.dumps(remind_intervals), next_remind,
            remind_character,
        )

        emoji = DODO_PRIORITY_EMOJIS[priority]
        desc_parts = [
            f"{emoji} **{task_text}**\n",
            f"Priority: **{priority.title()}** | Cooldown: **{DODO_COOLDOWN_MINUTES}min**",
        ]
        if timer_td:
            desc_parts.append(f" | Timer: **{_format_duration(timer_td)}**")
        if remind_intervals:
            desc_parts.append(f"\nReminders: {', '.join(remind_intervals)}")
        if remind_character:
            desc_parts.append(f" ({remind_character})")
        await interaction.response.send_message(
            embed=Embedder.success("Task Added! 🦤", "".join(desc_parts)),
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

        # Permission stack check
        user_id = interaction.user.id
        guild_id = interaction.guild_id
        available, next_unlock = await self._get_check_permissions(user_id, guild_id)
        if available <= 0:
            # Soft block — no strike, just inform
            if next_unlock:
                wait_msg = f"Next check unlocks in **{_format_duration(next_unlock)}**."
            else:
                wait_msg = "Add more tasks to earn check permissions!"
            embed = discord.Embed(
                title="⏳ No Checks Available",
                description=(
                    f"You’ve used all your check permissions.\n{wait_msg}\n\n"
                    f"Each task unlocks one check **{DODO_COOLDOWN_MINUTES} min** after creation."
                ),
                color=BOT_ERROR_COLOR,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        pool = self.bot.database.pool
        now = _now()
        await pool.execute(
            "UPDATE dodo_tasks SET is_completed = 1, completed_at = $1 WHERE id = $2",
            _sql_dt(now), task_id,
        )

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

        pool = self.bot.database.pool
        await pool.execute("DELETE FROM dodo_tasks WHERE id = $1", task_id)

        await interaction.response.send_message(
            embed=Embedder.success("Task Deleted 🗑️", f"**{task['task_text']}** has been removed."),
            ephemeral=True,
        )
        await self._refresh_user_embed(interaction.user.id, interaction.guild_id)

    async def _refresh_user_embed(self, user_id: int, guild_id: int) -> None:
        """Refresh the user's thread embed."""
        pool = self.bot.database.pool
        row = await pool.fetchrow(
            "SELECT thread_id, message_id FROM dodo_threads WHERE user_id = $1 AND guild_id = $2",
            user_id, guild_id,
        )

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
            pool = self.bot.database.pool
            await pool.execute(
                "UPDATE dodo_mvp SET boost_used = 1 WHERE user_id = $1 AND boost_available = 1 AND boost_used = 0",
                user_id,
            )

        if half_xp:
            xp = xp // 2

        # Apply XP
        pool = self.bot.database.pool
        await pool.execute(
            "UPDATE dodo_users SET xp = xp + $1 WHERE user_id = $2 AND guild_id = $3",
            xp, user_id, guild_id,
        )
        return xp

    async def _update_streak(self, user_id: int, guild_id: int) -> None:
        """Update streak on task completion."""
        pool = self.bot.database.pool
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

        await pool.execute(
            "UPDATE dodo_users SET streak = $1, last_active = $2 WHERE user_id = $3 AND guild_id = $4",
            new_streak, today, user_id, guild_id,
        )

    async def _apply_strike(self, interaction: discord.Interaction, task: Dict[str, Any]) -> None:
        """Apply a strike for anti-abuse violations."""
        user_id = interaction.user.id
        guild_id = interaction.guild_id
        pool = self.bot.database.pool
        today = _today()

        # Increment strike
        await pool.execute(
            """INSERT INTO dodo_strikes (user_id, guild_id, strike_date, strike_count)
               VALUES ($1, $2, $3, 1)
               ON CONFLICT(user_id, guild_id, strike_date)
               DO UPDATE SET strike_count = dodo_strikes.strike_count + 1""",
            user_id, guild_id, today,
        )

        strike_count = await self._get_strike_count(user_id, guild_id)

        # Ephemeral block message
        rule = DODO_STRIKE_RULES.get(strike_count, "zero_xp")
        if rule == "funny":
            desc = f"🛑 Nice try! You don't have any check permissions yet!\nStrike **{strike_count}/4** today."
        elif rule == "serious":
            desc = f"⚠️ Again? Wait for your cooldowns to unlock!\nStrike **{strike_count}/4** — next one halves your XP!"
        elif rule == "half_xp":
            desc = f"🔥 That's strike **{strike_count}**. Your XP is **halved** for today."
        else:
            desc = f"💀 Strike **{strike_count}**. **ZERO XP** for you today."

        color = DODO_STRIKE_COLORS.get(min(strike_count, 4), 0x000000)
        embed = discord.Embed(title="🚫 Not So Fast!", description=desc, color=color)
        await interaction.response.send_message(embed=embed, ephemeral=True)

        # Public callout in GC
        gc_channel_id = await self._get_gc_channel_id(guild_id)
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
        pool = self.bot.database.pool
        today = _today()
        row = await pool.fetchrow(
            """SELECT COUNT(*) as cnt FROM dodo_tasks
               WHERE user_id = $1 AND guild_id = $2 AND is_completed = 1 AND CAST(completed_at AS DATE) = CAST($3 AS DATE)""",
            user_id, guild_id, today,
        )
        tasks_done = row["cnt"] if row else 0
        # Approximate XP earned today
        user_data = await self._get_dodo_user(user_id, guild_id)
        xp = user_data["xp"] if user_data else 0
        return (tasks_done * 10) + xp

    async def _announce_daily_mvp(self) -> None:
        """Announce the daily MVP for each guild."""
        pool = self.bot.database.pool
        today = _today()

        # Get all guilds with active dodo users
        rows = await pool.fetch("SELECT DISTINCT guild_id FROM dodo_users")
        guilds = [row["guild_id"] for row in rows]

        for guild_id in guilds:
            try:
                # Find user with highest score today
                row = await pool.fetchrow(
                    """SELECT user_id, COUNT(*) as tasks_done
                       FROM dodo_tasks
                       WHERE guild_id = $1 AND is_completed = 1 AND CAST(completed_at AS DATE) = CAST($2 AS DATE)
                       GROUP BY user_id
                       ORDER BY tasks_done DESC
                       LIMIT 1""",
                    guild_id, today,
                )

                if not row:
                    continue

                winner_id = row["user_id"]
                score = row["tasks_done"]

                # Record MVP
                await pool.execute(
                    """INSERT INTO dodo_mvp (guild_id, user_id, mvp_type, expires_at)
                       VALUES ($1, $2, 'daily', NOW() + INTERVAL '1 day')""",
                    guild_id, winner_id,
                )

                # Grant steal shield
                await pool.execute(
                    "UPDATE dodo_users SET steal_shield = 1 WHERE user_id = $1 AND guild_id = $2",
                    winner_id, guild_id,
                )

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
                gc_channel_id = await self._get_gc_channel_id(guild_id)
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
        pool = self.bot.database.pool

        rows = await pool.fetch("SELECT DISTINCT guild_id FROM dodo_users")
        guilds = [row["guild_id"] for row in rows]

        for guild_id in guilds:
            try:
                # User with most XP
                row = await pool.fetchrow(
                    "SELECT user_id, xp FROM dodo_users WHERE guild_id = $1 ORDER BY xp DESC LIMIT 1",
                    guild_id,
                )

                if not row:
                    continue

                winner_id = row["user_id"]
                xp = row["xp"]

                # Record MVP with perks
                now = _now()
                next_sunday = now + timedelta(days=(6 - now.weekday()) % 7 + 7)
                await pool.execute(
                    """INSERT INTO dodo_mvp (guild_id, user_id, mvp_type, boost_available, steal_available, expires_at)
                       VALUES ($1, $2, 'weekly', 1, 1, $3)""",
                    guild_id, winner_id, _sql_dt(next_sunday),
                )

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
                gc_channel_id = await self._get_gc_channel_id(guild_id)
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
        gc_channel_id = await self._get_gc_channel_id(guild_id)
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
        pool = self.bot.database.pool
        now = _sql_now()
        row = await pool.fetchrow(
            """SELECT id FROM dodo_mvp
               WHERE user_id = $1 AND boost_available = 1 AND boost_used = 0
               AND (expires_at IS NULL OR expires_at > $2)
               ORDER BY awarded_at DESC LIMIT 1""",
            user_id, now,
        )
        if not row:
            return False
        self._boost_cache[user_id] = True
        return True

    async def _check_steal_available(self, user_id: int) -> bool:
        pool = self.bot.database.pool
        now = _sql_now()
        row = await pool.fetchrow(
            """SELECT id FROM dodo_mvp
               WHERE user_id = $1 AND steal_available = 1 AND steal_used = 0
               AND (expires_at IS NULL OR expires_at > $2)
               ORDER BY awarded_at DESC LIMIT 1""",
            user_id, now,
        )
        return row is not None

    async def _get_user_guild(self, user_id: int) -> Optional[int]:
        pool = self.bot.database.pool
        row = await pool.fetchrow(
            "SELECT guild_id FROM dodo_users WHERE user_id = $1 LIMIT 1", user_id,
        )
        return row["guild_id"] if row else None

    async def _get_steal_targets(self, guild_id: int, stealer_id: int) -> List[Tuple[int, str]]:
        """Get eligible steal targets (active dodo users, not the stealer, not already stolen this week)."""
        pool = self.bot.database.pool
        week = _week_start()
        rows = await pool.fetch(
            """SELECT du.user_id FROM dodo_users du
               WHERE du.guild_id = $1 AND du.user_id != $2
               AND du.user_id NOT IN (
                   SELECT target_id FROM dodo_steal_log
                   WHERE stealer_id = $3 AND week_start = $4
               )
               ORDER BY du.xp DESC LIMIT 25""",
            guild_id, stealer_id, stealer_id, week,
        )

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
        pool = self.bot.database.pool
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
        steal_exists = await pool.fetchrow(
            "SELECT id FROM dodo_steal_log WHERE stealer_id = $1 AND target_id = $2 AND week_start = $3",
            stealer_id, target_id, week,
        )
        if steal_exists:
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
        await pool.execute(
            "UPDATE dodo_users SET xp = xp - $1 WHERE user_id = $2 AND guild_id = $3",
            stolen_xp, target_id, guild_id,
        )
        await pool.execute(
            "UPDATE dodo_users SET xp = xp + $1 WHERE user_id = $2 AND guild_id = $3",
            stolen_xp, stealer_id, guild_id,
        )
        await pool.execute(
            "INSERT INTO dodo_steal_log (guild_id, stealer_id, target_id, week_start, stolen_xp) VALUES ($1, $2, $3, $4, $5)",
            guild_id, stealer_id, target_id, week, stolen_xp,
        )
        # Mark steal as used
        await pool.execute(
            "UPDATE dodo_mvp SET steal_used = 1, steal_target_id = $1 WHERE user_id = $2 AND steal_available = 1 AND steal_used = 0",
            target_id, stealer_id,
        )
        # Using a perk makes you steal-eligible
        await pool.execute(
            "UPDATE dodo_users SET steal_shield = 0 WHERE user_id = $1 AND guild_id = $2",
            stealer_id, guild_id,
        )

        await interaction.response.send_message(
            embed=Embedder.success(
                "Steal Successful! 💀",
                f"You stole **{stolen_xp:,} XP** from <@{target_id}>!"
            ),
            ephemeral=True,
        )

        # Public announcement
        gc_channel_id = await self._get_gc_channel_id(guild_id)
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
        pool = self.bot.database.pool
        # Check shield
        row = await pool.fetchrow(
            "SELECT steal_shield FROM dodo_users WHERE user_id = $1 AND steal_shield = 1",
            user_id,
        )
        if not row:
            return False

        # Reverse the most recent steal against this user
        steal = await pool.fetchrow(
            "SELECT id, stealer_id, guild_id, stolen_xp FROM dodo_steal_log WHERE target_id = $1 ORDER BY created_at DESC LIMIT 1",
            user_id,
        )

        if steal:
            # Reverse XP transfer
            await pool.execute(
                "UPDATE dodo_users SET xp = xp + $1 WHERE user_id = $2 AND guild_id = $3",
                steal["stolen_xp"], user_id, steal["guild_id"],
            )
            await pool.execute(
                "UPDATE dodo_users SET xp = GREATEST(0, xp - $1) WHERE user_id = $2 AND guild_id = $3",
                steal["stolen_xp"], steal["stealer_id"], steal["guild_id"],
            )
            await pool.execute("DELETE FROM dodo_steal_log WHERE id = $1", steal["id"])

        # Consume shield
        await pool.execute(
            "UPDATE dodo_users SET steal_shield = 0 WHERE user_id = $1", user_id,
        )

        # Public announcement
        if steal:
            gc_channel_id = await self._get_gc_channel_id(steal["guild_id"])
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

        # Use fixed character if user chose one, otherwise rotate through stages
        chosen = task.get("remind_character")
        if chosen:
            char = next((c for c in DODO_BSD_CHARACTERS if c["name"] == chosen), DODO_BSD_CHARACTERS[0])
        else:
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
            gc_channel_id = await self._get_gc_channel_id(guild_id)
            if gc_channel_id:
                gc_channel = self.bot.get_channel(gc_channel_id)
                if gc_channel:
                    try:
                        msg = await gc_channel.send(
                            content=f"<@{user_id}>",
                            embed=embed,
                            delete_after=60,
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
                        await member.send(embed=embed, delete_after=60)
                    except discord.HTTPException:
                        pass

    # ── Background Loop ──────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def check_expirations(self):
        """Background task: red task expiry, reminders, MVP, strike reset."""
        try:
            pool = self.bot.database.pool
            now = _now()
            now_iso = _sql_dt(now)

            # 1. Red task expiry
            expired_tasks = await pool.fetch(
                """SELECT id, user_id, guild_id FROM dodo_tasks
                   WHERE priority = 'red' AND is_completed = 0 AND is_expired = 0
                   AND timer_expires IS NOT NULL AND timer_expires < $1""",
                now_iso,
            )

            for task in expired_tasks:
                await pool.execute(
                    "UPDATE dodo_tasks SET is_expired = 1 WHERE id = $1", task["id"],
                )
                # Deduct XP
                await pool.execute(
                    "UPDATE dodo_users SET xp = GREATEST(0, xp - $1) WHERE user_id = $2 AND guild_id = $3",
                    DODO_RED_EXPIRE_PENALTY, task["user_id"], task["guild_id"],
                )
                await self._refresh_user_embed(task["user_id"], task["guild_id"])

            # 2. BSD reminders
            reminder_rows = await pool.fetch(
                """SELECT * FROM dodo_tasks
                   WHERE remind_enabled = 1 AND is_completed = 0 AND is_expired = 0
                   AND next_remind_at IS NOT NULL AND next_remind_at <= $1""",
                now_iso,
            )
            reminder_tasks = [dict(r) for r in reminder_rows]

            for task in reminder_tasks:
                try:
                    await self._send_bsd_reminder(task)
                except Exception as e:
                    logger.error("Error sending BSD reminder for task %s: %s", task["id"], e)

                # Increment stage and set next remind time (absolute from creation)
                chosen_char = task.get("remind_character")
                new_stage = task.get("remind_stage", 0) + (0 if chosen_char else 1)
                intervals = json.loads(task.get("remind_intervals", "[]"))
                if new_stage < len(intervals):
                    next_td = _parse_timer(intervals[new_stage])
                    if next_td:
                        try:
                            created = datetime.fromisoformat(task["created_at"]).replace(tzinfo=timezone.utc)
                            next_remind = _sql_dt(created + next_td)
                        except (ValueError, TypeError):
                            next_remind = _sql_dt(now + next_td)
                    else:
                        next_remind = None
                else:
                    next_remind = None  # No more reminders

                await pool.execute(
                    "UPDATE dodo_tasks SET remind_stage = $1, next_remind_at = $2 WHERE id = $3",
                    new_stage, next_remind, task["id"],
                )

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
            pool = self.bot.database.pool
            rows = await pool.fetch(
                "SELECT user_id, xp, streak FROM dodo_users WHERE guild_id = $1 ORDER BY xp DESC LIMIT 5",
                guild_id,
            )

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
        pool = self.bot.database.pool
        rows = await pool.fetch(
            "SELECT user_id, xp, streak FROM dodo_users WHERE guild_id = $1 ORDER BY xp DESC LIMIT 10",
            guild_id,
        )

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
