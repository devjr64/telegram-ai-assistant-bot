"""Unit tests for bot.reminders — scheduling, recurrence, and chat tracking."""

import calendar
import time
from datetime import datetime, timezone

from bot import reminders


class TestUserChats:
    def test_upsert_and_get(self):
        reminders.upsert_user_chat(1, 100)
        assert reminders.get_chat_for_user(1) == 100

    def test_upsert_updates_chat(self):
        reminders.upsert_user_chat(1, 100)
        reminders.upsert_user_chat(1, 200)
        assert reminders.get_chat_for_user(1) == 200

    def test_get_unknown_user(self):
        assert reminders.get_chat_for_user(99) is None


class TestCreateReminder:
    def test_create_returns_dict(self):
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "Llamar", now, "none")
        assert reminder["message"] == "Llamar"
        assert reminder["recurrence"] == "none"
        assert reminder["user_id"] == 1

    def test_invalid_recurrence_defaults_none(self):
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "X", now, "yearly")
        assert reminder["recurrence"] == "none"


class TestNextOccurrence:
    def test_none_returns_none(self):
        assert reminders.next_occurrence(1000, "none") is None

    def test_daily_adds_day(self):
        assert reminders.next_occurrence(1000, "daily") == 1000 + 86400

    def test_weekly_adds_week(self):
        assert reminders.next_occurrence(1000, "weekly") == 1000 + 7 * 86400

    def test_monthly_advances_month(self):
        jan_15 = datetime(2026, 1, 15, tzinfo=timezone.utc).timestamp()
        next_ts = reminders.next_occurrence(jan_15, "monthly")
        next_dt = datetime.fromtimestamp(next_ts, tz=timezone.utc)
        assert (next_dt.year, next_dt.month) == (2026, 2)
        assert next_dt.day == 15

    def test_monthly_clamps_day(self):
        # 31 Jan -> 28 Feb (2026, non-leap)
        jan_31 = datetime(2026, 1, 31, tzinfo=timezone.utc).timestamp()
        next_ts = reminders.next_occurrence(jan_31, "monthly")
        next_dt = datetime.fromtimestamp(next_ts, tz=timezone.utc)
        assert next_dt.day == calendar.monthrange(2026, 2)[1]

    def test_monthly_rolls_year(self):
        dec_15 = datetime(2026, 12, 15, tzinfo=timezone.utc).timestamp()
        next_ts = reminders.next_occurrence(dec_15, "monthly")
        next_dt = datetime.fromtimestamp(next_ts, tz=timezone.utc)
        assert (next_dt.year, next_dt.month) == (2027, 1)


class TestDueReminders:
    def test_get_due_reminders(self):
        now = time.time()
        reminders.create_reminder(1, 100, "due", now - 10, "none")
        reminders.create_reminder(2, 200, "future", now + 9999, "none")
        due = reminders.get_due_reminders(now)
        assert len(due) == 1
        assert due[0]["message"] == "due"

    def test_mark_reminder_sent(self):
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "x", now - 10)
        reminders.mark_reminder_sent(reminder["id"])
        assert reminders.get_due_reminders(now) == []

    def test_reschedule_reminder(self):
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "x", now - 10, "daily")
        next_ts = reminders.next_occurrence(reminder["remind_at"], "daily")
        reminders.reschedule_reminder(reminder["id"], next_ts)
        due = reminders.get_due_reminders(now)
        assert len(due) == 0  # rescheduled into the future


class TestListAndDelete:
    def test_list_pending_reminders(self):
        now = time.time()
        reminders.create_reminder(1, 100, "soon", now + 10)
        reminders.create_reminder(1, 100, "later", now + 1000)
        reminders.create_reminder(2, 200, "other user", now + 10)
        pending = reminders.list_pending_reminders(1)
        assert len(pending) == 2
        assert [r["message"] for r in pending] == ["soon", "later"]

    def test_delete_reminder_owned(self):
        now = time.time()
        r1 = reminders.create_reminder(1, 100, "mine", now + 10)
        r2 = reminders.create_reminder(2, 200, "theirs", now + 10)
        assert reminders.delete_reminder(r1["id"], 1) is True
        assert reminders.delete_reminder(r2["id"], 1) is False  # not owned


class TestRetryBackoff:
    def test_retry_delay_sequence(self):
        # 10, 20, 40, 80, 160 — exponential
        assert reminders.retry_delay(0) == 10
        assert reminders.retry_delay(1) == 20
        assert reminders.retry_delay(2) == 40
        assert reminders.retry_delay(3) == 80

    def test_retry_delay_caps_at_max(self):
        # capped at 8h = 28800s
        assert reminders.retry_delay(11) == 20480
        assert reminders.retry_delay(12) == 28800
        assert reminders.retry_delay(50) == 28800

    def test_schedule_retry_sets_next_retry_at(self):
        now = time.time()
        r = reminders.create_reminder(1, 100, "x", now - 10)
        next_at = reminders.schedule_retry(r["id"])
        assert next_at is not None and next_at >= now + 10
        # After first failure, it is withheld from due reminders
        assert reminders.get_due_reminders(now + 5) == []

    def test_schedule_retry_increments_attempts(self):
        now = time.time()
        r = reminders.create_reminder(1, 100, "x", now - 10)
        reminders.schedule_retry(r["id"])
        first = reminders.get_reminder_by_id(r["id"], 1)
        reminders.schedule_retry(r["id"])
        second = reminders.get_reminder_by_id(r["id"], 1)
        # use get via DB — schedule_retry increments attempts each call
        with reminders._get_connection() as conn:
            row = conn.execute("SELECT attempts, next_retry_at FROM reminders WHERE id = ?", (r["id"],)).fetchone()
            assert row["attempts"] == 2
        assert second is not None and second["id"] == first["id"]  # sanity

    def test_due_after_retry_window_elapses(self):
        now = time.time()
        r = reminders.create_reminder(1, 100, "x", now - 10)
        reminders.schedule_retry(r["id"])
        assert reminders.get_due_reminders(now + 5) == []
        # once the window passes, it's due again
        assert len(reminders.get_due_reminders(now + 11)) == 1

    def test_schedule_retry_unknown_id_returns_none(self):
        assert reminders.schedule_retry(99999) is None

    def test_reset_retry_clears_state(self):
        now = time.time()
        r = reminders.create_reminder(1, 100, "x", now - 10)
        reminders.schedule_retry(r["id"])
        reminders.reset_retry(r["id"])
        assert reminders.get_due_reminders(now)  # due again immediately
        with reminders._get_connection() as conn:
            row = conn.execute("SELECT attempts, next_retry_at FROM reminders WHERE id = ?", (r["id"],)).fetchone()
            assert row["attempts"] == 0
            assert row["next_retry_at"] is None


class TestEditReminders:
    def test_get_reminder_by_id_owned(self):
        now = time.time()
        r = reminders.create_reminder(1, 100, "Llamar", now + 10)
        found = reminders.get_reminder_by_id(r["id"], 1)
        assert found is not None and found["message"] == "Llamar"

    def test_get_reminder_by_id_foreign_rejected(self):
        now = time.time()
        r = reminders.create_reminder(1, 100, "Llamar", now + 10)
        assert reminders.get_reminder_by_id(r["id"], 2) is None

    def test_update_reminder_text(self):
        now = time.time()
        r = reminders.create_reminder(1, 100, "Llamar", now + 10)
        assert reminders.update_reminder_text(r["id"], 1, "Llamar de nuevo") is True
        found = reminders.get_reminder_by_id(r["id"], 1)
        assert found["message"] == "Llamar de nuevo"

    def test_update_reminder_text_foreign_rejected(self):
        now = time.time()
        r = reminders.create_reminder(1, 100, "Llamar", now + 10)
        assert reminders.update_reminder_text(r["id"], 2, "Hack") is False
        found = reminders.get_reminder_by_id(r["id"], 1)
        assert found["message"] == "Llamar"

    def test_update_reminder_time(self):
        now = time.time()
        r = reminders.create_reminder(1, 100, "Llamar", now + 10)
        new_time = now + 9999
        assert reminders.update_reminder_time(r["id"], 1, new_time) is True
        found = reminders.get_reminder_by_id(r["id"], 1)
        assert found["remind_at"] == new_time

    def test_postpone_reminder(self):
        now = time.time()
        r = reminders.create_reminder(1, 100, "Llamar", now + 10)
        assert reminders.postpone_reminder(r["id"], 1, 3600) is True
        found = reminders.get_reminder_by_id(r["id"], 1)
        assert found["remind_at"] == now + 10 + 3600

    def test_edit_resets_backoff_state(self):
        now = time.time()
        r = reminders.create_reminder(1, 100, "Llamar", now - 10)
        reminders.schedule_retry(r["id"])
        reminders.update_reminder_text(r["id"], 1, "Actualizado")
        found = reminders.get_reminder_by_id(r["id"], 1)
        assert found["attempts"] == 0 and found["next_retry_at"] is None
