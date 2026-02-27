"""
Music Cog — download, VC playback, lyrics, and platform URL resolution.

Commands:
    /music      — Search & download a song with quality selection
    /play       — Search & play in voice channel
    /playnext   — Search & insert a song at the front of the queue
    /skip       — Skip current song
    /music-stop — Stop playback & leave VC
    /queue      — Show the current queue (paginated)
    /nowplaying — Show current song info with live progress bar
    /pause      — Pause playback
    /resume     — Resume playback
    /volume     — Set playback volume
    /shuffle    — Shuffle the queue
    /loop       — Set loop mode (off / track / queue)
    /seek       — Seek to a position in the current song
    /remove     — Remove a song from the queue by position
    /clear      — Clear the entire queue
    /djrole     — Set or clear the DJ role for queue management
    /lyrics     — Search for song lyrics

Features:
    • Progress bar with live position tracking
    • Auto-resume on voice reconnect
    • DJ role restrictions for queue management
    • Queue pagination for large queues
    • Platform URL resolution (Spotify, YouTube, SoundCloud, etc.)

System requirement:
    FFmpeg must be installed on the host system (e.g. ``apt install ffmpeg``)
    for voice channel playback to work.  Without it, /play and VC streaming
    will fail.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import random
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config.constants import BOT_COLOR
from utils.embedder import Embedder
from utils.music_api import (
    DOWNLOAD_QUALITIES,
    MusicAPI,
    _get_url_for_quality,
    _pick_best_url,
)
from utils.lyrics import LyricsFetcher
from utils.platform_resolver import is_music_url, resolve_url

if TYPE_CHECKING:
    from bot import StarzaiBot

logger = logging.getLogger(__name__)

# ── Colours (consistent with bot theme via constants) ─────────────────
MUSIC_COLOR = BOT_COLOR


# =====================================================================
#  Guild-authorization view (shown when bot isn’t allowed in a server)
# =====================================================================

class _OwnerDMView(discord.ui.View):
    """Persistent view with link-buttons directing users to each bot owner’s DM."""

    def __init__(self, owner_ids: list[int]) -> None:
        super().__init__(timeout=None)  # persistent
        for oid in owner_ids:
            self.add_item(
                discord.ui.Button(
                    label=f"DM Owner",
                    style=discord.ButtonStyle.link,
                    url=f"https://discord.com/users/{oid}",
                    emoji="\U0001f4e9",
                )
            )

# ── Discord limits ───────────────────────────────────────────────────
DISCORD_UPLOAD_FALLBACK = 25 * 1024 * 1024  # 25 MB fallback (no guild / DM)
MAX_DOWNLOAD_SIZE = 200 * 1024 * 1024       # 200 MB max download buffer
MIN_BITRATE_KBPS = 64  # floor — below this quality is unacceptable
MAX_EMBED_DESC = 4096
MAX_SELECT_OPTIONS = 25
MAX_FILENAME_LEN = 100  # max chars for sanitised filenames

# ── Timeouts ─────────────────────────────────────────────────────────
VIEW_TIMEOUT = 60  # seconds for interactive views
VC_IDLE_TIMEOUT = 300  # 5 minutes idle before auto-disconnect

# ── FFmpeg / voice quality ──────────────────────────────────────────
# NOTE: FFmpeg must be installed on the host system for VC playback.
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}
# Max Opus encoder bitrate (bps).  512 kbps is the ceiling supported by
# discord.py's Opus wrapper — anything higher is ignored by the codec.
MAX_ENCODER_BITRATE = 512_000  # 512 kbps

# ── Audio Filters (FFmpeg -af chains) ────────────────────────────────
AUDIO_FILTERS: Dict[str, str] = {
    "off":        "",
    "bassboost":  "bass=g=10,dynaudnorm=f=150",
    "nightcore":  "aresample=48000,asetrate=48000*1.25",
    "vaporwave":  "aresample=48000,asetrate=48000*0.8",
    "karaoke":    "stereotools=mlev=0.03",
    "8d":         "apulsator=hz=0.09",
    "treble":     "treble=g=5,dynaudnorm=f=150",
    "vibrato":    "vibrato=f=6.5:d=0.5",
    "tremolo":    "tremolo=f=6.5:d=0.6",
    "pop":        "bass=g=3,treble=g=2,dynaudnorm=f=200",
    "soft":       "lowpass=f=3000,volume=1.2",
    "loud":       "volume=2.0,dynaudnorm=f=150",
}
AUDIO_FILTER_NAMES = list(AUDIO_FILTERS.keys())

MAX_HISTORY = 50  # cap on how many recently-played songs we remember

# ── Branding (user-facing) ───────────────────────────────────────────
BRAND = "Powered by StarzAI \u26a1"


# =====================================================================
#  Per-guild voice state
# =====================================================================
class GuildMusicState:
    """Per-guild state for the voice channel music player."""

    __slots__ = (
        "queue",
        "current",
        "voice_client",
        "volume",
        "text_channel",
        "idle_task",
        "requester_map",
        # Loop & playback tracking
        "loop_mode",
        "playback_start_time",
        "position_offset",
        "pause_start_time",
        "paused_elapsed",
        "_seeking",
        "_current_stream_url",
        # DJ role
        "dj_role_id",
        # Auto-resume
        "_resume_info",
        # Concurrency lock — serialises connect / play / queue mutations
        "_lock",
        # History & previous
        "history",
        "previous_song",
        # 24/7 mode (never auto-disconnect)
        "always_connected",
        # Audio filter
        "audio_filter",
        # Autoplay (find similar songs when queue ends)
        "autoplay",
        # Vote-skip tracking
        "skip_votes",
    )

    def __init__(self) -> None:
        self.queue: List[Dict[str, Any]] = []
        self.current: Optional[Dict[str, Any]] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.volume: float = 0.5
        self.text_channel: Optional[discord.abc.Messageable] = None
        self.idle_task: Optional[asyncio.Task] = None
        self.requester_map: Dict[str, int] = {}  # song_id -> user_id
        # Loop mode: "off", "track", "queue"
        self.loop_mode: str = "off"
        # Playback position tracking (for progress bar & seek)
        self.playback_start_time: float = 0.0   # time.monotonic()
        self.position_offset: float = 0.0        # seconds into the song
        self.pause_start_time: float = 0.0       # when pause began
        self.paused_elapsed: float = 0.0         # total seconds spent paused
        self._seeking: bool = False               # guard to prevent _play_next on seek
        self._current_stream_url: str = ""        # stream URL for seek/resume
        # DJ role (optional per-guild restriction)
        self.dj_role_id: Optional[int] = None
        # Auto-resume state
        self._resume_info: Optional[Dict[str, Any]] = None
        # Per-guild lock — serialises VC connect / play / queue mutations
        # so concurrent /play commands don't race and kill each other.
        self._lock: asyncio.Lock = asyncio.Lock()
        # History of recently played songs (capped at 50)
        self.history: List[Dict[str, Any]] = []
        # The song that played immediately before the current one
        self.previous_song: Optional[Dict[str, Any]] = None
        # 24/7 mode — bot stays in VC even when idle / alone
        self.always_connected: bool = False
        # Active audio filter ("off", "bassboost", "nightcore", "vaporwave", "karaoke", "8d")
        self.audio_filter: str = "off"
        # Autoplay — auto-queue similar songs when the queue runs out
        self.autoplay: bool = False
        # Set of user IDs who voted to skip the current song
        self.skip_votes: set = set()

    @property
    def current_position(self) -> float:
        """Return the estimated playback position in seconds."""
        if self.playback_start_time <= 0:
            return 0.0
        if self.voice_client and self.voice_client.is_paused():
            # While paused, freeze at the position when we paused
            paused_since = self.pause_start_time - self.playback_start_time
            return self.position_offset + paused_since - self.paused_elapsed
        elapsed = time.monotonic() - self.playback_start_time - self.paused_elapsed
        return self.position_offset + max(elapsed, 0.0)

    def clear(self) -> None:
        self.queue.clear()
        self.current = None
        self.requester_map.clear()
        self.loop_mode = "off"
        self.playback_start_time = 0.0
        self.position_offset = 0.0
        self.pause_start_time = 0.0
        self.paused_elapsed = 0.0
        self._seeking = False
        self._current_stream_url = ""
        self._resume_info = None
        self.history.clear()
        self.previous_song = None
        self.audio_filter = "off"
        self.autoplay = False
        self.skip_votes.clear()
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
            self.idle_task = None


# =====================================================================
#  Rate-limit helper
# =====================================================================

async def _check_rate_limit(
    bot: Any, interaction: discord.Interaction, *, expensive: bool = False
) -> bool:
    """
    Music-specific rate-limit check — intentionally very generous.

    This bot is for personal / friends-only servers, so music commands
    are effectively unrestricted.  The check is kept as a thin wrapper
    so it can be tightened later if needed.
    """
    # Generous: effectively allow everything for music commands.
    # Only do a very light global-burst guard (200 req/min) to avoid
    # accidental API abuse; individual user/expensive checks are skipped.
    return True


# =====================================================================
#  Interactive Views
# =====================================================================


class SongSelectView(discord.ui.View):
    """Dropdown to pick a song from search results."""

    def __init__(
        self,
        songs: List[Dict[str, Any]],
        cog: "MusicCog",
        interaction: discord.Interaction,
        *,
        for_play: bool = False,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.songs = songs
        self.cog = cog
        self.original_interaction = interaction
        self.for_play = for_play
        self._build_select()

    def _build_select(self) -> None:
        options: List[discord.SelectOption] = []
        for i, song in enumerate(self.songs[:MAX_SELECT_OPTIONS]):
            label = f"{song['name']}"[:100]
            desc = f"{song['artist']} \u2022 {song['duration_formatted']} \u2022 {song['year']}"[:100]
            options.append(
                discord.SelectOption(label=label, description=desc, value=str(i))
            )

        select = discord.ui.Select(
            placeholder="Choose a song\u2026",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.original_interaction.user.id:
            await interaction.response.send_message(
                "\u274c This menu isn\u2019t for you!", ephemeral=True
            )
            return

        # Defer FIRST — network work below can exceed Discord's 3s deadline.
        # Rate-limit check happens after deferral so we never leave the
        # interaction unacknowledged (which causes "Unknown interaction").
        await interaction.response.defer()

        if not await _check_rate_limit(self.cog.bot, interaction, expensive=True):
            return

        idx = int(interaction.data["values"][0])  # type: ignore[index]
        song = self.songs[idx]

        # Ensure download URLs
        song = await self.cog.music_api.ensure_download_urls(song)

        if self.for_play:
            # Directly play in VC
            await self.cog._play_song_in_vc(interaction, song, followup=True)
        else:
            # Show quality selection for download
            view = QualitySelectView(song, self.cog, interaction)
            embed = _song_detail_embed(song)
            await interaction.edit_original_response(embed=embed, view=view)

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Select, discord.ui.Button)):
                item.disabled = True  # type: ignore[union-attr]
        try:
            if self.message:
                await self.message.edit(content="\u23f0 Selection expired.", view=self)
        except Exception:
            pass


class QualitySelectView(discord.ui.View):
    """Buttons for quality selection + Play in VC."""

    def __init__(
        self,
        song: Dict[str, Any],
        cog: "MusicCog",
        interaction: discord.Interaction,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.song = song
        self.cog = cog
        self.original_interaction = interaction
        self._build_buttons()

    def _build_buttons(self) -> None:
        for quality in DOWNLOAD_QUALITIES:
            btn = discord.ui.Button(
                label=quality,
                style=discord.ButtonStyle.primary,
                custom_id=f"dl_{quality}",
            )
            btn.callback = self._make_dl_callback(quality)
            self.add_item(btn)

        play_btn = discord.ui.Button(
            label="\u25b6 Play in VC",
            style=discord.ButtonStyle.success,
            custom_id="play_vc",
            emoji="\U0001f50a",
        )
        play_btn.callback = self._on_play_vc
        self.add_item(play_btn)

        lyrics_btn = discord.ui.Button(
            label="\U0001f4dd Lyrics",
            style=discord.ButtonStyle.secondary,
            custom_id="lyrics_btn",
        )
        lyrics_btn.callback = self._on_lyrics
        self.add_item(lyrics_btn)

    def _make_dl_callback(self, quality: str):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.original_interaction.user.id:
                await interaction.response.send_message(
                    "\u274c These buttons aren\u2019t for you!", ephemeral=True
                )
                return
            # Defer FIRST to prevent "Unknown interaction" errors,
            # then check rate limit via followup.
            await interaction.response.defer()
            if not await _check_rate_limit(self.cog.bot, interaction, expensive=True):
                return
            await self.cog._download_song(interaction, self.song, quality)

        return callback

    async def _on_play_vc(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.original_interaction.user.id:
            await interaction.response.send_message(
                "\u274c These buttons aren\u2019t for you!", ephemeral=True
            )
            return
        # Defer FIRST to prevent "Unknown interaction" errors,
        # then check rate limit via followup.
        await interaction.response.defer()
        if not await _check_rate_limit(self.cog.bot, interaction, expensive=True):
            return
        await self.cog._play_song_in_vc(interaction, self.song, followup=True)

    async def _on_lyrics(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.original_interaction.user.id:
            await interaction.response.send_message(
                "\u274c These buttons aren\u2019t for you!", ephemeral=True
            )
            return
        query = f"{self.song['artist']} {self.song['name']}"
        await self.cog._send_lyrics(interaction, query, self.song["artist"], self.song["name"])

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Select, discord.ui.Button)):
                item.disabled = True  # type: ignore[union-attr]
        try:
            await self.original_interaction.edit_original_response(
                content="\u23f0 Selection expired.", view=self
            )
        except Exception:
            pass


class NowPlayingView(discord.ui.View):
    """Buttons shown on the Now Playing embed with staleness checks."""

    def __init__(self, song: Dict[str, Any], cog: "MusicCog", guild_id: int) -> None:
        # Timeout after the song duration + 60s buffer, minimum 120s
        duration = song.get("duration", 0) or 300
        timeout = max(duration + 60, 120)
        super().__init__(timeout=timeout)
        self.song = song
        self.cog = cog
        self.guild_id = guild_id

    def _is_stale(self) -> bool:
        """Check if this view's song is no longer current."""
        state = self.cog._get_state(self.guild_id)
        if not state.voice_client or not state.voice_client.is_connected():
            return True
        if state.current is None or state.current.get("id") != self.song.get("id"):
            return True
        return False

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="\u23ed")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            return
        try:
            if self._is_stale():
                await interaction.response.send_message("This song is no longer playing.", ephemeral=True)
                return
            state = self.cog._get_state(guild_id)
            if state.voice_client and state.voice_client.is_playing():
                state.voice_client.stop()
                await interaction.response.send_message("\u23ed Skipped!", ephemeral=True)
            else:
                await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        except discord.NotFound:
            pass  # Interaction expired

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, emoji="\u23f8")
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            return
        try:
            if self._is_stale():
                await interaction.response.send_message("This song is no longer playing.", ephemeral=True)
                return
            state = self.cog._get_state(guild_id)
            if state.voice_client and state.voice_client.is_playing():
                state.pause_start_time = time.monotonic()
                state.voice_client.pause()
                await interaction.response.send_message("\u23f8 Paused.", ephemeral=True)
            elif state.voice_client and state.voice_client.is_paused():
                # Toggle: resume if already paused
                if state.pause_start_time > 0:
                    state.paused_elapsed += time.monotonic() - state.pause_start_time
                    state.pause_start_time = 0.0
                state.voice_client.resume()
                await interaction.response.send_message("\u25b6 Resumed.", ephemeral=True)
            else:
                await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        except discord.NotFound:
            pass  # Interaction expired

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="\u23f9")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            return
        try:
            if self._is_stale():
                await interaction.response.send_message("No active playback to stop.", ephemeral=True)
                return
            await self.cog._stop_and_leave(guild_id)
            await interaction.response.send_message("\u23f9 Stopped and left the channel.", ephemeral=True)
        except discord.NotFound:
            pass  # Interaction expired

    @discord.ui.button(label="\U0001f4dd Lyrics", style=discord.ButtonStyle.secondary)
    async def lyrics_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        query = f"{self.song['artist']} {self.song['name']}"
        await self.cog._send_lyrics(interaction, query, self.song["artist"], self.song["name"])

    @discord.ui.button(label="\U0001f504 Refresh", style=discord.ButtonStyle.secondary)
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Refresh the now-playing embed with updated progress bar."""
        guild_id = interaction.guild_id
        if not guild_id:
            return
        try:
            state = self.cog._get_state(guild_id)
            if state.current and state.current.get("id") == self.song.get("id"):
                requester_id = state.requester_map.get(state.current["id"])
                requester = self.cog.bot.get_user(requester_id) if requester_id else None
                embed = _now_playing_embed(state, state.current, requester)
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.response.send_message("This song is no longer playing.", ephemeral=True)
        except discord.NotFound:
            pass

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Select, discord.ui.Button)):
                item.disabled = True  # type: ignore[union-attr]


class QueuePaginationView(discord.ui.View):
    """Paginated view for the music queue."""

    SONGS_PER_PAGE = 10

    def __init__(self, state: "GuildMusicState", cog: "MusicCog", guild_id: int) -> None:
        super().__init__(timeout=120)
        self.state = state
        self.cog = cog
        self.guild_id = guild_id
        self.page = 0

    @property
    def total_pages(self) -> int:
        total = len(self.state.queue)
        if total == 0:
            return 1
        return (total + self.SONGS_PER_PAGE - 1) // self.SONGS_PER_PAGE

    def _build_embed(self) -> discord.Embed:
        """Build the queue embed for the current page."""
        state = self.state
        lines: List[str] = []

        if state.current:
            loop = _loop_badge(state.loop_mode)
            lines.append(f"\U0001f3b5 **Now Playing:** {state.current['name']} \u2014 {state.current['artist']}{loop}")

        if state.queue:
            start_idx = self.page * self.SONGS_PER_PAGE
            end_idx = start_idx + self.SONGS_PER_PAGE
            page_songs = state.queue[start_idx:end_idx]

            lines.append("")
            for i, s in enumerate(page_songs, start_idx + 1):
                dur = s.get("duration_formatted", "?:??")
                lines.append(f"**{i}.** {s['name']} \u2014 {s['artist']}  `{dur}`")
        elif not state.current:
            lines.append("The queue is empty.")

        total = len(state.queue) + (1 if state.current else 0)
        total_dur = sum(s.get("duration", 0) for s in state.queue)
        if state.current:
            total_dur += state.current.get("duration", 0) or 0

        dur_m, dur_s = divmod(int(total_dur), 60)
        dur_h, dur_m = divmod(dur_m, 60)
        dur_str = f"{dur_h}h {dur_m}m" if dur_h else f"{dur_m}m {dur_s}s"

        footer = (
            f"{total} song{'s' if total != 1 else ''} \u2022 {dur_str} \u2022 "
            f"Page {self.page + 1}/{self.total_pages} \u2022 {BRAND}"
        )
        return Embedder.standard(
            "\U0001f4cb Music Queue",
            "\n".join(lines)[:MAX_EMBED_DESC],
            footer=footer,
        )

    @discord.ui.button(label="\u25c0", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page > 0:
            self.page -= 1
        try:
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        except discord.NotFound:
            pass

    @discord.ui.button(label="\u25b6", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page < self.total_pages - 1:
            self.page += 1
        try:
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        except discord.NotFound:
            pass

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Select, discord.ui.Button)):
                item.disabled = True  # type: ignore[union-attr]


# =====================================================================
#  Embed builders (using Embedder utility for consistency)
# =====================================================================

def _search_results_embed(
    query: str, songs: List[Dict[str, Any]]
) -> discord.Embed:
    """Build the search results embed."""
    lines: List[str] = []
    for i, s in enumerate(songs, 1):
        lines.append(
            f"**{i}.** {s['name']} \u2014 {s['artist']} \u2022 "
            f"{s['duration_formatted']} \u2022 {s['year']}"
        )

    return Embedder.standard(
        f"\U0001f3b5 Results for \u201c{query}\u201d",
        "\n".join(lines)[:MAX_EMBED_DESC],
        footer=f"Select a song \u2022 Expires in {VIEW_TIMEOUT}s \u2022 {BRAND}",
        thumbnail=songs[0]["image"] if songs and songs[0].get("image") else None,
    )


def _song_detail_embed(song: Dict[str, Any]) -> discord.Embed:
    """Build a detailed embed for a single song."""
    desc = (
        f"\U0001f3a4 {song['artist']}\n"
        f"\U0001f4bf {song['album']} \u2022 {song['year']}\n"
        f"\u23f1 {song['duration_formatted']}"
    )
    return Embedder.standard(
        f"\U0001f3b5 {song['name']}",
        desc,
        footer=BRAND,
        thumbnail=song.get("image"),
    )


def _progress_bar(position: float, duration: float, width: int = 16) -> str:
    """Build a Unicode progress bar like ▬▬▬▬🔘▬▬▬▬▬▬▬ 1:23 / 4:30."""
    duration = max(duration, 1)
    position = max(0.0, min(position, duration))
    filled = int((position / duration) * width)
    filled = min(filled, width - 1)

    bar = "\u25ac" * filled + "\U0001f518" + "\u25ac" * (width - filled - 1)

    def _fmt(s: float) -> str:
        m, sec = divmod(int(s), 60)
        return f"{m}:{sec:02d}"

    return f"{bar}  {_fmt(position)} / {_fmt(duration)}"


def _loop_badge(mode: str) -> str:
    """Return a small badge string for the current loop mode."""
    if mode == "track":
        return "  \U0001f502 Loop: Track"
    if mode == "queue":
        return "  \U0001f501 Loop: Queue"
    return ""


def _now_playing_embed(
    state: "GuildMusicState",
    song: Dict[str, Any],
    requester: Optional[discord.User] = None,
) -> discord.Embed:
    """Build the Now Playing embed with progress bar."""
    position = state.current_position if state else 0.0
    duration = song.get("duration", 0) or 0
    bar = _progress_bar(position, duration)
    loop = _loop_badge(state.loop_mode) if state else ""

    # Extra status badges
    badges: List[str] = []
    if state and state.audio_filter != "off":
        badges.append(f"\U0001f3db {state.audio_filter.title()}")
    if state and state.autoplay:
        badges.append("\U0001f525 Autoplay")
    if state and state.always_connected:
        badges.append("\U0001f504 24/7")
    badge_str = ("  " + " \u2022 ".join(badges)) if badges else ""

    desc = (
        f"**{song['name']}**\n"
        f"\U0001f3a4 {song['artist']}\n"
        f"\U0001f4bf {song['album']} \u2022 {song['year']}\n\n"
        f"{bar}{loop}{badge_str}"
    )
    footer_parts = ["320kbps"]
    if requester:
        footer_parts.append(f"Requested by @{requester.display_name}")
    footer_parts.append(BRAND)
    return Embedder.standard(
        "\U0001f3b5 Now Playing",
        desc,
        footer=" \u2022 ".join(footer_parts),
        thumbnail=song.get("image"),
    )


def _download_embed(
    song: Dict[str, Any], quality: str, size_mb: float
) -> discord.Embed:
    """Build a download embed."""
    desc = (
        f"\U0001f3a4 {song['artist']}\n"
        f"\U0001f4bf {song['album']}\n"
        f"\u23f1 {song['duration_formatted']}\n"
        f"\U0001f4ca {quality} \u2022 {size_mb:.1f} MB"
    )
    return Embedder.standard(
        f"\U0001f4e5 {song['name']}",
        desc,
        footer=BRAND,
        thumbnail=song.get("image"),
    )


def _queue_embed(state: GuildMusicState) -> discord.Embed:
    """Build the queue embed."""
    lines: List[str] = []
    if state.current:
        lines.append(f"\U0001f3b5 **Now Playing:** {state.current['name']} \u2014 {state.current['artist']}")

    if state.queue:
        lines.append("")
        for i, s in enumerate(state.queue, 1):
            lines.append(f"**{i}.** {s['name']} \u2014 {s['artist']}")
    elif not state.current:
        lines.append("The queue is empty.")

    total = len(state.queue) + (1 if state.current else 0)
    return Embedder.standard(
        "\U0001f4cb Music Queue",
        "\n".join(lines)[:MAX_EMBED_DESC],
        footer=f"{total} song{'s' if total != 1 else ''} in queue \u2022 {BRAND}",
    )


# =====================================================================
#  Helpers
# =====================================================================

def _sanitise_filename(artist: str, name: str) -> str:
    """Build a safe, length-limited filename for downloads."""
    safe_artist = "".join(c for c in artist if c.isalnum() or c in " -_").strip()
    safe_name = "".join(c for c in name if c.isalnum() or c in " -_").strip()
    base = f"{safe_artist} - {safe_name}"
    # Truncate to MAX_FILENAME_LEN (leaving room for .mp3)
    if len(base) > MAX_FILENAME_LEN - 4:
        base = base[: MAX_FILENAME_LEN - 4].rstrip()
    return f"{base}.mp3"


def _parse_seek_position(raw: str) -> Optional[float]:
    """Parse a seek position string like '1:30', '90', '0:45' into seconds."""
    raw = raw.strip()
    # Try M:SS or MM:SS format
    parts = raw.split(":")
    if len(parts) == 2:
        try:
            minutes = int(parts[0])
            seconds = int(parts[1])
            return float(minutes * 60 + seconds)
        except ValueError:
            return None
    # Try H:MM:SS
    if len(parts) == 3:
        try:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2])
            return float(hours * 3600 + minutes * 60 + seconds)
        except ValueError:
            return None
    # Try plain seconds
    try:
        val = float(raw)
        if val < 0:
            return None
        return val
    except ValueError:
        return None


def _fmt_seconds(s: float) -> str:
    """Format seconds into M:SS."""
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}"


def _split_text(text: str, max_len: int = 4000) -> List[str]:
    """Split text into chunks respecting line boundaries.

    Guards against non-positive *max_len* (which could otherwise cause an
    infinite loop) by falling back to a minimal chunk size.
    """
    # Ensure max_len is always positive to prevent infinite loops
    if max_len <= 0:
        max_len = 200  # minimal safe fallback

    if len(text) <= max_len:
        return [text]

    chunks: List[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Find last newline within limit
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


# =====================================================================
#  The Cog
# =====================================================================

class MusicCog(commands.Cog, name="Music"):
    """Music download, VC playback, lyrics, and platform URL resolution.

    Requires FFmpeg to be installed on the host system for voice
    channel playback (``apt install ffmpeg``).
    """

    def __init__(self, bot: "StarzaiBot") -> None:
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        self.music_api: Optional[MusicAPI] = None
        self.lyrics_fetcher: Optional[LyricsFetcher] = None
        self._states: Dict[int, GuildMusicState] = {}

    # ── Cog-wide authorization gate ──────────────────────────────────

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Runs before every music slash-command.

        If the guild is not in the bot’s allowlist the user sees a
        friendly embed with DM-link buttons for every configured owner.
        Owner-initiated commands always pass so they can run /allow.
        """
        # Always let bot owners through (they need to run /allow)
        if interaction.user.id in self.bot.settings.owner_ids:
            return True

        if self.bot.is_guild_allowed(interaction.guild_id):
            return True

        # Guild not allowed — show the owner-DM redirect
        embed = Embedder.error(
            "Bot Not Authorised",
            "This bot hasn’t been enabled for this server yet.\n\n"
            "Ask a **bot owner** to run `/allow` here, or DM them "
            "using the buttons below.",
        )
        view = _OwnerDMView(self.bot.settings.owner_ids)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except discord.NotFound:
            pass
        return False

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        """Create a shared aiohttp session when the cog is loaded."""
        self._session = aiohttp.ClientSession()
        self.music_api = MusicAPI(self._session)
        self.lyrics_fetcher = LyricsFetcher(self._session)
        logger.info("Music cog loaded — session created")

    async def cog_unload(self) -> None:
        """Clean up all VC connections and close the HTTP session."""
        for guild_id, state in list(self._states.items()):
            try:
                await self._stop_and_leave(guild_id)
            except Exception:
                pass
        self._states.clear()

        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("Music cog unloaded — session closed")

    # ── State helpers ─────────────────────────────────────────────────

    def _get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self._states:
            self._states[guild_id] = GuildMusicState()
        return self._states[guild_id]

    # ── Safe VC connection helper ─────────────────────────────────────

    async def _ensure_voice(
        self,
        guild: discord.Guild,
        channel: discord.VoiceChannel,
        state: GuildMusicState,
    ) -> Optional[discord.VoiceClient]:
        """Return a connected VoiceClient, reusing an existing one if possible.

        This method is the **single place** that creates or re-syncs the
        voice-client reference, which prevents the race where two concurrent
        ``connect()`` calls kill each other's streams.

        Must be called while holding ``state._lock``.
        """
        vc = state.voice_client

        # 1. Re-sync: discord.py tracks the guild's VC independently.
        #    If our local reference is stale, adopt the guild's client.
        guild_vc = guild.voice_client
        if guild_vc is not None and guild_vc.is_connected():
            if vc is None or vc != guild_vc:
                state.voice_client = guild_vc  # type: ignore[assignment]
                vc = guild_vc

        # 2. Already connected — just move if necessary.
        if vc is not None and vc.is_connected():
            if vc.channel.id != channel.id:
                await vc.move_to(channel)
            state.voice_client = vc  # type: ignore[assignment]
            return vc  # type: ignore[return-value]

        # 3. Not connected at all — clean up any dangling reference first
        #    so that discord.py doesn't raise "Already connected".
        if guild_vc is not None:
            try:
                await guild_vc.disconnect(force=True)
            except Exception:
                pass

        # 4. Fresh connect.
        vc = await channel.connect(self_deaf=True)
        state.voice_client = vc
        return vc

    def _tune_encoder(self, vc: discord.VoiceClient) -> None:
        """Max-out the Opus encoder knobs for best music quality."""
        if not hasattr(vc, "encoder"):
            return
        try:
            enc = vc.encoder
            enc.set_bitrate(MAX_ENCODER_BITRATE)       # 512 kbps ceiling
            enc.set_signal_type("music")                # optimise for music
            enc.set_bandwidth("full")                   # full 20 kHz bandwidth
            enc.set_fec(True)                           # forward error correction
            enc.set_expected_packet_loss_percent(0.05)  # 5 %
        except Exception as exc:
            logger.debug("Could not tune encoder: %s", exc)

    # ── Service availability guard ─────────────────────────────────────

    def _services_ready(self) -> bool:
        """Return True if all music services are initialised and available."""
        return (
            self._session is not None
            and self.music_api is not None
            and self.lyrics_fetcher is not None
        )

    async def _ensure_services(self, interaction: discord.Interaction) -> bool:
        """Check services are ready; send error and return False if not."""
        if self._services_ready():
            return True
        await interaction.response.send_message(
            embed=Embedder.error(
                "Service Unavailable",
                "\u274c Music services are not available right now. Please try again later.",
            ),
            ephemeral=True,
        )
        return False

    # ── Usage logging helper ──────────────────────────────────────────

    async def _log_usage(
        self,
        interaction: discord.Interaction,
        command: str,
        *,
        latency_ms: float = 0.0,
        success: bool = True,
        error_message: Optional[str] = None,
    ) -> None:
        """Log command usage to database following project convention."""
        try:
            if hasattr(self.bot, "database"):
                await self.bot.database.log_usage(
                    user_id=interaction.user.id,
                    command=command,
                    guild_id=interaction.guild_id,
                    latency_ms=latency_ms,
                    success=success,
                    error_message=error_message,
                )
        except Exception as exc:
            logger.warning("Failed to log usage for %s: %s", command, exc)

    # ── Resolve query (text or URL) ───────────────────────────────────

    async def _resolve_query(self, query: str) -> str:
        """If query is a music platform URL, resolve it to a search string."""
        if is_music_url(query) and self._session:
            resolved = await resolve_url(query, self._session)
            if resolved:
                return resolved
        return query

    # ── /music ────────────────────────────────────────────────────────

    @app_commands.command(name="music", description="Search and download a song")
    @app_commands.describe(query="Song name, artist, or link (Spotify, YouTube Music, YouTube, Deezer, Apple Music, SoundCloud, Tidal)")
    async def music_cmd(self, interaction: discord.Interaction, query: str) -> None:
        """Search for a song and present download/play options."""
        if not await _check_rate_limit(self.bot, interaction, expensive=True):
            return
        if not await self._ensure_services(interaction):
            return

        await interaction.response.defer()
        start = time.monotonic()

        search_query = await self._resolve_query(query)
        songs = await self.music_api.search(search_query, limit=7)

        latency_ms = (time.monotonic() - start) * 1000

        if songs is None:
            await interaction.followup.send(
                embed=Embedder.error(
                    "Service Unavailable",
                    "\u274c Music service is temporarily unavailable. Please try again later.",
                )
            )
            await self._log_usage(interaction, "music", latency_ms=latency_ms, success=False, error_message="API failure")
            return

        if not songs:
            await interaction.followup.send(
                embed=Embedder.error("No Results", "\u274c No songs found. Try a different search.")
            )
            await self._log_usage(interaction, "music", latency_ms=latency_ms, success=False, error_message="No results")
            return

        embed = _search_results_embed(search_query, songs)
        view = SongSelectView(songs, self, interaction, for_play=False)
        msg = await interaction.followup.send(embed=embed, view=view)
        view.message = msg
        await self._log_usage(interaction, "music", latency_ms=latency_ms)

    # ── /play ─────────────────────────────────────────────────────────

    @app_commands.command(name="play", description="Search and play a song in your voice channel")
    @app_commands.describe(query="Song name, artist, or link (Spotify, YouTube Music, YouTube, Deezer, Apple Music, SoundCloud, Tidal)")
    async def play_cmd(self, interaction: discord.Interaction, query: str) -> None:
        """Search for a song and play it in the user's voice channel."""
        if not await _check_rate_limit(self.bot, interaction, expensive=True):
            return
        if not await self._ensure_services(interaction):
            return

        if not interaction.guild:
            await interaction.response.send_message(
                embed=Embedder.error("Server Only", "This command can only be used in a server."),
                ephemeral=True,
            )
            return

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice or not member.voice.channel:
            await interaction.response.send_message(
                embed=Embedder.error("Not in VC", "\U0001f50a Join a voice channel first!"),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        start = time.monotonic()

        search_query = await self._resolve_query(query)
        songs = await self.music_api.search(search_query, limit=5)

        latency_ms = (time.monotonic() - start) * 1000

        if songs is None:
            await interaction.followup.send(
                embed=Embedder.error(
                    "Service Unavailable",
                    "\u274c Music service is temporarily unavailable. Please try again later.",
                )
            )
            await self._log_usage(interaction, "play", latency_ms=latency_ms, success=False, error_message="API failure")
            return

        if not songs:
            await interaction.followup.send(
                embed=Embedder.error("No Results", "\u274c No songs found. Try a different search.")
            )
            await self._log_usage(interaction, "play", latency_ms=latency_ms, success=False, error_message="No results")
            return

        if len(songs) == 1:
            song = await self.music_api.ensure_download_urls(songs[0])
            await self._play_song_in_vc(interaction, song, followup=True)
        else:
            embed = _search_results_embed(search_query, songs)
            view = SongSelectView(songs, self, interaction, for_play=True)
            msg = await interaction.followup.send(embed=embed, view=view)
            view.message = msg
        await self._log_usage(interaction, "play", latency_ms=latency_ms)

    # ── /skip ─────────────────────────────────────────────────────────

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if not await self._check_dj(interaction, state):
            return
        if state.voice_client and state.voice_client.is_playing():
            skipped_name = state.current["name"] if state.current else "current song"
            state.voice_client.stop()  # Triggers the after callback -> play_next
            await interaction.response.send_message(
                embed=Embedder.success("Skipped", f"\u23ed Skipped **{skipped_name}**")
            )
        else:
            await interaction.response.send_message(
                embed=Embedder.warning("Nothing Playing", "There\u2019s nothing to skip."),
                ephemeral=True,
            )

    # ── /stop (music-specific) ────────────────────────────────────────

    @app_commands.command(name="music-stop", description="Stop music and leave the voice channel")
    async def music_stop_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if state.voice_client:
            await self._stop_and_leave(interaction.guild_id)
            await interaction.response.send_message(
                embed=Embedder.info("Stopped", "\u23f9 Music stopped and left the voice channel.")
            )
        else:
            await interaction.response.send_message(
                embed=Embedder.warning("Not Connected", "I\u2019m not in a voice channel."),
                ephemeral=True,
            )

    # ── /queue ────────────────────────────────────────────────────────

    @app_commands.command(name="queue", description="Show the music queue")
    async def queue_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if len(state.queue) > QueuePaginationView.SONGS_PER_PAGE:
            view = QueuePaginationView(state, self, interaction.guild_id)
            await interaction.response.send_message(embed=view._build_embed(), view=view)
        else:
            await interaction.response.send_message(embed=_queue_embed(state))

    # ── /nowplaying ───────────────────────────────────────────────────

    @app_commands.command(name="nowplaying", description="Show the currently playing song")
    async def nowplaying_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if state.current:
            requester_id = state.requester_map.get(state.current["id"])
            requester = self.bot.get_user(requester_id) if requester_id else None
            embed = _now_playing_embed(state, state.current, requester)
            view = NowPlayingView(state.current, self, interaction.guild_id)
            await interaction.response.send_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(
                embed=Embedder.warning("Nothing Playing", "No song is currently playing."),
                ephemeral=True,
            )

    # ── /pause ────────────────────────────────────────────────────────

    @app_commands.command(name="pause", description="Pause the current song")
    async def pause_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_playing():
            state.pause_start_time = time.monotonic()
            state.voice_client.pause()
            await interaction.response.send_message(
                embed=Embedder.info("Paused", "\u23f8 Playback paused.")
            )
        else:
            await interaction.response.send_message(
                embed=Embedder.warning("Nothing Playing", "Nothing to pause."),
                ephemeral=True,
            )

    # ── /resume ───────────────────────────────────────────────────────

    @app_commands.command(name="resume", description="Resume playback")
    async def resume_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_paused():
            # Track time spent paused for accurate progress bar
            if state.pause_start_time > 0:
                state.paused_elapsed += time.monotonic() - state.pause_start_time
                state.pause_start_time = 0.0
            state.voice_client.resume()
            await interaction.response.send_message(
                embed=Embedder.success("Resumed", "\u25b6 Playback resumed.")
            )
        else:
            await interaction.response.send_message(
                embed=Embedder.warning("Not Paused", "Playback is not paused."),
                ephemeral=True,
            )

    # ── /volume ───────────────────────────────────────────────────────

    @app_commands.command(name="volume", description="Set playback volume (0-100)")
    @app_commands.describe(level="Volume level (0-100)")
    async def volume_cmd(self, interaction: discord.Interaction, level: int) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        level = max(0, min(100, level))
        state = self._get_state(interaction.guild_id)
        state.volume = level / 100.0

        if state.voice_client and state.voice_client.source:
            if hasattr(state.voice_client.source, "volume"):
                state.voice_client.source.volume = state.volume  # type: ignore[attr-defined]

        await interaction.response.send_message(
            embed=Embedder.info("Volume", f"\U0001f50a Volume set to **{level}%**")
        )

    # ── /shuffle ─────────────────────────────────────────────────────

    @app_commands.command(name="shuffle", description="Shuffle the music queue")
    async def shuffle_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if not await self._check_dj(interaction, state):
            return
        if len(state.queue) < 2:
            await interaction.response.send_message(
                embed=Embedder.warning("Can't Shuffle", "Need at least 2 songs in queue to shuffle."),
                ephemeral=True,
            )
            return
        random.shuffle(state.queue)
        await interaction.response.send_message(
            embed=Embedder.success("Shuffled", f"\U0001f500 Shuffled **{len(state.queue)}** songs in the queue.")
        )

    # ── /loop ────────────────────────────────────────────────────────

    @app_commands.command(name="loop", description="Set loop mode for the music player")
    @app_commands.describe(mode="Loop mode: off, track (repeat current), or queue (repeat all)")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Off", value="off"),
        app_commands.Choice(name="Track (repeat current song)", value="track"),
        app_commands.Choice(name="Queue (repeat entire queue)", value="queue"),
    ])
    async def loop_cmd(self, interaction: discord.Interaction, mode: str) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if not await self._check_dj(interaction, state):
            return
        state.loop_mode = mode
        icons = {"off": "\u274c", "track": "\U0001f502", "queue": "\U0001f501"}
        labels = {"off": "Off", "track": "Track", "queue": "Queue"}
        await interaction.response.send_message(
            embed=Embedder.success(
                "Loop Mode",
                f"{icons.get(mode, '')} Loop mode set to **{labels.get(mode, mode)}**",
            )
        )

    # ── /seek ────────────────────────────────────────────────────────

    @app_commands.command(name="seek", description="Seek to a position in the current song")
    @app_commands.describe(position="Position to seek to (e.g. '1:30', '90', '0:45')")
    async def seek_cmd(self, interaction: discord.Interaction, position: str) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if not state.current:
            await interaction.response.send_message(
                embed=Embedder.warning("Nothing Playing", "No song is currently playing."),
                ephemeral=True,
            )
            return
        if not state.voice_client or not state.voice_client.is_connected():
            await interaction.response.send_message(
                embed=Embedder.warning("Not Connected", "Not connected to a voice channel."),
                ephemeral=True,
            )
            return

        # Parse position string (supports M:SS or just seconds)
        seek_seconds = _parse_seek_position(position)
        if seek_seconds is None:
            await interaction.response.send_message(
                embed=Embedder.error("Invalid Position", "Use a format like `1:30` or `90` (seconds)."),
                ephemeral=True,
            )
            return

        duration = state.current.get("duration", 0) or 0
        if duration > 0 and seek_seconds >= duration:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Out of Range",
                    f"Song is only {_fmt_seconds(duration)} long.",
                ),
                ephemeral=True,
            )
            return

        stream_url = state._current_stream_url
        if not stream_url:
            stream_url = _pick_best_url(state.current.get("download_urls", []), "320kbps")
        if not stream_url:
            await interaction.response.send_message(
                embed=Embedder.error("Seek Failed", "No stream URL available."),
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        async with state._lock:
            # Stop current playback (mark as seeking so _play_next doesn't trigger)
            state._seeking = True
            if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
                state.voice_client.stop()

            # Small delay to let the stop callback fire
            await asyncio.sleep(0.3)

            # Restart playback at the seeked position
            await self._start_playback(interaction.guild_id, stream_url, seek_to=seek_seconds)

        await interaction.followup.send(
            embed=Embedder.success(
                "Seeked",
                f"\u23e9 Jumped to **{_fmt_seconds(seek_seconds)}**",
            )
        )

    # ── /remove ──────────────────────────────────────────────────────

    @app_commands.command(name="remove", description="Remove a song from the queue by position")
    @app_commands.describe(position="Queue position to remove (1-based)")
    async def remove_cmd(self, interaction: discord.Interaction, position: int) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if not await self._check_dj(interaction, state):
            return

        idx = position - 1
        if idx < 0 or idx >= len(state.queue):
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Invalid Position",
                    f"Queue has **{len(state.queue)}** songs. Use a number between 1 and {len(state.queue)}.",
                ),
                ephemeral=True,
            )
            return

        removed = state.queue.pop(idx)
        await interaction.response.send_message(
            embed=Embedder.success(
                "Removed",
                f"\U0001f5d1 Removed **{removed['name']}** \u2014 {removed['artist']} from the queue.",
            )
        )

    # ── /clear ───────────────────────────────────────────────────────

    @app_commands.command(name="clear", description="Clear the entire music queue")
    async def clear_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if not await self._check_dj(interaction, state):
            return

        count = len(state.queue)
        state.queue.clear()
        state.loop_mode = "off"
        await interaction.response.send_message(
            embed=Embedder.success(
                "Queue Cleared",
                f"\U0001f9f9 Cleared **{count}** song{'s' if count != 1 else ''} from the queue.\n"
                "Currently playing song will finish, then playback stops.",
            )
        )

    # ── /playnext ────────────────────────────────────────────────────

    @app_commands.command(name="playnext", description="Search and add a song to the front of the queue")
    @app_commands.describe(query="Song name, artist, or link")
    async def playnext_cmd(self, interaction: discord.Interaction, query: str) -> None:
        if not await _check_rate_limit(self.bot, interaction, expensive=True):
            return
        if not await self._ensure_services(interaction):
            return

        if not interaction.guild:
            await interaction.response.send_message(
                embed=Embedder.error("Server Only", "This command can only be used in a server."),
                ephemeral=True,
            )
            return

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice or not member.voice.channel:
            await interaction.response.send_message(
                embed=Embedder.error("Not in VC", "\U0001f50a Join a voice channel first!"),
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        search_query = await self._resolve_query(query)
        songs = await self.music_api.search(search_query, limit=1)

        if not songs:
            await interaction.followup.send(
                embed=Embedder.error("No Results", "\u274c No songs found. Try a different search.")
            )
            return

        song = await self.music_api.ensure_download_urls(songs[0])
        guild_id = interaction.guild.id
        state = self._get_state(guild_id)

        if song.get("id"):
            state.requester_map[song["id"]] = interaction.user.id

        # Use the lock to safely check playback state and mutate the queue
        async with state._lock:
            # If nothing is playing, just play it directly (release lock first)
            if not state.voice_client or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
                pass  # fall through — will call _play_song_in_vc below
            else:
                # Insert at the front of the queue
                state.queue.insert(0, song)
                await interaction.followup.send(
                    embed=Embedder.success(
                        "\u23cf\ufe0f Play Next",
                        f"**{song['name']}** \u2014 {song['artist']} will play next!",
                    )
                )
                return

        # Nothing playing — delegate to _play_song_in_vc which handles
        # its own locking.
        await self._play_song_in_vc(interaction, song, followup=True)

    # ── /skipto ──────────────────────────────────────────────────────

    @app_commands.command(name="skipto", description="Skip to a specific position in the queue")
    @app_commands.describe(position="Queue position to skip to (1-based)")
    async def skipto_cmd(self, interaction: discord.Interaction, position: int) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if not await self._check_dj(interaction, state):
            return

        idx = position - 1
        if idx < 0 or idx >= len(state.queue):
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Invalid Position",
                    f"Queue has **{len(state.queue)}** songs. Use a number between 1 and {len(state.queue)}.",
                ),
                ephemeral=True,
            )
            return

        if not state.voice_client or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
            await interaction.response.send_message(
                embed=Embedder.warning("Nothing Playing", "Nothing is currently playing."),
                ephemeral=True,
            )
            return

        # Remove all songs before the target position
        skipped = state.queue[:idx]
        target = state.queue[idx]
        state.queue = state.queue[idx:]  # target is now at front

        await interaction.response.send_message(
            embed=Embedder.success(
                "Skipped To",
                f"\u23ed Skipping to **{target['name']}** \u2014 {target['artist']}\n"
                f"Removed **{len(skipped)}** song{'s' if len(skipped) != 1 else ''} from queue.",
            )
        )
        state.voice_client.stop()  # triggers _play_next -> plays the target

    # ── /move ────────────────────────────────────────────────────────

    @app_commands.command(name="move", description="Move a song to a different position in the queue")
    @app_commands.describe(
        from_pos="Current position of the song (1-based)",
        to_pos="New position for the song (1-based)",
    )
    async def move_cmd(self, interaction: discord.Interaction, from_pos: int, to_pos: int) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if not await self._check_dj(interaction, state):
            return

        from_idx = from_pos - 1
        to_idx = to_pos - 1
        q_len = len(state.queue)

        if from_idx < 0 or from_idx >= q_len or to_idx < 0 or to_idx >= q_len:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Invalid Position",
                    f"Queue has **{q_len}** songs. Both positions must be between 1 and {q_len}.",
                ),
                ephemeral=True,
            )
            return

        song = state.queue.pop(from_idx)
        state.queue.insert(to_idx, song)
        await interaction.response.send_message(
            embed=Embedder.success(
                "Moved",
                f"\u21c5 Moved **{song['name']}** from position #{from_pos} to #{to_pos}.",
            )
        )

    # ── /swap ────────────────────────────────────────────────────────

    @app_commands.command(name="swap", description="Swap two songs in the queue")
    @app_commands.describe(pos1="First position (1-based)", pos2="Second position (1-based)")
    async def swap_cmd(self, interaction: discord.Interaction, pos1: int, pos2: int) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if not await self._check_dj(interaction, state):
            return

        idx1, idx2 = pos1 - 1, pos2 - 1
        q_len = len(state.queue)

        if idx1 < 0 or idx1 >= q_len or idx2 < 0 or idx2 >= q_len:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Invalid Position",
                    f"Queue has **{q_len}** songs. Both positions must be between 1 and {q_len}.",
                ),
                ephemeral=True,
            )
            return

        state.queue[idx1], state.queue[idx2] = state.queue[idx2], state.queue[idx1]
        await interaction.response.send_message(
            embed=Embedder.success(
                "Swapped",
                f"\U0001f500 Swapped **#{pos1}** ({state.queue[idx2]['name']}) "
                f"with **#{pos2}** ({state.queue[idx1]['name']}).",
            )
        )

    # ── /replay ──────────────────────────────────────────────────────

    @app_commands.command(name="replay", description="Restart the current song from the beginning")
    async def replay_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if not state.current:
            await interaction.response.send_message(
                embed=Embedder.warning("Nothing Playing", "No song is currently playing."),
                ephemeral=True,
            )
            return
        if not state.voice_client or not state.voice_client.is_connected():
            await interaction.response.send_message(
                embed=Embedder.warning("Not Connected", "Not connected to a voice channel."),
                ephemeral=True,
            )
            return

        stream_url = state._current_stream_url
        if not stream_url:
            stream_url = _pick_best_url(state.current.get("download_urls", []), "320kbps")
        if not stream_url:
            await interaction.response.send_message(
                embed=Embedder.error("Replay Failed", "No stream URL available."),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        async with state._lock:
            state._seeking = True
            if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
                state.voice_client.stop()
            await asyncio.sleep(0.3)
            await self._start_playback(interaction.guild_id, stream_url, seek_to=0.0)

        await interaction.followup.send(
            embed=Embedder.success("Replaying", f"\U0001f501 Restarted **{state.current['name']}**")
        )

    # ── /previous ────────────────────────────────────────────────────

    @app_commands.command(name="previous", description="Go back to the previously played song")
    async def previous_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)

        if not state.previous_song:
            await interaction.response.send_message(
                embed=Embedder.warning("No Previous Song", "There is no previous song to go back to."),
                ephemeral=True,
            )
            return

        if not state.voice_client or not state.voice_client.is_connected():
            await interaction.response.send_message(
                embed=Embedder.warning("Not Connected", "Not connected to a voice channel."),
                ephemeral=True,
            )
            return

        prev = state.previous_song
        prev = await self.music_api.ensure_download_urls(prev)
        stream_url = _pick_best_url(prev.get("download_urls", []), "320kbps")
        if not stream_url:
            await interaction.response.send_message(
                embed=Embedder.error("Unavailable", "Could not get a stream URL for the previous song."),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        async with state._lock:
            # Push current song to front of queue so it plays next
            if state.current:
                state.queue.insert(0, state.current)

            state._seeking = True
            if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
                state.voice_client.stop()
            await asyncio.sleep(0.3)

            state.current = prev
            state.previous_song = None  # prevent infinite back-and-forth
            await self._start_playback(interaction.guild_id, stream_url)

        requester = self.bot.get_user(interaction.user.id)
        embed = _now_playing_embed(state, prev, requester)
        embed.title = "\u23ee Now Playing (Previous)"
        await interaction.followup.send(embed=embed, view=NowPlayingView(prev, self, interaction.guild_id))

    # ── /grab ────────────────────────────────────────────────────────

    @app_commands.command(name="grab", description="Save the current song info to your DMs")
    async def grab_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if not state.current:
            await interaction.response.send_message(
                embed=Embedder.warning("Nothing Playing", "No song is currently playing."),
                ephemeral=True,
            )
            return

        song = state.current
        desc = (
            f"**{song['name']}**\n"
            f"\U0001f3a4 {song['artist']}\n"
            f"\U0001f4bf {song['album']} \u2022 {song['year']}\n"
            f"\u23f1 {song['duration_formatted']}"
        )
        embed = Embedder.standard(
            "\U0001f4be Saved Song",
            desc,
            footer=f"Saved from #{interaction.channel.name if interaction.channel else 'vc'} \u2022 {BRAND}",
            thumbnail=song.get("image"),
        )

        try:
            await interaction.user.send(embed=embed)
            await interaction.response.send_message(
                embed=Embedder.success("Saved", "\U0001f4be Sent the song info to your DMs!"),
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=Embedder.error("DMs Closed", "I can't send you a DM. Please enable DMs from server members."),
                ephemeral=True,
            )

    # ── /history ─────────────────────────────────────────────────────

    @app_commands.command(name="history", description="Show recently played songs")
    async def history_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)

        if not state.history:
            await interaction.response.send_message(
                embed=Embedder.warning("No History", "No songs have been played yet in this session."),
                ephemeral=True,
            )
            return

        lines: List[str] = []
        # Show most recent first
        for i, s in enumerate(reversed(state.history[-20:]), 1):
            dur = s.get("duration_formatted", "?:??")
            lines.append(f"**{i}.** {s['name']} \u2014 {s['artist']}  `{dur}`")

        await interaction.response.send_message(
            embed=Embedder.standard(
                "\U0001f553 Recently Played",
                "\n".join(lines)[:MAX_EMBED_DESC],
                footer=f"{len(state.history)} total songs played \u2022 {BRAND}",
            )
        )

    # ── /duplicates ──────────────────────────────────────────────────

    @app_commands.command(name="duplicates", description="Remove duplicate songs from the queue")
    async def duplicates_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if not await self._check_dj(interaction, state):
            return

        if not state.queue:
            await interaction.response.send_message(
                embed=Embedder.warning("Empty Queue", "The queue is empty."),
                ephemeral=True,
            )
            return

        seen: set = set()
        unique: List[Dict[str, Any]] = []
        removed = 0
        for s in state.queue:
            song_id = s.get("id", "")
            if song_id and song_id in seen:
                removed += 1
            else:
                seen.add(song_id)
                unique.append(s)

        state.queue = unique
        if removed == 0:
            await interaction.response.send_message(
                embed=Embedder.info("No Duplicates", "There are no duplicate songs in the queue."),
            )
        else:
            await interaction.response.send_message(
                embed=Embedder.success(
                    "Duplicates Removed",
                    f"\U0001f9f9 Removed **{removed}** duplicate{'s' if removed != 1 else ''} from the queue.",
                )
            )

    # ── /247 ─────────────────────────────────────────────────────────

    @app_commands.command(name="247", description="Toggle 24/7 mode (stay in VC even when idle)")
    async def always_on_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)
        if not await self._check_dj(interaction, state):
            return

        state.always_connected = not state.always_connected

        if state.always_connected:
            # Cancel any pending idle disconnect
            if state.idle_task and not state.idle_task.done():
                state.idle_task.cancel()
                state.idle_task = None
            await interaction.response.send_message(
                embed=Embedder.success(
                    "24/7 Mode Enabled",
                    "\U0001f504 I'll stay in the voice channel even when idle or alone.\n"
                    "Use `/247` again to disable.",
                )
            )
        else:
            await interaction.response.send_message(
                embed=Embedder.info(
                    "24/7 Mode Disabled",
                    "\u23f0 I'll auto-disconnect after being idle for "
                    f"{VC_IDLE_TIMEOUT // 60} minutes.",
                )
            )

    # ── /voteskip ────────────────────────────────────────────────────

    @app_commands.command(name="voteskip", description="Vote to skip the current song")
    async def voteskip_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)

        if not state.voice_client or not state.voice_client.is_connected():
            await interaction.response.send_message(
                embed=Embedder.warning("Not Connected", "Not connected to a voice channel."),
                ephemeral=True,
            )
            return

        if not state.voice_client.is_playing():
            await interaction.response.send_message(
                embed=Embedder.warning("Nothing Playing", "There's nothing to skip."),
                ephemeral=True,
            )
            return

        # Only count users actually in the VC
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member or not member.voice or member.voice.channel != state.voice_client.channel:
            await interaction.response.send_message(
                embed=Embedder.error("Not in VC", "You must be in the voice channel to vote."),
                ephemeral=True,
            )
            return

        state.skip_votes.add(interaction.user.id)
        humans = sum(1 for m in state.voice_client.channel.members if not m.bot)
        needed = max(1, (humans + 1) // 2)  # majority (at least 1)
        current_votes = len(state.skip_votes)

        if current_votes >= needed:
            skipped_name = state.current["name"] if state.current else "current song"
            state.skip_votes.clear()
            state.voice_client.stop()
            await interaction.response.send_message(
                embed=Embedder.success("Vote Skip", f"\u23ed Vote passed! Skipped **{skipped_name}** ({current_votes}/{needed})")
            )
        else:
            await interaction.response.send_message(
                embed=Embedder.info(
                    "Vote Skip",
                    f"\U0001f5f3 **{interaction.user.display_name}** voted to skip "
                    f"({current_votes}/{needed} needed).",
                )
            )

    # ── /autoplay ────────────────────────────────────────────────────

    @app_commands.command(name="autoplay", description="Toggle autoplay — auto-queue similar songs when queue ends")
    async def autoplay_cmd(self, interaction: discord.Interaction) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)

        state.autoplay = not state.autoplay
        if state.autoplay:
            await interaction.response.send_message(
                embed=Embedder.success(
                    "Autoplay Enabled",
                    "\U0001f525 When the queue ends, I'll automatically find and queue similar songs.\n"
                    "Use `/autoplay` again to disable.",
                )
            )
        else:
            await interaction.response.send_message(
                embed=Embedder.info(
                    "Autoplay Disabled",
                    "\u23f9 Autoplay turned off. Playback will stop when the queue ends.",
                )
            )

    # ── /filter ──────────────────────────────────────────────────────

    @app_commands.command(name="filter", description="Apply an audio filter to playback")
    @app_commands.describe(name="Filter to apply")
    @app_commands.choices(name=[
        app_commands.Choice(name="Off (no filter)", value="off"),
        app_commands.Choice(name="Bass Boost", value="bassboost"),
        app_commands.Choice(name="Nightcore", value="nightcore"),
        app_commands.Choice(name="Vaporwave", value="vaporwave"),
        app_commands.Choice(name="Karaoke", value="karaoke"),
        app_commands.Choice(name="8D Audio", value="8d"),
        app_commands.Choice(name="Treble Boost", value="treble"),
        app_commands.Choice(name="Vibrato", value="vibrato"),
        app_commands.Choice(name="Tremolo", value="tremolo"),
        app_commands.Choice(name="Pop", value="pop"),
        app_commands.Choice(name="Soft", value="soft"),
        app_commands.Choice(name="Loud", value="loud"),
    ])
    async def filter_cmd(self, interaction: discord.Interaction, name: str) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = self._get_state(interaction.guild_id)

        if name not in AUDIO_FILTERS:
            await interaction.response.send_message(
                embed=Embedder.error("Invalid Filter", f"Unknown filter: `{name}`."),
                ephemeral=True,
            )
            return

        old_filter = state.audio_filter
        state.audio_filter = name

        # If something is playing, restart playback with the new filter
        if state.voice_client and state.voice_client.is_connected() and state.current:
            stream_url = state._current_stream_url
            if not stream_url:
                stream_url = _pick_best_url(state.current.get("download_urls", []), "320kbps")
            if stream_url:
                current_pos = state.current_position
                await interaction.response.defer()
                async with state._lock:
                    state._seeking = True
                    if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
                        state.voice_client.stop()
                    await asyncio.sleep(0.3)
                    await self._start_playback(interaction.guild_id, stream_url, seek_to=current_pos)

                label = name.title() if name != "off" else "Off"
                await interaction.followup.send(
                    embed=Embedder.success(
                        "Filter Applied",
                        f"\U0001f3db Filter set to **{label}**. Playback restarted at current position.",
                    )
                )
                return

        # Not currently playing — just store the setting
        label = name.title() if name != "off" else "Off"
        await interaction.response.send_message(
            embed=Embedder.success(
                "Filter Set",
                f"\U0001f3db Audio filter set to **{label}**. It will apply to the next song.",
            )
        )

    # ── /djrole ──────────────────────────────────────────────────────

    @app_commands.command(name="djrole", description="Set or clear the DJ role for music commands")
    @app_commands.describe(role="Role required for queue management (leave empty to clear)")
    async def djrole_cmd(
        self, interaction: discord.Interaction, role: Optional[discord.Role] = None
    ) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        # Only server admins or bot owners can set the DJ role
        is_admin = interaction.user.guild_permissions.manage_guild if hasattr(interaction.user, "guild_permissions") else False
        is_owner = interaction.user.id in self.bot.settings.owner_ids
        if not is_admin and not is_owner:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Permission Denied",
                    "You need **Manage Server** permission to set the DJ role.",
                ),
                ephemeral=True,
            )
            return

        state = self._get_state(interaction.guild_id)
        if role is None:
            state.dj_role_id = None
            await interaction.response.send_message(
                embed=Embedder.success(
                    "DJ Role Cleared",
                    "\U0001f3a7 DJ role restriction removed. Anyone can manage the queue.",
                )
            )
        else:
            state.dj_role_id = role.id
            await interaction.response.send_message(
                embed=Embedder.success(
                    "DJ Role Set",
                    f"\U0001f3a7 DJ role set to **{role.name}**. Only members with this role "
                    "can shuffle, loop, remove, clear, and skip.",
                )
            )

    # ── DJ role check helper ─────────────────────────────────────────

    async def _check_dj(
        self, interaction: discord.Interaction, state: GuildMusicState
    ) -> bool:
        """Return True if the user has DJ permissions (or no DJ role is set)."""
        if state.dj_role_id is None:
            return True  # No restriction
        # Bot owners always pass
        if interaction.user.id in self.bot.settings.owner_ids:
            return True
        # Check if user has the DJ role
        if interaction.guild:
            member = interaction.guild.get_member(interaction.user.id)
            if member:
                for r in member.roles:
                    if r.id == state.dj_role_id:
                        return True
        await interaction.response.send_message(
            embed=Embedder.error(
                "DJ Only",
                "\U0001f3a7 You need the **DJ** role to use this command.",
            ),
            ephemeral=True,
        )
        return False

    # ── /lyrics ───────────────────────────────────────────────────────

    @app_commands.command(name="lyrics", description="Search for song lyrics")
    @app_commands.describe(query="Song name and/or artist")
    async def lyrics_cmd(self, interaction: discord.Interaction, query: str) -> None:
        if not await _check_rate_limit(self.bot, interaction):
            return
        if not await self._ensure_services(interaction):
            return
        await self._send_lyrics(interaction, query)

    # ── Internal: send lyrics ─────────────────────────────────────────

    async def _send_lyrics(
        self,
        interaction: discord.Interaction,
        query: str,
        artist: str = "",
        title: str = "",
    ) -> None:
        """Search for lyrics and send them as embeds."""
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            pass

        start = time.monotonic()
        result = await self.lyrics_fetcher.search(query, artist=artist, title=title)
        latency_ms = (time.monotonic() - start) * 1000

        if not result:
            embed = Embedder.error("Lyrics Not Found", "\u274c Couldn\u2019t find lyrics for that song.")
            try:
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception:
                pass
            await self._log_usage(interaction, "lyrics", latency_ms=latency_ms, success=False, error_message="Not found")
            return

        if result.get("instrumental"):
            embed = Embedder.info(
                f"\U0001f3b5 {result.get('track', query)}",
                "\U0001f3b5 This is an instrumental track \u2014 no lyrics available.",
            )
            try:
                await interaction.followup.send(embed=embed)
            except Exception:
                pass
            await self._log_usage(interaction, "lyrics", latency_ms=latency_ms)
            return

        lyrics_text = result["lyrics"]
        track_name = result.get("track", query)
        artist_name = result.get("artist", "")

        header = f"\U0001f3a4 {artist_name}" if artist_name else ""
        chunks = _split_text(lyrics_text, MAX_EMBED_DESC - len(header) - 10)

        embeds: List[discord.Embed] = []
        for i, chunk in enumerate(chunks):
            desc = f"{header}\n\n{chunk}" if i == 0 and header else chunk
            embed = Embedder.standard(
                f"\U0001f4dd {track_name}" if i == 0 else f"\U0001f4dd {track_name} (cont.)",
                desc[:MAX_EMBED_DESC],
                footer=f"Source: {result.get('source', 'Unknown')} \u2022 {BRAND}" if i == len(chunks) - 1 else BRAND,
            )
            embeds.append(embed)

        try:
            for embed in embeds:
                await interaction.followup.send(embed=embed)
        except Exception:
            pass

        await self._log_usage(interaction, "lyrics", latency_ms=latency_ms)

    # ── Internal: download a song ─────────────────────────────────────

    def _guild_upload_limit(self, interaction: discord.Interaction) -> int:
        """Return the upload-size ceiling for the current guild.

        Uses discord.py's ``Guild.filesize_limit`` which already accounts
        for the server's Nitro Boost tier (25 MB / 50 MB / 100 MB).
        Falls back to 25 MB when used in DMs or if unavailable.
        """
        if interaction.guild is not None:
            try:
                return interaction.guild.filesize_limit
            except Exception:
                pass
        return DISCORD_UPLOAD_FALLBACK

    @staticmethod
    async def _ffmpeg_reencode(src: str, dst: str, bitrate_kbps: int) -> bool:
        """Re-encode *src* MP3 to *dst* at the given bitrate using FFmpeg.

        Returns True on success, False on failure.
        """
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", src,
            "-b:a", f"{bitrate_kbps}k",
            "-map", "a",           # audio only
            "-write_xing", "1",    # proper VBR header
            dst,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("FFmpeg re-encode failed (rc=%s): %s", proc.returncode, stderr.decode(errors="replace")[:500])
            return False
        return True

    async def _download_song(
        self,
        interaction: discord.Interaction,
        song: Dict[str, Any],
        quality: str,
    ) -> None:
        """Download a song and send it as a **single playable .mp3**.

        Flow:
        1. Download the full-quality file from the API into a temp file.
        2. If it already fits within the guild's upload limit → send as-is.
        3. If it's too large → use FFmpeg to re-encode to the highest
           bitrate that fits, then send the re-encoded file.

        The API download URL is **never** exposed to users.
        """
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            pass

        # Guard against session being closed (e.g. cog unloaded while UI active)
        if not self._services_ready():
            try:
                await interaction.followup.send(
                    embed=Embedder.error(
                        "Service Unavailable",
                        "\u274c Music services are not available right now. Please try again later.",
                    ),
                    ephemeral=True,
                )
            except Exception:
                pass
            return

        start = time.monotonic()
        download_url = _get_url_for_quality(song.get("download_urls", []), quality)
        if not download_url:
            latency_ms = (time.monotonic() - start) * 1000
            await interaction.followup.send(
                embed=Embedder.error(
                    "Download Failed",
                    "\u274c No download URL available for this song. Try another quality.",
                ),
                ephemeral=True,
            )
            await self._log_usage(interaction, "music-download", latency_ms=latency_ms, success=False, error_message="No download URL")
            return

        upload_limit = self._guild_upload_limit(interaction)
        tmp_original: Optional[str] = None
        tmp_reencoded: Optional[str] = None

        try:
            # ── 1. Download full-quality file to a temp file ─────────
            tmp_fd, tmp_original = tempfile.mkstemp(suffix=".mp3")
            os.close(tmp_fd)

            async with self._session.get(
                download_url,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    latency_ms = (time.monotonic() - start) * 1000
                    await interaction.followup.send(
                        embed=Embedder.error("Download Failed", "\u274c Could not download the song file."),
                        ephemeral=True,
                    )
                    await self._log_usage(interaction, "music-download", latency_ms=latency_ms, success=False, error_message=f"HTTP {resp.status}")
                    return

                # Reject absurdly large files up-front via Content-Length
                content_length = resp.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except (TypeError, ValueError):
                        pass
                    else:
                        if declared_size > MAX_DOWNLOAD_SIZE:
                            latency_ms = (time.monotonic() - start) * 1000
                            size_mb = declared_size / (1024 * 1024)
                            await interaction.followup.send(
                                embed=Embedder.warning(
                                    "File Too Large",
                                    f"\u26a0\ufe0f File is {size_mb:.1f} MB which exceeds the safety limit.\n"
                                    "Try a lower quality.",
                                ),
                                ephemeral=True,
                            )
                            await self._log_usage(interaction, "music-download", latency_ms=latency_ms, success=False, error_message=f"File too large ({size_mb:.1f} MB)")
                            return

                # Stream to temp file
                downloaded = 0
                with open(tmp_original, "wb") as fp:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > MAX_DOWNLOAD_SIZE:
                            latency_ms = (time.monotonic() - start) * 1000
                            await interaction.followup.send(
                                embed=Embedder.warning(
                                    "File Too Large",
                                    "\u26a0\ufe0f File exceeds the safety limit. Try a lower quality.",
                                ),
                                ephemeral=True,
                            )
                            await self._log_usage(interaction, "music-download", latency_ms=latency_ms, success=False, error_message="File too large (streamed)")
                            return
                        fp.write(chunk)

            original_size = os.path.getsize(tmp_original)
            filename = _sanitise_filename(song["artist"], song["name"])

            # ── 2. Decide: send as-is or re-encode ──────────────────
            send_path = tmp_original
            actual_quality = quality

            if original_size > upload_limit:
                # Calculate the highest bitrate that fits within ~95% of
                # the upload limit (margin for container overhead / VBR).
                duration = song.get("duration", 0) or 0
                if duration <= 0:
                    # Estimate duration from original file size at 320 kbps
                    duration = max((original_size * 8) / (320 * 1000), 30)

                target_bytes = int(upload_limit * 0.95)
                target_kbps = int((target_bytes * 8) / (duration * 1000))
                target_kbps = max(target_kbps, MIN_BITRATE_KBPS)

                if target_kbps < MIN_BITRATE_KBPS:
                    # Song is so long that even 64 kbps won't fit
                    latency_ms = (time.monotonic() - start) * 1000
                    limit_mb = upload_limit / (1024 * 1024)
                    await interaction.followup.send(
                        embed=Embedder.warning(
                            "Song Too Long",
                            f"\u26a0\ufe0f This song is too long to fit in a single "
                            f"{limit_mb:.0f} MB upload even at minimum quality.\n"
                            "Try a shorter song or use `/play` to stream it in VC instead.",
                        ),
                        ephemeral=True,
                    )
                    await self._log_usage(interaction, "music-download", latency_ms=latency_ms, success=False, error_message="Song too long for re-encode")
                    return

                # Re-encode with FFmpeg
                tmp_fd2, tmp_reencoded = tempfile.mkstemp(suffix=".mp3")
                os.close(tmp_fd2)

                logger.info(
                    "Re-encoding '%s' from %s (%.1f MB) → %d kbps to fit %d MB limit",
                    song.get("name"), quality,
                    original_size / (1024 * 1024),
                    target_kbps,
                    upload_limit / (1024 * 1024),
                )

                ok = await self._ffmpeg_reencode(tmp_original, tmp_reencoded, target_kbps)
                if not ok or not os.path.exists(tmp_reencoded) or os.path.getsize(tmp_reencoded) == 0:
                    latency_ms = (time.monotonic() - start) * 1000
                    await interaction.followup.send(
                        embed=Embedder.error("Re-encode Failed", "\u274c Could not compress the song. Try a lower quality."),
                        ephemeral=True,
                    )
                    await self._log_usage(interaction, "music-download", latency_ms=latency_ms, success=False, error_message="FFmpeg re-encode failed")
                    return

                send_path = tmp_reencoded
                actual_quality = f"~{target_kbps}kbps"

            # ── 3. Send the single .mp3 ─────────────────────────────
            final_size = os.path.getsize(send_path)
            final_mb = final_size / (1024 * 1024)

            with open(send_path, "rb") as fp:
                file = discord.File(fp, filename=filename)
                embed = _download_embed(song, actual_quality, final_mb)

                # If we re-encoded, note it on the embed
                if send_path == tmp_reencoded:
                    embed.description += (
                        f"\n\n\u2139\ufe0f Re-encoded to **{actual_quality}** to fit "
                        f"the server's {upload_limit / (1024 * 1024):.0f} MB upload limit."
                    )

                await interaction.followup.send(embed=embed, file=file)

            latency_ms = (time.monotonic() - start) * 1000
            await self._log_usage(interaction, "music-download", latency_ms=latency_ms)

        except asyncio.TimeoutError:
            latency_ms = (time.monotonic() - start) * 1000
            await interaction.followup.send(
                embed=Embedder.error("Timeout", "\u274c Download timed out. Please try again."),
                ephemeral=True,
            )
            await self._log_usage(interaction, "music-download", latency_ms=latency_ms, success=False, error_message="Timeout")
        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1000
            logger.error("Download error: %s", exc, exc_info=True)
            await interaction.followup.send(
                embed=Embedder.error("Download Failed", "\u274c An error occurred during download."),
                ephemeral=True,
            )
            await self._log_usage(interaction, "music-download", latency_ms=latency_ms, success=False, error_message=str(exc))
        finally:
            # Clean up temp files
            for path in (tmp_original, tmp_reencoded):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    # ── Internal: send a response/followup safely ──────────────────────

    async def _send(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed,
        view: Optional[discord.ui.View] = None,
        ephemeral: bool = False,
        followup: bool = False,
    ) -> None:
        """Send an embed via the most appropriate method (response / followup)."""
        try:
            if followup or interaction.response.is_done():
                await interaction.followup.send(embed=embed, view=view, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(embed=embed, view=view, ephemeral=ephemeral)
        except Exception:
            pass

    # ── Internal: play song in VC ─────────────────────────────────────

    async def _play_song_in_vc(
        self,
        interaction: discord.Interaction,
        song: Dict[str, Any],
        *,
        followup: bool = False,
    ) -> None:
        """Join VC (if needed) and play/queue the song.

        All VC-mutation logic is serialised through ``state._lock`` so that
        concurrent ``/play`` commands cannot race each other and kill an
        active stream.
        """
        # Defer early — VC connect + API calls can exceed Discord's 3s deadline
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
                followup = True  # After deferring we must use followup
        except Exception:
            pass

        # Guard against session being closed (e.g. cog unloaded while UI active)
        if not self._services_ready():
            await self._send(
                interaction,
                embed=Embedder.error(
                    "Service Unavailable",
                    "\u274c Music services are not available right now. Please try again later.",
                ),
                ephemeral=True, followup=followup,
            )
            return

        if not interaction.guild:
            await self._send(
                interaction,
                embed=Embedder.error("Error", "\u274c This command can only be used in a server."),
                ephemeral=True, followup=followup,
            )
            return

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice or not member.voice.channel:
            await self._send(
                interaction,
                embed=Embedder.error("Not in VC", "\U0001f50a Join a voice channel first!"),
                ephemeral=True, followup=followup,
            )
            return

        voice_channel = member.voice.channel
        guild_id = interaction.guild.id
        state = self._get_state(guild_id)

        # Ensure download URL exists (HTTP work — done OUTSIDE the lock
        # so we don't hold the lock during a slow network request).
        song = await self.music_api.ensure_download_urls(song)
        stream_url = _pick_best_url(song.get("download_urls", []), "320kbps")
        if not stream_url:
            await self._send(
                interaction,
                embed=Embedder.error("No Stream", "\u274c Could not find a stream URL for this song."),
                ephemeral=True, followup=followup,
            )
            return

        # ── All VC / state mutation below is protected by the lock ────
        async with state._lock:
            # Store requester (only when the song has a valid ID)
            if song.get("id"):
                state.requester_map[song["id"]] = interaction.user.id
            state.text_channel = interaction.channel

            # Connect to VC using the safe helper
            try:
                vc = await self._ensure_voice(interaction.guild, voice_channel, state)
                self._tune_encoder(vc)
            except Exception as exc:
                logger.error("VC connection error: %s", exc, exc_info=True)
                await self._send(
                    interaction,
                    embed=Embedder.error("Connection Error", "\u274c Could not join the voice channel."),
                    ephemeral=True, followup=followup,
                )
                return

            # Cancel idle task if it exists
            if state.idle_task and not state.idle_task.done():
                state.idle_task.cancel()
                state.idle_task = None

            # If already playing, queue the song — do NOT touch playback
            if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
                state.queue.append(song)
                pos = len(state.queue)
                await self._send(
                    interaction,
                    embed=Embedder.standard(
                        "\U0001f3b5 Added to Queue",
                        f"**{song['name']}** \u2014 {song['artist']}\nPosition: #{pos}",
                        footer=BRAND,
                    ),
                    followup=followup,
                )
                return

            # Nothing playing — start playback immediately
            state.current = song
            await self._start_playback(guild_id, stream_url)

        # ── Lock released — safe to do slow Discord API work ──────────
        requester = self.bot.get_user(interaction.user.id)
        embed = _now_playing_embed(state, song, requester)
        view = NowPlayingView(song, self, guild_id)
        await self._send(interaction, embed=embed, view=view, followup=followup)

    # ── Internal: start FFmpeg playback ───────────────────────────────

    async def _start_playback(
        self, guild_id: int, url: str, *, seek_to: float = 0.0
    ) -> None:
        """Start FFmpeg playback on the guild's voice client.

        Parameters
        ----------
        seek_to:
            If > 0 FFmpeg skips to this position (seconds) using ``-ss``.
        """
        state = self._get_state(guild_id)
        if not state.voice_client or not state.voice_client.is_connected():
            return

        try:
            # Build FFmpeg options, injecting -ss for seeking and -af for filters
            before_opts = FFMPEG_OPTIONS["before_options"]
            if seek_to > 0:
                before_opts = f"-ss {seek_to:.2f} {before_opts}"

            output_opts = FFMPEG_OPTIONS["options"]
            af_chain = AUDIO_FILTERS.get(state.audio_filter, "")
            if af_chain:
                output_opts = f"{output_opts} -af {af_chain}"

            ffmpeg_opts = {
                "before_options": before_opts,
                "options": output_opts,
            }

            source = discord.FFmpegPCMAudio(url, **ffmpeg_opts)
            source = discord.PCMVolumeTransformer(source, volume=state.volume)

            # Track playback position
            state._current_stream_url = url
            state.playback_start_time = time.monotonic()
            state.position_offset = seek_to
            state.paused_elapsed = 0.0

            state.voice_client.play(
                source,
                after=lambda e: self.bot.loop.call_soon_threadsafe(
                    asyncio.ensure_future, self._play_next(guild_id, e)
                ),
            )
        except Exception as exc:
            logger.error("Playback start error for guild %d: %s", guild_id, exc, exc_info=True)
            state.current = None

    # ── Internal: play next in queue ──────────────────────────────────

    async def _play_next(self, guild_id: int, error: Optional[Exception] = None) -> None:
        """Called when a song ends; plays the next in queue or starts idle timer.

        Acquires ``state._lock`` to prevent races with concurrent ``/play``
        commands that might also try to mutate the queue or start playback.
        """
        if error:
            logger.error("Playback error in guild %d: %s", guild_id, error)

        state = self._get_state(guild_id)

        # If we were just seeking (not actually finishing), do nothing
        if state._seeking:
            state._seeking = False
            return

        text_ch = None
        np_song: Optional[Dict[str, Any]] = None
        autoplay_triggered = False

        async with state._lock:
            # ── Track history & previous ─────────────────────────────
            if state.current:
                state.previous_song = state.current
                state.history.append(state.current)
                if len(state.history) > MAX_HISTORY:
                    state.history = state.history[-MAX_HISTORY:]

            # Reset vote-skip votes for the new track
            state.skip_votes.clear()

            # ── Loop mode: TRACK — replay the same song ──────────────
            if state.loop_mode == "track" and state.current:
                stream_url = state._current_stream_url or _pick_best_url(
                    state.current.get("download_urls", []), "320kbps"
                )
                if stream_url:
                    await self._start_playback(guild_id, stream_url)
                    return

            # ── Loop mode: QUEUE — append current to the end before advancing
            if state.loop_mode == "queue" and state.current:
                state.queue.append(state.current)

            # ── Autoplay: if queue is empty and autoplay is on, find similar ─
            if not state.queue and state.autoplay and state.current:
                autoplay_triggered = True
                # Search for more by the same artist
                try:
                    artist = state.current.get("artist", "")
                    if artist and self.music_api:
                        suggestions = await self.music_api.search(artist, limit=5)
                        if suggestions:
                            # Filter out the song that just played
                            current_id = state.current.get("id", "")
                            for s in suggestions:
                                if s.get("id") != current_id:
                                    s = await self.music_api.ensure_download_urls(s)
                                    if _pick_best_url(s.get("download_urls", []), "320kbps"):
                                        state.queue.append(s)
                            logger.info(
                                "Autoplay: queued %d songs by '%s' in guild %d",
                                len(state.queue), artist, guild_id,
                            )
                except Exception as exc:
                    logger.warning("Autoplay search failed: %s", exc)

            # Iterate through the queue until we find a playable track or the queue is empty.
            while state.queue:
                next_song = state.queue.pop(0)
                state.current = next_song
                stream_url = _pick_best_url(next_song.get("download_urls", []), "320kbps")
                if stream_url:
                    await self._start_playback(guild_id, stream_url)
                    text_ch = state.text_channel
                    np_song = next_song
                    break
                else:
                    logger.warning("Skipping song '%s' — no stream URL", next_song.get("name", ""))
                    state.current = None
            else:
                # Queue exhausted — nothing more to play.
                state.current = None
                state.playback_start_time = 0.0
                if not state.always_connected:
                    if state.voice_client and state.voice_client.is_connected():
                        state.idle_task = asyncio.ensure_future(self._idle_disconnect(guild_id))
                return

        # ── Lock released — send the Now Playing embed ────────────
        if text_ch and np_song:
            try:
                requester_id = state.requester_map.get(np_song.get("id", ""))
                requester = self.bot.get_user(requester_id) if requester_id else None
                embed = _now_playing_embed(state, np_song, requester)
                if autoplay_triggered:
                    embed.set_footer(text=f"Autoplay \u2022 {BRAND}")
                view = NowPlayingView(np_song, self, guild_id)
                await text_ch.send(embed=embed, view=view)
            except Exception:
                pass

    # ── Internal: idle disconnect ─────────────────────────────────────

    async def _idle_disconnect(self, guild_id: int) -> None:
        """Disconnect from VC after idle timeout.  Respects 24/7 mode."""
        try:
            await asyncio.sleep(VC_IDLE_TIMEOUT)
            state = self._get_state(guild_id)

            # 24/7 mode — never auto-disconnect
            if state.always_connected:
                return

            if state.voice_client and state.voice_client.is_connected():
                if not state.voice_client.is_playing() and not state.voice_client.is_paused():
                    if state.text_channel:
                        try:
                            embed = Embedder.info(
                                "\U0001f44b Disconnected",
                                "Left the voice channel due to inactivity.",
                            )
                            await state.text_channel.send(embed=embed)
                        except Exception:
                            pass
                    await self._stop_and_leave(guild_id)
        except asyncio.CancelledError:
            pass  # Task was cancelled because new playback started
        except Exception as exc:
            logger.error("Idle disconnect error for guild %d: %s", guild_id, exc)

    # ── Internal: stop and leave ──────────────────────────────────────

    async def _stop_and_leave(self, guild_id: int) -> None:
        """Stop playback, clear queue, and disconnect from VC.

        Sets ``_seeking = True`` before calling ``stop()`` so that the
        ``after`` callback (``_play_next``) is a no-op and doesn't
        race with the cleanup below.
        """
        state = self._get_state(guild_id)

        # 1. Prevent the after-callback from doing anything.
        state._seeking = True

        # 2. Stop playback first (fires the after-callback synchronously
        #    inside discord.py, but _seeking guard makes it a no-op).
        if state.voice_client:
            try:
                if state.voice_client.is_playing() or state.voice_client.is_paused():
                    state.voice_client.stop()
                await state.voice_client.disconnect(force=True)
            except Exception:
                pass
            state.voice_client = None

        # 3. Now it's safe to wipe everything.
        state.clear()

    # ── Voice state change listener ───────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Handle auto-leave when alone and auto-resume when bot is moved/reconnected."""
        guild_id = member.guild.id
        state = self._get_state(guild_id)

        # ── Bot itself was disconnected (e.g. moved, kicked) ──────
        if member.id == self.bot.user.id:  # type: ignore[union-attr]
            if before.channel and not after.channel:
                # Bot was disconnected from VC — save state for potential resume
                if state.current or state.queue:
                    state._resume_info = {
                        "current": state.current,
                        "queue": list(state.queue),
                        "loop_mode": state.loop_mode,
                        "position": state.current_position,
                        "stream_url": state._current_stream_url,
                        "channel_id": before.channel.id,
                    }
                    logger.info(
                        "Bot disconnected from VC in guild %d — saved resume state "
                        "(song=%s, pos=%.1fs, queue=%d)",
                        guild_id,
                        state.current.get("name", "?") if state.current else "none",
                        state.current_position,
                        len(state.queue),
                    )
                state.voice_client = None
                state.playback_start_time = 0.0
                return

            if not before.channel and after.channel:
                # Bot just joined a VC — try to auto-resume if we have saved state
                resume = state._resume_info
                if resume and resume.get("current"):
                    logger.info("Bot rejoined VC in guild %d — attempting auto-resume", guild_id)
                    state._resume_info = None
                    state.current = resume["current"]
                    state.queue = resume.get("queue", [])
                    state.loop_mode = resume.get("loop_mode", "off")
                    stream_url = resume.get("stream_url", "")
                    pos = resume.get("position", 0.0)

                    # Re-sync the voice_client reference
                    guild_vc = member.guild.voice_client
                    if guild_vc and guild_vc.is_connected():
                        state.voice_client = guild_vc  # type: ignore[assignment]

                    if stream_url and state.voice_client and state.voice_client.is_connected():
                        self._tune_encoder(state.voice_client)
                        async with state._lock:
                            await self._start_playback(guild_id, stream_url, seek_to=pos)
                        if state.text_channel:
                            try:
                                embed = Embedder.info(
                                    "\U0001f504 Resumed",
                                    f"Auto-resumed **{state.current['name']}** at {_fmt_seconds(pos)}.",
                                )
                                await state.text_channel.send(embed=embed)
                            except Exception:
                                pass
                return

        # ── Human voice state changes (idle detection) ────────────
        if member.bot:
            return

        if not state.voice_client or not state.voice_client.is_connected():
            return

        vc_channel = state.voice_client.channel
        humans = sum(1 for m in vc_channel.members if not m.bot)

        if humans == 0:
            # 24/7 mode — stay connected even when alone
            if state.always_connected:
                return
            # All humans left — start idle disconnect timer
            if state.idle_task and not state.idle_task.done():
                state.idle_task.cancel()
            state.idle_task = asyncio.ensure_future(self._idle_disconnect(guild_id))
        else:
            # A human is present — cancel any pending idle disconnect
            if state.idle_task and not state.idle_task.done():
                state.idle_task.cancel()
                state.idle_task = None


# =====================================================================
#  Setup
# =====================================================================

async def setup(bot: "StarzaiBot") -> None:
    await bot.add_cog(MusicCog(bot))
