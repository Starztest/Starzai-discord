"""
Platform URL resolver for music links.

Extracts song metadata from platform URLs and converts them to
search queries usable by the music API.

Supported platforms:
    • Spotify          (open.spotify.com)
    • Deezer           (deezer.com)
    • Apple Music      (music.apple.com)
    • YouTube Music    (music.youtube.com)
    • YouTube          (youtube.com / youtu.be)
    • SoundCloud       (soundcloud.com)
    • Tidal            (tidal.com / listen.tidal.com)
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote, unquote

import aiohttp

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15  # seconds

# =====================================================================
#  URL regex patterns
# =====================================================================

# ── Spotify ──────────────────────────────────────────────────────────
SPOTIFY_PATTERN = re.compile(
    r"(?:https?://)?open\.spotify\.com/(?:intl-\w+/)?track/([a-zA-Z0-9]+)"
)

# ── Deezer ───────────────────────────────────────────────────────────
DEEZER_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?deezer\.com/(?:\w+/)?track/(\d+)"
)

# ── Apple Music ──────────────────────────────────────────────────────
APPLE_MUSIC_PATTERN = re.compile(
    r"(?:https?://)?music\.apple\.com/(\w+)/album/([^/]+)/(\d+)(?:\?i=(\d+))?"
)
APPLE_MUSIC_SONG_PATTERN = re.compile(
    r"(?:https?://)?music\.apple\.com/(\w+)/song/([^/]+)/(\d+)"
)

# ── YouTube Music ────────────────────────────────────────────────────
# music.youtube.com/watch?v=XXXX  (single track)
YT_MUSIC_PATTERN = re.compile(
    r"(?:https?://)?music\.youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})"
)

# ── YouTube (regular) ───────────────────────────────────────────────
# youtube.com/watch?v=XXXX  |  youtu.be/XXXX  |  youtube.com/shorts/XXXX
YT_PATTERN = re.compile(
    r"(?:https?://)?"
    r"(?:"
    r"(?:www\.)?youtube\.com/(?:watch\?v=|shorts/)([a-zA-Z0-9_-]{11})"
    r"|"
    r"youtu\.be/([a-zA-Z0-9_-]{11})"
    r")"
)

# ── SoundCloud ──────────────────────────────────────────────────────
# soundcloud.com/<user>/<track>  (not sets, playlists, or user pages)
SOUNDCLOUD_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?soundcloud\.com/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)(?:\?|$|#)"
)
# Also catch mobile links: on.soundcloud.com/<short-code>
SOUNDCLOUD_SHORT_PATTERN = re.compile(
    r"(?:https?://)?on\.soundcloud\.com/([a-zA-Z0-9]+)"
)

# ── Tidal ───────────────────────────────────────────────────────────
# tidal.com/browse/track/XXXX  |  listen.tidal.com/track/XXXX
TIDAL_PATTERN = re.compile(
    r"(?:https?://)?(?:listen\.)?tidal\.com/(?:browse/)?track/(\d+)"
)


# =====================================================================
#  Public helpers
# =====================================================================

def is_music_url(text: str) -> bool:
    """Check if the given text contains a recognised music platform URL."""
    text = text.strip()
    return bool(
        SPOTIFY_PATTERN.search(text)
        or DEEZER_PATTERN.search(text)
        or APPLE_MUSIC_PATTERN.search(text)
        or APPLE_MUSIC_SONG_PATTERN.search(text)
        or YT_MUSIC_PATTERN.search(text)
        or YT_PATTERN.search(text)
        or SOUNDCLOUD_PATTERN.search(text)
        or SOUNDCLOUD_SHORT_PATTERN.search(text)
        or TIDAL_PATTERN.search(text)
    )


def _normalize_url(match: re.Match) -> str:
    """Extract the matched URL and ensure it has an https:// scheme."""
    raw = match.group(0)
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw


async def resolve_url(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    """
    Resolve a music platform URL to a search query string.

    Supports Spotify, Deezer, Apple Music, YouTube Music, YouTube,
    SoundCloud, and Tidal track URLs.

    Returns a search query string, or ``None`` if resolution fails.
    """
    url = url.strip()

    # ── Spotify ──────────────────────────────────────────────────
    match = SPOTIFY_PATTERN.search(url)
    if match:
        return await _resolve_spotify(_normalize_url(match), session)

    # ── Deezer ───────────────────────────────────────────────────
    match = DEEZER_PATTERN.search(url)
    if match:
        return await _resolve_deezer(match.group(1), session)

    # ── Apple Music (album link with ?i= track param) ────────────
    match = APPLE_MUSIC_PATTERN.search(url)
    if match:
        return _resolve_apple_music_album(match)

    # ── Apple Music (direct song link) ───────────────────────────
    match = APPLE_MUSIC_SONG_PATTERN.search(url)
    if match:
        return _resolve_apple_music_song(match)

    # ── YouTube Music ────────────────────────────────────────────
    match = YT_MUSIC_PATTERN.search(url)
    if match:
        return await _resolve_youtube(_normalize_url(match), match.group(1), session)

    # ── YouTube ──────────────────────────────────────────────────
    match = YT_PATTERN.search(url)
    if match:
        video_id = match.group(1) or match.group(2)
        return await _resolve_youtube(_normalize_url(match), video_id, session)

    # ── SoundCloud (full URL) ────────────────────────────────────
    match = SOUNDCLOUD_PATTERN.search(url)
    if match:
        return _resolve_soundcloud_slug(match)

    # ── SoundCloud (short link) ──────────────────────────────────
    match = SOUNDCLOUD_SHORT_PATTERN.search(url)
    if match:
        return await _resolve_soundcloud_short(_normalize_url(match), session)

    # ── Tidal ────────────────────────────────────────────────────
    match = TIDAL_PATTERN.search(url)
    if match:
        return await _resolve_tidal(match.group(1), session)

    return None


# =====================================================================
#  Per-platform resolvers
# =====================================================================

# ── Spotify ──────────────────────────────────────────────────────────

async def _resolve_spotify(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    """Resolve a Spotify track URL via the oEmbed endpoint."""
    try:
        encoded_url = quote(url, safe="")
        oembed_url = f"https://open.spotify.com/oembed?url={encoded_url}"
        async with session.get(
            oembed_url,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.warning("Spotify oEmbed returned %d", resp.status)
                return None

            data = await resp.json(content_type=None)
            title = data.get("title", "")
            if title:
                logger.info("Resolved Spotify URL to: %s", title)
                return title

    except Exception as exc:
        logger.warning("Spotify URL resolution failed: %s", exc)

    return None


# ── Deezer ───────────────────────────────────────────────────────────

async def _resolve_deezer(
    track_id: str, session: aiohttp.ClientSession
) -> Optional[str]:
    """Resolve a Deezer track URL via their public API."""
    try:
        api_url = f"https://api.deezer.com/track/{track_id}"
        async with session.get(
            api_url,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.warning("Deezer API returned %d", resp.status)
                return None

            data = await resp.json(content_type=None)
            title = data.get("title", "")
            artist_obj = data.get("artist", {})
            artist_name = artist_obj.get("name", "") if isinstance(artist_obj, dict) else ""

            if title and artist_name:
                query = f"{title} {artist_name}"
                logger.info("Resolved Deezer URL to: %s", query)
                return query
            elif title:
                return title

    except Exception as exc:
        logger.warning("Deezer URL resolution failed: %s", exc)

    return None


# ── Apple Music ──────────────────────────────────────────────────────

def _resolve_apple_music_album(match: re.Match) -> Optional[str]:
    """Extract search query from an Apple Music album URL with track param."""
    album_slug = match.group(2)
    if album_slug:
        query = unquote(album_slug).replace("-", " ")
        logger.info("Resolved Apple Music URL to: %s", query)
        return query
    return None


def _resolve_apple_music_song(match: re.Match) -> Optional[str]:
    """Extract search query from an Apple Music song URL."""
    song_slug = match.group(2)
    if song_slug:
        query = unquote(song_slug).replace("-", " ")
        logger.info("Resolved Apple Music song URL to: %s", query)
        return query
    return None


# ── YouTube / YouTube Music ──────────────────────────────────────────

async def _resolve_youtube(
    url: str, video_id: str, session: aiohttp.ClientSession
) -> Optional[str]:
    """Resolve a YouTube / YouTube Music URL via oEmbed.

    Works for both youtube.com and music.youtube.com — the oEmbed
    endpoint returns the video title which is typically
    ``"Artist - Song Name"`` for music videos.
    """
    try:
        # YouTube's oEmbed works for any youtube.com or music.youtube.com URL
        oembed_url = (
            f"https://www.youtube.com/oembed"
            f"?url=https://www.youtube.com/watch?v={video_id}&format=json"
        )
        async with session.get(
            oembed_url,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.warning("YouTube oEmbed returned %d for %s", resp.status, video_id)
                return None

            data = await resp.json(content_type=None)
            title = data.get("title", "")
            author = data.get("author_name", "")

            if title:
                # Strip common noise from YouTube titles
                clean = _clean_yt_title(title)
                # If the title doesn't already include the artist, prepend it
                if author and author.lower() not in clean.lower():
                    clean = f"{clean} {author}"
                logger.info("Resolved YouTube URL to: %s", clean)
                return clean

    except Exception as exc:
        logger.warning("YouTube URL resolution failed: %s", exc)

    return None


def _clean_yt_title(title: str) -> str:
    """Remove common YouTube title noise like '(Official Video)', 'MV', etc."""
    # Remove parenthesised/bracketed noise
    noise_patterns = [
        r"\s*[\(\[]\s*(?:Official\s+)?(?:Music\s+)?Video\s*[\)\]]",
        r"\s*[\(\[]\s*(?:Official\s+)?(?:Lyric\s+)?Video\s*[\)\]]",
        r"\s*[\(\[]\s*(?:Official\s+)?Audio\s*[\)\]]",
        r"\s*[\(\[]\s*Visuali[sz]er\s*[\)\]]",
        r"\s*[\(\[]\s*Lyrics?\s*[\)\]]",
        r"\s*[\(\[]\s*HD\s*[\)\]]",
        r"\s*[\(\[]\s*HQ\s*[\)\]]",
        r"\s*[\(\[]\s*4K\s*[\)\]]",
        r"\s*[\(\[]\s*M/?V\s*[\)\]]",
        r"\s*[\(\[]\s*Live\s*[\)\]]",
    ]
    result = title
    for pat in noise_patterns:
        result = re.sub(pat, "", result, flags=re.IGNORECASE)
    # Also strip trailing "| Official …" pipes
    result = re.sub(r"\s*\|\s*Official.*$", "", result, flags=re.IGNORECASE)
    return result.strip()


# ── SoundCloud ──────────────────────────────────────────────────────

def _resolve_soundcloud_slug(match: re.Match) -> Optional[str]:
    """Extract a search query from a SoundCloud artist/track slug URL."""
    user_slug = match.group(1)
    track_slug = match.group(2)

    # Skip non-track pages
    if track_slug in ("sets", "likes", "followers", "following",
                       "reposts", "tracks", "albums", "popular-tracks"):
        return None

    # Convert hyphens to spaces for a usable search query
    artist = unquote(user_slug).replace("-", " ")
    track = unquote(track_slug).replace("-", " ")
    query = f"{track} {artist}"
    logger.info("Resolved SoundCloud URL to: %s", query)
    return query


async def _resolve_soundcloud_short(
    url: str, session: aiohttp.ClientSession
) -> Optional[str]:
    """Resolve a SoundCloud short link (on.soundcloud.com) by following the redirect."""
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            allow_redirects=False,
        ) as resp:
            location = resp.headers.get("Location", "")
            if location:
                # The redirect target is a normal soundcloud.com URL — recurse
                match = SOUNDCLOUD_PATTERN.search(location)
                if match:
                    return _resolve_soundcloud_slug(match)
                # Maybe it redirected to a full URL we can parse differently
                logger.info("SoundCloud short link redirected to: %s", location)

    except Exception as exc:
        logger.warning("SoundCloud short URL resolution failed: %s", exc)

    return None


# ── Tidal ───────────────────────────────────────────────────────────

async def _resolve_tidal(
    track_id: str, session: aiohttp.ClientSession
) -> Optional[str]:
    """Resolve a Tidal track URL via their oEmbed endpoint."""
    try:
        oembed_url = (
            f"https://oembed.tidal.com/"
            f"?url=https://tidal.com/browse/track/{track_id}"
        )
        async with session.get(
            oembed_url,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.warning("Tidal oEmbed returned %d for track %s", resp.status, track_id)
                # Fallback: can't resolve, just return None
                return None

            data = await resp.json(content_type=None)
            title = data.get("title", "")
            if title:
                logger.info("Resolved Tidal URL to: %s", title)
                return title

    except Exception as exc:
        logger.warning("Tidal URL resolution failed: %s", exc)

    return None
