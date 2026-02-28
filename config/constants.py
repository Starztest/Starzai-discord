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
EXPENSIVE_COMMANDS = {"chat", "ask", "analyze-file", "summarize-file", "analyze-personality"}
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

