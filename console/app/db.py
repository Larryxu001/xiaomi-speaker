"""SQLite access for the XiaoAI console.

Kept intentionally simple: one connection per request, plain SQL, no ORM.
Everything the console remembers (settings, scheduled tasks, the pending
announcement queue, and the speaker's last check-in) lives in this one file
so the whole console can be backed up by copying ``data/console.db``.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_DIR = Path(os.environ.get("CONSOLE_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "console.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL,              -- 'announcement' | 'news_analysis'
    recurrence TEXT NOT NULL,        -- 'once' | 'daily' | 'weekdays' | 'weekly'
    run_at TEXT NOT NULL,            -- 'HH:MM' for recurring, ISO datetime for 'once'
    weekday INTEGER,                 -- 0=Mon..6=Sun, only for 'weekly'
    payload TEXT NOT NULL DEFAULT '',-- announcement text, or news topic/prompt
    last_run_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,              -- 'newsnow' | 'rss' | 'web'
    target TEXT NOT NULL,            -- NewsNow id, or URL
    follow TEXT NOT NULL DEFAULT '', -- web: open the article whose title contains this
    max_items INTEGER NOT NULL DEFAULT 10,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS music_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,              -- 'library' | 'radio' | 'custom'
    config TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 50,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '其他',
    source TEXT NOT NULL DEFAULT '手动',   -- '对话' (learned) | '语音' | '手动'
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_text TEXT NOT NULL,
    assistant_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS radio_favorites (
    station_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS radio_history (
    station_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    plays INTEGER NOT NULL DEFAULT 0,
    skips INTEGER NOT NULL DEFAULT 0,
    last_played TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS model_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,       -- what you call it by voice, e.g. "DeepSeek"
    protocol TEXT NOT NULL,
    base_url TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS asr_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,       -- what you call it by voice, e.g. "DeepSeek"
    base_url TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mcp_servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,       -- [a-z0-9_-], shown to the model
    url TEXT NOT NULL,               -- ".../sse" = legacy SSE, else streamable HTTP
    token TEXT NOT NULL DEFAULT '',  -- sent as "Authorization: Bearer <token>"
    tools TEXT NOT NULL DEFAULT '',  -- comma-separated allowlist; empty = all
    timeout_s REAL NOT NULL DEFAULT 10,
    enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS device_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_seen_at TEXT,
    last_report TEXT
);
"""

DEFAULTS = {
    # Web UI login (plain password stored only as a salted hash; see auth.py).
    "admin_password_hash": "",
    "session_secret": secrets.token_hex(32),
    # Shared secret the speaker's poller script sends back; generated once.
    "device_token": secrets.token_hex(24),
    # LLM (dialogue model) — OpenAI-chat-compatible or Anthropic-compatible.
    "llm_protocol": "open_ai_chat",
    "llm_base_url": "https://api.deepseek.com/v1",
    "llm_api_key": "",
    "llm_model": "deepseek-chat",
    "llm_max_tokens": "400",
    "llm_temperature": "0.5",
    # ASR (speech-to-text), OpenAI /audio/transcriptions compatible.
    "asr_base_url": "https://api.siliconflow.cn/v1",
    "asr_api_key": "",
    "asr_model": "FunAudioLLM/SenseVoiceSmall",
    # Optional web search used by scheduled "news analysis" tasks and by the
    # speaker's own web_search tool (Tavily-compatible).
    "web_search_enabled": "0",
    "search_provider": "tavily",       # scheduled news: 'tavily' | 'firecrawl'
    "web_search_api_key": "",          # Tavily
    "firecrawl_api_key": "",
    # Speaker voice: 'native' (XiaoAI cloud TTS) or a voice id from tts.VOICES.
    "tts_voice": "native",
    # Backups, tried in order when the primary fails: model profile ids, and
    # a JSON list of {"name","base_url","api_key","model"} transcription services.
    "llm_fallback_profiles": "",
    "asr_fallbacks": "[]",
    "tts_rate": "normal",              # 'slow' | 'normal' | 'fast'
    "barge_in": "1",                   # talking over a reply interrupts it
    "memory_enabled": "1",             # learn about the user from conversations
    "listen_seconds": "15",            # how long the speaker waits for the next sentence
    "tts_api_key": "",                 # optional SiliconFlow key for CosyVoice; defaults to the ASR key
    # Free-text notes the user wants scheduled tasks to take into account
    # ("about me"): interests, routine, things to always mention.
    "profile_notes": "",
    "timezone": "Asia/Shanghai",
}


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULTS.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (key, value),
            )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        if "source_ids" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN source_ids TEXT NOT NULL DEFAULT ''")
        if "length" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN length TEXT NOT NULL DEFAULT 'standard'")
        conn.execute(
            "INSERT INTO device_status (id, last_seen_at, last_report) "
            "VALUES (1, NULL, NULL) ON CONFLICT(id) DO NOTHING"
        )
        conn.commit()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def local_tz(settings: dict[str, str] | None = None) -> ZoneInfo:
    tz_name = (settings or get_settings()).get("timezone") or "Asia/Shanghai"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def now_local(settings: dict[str, str] | None = None) -> datetime:
    """Timezone-aware 'now' in the console's configured zone.

    The container clock is UTC; storing naive datetime.now() values and later
    comparing them with zone-aware times made recurring tasks fire twice.
    Every timestamp the console stores goes through this function.
    """
    return datetime.now(local_tz(settings))


def get_settings() -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}


def set_settings(values: dict[str, str]) -> None:
    with get_conn() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        conn.commit()
