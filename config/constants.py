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

# ── Dodo Todo System ────────────────────────────────────────────────
DODO_COOK_TIMES = {"red": 60, "yellow": 45, "green": 20}   # minutes
DODO_XP_VALUES = {"red": 30, "yellow": 20, "green": 10}    # base XP per check
DODO_MAX_ACTIVE = {"red": 3, "yellow": 10, "green": 999}   # max active tasks
DODO_RED_MAX_TIMER_HOURS = 12
DODO_RED_EXPIRE_PENALTY = 2                                 # XP deducted
DODO_DAILY_XP_CAP = 10                                      # max tasks counting toward XP/day
DODO_MIN_SERVER_AGE_DAYS = 7                                # days before user can use bot
DODO_STREAK_DECAY = 0.5                                     # lose 50% on missed day
DODO_STREAK_MULTIPLIER_CAP = 3.0
DODO_STREAK_MILESTONES = [7, 14, 30, 60, 100]

DODO_STRIKE_RULES = {
    1: "funny",     # public callout — funny
    2: "serious",   # public callout — serious warning
    3: "half_xp",   # half XP for the day
}
# 4+ → zero XP
DODO_STRIKE_COLORS = {
    1: 0xF1C40F,    # yellow
    2: 0xE67E22,    # orange
    3: 0xE74C3C,    # red
    4: 0x000000,    # black
}

DODO_BSD_CHARACTERS = [
    {"name": "Atsushi 🐯",      "tone": "Gentle and encouraging",       "color": 0xFFD700},
    {"name": "Dazai 🎭",        "tone": "Dramatic and teasing",         "color": 0x8B4513},
    {"name": "Chuuya 🍷",       "tone": "Annoyed and sarcastic",        "color": 0xE74C3C},
    {"name": "Akutagawa 🖤",    "tone": "Cold and threatening",         "color": 0x000000},
    {"name": "Dostoyevsky 🕊️",  "tone": "Reflecting and cryptic",       "color": 0x4B0082},
    {"name": "Sigma 📋",        "tone": "Awkward and trying his best",  "color": 0x808080},
]

DODO_PRIORITY_EMOJIS = {"red": "🔴", "yellow": "🟡", "green": "🟢"}
