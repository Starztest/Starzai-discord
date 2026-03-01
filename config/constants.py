"""
Bot-wide constants and default values.
"""

# ── Bot Identity ─────────────────────────────────────────────────────
BOT_NAME = "Starzai"
BOT_COLOR = 0x9B59B6  # Purple theme
BOT_ERROR_COLOR = 0xE74C3C  # Red
BOT_SUCCESS_COLOR = 0x2ECC71  # Green
BOT_INFO_COLOR = 0x3498DB  # Blue
BOT_WARN_COLOR = 0xF39C12  # Orange

# ── Conversation ─────────────────────────────────────────────────────
MAX_CONVERSATION_MESSAGES = 10  # 5 user + 5 AI
MAX_MESSAGE_LENGTH = 2000  # Discord limit
MAX_CONTEXT_CHARS = 4000  # Truncation limit per message in context
STREAMING_EDIT_INTERVAL = 1.5  # Seconds between edits during streaming

# ── Rate Limiting ────────────────────────────────────────────────────
EXPENSIVE_COMMANDS = {"chat", "ask", "analyze-file", "summarize-file", "analyze-personality", "search", "news"}
EXPENSIVE_RATE_LIMIT = 5  # per minute for expensive commands
GENERAL_RATE_LIMIT = 10  # per minute for general commands

# ── API ──────────────────────────────────────────────────────────────
API_TIMEOUT = 30  # seconds
API_MAX_RETRIES = 3
API_RETRY_BASE_DELAY = 1.0  # seconds, exponential backoff base

# ── File Processing ──────────────────────────────────────────────────
MAX_FILE_CONTENT_CHARS = 10000  # Max chars to extract from files

# ── Games ────────────────────────────────────────────────────────────
TRIVIA_CATEGORIES = [
    "science", "history", "geography", "entertainment",
    "sports", "art", "technology", "nature",
]

# ── Astrology ────────────────────────────────────────────────────────
ZODIAC_SIGNS = [
    "aries", "taurus", "gemini", "cancer",
    "leo", "virgo", "libra", "scorpio",
    "sagittarius", "capricorn", "aquarius", "pisces",
]

ZODIAC_EMOJIS = {
    "aries": "♈", "taurus": "♉", "gemini": "♊", "cancer": "♋",
    "leo": "♌", "virgo": "♍", "libra": "♎", "scorpio": "♏",
    "sagittarius": "♐", "capricorn": "♑", "aquarius": "♒", "pisces": "♓",
}

# ── Text Styles ──────────────────────────────────────────────────────
TEXT_STYLES = ["formal", "casual", "academic", "creative", "concise", "professional"]

# ── Music ────────────────────────────────────────────────────────────
DISCORD_UPLOAD_FALLBACK = 25 * 1024 * 1024  # 25 MB — DM / no-boost limit
MAX_DOWNLOAD_SIZE = 200 * 1024 * 1024       # 200 MB max download buffer
MIN_BITRATE_KBPS = 64                       # floor — below this quality is unacceptable
MAX_SELECT_OPTIONS = 25                     # Discord select menu cap
MAX_FILENAME_LEN = 100                      # max chars for sanitised filenames
MUSIC_VIEW_TIMEOUT = 60                     # seconds for interactive views
VC_IDLE_TIMEOUT = 300                       # 5 min idle before auto-disconnect
NP_UPDATE_INTERVAL = 2                      # seconds between live progress-bar edits
MAX_HISTORY = 50                            # cap on recently-played songs
MAX_ENCODER_BITRATE = 512_000               # 512 kbps — discord.py Opus ceiling
BRAND = "Powered by StarzAI \u26a1"

# ── Web Search ────────────────────────────────────────────────
WEB_SEARCH_MAX_RESULTS = 5          # max results per search query
WEB_SEARCH_NEWS_MAX_RESULTS = 8     # more results for news queries
WEB_SEARCH_CACHE_TTL = 300          # 5-minute cache for repeated queries
WEB_SEARCH_TIMEOUT = 15             # seconds timeout for web search
WEB_SEARCH_MAX_SNIPPET_CHARS = 500  # max chars per snippet for LLM context

# ── Auto-News ────────────────────────────────────────────────────
AUTO_NEWS_CHECK_INTERVAL = 5        # minutes between background task ticks
AUTO_NEWS_MIN_INTERVAL = 15         # minimum user-configurable interval (mins)
AUTO_NEWS_MAX_INTERVAL = 1440       # maximum interval (24 hours)
AUTO_NEWS_DEFAULT_INTERVAL = 30     # default interval (30 minutes)
AUTO_NEWS_MAX_GUILDS_PER_TICK = 5   # rate limit: max guilds processed per tick

# ── Music Premium ────────────────────────────────────────────────────
MAX_PLAYLISTS_PER_USER = 25
MAX_SONGS_PER_PLAYLIST = 200
MAX_FAVORITES = 500
PREMIUM_VIEW_TIMEOUT = 120                  # seconds for premium interactive views
