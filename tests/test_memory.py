"""Unit tests for bot.memory — SQLite metadata + per-user Markdown journal.

The storage layer is the foundation of the hierarchical memory system: raw
day entries, daily compacts and weekly/quarterly/yearly summaries, all
namespaced per user under ``memory/{user_id}/`` and guarded against path
traversal. Every test runs against a fresh tmp directory via the autouse
``isolated_data_dir`` fixture (see tests/conftest.py); no network, no real
tokens, fixed timestamps and fixed timezones keep everything deterministic.
"""

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from bot import memory

# Fixed timezone and instant for deterministic assertions. America/Caracas
# is UTC-4 with no DST, so 2026-08-14 15:45 local == 19:45 UTC.
TZ = ZoneInfo("America/Caracas")
TS = datetime(2026, 8, 14, 15, 45, tzinfo=TZ).timestamp()

# Day strings the path-traversal gate must reject: wrong shapes, impossible
# calendar dates, and crafted traversal/absolute paths.
INVALID_DAYS = [
    "",
    "14-08-2026",
    "2026/08/14",
    "2026-8-14",
    "14/08/2026",
    "2026-02-30",  # calendar-invalid (February 30)
    "2026-13-01",  # month out of range
    "2026-00-10",  # zero month
    "2026-08-00",  # zero day
    "..",
    "../..",
    "../../etc/passwd",
    "/etc/passwd",
    "/tmp/x",
    "2026-08-14/extra",
]


class TestPeriodHelpers:
    """Pure key helpers: day/week/quarter/year keys for sample datetimes."""

    def test_day_key(self):
        assert memory.day_key(datetime(2026, 8, 14, 15, 30)) == "2026-08-14"

    def test_week_key(self):
        # 2026-08-14 is a Friday, which falls in ISO week 33 of 2026
        # (Monday 2026-08-10 .. Sunday 2026-08-16).
        assert memory.week_key(datetime(2026, 8, 14, 12, 0)) == "2026-W33"

    def test_quarter_key(self):
        assert memory.quarter_key(datetime(2026, 8, 14)) == "2026-Q3"
        assert memory.quarter_key(datetime(2026, 1, 1)) == "2026-Q1"

    def test_year_key(self):
        assert memory.year_key(datetime(2026, 8, 14)) == "2026"

    def test_period_key_for_date_levels(self):
        dt = datetime(2026, 8, 14, 12, 0)
        assert memory.period_key_for_date(dt, "raw") == "2026-08-14"
        assert memory.period_key_for_date(dt, "daily") == "2026-08-14"
        assert memory.period_key_for_date(dt, "weekly") == "2026-W33"
        assert memory.period_key_for_date(dt, "quarterly") == "2026-Q3"
        assert memory.period_key_for_date(dt, "yearly") == "2026"

    def test_period_key_for_date_unknown_level(self):
        with pytest.raises(ValueError):
            memory.period_key_for_date(datetime(2026, 8, 14), "bogus")

    def test_count_words(self):
        assert memory.count_words("") == 0
        assert memory.count_words("  uno  dos  ") == 2
        assert memory.count_words("a b c") == 3


class TestRangeHelpers:
    """Range helpers produce half-open [start, end) epochs in a fixed tz."""

    def test_day_range(self):
        # A local day spans midnight-to-midnight in America/Caracas.
        start, end = memory.day_range("2026-08-14", TZ)
        assert start == datetime(2026, 8, 14, tzinfo=TZ).timestamp()
        assert end == datetime(2026, 8, 15, tzinfo=TZ).timestamp()
        assert start < end

    def test_week_range(self):
        # ISO week 33 of 2026 starts Monday 2026-08-10 and ends the
        # following Monday.
        start, end = memory.week_range("2026-W33", TZ)
        assert start == datetime(2026, 8, 10, tzinfo=TZ).timestamp()
        assert end == datetime(2026, 8, 17, tzinfo=TZ).timestamp()

    def test_quarter_range(self):
        # Q3 2026 spans July 1 .. October 1.
        start, end = memory.quarter_range("2026-Q3", TZ)
        assert start == datetime(2026, 7, 1, tzinfo=TZ).timestamp()
        assert end == datetime(2026, 10, 1, tzinfo=TZ).timestamp()

    def test_year_range(self):
        start, end = memory.year_range("2026", TZ)
        assert start == datetime(2026, 1, 1, tzinfo=TZ).timestamp()
        assert end == datetime(2027, 1, 1, tzinfo=TZ).timestamp()

    def test_bad_inputs_raise_value_error(self):
        with pytest.raises(ValueError):
            memory.day_range("2026-02-30", TZ)
        with pytest.raises(ValueError):
            memory.day_range("14-08-2026", TZ)
        with pytest.raises(ValueError):
            memory.week_range("2026-33", TZ)
        with pytest.raises(ValueError):
            memory.week_range("2026-W99", TZ)
        with pytest.raises(ValueError):
            memory.quarter_range("2026-Q5", TZ)
        with pytest.raises(ValueError):
            memory.year_range("2026-01", TZ)


class TestAppendRawEntry:
    """append_raw_entry: append-only Markdown lines + synced days metadata."""

    def test_append_creates_raw_file_with_timestamp(self, isolated_data_dir):
        entry = memory.append_raw_entry(
            1, "2026-08-14", "comí arepa", ts=TS, tz_name="America/Caracas"
        )
        assert entry is not None
        raw_path = isolated_data_dir / "memory" / "1" / "raw" / "2026-08-14.md"
        assert raw_path.exists()
        # HH:MM comes from the provided timezone: 15:45 local in Caracas.
        assert raw_path.read_text(encoding="utf-8") == "- [15:45] comí arepa\n"

    def test_append_formats_time_in_provided_tz(self, isolated_data_dir):
        # The same instant renders as 19:45 in UTC (Caracas is UTC-4).
        memory.append_raw_entry(1, "2026-08-14", "nota", ts=TS, tz_name="UTC")
        raw = (isolated_data_dir / "memory" / "1" / "raw" / "2026-08-14.md").read_text(
            encoding="utf-8"
        )
        assert raw == "- [19:45] nota\n"

    def test_append_unknown_tz_falls_back_to_utc(self, isolated_data_dir):
        # A bad timezone config must never block journaling (logged + UTC).
        memory.append_raw_entry(1, "2026-08-14", "nota", ts=TS, tz_name="Mars/Olympus")
        raw = (isolated_data_dir / "memory" / "1" / "raw" / "2026-08-14.md").read_text(
            encoding="utf-8"
        )
        assert raw == "- [19:45] nota\n"

    def test_word_count_matches_file(self):
        memory.append_raw_entry(1, "2026-08-14", "uno dos tres", ts=TS)
        day = memory.get_raw_day(1, "2026-08-14")
        content = memory.get_raw_content(1, "2026-08-14")
        assert day["raw_word_count"] == memory.count_words(content)
        # "- [19:45] uno dos tres" splits into 5 whitespace-separated tokens.
        assert day["raw_word_count"] == 5

    def test_entry_count_increments(self):
        memory.append_raw_entry(1, "2026-08-14", "primera", ts=TS)
        memory.append_raw_entry(1, "2026-08-14", "segunda", ts=TS + 60)
        day = memory.get_raw_day(1, "2026-08-14")
        assert day["entry_count"] == 2
        content = memory.get_raw_content(1, "2026-08-14")
        assert content.count("\n") == 2
        assert content.splitlines()[0].endswith("primera")
        assert content.splitlines()[1].endswith("segunda")

    def test_range_start_stays_range_end_moves(self):
        memory.append_raw_entry(1, "2026-08-14", "a", ts=TS)
        second = memory.append_raw_entry(1, "2026-08-14", "b", ts=TS + 3600)
        assert second["range_start"] == TS
        assert second["range_end"] == TS + 3600
        # created_at is only set on insert; updated_at moves with the entry.
        first = memory.get_raw_day(1, "2026-08-14")
        assert second["created_at"] == first["created_at"] == TS
        assert second["updated_at"] == TS + 3600


class TestGetRawDay:
    def test_get_raw_day_returns_metadata(self):
        memory.append_raw_entry(1, "2026-08-14", "hola", ts=TS)
        day = memory.get_raw_day(1, "2026-08-14")
        assert day is not None
        assert day["user_id"] == 1
        assert day["day"] == "2026-08-14"
        assert day["entry_count"] == 1
        assert day["raw_word_count"] == memory.count_words(
            memory.get_raw_content(1, "2026-08-14")
        )
        assert day["compact_path"] is None

    def test_get_raw_day_missing_returns_none(self):
        assert memory.get_raw_day(1, "2026-08-14") is None

    def test_get_raw_day_invalid_day_raises(self):
        with pytest.raises(ValueError):
            memory.get_raw_day(1, "2026-02-30")


class TestGetRawEntries:
    """get_raw_entries: ordered by day ASC, optional [day_from, day_to]."""

    def test_returns_ordered_content(self):
        for day in ("2026-08-10", "2026-08-12", "2026-08-14"):
            memory.append_raw_entry(1, day, "nota", ts=TS)
        entries = memory.get_raw_entries(1)
        assert [e["day"] for e in entries] == [
            "2026-08-10",
            "2026-08-12",
            "2026-08-14",
        ]
        assert entries[1]["content"].endswith("nota\n")
        assert entries[1]["entry_count"] == 1

    def test_filters_by_range(self):
        for day in ("2026-08-10", "2026-08-12", "2026-08-14"):
            memory.append_raw_entry(1, day, "x", ts=TS)
        entries = memory.get_raw_entries(1, day_from="2026-08-11", day_to="2026-08-13")
        assert [e["day"] for e in entries] == ["2026-08-12"]

    def test_empty_range_returns_empty_list(self):
        memory.append_raw_entry(1, "2026-08-14", "x", ts=TS)
        # Range completely after the stored day.
        assert memory.get_raw_entries(1, day_from="2026-08-15", day_to="2026-08-20") == []
        # Unknown user.
        assert memory.get_raw_entries(99) == []


class TestRawContentHelpers:
    def test_get_raw_content_missing_returns_empty(self):
        assert memory.get_raw_content(1, "2026-08-14") == ""

    def test_get_raw_content_returns_markdown(self):
        memory.append_raw_entry(1, "2026-08-14", "café con leche", ts=TS)
        content = memory.get_raw_content(1, "2026-08-14")
        assert content.startswith("- [")
        assert content.endswith("café con leche\n")

    def test_day_entries_are_empty(self):
        assert memory.day_entries_are_empty(1, "2026-08-14") is True
        memory.append_raw_entry(1, "2026-08-14", "algo", ts=TS)
        assert memory.day_entries_are_empty(1, "2026-08-14") is False

    def test_invalid_day_raises(self):
        with pytest.raises(ValueError):
            memory.day_entries_are_empty(1, "2026-02-30")
        with pytest.raises(ValueError):
            memory.get_raw_content(1, "../../etc/passwd")


class TestInvalidDays:
    """Day strings are the path-traversal gate: anything but YYYY-MM-DD raises."""

    @pytest.mark.parametrize("day", INVALID_DAYS)
    def test_append_raw_entry_rejects(self, day):
        with pytest.raises(ValueError):
            memory.append_raw_entry(1, day, "texto", ts=TS)

    @pytest.mark.parametrize("day", INVALID_DAYS)
    def test_save_daily_compact_rejects(self, day):
        with pytest.raises(ValueError):
            memory.save_daily_compact(1, day, "resumen")

    def test_invalid_day_never_creates_files(self, isolated_data_dir):
        # Validation happens before layout creation or any write, so a
        # crafted day cannot leave a file outside the memory folder.
        with pytest.raises(ValueError):
            memory.append_raw_entry(1, "../../etc/passwd", "x", ts=TS)
        assert not (isolated_data_dir / "memory").exists()
        assert not (isolated_data_dir / "etc").exists()


class TestDailyCompact:
    """save_daily_compact / get_daily_compact / get_daily_compact_metadata."""

    def test_save_writes_file_and_metadata(self, isolated_data_dir):
        memory.append_raw_entry(1, "2026-08-14", "comí arepa", ts=TS)
        before = memory.get_raw_day(1, "2026-08-14")["raw_word_count"]

        day = memory.save_daily_compact(1, "2026-08-14", "Resumen del día: arepa.")

        compact_path = isolated_data_dir / "memory" / "1" / "days" / "2026-08-14.md"
        assert day is not None
        assert compact_path.exists()
        assert compact_path.read_text(encoding="utf-8") == "Resumen del día: arepa."
        assert day["compact_path"] == str(compact_path.resolve())
        assert day["compact_word_count"] == 4
        # Compaction must not touch the raw statistics.
        assert day["entry_count"] == 1
        assert day["raw_word_count"] == before

    def test_get_daily_compact_returns_content(self):
        memory.save_daily_compact(1, "2026-08-14", "resumen")
        assert memory.get_daily_compact(1, "2026-08-14") == "resumen"

    def test_get_daily_compact_missing_returns_empty(self):
        assert memory.get_daily_compact(1, "2026-08-14") == ""

    def test_compact_without_raw_row_creates_row(self):
        # A compact can exist for a day that never had raw entries; the row
        # is created with empty raw fields.
        day = memory.save_daily_compact(1, "2026-08-14", "compacto sin raw")
        assert day is not None
        meta = memory.get_daily_compact_metadata(1, "2026-08-14")
        assert meta["raw_path"] == ""
        assert meta["raw_word_count"] == 0
        assert meta["entry_count"] == 0
        assert meta["compact_path"] is not None

    def test_get_daily_compact_metadata_missing_returns_none(self):
        assert memory.get_daily_compact_metadata(1, "2026-08-14") is None


class TestSaveSummary:
    """save_summary: file + row, and the exactly-one-current-per-level rule."""

    def test_save_weekly_writes_file(self, isolated_data_dir):
        summary = memory.save_summary(
            1,
            "weekly",
            "2026-W33",
            "Semana de viajes.",
            range_start=1000.0,
            range_end=2000.0,
        )
        assert summary is not None
        assert summary["level"] == "weekly"
        assert summary["period"] == "2026-W33"
        assert summary["status"] == "current"
        assert summary["word_count"] == 3
        path = isolated_data_dir / "memory" / "1" / "summaries" / "weekly" / "2026-W33.md"
        assert path.exists()
        assert path.read_text(encoding="utf-8") == "Semana de viajes."
        assert summary["content_path"] == str(path.resolve())

    def test_save_quarterly_and_yearly(self, isolated_data_dir):
        q = memory.save_summary(1, "quarterly", "2026-Q3", "Trimestre productivo")
        y = memory.save_summary(1, "yearly", "2026", "Año completo")
        assert q is not None and q["status"] == "current"
        assert y is not None and y["status"] == "current"
        assert (
            isolated_data_dir / "memory" / "1" / "summaries" / "quarterly" / "2026-Q3.md"
        ).exists()
        assert (isolated_data_dir / "memory" / "1" / "summaries" / "yearly" / "2026.md").exists()

    def test_new_summary_finalizes_previous_current(self):
        memory.save_summary(
            1, "weekly", "2026-W32", "vieja", range_start=1.0, range_end=1000.0
        )
        memory.save_summary(
            1, "weekly", "2026-W33", "nueva", range_start=1000.0, range_end=2000.0
        )
        assert memory.get_summary(1, "weekly", "2026-W32")["status"] == "finalized"
        assert memory.get_summary(1, "weekly", "2026-W33")["status"] == "current"
        # Exactly one current summary per level after every save.
        currents = [
            s for s in memory.list_summaries(1, "weekly") if s["status"] == "current"
        ]
        assert len(currents) == 1

    def test_exactly_one_current_per_level_independent(self):
        memory.save_summary(1, "weekly", "2026-W32", "sem")
        memory.save_summary(1, "weekly", "2026-W33", "sem2")
        memory.save_summary(1, "quarterly", "2026-Q2", "trim")
        memory.save_summary(1, "quarterly", "2026-Q3", "trim2")
        for level in ("weekly", "quarterly"):
            currents = [
                s for s in memory.list_summaries(1, level) if s["status"] == "current"
            ]
            assert len(currents) == 1

    def test_invalid_level_raises(self):
        with pytest.raises(ValueError):
            memory.save_summary(1, "daily", "2026-08-14", "no")
        with pytest.raises(ValueError):
            memory.save_summary(1, "bogus", "x", "no")

    @pytest.mark.parametrize(
        "level,period",
        [
            ("weekly", "2026-33"),
            ("weekly", "2026-W"),
            ("weekly", "2026-W1"),
            ("weekly", "2026-W33x"),
            ("weekly", "../../etc/passwd"),
            ("quarterly", "2026-Q5"),
            ("quarterly", "2026-Q0"),
            ("quarterly", "2026-3"),
            ("quarterly", "../x"),
            ("yearly", "2026-01"),
            ("yearly", "abcd"),
            ("yearly", "/etc/passwd"),
        ],
    )
    def test_invalid_period_shape_raises(self, level, period):
        with pytest.raises(ValueError):
            memory.save_summary(1, level, period, "contenido")


class TestGetSummary:
    """get_summary: exact period, or latest by range_end when period is None."""

    def test_get_by_period(self):
        memory.save_summary(
            1, "weekly", "2026-W33", "contenido", range_start=1000.0, range_end=2000.0
        )
        summary = memory.get_summary(1, "weekly", "2026-W33")
        assert summary is not None
        assert summary["period"] == "2026-W33"

    def test_get_latest_by_range_end_when_period_none(self):
        memory.save_summary(
            1, "weekly", "2026-W32", "vieja", range_start=1.0, range_end=1000.0
        )
        memory.save_summary(
            1, "weekly", "2026-W33", "nueva", range_start=1000.0, range_end=2000.0
        )
        latest = memory.get_summary(1, "weekly")
        assert latest is not None
        assert latest["period"] == "2026-W33"

    def test_get_missing_returns_none(self):
        assert memory.get_summary(1, "weekly", "2026-W33") is None
        assert memory.get_summary(1, "weekly") is None

    def test_unknown_level_raises(self):
        with pytest.raises(ValueError):
            memory.get_summary(1, "bogus", "2026-W33")

    def test_get_summary_content_returns_content_or_empty(self):
        memory.save_summary(1, "quarterly", "2026-Q3", "texto del trimestre")
        assert memory.get_summary_content(1, "quarterly", "2026-Q3") == "texto del trimestre"
        assert memory.get_summary_content(1, "quarterly", "2026-Q4") == ""

    def test_get_summary_content_invalid_period_raises(self):
        with pytest.raises(ValueError):
            memory.get_summary_content(1, "weekly", "../x")


class TestListSummaries:
    """list_summaries: oldest range first, optional level filter."""

    def test_orders_by_range_end_ascending(self):
        memory.save_summary(
            1, "weekly", "2026-W33", "b", range_start=1000.0, range_end=2000.0
        )
        memory.save_summary(
            1, "weekly", "2026-W32", "a", range_start=1.0, range_end=1000.0
        )
        periods = [s["period"] for s in memory.list_summaries(1)]
        assert periods == ["2026-W32", "2026-W33"]

    def test_filters_by_level(self):
        memory.save_summary(
            1, "weekly", "2026-W33", "semana", range_start=1.0, range_end=1000.0
        )
        memory.save_summary(
            1, "yearly", "2026", "año", range_start=1.0, range_end=2000.0
        )
        assert [s["period"] for s in memory.list_summaries(1, "weekly")] == ["2026-W33"]
        assert [s["period"] for s in memory.list_summaries(1, "yearly")] == ["2026"]

    def test_empty(self):
        assert memory.list_summaries(1) == []

    def test_unknown_level_raises(self):
        with pytest.raises(ValueError):
            memory.list_summaries(1, "bogus")


class TestFinalizeSummary:
    """finalize_summary: marks a row finalized; idempotent; False when absent."""

    def test_marks_finalized(self):
        memory.save_summary(
            1, "weekly", "2026-W33", "x", range_start=1.0, range_end=1000.0
        )
        assert memory.finalize_summary(1, "weekly", "2026-W33") is True
        assert memory.get_summary(1, "weekly", "2026-W33")["status"] == "finalized"

    def test_missing_row_returns_false(self):
        assert memory.finalize_summary(1, "weekly", "2026-W33") is False

    def test_idempotent(self):
        memory.save_summary(1, "weekly", "2026-W33", "x")
        assert memory.finalize_summary(1, "weekly", "2026-W33") is True
        # Finalizing an already-finalized summary neither raises nor
        # corrupts the row.
        assert memory.finalize_summary(1, "weekly", "2026-W33") is True
        assert memory.get_summary(1, "weekly", "2026-W33")["status"] == "finalized"

    def test_invalid_level_or_period_raises(self):
        with pytest.raises(ValueError):
            memory.finalize_summary(1, "bogus", "x")
        with pytest.raises(ValueError):
            memory.finalize_summary(1, "weekly", "../x")


class TestFindSummaryCovering:
    """find_summary_covering: strict cover by ts, latest fallback otherwise."""

    def _save_two_weekly(self):
        memory.save_summary(
            1, "weekly", "2026-W32", "a", range_start=1000.0, range_end=2000.0
        )
        memory.save_summary(
            1, "weekly", "2026-W33", "b", range_start=2000.0, range_end=3000.0
        )

    def test_strict_cover_by_ts(self):
        self._save_two_weekly()
        assert memory.find_summary_covering(1, 1500.0, "weekly", "UTC")["period"] == "2026-W32"
        assert memory.find_summary_covering(1, 2500.0, "weekly", "UTC")["period"] == "2026-W33"

    def test_latest_fallback_when_beyond_ranges(self):
        self._save_two_weekly()
        # Before every range and after every range → latest by range_end.
        assert memory.find_summary_covering(1, 500.0, "weekly", "UTC")["period"] == "2026-W33"
        assert memory.find_summary_covering(1, 3500.0, "weekly", "UTC")["period"] == "2026-W33"

    def test_none_when_no_summaries(self):
        assert memory.find_summary_covering(1, 1500.0, "weekly", "UTC") is None

    def test_unknown_level_raises(self):
        with pytest.raises(ValueError):
            memory.find_summary_covering(1, 1500.0, "bogus", "UTC")


class TestUserIsolation:
    """Each user's journal lives under its own folder and user_id-scoped rows."""

    def test_raw_data_invisible_to_other_user(self):
        memory.append_raw_entry(1, "2026-08-14", "secreto de uno", ts=TS)
        assert memory.get_raw_day(2, "2026-08-14") is None
        assert memory.get_raw_entries(2) == []
        assert memory.get_raw_content(2, "2026-08-14") == ""
        assert memory.day_entries_are_empty(2, "2026-08-14") is True

    def test_summaries_invisible_to_other_user(self):
        memory.save_summary(1, "weekly", "2026-W33", "privado")
        assert memory.list_summaries(2) == []
        assert memory.get_summary(2, "weekly", "2026-W33") is None
        assert memory.get_summary_content(2, "weekly", "2026-W33") == ""

    def test_files_under_separate_folders(self, isolated_data_dir):
        memory.append_raw_entry(1, "2026-08-14", "a", ts=TS)
        memory.save_summary(1, "weekly", "2026-W33", "b")
        user1 = isolated_data_dir / "memory" / "1"
        assert (user1 / "raw" / "2026-08-14.md").exists()
        assert (user1 / "summaries" / "weekly" / "2026-W33.md").exists()
        # User 2's folder simply does not exist yet.
        assert not (isolated_data_dir / "memory" / "2").exists()

    def test_same_day_different_users_do_not_collide(self):
        memory.append_raw_entry(1, "2026-08-14", "de uno", ts=TS)
        memory.append_raw_entry(2, "2026-08-14", "de dos", ts=TS)
        assert memory.get_raw_content(1, "2026-08-14").endswith("de uno\n")
        assert memory.get_raw_content(2, "2026-08-14").endswith("de dos\n")


class TestPathTraversalUserId:
    """user_id-derived paths cannot escape MEMORY_DIR (guard: _safe_under_memory)."""

    def test_dotdot_relative_raises(self):
        with pytest.raises(ValueError):
            memory._safe_under_memory(1, "../../etc/passwd")

    def test_absolute_relative_raises(self):
        with pytest.raises(ValueError):
            memory._safe_under_memory(1, "/etc/passwd")

    def test_deep_escape_raises(self):
        with pytest.raises(ValueError):
            memory._safe_under_memory(1, "raw/../../user2/secret.md")

    def test_dotdot_user_id_raises(self):
        # A '..' user_id resolves to the parent of MEMORY_DIR itself.
        with pytest.raises(ValueError):
            memory._safe_under_memory("..", "raw/x.md")

    def test_absolute_user_id_raises(self):
        with pytest.raises(ValueError):
            memory._safe_under_memory("/tmp/evil", "raw/x.md")

    def test_negative_user_id_stays_inside_memory_dir(self):
        # A negative id is a plain folder name under MEMORY_DIR — benign —
        # but it can still not climb out via '..'.
        inside = memory._safe_under_memory(-1, "raw/2026-08-14.md")
        assert inside.is_relative_to(memory.MEMORY_DIR.resolve())
        with pytest.raises(ValueError):
            memory._safe_under_memory(-1, "../../x.md")
