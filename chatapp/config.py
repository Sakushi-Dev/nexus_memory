"""Settings — environment variables and CLI flags, nothing else.

This module is deliberately dependency-free (no Nexus, no LLM, no rich): it only
resolves *what the user asked for* into a plain :class:`Settings` value object.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# nexus-chat-demo/ — the project root (this file lives in chatapp/).
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "chat_memory.db"

SYSTEM_PROMPT = (
    "You are a helpful, friendly assistant with persistent long-term memory about "
    "the user. Before each reply you are given a <memory_context> block retrieved "
    "from that memory. Use any relevant facts naturally, as if you simply remember "
    "them — do not mention the memory mechanism or the XML. If the block is empty, "
    "you have nothing stored about the topic yet; just answer normally."
)

# Supported reply languages: code -> human-readable label (for /lang + the footer).
LANGUAGES = {
    "en": "English",
    "de": "German (Deutsch)",
}

# Accept a few friendly spellings (from env or /lang) and fold them onto a code.
_LANG_ALIASES = {
    "en": "en", "english": "en", "englisch": "en",
    "de": "de", "german": "de", "deutsch": "de",
}

# The system-prompt line that PINS the reply language. It deliberately repeats
# "regardless of the language of these instructions / stored notes" because the
# base prompt and the recalled facts are English-labelled — without this the model
# drifts between German and English. The directive is appended LAST so recency
# makes it win over the English text above it.
_LANGUAGE_DIRECTIVE = {
    "en": "Always write your replies in English, regardless of the language used "
          "in these instructions or in any stored memory facts.",
    "de": "Antworte ausschließlich auf Deutsch – unabhängig davon, in welcher "
          "Sprache diese Anweisungen oder gespeicherte Notizen verfasst sind.",
}


def resolve_language(value: str | None) -> str | None:
    """Map a user/env language token onto a supported code, or None if unknown."""
    if not value:
        return None
    return _LANG_ALIASES.get(value.strip().lower())


def language_directive(code: str) -> str:
    """The system-prompt line that pins the reply language (defaults to English)."""
    return _LANGUAGE_DIRECTIVE.get(code, _LANGUAGE_DIRECTIVE["en"])


@dataclass(frozen=True)
class Settings:
    """Resolved demo configuration (immutable)."""

    model: str          # primary model — the chat response (streamed)
    aux_model: str      # secondary model — side tasks (memory summaries + diary)
    base_url: str
    api_key: str
    app_url: str
    app_title: str
    db_path: str
    trace_on: bool
    diary_on: bool
    token_window: int
    language: str      # reply language code ("en" default, "de" available)
    embedder_backend: str  # "hashing" (default) | "fastembed" (0.7.0 local semantic embedder)


def trace_on() -> bool:
    """Trace X-ray is on by default; ``--notrace`` starts quiet."""
    return "--notrace" not in sys.argv


def diary_on() -> bool:
    """Layer V diary is **on by default** in the live chat.

    Disable with ``--no-diary`` or ``NEXUS_DIARY=0`` (``--diary`` / ``NEXUS_DIARY=1``
    also force it on). The offline self-test enables it separately.
    """
    if "--no-diary" in sys.argv:
        return False
    env = os.getenv("NEXUS_DIARY", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    return True


def embedder_backend() -> str:
    """Embedder backend: ``--embedder <backend>`` CLI flag, else ``NEXUS_EMBEDDER``
    env, else ``"hashing"`` (the offline default).

    ``"fastembed"`` enables the 0.7.0 local semantic embedder (downloads
    BAAI/bge-base-en-v1.5 once, then runs offline). NOTE: switching the backend on
    an EXISTING store needs a re-index (``python -m nexus_memory.reindex``) — the
    store refuses to mix vector spaces.
    """
    argv = sys.argv
    if "--embedder" in argv:
        i = argv.index("--embedder")
        if i + 1 < len(argv):
            return argv[i + 1].strip().lower() or "hashing"
    return os.getenv("NEXUS_EMBEDDER", "hashing").strip().lower() or "hashing"


def _token_window() -> int:
    """Default token budget for the rolling context window (env-overridable)."""
    try:
        return max(256, int(os.getenv("NEXUS_TOKEN_WINDOW", "50000")))
    except ValueError:
        return 50000


def load_settings() -> Settings:
    """Load ``.env`` next to the demo and fold in the CLI flags."""
    load_dotenv(ROOT / ".env")
    DATA_DIR.mkdir(exist_ok=True)
    model = os.getenv("OPENROUTER_MODEL", "google/gemini-3.5-flash").strip()
    return Settings(
        model=model,
        # secondary model for side tasks; falls back to the primary model when unset
        aux_model=os.getenv("OPENROUTER_AUX_MODEL", "").strip() or model,
        base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip(),
        api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        app_url=os.getenv("APP_URL", "http://localhost"),
        app_title=os.getenv("APP_TITLE", "Nexus Chat Demo"),
        db_path=str(DB_PATH),
        trace_on=trace_on(),
        diary_on=diary_on(),
        token_window=_token_window(),
        # Default English; NEXUS_LANG=de (or "deutsch"/"german") flips the default,
        # and the TUI's /lang switches it live without touching .env.
        language=resolve_language(os.getenv("NEXUS_LANG")) or "en",
        # Embedder backend: "hashing" (default, offline, lexical) or "fastembed"
        # (0.7.0 local semantic embedder). Set via --embedder <backend> or
        # NEXUS_EMBEDDER=fastembed.
        embedder_backend=embedder_backend(),
    )
