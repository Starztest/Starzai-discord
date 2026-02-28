"""
Music API wrapper with multi-provider cascading fallback.

Primary provider: JioSaavn (multiple API mirrors).
Fallback providers: YouTube and SoundCloud (via yt-dlp).

When JioSaavn returns no results for a query the search
automatically cascades to YouTube, then SoundCloud.  All
results are normalised into a common song-dict format so
downstream code (playback, queue, embeds, download) is
provider-agnostic.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

# ── Internal API endpoints (NEVER expose to users) ──────────────────
MUSIC_APIS: List[str] = [
    "https://jiosaavn-api2.vercel.app",
    "https://jiosaavn-api-privatecvc2.vercel.app",
    "https://saavn.dev/api",
    "https://jiosaavn-api.vercel.app",
]

API_TIMEOUT = 30  # seconds per endpoint

QUALITY_TIERS: List[str] = ["12kbps", "48kbps", "96kbps", "160kbps", "320kbps"]

# Qualities we offer for download (user-facing)
DOWNLOAD_QUALITIES: List[str] = ["96kbps", "160kbps", "320kbps"]


def _safe_unescape(text: Any) -> str:
    """HTML-unescape a value, returning empty string for non-strings."""
    if not text:
        return ""
    if not isinstance(text, str):
        return str(text)
    return html.unescape(text)


def _extract_url(entry: Dict[str, Any]) -> str:
    """Extract URL from a download/image entry that may use 'url' or 'link' key."""
    return entry.get("url") or entry.get("link") or ""


def _extract_artist(song: Dict[str, Any]) -> str:
    """Extract artist string from either response format."""
    # Format A: primaryArtists is a plain string
    if isinstance(song.get("primaryArtists"), str) and song["primaryArtists"]:
        return _safe_unescape(song["primaryArtists"])

    # Format A: primaryArtists could also be a list
    if isinstance(song.get("primaryArtists"), list):
        names = [a.get("name", "") for a in song["primaryArtists"] if isinstance(a, dict)]
        if names:
            return _safe_unescape(", ".join(n for n in names if n))

    # Format B: artists.primary is an array of objects
    artists_obj = song.get("artists")
    if isinstance(artists_obj, dict):
        primary = artists_obj.get("primary")
        if isinstance(primary, list) and primary:
            names = [a.get("name", "") for a in primary if isinstance(a, dict)]
            joined = ", ".join(n for n in names if n)
            if joined:
                return _safe_unescape(joined)

    # Fallback: try 'artist' key or featured artists
    if song.get("artist"):
        return _safe_unescape(song["artist"])

    return "Unknown"


def _extract_image(song: Dict[str, Any]) -> str:
    """Extract the best quality image URL (prefer 500x500)."""
    images = song.get("image")
    if not images or not isinstance(images, list):
        return ""

    # Try to find 500x500
    for img in images:
        if isinstance(img, dict) and img.get("quality") == "500x500":
            return _extract_url(img)

    # Fall back to the last (highest quality) entry
    if images and isinstance(images[-1], dict):
        return _extract_url(images[-1])

    return ""


def _extract_download_urls(song: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract and normalise the download URL array."""
    raw = song.get("downloadUrl") or song.get("download_url") or []
    if not isinstance(raw, list):
        return []

    result: List[Dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        quality = entry.get("quality", "")
        url = _extract_url(entry)
        if quality and url:
            result.append({"quality": quality, "url": url})
    return result


def _pick_best_url(download_urls: List[Dict[str, str]], preferred: str = "320kbps") -> str:
    """Pick the best available download URL by quality preference."""
    if not download_urls:
        return ""

    # Preference cascade
    preference_order = [preferred]
    # Build fallback chain
    if preferred in QUALITY_TIERS:
        idx = QUALITY_TIERS.index(preferred)
        # Add lower qualities as fallback
        for i in range(idx - 1, -1, -1):
            preference_order.append(QUALITY_TIERS[i])

    url_map = {d["quality"]: d["url"] for d in download_urls}

    for q in preference_order:
        if q in url_map:
            return url_map[q]

    # Absolute fallback: highest available
    for q in reversed(QUALITY_TIERS):
        if q in url_map:
            return url_map[q]

    # Last resort: last entry
    return download_urls[-1]["url"] if download_urls else ""


def _get_url_for_quality(download_urls: List[Dict[str, str]], quality: str) -> str:
    """Get URL for a specific quality tier, with fallback."""
    url_map = {d["quality"]: d["url"] for d in download_urls}
    if quality in url_map:
        return url_map[quality]
    return _pick_best_url(download_urls, quality)


def _format_duration(seconds: Any) -> str:
    """Convert seconds to M:SS format."""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "0:00"
    minutes = total // 60
    secs = total % 60
    return f"{minutes}:{secs:02d}"


def normalize_song(song: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a single song object from any endpoint format."""
    download_urls = _extract_download_urls(song)
    album_obj = song.get("album")
    album_name = ""
    if isinstance(album_obj, dict):
        album_name = album_obj.get("name", "")
    elif isinstance(album_obj, str):
        album_name = album_obj

    duration_raw = song.get("duration", 0)
    try:
        duration_sec = int(duration_raw)
    except (TypeError, ValueError):
        duration_sec = 0

    return {
        "id": song.get("id", ""),
        "name": _safe_unescape(song.get("name", "Unknown")),
        "artist": _extract_artist(song),
        "album": _safe_unescape(album_name),
        "year": str(song["year"]) if song.get("year") is not None else "",
        "duration": duration_sec,
        "duration_formatted": _format_duration(duration_sec),
        "language": song.get("language", ""),
        "has_lyrics": bool(song.get("hasLyrics")),
        "image": _extract_image(song),
        "download_urls": download_urls,
        "best_url": _pick_best_url(download_urls),
    }


def normalize_songs(songs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalise a list of song objects."""
    return [normalize_song(s) for s in songs if isinstance(s, dict)]


# ── Edit / remix detection for "official song" ranking ────────────────

# Phrases that strongly indicate a fan-made edit rather than the
# original release.  Each entry is compiled into a regex that matches
# as a whole phrase (word-boundary delimited) so we don't accidentally
# penalise songs whose *actual* title contains a substring (e.g. a
# song literally called "Reverb" won't be penalised by the "reverb"
# marker because we check against the query too).
_EDIT_MARKERS: List[str] = [
    r"slowed",
    r"reverb",
    r"sped\s*up",
    r"speed\s*up",
    r"nightcore",
    r"daycore",
    r"chopped",
    r"screwed",
    r"bass\s*boost(?:ed)?",
    r"8\s*d(?:\s*audio)?",
    r"lo[\-\s]?fi",
    r"anti[\-\s]?nightcore",
    r"pitched",
    r"chipmunk",
    r"deep\s*voice",
]

# Pre-compiled as one big alternation for speed
_EDIT_RE = re.compile(
    r"(?i)(?:"
    + "|".join(_EDIT_MARKERS)
    + r")",
)


def _has_edit_markers(text: str, *, ignore_markers_in: str = "") -> bool:
    """Return True if *text* contains edit-style markers.

    Any marker that also appears in *ignore_markers_in* is skipped so
    that an intentional search for "song slowed" still returns the
    slowed version.
    """
    text_lower = text.lower()
    ignore_lower = ignore_markers_in.lower()

    for marker in _EDIT_MARKERS:
        pat = re.compile(r"(?i)" + marker)
        if pat.search(text_lower) and not pat.search(ignore_lower):
            return True
    return False


def _edit_marker_count(text: str, *, ignore_markers_in: str = "") -> int:
    """Count how many distinct edit markers appear in *text*.

    Markers also present in *ignore_markers_in* are not counted.
    """
    text_lower = text.lower()
    ignore_lower = ignore_markers_in.lower()
    count = 0
    for marker in _EDIT_MARKERS:
        pat = re.compile(r"(?i)" + marker)
        if pat.search(text_lower) and not pat.search(ignore_lower):
            count += 1
    return count


def pick_best_match(
    results: List[Dict[str, Any]],
    query: str = "",
) -> Dict[str, Any]:
    """Pick the best 'official' song from a list of search results.

    Strongly penalises fan-made edits (slowed, reverb, nightcore, etc.)
    unless the *query* itself contains those words — in which case the
    user explicitly wants them.

    Falls back to ``results[0]`` when all scores are equal.
    """
    if not results:
        raise ValueError("results must be a non-empty list")
    if len(results) == 1:
        return results[0]

    query_lower = query.lower().strip()

    best_song = results[0]
    best_score = float("-inf")

    for song in results:
        score = 0.0
        name = (song.get("name") or "").lower()
        artist = (song.get("artist") or "").lower()
        full_text = f"{name} {artist}"

        # ── Heavy penalty for edit markers ────────────────────────
        marker_count = _edit_marker_count(full_text, ignore_markers_in=query)
        score -= marker_count * 15

        # ── Mild penalty for very long names (edits often append
        #    lots of parenthetical noise) ─────────────────────────
        if len(name) > 80:
            score -= 3

        # ── Bonus: the song name starts with the query text ──────
        if query_lower and name.startswith(query_lower):
            score += 5

        # ── Bonus: query words all appear in the song name ───────
        if query_lower:
            q_words = set(query_lower.split())
            n_words = set(name.split())
            if q_words and q_words <= n_words:
                score += 3

        if score > best_score:
            best_score = score
            best_song = song

    return best_song


class MusicAPI:
    """Async wrapper with cascading provider fallback.

    Search priority:
        1. JioSaavn (multiple mirrors)
        2. YouTube  (via yt-dlp)
        3. SoundCloud (via yt-dlp)
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    # ── Search ────────────────────────────────────────────────────────

    async def _search_jiosaavn(self, query: str, limit: int = 7) -> Optional[List[Dict[str, Any]]]:
        """Search JioSaavn across all mirrors.

        Returns normalised results, ``[]`` if endpoints responded but
        had no matches, or ``None`` if every mirror failed.
        """
        any_endpoint_succeeded = False

        for api_base in MUSIC_APIS:
            try:
                url = f"{api_base}/search/songs"
                async with self._session.get(
                    url,
                    params={"query": query, "limit": limit},
                    timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                ) as resp:
                    if resp.status != 200:
                        logger.warning("Music API %s returned status %d", api_base, resp.status)
                        continue

                    data = await resp.json(content_type=None)

                    # Handle nested data structures
                    results = None
                    if isinstance(data, dict):
                        # Try data.data.results (double-wrapped)
                        inner = data.get("data")
                        if isinstance(inner, dict):
                            results = inner.get("results")
                            # Some endpoints wrap again
                            if results is None:
                                inner2 = inner.get("data")
                                if isinstance(inner2, dict):
                                    results = inner2.get("results")
                        # Try data.results directly
                        if results is None:
                            results = data.get("results")

                    # Endpoint responded OK — mark as succeeded even if empty
                    if isinstance(results, list):
                        any_endpoint_succeeded = True
                        normalised = normalize_songs(results)
                        if normalised:
                            # Tag source so downstream knows the provider
                            for s in normalised:
                                s.setdefault("source", "jiosaavn")
                            logger.info(
                                "Music search '%s' returned %d results from %s",
                                query, len(normalised), api_base,
                            )
                            return normalised

            except asyncio.TimeoutError:
                logger.warning("Music API %s timed out for query '%s'", api_base, query)
            except Exception as exc:
                logger.warning("Music API %s error for query '%s': %s", api_base, query, exc)

        if any_endpoint_succeeded:
            logger.info("JioSaavn search '%s' returned no results", query)
            return []

        logger.error("All JioSaavn mirrors failed for query: %s", query)
        return None

    # ── Cascading search ──────────────────────────────────────────────

    async def search(self, query: str, limit: int = 7) -> Optional[List[Dict[str, Any]]]:
        """
        Search across **all providers** with automatic cascading fallback.

        Order: JioSaavn → YouTube → SoundCloud.

        Returns a normalised list of song dicts, ``[]`` if every
        provider responded but had no matches, or ``None`` only if
        the primary provider's mirrors all had network failures AND
        the fallback providers also failed.
        """
        # 1) JioSaavn (primary)
        results = await self._search_jiosaavn(query, limit=limit)
        if results:  # non-empty list
            return results

        # 2) YouTube fallback (via yt-dlp)
        try:
            from utils.ytdlp_provider import search_youtube
            yt_results = await search_youtube(query, limit=limit)
            if yt_results:
                logger.info(
                    "YouTube fallback returned %d results for '%s'",
                    len(yt_results), query,
                )
                return yt_results
        except Exception as exc:
            logger.warning("YouTube fallback failed for '%s': %s", query, exc)

        # 3) SoundCloud fallback (via yt-dlp)
        try:
            from utils.ytdlp_provider import search_soundcloud
            sc_results = await search_soundcloud(query, limit=limit)
            if sc_results:
                logger.info(
                    "SoundCloud fallback returned %d results for '%s'",
                    len(sc_results), query,
                )
                return sc_results
        except Exception as exc:
            logger.warning("SoundCloud fallback failed for '%s': %s", query, exc)

        # All providers exhausted
        if results is not None:
            # At least JioSaavn responded (just had no results)
            return []
        return None

    # ── Get song by ID ────────────────────────────────────────────────

    async def get_song_by_id(self, song_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch a single song by ID (fallback when search results lack download URLs).

        Returns a normalised song dict, or None if every endpoint fails.
        """
        for api_base in MUSIC_APIS:
            try:
                url = f"{api_base}/songs/{song_id}"
                async with self._session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                ) as resp:
                    if resp.status != 200:
                        continue

                    data = await resp.json(content_type=None)

                    song_data = None
                    if isinstance(data, dict):
                        inner = data.get("data")
                        if isinstance(inner, list) and inner:
                            song_data = inner[0]
                        elif isinstance(inner, dict):
                            # Could be the song itself or another wrapper
                            if inner.get("id"):
                                song_data = inner
                            else:
                                results = inner.get("results") or inner.get("data")
                                if isinstance(results, list) and results:
                                    song_data = results[0]
                                elif isinstance(results, dict) and results.get("id"):
                                    song_data = results
                        elif data.get("id"):
                            song_data = data

                    if song_data and isinstance(song_data, dict):
                        normalised = normalize_song(song_data)
                        if normalised.get("id"):
                            logger.info("Fetched song %s from %s", song_id, api_base)
                            return normalised

            except asyncio.TimeoutError:
                logger.warning("Music API %s timed out for song ID '%s'", api_base, song_id)
            except Exception as exc:
                logger.warning("Music API %s error for song ID '%s': %s", api_base, song_id, exc)

        logger.error("All music APIs failed for song ID: %s", song_id)
        return None

    # ── Ensure download URLs ──────────────────────────────────────────

    async def ensure_download_urls(self, song: Dict[str, Any]) -> Dict[str, Any]:
        """
        If a song object is missing download URLs, fetch them.

        For JioSaavn songs this calls ``get_song_by_id``.
        For YouTube / SoundCloud songs this re-extracts via yt-dlp
        (stream URLs from those platforms expire after a few hours).
        """
        source = song.get("source", "jiosaavn")

        # ── YouTube / SoundCloud: always re-extract (URLs expire) ────
        if source in ("youtube", "soundcloud") and song.get("webpage_url"):
            return await self.refresh_stream_url(song)

        # ── JioSaavn: fetch by ID if URLs are missing ────────────────
        if song.get("download_urls") and song.get("best_url"):
            return song

        song_id = song.get("id")
        if not song_id:
            return song

        logger.info("Song '%s' missing download URLs, fetching by ID", song.get("name", ""))
        full_song = await self.get_song_by_id(song_id)
        if full_song and full_song.get("download_urls"):
            song["download_urls"] = full_song["download_urls"]
            song["best_url"] = full_song["best_url"]

        return song

    # ── Stream URL refresh (YouTube / SoundCloud) ─────────────────────

    async def refresh_stream_url(self, song: Dict[str, Any]) -> Dict[str, Any]:
        """Re-extract a fresh audio URL for a yt-dlp sourced track.

        YouTube / SoundCloud stream URLs expire after a few hours.
        This method fetches a new one using the permanent
        ``webpage_url`` stored in the song dict.
        """
        try:
            from utils.ytdlp_provider import refresh_song
            song = await refresh_song(song)
        except Exception as exc:
            logger.warning(
                "Stream URL refresh failed for '%s': %s",
                song.get("name", ""), exc,
            )
        return song


__all__ = [
    "MusicAPI",
    "MUSIC_APIS",
    "QUALITY_TIERS",
    "DOWNLOAD_QUALITIES",
    "normalize_song",
    "normalize_songs",
    "pick_best_match",
    "_has_edit_markers",
    "_pick_best_url",
    "_get_url_for_quality",
    "_format_duration",
]
