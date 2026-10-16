"""Unit tests for bot.summarizer — prompt templates, lazy hierarchy, API errors.

The summarizer turns raw memory into hierarchical summaries (daily compact,
weekly, quarterly, yearly) through the chat model and bot.memory. Every
test that reaches the model monkeypatches ``summarizer.chat_completion``
(the module imports it into its own namespace from bot.ai_client) with an
async recording stub, so no network is ever touched. The autouse
``isolated_data_dir`` fixture (tests/conftest.py) points bot.memory at a
fresh tmp directory per test; fixed timestamps and the fixed
"America/Caracas" timezone keep everything deterministic.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot import memory
from bot import summarizer

# Fixed timezone and instant (America/Caracas is UTC-4, no DST).
TZ = ZoneInfo("America/Caracas")
TZ_NAME = "America/Caracas"
TS = datetime(2026, 8, 14, 15, 45, tzinfo=TZ).timestamp()

# Content the fake model returns; generation tests assert it is persisted.
FAKE_CONTENT = "Resumen de prueba generado por el modelo."


class FakeChat:
    """Async stand-in for summarizer.chat_completion.

    Records every call and every prompt (so tests can assert what the model
    was asked), and can be configured to raise an API error on each call —
    the error classes come from summarizer's own namespace, matching what
    _call_model catches.
    """

    def __init__(self, content: str = FAKE_CONTENT):
        self.content = content
        self.calls = 0
        self.prompts: list[str] = []
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
        self.calls += 1
        self.prompts.append(messages[0]["content"] if messages else "")
        if self.error is not None:
            raise self.error
        return self.content


@pytest.fixture
def fake_chat(monkeypatch) -> FakeChat:
    """Patch summarizer.chat_completion with a recording stub."""
    fake = FakeChat()
    monkeypatch.setattr(summarizer, "chat_completion", fake)
    return fake


def _seed_raw(user_id: int, day: str, text: str = "comí arepa y fui al gimnasio") -> None:
    """Append a raw journal entry for ``day`` at the fixed instant."""
    memory.append_raw_entry(user_id, day, text, ts=TS, tz_name=TZ_NAME)


# --- Part 1: prompt builder -------------------------------------------------


class TestBuildSummaryPrompt:
    """build_summary_prompt: fixed structure, ordered sections, child pointers."""

    def test_contains_all_11_sections_in_fixed_order(self):
        prompt = summarizer.build_summary_prompt(
            "weekly", "2026-W33", [{"period": "2026-08-10", "content": "x"}]
        )
        # The template joins SUMMARY_SECTIONS verbatim, so their headings
        # must appear in exactly the module order (single source of truth).
        positions = [prompt.index(f"## {name}") for name in summarizer.SUMMARY_SECTIONS]
        assert positions == sorted(positions)
        assert len(summarizer.SUMMARY_SECTIONS) == 11

    def test_contains_required_structural_headings(self):
        prompt = summarizer.build_summary_prompt(
            "weekly", "2026-W33", [{"period": "2026-08-10", "content": "x"}]
        )
        assert "## Eventos con fecha" in prompt
        assert "## Patrones y rachas" in prompt
        assert "## Referencias al nivel hijo" in prompt

    def test_includes_source_contents_and_period_keys(self):
        sources = [
            {"period": "2026-08-10", "content": "fui a la playa"},
            {"period": "2026-08-12", "content": "pagé el teléfono"},
        ]
        prompt = summarizer.build_summary_prompt("weekly", "2026-W33", sources)
        assert "--- Fuente: 2026-08-10 ---" in prompt
        assert "fui a la playa" in prompt
        assert "--- Fuente: 2026-08-12 ---" in prompt
        assert "pagé el teléfono" in prompt

    def test_metadata_line_lists_range_and_counts(self):
        prompt = summarizer.build_summary_prompt(
            "weekly", "2026-W33", [{"period": "2026-08-10", "content": "x"}]
        )
        assert "Metadatos: rango 2026-08-10 al 2026-08-16" in prompt
        assert "1 fuente" in prompt
        assert "7 unidades cubiertas" in prompt

    def test_child_pointers_list_the_week_days(self):
        prompt = summarizer.build_summary_prompt("weekly", "2026-W33", [])
        for day in ("2026-08-10", "2026-08-16"):
            assert f"- {day}" in prompt

    def test_invalid_level_raises(self):
        with pytest.raises(ValueError, match="Unknown summary level"):
            summarizer.build_summary_prompt("bogus", "2026-W33", [])

    def test_daily_accepts_plain_string_source(self):
        # A daily compact has no child units; the raw day text is passed as
        # a plain string and normalized to a single labelled source.
        prompt = summarizer.build_summary_prompt("daily", "2026-08-14", "texto crudo del día")
        assert "--- Fuente: 2026-08-14 ---" in prompt
        assert "texto crudo del día" in prompt


# --- Part 1: pure period helpers --------------------------------------------


class TestDaysInWeek:
    """days_in_week: ISO week expansion into 7 calendar days."""

    def test_expands_iso_week_to_seven_days(self):
        # 2026-W33 is Monday 2026-08-10 .. Sunday 2026-08-16.
        assert summarizer.days_in_week("2026-W33") == [
            "2026-08-10",
            "2026-08-11",
            "2026-08-12",
            "2026-08-13",
            "2026-08-14",
            "2026-08-15",
            "2026-08-16",
        ]

    @pytest.mark.parametrize(
        "bad",
        ["", "2026-33", "2026-W1", "2026-W0", "2026-W99", "2026-W00", "abcd-W33"],
    )
    def test_invalid_week_raises(self, bad):
        with pytest.raises(ValueError):
            summarizer.days_in_week(bad)


class TestWeeksInQuarter:
    """weeks_in_quarter: ascending ISO weeks of a quarter (computed, not fixed)."""

    def test_contains_w33_and_is_ascending(self):
        weeks = summarizer.weeks_in_quarter("2026-Q3")
        assert "2026-W33" in weeks
        assert weeks == sorted(weeks)
        assert len(weeks) in (13, 14)

    @pytest.mark.parametrize("bad", ["", "2026-3", "2026-Q0", "2026-Q5", "2026-Q33"])
    def test_invalid_quarter_raises(self, bad):
        with pytest.raises(ValueError):
            summarizer.weeks_in_quarter(bad)


class TestQuartersInYear:
    """quarters_in_year: the 4 fixed quarter keys of a year."""

    def test_returns_q1_to_q4(self):
        assert summarizer.quarters_in_year("2026") == [
            "2026-Q1",
            "2026-Q2",
            "2026-Q3",
            "2026-Q4",
        ]

    @pytest.mark.parametrize("bad", ["", "abcd", "2026-01", "20", None])
    def test_invalid_year_raises(self, bad):
        with pytest.raises(ValueError):
            summarizer.quarters_in_year(bad)


class TestCleanModelOutput:
    """clean_model_output: strips a wrapping markdown fence, keeps plain text."""

    def test_strips_markdown_fence(self):
        raw = "```markdown\n# Resumen semana\n## Salud\n```"
        assert summarizer.clean_model_output(raw) == "# Resumen semana\n## Salud"

    def test_strips_bare_fence(self):
        assert summarizer.clean_model_output("```\ncontenido\n```") == "contenido"

    def test_plain_text_left_untouched(self):
        assert summarizer.clean_model_output("  texto plano  ") == "texto plano"
        assert summarizer.clean_model_output("sin fence") == "sin fence"


# --- Part 2: daily compact ---------------------------------------------------


class TestGenerateDailyCompact:
    """generate_daily_compact: threshold gate, persistence, idempotency."""

    async def test_below_threshold_returns_none_and_no_file(self, isolated_data_dir, fake_chat):
        memory.append_raw_entry(1, "2026-08-14", "pocas palabras", ts=TS, tz_name=TZ_NAME)
        result = await summarizer.generate_daily_compact(
            1, "2026-08-14", threshold=1000, client=None, model="m", tz_name=TZ_NAME
        )
        assert result is None
        assert fake_chat.calls == 0
        compact = isolated_data_dir / "memory" / "1" / "days" / "2026-08-14.md"
        assert not compact.exists()

    async def test_above_threshold_calls_model_and_writes_compact(
        self, isolated_data_dir, fake_chat
    ):
        fake_chat.content = "```markdown\n# Compacto 2026-08-14\n```"
        memory.append_raw_entry(1, "2026-08-14", "palabra " * 50, ts=TS, tz_name=TZ_NAME)
        result = await summarizer.generate_daily_compact(
            1, "2026-08-14", threshold=20, client=None, model="m", tz_name=TZ_NAME
        )
        assert fake_chat.calls == 1
        # The markdown fence is cleaned before persistence.
        assert result == "# Compacto 2026-08-14"
        compact = isolated_data_dir / "memory" / "1" / "days" / "2026-08-14.md"
        assert compact.exists()
        assert compact.read_text(encoding="utf-8") == "# Compacto 2026-08-14"
        meta = memory.get_daily_compact_metadata(1, "2026-08-14")
        assert meta["compact_path"] == str(compact.resolve())
        # The model was asked to summarize exactly this day.
        assert "--- Fuente: 2026-08-14 ---" in fake_chat.prompts[0]

    async def test_idempotent_second_call_does_not_call_model(self, isolated_data_dir, fake_chat):
        memory.append_raw_entry(1, "2026-08-14", "palabra " * 50, ts=TS, tz_name=TZ_NAME)
        first = await summarizer.generate_daily_compact(
            1, "2026-08-14", threshold=20, client=None, model="m", tz_name=TZ_NAME
        )
        second = await summarizer.generate_daily_compact(
            1, "2026-08-14", threshold=20, client=None, model="m", tz_name=TZ_NAME
        )
        assert first == second == fake_chat.content
        assert fake_chat.calls == 1

    async def test_empty_raw_content_returns_none(self, isolated_data_dir, fake_chat):
        memory.append_raw_entry(1, "2026-08-14", "palabra " * 50, ts=TS, tz_name=TZ_NAME)
        # Metadata still reports the day above threshold, but the raw file is
        # now empty (simulates tampering): no model call, nothing persisted.
        raw = isolated_data_dir / "memory" / "1" / "raw" / "2026-08-14.md"
        raw.write_text("", encoding="utf-8")
        result = await summarizer.generate_daily_compact(
            1, "2026-08-14", threshold=20, client=None, model="m", tz_name=TZ_NAME
        )
        assert result is None
        assert fake_chat.calls == 0
        assert memory.get_daily_compact(1, "2026-08-14") == ""

    async def test_api_failure_returns_none_and_logs(self, isolated_data_dir, monkeypatch, caplog):
        fake = FakeChat()
        fake.error = summarizer.OpenCodeError("API caída")
        monkeypatch.setattr(summarizer, "chat_completion", fake)
        memory.append_raw_entry(1, "2026-08-14", "palabra " * 50, ts=TS, tz_name=TZ_NAME)
        with caplog.at_level(logging.WARNING, logger="bot.summarizer"):
            result = await summarizer.generate_daily_compact(
                1, "2026-08-14", threshold=20, client=None, model="m", tz_name=TZ_NAME
            )
        assert result is None
        assert "Model call failed for a summary" in caplog.text
        assert memory.get_daily_compact(1, "2026-08-14") == ""


# --- Part 2: weekly summary --------------------------------------------------


class TestGenerateWeeklySummary:
    """generate_weekly_summary: reads the week's days, saves one current row."""

    async def test_builds_from_day_raw_content(self, isolated_data_dir, fake_chat):
        _seed_raw(1, "2026-08-10", "viaje a Panamá")
        _seed_raw(1, "2026-08-12", "pagé el teléfono")
        result = await summarizer.generate_weekly_summary(
            1, "2026-W33", client=None, model="m", tz_name=TZ_NAME
        )
        assert result == fake_chat.content
        prompt = fake_chat.prompts[0]
        assert "--- Fuente: 2026-08-10 ---" in prompt
        assert "viaje a Panamá" in prompt
        assert "--- Fuente: 2026-08-12 ---" in prompt
        assert "pagé el teléfono" in prompt
        assert "rango 2026-08-10 al 2026-08-16" in prompt

    async def test_prefers_compact_over_raw(self, isolated_data_dir, fake_chat):
        _seed_raw(1, "2026-08-10", "texto crudo del día")
        memory.save_daily_compact(1, "2026-08-10", "texto compacto del día")
        await summarizer.generate_weekly_summary(
            1, "2026-W33", client=None, model="m", tz_name=TZ_NAME
        )
        prompt = fake_chat.prompts[0]
        assert "texto compacto del día" in prompt
        assert "texto crudo del día" not in prompt

    async def test_saves_summary_row_current_with_week_range(
        self, isolated_data_dir, fake_chat
    ):
        _seed_raw(1, "2026-08-10", "viaje a Panamá")
        await summarizer.generate_weekly_summary(
            1, "2026-W33", client=None, model="m", tz_name=TZ_NAME
        )
        row = memory.get_summary(1, "weekly", "2026-W33")
        assert row is not None
        assert row["status"] == "current"
        start, end = memory.week_range("2026-W33", TZ)
        assert row["range_start"] == start
        assert row["range_end"] == end
        assert memory.get_summary_content(1, "weekly", "2026-W33") == fake_chat.content

    async def test_no_day_content_returns_none(self, isolated_data_dir, fake_chat):
        result = await summarizer.generate_weekly_summary(
            1, "2026-W33", client=None, model="m", tz_name=TZ_NAME
        )
        assert result is None
        assert fake_chat.calls == 0
        assert memory.get_summary(1, "weekly", "2026-W33") is None


# --- Part 2: quarterly / yearly (hierarchy) ----------------------------------


class TestGenerateQuarterlySummary:
    """generate_quarterly_summary: lazily ensures child weeks, then builds."""

    async def test_lazily_ensures_weeks_then_builds_quarter(
        self, isolated_data_dir, fake_chat
    ):
        # Only 2026-08-10 (Monday of ISO week 33) has content, so the
        # quarterly run lazily generates that week's summary first.
        _seed_raw(1, "2026-08-10", "viaje a Panamá")
        result = await summarizer.generate_quarterly_summary(
            1, "2026-Q3", client=None, model="m", tz_name=TZ_NAME
        )
        assert result == fake_chat.content
        week_row = memory.get_summary(1, "weekly", "2026-W33")
        assert week_row is not None
        assert memory.get_summary_content(1, "weekly", "2026-W33") == fake_chat.content
        q_row = memory.get_summary(1, "quarterly", "2026-Q3")
        assert q_row is not None
        assert q_row["status"] == "current"
        start, end = memory.quarter_range("2026-Q3", TZ)
        assert q_row["range_start"] == start
        assert q_row["range_end"] == end

    async def test_child_pointers_in_quarter_prompt(self, isolated_data_dir, fake_chat):
        _seed_raw(1, "2026-08-10", "viaje a Panamá")
        await summarizer.generate_quarterly_summary(
            1, "2026-Q3", client=None, model="m", tz_name=TZ_NAME
        )
        quarter_prompt = fake_chat.prompts[-1]
        assert "## Referencias al nivel hijo" in quarter_prompt
        assert "- 2026-W33" in quarter_prompt
        # The ensured week summary is one of the quarter's sources.
        assert "--- Fuente: 2026-W33 ---" in quarter_prompt


class TestGenerateYearlySummary:
    """generate_yearly_summary: lazily ensures quarters, then builds the year."""

    async def test_lazily_ensures_quarters_then_builds_year(
        self, isolated_data_dir, fake_chat
    ):
        # 2026-01-05 is the Monday of ISO week 2026-W02 (Q1).
        _seed_raw(1, "2026-01-05", "empecé el año en el gimnasio")
        result = await summarizer.generate_yearly_summary(
            1, "2026", client=None, model="m", tz_name=TZ_NAME
        )
        assert result == fake_chat.content
        assert memory.get_summary(1, "weekly", "2026-W02") is not None
        assert memory.get_summary(1, "quarterly", "2026-Q1") is not None
        y_row = memory.get_summary(1, "yearly", "2026")
        assert y_row is not None
        assert y_row["status"] == "current"
        start, end = memory.year_range("2026", TZ)
        assert y_row["range_start"] == start
        assert y_row["range_end"] == end

    async def test_child_pointers_in_year_prompt(self, isolated_data_dir, fake_chat):
        _seed_raw(1, "2026-01-05", "empecé el año en el gimnasio")
        await summarizer.generate_yearly_summary(
            1, "2026", client=None, model="m", tz_name=TZ_NAME
        )
        year_prompt = fake_chat.prompts[-1]
        assert "## Referencias al nivel hijo" in year_prompt
        for quarter in ("2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"):
            assert f"- {quarter}" in year_prompt
        assert "--- Fuente: 2026-Q1 ---" in year_prompt


# --- Part 2: lazy ensure_summary ---------------------------------------------


class TestEnsureSummary:
    """ensure_summary: returns existing rows, generates missing ones lazily."""

    async def test_existing_with_content_no_model_call(self, isolated_data_dir, fake_chat):
        memory.save_summary(
            1, "weekly", "2026-W33", "contenido existente", range_start=1.0, range_end=2.0
        )
        row = await summarizer.ensure_summary(
            1, "weekly", "2026-W33", client=None, model="m", tz_name=TZ_NAME
        )
        assert row is not None
        assert row["period"] == "2026-W33"
        assert fake_chat.calls == 0

    async def test_missing_generates_and_saves(self, isolated_data_dir, fake_chat):
        _seed_raw(1, "2026-08-10", "viaje a Panamá")
        row = await summarizer.ensure_summary(
            1, "weekly", "2026-W33", client=None, model="m", tz_name=TZ_NAME
        )
        assert row is not None
        assert fake_chat.calls == 1
        assert memory.get_summary_content(1, "weekly", "2026-W33") == fake_chat.content

    async def test_unknown_level_raises(self, isolated_data_dir, fake_chat):
        with pytest.raises(ValueError, match="Unknown summary level"):
            await summarizer.ensure_summary(
                1, "bogus", "x", client=None, model="m", tz_name=TZ_NAME
            )

    async def test_generation_failure_returns_none(self, isolated_data_dir, monkeypatch):
        fake = FakeChat()
        fake.error = summarizer.OpenCodeError("boom")
        monkeypatch.setattr(summarizer, "chat_completion", fake)
        _seed_raw(1, "2026-08-10", "viaje a Panamá")
        row = await summarizer.ensure_summary(
            1, "weekly", "2026-W33", client=None, model="m", tz_name=TZ_NAME
        )
        assert row is None
        assert memory.get_summary(1, "weekly", "2026-W33") is None


# --- Part 2: API error handling ----------------------------------------------


class TestAPIErrorHandling:
    """Generation degrades gracefully: OpenCodeError / RateLimitError → None."""

    @pytest.mark.parametrize(
        "exc_cls",
        [summarizer.OpenCodeError, summarizer.RateLimitError],
    )
    async def test_generation_returns_none_and_logs(
        self, isolated_data_dir, monkeypatch, caplog, exc_cls
    ):
        fake = FakeChat()
        fake.error = exc_cls("API caída")
        monkeypatch.setattr(summarizer, "chat_completion", fake)
        _seed_raw(1, "2026-08-10", "viaje a Panamá")
        with caplog.at_level(logging.WARNING, logger="bot.summarizer"):
            result = await summarizer.generate_weekly_summary(
                1, "2026-W33", client=None, model="m", tz_name=TZ_NAME
            )
        assert result is None
        assert memory.get_summary(1, "weekly", "2026-W33") is None
        assert "Model call failed for a summary" in caplog.text
        assert exc_cls.__name__ in caplog.text

    async def test_transient_failure_then_success(self, isolated_data_dir, monkeypatch):
        # A flaky API must not crash the job: a later run can still generate.
        calls = {"n": 0}

        async def flaky_chat(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise summarizer.OpenCodeError("transitorio")
            return "resumen recuperado"

        monkeypatch.setattr(summarizer, "chat_completion", flaky_chat)
        _seed_raw(1, "2026-08-10", "viaje a Panamá")
        first = await summarizer.generate_weekly_summary(
            1, "2026-W33", client=None, model="m", tz_name=TZ_NAME
        )
        assert first is None
        second = await summarizer.generate_weekly_summary(
            1, "2026-W33", client=None, model="m", tz_name=TZ_NAME
        )
        assert second == "resumen recuperado"
        assert memory.get_summary(1, "weekly", "2026-W33") is not None


# --- Part 2: per-user isolation -----------------------------------------------


class TestUserIsolation:
    """Summaries are per-user: user B never sees user A's generated content."""

    async def test_summary_of_user_a_invisible_to_user_b(self, isolated_data_dir, fake_chat):
        _seed_raw(1, "2026-08-10", "viaje a Panamá")
        result = await summarizer.generate_weekly_summary(
            1, "2026-W33", client=None, model="m", tz_name=TZ_NAME
        )
        assert result is not None
        assert memory.get_summary(2, "weekly", "2026-W33") is None
        assert memory.get_summary_content(2, "weekly", "2026-W33") == ""
        assert memory.list_summaries(2) == []

    async def test_user_b_generation_does_not_see_user_a_sources(
        self, isolated_data_dir, fake_chat
    ):
        _seed_raw(1, "2026-08-10", "viaje a Panamá")
        result_a = await summarizer.generate_weekly_summary(
            1, "2026-W33", client=None, model="m", tz_name=TZ_NAME
        )
        assert result_a == fake_chat.content
        result_b = await summarizer.generate_weekly_summary(
            2, "2026-W33", client=None, model="m", tz_name=TZ_NAME
        )
        assert result_b is None
        # The model was called exactly once, for user A only.
        assert fake_chat.calls == 1
        assert memory.list_summaries(2) == []
