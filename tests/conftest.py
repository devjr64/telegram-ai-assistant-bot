"""Shared pytest fixtures.

Key design: the DB modules (history, media_usage, projects, reminders,
memory, proactive) resolve their DB_PATH / PROJECTS_DIR / MEMORY_DIR from
DATA_DIR at import time. Instead of relying on import order, fixtures
monkeypatch the module-level path constants to a per-test tmp directory, so
every test gets an isolated database that is automatically cleaned up.
"""

import os
import sys
from pathlib import Path

import pytest

# Ensure the project root is importable regardless of where pytest runs.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# bot.access calls load_settings() at import time (fail-secure). Provide
# dummy env vars BEFORE any bot module is imported so the test suite runs
# without a real .env (local or CI).
os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("OPENCODE_GO_API_KEY", "test-key")
os.environ.setdefault("AUTHORIZED_USERS", "")


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point every bot DB module at a fresh temp directory per test."""
    for module_name in (
        "bot.history",
        "bot.media_usage",
        "bot.projects",
        "bot.reminders",
        "bot.memory",
        "bot.proactive",
        "bot.entities",
    ):
        module = pytest.importorskip(module_name)
        monkeypatch.setattr(module, "DB_PATH", tmp_path / "data.db")
        monkeypatch.setattr(module, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("bot.projects.PROJECTS_DIR", tmp_path / "projects")
    # bot.memory keeps its Markdown journal under memory/{user_id}/; point it
    # at the isolated tmp dir so memory tests never touch a real user journal.
    monkeypatch.setattr("bot.memory.MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level mutable state between tests."""
    import bot.dolar as dolar

    dolar._cache = {"ts": 0.0, "data": None}

    import bot.handlers as handlers

    handlers._rate_limit_tracker.clear()
    yield


@pytest.fixture
def sample_settings(monkeypatch):
    """A Settings instance with test-safe values."""
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "test-opencode-key")
    monkeypatch.setenv("AUTHORIZED_USERS", "111,222")
    from bot.config import load_settings

    return load_settings()
