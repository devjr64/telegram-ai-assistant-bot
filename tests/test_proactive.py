"""Unit tests for bot.proactive — proactive engine, caps, windows, jobs, delivery.

The proactive module reaches the chat model through ``bot.proactive.chat_completion``
and writes to the ``proactive_log`` table in the shared bot DB, so every model
call is replaced with an async recording stub and the autouse
``isolated_data_dir`` fixture (tests/conftest.py) points bot.proactive.DB_PATH
at a fresh tmp database per test (the same DB the reminders table lives in, as
in production). Memory/reminders are seeded through bot.memory / bot.reminders
exactly as the production drivers do; fixed timestamps and the fixed
"America/Caracas" timezone keep everything deterministic. No network, no real
tokens.
"""

import dataclasses
from datetime import datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from telegram.constants import ParseMode
from telegram.error import BadRequest

import bot.bot as bot_wiring
from bot import memory, proactive, reminders, summarizer

# Fixed timezone and instants (America/Caracas is UTC-4, no DST).
TZ = ZoneInfo("America/Caracas")
TZ_NAME = "America/Caracas"
DAY = "2026-08-14"
FIXED_NOW = datetime(2026, 8, 14, 8, 0, tzinfo=TZ)


class FakeChatCompletion:
    """Async stand-in for proactive/summarizer chat_completion.

    Records every prompt (so tests can assert what the model was asked) and
    can be configured to raise an API error on each call. The error classes
    come from the module's own namespace, matching what _generate_message
    and summarizer._call_model catch.
    """

    def __init__(self, content: str = "Mensaje de prueba"):
        self.content = content
        self.calls: list[str] = []
        self.error: Exception | None = None

    async def __call__(
        self,
        *,
        client=None,
        model: str = "",
        system_prompt: str = "",
        messages: list[dict] | None = None,
        session_id: str | None = None,
        provider: str = "opencode",
    ) -> str:
        self.calls.append(messages[0]["content"] if messages else "")
        if self.error is not None:
            raise self.error
        return self.content


@pytest.fixture
def fake_chat(monkeypatch) -> FakeChatCompletion:
    """Patch proactive.chat_completion with a recording stub."""
    fake = FakeChatCompletion()
    monkeypatch.setattr(proactive, "chat_completion", fake)
    return fake


@pytest.fixture
def fake_summarizer_chat(monkeypatch) -> FakeChatCompletion:
    """Patch summarizer.chat_completion with a recording stub."""
    fake = FakeChatCompletion(content="Resumen compacto")
    monkeypatch.setattr(summarizer, "chat_completion", fake)
    return fake


@pytest.fixture
def now_at(monkeypatch):
    """Freeze proactive.now_local to a fixed datetime for the test."""

    def set_now(dt: datetime) -> None:
        monkeypatch.setattr(proactive, "now_local", lambda settings: dt)

    return set_now


def make_settings(sample_settings, **overrides):
    """Return a Settings copy with the given fields overridden."""
    return dataclasses.replace(sample_settings, **overrides)


def make_job_context(settings, client=None):
    """Build the minimal context the proactive job drivers read."""
    return SimpleNamespace(
        bot_data={"settings": settings, "ai_client": client or object()}
    )


def _seed_raw(user_id: int, day: str, text: str, hour: int = 8) -> None:
    """Append a raw journal entry at a fixed instant for determinism."""
    ts = datetime(2026, int(day[5:7]), int(day[8:10]), hour, 0, tzinfo=TZ).timestamp()
    memory.append_raw_entry(user_id, day, text, ts=ts, tz_name=TZ_NAME)


# --- proactive_log: daily cap ------------------------------------------------


class TestProactiveLog:
    """proactive_log: per-user/per-day daily cap, atomic upsert, fail-closed."""

    def test_get_daily_count_starts_zero(self):
        assert proactive.get_daily_count(1, DAY) == 0

    def test_increment_upserts_and_stores_last_message(self):
        # Act: two increments on the same (user, day).
        assert proactive.increment_daily_count(1, DAY, "hola") == 1
        assert proactive.increment_daily_count(1, DAY, "otra vez") == 2

        # Assert: the upsert accumulated the count and kept the last text.
        with proactive._get_connection() as conn:
            row = conn.execute(
                "SELECT count, last_message FROM proactive_log"
                " WHERE user_id = 1 AND day = ?",
                (DAY,),
            ).fetchone()
        assert row["count"] == 2
        assert row["last_message"] == "otra vez"

    def test_can_send_true_under_max_false_at_max(self):
        proactive.increment_daily_count(1, DAY, "uno")
        proactive.increment_daily_count(1, DAY, "dos")
        assert proactive.can_send(1, DAY, 3) is True
        assert proactive.can_send(1, DAY, 2) is False

    def test_can_send_fail_closed_when_max_daily_nonpositive(self):
        # A misconfigured cap (<= 0) disables the feature entirely.
        proactive.increment_daily_count(1, DAY, "uno")
        assert proactive.can_send(1, DAY, 0) is False
        assert proactive.can_send(1, DAY, -1) is False

    def test_per_user_isolation(self):
        proactive.increment_daily_count(1, DAY, "para uno")
        assert proactive.get_daily_count(2, DAY) == 0
        assert proactive.can_send(2, DAY, 1) is True

    def test_per_day_isolation(self):
        proactive.increment_daily_count(1, DAY, "hoy")
        assert proactive.get_daily_count(1, "2026-08-13") == 0
        assert proactive.can_send(1, "2026-08-13", 1) is True


# --- time windows ------------------------------------------------------------


class TestWindowOf:
    """window_of: 3-hour morning/evening windows from the configured times."""

    def _now(self, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 8, 14, hour, minute, tzinfo=TZ)

    def test_inside_morning_window(self, sample_settings, now_at):
        now_at(self._now(8, 30))
        assert proactive.window_of(sample_settings) == "morning"

    def test_inside_evening_window(self, sample_settings, now_at):
        now_at(self._now(20, 30))
        assert proactive.window_of(sample_settings) == "evening"

    def test_outside_both_windows(self, sample_settings, now_at):
        now_at(self._now(14, 0))
        assert proactive.window_of(sample_settings) is None

    def test_morning_start_boundary_is_morning(self, sample_settings, now_at):
        now_at(self._now(8, 0))
        assert proactive.window_of(sample_settings) == "morning"

    def test_morning_end_boundary_is_outside(self, sample_settings, now_at):
        # The window is [08:00, 11:00): 11:00 itself is outside.
        now_at(self._now(11, 0))
        assert proactive.window_of(sample_settings) is None

    def test_evening_start_boundary_is_evening(self, sample_settings, now_at):
        now_at(self._now(20, 0))
        assert proactive.window_of(sample_settings) == "evening"

    def test_evening_end_boundary_is_outside(self, sample_settings, now_at):
        # The window is [20:00, 23:00): 23:00 itself is outside.
        now_at(self._now(23, 0))
        assert proactive.window_of(sample_settings) is None

    def test_invalid_morning_time_only_disables_morning(self, sample_settings, now_at):
        settings = make_settings(sample_settings, proactive_morning_time="oops")
        now_at(self._now(20, 30))
        assert proactive.window_of(settings) == "evening"
        now_at(self._now(8, 30))
        assert proactive.window_of(settings) is None


# --- message builders --------------------------------------------------------


class TestMessageBuilders:
    """build_morning_message / build_evening_message: model text or None."""

    async def test_morning_returns_model_message(self, fake_chat, sample_settings):
        fake_chat.content = "¡Buenos días! ¿Qué tienes planeado hoy?"
        message = await proactive.build_morning_message(
            client=object(), model="deepseek-v4-flash", user_id=1, settings=sample_settings
        )
        assert message == fake_chat.content
        assert fake_chat.calls  # the model was asked

    async def test_morning_returns_none_on_api_error(self, fake_chat, sample_settings):
        fake_chat.error = proactive.OpenCodeError("boom")
        message = await proactive.build_morning_message(
            client=object(), model="m", user_id=1, settings=sample_settings
        )
        assert message is None

    async def test_morning_returns_none_on_rate_limit(self, fake_chat, sample_settings):
        fake_chat.error = proactive.RateLimitError("busy")
        message = await proactive.build_morning_message(
            client=object(), model="m", user_id=1, settings=sample_settings
        )
        assert message is None

    async def test_morning_empty_context_still_asks_for_greeting(
        self, fake_chat, sample_settings
    ):
        # No journal, no reminders: the builder still calls the model so the
        # user gets the generic greeting the prompt guarantees.
        message = await proactive.build_morning_message(
            client=object(), model="m", user_id=1, settings=sample_settings
        )
        assert message == fake_chat.content
        assert "Recordatorios próximos" in fake_chat.calls[0]

    async def test_evening_returns_model_message(self, fake_chat, sample_settings):
        fake_chat.content = "¿Cómo te fue hoy?"
        message = await proactive.build_evening_message(
            client=object(), model="m", user_id=1, settings=sample_settings
        )
        assert message == fake_chat.content

    async def test_evening_returns_none_on_api_error(self, fake_chat, sample_settings):
        fake_chat.error = proactive.OpenCodeError("boom")
        message = await proactive.build_evening_message(
            client=object(), model="m", user_id=1, settings=sample_settings
        )
        assert message is None

    async def test_evening_empty_context_still_asks(self, fake_chat, sample_settings):
        message = await proactive.build_evening_message(
            client=object(), model="m", user_id=1, settings=sample_settings
        )
        assert message == fake_chat.content
        assert "no compartió nada hoy" in fake_chat.calls[0]


# --- context builders (memory + reminders) -----------------------------------


class TestContextBuilders:
    """_journal_context / _today_reminders: recent days, week summary, due items."""

    def test_journal_context_includes_recent_days(self, sample_settings, now_at):
        now_at(FIXED_NOW)
        _seed_raw(1, "2026-08-13", "fui al gimnasio")
        _seed_raw(1, "2026-08-10", "pagé la luz")
        context = proactive._journal_context(1, sample_settings)
        assert "2026-08-13" in context and "fui al gimnasio" in context
        assert "2026-08-10" in context and "pagé la luz" in context
        assert "Resumen semanal" not in context

    def test_journal_context_appends_weekly_summary(self, sample_settings, now_at):
        now_at(FIXED_NOW)
        memory.save_summary(1, "weekly", "2026-W33", "Semana de viajes y ejercicio")
        context = proactive._journal_context(1, sample_settings)
        assert "Resumen semanal: Semana de viajes y ejercicio" in context

    def test_journal_context_empty_when_no_data(self, sample_settings, now_at):
        now_at(FIXED_NOW)
        assert proactive._journal_context(1, sample_settings) == ""

    def test_today_reminders_lists_due(self, sample_settings, now_at):
        now_at(FIXED_NOW)
        reminders.create_reminder(
            1, 100, "Llamar al mecánico", FIXED_NOW.timestamp() + 3600
        )
        text = proactive._today_reminders(1, sample_settings)
        assert "Llamar al mecánico" in text
        assert "14/08 09:00" in text  # dd/mm HH:MM in the user's timezone

    def test_today_reminders_excludes_far_future(self, sample_settings, now_at):
        now_at(FIXED_NOW)
        reminders.create_reminder(
            1, 100, "Futuro lejano", FIXED_NOW.timestamp() + 25 * 3600
        )
        assert "Futuro lejano" not in proactive._today_reminders(1, sample_settings)

    def test_today_reminders_ninguno_when_empty(self, sample_settings, now_at):
        now_at(FIXED_NOW)
        assert proactive._today_reminders(1, sample_settings) == "Ninguno"


# --- check-in drivers --------------------------------------------------------


class TestCheckins:
    """run_checkins: send each user's check-in only when allowed."""

    @staticmethod
    def _settings(sample_settings, **overrides):
        return make_settings(
            sample_settings, authorized_users=frozenset({1}), **overrides
        )

    async def test_morning_checkin_sends_and_increments(
        self, sample_settings, monkeypatch, fake_chat, now_at
    ):
        settings = self._settings(sample_settings)
        now_at(datetime(2026, 8, 14, 8, 30, tzinfo=TZ))
        reminders.upsert_user_chat(1, 100)
        fake_chat.content = "¡Buenos días! ¿Qué tienes planeado hoy?"
        sent = []

        async def fake_send(context, chat_id, text):
            sent.append((chat_id, text))
            return True

        monkeypatch.setattr(proactive, "send_proactive", fake_send)

        await proactive.run_checkins(make_job_context(settings))

        assert sent == [(100, fake_chat.content)]
        assert proactive.get_daily_count(1, DAY) == 1

    async def test_evening_checkin_sends_and_increments(
        self, sample_settings, monkeypatch, fake_chat, now_at
    ):
        settings = self._settings(sample_settings)
        now_at(datetime(2026, 8, 14, 20, 30, tzinfo=TZ))
        reminders.upsert_user_chat(1, 100)
        fake_chat.content = "¿Cómo te fue hoy?"
        sent = []

        async def fake_send(context, chat_id, text):
            sent.append((chat_id, text))
            return True

        monkeypatch.setattr(proactive, "send_proactive", fake_send)

        await proactive.run_checkins(make_job_context(settings))

        assert sent == [(100, fake_chat.content)]
        assert proactive.get_daily_count(1, DAY) == 1

    async def test_checkin_skips_outside_window(
        self, sample_settings, monkeypatch, fake_chat, now_at
    ):
        settings = self._settings(sample_settings)
        now_at(datetime(2026, 8, 14, 14, 0, tzinfo=TZ))  # outside both windows
        reminders.upsert_user_chat(1, 100)
        sent = []

        async def fake_send(context, chat_id, text):
            sent.append((chat_id, text))
            return True

        monkeypatch.setattr(proactive, "send_proactive", fake_send)

        await proactive.run_checkins(make_job_context(settings))

        assert sent == []
        assert fake_chat.calls == []  # the cap/window guard ran before any model call
        assert proactive.get_daily_count(1, DAY) == 0

    async def test_checkin_skips_when_cap_reached(
        self, sample_settings, monkeypatch, fake_chat, now_at
    ):
        settings = self._settings(sample_settings, proactive_max_daily=1)
        now_at(datetime(2026, 8, 14, 8, 30, tzinfo=TZ))
        reminders.upsert_user_chat(1, 100)
        proactive.increment_daily_count(1, DAY, "antes")
        sent = []

        async def fake_send(context, chat_id, text):
            sent.append((chat_id, text))
            return True

        monkeypatch.setattr(proactive, "send_proactive", fake_send)

        await proactive.run_checkins(make_job_context(settings))

        assert sent == []
        assert fake_chat.calls == []  # no model call for a capped-out user
        assert proactive.get_daily_count(1, DAY) == 1

    async def test_checkin_skips_when_no_chat_known(
        self, sample_settings, monkeypatch, fake_chat, now_at
    ):
        settings = self._settings(sample_settings)
        now_at(datetime(2026, 8, 14, 8, 30, tzinfo=TZ))
        sent = []

        async def fake_send(context, chat_id, text):
            sent.append((chat_id, text))
            return True

        monkeypatch.setattr(proactive, "send_proactive", fake_send)

        await proactive.run_checkins(make_job_context(settings))

        assert sent == []
        assert proactive.get_daily_count(1, DAY) == 0

    async def test_checkin_does_not_count_when_generation_fails(
        self, sample_settings, monkeypatch, fake_chat, now_at
    ):
        settings = self._settings(sample_settings)
        now_at(datetime(2026, 8, 14, 8, 30, tzinfo=TZ))
        reminders.upsert_user_chat(1, 100)
        fake_chat.error = proactive.OpenCodeError("boom")
        sent = []

        async def fake_send(context, chat_id, text):
            sent.append((chat_id, text))
            return True

        monkeypatch.setattr(proactive, "send_proactive", fake_send)

        await proactive.run_checkins(make_job_context(settings))

        assert sent == []
        assert proactive.get_daily_count(1, DAY) == 0

    async def test_checkin_noop_when_proactive_disabled(
        self, sample_settings, monkeypatch, fake_chat, now_at
    ):
        settings = self._settings(sample_settings, proactive_enabled=False)
        now_at(datetime(2026, 8, 14, 8, 30, tzinfo=TZ))
        reminders.upsert_user_chat(1, 100)
        sent = []

        async def fake_send(context, chat_id, text):
            sent.append((chat_id, text))
            return True

        monkeypatch.setattr(proactive, "send_proactive", fake_send)

        await proactive.run_checkins(make_job_context(settings))

        assert sent == []
        assert fake_chat.calls == []
        assert proactive.get_daily_count(1, DAY) == 0

    async def test_checkin_noop_when_memory_disabled(
        self, sample_settings, monkeypatch, fake_chat, now_at
    ):
        settings = self._settings(sample_settings, memory_enabled=False)
        now_at(datetime(2026, 8, 14, 20, 30, tzinfo=TZ))
        reminders.upsert_user_chat(1, 100)
        sent = []

        async def fake_send(context, chat_id, text):
            sent.append((chat_id, text))
            return True

        monkeypatch.setattr(proactive, "send_proactive", fake_send)

        await proactive.run_checkins(make_job_context(settings))

        assert sent == []
        assert fake_chat.calls == []
        assert proactive.get_daily_count(1, DAY) == 0

    async def test_checkin_sends_once_per_window(
        self, sample_settings, monkeypatch, fake_chat, now_at
    ):
        # The periodic job fires many times inside a 3h window; the sent
        # marker must prevent a second delivery of the same window.
        settings = self._settings(sample_settings)
        now_at(datetime(2026, 8, 14, 8, 30, tzinfo=TZ))
        reminders.upsert_user_chat(1, 100)
        fake_chat.content = "¡Buenos días!"
        sent = []

        async def fake_send(context, chat_id, text):
            sent.append((chat_id, text))
            return True

        monkeypatch.setattr(proactive, "send_proactive", fake_send)

        await proactive.run_checkins(make_job_context(settings))
        await proactive.run_checkins(make_job_context(settings))  # second firing

        assert sent == [(100, fake_chat.content)]
        assert proactive.get_daily_count(1, DAY) == 1
        assert proactive.checkin_sent_today(1, DAY, "morning")
        assert not proactive.checkin_sent_today(1, DAY, "evening")

    async def test_checkin_respects_per_user_times(
        self, sample_settings, monkeypatch, fake_chat, now_at
    ):
        # Default morning is 08:00; user sets it to 12:00. At 13:00 the
        # default window is closed but the custom one is open -> still sent.
        settings = self._settings(sample_settings)
        proactive.set_checkin_time(1, "morning", "12:00")
        now_at(datetime(2026, 8, 14, 13, 0, tzinfo=TZ))
        reminders.upsert_user_chat(1, 100)
        fake_chat.content = "Buenas tardes de mañana"
        sent = []

        async def fake_send(context, chat_id, text):
            sent.append((chat_id, text))
            return True

        monkeypatch.setattr(proactive, "send_proactive", fake_send)

        await proactive.run_checkins(make_job_context(settings))

        assert sent == [(100, fake_chat.content)]

    async def test_checkin_skips_outside_per_user_window(
        self, sample_settings, monkeypatch, fake_chat, now_at
    ):
        settings = self._settings(sample_settings)
        proactive.set_checkin_time(1, "evening", "18:00")
        now_at(datetime(2026, 8, 14, 14, 0, tzinfo=TZ))  # outside 08-11 and 18-21
        reminders.upsert_user_chat(1, 100)
        sent = []

        async def fake_send(context, chat_id, text):
            sent.append((chat_id, text))
            return True

        monkeypatch.setattr(proactive, "send_proactive", fake_send)

        await proactive.run_checkins(make_job_context(settings))

        assert sent == []
        assert fake_chat.calls == []


# --- per-user check-in settings ----------------------------------------------


class TestPerUserSettings:
    """set_checkin_time / get_checkin_times: per-user overrides with defaults."""

    def test_defaults_fall_back_to_settings(self, sample_settings):
        settings = make_settings(
            sample_settings,
            proactive_morning_time="07:00",
            proactive_evening_time="21:00",
        )
        assert proactive.get_checkin_times(1, settings) == ("07:00", "21:00")

    def test_set_morning_preserves_evening(self, sample_settings):
        settings = make_settings(sample_settings)
        assert proactive.set_checkin_time(1, "morning", "09:30")["ok"] is True
        assert proactive.get_checkin_times(1, settings) == ("09:30", "20:00")

    def test_set_evening_preserves_morning(self, sample_settings):
        settings = make_settings(sample_settings)
        proactive.set_checkin_time(1, "morning", "09:30")
        assert proactive.set_checkin_time(1, "evening", "18:45")["ok"] is True
        assert proactive.get_checkin_times(1, settings) == ("09:30", "18:45")

    def test_rejects_invalid_time(self, sample_settings):
        result = proactive.set_checkin_time(1, "morning", "25:99")
        assert result["ok"] is False
        assert "Hora inválida" in result["error"]

    def test_rejects_invalid_period(self, sample_settings):
        result = proactive.set_checkin_time(1, "midday", "09:00")
        assert result["ok"] is False
        assert "Periodo inválido" in result["error"]

    def test_isolation_between_users(self, sample_settings):
        proactive.set_checkin_time(1, "morning", "09:30")
        assert proactive.get_checkin_times(2, sample_settings) == ("08:00", "20:00")


class TestSentMarkers:
    """checkin_sent_today / mark_checkin_sent: at most once per window per day."""

    def test_unsent_by_default(self):
        assert not proactive.checkin_sent_today(1, DAY, "morning")
        assert not proactive.checkin_sent_today(1, DAY, "evening")

    def test_mark_sets_only_that_window(self):
        proactive.mark_checkin_sent(1, DAY, "morning", ts=100.0)
        assert proactive.checkin_sent_today(1, DAY, "morning")
        assert not proactive.checkin_sent_today(1, DAY, "evening")

    def test_mark_is_idempotent(self):
        proactive.mark_checkin_sent(1, DAY, "morning", ts=100.0)
        proactive.mark_checkin_sent(1, DAY, "morning", ts=200.0)
        with proactive._get_connection() as conn:
            row = conn.execute(
                "SELECT morning_sent_at FROM proactive_log"
                " WHERE user_id = 1 AND day = ?",
                (DAY,),
            ).fetchone()
        assert row["morning_sent_at"] == 100.0  # first write wins

    def test_per_day_scope(self):
        proactive.mark_checkin_sent(1, DAY, "morning", ts=100.0)
        assert not proactive.checkin_sent_today(1, "2026-08-15", "morning")


# --- delivery ----------------------------------------------------------------


class RecordingBot:
    """Fake PTB bot that records send_message calls and can fail per attempt."""

    def __init__(self, failures=()):
        self.failures = list(failures)
        self.calls: list[dict] = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.calls.append(
            {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        )
        if self.failures:
            raise self.failures.pop(0)
        return True


class TestSendProactive:
    """send_proactive: markdown-first delivery with plain-text fallback."""

    async def test_sends_markdown_success(self):
        bot = RecordingBot()
        context = SimpleNamespace(bot=bot)

        ok = await proactive.send_proactive(context, 100, "Hola")

        assert ok is True
        assert len(bot.calls) == 1
        assert bot.calls[0]["parse_mode"] == ParseMode.MARKDOWN

    async def test_falls_back_to_plain_text_on_bad_request(self):
        # The model sometimes emits unbalanced markdown: a BadRequest on the
        # first send must not fail the delivery, only downgrade parse mode.
        bot = RecordingBot(failures=[BadRequest("malformed markdown")])
        context = SimpleNamespace(bot=bot)

        ok = await proactive.send_proactive(context, 100, "Hola *mundo")

        assert ok is True
        assert len(bot.calls) == 2
        assert bot.calls[0]["parse_mode"] == ParseMode.MARKDOWN
        assert bot.calls[1]["parse_mode"] is None

    async def test_returns_false_when_all_attempts_fail(self, monkeypatch):
        async def no_sleep(attempts):
            return None

        monkeypatch.setattr(proactive, "_backoff_sleep", no_sleep)
        bot = RecordingBot(
            failures=[
                Exception("net down"),
                Exception("net down"),
                Exception("net down"),
            ]
        )
        context = SimpleNamespace(bot=bot)

        ok = await proactive.send_proactive(context, 100, "Hola")

        assert ok is False
        assert len(bot.calls) == 3  # MAX_DELIVERY_ATTEMPTS tries


# --- nightly compaction ------------------------------------------------------


class TestNightlyCompaction:
    """run_nightly_compaction: compact yesterday only when it earns it."""

    @staticmethod
    def _settings(sample_settings):
        return make_settings(sample_settings, authorized_users=frozenset({1}))

    async def test_compacts_day_above_threshold(
        self, sample_settings, fake_summarizer_chat, now_at
    ):
        settings = self._settings(sample_settings)
        now_at(datetime(2026, 8, 14, 1, 0, tzinfo=TZ))  # shortly after midnight
        _seed_raw(1, "2026-08-13", " ".join(["palabra"] * 1300))

        await proactive.run_nightly_compaction(make_job_context(settings))

        assert memory.get_daily_compact(1, "2026-08-13") == "Resumen compacto"
        assert memory.get_raw_day(1, "2026-08-13")["compact_path"]

    async def test_skips_day_below_threshold(
        self, sample_settings, fake_summarizer_chat, now_at
    ):
        settings = self._settings(sample_settings)
        now_at(datetime(2026, 8, 14, 1, 0, tzinfo=TZ))
        _seed_raw(1, "2026-08-13", "solo tres palabras")

        await proactive.run_nightly_compaction(make_job_context(settings))

        assert fake_summarizer_chat.calls == []  # no model call below threshold
        assert memory.get_daily_compact(1, "2026-08-13") == ""

    async def test_skips_when_compact_exists(
        self, sample_settings, fake_summarizer_chat, now_at
    ):
        settings = self._settings(sample_settings)
        now_at(datetime(2026, 8, 14, 1, 0, tzinfo=TZ))
        _seed_raw(1, "2026-08-13", " ".join(["palabra"] * 1300))
        memory.save_daily_compact(1, "2026-08-13", "ya compactado")

        await proactive.run_nightly_compaction(make_job_context(settings))

        assert fake_summarizer_chat.calls == []  # idempotent, never pays twice
        assert memory.get_daily_compact(1, "2026-08-13") == "ya compactado"

    async def test_catches_up_missed_days_after_downtime(
        self, sample_settings, fake_summarizer_chat, now_at
    ):
        # Server was down for two nights: days 08-12 and 08-13 both exceed the
        # threshold and neither has a compact. The catch-up window (3 days)
        # must compact BOTH on the next run, not just yesterday.
        settings = self._settings(sample_settings)
        now_at(datetime(2026, 8, 14, 1, 0, tzinfo=TZ))
        _seed_raw(1, "2026-08-12", " ".join(["palabra"] * 1300))
        _seed_raw(1, "2026-08-13", " ".join(["palabra"] * 1300))

        await proactive.run_nightly_compaction(make_job_context(settings))

        assert memory.get_daily_compact(1, "2026-08-12") == "Resumen compacto"
        assert memory.get_daily_compact(1, "2026-08-13") == "Resumen compacto"
        assert memory.get_daily_compact(1, "2026-08-14") == ""

    async def test_noop_when_memory_disabled(
        self, sample_settings, fake_summarizer_chat, now_at
    ):
        settings = make_settings(
            self._settings(sample_settings), memory_enabled=False
        )
        now_at(datetime(2026, 8, 14, 1, 0, tzinfo=TZ))
        _seed_raw(1, "2026-08-13", " ".join(["palabra"] * 1300))

        await proactive.run_nightly_compaction(make_job_context(settings))

        assert fake_summarizer_chat.calls == []
        assert memory.get_daily_compact(1, "2026-08-13") == ""


# --- weekly summaries --------------------------------------------------------


class TestWeeklySummaries:
    """run_weekly_summaries: catch up the last few ISO weeks per user.

    The job looks back WEEKLY_CATCHUP_WEEKS (8) weeks so a server that was
    down when a Monday job should have run fills the gap on the next run.
    """

    async def test_ensures_last_weeks_per_user(
        self, sample_settings, monkeypatch, now_at
    ):
        settings = make_settings(
            sample_settings, authorized_users=frozenset({1, 2})
        )
        # 2026-08-14 is Friday of ISO week 2026-W33; offsets 1..8 days*7
        # cover W32 down to W25.
        now_at(datetime(2026, 8, 14, 0, 30, tzinfo=TZ))
        calls = []

        async def fake_ensure(
            user_id, level, period, client, model, tz_name="UTC", daily_threshold=600,
            group_id=None, session_id=None, provider="opencode",
        ):
            calls.append((user_id, level, period, tz_name, daily_threshold))

        monkeypatch.setattr(summarizer, "ensure_summary", fake_ensure)

        await proactive.run_weekly_summaries(make_job_context(settings))

        weeks = ["2026-W32", "2026-W31", "2026-W30", "2026-W29",
                 "2026-W28", "2026-W27", "2026-W26", "2026-W25"]
        assert calls == [
            (1, "weekly", w, TZ_NAME, 1200) for w in weeks
        ] + [(2, "weekly", w, TZ_NAME, 1200) for w in weeks]

    async def test_catches_up_missing_weeks_after_downtime(
        self, sample_settings, monkeypatch, now_at
    ):
        # Server was down for two Mondays: only W32 got summarized. The next
        # run must generate the other seven missed weeks, not just the last
        # one, and never regenerate the existing summary.
        settings = make_settings(
            sample_settings, authorized_users=frozenset({1})
        )
        now_at(datetime(2026, 8, 14, 0, 30, tzinfo=TZ))
        memory.save_summary(1, "weekly", "2026-W32", "ya resumido")
        calls = []

        async def fake_ensure(
            user_id, level, period, client, model, tz_name="UTC", daily_threshold=600,
            group_id=None, session_id=None, provider="opencode",
        ):
            calls.append((user_id, level, period))

        monkeypatch.setattr(summarizer, "ensure_summary", fake_ensure)

        await proactive.run_weekly_summaries(make_job_context(settings))

        assert "2026-W32" not in calls  # existing summary skipped
        assert len(calls) == 7  # the seven missing weeks
        assert all(period != "2026-W32" for _, _, period in calls)

    async def test_noop_when_memory_disabled(
        self, sample_settings, monkeypatch, now_at
    ):
        settings = make_settings(
            sample_settings, memory_enabled=False, authorized_users=frozenset({1})
        )
        now_at(datetime(2026, 8, 14, 0, 30, tzinfo=TZ))
        calls = []

        async def fake_ensure(
            user_id, level, period, client, model, tz_name="UTC", daily_threshold=600,
            group_id=None, session_id=None, provider="opencode",
        ):
            calls.append((user_id, level, period))

        monkeypatch.setattr(summarizer, "ensure_summary", fake_ensure)

        await proactive.run_weekly_summaries(make_job_context(settings))

        assert calls == []


# --- quarterly summaries -----------------------------------------------------


class TestQuarterlySummaries:
    """run_quarterly_summaries: catch up the last few quarters, any day.

    The job has no calendar guard: it runs daily and ensures the last
    QUARTERLY_CATCHUP_QUARTERS (4) quarters, so a quarter whose closing day
    (1st of Jan/Apr/Jul/Oct) was missed while the server was down is filled
    in on the next run instead of being lost.
    """

    async def _run(self, sample_settings, monkeypatch):
        settings = make_settings(
            sample_settings, authorized_users=frozenset({1})
        )
        calls = []

        async def fake_ensure(
            user_id, level, period, client, model, tz_name="UTC", daily_threshold=600,
            group_id=None, session_id=None, provider="opencode",
        ):
            calls.append((user_id, level, period, tz_name, daily_threshold))

        monkeypatch.setattr(summarizer, "ensure_summary", fake_ensure)
        await proactive.run_quarterly_summaries(make_job_context(settings))
        return calls

    async def test_ensures_recent_quarters(
        self, sample_settings, monkeypatch, now_at
    ):
        now_at(datetime(2026, 4, 1, 0, 45, tzinfo=TZ))
        calls = await self._run(sample_settings, monkeypatch)
        assert calls == [
            (1, "quarterly", "2025-Q3", TZ_NAME, 1200),
            (1, "quarterly", "2025-Q4", TZ_NAME, 1200),
            (1, "quarterly", "2026-Q1", TZ_NAME, 1200),
            (1, "quarterly", "2026-Q2", TZ_NAME, 1200),
        ]

    async def test_catches_up_missed_quarter_after_downtime(
        self, sample_settings, monkeypatch, now_at
    ):
        # Server was down on April 1 (the Q1 closing day); by June the Q1
        # summary is still missing while Q2 has already been summarized. The
        # next run must generate Q1 (not just the most recent Q2).
        now_at(datetime(2026, 6, 15, 0, 45, tzinfo=TZ))
        memory.save_summary(1, "quarterly", "2026-Q2", "ya resumido")
        calls = await self._run(sample_settings, monkeypatch)
        periods = [period for _, _, period, _, _ in calls]
        assert "2026-Q1" in periods  # the missed quarter gets generated
        assert "2026-Q2" not in periods  # existing summary skipped

    async def test_noop_when_memory_disabled(
        self, sample_settings, monkeypatch, now_at
    ):
        settings = make_settings(
            sample_settings, memory_enabled=False, authorized_users=frozenset({1})
        )
        now_at(datetime(2026, 4, 1, 0, 45, tzinfo=TZ))
        calls = []

        async def fake_ensure(
            user_id, level, period, client, model, tz_name="UTC", daily_threshold=600,
            group_id=None, session_id=None, provider="opencode",
        ):
            calls.append((user_id, level, period))

        monkeypatch.setattr(summarizer, "ensure_summary", fake_ensure)

        await proactive.run_quarterly_summaries(make_job_context(settings))

        assert calls == []


# --- yearly summaries --------------------------------------------------------


class TestYearlySummaries:
    """run_yearly_summaries: catch up the last few years, any day.

    The job has no Jan-1 guard: it runs daily and ensures the current and
    previous calendar years, so a year whose January 1 job was missed while
    the server was down is filled in on the next run.
    """

    async def test_ensures_recent_years(
        self, sample_settings, monkeypatch, now_at
    ):
        settings = make_settings(
            sample_settings, authorized_users=frozenset({1})
        )
        now_at(datetime(2026, 1, 1, 1, 0, tzinfo=TZ))
        calls = []

        async def fake_ensure(
            user_id, level, period, client, model, tz_name="UTC", daily_threshold=600,
            group_id=None, session_id=None, provider="opencode",
        ):
            calls.append((user_id, level, period, tz_name, daily_threshold))

        monkeypatch.setattr(summarizer, "ensure_summary", fake_ensure)

        await proactive.run_yearly_summaries(make_job_context(settings))

        assert calls == [
            (1, "yearly", "2026", TZ_NAME, 1200),
            (1, "yearly", "2025", TZ_NAME, 1200),
        ]

    async def test_catches_up_missed_year_after_downtime(
        self, sample_settings, monkeypatch, now_at
    ):
        # Server was down on Jan 1; by February the previous year's summary
        # is still missing. Any-day run must generate it (2025) plus the
        # in-progress current year (2026).
        settings = make_settings(
            sample_settings, authorized_users=frozenset({1})
        )
        now_at(datetime(2026, 2, 15, 1, 0, tzinfo=TZ))
        calls = []

        async def fake_ensure(
            user_id, level, period, client, model, tz_name="UTC", daily_threshold=600,
            group_id=None, session_id=None, provider="opencode",
        ):
            calls.append((user_id, level, period))

        monkeypatch.setattr(summarizer, "ensure_summary", fake_ensure)

        await proactive.run_yearly_summaries(make_job_context(settings))

        assert [period for _, _, period in calls] == ["2026", "2025"]

    async def test_noop_when_memory_disabled(
        self, sample_settings, monkeypatch, now_at
    ):
        settings = make_settings(
            sample_settings, memory_enabled=False, authorized_users=frozenset({1})
        )
        now_at(datetime(2026, 1, 1, 1, 0, tzinfo=TZ))
        calls = []

        async def fake_ensure(
            user_id, level, period, client, model, tz_name="UTC", daily_threshold=600,
            group_id=None, session_id=None, provider="opencode",
        ):
            calls.append((user_id, level, period))

        monkeypatch.setattr(summarizer, "ensure_summary", fake_ensure)

        await proactive.run_yearly_summaries(make_job_context(settings))

        assert calls == []


# --- job wiring in bot.bot ---------------------------------------------------


class FakeJobQueue:
    """Records run_daily/run_repeating registrations without a real scheduler."""

    def __init__(self):
        self.jobs: list[dict] = []

    def run_daily(self, callback, time=None, days=None, name=None):
        self.jobs.append(
            {"kind": "daily", "callback": callback, "time": time,
             "days": days, "name": name}
        )

    def run_repeating(self, callback, interval=None, first=None, name=None):
        self.jobs.append(
            {"kind": "repeating", "callback": callback, "interval": interval,
             "first": first, "name": name}
        )


class TestJobWiring:
    """bot.bot._register_memory_jobs: every job guarded by Settings flags."""

    def test_registers_five_jobs_when_full_enabled(self, sample_settings):
        app = SimpleNamespace(job_queue=FakeJobQueue())

        bot_wiring._register_memory_jobs(app, sample_settings)

        by_name = {job["name"]: job for job in app.job_queue.jobs}
        assert set(by_name) == {
            "memory_nightly_compaction",
            "memory_weekly_summaries",
            "memory_quarterly_summaries",
            "memory_yearly_summaries",
            "memory_checkins",
            "auto_journal",
        }
        assert by_name["memory_nightly_compaction"]["time"] == time(0, 15)
        # All summary jobs run daily (catch-up scanners): no day filter.
        assert by_name["memory_weekly_summaries"]["days"] is None
        assert by_name["memory_weekly_summaries"]["time"] == time(0, 30)
        assert by_name["memory_quarterly_summaries"]["time"] == time(0, 45)
        assert by_name["memory_yearly_summaries"]["time"] == time(1, 0)
        # The check-in driver is periodic (15 min) so per-user times resolve
        # without per-user scheduling.
        checkins = by_name["memory_checkins"]
        assert checkins["kind"] == "repeating"
        assert checkins["interval"] == 900
        assert checkins["callback"] is proactive.run_checkins
        # Auto-journal runs every 60 minutes (configurable, 75% saving vs 30min)
        auto_journal_job = by_name["auto_journal"]
        assert auto_journal_job["kind"] == "repeating"
        assert auto_journal_job["interval"] == 3600

    def test_no_jobs_when_memory_disabled(self, sample_settings):
        settings = make_settings(sample_settings, memory_enabled=False)
        app = SimpleNamespace(job_queue=FakeJobQueue())

        bot_wiring._register_memory_jobs(app, settings)

        assert app.job_queue.jobs == []

    def test_four_summary_jobs_when_proactive_disabled(self, sample_settings):
        settings = make_settings(sample_settings, proactive_enabled=False)
        app = SimpleNamespace(job_queue=FakeJobQueue())

        bot_wiring._register_memory_jobs(app, settings)

        names = {job["name"] for job in app.job_queue.jobs}
        assert names == {
            "memory_nightly_compaction",
            "memory_weekly_summaries",
            "memory_quarterly_summaries",
            "memory_yearly_summaries",
            "auto_journal",
        }
