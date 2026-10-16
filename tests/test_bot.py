"""Smoke test for the bot wiring in ``bot/bot.py`` (``create_application``).

This is the wiring smoke test documented as pending in ``docs/development.md``
("Pendiente: smoke test del wiring del bot (bot/bot.py)"). It is a pure
offline unit test: it calls ``create_application()`` with test settings and
verifies that every handler and scheduled job is registered correctly, so a
typo in a handler name or a mis-registered job is caught here instead of in
production.

No network, no real Telegram, no real AI: ``create_application`` only builds
the PTB ``Application`` object and instantiates the ``AsyncOpenAI`` client
(no request is made at construction). The autouse ``isolated_data_dir``
fixture (tests/conftest.py) keeps any DB writes out of the real data dir.
"""

import dataclasses
from datetime import timedelta

import pytest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
)

import bot.bot as bot_wiring
from bot import proactive


def make_settings(sample_settings, **overrides):
    """Return a Settings copy with the given fields overridden."""
    return dataclasses.replace(sample_settings, **overrides)


def _handler_commands(app) -> set[str]:
    """Collect every command name registered on the application."""
    commands: set[str] = set()
    for handler in app.handlers[0]:
        if isinstance(handler, CommandHandler):
            commands.update(handler.commands)
    return commands


def _callback_patterns(app) -> set[str]:
    """Collect every CallbackQueryHandler pattern (as raw string) on the app."""
    patterns: set[str] = set()
    for handler in app.handlers[0]:
        if isinstance(handler, CallbackQueryHandler):
            patterns.add(handler.pattern.pattern)
    return patterns


def _message_handler_filters(app) -> list:
    """Return the filters of every MessageHandler registered on the app."""
    return [
        handler.filters
        for handler in app.handlers[0]
        if isinstance(handler, MessageHandler)
    ]


def _jobs_by_name(app) -> dict:
    """Map job name -> job for every scheduled job on the application."""
    return {job.name: job for job in app.job_queue.jobs()}


def _interval_seconds(job) -> int:
    """Return the interval (in seconds) of an interval-triggered job."""
    return int(job.trigger.interval.total_seconds())


def _cron_hour_minute(job) -> tuple[int, int]:
    """Return (hour, minute) of a cron-triggered (daily) job."""
    hour = int(job.trigger.fields[5].expressions[0].first)
    minute = int(job.trigger.fields[6].expressions[0].first)
    return hour, minute


class TestHandlerWiring:
    """create_application registers every expected handler."""

    def test_all_command_handlers_registered(self, sample_settings, monkeypatch):
        monkeypatch.setenv("MEMORY_ENABLED", "false")
        monkeypatch.setenv("PROACTIVE_ENABLED", "false")
        app = bot_wiring.create_application()

        commands = _handler_commands(app)
        expected = {
            "start", "iniciar", "ayuda", "info", "reiniciar", "dolar",
            "proyectos", "nuevoproyecto", "proyecto", "estadoproyecto",
            "prioridadproyecto", "agregarnota", "buscarproyecto",
            "eliminarproyecto", "compartir", "quitaracceso",
            "recordar", "recordatorios", "cancelarrecordatorio",
            "editarrecordatorio", "posponerrecordatorio",
            "reprogramarrecordatorio", "imagina",
        }
        assert expected <= commands

    def test_callback_query_handlers_registered(self, sample_settings, monkeypatch):
        monkeypatch.setenv("MEMORY_ENABLED", "false")
        monkeypatch.setenv("PROACTIVE_ENABLED", "false")
        app = bot_wiring.create_application()

        patterns = _callback_patterns(app)
        assert r"^remind_(cancel|postpone):\d+$" in patterns
        assert r"^delproj(_cancel)?:\d+$" in patterns

    def test_message_handlers_registered(self, sample_settings, monkeypatch):
        monkeypatch.setenv("MEMORY_ENABLED", "false")
        monkeypatch.setenv("PROACTIVE_ENABLED", "false")
        app = bot_wiring.create_application()

        # Text (non-command) for private + group, voice (private), and photo (private) handlers.
        assert len(_message_handler_filters(app)) == 4


class TestBotData:
    """create_application populates the shared bot_data."""

    def test_bot_data_populated(self, sample_settings, monkeypatch):
        monkeypatch.setenv("MEMORY_ENABLED", "false")
        monkeypatch.setenv("PROACTIVE_ENABLED", "false")
        app = bot_wiring.create_application()

        settings = app.bot_data["settings"]
        # create_application loads its own Settings from env; compare fields.
        assert settings.bot_token == sample_settings.bot_token
        assert settings.ai_provider == sample_settings.ai_provider
        assert app.bot_data["provider"] == settings.ai_provider
        assert app.bot_data["ai_client"] is not None


class TestJobWiring:
    """create_application registers the scheduled jobs with correct config."""

    def test_base_jobs_registered(self, sample_settings, monkeypatch):
        monkeypatch.setenv("MEMORY_ENABLED", "false")
        monkeypatch.setenv("PROACTIVE_ENABLED", "false")
        app = bot_wiring.create_application()

        jobs = _jobs_by_name(app)
        assert _interval_seconds(jobs["check_due_reminders"]) == 20
        # No memory jobs when MEMORY_ENABLED=false.
        assert not any(name.startswith("memory_") for name in jobs)

    def test_memory_jobs_registered_when_enabled(self, sample_settings, monkeypatch):
        monkeypatch.setenv("MEMORY_ENABLED", "true")
        monkeypatch.setenv("PROACTIVE_ENABLED", "false")
        app = bot_wiring.create_application()

        jobs = _jobs_by_name(app)
        assert _cron_hour_minute(jobs["memory_nightly_compaction"]) == (0, 15)
        assert _cron_hour_minute(jobs["memory_weekly_summaries"]) == (0, 30)
        assert _cron_hour_minute(jobs["memory_quarterly_summaries"]) == (0, 45)
        assert _cron_hour_minute(jobs["memory_yearly_summaries"]) == (1, 0)
        # Check-in driver requires PROACTIVE_ENABLED, so it must be absent.
        assert "memory_checkins" not in jobs

    def test_checkins_job_registered_when_proactive_enabled(
        self, sample_settings, monkeypatch
    ):
        monkeypatch.setenv("MEMORY_ENABLED", "true")
        monkeypatch.setenv("PROACTIVE_ENABLED", "true")
        app = bot_wiring.create_application()

        jobs = _jobs_by_name(app)
        checkins = jobs["memory_checkins"]
        assert _interval_seconds(checkins) == 900
        assert checkins.callback is proactive.run_checkins
