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
    )
