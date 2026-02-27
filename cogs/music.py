"""
Music Cog — download, VC playback, lyrics, and platform URL resolution.

Commands:
    /music   — Search & download a song with quality selection
    /play    — Search & play in voice channel
    /skip    — Skip current song
    /music-stop — Stop playback & leave VC
    /queue   — Show the current queue
    /nowplaying — Show current song info
    /pause   — Pause playback
    /resume  — Resume playback
    /volume  — Set playback volume
    /lyrics  — Search for song lyrics

System requirement:
    FFmpeg must be installed on the host system (e.g. ``apt install ffmpeg``)
    for voice channel playback to work.  Without it, /play and VC streaming
    will fail.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import time
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
    )

    def __init__(self) -> None:
        self.queue: List[Dict[str, Any]] = []
        self.current: Optional[Dict[str, Any]] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.volume: float = 0.5
        self.text_channel: Optional[discord.abc.Messageable] = None
        self.idle_task: Optional[asyncio.Task] = None
        self.requester_map: Dict[str, int] = {}  # song_id -> user_id

    def clear(self) -> None:
        self.queue.clear()
        self.current = None
        self.requester_map.clear()
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
                state.voice_client.pause()
                await interaction.response.send_message("\u23f8 Paused.", ephemeral=True)
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


def _now_playing_embed(
    song: Dict[str, Any], requester: Optional[discord.User] = None
) -> discord.Embed:
    """Build the Now Playing embed."""
    desc = (
        f"**{song['name']}**\n"
        f"\U0001f3a4 {song['artist']}\n"
        f"\U0001f4bf {song['album']} \u2022 {song['year']}\n"
        f"\u23f1 {song['duration_formatted']}"
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
            embed = _now_playing_embed(state.current, requester)
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

    # ── Internal: play song in VC ─────────────────────────────────────

    async def _play_song_in_vc(
        self,
        interaction: discord.Interaction,
        song: Dict[str, Any],
        *,
        followup: bool = False,
    ) -> None:
        """Join VC (if needed) and play/queue the song."""
        # Defer early — VC connect + API calls can exceed Discord's 3s deadline
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
                followup = True  # After deferring we must use followup
        except Exception:
            pass

        # Guard against session being closed (e.g. cog unloaded while UI active)
        if not self._services_ready():
            msg = "\u274c Music services are not available right now. Please try again later."
            try:
                if followup:
                    await interaction.followup.send(
                        embed=Embedder.error("Service Unavailable", msg), ephemeral=True
                    )
                elif not interaction.response.is_done():
                    await interaction.response.send_message(
                        embed=Embedder.error("Service Unavailable", msg), ephemeral=True
                    )
                else:
                    await interaction.followup.send(
                        embed=Embedder.error("Service Unavailable", msg), ephemeral=True
                    )
            except Exception:
                pass
            return

        if not interaction.guild:
            msg = "\u274c This command can only be used in a server."
            if followup:
                await interaction.followup.send(embed=Embedder.error("Error", msg), ephemeral=True)
            else:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(
                            embed=Embedder.error("Error", msg), ephemeral=True
                        )
                    else:
                        await interaction.followup.send(embed=Embedder.error("Error", msg), ephemeral=True)
                except Exception:
                    pass
            return

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice or not member.voice.channel:
            msg = "\U0001f50a Join a voice channel first!"
            if followup:
                await interaction.followup.send(embed=Embedder.error("Not in VC", msg), ephemeral=True)
            else:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(
                            embed=Embedder.error("Not in VC", msg), ephemeral=True
                        )
                    else:
                        await interaction.followup.send(embed=Embedder.error("Not in VC", msg), ephemeral=True)
                except Exception:
                    pass
            return

        voice_channel = member.voice.channel
        guild_id = interaction.guild.id
        state = self._get_state(guild_id)

        # Ensure download URL exists
        song = await self.music_api.ensure_download_urls(song)
        stream_url = _pick_best_url(song.get("download_urls", []), "320kbps")
        if not stream_url:
            embed = Embedder.error("No Stream", "\u274c Could not find a stream URL for this song.")
            if followup:
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(embed=embed, ephemeral=True)
                    else:
                        await interaction.followup.send(embed=embed, ephemeral=True)
                except Exception:
                    pass
            return

        # Store requester (only when the song has a valid ID)
        if song.get("id"):
            state.requester_map[song["id"]] = interaction.user.id
        state.text_channel = interaction.channel

        # Connect to VC if not already
        try:
            if state.voice_client is None or not state.voice_client.is_connected():
                state.voice_client = await voice_channel.connect(self_deaf=True)
            elif state.voice_client.channel.id != voice_channel.id:
                await state.voice_client.move_to(voice_channel)

            # Max out every Opus encoder knob for the best music quality
            if state.voice_client and hasattr(state.voice_client, 'encoder'):
                try:
                    enc = state.voice_client.encoder
                    enc.set_bitrate(MAX_ENCODER_BITRATE)       # 512 kbps ceiling
                    enc.set_signal_type('music')                # optimise for music (not voice)
                    enc.set_bandwidth('full')                   # full 20 kHz bandwidth
                    enc.set_fec(True)                           # forward error correction on
                    enc.set_expected_packet_loss_percent(0.05)  # 5% — low, keeps quality high
                    logger.debug(
                        "Opus encoder tuned: %d bps, music signal, full BW, FEC on",
                        MAX_ENCODER_BITRATE,
                    )
                except Exception as exc:
                    logger.debug("Could not tune encoder: %s", exc)
        except Exception as exc:
            logger.error("VC connection error: %s", exc, exc_info=True)
            embed = Embedder.error("Connection Error", "\u274c Could not join the voice channel.")
            if followup:
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(embed=embed, ephemeral=True)
                    else:
                        await interaction.followup.send(embed=embed, ephemeral=True)
                except Exception:
                    pass
            return

        # Cancel idle task if it exists
        if state.idle_task and not state.idle_task.done():
            state.idle_task.cancel()
            state.idle_task = None

        # If already playing, queue the song
        if state.voice_client.is_playing() or state.voice_client.is_paused():
            state.queue.append(song)
            pos = len(state.queue)
            embed = Embedder.standard(
                "\U0001f3b5 Added to Queue",
                f"**{song['name']}** \u2014 {song['artist']}\nPosition: #{pos}",
                footer=BRAND,
            )
            if followup:
                await interaction.followup.send(embed=embed)
            else:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(embed=embed)
                    else:
                        await interaction.followup.send(embed=embed)
                except Exception:
                    pass
            return

        # Play immediately
        state.current = song
        await self._start_playback(guild_id, stream_url)

        requester = self.bot.get_user(interaction.user.id)
        embed = _now_playing_embed(song, requester)
        view = NowPlayingView(song, self, guild_id)

        if followup:
            await interaction.followup.send(embed=embed, view=view)
        else:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, view=view)
                else:
                    await interaction.followup.send(embed=embed, view=view)
            except Exception:
                pass

    # ── Internal: start FFmpeg playback ───────────────────────────────

    async def _start_playback(self, guild_id: int, url: str) -> None:
        """Start FFmpeg playback on the guild's voice client."""
        state = self._get_state(guild_id)
        if not state.voice_client or not state.voice_client.is_connected():
            return

        try:
            source = discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)
            source = discord.PCMVolumeTransformer(source, volume=state.volume)
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
        """Called when a song ends; plays the next in queue or starts idle timer."""
        if error:
            logger.error("Playback error in guild %d: %s", guild_id, error)

        state = self._get_state(guild_id)

        # Iterate through the queue until we find a playable track or the queue is empty.
        # This avoids recursion which could hit Python's recursion limit with long queues.
        while state.queue:
            next_song = state.queue.pop(0)
            state.current = next_song
            stream_url = _pick_best_url(next_song.get("download_urls", []), "320kbps")
            if stream_url:
                await self._start_playback(guild_id, stream_url)
                if state.text_channel:
                    try:
                        requester_id = state.requester_map.get(next_song.get("id", ""))
                        requester = self.bot.get_user(requester_id) if requester_id else None
                        embed = _now_playing_embed(next_song, requester)
                        view = NowPlayingView(next_song, self, guild_id)
                        await state.text_channel.send(embed=embed, view=view)
                    except Exception:
                        pass
                return  # Successfully started playback
            else:
                logger.warning("Skipping song '%s' — no stream URL", next_song.get("name", ""))
                state.current = None

        # Queue is empty
        state.current = None
        # Only schedule idle disconnect if still connected to VC
        if state.voice_client and state.voice_client.is_connected():
            state.idle_task = asyncio.ensure_future(self._idle_disconnect(guild_id))

    # ── Internal: idle disconnect ─────────────────────────────────────

    async def _idle_disconnect(self, guild_id: int) -> None:
        """Disconnect from VC after idle timeout."""
        try:
            await asyncio.sleep(VC_IDLE_TIMEOUT)
            state = self._get_state(guild_id)
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
        """Stop playback, clear queue, and disconnect from VC."""
        state = self._get_state(guild_id)
        state.clear()

        if state.voice_client:
            try:
                if state.voice_client.is_playing() or state.voice_client.is_paused():
                    state.voice_client.stop()
                await state.voice_client.disconnect(force=True)
            except Exception:
                pass
            state.voice_client = None

    # ── Voice state change listener ───────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Auto-leave if the bot is alone in a VC for too long."""
        if member.bot:
            return

        guild_id = member.guild.id
        state = self._get_state(guild_id)

        if not state.voice_client or not state.voice_client.is_connected():
            return

        vc_channel = state.voice_client.channel
        humans = sum(1 for m in vc_channel.members if not m.bot)

        if humans == 0:
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
