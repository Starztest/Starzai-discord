# ✨ Starzai Discord Bot

A production-ready AI-powered Discord bot built with Python and `discord.py`, designed for deployment on Railway.

## 🚀 Features

### 💬 Core AI Chat
- `/chat` — Send a message to Starzai AI (streaming responses)
- `/ask` — Ask a question with a specific model
- `/say` — Continue an active conversation with memory
- `/conversation start` — Start a persistent conversation
- `/conversation end` — End your current conversation
- `/conversation clear` — Clear conversation history
- `/conversation export` — Export conversation as a text file
- `/set-model` — Set your preferred AI model
- `/models` — List available AI models

### 🌐 Translator
- `/translate` — Translate text between 24+ languages
- `/detect-language` — Auto-detect language from text

### 📜 Etymology
- `/etymology` — Discover word origins and roots
- `/word-history` — Explore a word's historical timeline

### ✏️ Grammar & Writing
- `/check-grammar` — Check text for grammar/spelling errors
- `/improve-text` — Rewrite text in a specific style (formal, casual, academic, etc.)

### 🔮 Astrology
- `/horoscope` — Get daily/weekly/monthly horoscopes
- `/birth-chart` — Get a personalized birth chart reading

### 🧠 Personality Analysis
- `/analyze-personality` — Analyze personality traits from text

### 📄 File Analysis
- `/analyze-file` — Deep analysis of uploaded files
- `/summarize-file` — Quick summary of file contents

### 🎮 Games
- `/trivia` — Category-based trivia questions
- `/word-game` — Fun word puzzles and challenges
- `/riddle` — Brain-teasing riddles

### 🔧 Admin (Owner Only)
- `/reload` — Hot-reload a cog
- `/stats` — View bot statistics
- `/sync` — Sync slash commands
- `/shutdown` — Graceful shutdown
- `/usage` — Personal usage statistics (available to all)

---

## 🛠️ Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11+ |
| Discord Library | discord.py 2.3+ |
| AI API | MegaLLM (`https://ai.megallm.io/v1`) |
| Database | SQLite (async via aiosqlite) |
| Rate Limiting | In-memory (cachetools) |
| Deployment | Railway |

---

## 📁 Project Structure

```
├── bot.py              # Main entry point
├── Procfile            # Railway deployment
├── requirements.txt    # Python dependencies
├── .env.example        # Environment variable template
├── config/
│   ├── settings.py     # Environment config loader
│   └── constants.py    # Bot-wide constants
├── cogs/
│   ├── chat.py         # Core LLM chat
│   ├── translator.py   # Translation
│   ├── etymology.py    # Word origins
│   ├── grammar.py      # Grammar checking
│   ├── astrology.py    # Horoscopes & charts
│   ├── personality.py  # Personality analysis
│   ├── files.py        # File processing
│   ├── games.py        # Trivia & games
│   └── admin.py        # Owner commands
├── utils/
│   ├── llm_client.py   # MegaLLM API wrapper
│   ├── embedder.py     # Discord embed builder
│   ├── rate_limiter.py # Multi-level rate limiting
│   ├── db_manager.py   # SQLite database handler
│   └── file_handler.py # File processing
├── models/
│   └── schema.sql      # Database schema reference
└── tests/
    ├── test_llm_client.py
    ├── test_rate_limiter.py
    └── test_db_manager.py
```

---

## ⚡ Quick Start

### 1. Clone & Install
```bash
git clone https://github.com/Lemonsupqt/Starzai-discord.git
cd Starzai-discord
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env with your actual values
```

**Required variables:**
- `DISCORD_TOKEN` — Your Discord bot token
- `MEGALLM_API_KEY` — Your MegaLLM API key

### 3. Run Locally
```bash
python bot.py
```

---

## 🚂 Railway Deployment

### 1. Create a Railway Project
- Go to [railway.app](https://railway.app)
- Create a new project from this GitHub repo

### 2. Set Environment Variables
In the Railway dashboard, add all variables from `.env.example`:
- `DISCORD_TOKEN`
- `MEGALLM_API_KEY`
- `MEGALLM_BASE_URL`
- `AVAILABLE_MODELS`
- `DEFAULT_MODEL`
- `OWNER_IDS`
- `PORT` (Railway sets this automatically)

### 3. Add a Persistent Volume (Recommended)
For SQLite data persistence:
- Add a volume in Railway dashboard
- Mount it at `/data`
- Set `DB_PATH=/data/starzai.db` in environment variables

### 4. Deploy
Railway will auto-detect the `Procfile` and run `python bot.py`.

### Health Check
The bot runs an HTTP health endpoint on the configured `PORT`:
- `GET /` or `GET /health` returns bot status as JSON

---

## ⏱️ Rate Limiting

The bot uses a multi-level rate limiting system:

| Level | Default Limit | Scope |
|-------|--------------|-------|
| Per-User | 10 req/min | General commands |
| Expensive | 5 req/min | AI commands (`/chat`, `/analyze-file`, etc.) |
| Per-Server | 100 req/min | All commands in a guild |
| Global | 200 req/min | Entire bot |
| Daily Tokens (User) | 50,000 | Per user per day |
| Daily Tokens (Server) | 500,000 | Per server per day |

---

## 🧪 Running Tests

```bash
python -m unittest discover -s tests -v
```

---

## 📜 License

MIT

Lemon
