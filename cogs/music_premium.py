"""
Music Premium Cog — Premium-tier features that competitors charge $4–10/month for, free.

Commands:
    /favorite             — Toggle favorite on the currently playing song
    /favorites            — View your favorites list with interactive UI
    /playlist create      — Create a new saved playlist
    /playlist delete      — Delete a saved playlist
    /playlist view        — View songs in a playlist
    /playlist add         — Add the current song (or a search) to a playlist
    /playlist play        — Load a playlist into the queue
    /playlist list        — List all your playlists
    /playlist rename      — Rename a playlist
    /playlist save-queue  — Save the current queue as a playlist
    /musicprofile         — View your (or another user's) music profile
    /requestchannel       — Set up a song request channel
    /sleeptimer           — Set a sleep timer to auto-disconnect
    /import               — Import a Spotify/Apple Music/YouTube Music/Tidal playlist/mix

Features:
    • Full interactive UI with buttons for playlists & favorites
    • Per-user music profiles with listening stats, top artists, top songs
    • Song request channel — just type a song name and it auto-queues
    • Sleep timer with live countdown
    • Spotify, Apple Music, YouTube Music & Tidal playlist/album import
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from html import unescape
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config.constants import (
    BOT_COLOR,
    BRAND,
    MAX_FAVORITES,
    MAX_PLAYLISTS_PER_USER,
    MAX_SONGS_PER_PLAYLIST,
    PREMIUM_VIEW_TIMEOUT,
)
from utils.embedder import Embedder
from utils.platform_resolver import is_music_url, resolve_url
from utils.song_helpers import song_key as _song_key

if TYPE_CHECKING:
    from bot import StarzaiBot

logger = logging.getLogger(__name__)

# ── Local aliases ──────────────────────────────────────────────────
MAX_EMBED_DESC = 4096
VIEW_TIMEOUT = PREMIUM_VIEW_TIMEOUT


def _fmt_duration(seconds: float) -> str:
    """Format seconds into a human-friendly string."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _truncate(text: str, length: int = 100) -> str:
    if len(text) <= length:
        return text
    return text[: length - 1] + "\u2026"


# =====================================================================
#  Interactive Views
# =====================================================================

class FavoritesView(discord.ui.View):
    """Paginated view for user favorites with play/remove buttons."""

    SONGS_PER_PAGE = 8

    def __init__(
        self,
        favorites: List[Dict[str, Any]],
        user: discord.User,
        cog: "MusicPremiumCog",
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.favorites = favorites
        self.user = user
        self.cog = cog
        self.page = 0

    @property
    def total_pages(self) -> int:
        total = len(self.favorites)
        if total == 0:
            return 1
        return (total + self.SONGS_PER_PAGE - 1) // self.SONGS_PER_PAGE

    def _build_embed(self) -> discord.Embed:
        if not self.favorites:
            return Embedder.standard(
                "\u2764\ufe0f Your Favorites",
                "You haven't favorited any songs yet!\n\n"
                "Use the \u2764 button on the Now Playing embed, or `/favorite` to add songs.",
                footer=BRAND,
            )

        start = self.page * self.SONGS_PER_PAGE
        end = start + self.SONGS_PER_PAGE
        page_songs = self.favorites[start:end]

        lines: List[str] = []
        for i, song in enumerate(page_songs, start + 1):
            dur = song.get("duration_formatted", "?:??")
            lines.append(f"**{i}.** {song['name']} \u2014 {song['artist']}  `{dur}`")

        total = len(self.favorites)
        return Embedder.standard(
            "\u2764\ufe0f Your Favorites",
            "\n".join(lines)[:MAX_EMBED_DESC],
            footer=f"{total} song{'s' if total != 1 else ''} \u2022 "
                   f"Page {self.page + 1}/{self.total_pages} \u2022 {BRAND}",
        )

    @discord.ui.button(label="\u25c0", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("\u274c Not your menu.", ephemeral=True)
            return
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="\u25b6", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("\u274c Not your menu.", ephemeral=True)
            return
        if self.page < self.total_pages - 1:
            self.page += 1
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="\u25b6 Play All", style=discord.ButtonStyle.success, emoji="\U0001f3b5")
    async def play_all_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("\u274c Not your menu.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog._queue_songs(interaction, self.favorites, "Favorites")

    @discord.ui.button(label="\U0001f500 Shuffle Play", style=discord.ButtonStyle.primary)
    async def shuffle_play_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("\u274c Not your menu.", ephemeral=True)
            return
        await interaction.response.defer()
        import random
        shuffled = list(self.favorites)
        random.shuffle(shuffled)
        await self.cog._queue_songs(interaction, shuffled, "Favorites (Shuffled)")

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Select, discord.ui.Button)):
                item.disabled = True  # type: ignore[union-attr]


class PlaylistSongsView(discord.ui.View):
    """Paginated view for songs in a playlist with play button."""

    SONGS_PER_PAGE = 8

    def __init__(
        self,
        songs: List[Dict[str, Any]],
        playlist_name: str,
        playlist_id: int,
        user: discord.User,
        cog: "MusicPremiumCog",
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.songs = songs
        self.playlist_name = playlist_name
        self.playlist_id = playlist_id
        self.user = user
        self.cog = cog
        self.page = 0

    @property
    def total_pages(self) -> int:
        total = len(self.songs)
        if total == 0:
            return 1
        return (total + self.SONGS_PER_PAGE - 1) // self.SONGS_PER_PAGE

    def _build_embed(self) -> discord.Embed:
        if not self.songs:
            return Embedder.standard(
                f"\U0001f4cb {self.playlist_name}",
                "This playlist is empty.\n\nUse `/playlist add` to add songs!",
                footer=BRAND,
            )

        start = self.page * self.SONGS_PER_PAGE
        end = start + self.SONGS_PER_PAGE
        page_songs = self.songs[start:end]

        lines: List[str] = []
        for i, song in enumerate(page_songs, start + 1):
            dur = song.get("duration_formatted", "?:??")
            lines.append(f"**{i}.** {song['name']} \u2014 {song['artist']}  `{dur}`")

        total = len(self.songs)
        total_dur = sum(s.get("duration", 0) for s in self.songs)
        dur_str = _fmt_duration(total_dur)

        return Embedder.standard(
            f"\U0001f4cb {self.playlist_name}",
            "\n".join(lines)[:MAX_EMBED_DESC],
            footer=f"{total} song{'s' if total != 1 else ''} \u2022 {dur_str} \u2022 "
                   f"Page {self.page + 1}/{self.total_pages} \u2022 {BRAND}",
        )

    @discord.ui.button(label="\u25c0", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("\u274c Not your menu.", ephemeral=True)
            return
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="\u25b6", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("\u274c Not your menu.", ephemeral=True)
            return
        if self.page < self.total_pages - 1:
            self.page += 1
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="\u25b6 Play All", style=discord.ButtonStyle.success, emoji="\U0001f3b5")
    async def play_all_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("\u274c Not your menu.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog._queue_songs(interaction, self.songs, self.playlist_name)

    @discord.ui.button(label="\U0001f500 Shuffle Play", style=discord.ButtonStyle.primary)
    async def shuffle_play_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("\u274c Not your menu.", ephemeral=True)
            return
        await interaction.response.defer()
        import random
        shuffled = list(self.songs)
        random.shuffle(shuffled)
        await self.cog._queue_songs(interaction, shuffled, f"{self.playlist_name} (Shuffled)")

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Select, discord.ui.Button)):
                item.disabled = True  # type: ignore[union-attr]


class PlaylistListView(discord.ui.View):
    """Dropdown to select a playlist from the user's list."""

    def __init__(
        self,
        playlists: List[Dict[str, Any]],
        user: discord.User,
        cog: "MusicPremiumCog",
        *,
        action: str = "view",  # "view" or "play" or "delete"
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.playlists = playlists
        self.user = user
        self.cog = cog
        self.action = action
        self._build_select()

    def _build_select(self) -> None:
        options: List[discord.SelectOption] = []
        for pl in self.playlists[:25]:
            label = _truncate(pl["name"], 100)
            desc = f"{pl['song_count']} song{'s' if pl['song_count'] != 1 else ''}"
            options.append(
                discord.SelectOption(label=label, description=desc, value=str(pl["id"]))
            )

        select = discord.ui.Select(
            placeholder="Choose a playlist\u2026",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("\u274c Not your menu.", ephemeral=True)
            return

        playlist_id = int(interaction.data["values"][0])  # type: ignore[index]
        playlist = next((p for p in self.playlists if p["id"] == playlist_id), None)
        if not playlist:
            await interaction.response.send_message("\u274c Playlist not found.", ephemeral=True)
            return

        db = self.cog.bot.database
        songs = await db.get_playlist_songs(playlist_id)

        if self.action == "delete":
            await db.delete_playlist(str(self.user.id), playlist_id)
            await interaction.response.edit_message(
                embed=Embedder.success(
                    "Playlist Deleted",
                    f"\U0001f5d1 Deleted **{playlist['name']}** ({playlist['song_count']} songs).",
                ),
                view=None,
            )
        elif self.action == "play":
            await interaction.response.defer()
            await self.cog._queue_songs(interaction, songs, playlist["name"])
        else:
            # View
            view = PlaylistSongsView(songs, playlist["name"], playlist_id, self.user, self.cog)
            await interaction.response.edit_message(embed=view._build_embed(), view=view)

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Select, discord.ui.Button)):
                item.disabled = True  # type: ignore[union-attr]


class PlaylistSelectForAdd(discord.ui.View):
    """Dropdown to select which playlist to add a song to."""

    def __init__(
        self,
        playlists: List[Dict[str, Any]],
        song: Dict[str, Any],
        user: discord.User,
        cog: "MusicPremiumCog",
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.playlists = playlists
        self.song = song
        self.user = user
        self.cog = cog
        self._build_select()

    def _build_select(self) -> None:
        options: List[discord.SelectOption] = []
        for pl in self.playlists[:25]:
            label = _truncate(pl["name"], 100)
            desc = f"{pl['song_count']} songs"
            options.append(
                discord.SelectOption(label=label, description=desc, value=str(pl["id"]))
            )
        select = discord.ui.Select(
            placeholder="Add to which playlist?",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("\u274c Not your menu.", ephemeral=True)
            return

        playlist_id = int(interaction.data["values"][0])  # type: ignore[index]
        playlist = next((p for p in self.playlists if p["id"] == playlist_id), None)
        if not playlist:
            await interaction.response.send_message("\u274c Playlist not found.", ephemeral=True)
            return

        db = self.cog.bot.database
        key = _song_key(self.song)
        ok = await db.add_song_to_playlist(playlist_id, key)
        if ok:
            await interaction.response.edit_message(
                embed=Embedder.success(
                    "Song Added",
                    f"\u2795 Added **{self.song['name']}** to **{playlist['name']}**",
                ),
                view=None,
            )
        else:
            await interaction.response.edit_message(
                embed=Embedder.error("Failed", "\u274c Could not add the song."),
                view=None,
            )

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Select, discord.ui.Button)):
                item.disabled = True  # type: ignore[union-attr]


# =====================================================================
#  The Cog
# =====================================================================

class MusicPremiumCog(commands.Cog, name="MusicPremium"):
    """Premium music features — playlists, favorites, profiles, and more.

    All the features competitors charge $4-10/month for, completely free.
    """

    def __init__(self, bot: "StarzaiBot") -> None:
        self.bot = bot
        self._sleep_timers: Dict[int, asyncio.Task] = {}  # guild_id -> timer task
        self._request_channel_cache: Dict[int, int] = {}   # guild_id -> channel_id

    # ── Helpers ──────────────────────────────────────────────────────

    def _get_music_cog(self):
        """Get the main MusicCog instance."""
        return self.bot.get_cog("Music")

    def _get_music_state(self, guild_id: int):
        """Get the GuildMusicState from the main music cog."""
        music_cog = self._get_music_cog()
        if music_cog:
            return music_cog._get_state(guild_id)
        return None

    async def _queue_songs(
        self,
        interaction: discord.Interaction,
        songs: List[Dict[str, Any]],
        source_name: str,
    ) -> None:
        """Queue multiple songs into the music player from stored song dicts.

        Resolves each stored song through the music API before queuing.
        """
        music_cog = self._get_music_cog()
        if not music_cog or not music_cog._services_ready():
            await interaction.followup.send(
                embed=Embedder.error("Unavailable", "\u274c Music services are not available."),
                ephemeral=True,
            )
            return

        if not interaction.guild:
            await interaction.followup.send(
                embed=Embedder.error("Server Only", "This command can only be used in a server."),
                ephemeral=True,
            )
            return

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice or not member.voice.channel:
            await interaction.followup.send(
                embed=Embedder.error("Not in VC", "\U0001f50a Join a voice channel first!"),
                ephemeral=True,
            )
            return

        if not songs:
            await interaction.followup.send(
                embed=Embedder.warning("Empty", f"**{source_name}** has no songs to play."),
                ephemeral=True,
            )
            return

        state = self._get_music_state(interaction.guild.id)
        if not state:
            await interaction.followup.send(
                embed=Embedder.error("Error", "\u274c Music system unavailable."),
                ephemeral=True,
            )
            return

        # Search and resolve the first song immediately, queue the rest in background
        status_embed = Embedder.standard(
            f"\U0001f4cb Loading {source_name}",
            f"\u23f3 Resolving **{len(songs)}** songs...\n"
            f"First song will start playing shortly.",
            footer=BRAND,
        )
        msg = await interaction.followup.send(embed=status_embed)

        resolved_count = 0
        failed_count = 0
        first_played = False

        for song in songs:
            # Build search query from stored song data
            query = f"{song.get('artist', '')} {song.get('name', '')}".strip()
            if not query:
                failed_count += 1
                continue

            try:
                results = await music_cog.music_api.search(query, limit=5)
                if not results:
                    failed_count += 1
                    continue

                from utils.music_api import _pick_best_url, pick_best_match
                resolved = await music_cog.music_api.ensure_download_urls(pick_best_match(results, query))
                stream_url = _pick_best_url(resolved.get("download_urls", []), "320kbps")
                if not stream_url:
                    failed_count += 1
                    continue

                async with state._lock:
                    if resolved.get("id"):
                        state.requester_map[resolved["id"]] = interaction.user.id
                    state.text_channel = interaction.channel

                    if not first_played:
                        # First song — connect and play, or just queue if
                        # something is already playing.

                        # Cancel idle if exists
                        if state.idle_task and not state.idle_task.done():
                            state.idle_task.cancel()
                            state.idle_task = None

                        # Fast path: already connected and playing — just
                        # queue without touching the encoder (avoids
                        # audible glitches mid-stream).
                        already_playing = (
                            state.voice_client
                            and state.voice_client.is_connected()
                            and (state.voice_client.is_playing() or state.voice_client.is_paused())
                        )

                        if already_playing:
                            state.queue.append(resolved)
                        else:
                            # Need to connect / ensure voice
                            voice_channel = member.voice.channel
                            try:
                                vc = await music_cog._ensure_voice(interaction.guild, voice_channel, state)
                                music_cog._tune_encoder(vc)
                            except Exception:
                                await interaction.followup.send(
                                    embed=Embedder.error("Connection Error", "\u274c Could not join the voice channel."),
                                    ephemeral=True,
                                )
                                return

                            state.current = resolved
                            await music_cog._start_playback(interaction.guild.id, stream_url)
                        first_played = True
                    else:
                        # Subsequent songs — just add to queue
                        state.queue.append(resolved)

                resolved_count += 1

            except Exception as exc:
                logger.warning("Failed to resolve song '%s': %s", query, exc)
                failed_count += 1
                continue

        # Final status update
        lines = [f"\u2705 Loaded **{resolved_count}** song{'s' if resolved_count != 1 else ''}"]
        if failed_count > 0:
            lines.append(f"\u26a0\ufe0f {failed_count} song{'s' if failed_count != 1 else ''} could not be resolved")

        # If first song started, show NP
        if first_played and state.current:
            requester = self.bot.get_user(interaction.user.id)
            from cogs.music import _now_playing_embed, NowPlayingView
            embed = _now_playing_embed(state, state.current, requester)
            embed.set_footer(text=f"Loaded from {source_name} \u2022 {BRAND}")
            view = NowPlayingView(state.current, music_cog, interaction.guild.id)
            try:
                await msg.edit(embed=embed, view=view)
                state._np_message = msg
                music_cog._start_progress_updater(interaction.guild.id)
            except Exception:
                pass
        else:
            final_embed = Embedder.success(
                f"Loaded {source_name}",
                "\n".join(lines),
            )
            try:
                await msg.edit(embed=final_embed)
            except Exception:
                pass

    # ── Cog-wide authorization gate (mirrors MusicCog) ───────────────

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.bot.settings.owner_ids:
            return True
        if self.bot.is_guild_allowed(interaction.guild_id):
            return True

        embed = Embedder.error(
            "Bot Not Authorised",
            "This bot hasn't been enabled for this server yet.\n"
            "Ask a **bot owner** to run `/allow` here.",
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.NotFound:
            pass
        return False

    # ══════════════════════════════════════════════════════════════════
    #  /favorite — Quick toggle
    # ══════════════════════════════════════════════════════════════════

    @app_commands.command(name="favorite", description="Toggle favorite on the currently playing song")
    async def favorite_cmd(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        state = self._get_music_state(interaction.guild_id)
        if not state or not state.current:
            await interaction.response.send_message(
                embed=Embedder.warning("Nothing Playing", "No song is currently playing."),
                ephemeral=True,
            )
            return

        db = self.bot.database
        uid = str(interaction.user.id)
        key = _song_key(state.current)
        is_fav = await db.is_favorite(uid, key)

        if is_fav:
            await db.remove_favorite(uid, key)
            await interaction.response.send_message(
                embed=Embedder.info(
                    "Removed from Favorites",
                    f"\U0001f494 Removed **{state.current['name']}** from your favorites.",
                ),
                ephemeral=True,
            )
        else:
            await db.add_favorite(uid, key)
            await interaction.response.send_message(
                embed=Embedder.success(
                    "Added to Favorites",
                    f"\u2764\ufe0f Added **{state.current['name']}** \u2014 {state.current['artist']} to your favorites!",
                ),
                ephemeral=True,
            )

    # ══════════════════════════════════════════════════════════════════
    #  /favorites — View favorites with interactive UI
    # ══════════════════════════════════════════════════════════════════

    @app_commands.command(name="favorites", description="View your favorite songs")
    async def favorites_cmd(self, interaction: discord.Interaction) -> None:
        db = self.bot.database
        uid = str(interaction.user.id)
        favorites = await db.get_favorites(uid, limit=MAX_FAVORITES)
        view = FavoritesView(favorites, interaction.user, self)
        await interaction.response.send_message(embed=view._build_embed(), view=view)

    # ══════════════════════════════════════════════════════════════════
    #  /playlist — Playlist management group
    # ══════════════════════════════════════════════════════════════════

    playlist_group = app_commands.Group(
        name="playlist",
        description="Manage your saved playlists",
    )

    @playlist_group.command(name="create", description="Create a new playlist")
    @app_commands.describe(name="Playlist name", description="Optional description")
    async def playlist_create_cmd(
        self, interaction: discord.Interaction, name: str, description: str = ""
    ) -> None:
        db = self.bot.database
        uid = str(interaction.user.id)

        # Check limit
        playlists = await db.get_playlists(uid)
        if len(playlists) >= MAX_PLAYLISTS_PER_USER:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Limit Reached",
                    f"\u274c You can have at most **{MAX_PLAYLISTS_PER_USER}** playlists.\n"
                    "Delete one first with `/playlist delete`.",
                ),
                ephemeral=True,
            )
            return

        pl_id = await db.create_playlist(uid, name, description)
        if pl_id is None:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Already Exists",
                    f"\u274c You already have a playlist named **{name}**.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=Embedder.success(
                "Playlist Created",
                f"\U0001f4cb Created **{name}**!\n"
                f"Add songs with `/playlist add` or `/playlist save-queue`.",
            )
        )

    @playlist_group.command(name="list", description="List all your playlists")
    async def playlist_list_cmd(self, interaction: discord.Interaction) -> None:
        db = self.bot.database
        uid = str(interaction.user.id)
        playlists = await db.get_playlists(uid)

        if not playlists:
            await interaction.response.send_message(
                embed=Embedder.standard(
                    "\U0001f4cb Your Playlists",
                    "You don't have any playlists yet!\n\n"
                    "Create one with `/playlist create`.",
                    footer=BRAND,
                )
            )
            return

        lines: List[str] = []
        for i, pl in enumerate(playlists, 1):
            lines.append(
                f"**{i}.** {pl['name']} \u2014 `{pl['song_count']} songs`"
            )

        await interaction.response.send_message(
            embed=Embedder.standard(
                "\U0001f4cb Your Playlists",
                "\n".join(lines)[:MAX_EMBED_DESC],
                footer=f"{len(playlists)} playlist{'s' if len(playlists) != 1 else ''} \u2022 {BRAND}",
            )
        )

    @playlist_group.command(name="view", description="View songs in a playlist")
    @app_commands.describe(name="Playlist name")
    async def playlist_view_cmd(self, interaction: discord.Interaction, name: str) -> None:
        db = self.bot.database
        uid = str(interaction.user.id)
        playlist = await db.get_playlist_by_name(uid, name)

        if not playlist:
            await interaction.response.send_message(
                embed=Embedder.error("Not Found", f"\u274c No playlist named **{name}**."),
                ephemeral=True,
            )
            return

        songs = await db.get_playlist_songs(playlist["id"])
        view = PlaylistSongsView(songs, playlist["name"], playlist["id"], interaction.user, self)
        await interaction.response.send_message(embed=view._build_embed(), view=view)

    @playlist_group.command(name="add", description="Add the current song or a search to a playlist")
    @app_commands.describe(name="Playlist name", query="Song to search for (leave blank for current song)")
    async def playlist_add_cmd(
        self, interaction: discord.Interaction, name: str, query: Optional[str] = None
    ) -> None:
        db = self.bot.database
        uid = str(interaction.user.id)
        playlist = await db.get_playlist_by_name(uid, name)

        if not playlist:
            await interaction.response.send_message(
                embed=Embedder.error("Not Found", f"\u274c No playlist named **{name}**."),
                ephemeral=True,
            )
            return

        # Check song count limit
        songs = await db.get_playlist_songs(playlist["id"])
        if len(songs) >= MAX_SONGS_PER_PLAYLIST:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Playlist Full",
                    f"\u274c This playlist has the maximum **{MAX_SONGS_PER_PLAYLIST}** songs.",
                ),
                ephemeral=True,
            )
            return

        if query:
            # Search for the song
            music_cog = self._get_music_cog()
            if not music_cog or not music_cog._services_ready():
                await interaction.response.send_message(
                    embed=Embedder.error("Unavailable", "\u274c Music services unavailable."),
                    ephemeral=True,
                )
                return

            await interaction.response.defer()
            search_query = await music_cog._resolve_query(query)
            results = await music_cog.music_api.search(search_query, limit=5)
            if not results:
                await interaction.followup.send(
                    embed=Embedder.error("No Results", f"\u274c No results for **{query}**."),
                    ephemeral=True,
                )
                return

            from utils.music_api import pick_best_match
            song = pick_best_match(results, search_query)
            key = _song_key(song)
            ok = await db.add_song_to_playlist(playlist["id"], key)
            if ok:
                await interaction.followup.send(
                    embed=Embedder.success(
                        "Added to Playlist",
                        f"\u2795 Added **{song['name']}** \u2014 {song['artist']} to **{name}**",
                    )
                )
            else:
                await interaction.followup.send(
                    embed=Embedder.error("Failed", "\u274c Could not add the song."),
                    ephemeral=True,
                )
        else:
            # Use currently playing song
            if not interaction.guild_id:
                await interaction.response.send_message("Server only.", ephemeral=True)
                return

            state = self._get_music_state(interaction.guild_id)
            if not state or not state.current:
                await interaction.response.send_message(
                    embed=Embedder.warning(
                        "Nothing Playing",
                        "No song is playing. Provide a `query` to search, or play a song first.",
                    ),
                    ephemeral=True,
                )
                return

            key = _song_key(state.current)
            ok = await db.add_song_to_playlist(playlist["id"], key)
            if ok:
                await interaction.response.send_message(
                    embed=Embedder.success(
                        "Added to Playlist",
                        f"\u2795 Added **{state.current['name']}** \u2014 {state.current['artist']} to **{name}**",
                    )
                )
            else:
                await interaction.response.send_message(
                    embed=Embedder.error("Failed", "\u274c Could not add the song."),
                    ephemeral=True,
                )

    @playlist_group.command(name="remove", description="Remove a song from a playlist by position")
    @app_commands.describe(name="Playlist name", position="Song position to remove (1-based)")
    async def playlist_remove_cmd(
        self, interaction: discord.Interaction, name: str, position: int
    ) -> None:
        db = self.bot.database
        uid = str(interaction.user.id)
        playlist = await db.get_playlist_by_name(uid, name)

        if not playlist:
            await interaction.response.send_message(
                embed=Embedder.error("Not Found", f"\u274c No playlist named **{name}**."),
                ephemeral=True,
            )
            return

        # position is 1-based, DB is 0-based
        ok = await db.remove_song_from_playlist(playlist["id"], position - 1)
        if ok:
            await interaction.response.send_message(
                embed=Embedder.success(
                    "Removed",
                    f"\U0001f5d1 Removed song #{position} from **{name}**.",
                )
            )
        else:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Invalid Position",
                    f"\u274c No song at position #{position} in **{name}**.",
                ),
                ephemeral=True,
            )

    @playlist_group.command(name="play", description="Load a playlist into the queue and start playing")
    @app_commands.describe(name="Playlist name")
    async def playlist_play_cmd(self, interaction: discord.Interaction, name: str) -> None:
        db = self.bot.database
        uid = str(interaction.user.id)
        playlist = await db.get_playlist_by_name(uid, name)

        if not playlist:
            await interaction.response.send_message(
                embed=Embedder.error("Not Found", f"\u274c No playlist named **{name}**."),
                ephemeral=True,
            )
            return

        songs = await db.get_playlist_songs(playlist["id"])
        if not songs:
            await interaction.response.send_message(
                embed=Embedder.warning("Empty Playlist", f"**{name}** has no songs."),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        await self._queue_songs(interaction, songs, name)

    @playlist_group.command(name="delete", description="Delete a playlist")
    @app_commands.describe(name="Playlist name")
    async def playlist_delete_cmd(self, interaction: discord.Interaction, name: str) -> None:
        db = self.bot.database
        uid = str(interaction.user.id)
        playlist = await db.get_playlist_by_name(uid, name)

        if not playlist:
            await interaction.response.send_message(
                embed=Embedder.error("Not Found", f"\u274c No playlist named **{name}**."),
                ephemeral=True,
            )
            return

        ok = await db.delete_playlist(uid, playlist["id"])
        if ok:
            await interaction.response.send_message(
                embed=Embedder.success(
                    "Playlist Deleted",
                    f"\U0001f5d1 Deleted **{name}** and all its songs.",
                )
            )
        else:
            await interaction.response.send_message(
                embed=Embedder.error("Failed", "\u274c Could not delete the playlist."),
                ephemeral=True,
            )

    @playlist_group.command(name="rename", description="Rename a playlist")
    @app_commands.describe(name="Current playlist name", new_name="New name")
    async def playlist_rename_cmd(
        self, interaction: discord.Interaction, name: str, new_name: str
    ) -> None:
        db = self.bot.database
        uid = str(interaction.user.id)
        playlist = await db.get_playlist_by_name(uid, name)

        if not playlist:
            await interaction.response.send_message(
                embed=Embedder.error("Not Found", f"\u274c No playlist named **{name}**."),
                ephemeral=True,
            )
            return

        ok = await db.rename_playlist(uid, playlist["id"], new_name)
        if ok:
            await interaction.response.send_message(
                embed=Embedder.success(
                    "Renamed",
                    f"\u270f\ufe0f Renamed **{name}** \u2192 **{new_name}**",
                )
            )
        else:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Failed",
                    f"\u274c Could not rename. A playlist named **{new_name}** may already exist.",
                ),
                ephemeral=True,
            )

    @playlist_group.command(name="save-queue", description="Save the current queue as a playlist")
    @app_commands.describe(name="Name for the new playlist")
    async def playlist_save_queue_cmd(self, interaction: discord.Interaction, name: str) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        state = self._get_music_state(interaction.guild_id)
        if not state:
            await interaction.response.send_message(
                embed=Embedder.error("Error", "\u274c Music system unavailable."),
                ephemeral=True,
            )
            return

        all_songs: List[Dict[str, Any]] = []
        if state.current:
            all_songs.append(state.current)
        all_songs.extend(state.queue)

        if not all_songs:
            await interaction.response.send_message(
                embed=Embedder.warning("Empty Queue", "There are no songs to save."),
                ephemeral=True,
            )
            return

        db = self.bot.database
        uid = str(interaction.user.id)

        pl_id = await db.create_playlist(uid, name)
        if pl_id is None:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Already Exists",
                    f"\u274c You already have a playlist named **{name}**.",
                ),
                ephemeral=True,
            )
            return

        count = 0
        for song in all_songs[:MAX_SONGS_PER_PLAYLIST]:
            key = _song_key(song)
            if await db.add_song_to_playlist(pl_id, key):
                count += 1

        await interaction.response.send_message(
            embed=Embedder.success(
                "Queue Saved",
                f"\U0001f4be Saved **{count}** song{'s' if count != 1 else ''} to **{name}**!",
            )
        )

    # ══════════════════════════════════════════════════════════════════
    #  /musicprofile — User music profile
    # ══════════════════════════════════════════════════════════════════

    @app_commands.command(name="musicprofile", description="View your or another user's music profile")
    @app_commands.describe(user="User to view (leave blank for yourself)")
    async def musicprofile_cmd(
        self, interaction: discord.Interaction, user: Optional[discord.User] = None
    ) -> None:
        target = user or interaction.user
        db = self.bot.database
        profile = await db.get_music_profile(str(target.id))

        total_time = profile.get("total_listening_seconds", 0)
        total_songs = profile.get("total_songs_played", 0)
        top_artists = profile.get("top_artists", [])
        top_songs = profile.get("top_songs", [])
        recent = profile.get("recent_songs", [])

        # Also get favorites count
        fav_count = await db.get_favorites_count(str(target.id))
        playlists = await db.get_playlists(str(target.id))
        playlist_count = len(playlists)

        if total_songs == 0 and fav_count == 0:
            await interaction.response.send_message(
                embed=Embedder.standard(
                    f"\U0001f3b5 {target.display_name}'s Music Profile",
                    f"No listening data yet for **{target.display_name}**.\n\n"
                    "Start listening to build your profile!",
                    footer=BRAND,
                    thumbnail=target.display_avatar.url if target.display_avatar else None,
                )
            )
            return

        # Build the profile embed
        lines: List[str] = []

        # ── Stats card ──
        lines.append("**\u2500\u2500\u2500 Stats \u2500\u2500\u2500**")
        lines.append(f"\U0001f3b5 **Songs Played:** {total_songs:,}")
        lines.append(f"\u23f1 **Listening Time:** {_fmt_duration(total_time)}")
        lines.append(f"\u2764\ufe0f **Favorites:** {fav_count}")
        lines.append(f"\U0001f4cb **Playlists:** {playlist_count}")
        lines.append("")

        # ── Top Artists ──
        if top_artists:
            lines.append("**\u2500\u2500\u2500 Top Artists \u2500\u2500\u2500**")
            for i, a in enumerate(top_artists[:5], 1):
                medal = ["\U0001f947", "\U0001f948", "\U0001f949", "4\ufe0f\u20e3", "5\ufe0f\u20e3"][i - 1]
                lines.append(f"{medal} {a['name']} \u2014 {a['plays']} plays")
            lines.append("")

        # ── Top Songs ──
        if top_songs:
            lines.append("**\u2500\u2500\u2500 Top Songs \u2500\u2500\u2500**")
            for i, s in enumerate(top_songs[:5], 1):
                plays = s.get("_plays", 0)
                lines.append(f"**{i}.** {s['name']} \u2014 {s['artist']} ({plays}x)")
            lines.append("")

        # ── Recent ──
        if recent:
            lines.append("**\u2500\u2500\u2500 Recently Played \u2500\u2500\u2500**")
            for s in recent[:5]:
                lines.append(f"\u25b8 {s['name']} \u2014 {s['artist']}")

        embed = Embedder.standard(
            f"\U0001f3b5 {target.display_name}'s Music Profile",
            "\n".join(lines)[:MAX_EMBED_DESC],
            footer=BRAND,
            thumbnail=target.display_avatar.url if target.display_avatar else None,
        )
        await interaction.response.send_message(embed=embed)

    # ══════════════════════════════════════════════════════════════════
    #  /requestchannel — Song request channel setup
    # ══════════════════════════════════════════════════════════════════

    @app_commands.command(
        name="requestchannel",
        description="Set up or remove a song request channel (type a song name to auto-queue)",
    )
    @app_commands.describe(
        channel="Channel to use for song requests (leave blank to remove)",
    )
    async def requestchannel_cmd(
        self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None
    ) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        # Only admins or bot owners
        is_admin = (
            interaction.user.guild_permissions.manage_guild
            if hasattr(interaction.user, "guild_permissions")
            else False
        )
        is_owner = interaction.user.id in self.bot.settings.owner_ids
        if not is_admin and not is_owner:
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Permission Denied",
                    "You need **Manage Server** permission.",
                ),
                ephemeral=True,
            )
            return

        db = self.bot.database
        gid = str(interaction.guild_id)

        if channel is None:
            # Remove
            removed = await db.remove_request_channel(gid)
            self._request_channel_cache.pop(interaction.guild_id, None)
            if removed:
                await interaction.response.send_message(
                    embed=Embedder.success(
                        "Request Channel Removed",
                        "\U0001f5d1 Song request channel has been removed.",
                    )
                )
            else:
                await interaction.response.send_message(
                    embed=Embedder.warning(
                        "No Channel Set",
                        "There's no song request channel configured.",
                    ),
                    ephemeral=True,
                )
        else:
            await db.set_request_channel(gid, str(channel.id), str(interaction.user.id))
            self._request_channel_cache[interaction.guild_id] = channel.id
            await interaction.response.send_message(
                embed=Embedder.success(
                    "Request Channel Set",
                    f"\U0001f3b5 {channel.mention} is now the song request channel!\n\n"
                    "Users can type a song name or paste a music link there to auto-queue it.",
                )
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Auto-queue songs from the request channel."""
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id

        # Check cache first
        channel_id = self._request_channel_cache.get(guild_id)
        if channel_id is None:
            # Check DB
            db = getattr(self.bot, "database", None)
            if not db:
                return
            ch_id = await db.get_request_channel(str(guild_id))
            if ch_id:
                channel_id = int(ch_id)
                self._request_channel_cache[guild_id] = channel_id
            else:
                # No request channel — store 0 to avoid repeated DB lookups
                self._request_channel_cache[guild_id] = 0
                return

        if channel_id == 0 or message.channel.id != channel_id:
            return

        # This is a message in the request channel — treat as song request
        query = message.content.strip()
        if not query or len(query) > 200:
            return

        music_cog = self._get_music_cog()
        if not music_cog or not music_cog._services_ready():
            return

        member = message.guild.get_member(message.author.id)
        if not member or not member.voice or not member.voice.channel:
            try:
                await message.add_reaction("\u274c")
                reply = await message.reply(
                    embed=Embedder.error("Not in VC", "\U0001f50a Join a voice channel first!"),
                )
                await asyncio.sleep(5)
                await reply.delete()
            except Exception:
                pass
            return

        try:
            await message.add_reaction("\U0001f3b5")

            # Resolve URL if needed
            search_query = await music_cog._resolve_query(query)
            songs = await music_cog.music_api.search(search_query, limit=5)
            if not songs:
                await message.remove_reaction("\U0001f3b5", self.bot.user)
                await message.add_reaction("\u274c")
                return

            from utils.music_api import _pick_best_url, pick_best_match
            song = await music_cog.music_api.ensure_download_urls(pick_best_match(songs, search_query))
            stream_url = _pick_best_url(song.get("download_urls", []), "320kbps")
            if not stream_url:
                await message.remove_reaction("\U0001f3b5", self.bot.user)
                await message.add_reaction("\u274c")
                return

            state = self._get_music_state(guild_id)
            if not state:
                return

            async with state._lock:
                if song.get("id"):
                    state.requester_map[song["id"]] = message.author.id
                state.text_channel = message.channel

                if state.idle_task and not state.idle_task.done():
                    state.idle_task.cancel()
                    state.idle_task = None

                # Fast path: already connected and playing — just queue
                # without touching the encoder (avoids audible glitches).
                already_playing = (
                    state.voice_client
                    and state.voice_client.is_connected()
                    and (state.voice_client.is_playing() or state.voice_client.is_paused())
                )

                if already_playing:
                    state.queue.append(song)
                    await message.remove_reaction("\U0001f3b5", self.bot.user)
                    await message.add_reaction("\u2705")
                    try:
                        pos = len(state.queue)
                        reply = await message.reply(
                            embed=Embedder.standard(
                                "\U0001f3b5 Queued",
                                f"**{song['name']}** \u2014 {song['artist']} (#{pos})",
                                footer=BRAND,
                            )
                        )
                        await asyncio.sleep(10)
                        await reply.delete()
                    except Exception:
                        pass
                else:
                    voice_channel = member.voice.channel
                    try:
                        vc = await music_cog._ensure_voice(message.guild, voice_channel, state)
                        music_cog._tune_encoder(vc)
                    except Exception:
                        await message.add_reaction("\u274c")
                        return

                    state.current = song
                    await music_cog._start_playback(guild_id, stream_url)
                    await message.remove_reaction("\U0001f3b5", self.bot.user)
                    await message.add_reaction("\u25b6\ufe0f")

                    # Send NP embed
                    try:
                        from cogs.music import _now_playing_embed, NowPlayingView
                        requester = self.bot.get_user(message.author.id)
                        embed = _now_playing_embed(state, song, requester)
                        view = NowPlayingView(song, music_cog, guild_id)
                        msg = await message.channel.send(embed=embed, view=view)
                        state._np_message = msg
                        music_cog._start_progress_updater(guild_id)
                    except Exception:
                        pass

        except Exception as exc:
            logger.warning("Request channel auto-queue failed: %s", exc)
            try:
                await message.add_reaction("\u274c")
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════════════
    #  /sleeptimer — Sleep timer
    # ══════════════════════════════════════════════════════════════════

    @app_commands.command(name="sleeptimer", description="Set a sleep timer to auto-disconnect")
    @app_commands.describe(minutes="Minutes until disconnect (0 to cancel)")
    async def sleeptimer_cmd(self, interaction: discord.Interaction, minutes: int) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        guild_id = interaction.guild_id

        # Cancel existing timer
        existing = self._sleep_timers.get(guild_id)
        if existing and not existing.done():
            existing.cancel()
            del self._sleep_timers[guild_id]

        if minutes <= 0:
            await interaction.response.send_message(
                embed=Embedder.info(
                    "Sleep Timer Cancelled",
                    "\u23f0 Sleep timer has been cancelled.",
                )
            )
            return

        if minutes > 480:  # 8 hours max
            await interaction.response.send_message(
                embed=Embedder.error(
                    "Too Long",
                    "\u274c Maximum sleep timer is **480 minutes** (8 hours).",
                ),
                ephemeral=True,
            )
            return

        # Start the timer
        self._sleep_timers[guild_id] = asyncio.ensure_future(
            self._sleep_timer_task(guild_id, minutes, interaction.channel)
        )

        await interaction.response.send_message(
            embed=Embedder.success(
                "Sleep Timer Set",
                f"\U0001f634 Music will stop in **{minutes} minute{'s' if minutes != 1 else ''}**.\n"
                f"Use `/sleeptimer 0` to cancel.",
            )
        )

    async def _sleep_timer_task(
        self,
        guild_id: int,
        minutes: int,
        channel: Optional[discord.abc.Messageable],
    ) -> None:
        """Background task that disconnects after the timer expires."""
        try:
            await asyncio.sleep(minutes * 60)

            music_cog = self._get_music_cog()
            if music_cog:
                state = music_cog._get_state(guild_id)
                if state.voice_client and state.voice_client.is_connected():
                    await music_cog._stop_and_leave(guild_id)
                    if channel:
                        try:
                            await channel.send(
                                embed=Embedder.info(
                                    "\U0001f634 Sleep Timer",
                                    "Music stopped and disconnected. Good night!",
                                )
                            )
                        except Exception:
                            pass

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("Sleep timer error for guild %d: %s", guild_id, exc)
        finally:
            self._sleep_timers.pop(guild_id, None)

    # ══════════════════════════════════════════════════════════════════
    #  /import — Spotify / Apple Music / YouTube Music playlist import
    # ══════════════════════════════════════════════════════════════════

    @app_commands.command(
        name="import",
        description="Import a Spotify, Apple Music, YouTube Music, or Tidal playlist/mix into a saved playlist",
    )
    @app_commands.describe(
        url="Spotify, Apple Music, YouTube Music, or Tidal playlist/album URL",
        playlist_name="Name for the imported playlist (auto-generated if blank)",
    )
    async def import_cmd(
        self, interaction: discord.Interaction, url: str, playlist_name: Optional[str] = None
    ) -> None:
        await interaction.response.defer()

        # Detect platform
        tracks: List[Dict[str, str]] = []
        auto_name = "Imported Playlist"

        spotify_playlist_match = re.search(
            r"(?:https?://)?open\.spotify\.com/(?:intl-\w+/)?playlist/([a-zA-Z0-9]+)", url
        )
        spotify_album_match = re.search(
            r"(?:https?://)?open\.spotify\.com/(?:intl-\w+/)?album/([a-zA-Z0-9]+)", url
        )
        apple_playlist_match = re.search(
            r"(?:https?://)?music\.apple\.com/(\w+)/playlist/([^/]+)/([a-zA-Z0-9.]+)", url
        )
        apple_album_match = re.search(
            r"(?:https?://)?music\.apple\.com/(\w+)/album/([^/]+)/(\d+)", url
        )
        # YouTube Music / YouTube playlist and album patterns
        yt_playlist_match = re.search(
            r"(?:https?://)?(?:music\.)?youtube\.com/playlist\?list=([a-zA-Z0-9_-]+)", url
        )
        yt_album_match = re.search(
            r"(?:https?://)?music\.youtube\.com/browse/(MPREb_[a-zA-Z0-9_-]+)", url
        )
        # Tidal playlist, album, and mix patterns
        tidal_playlist_match = re.search(
            r"(?:https?://)?(?:listen\.)?tidal\.com/(?:browse/)?playlist/([a-f0-9-]+)", url
        )
        tidal_album_match = re.search(
            r"(?:https?://)?(?:listen\.)?tidal\.com/(?:browse/)?album/(\d+)", url
        )
        tidal_mix_match = re.search(
            r"(?:https?://)?(?:listen\.)?tidal\.com/(?:browse/)?mix/([a-zA-Z0-9]+)", url
        )

        session = getattr(self._get_music_cog(), "_session", None)
        if not session:
            await interaction.followup.send(
                embed=Embedder.error("Unavailable", "\u274c Music services unavailable."),
                ephemeral=True,
            )
            return

        if spotify_playlist_match or spotify_album_match:
            tracks, auto_name = await self._import_spotify(
                url, session,
                is_album=bool(spotify_album_match),
            )
        elif apple_playlist_match or apple_album_match:
            tracks, auto_name = await self._import_apple_music(url, session)
        elif yt_playlist_match or yt_album_match:
            tracks, auto_name = await self._import_youtube_music(
                url, session,
                playlist_id=yt_playlist_match.group(1) if yt_playlist_match else None,
                browse_id=yt_album_match.group(1) if yt_album_match else None,
            )
        elif tidal_playlist_match or tidal_album_match or tidal_mix_match:
            if tidal_playlist_match:
                item_type = "playlist"
                item_id = tidal_playlist_match.group(1)
            elif tidal_album_match:
                item_type = "album"
                item_id = tidal_album_match.group(1)
            else:
                item_type = "mix"
                item_id = tidal_mix_match.group(1)
            tracks, auto_name = await self._import_tidal(
                session, item_type=item_type, item_id=item_id,
            )
        else:
            await interaction.followup.send(
                embed=Embedder.error(
                    "Unsupported URL",
                    "\u274c Please provide a **playlist** or **album** URL.\n\n"
                    "**Supported formats:**\n"
                    "\u2022 `https://open.spotify.com/playlist/...`\n"
                    "\u2022 `https://open.spotify.com/album/...`\n"
                    "\u2022 `https://music.apple.com/.../playlist/...`\n"
                    "\u2022 `https://music.apple.com/.../album/...`\n"
                    "\u2022 `https://music.youtube.com/playlist?list=...`\n"
                    "\u2022 `https://music.youtube.com/browse/MPREb_...`\n"
                    "\u2022 `https://youtube.com/playlist?list=...`\n"
                    "\u2022 `https://tidal.com/browse/playlist/...`\n"
                    "\u2022 `https://tidal.com/browse/album/...`\n"
                    "\u2022 `https://listen.tidal.com/playlist/...`\n"
                    "\u2022 `https://listen.tidal.com/album/...`\n"
                    "\u2022 `https://tidal.com/mix/...`",
                ),
                ephemeral=True,
            )
            return

        if not tracks:
            await interaction.followup.send(
                embed=Embedder.error(
                    "Import Failed",
                    "\u274c Could not extract any tracks from that URL.\n"
                    "The playlist may be private or the URL may be invalid.",
                ),
                ephemeral=True,
            )
            return

        # ── Smart duplicate recognition ─────────────────────────────
        # If a playlist with the same name already exists, detect the
        # difference and update it (add new songs, report changes)
        # instead of failing or creating a duplicate.
        db = self.bot.database
        uid = str(interaction.user.id)
        name = playlist_name or auto_name

        existing_playlist = await db.get_playlist_by_name(uid, name)
        is_update = False
        existing_keys: set = set()

        if existing_playlist:
            # Smart recognition: update the existing playlist
            pl_id = existing_playlist["id"]
            is_update = True
            existing_keys = await db.get_playlist_song_keys(pl_id)
            existing_count = existing_playlist["song_count"]
        else:
            pl_id = await db.create_playlist(uid, name)
            if pl_id is None:
                # Try with a unique suffix
                import datetime
                suffix = datetime.datetime.now().strftime("%m%d-%H%M")
                name = f"{name} ({suffix})"
                pl_id = await db.create_playlist(uid, name)
                if pl_id is None:
                    await interaction.followup.send(
                        embed=Embedder.error(
                            "Failed",
                            "\u274c Could not create the playlist. Try a different name.",
                        ),
                        ephemeral=True,
                    )
                    return

        # Now resolve each track through the music API and save
        music_cog = self._get_music_cog()
        if not music_cog or not music_cog._services_ready():
            await interaction.followup.send(
                embed=Embedder.error("Unavailable", "\u274c Music services unavailable."),
                ephemeral=True,
            )
            return

        action_word = "Updating" if is_update else "Importing"
        status_msg = await interaction.followup.send(
            embed=Embedder.standard(
                f"\U0001f4e5 {action_word} Playlist",
                f"\u23f3 Resolving **{len(tracks)}** tracks from **{auto_name}**...\n"
                + (f"Existing playlist found with **{existing_count}** songs \u2014 adding new tracks only."
                   if is_update else "This may take a moment."),
                footer=BRAND,
            )
        )

        saved_count = 0
        skipped_count = 0
        failed_count = 0

        for track in tracks[:MAX_SONGS_PER_PLAYLIST]:
            query = f"{track.get('artist', '')} {track.get('name', '')}".strip()
            if not query:
                failed_count += 1
                continue

            try:
                results = await music_cog.music_api.search(query, limit=5)
                if results:
                    from utils.music_api import pick_best_match
                    key = _song_key(pick_best_match(results, query))

                    # Skip songs that already exist in the playlist
                    if is_update and key in existing_keys:
                        skipped_count += 1
                        continue

                    if await db.add_song_to_playlist(pl_id, key):
                        saved_count += 1
                        existing_keys.add(key)  # track newly added
                    else:
                        failed_count += 1
                else:
                    failed_count += 1
            except Exception:
                failed_count += 1

            # Small delay to avoid rate limits
            await asyncio.sleep(0.3)

        # Touch the timestamp so it sorts to the top
        if is_update:
            await db.update_playlist_timestamp(pl_id)

        # Final report
        if is_update:
            lines = [
                f"\U0001f504 Updated **{name}**",
                f"\u2795 **{saved_count}** new track{'s' if saved_count != 1 else ''} added",
            ]
            if skipped_count > 0:
                lines.append(f"\u2714\ufe0f {skipped_count} track{'s' if skipped_count != 1 else ''} already existed (skipped)")
            if failed_count > 0:
                lines.append(f"\u26a0\ufe0f {failed_count} track{'s' if failed_count != 1 else ''} could not be resolved")
            total_now = (existing_count or 0) + saved_count
            lines.append(f"\n**{total_now}** total songs in playlist")
        else:
            lines = [
                f"\u2705 Saved **{saved_count}** of **{len(tracks)}** tracks to **{name}**",
            ]
            if failed_count > 0:
                lines.append(f"\u26a0\ufe0f {failed_count} track{'s' if failed_count != 1 else ''} could not be resolved")
        lines.append(f"\nUse `/playlist play {name}` to start listening!")

        try:
            await status_msg.edit(
                embed=Embedder.success(
                    "Update Complete" if is_update else "Import Complete",
                    "\n".join(lines),
                )
            )
        except Exception:
            pass

    # ── Spotify import helper ──────────────────────────────────────

    async def _import_spotify(
        self,
        url: str,
        session: aiohttp.ClientSession,
        *,
        is_album: bool = False,
    ) -> tuple:
        """Extract track names from a Spotify playlist/album using the embed page."""
        tracks: List[Dict[str, str]] = []
        name = "Spotify Import"

        try:
            # Use Spotify's oEmbed to get the title
            from urllib.parse import quote
            oembed_url = f"https://open.spotify.com/oembed?url={quote(url, safe='')}"
            async with session.get(
                oembed_url,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    name = data.get("title", name)

            # Fetch the embed page to extract track listing
            if not url.startswith("http"):
                url = f"https://{url}"

            # Use the Spotify embed endpoint which returns track data
            # Extract the playlist/album ID
            match = re.search(r"(playlist|album)/([a-zA-Z0-9]+)", url)
            if not match:
                return tracks, name

            item_type = match.group(1)
            item_id = match.group(2)

            embed_url = f"https://open.spotify.com/embed/{item_type}/{item_id}"
            async with session.get(
                embed_url,
                timeout=aiohttp.ClientTimeout(total=20),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                },
            ) as resp:
                if resp.status != 200:
                    return tracks, name

                html = await resp.text()

                # Parse tracks from the embed page HTML
                # Spotify embed pages contain JSON data with track listing
                # Look for the track list in the page's script data
                track_pattern = re.findall(
                    r'"name"\s*:\s*"([^"]+)"[^}]*?"artists"\s*:\s*\[\s*\{[^}]*?"name"\s*:\s*"([^"]+)"',
                    html,
                )
                if track_pattern:
                    for track_name, artist_name in track_pattern:
                        tracks.append({"name": track_name, "artist": artist_name})
                else:
                    # Alternative: look for simpler patterns
                    alt_pattern = re.findall(
                        r'"title"\s*:\s*"([^"]+)"[^}]*?"subtitle"\s*:\s*"([^"]+)"',
                        html,
                    )
                    for track_name, artist_name in alt_pattern:
                        if track_name and artist_name:
                            tracks.append({"name": track_name, "artist": artist_name})

        except Exception as exc:
            logger.warning("Spotify import failed: %s", exc)

        return tracks, name

    # ── Apple Music import helper ──────────────────────────────────

    async def _import_apple_music(
        self, url: str, session: aiohttp.ClientSession
    ) -> tuple:
        """Extract track names from an Apple Music playlist/album page."""
        tracks: List[Dict[str, str]] = []
        name = "Apple Music Import"

        try:
            if not url.startswith("http"):
                url = f"https://{url}"

            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=20),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                },
            ) as resp:
                if resp.status != 200:
                    return tracks, name

                html = await resp.text()

                # Extract playlist/album title
                title_match = re.search(r'<title>([^<]+)</title>', html)
                if title_match:
                    raw_title = title_match.group(1)
                    # Clean up " - Apple Music" suffix
                    raw_title = re.sub(r'\s*[-\u2014]\s*Apple Music\s*$', '', raw_title)
                    if raw_title:
                        name = raw_title

                # Apple Music pages have structured data (JSON-LD)
                # Look for MusicRecording entries
                json_ld_matches = re.findall(
                    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                    html,
                    re.DOTALL,
                )
                for json_str in json_ld_matches:
                    try:
                        data = json.loads(json_str)
                        if isinstance(data, dict) and data.get("@type") == "MusicAlbum":
                            for track in data.get("track", []):
                                if isinstance(track, dict):
                                    t_name = track.get("name", "")
                                    t_artist = ""
                                    by_artist = track.get("byArtist", {})
                                    if isinstance(by_artist, dict):
                                        t_artist = by_artist.get("name", "")
                                    elif isinstance(by_artist, list) and by_artist:
                                        t_artist = by_artist[0].get("name", "")
                                    if t_name:
                                        tracks.append({"name": t_name, "artist": t_artist})
                    except json.JSONDecodeError:
                        continue

                # Fallback: parse meta tags
                if not tracks:
                    song_matches = re.findall(
                        r'class="songs-list-row__song-name[^"]*"[^>]*>([^<]+)<',
                        html,
                    )
                    artist_matches = re.findall(
                        r'class="songs-list-row__by-artist[^"]*"[^>]*>([^<]+)<',
                        html,
                    )
                    for i, t_name in enumerate(song_matches):
                        t_artist = artist_matches[i] if i < len(artist_matches) else ""
                        tracks.append({
                            "name": t_name.strip(),
                            "artist": t_artist.strip(),
                        })

        except Exception as exc:
            logger.warning("Apple Music import failed: %s", exc)

        return tracks, name

    # ── YouTube Music import helper ────────────────────────────────

    async def _import_youtube_music(
        self,
        url: str,
        session: aiohttp.ClientSession,
        *,
        playlist_id: Optional[str] = None,
        browse_id: Optional[str] = None,
    ) -> tuple:
        """Extract track names from a YouTube Music/YouTube playlist or album.

        Strategy:
          1. For playlists — fetch the YouTube playlist page HTML, extract the
             ``ytInitialData`` JSON blob and parse video titles + channel names.
          2. For YT Music albums (browse_id) — call the internal ``youtubei``
             browse API which returns structured JSON with track listings.
          3. Fallback — attempt to parse ``<title>`` and ``videoRenderer``
             patterns from the raw HTML.
        """
        tracks: List[Dict[str, str]] = []
        name = "YouTube Music Import"

        _HEADERS = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        _COOKIES = {"CONSENT": "YES+1"}  # bypass EU consent wall

        try:
            # ── Album via browse API ──────────────────────────────
            if browse_id:
                tracks, name = await self._yt_browse_album(browse_id, session, _HEADERS)
                if tracks:
                    return tracks, name

            # ── Playlist via page scraping ────────────────────────
            if playlist_id:
                page_url = f"https://www.youtube.com/playlist?list={playlist_id}"
            else:
                page_url = url if url.startswith("http") else f"https://{url}"
                # Normalise music.youtube.com to www.youtube.com for playlist pages
                page_url = page_url.replace("music.youtube.com", "www.youtube.com")

            async with session.get(
                page_url,
                timeout=aiohttp.ClientTimeout(total=25),
                headers=_HEADERS,
                cookies=_COOKIES,
            ) as resp:
                if resp.status != 200:
                    logger.warning("YouTube playlist page returned %d", resp.status)
                    return tracks, name

                html_text = await resp.text()

            # Extract ytInitialData JSON blob
            yt_data = self._extract_yt_initial_data(html_text)
            if not yt_data:
                logger.warning("Could not extract ytInitialData from YouTube page")
                return tracks, name

            # Extract playlist title
            try:
                metadata = yt_data.get("metadata", {})
                pl_renderer = metadata.get("playlistMetadataRenderer", {})
                name = pl_renderer.get("title", name)
            except Exception:
                pass

            # Navigate to the video list
            # Path: contents → twoColumnBrowseResultsRenderer → tabs[0]
            #   → tabRenderer → content → sectionListRenderer → contents[0]
            #   → itemSectionRenderer → contents[0]
            #   → playlistVideoListRenderer → contents[]
            try:
                tabs = (
                    yt_data
                    .get("contents", {})
                    .get("twoColumnBrowseResultsRenderer", {})
                    .get("tabs", [])
                )
                if not tabs:
                    return tracks, name

                tab_content = tabs[0].get("tabRenderer", {}).get("content", {})
                section_contents = (
                    tab_content
                    .get("sectionListRenderer", {})
                    .get("contents", [])
                )

                video_items = []
                for section in section_contents:
                    item_section = section.get("itemSectionRenderer", {})
                    for inner in item_section.get("contents", []):
                        playlist_renderer = inner.get("playlistVideoListRenderer", {})
                        if playlist_renderer:
                            video_items = playlist_renderer.get("contents", [])
                            break
                    if video_items:
                        break

                for item in video_items:
                    renderer = item.get("playlistVideoRenderer", {})
                    if not renderer:
                        continue

                    title_obj = renderer.get("title", {})
                    video_title = ""
                    # title.runs[0].text or title.simpleText
                    runs = title_obj.get("runs", [])
                    if runs:
                        video_title = runs[0].get("text", "")
                    elif title_obj.get("simpleText"):
                        video_title = title_obj["simpleText"]

                    # Channel / artist: shortBylineText.runs[0].text
                    channel_name = ""
                    byline = renderer.get("shortBylineText", {})
                    byline_runs = byline.get("runs", [])
                    if byline_runs:
                        channel_name = byline_runs[0].get("text", "")

                    if video_title:
                        # Try to split "Artist - Song" from the title
                        parsed_name, parsed_artist = self._parse_yt_title(
                            video_title, channel_name
                        )
                        tracks.append({"name": parsed_name, "artist": parsed_artist})

            except Exception as exc:
                logger.warning("YouTube playlist parsing error: %s", exc)

        except Exception as exc:
            logger.warning("YouTube Music import failed: %s", exc)

        return tracks, name

    async def _yt_browse_album(
        self,
        browse_id: str,
        session: aiohttp.ClientSession,
        headers: Dict[str, str],
    ) -> tuple:
        """Fetch a YouTube Music album via the internal browse API."""
        tracks: List[Dict[str, str]] = []
        name = "YouTube Music Album"

        try:
            api_url = "https://music.youtube.com/youtubei/v1/browse?prettyPrint=false"
            payload = {
                "browseId": browse_id,
                "context": {
                    "client": {
                        "clientName": "WEB_REMIX",
                        "clientVersion": "1.20241120.01.00",
                        "hl": "en",
                        "gl": "US",
                    },
                },
            }

            async with session.post(
                api_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=20),
                headers={
                    **headers,
                    "Content-Type": "application/json",
                    "Origin": "https://music.youtube.com",
                    "Referer": "https://music.youtube.com/",
                },
            ) as resp:
                if resp.status != 200:
                    logger.warning("YouTube Music browse API returned %d", resp.status)
                    return tracks, name

                data = await resp.json(content_type=None)

            # Extract album title from header
            header = data.get("header", {})
            music_header = header.get("musicImmersiveHeaderRenderer", {}) or header.get(
                "musicDetailHeaderRenderer", {}
            )
            title_obj = music_header.get("title", {})
            runs = title_obj.get("runs", [])
            if runs:
                name = runs[0].get("text", name)

            # Extract album artist from subtitle
            album_artist = ""
            subtitle = music_header.get("subtitle", {})
            sub_runs = subtitle.get("runs", [])
            # Subtitle runs are usually: ["Album", " • ", "2024", " • ", "ArtistName", ...]
            # or ["ArtistName", " • ", "Album", " • ", "2024"]
            for run in sub_runs:
                text = run.get("text", "").strip()
                # Skip separators and common labels
                if text in ("", "•", "\u2022") or text.isdigit() or len(text) <= 2:
                    continue
                if text.lower() in ("album", "single", "ep", "playlist", "song", "video"):
                    continue
                # First real text run that looks like an artist name
                album_artist = text
                break

            # Extract tracks from the shelf
            # Path: contents → singleColumnBrowseResultsRenderer → tabs[0]
            #   → tabRenderer → content → sectionListRenderer → contents[]
            #   → musicShelfRenderer → contents[]
            #   → musicResponsiveListItemRenderer
            try:
                tabs = (
                    data
                    .get("contents", {})
                    .get("singleColumnBrowseResultsRenderer", {})
                    .get("tabs", [])
                )
                if not tabs:
                    return tracks, name

                tab_content = tabs[0].get("tabRenderer", {}).get("content", {})
                sections = tab_content.get("sectionListRenderer", {}).get("contents", [])

                for section in sections:
                    shelf = section.get("musicShelfRenderer", {})
                    if not shelf:
                        continue

                    for item in shelf.get("contents", []):
                        renderer = item.get("musicResponsiveListItemRenderer", {})
                        if not renderer:
                            continue

                        flex_columns = renderer.get("flexColumns", [])
                        track_name = ""
                        track_artist = album_artist  # default to album artist

                        # First flex column → song title
                        if flex_columns:
                            col0 = flex_columns[0].get(
                                "musicResponsiveListItemFlexColumnRenderer", {}
                            )
                            text_obj = col0.get("text", {})
                            col_runs = text_obj.get("runs", [])
                            if col_runs:
                                track_name = col_runs[0].get("text", "")

                        # Second flex column → artist (if present)
                        if len(flex_columns) > 1:
                            col1 = flex_columns[1].get(
                                "musicResponsiveListItemFlexColumnRenderer", {}
                            )
                            text_obj = col1.get("text", {})
                            col_runs = text_obj.get("runs", [])
                            if col_runs:
                                artist_text = col_runs[0].get("text", "")
                                if artist_text:
                                    track_artist = artist_text

                        if track_name:
                            tracks.append({"name": track_name, "artist": track_artist})

            except Exception as exc:
                logger.warning("YouTube Music album track extraction error: %s", exc)

        except Exception as exc:
            logger.warning("YouTube Music browse API failed: %s", exc)

        return tracks, name

    # ── Tidal import helper ───────────────────────────────────────

    async def _import_tidal(
        self,
        session: aiohttp.ClientSession,
        *,
        item_type: str,
        item_id: str,
    ) -> tuple:
        """Extract track names from a Tidal playlist, album, or mix.

        Strategy:
          1. Use Tidal's oEmbed endpoint to retrieve the playlist/album title.
          2. Fetch the Tidal embed page which renders track listings
             server-side and parse the HTML for track data.
          3. Fallback: scrape the main Tidal page ``<title>`` and any
             structured ``<meta>`` / JSON-LD data available in the HTML.
        """
        tracks: List[Dict[str, str]] = []
        name = "Tidal Import"

        _HEADERS = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

        # Mixes use /mix/ in the URL path (no /browse/ prefix typically)
        if item_type == "mix":
            canonical_url = f"https://tidal.com/mix/{item_id}"
        else:
            canonical_url = f"https://tidal.com/browse/{item_type}/{item_id}"

        try:
            # ── Step 1: Get the title via oEmbed ──────────────────
            try:
                oembed_url = f"https://oembed.tidal.com/?url={canonical_url}"
                async with session.get(
                    oembed_url,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        oembed_title = data.get("title", "")
                        if oembed_title:
                            name = oembed_title
            except Exception:
                pass  # title is a nice-to-have; continue even if this fails

            # ── Step 2: Fetch the embed page ──────────────────────
            # Playlists/albums use plural path (playlists/, albums/)
            # Mixes are not pluralised on the embed domain
            if item_type == "mix":
                embed_url = f"https://embed.tidal.com/mix/{item_id}"
            else:
                embed_url = f"https://embed.tidal.com/{item_type}s/{item_id}"
            async with session.get(
                embed_url,
                timeout=aiohttp.ClientTimeout(total=20),
                headers=_HEADERS,
            ) as resp:
                if resp.status == 200:
                    html = await resp.text()

                    # The embed page often includes a JSON data blob with
                    # track information in a <script> tag.
                    # Look for track data in __NEXT_DATA__ or similar JSON blobs
                    next_data_match = re.search(
                        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                        html,
                        re.DOTALL,
                    )
                    if next_data_match:
                        try:
                            next_data = json.loads(next_data_match.group(1))
                            tracks = self._extract_tidal_tracks_from_json(next_data)
                        except json.JSONDecodeError:
                            pass

                    # Alternative: look for inline JSON state/data blobs
                    if not tracks:
                        state_match = re.search(
                            r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\});\s*</script>',
                            html,
                            re.DOTALL,
                        )
                        if state_match:
                            try:
                                state_data = json.loads(state_match.group(1))
                                tracks = self._extract_tidal_tracks_from_json(state_data)
                            except json.JSONDecodeError:
                                pass

                    # Fallback: parse structured meta / track patterns from HTML
                    if not tracks:
                        track_pattern = re.findall(
                            r'"title"\s*:\s*"([^"]+)"[^}]*?"artist(?:Name|s?)"\s*:\s*"([^"]+)"',
                            html,
                        )
                        for track_name, artist_name in track_pattern:
                            if track_name and artist_name:
                                tracks.append({"name": track_name, "artist": artist_name})

                    # Fallback: parse <list-item> custom HTML elements used
                    # by the Tidal embed player for mixes, playlists & albums.
                    if not tracks:
                        tracks = self._extract_tidal_tracks_from_html(html)

                    # Try to extract the collection title from the embed page
                    if name == "Tidal Import":
                        title_h1 = re.search(
                            r'<h1[^>]*class="media-album"[^>]*>'
                            r'\s*(?:<a[^>]*>)?\s*([^<]+)',
                            html,
                        )
                        if title_h1:
                            extracted = unescape(title_h1.group(1).strip())
                            if extracted:
                                name = extracted

            # ── Step 3: Fallback — scrape the main page ───────────
            if not tracks:
                async with session.get(
                    canonical_url,
                    timeout=aiohttp.ClientTimeout(total=20),
                    headers=_HEADERS,
                ) as resp:
                    if resp.status == 200:
                        html = await resp.text()

                        # Try to extract title if we don't have one yet
                        if name == "Tidal Import":
                            title_match = re.search(r'<title>([^<]+)</title>', html)
                            if title_match:
                                raw_title = title_match.group(1)
                                raw_title = re.sub(
                                    r'\s*[-\u2014]\s*(?:Tidal|TIDAL)\s*$', '', raw_title
                                )
                                if raw_title:
                                    name = raw_title

                        # Look for JSON-LD structured data
                        json_ld_matches = re.findall(
                            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                            html,
                            re.DOTALL,
                        )
                        for json_str in json_ld_matches:
                            try:
                                ld_data = json.loads(json_str)
                                if isinstance(ld_data, dict):
                                    ld_type = ld_data.get("@type", "")
                                    if ld_type in ("MusicAlbum", "MusicPlaylist"):
                                        for track in ld_data.get("track", []):
                                            if isinstance(track, dict):
                                                t_name = track.get("name", "")
                                                t_artist = ""
                                                by_artist = track.get("byArtist", {})
                                                if isinstance(by_artist, dict):
                                                    t_artist = by_artist.get("name", "")
                                                elif isinstance(by_artist, list) and by_artist:
                                                    t_artist = by_artist[0].get("name", "")
                                                if t_name:
                                                    tracks.append({
                                                        "name": t_name,
                                                        "artist": t_artist,
                                                    })
                            except json.JSONDecodeError:
                                continue

                        # Last resort: look for __NEXT_DATA__ on main page too
                        if not tracks:
                            next_match = re.search(
                                r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                                html,
                                re.DOTALL,
                            )
                            if next_match:
                                try:
                                    nd = json.loads(next_match.group(1))
                                    tracks = self._extract_tidal_tracks_from_json(nd)
                                except json.JSONDecodeError:
                                    pass

                        # Pattern fallback on main page HTML
                        if not tracks:
                            track_pattern = re.findall(
                                r'"title"\s*:\s*"([^"]+)"[^}]*?"artist(?:Name|s?)"\s*:\s*"([^"]+)"',
                                html,
                            )
                            for track_name, artist_name in track_pattern:
                                if track_name and artist_name:
                                    tracks.append({"name": track_name, "artist": artist_name})

        except Exception as exc:
            logger.warning("Tidal import failed: %s", exc)

        return tracks, name

    @staticmethod
    def _extract_tidal_tracks_from_json(data: Any) -> List[Dict[str, str]]:
        """Recursively search a JSON blob for Tidal track listings.

        Tidal embeds and pages may store track data in various nested
        structures.  This helper walks the tree looking for arrays of
        objects that contain ``title`` (or ``name``) and ``artist``
        (or ``artists``) keys — the typical shape of a track item.
        """
        tracks: List[Dict[str, str]] = []

        def _walk(obj: Any) -> None:
            if isinstance(obj, dict):
                # Check if this dict looks like a track item
                title = obj.get("title") or obj.get("name") or ""
                artist = ""
                # artist could be a string, a dict, or a list
                raw_artist = (
                    obj.get("artist")
                    or obj.get("artists")
                    or obj.get("artistName")
                    or ""
                )
                if isinstance(raw_artist, str):
                    artist = raw_artist
                elif isinstance(raw_artist, dict):
                    artist = raw_artist.get("name", "")
                elif isinstance(raw_artist, list):
                    names = []
                    for a in raw_artist:
                        if isinstance(a, dict):
                            names.append(a.get("name", ""))
                        elif isinstance(a, str):
                            names.append(a)
                    artist = ", ".join(n for n in names if n)

                # Heuristic: if we have both a title and artist and the
                # object also has a duration/trackNumber, it's very
                # likely a track item.
                if title and artist and (
                    "duration" in obj
                    or "trackNumber" in obj
                    or "trackId" in obj
                    or "id" in obj
                ):
                    tracks.append({"name": str(title), "artist": str(artist)})
                    return  # don't recurse into sub-fields of a matched track

                # Otherwise keep searching
                for v in obj.values():
                    _walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk(item)

        _walk(data)
        return tracks

    @staticmethod
    def _extract_tidal_tracks_from_html(html: str) -> List[Dict[str, str]]:
        """Parse ``<list-item>`` custom elements from the Tidal embed player.

        The Tidal embed page renders tracks as custom HTML elements::

            <list-item product-type="track" ...>
              <span slot="title">Song Title</span>
              <span slot="artist"><a ...>Artist 1</a><a ...>Artist 2</a></span>
            </list-item>

        This helper extracts every track-typed ``<list-item>`` block and
        returns a list of ``{"name": ..., "artist": ...}`` dicts.
        """
        _STRIP_TAGS = re.compile(r"<[^>]+>")
        tracks: List[Dict[str, str]] = []

        for item_match in re.finditer(
            r"<list-item\b[^>]*?product-type=\"track\"[^>]*>(.*?)</list-item>",
            html,
            re.DOTALL,
        ):
            block = item_match.group(1)

            title_m = re.search(
                r'<span\s+slot="title">(.*?)</span>', block, re.DOTALL
            )
            artist_m = re.search(
                r'<span\s+slot="artist">(.*?)</span>', block, re.DOTALL
            )

            if not title_m:
                continue

            track_title = unescape(_STRIP_TAGS.sub("", title_m.group(1)).strip())
            if not track_title:
                continue

            artist_name = ""
            if artist_m:
                # Artists may be wrapped in individual <a> tags; strip them
                # and collapse multiple names with ", ".
                raw = artist_m.group(1)
                # Each <a>...</a> is one artist — grab inner text of each
                artist_parts = re.findall(r"<a[^>]*>([^<]+)</a>", raw)
                if artist_parts:
                    artist_name = ", ".join(
                        unescape(a.strip()) for a in artist_parts if a.strip()
                    )
                else:
                    # No <a> tags — plain text artist
                    artist_name = unescape(_STRIP_TAGS.sub("", raw).strip())

            tracks.append({"name": track_title, "artist": artist_name})

        return tracks

    @staticmethod
    def _extract_yt_initial_data(html: str) -> Optional[Dict[str, Any]]:
        """Extract and parse the ``ytInitialData`` JSON blob from YouTube HTML."""
        # Pattern 1: var ytInitialData = {...};
        match = re.search(r"var\s+ytInitialData\s*=\s*(\{.+?\});\s*</script>", html, re.DOTALL)
        if not match:
            # Pattern 2: window["ytInitialData"] = {...};
            match = re.search(
                r'window\["ytInitialData"\]\s*=\s*(\{.+?\});\s*</script>',
                html,
                re.DOTALL,
            )
        if not match:
            return None

        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse ytInitialData JSON: %s", exc)
            return None

    @staticmethod
    def _parse_yt_title(title: str, channel: str = "") -> tuple:
        """Parse a YouTube video title into (song_name, artist).

        Many music videos use the "Artist - Song Title" format.
        This also strips common noise suffixes.
        """
        # Strip common YouTube noise in parens/brackets
        cleaned = re.sub(
            r"\s*[\(\[]\s*(?:Official\s+)?(?:Music\s+|Lyric\s+)?(?:Video|Audio|Visuali[sz]er|Lyrics?|HD|HQ|4K|M/?V|Live)\s*[\)\]]",
            "",
            title,
            flags=re.IGNORECASE,
        )
        # Strip trailing "| Official ..." pipes
        cleaned = re.sub(r"\s*\|\s*Official.*$", "", cleaned, flags=re.IGNORECASE).strip()

        # Try "Artist - Title" split
        if " - " in cleaned:
            parts = cleaned.split(" - ", 1)
            artist = parts[0].strip()
            song = parts[1].strip()
            if artist and song:
                return song, artist

        # Fallback: title is the song name, channel is the artist
        return cleaned or title, channel


# =====================================================================
#  Setup
# =====================================================================

async def setup(bot: "StarzaiBot") -> None:
    await bot.add_cog(MusicPremiumCog(bot))
