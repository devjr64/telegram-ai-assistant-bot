"""Integration tests for bot.handlers — commands, chat loop, rate limiting.

Uses FakeUpdate/FakeContext and mocks the AI client to avoid any network.
The access gate is patched via ``bot.handlers.is_authorized`` (the module
imports it by name), so each test controls authorization explicitly.
"""

import dataclasses
import json
import time

import pytest

import bot.handlers as handlers
from bot import history, reminders
from bot.projects import create_project, resolve_username

from fakes import FakeUpdate, FakeContext, FakeCallbackQuery, FakeMessage


def make_context(settings, ai_client=None, args=None):
    return FakeContext(settings, ai_client=ai_client, args=args)


def authorize(monkeypatch, value=True):
    monkeypatch.setattr(handlers, "is_authorized", lambda uid: value)


class TestStartCommand:
    async def test_welcome_message(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings)
        await handlers.start_command(update, context)
        assert update.message.replies
        text = update.message.replies[0]["text"]
        assert "¡Hola!" in text
        assert "/ayuda" in text
        assert "/proyectos" in text
        assert "/nuevoproyecto" in text
        assert "/reiniciar" in text

    async def test_start_uses_spanish_commands_only(self, sample_settings, monkeypatch):
        # Arrange: an authorized user invokes /start.
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings)

        # Act: run the start command.
        await handlers.start_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: the welcome message only references Spanish command names.
        english_project_commands = ("/projects", "/newproject", "/project",
                                    "/projectstatus", "/projectpriority",
                                    "/addnote", "/searchproject")
        for cmd in english_project_commands:
            assert cmd not in text

    async def test_denies_unauthorized(self, sample_settings, monkeypatch):
        authorize(monkeypatch, value=False)
        update = FakeUpdate()
        context = make_context(sample_settings)
        await handlers.start_command(update, context)
        assert "no estás autorizado" in update.message.replies[0]["text"]

    async def test_start_registers_user_profile(self, sample_settings, monkeypatch):
        # Arrange: a command-only user (no free-text) with a Telegram username.
        authorize(monkeypatch)
        update = FakeUpdate(user_id=123)
        update.effective_user.username = "jose"
        update.effective_user.first_name = "Jose"
        context = make_context(sample_settings)

        # Act: run /start.
        await handlers.start_command(update, context)

        # Assert: the profile is recorded so @jose can be invited to projects.
        assert resolve_username("@jose") == 123


class TestHelpCommand:
    async def test_lists_commands_in_spanish_by_category(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings)
        await handlers.help_command(update, context)
        text = update.message.replies[0]["text"]
        assert "/ayuda" in text
        assert "/proyectos" in text
        assert "/nuevoproyecto" in text
        assert "/estadoproyecto" in text
        assert "/prioridadproyecto" in text
        assert "*General:*" in text
        assert "*Proyectos:*" in text
        assert "*Recordatorios:*" in text
        assert "*Multimedia:*" in text

    async def test_help_lists_all_spanish_project_commands(self, sample_settings, monkeypatch):
        # Arrange: an authorized user invokes /ayuda.
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings)

        # Act: run the help command.
        await handlers.help_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: every Spanish project command is present in the help text.
        spanish_project_commands = (
            "/proyectos",
            "/nuevoproyecto",
            "/proyecto",
            "/estadoproyecto",
            "/prioridadproyecto",
            "/agregarnota",
            "/buscarproyecto",
            "/eliminarproyecto",
        )
        for cmd in spanish_project_commands:
            assert cmd in text, f"{cmd} should appear in /ayuda"

    async def test_mentions_natural_language(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings)
        await handlers.help_command(update, context)
        text = update.message.replies[0]["text"]
        assert "texto natural" in text

    async def test_help_does_not_mention_model_provider_switching(
        self, sample_settings, monkeypatch
    ):
        # Model/provider switching commands were removed.
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings)
        await handlers.help_command(update, context)
        text = update.message.replies[0]["text"]
        assert "/modelo" not in text
        assert "/proveedor" not in text

    async def test_denies_unauthorized(self, sample_settings, monkeypatch):
        authorize(monkeypatch, value=False)
        update = FakeUpdate()
        context = make_context(sample_settings)
        await handlers.help_command(update, context)
        assert "no estás autorizado" in update.message.replies[0]["text"]


class TestInfoCommand:
    async def test_shows_provider_and_model(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings)
        await handlers.info_command(update, context)
        text = update.message.replies[0]["text"]
        assert "Proveedor" in text
        assert sample_settings.model in text


class TestRateLimit:
    async def test_second_message_immediate_is_limited(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate(text="hola")
        context = make_context(sample_settings, ai_client=object())

        # Mock the AI reply so no network happens.
        async def fake_tool_loop(**kwargs):
            return "respuesta de prueba"

        monkeypatch.setattr(handlers, "_run_ai_tool_loop", fake_tool_loop)

        await handlers.handle_message(update, context)
        assert not update.message.replies[0]["text"].startswith("Espera")

        # Second message within the window -> rate limited
        update2 = FakeUpdate(text="otra cosa")
        await handlers.handle_message(update2, context)
        assert "Espera" in update2.message.replies[0]["text"]

    async def test_rate_limiter_resets_after_window(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        async def fake_tool_loop(**kwargs):
            return "ok"

        monkeypatch.setattr(handlers, "_run_ai_tool_loop", fake_tool_loop)
        update = FakeUpdate(text="primero")
        context = make_context(sample_settings, ai_client=object())
        await handlers.handle_message(update, context)

        # Simulate time passing beyond the rate window.
        handlers._rate_limit_tracker[1] -= sample_settings.rate_limit_seconds + 1
        update2 = FakeUpdate(text="segundo")
        await handlers.handle_message(update2, context)
        assert "Espera" not in update2.message.replies[0]["text"]


class TestHandleMessage:
    async def test_stores_and_responds(self, sample_settings, monkeypatch):
        authorize(monkeypatch)

        async def fake_tool_loop(**kwargs):
            assert kwargs["user_id"] == 1
            return "respuesta IA"

        monkeypatch.setattr(handlers, "_run_ai_tool_loop", fake_tool_loop)

        update = FakeUpdate(text="Hola bot")
        context = make_context(sample_settings, ai_client=object())
        await handlers.handle_message(update, context)

        assert update.message.replies[0]["text"] == "respuesta IA"
        # user + assistant messages persisted
        assert len(history.get_history(1, 20)) == 2

    async def test_unauthorized_denied(self, sample_settings, monkeypatch):
        authorize(monkeypatch, value=False)
        update = FakeUpdate(text="hola")
        context = make_context(sample_settings)
        await handlers.handle_message(update, context)
        assert "no estás autorizado" in update.message.replies[0]["text"]

    async def test_rate_limit_error_replies(self, sample_settings, monkeypatch):
        from bot.ai_client import RateLimitError

        authorize(monkeypatch)

        async def fake_tool_loop(**kwargs):
            raise RateLimitError("busy")

        monkeypatch.setattr(handlers, "_run_ai_tool_loop", fake_tool_loop)
        update = FakeUpdate(text="hola")
        context = make_context(sample_settings, ai_client=object())
        await handlers.handle_message(update, context)
        assert "ocupado" in update.message.replies[0]["text"]

    async def test_generic_error_replies(self, sample_settings, monkeypatch):
        from bot.ai_client import OpenCodeError

        authorize(monkeypatch)

        async def fake_tool_loop(**kwargs):
            raise OpenCodeError("boom")

        monkeypatch.setattr(handlers, "_run_ai_tool_loop", fake_tool_loop)
        update = FakeUpdate(text="hola")
        context = make_context(sample_settings, ai_client=object())
        await handlers.handle_message(update, context)
        assert "problema" in update.message.replies[0]["text"]


class TestToolLoop:
    async def test_tool_loop_executes_and_returns_text(self, sample_settings, monkeypatch):
        """Model asks for create_project, tool runs, model then replies text."""
        authorize(monkeypatch)
        calls = {"n": 0}

        async def fake_completion(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "kind": "tool_call",
                    "name": "create_project",
                    "arguments": {"name": "Comprar repuesto"},
                    "call_id": "call-1",
                    "content": None,
                    "reasoning_content": None,
                    "thought_signature": None,
                }
            return {"kind": "text", "content": "Listo, proyecto creado."}

        monkeypatch.setattr(handlers, "chat_completion_with_tools", fake_completion)

        client = object()
        reply = await handlers._run_ai_tool_loop(
            client=client,
            settings=sample_settings,
            messages=[],
            user_id=1,
            model="deepseek-v4-flash",
        )
        assert reply == "Listo, proyecto creado."
        assert calls["n"] == 2

    async def test_tool_loop_echoes_reasoning_and_thought_signature(
        self, sample_settings, monkeypatch
    ):
        authorize(monkeypatch)
        seen = {}
        calls = {"n": 0}

        async def fake_completion(**kwargs):
            calls["n"] += 1
            messages = kwargs["messages"]
            seen["messages"] = messages
            if calls["n"] == 1:
                return {
                    "kind": "tool_call",
                    "name": "create_project",
                    "arguments": {"name": "X"},
                    "call_id": "c1",
                    "content": None,
                    "reasoning_content": "pensando",
                    "thought_signature": "sig-1",
                }
            return {"kind": "text", "content": "hecho"}

        monkeypatch.setattr(handlers, "chat_completion_with_tools", fake_completion)

        async def fake_execute_tool(name, args, user_id, **kwargs):
            return '{"ok": true}'

        monkeypatch.setattr(handlers, "execute_tool", fake_execute_tool)

        await handlers._run_ai_tool_loop(
            client=object(),
            settings=sample_settings,
            messages=[],
            user_id=1,
            model="m",
        )
        # The second call's message list includes the tool-call assistant
        # message with reasoning echoed back plus the tool result message.
        assert seen["messages"][0]["reasoning_content"] == "pensando"
        assert seen["messages"][0]["tool_calls"][0]["extra_content"] == {
            "google": {"thought_signature": "sig-1"}
        }
        assert seen["messages"][1]["role"] == "tool"

    async def test_tool_loop_max_rounds(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        # Pin a small cap so the test does not depend on the MAX_TOOL_ROUNDS
        # default: the loop must stop after exactly max_tool_rounds tool
        # calls and return the friendly cutoff message.
        settings = dataclasses.replace(sample_settings, max_tool_rounds=2)
        calls = {"n": 0}

        async def fake_completion(**kwargs):
            calls["n"] += 1
            return {
                "kind": "tool_call",
                "name": "list_projects",
                "arguments": {},
                "call_id": f"c{calls['n']}",
                "content": None,
                "reasoning_content": None,
                "thought_signature": None,
            }

        async def fake_execute_tool(name, args, user_id, **kwargs):
            return '{"ok": true}'

        monkeypatch.setattr(handlers, "chat_completion_with_tools", fake_completion)
        monkeypatch.setattr(handlers, "execute_tool", fake_execute_tool)

        reply = await handlers._run_ai_tool_loop(
            client=object(),
            settings=settings,
            messages=[],
            user_id=1,
            model="m",
        )
        assert calls["n"] == 2  # never exceeds the configured cap
        assert "demasiadas acciones" in reply

    async def test_tool_loop_stops_on_confirmation(self, sample_settings, monkeypatch):
        """When delete_project returns confirmation_required, the loop exits
        immediately returning "" — the model never produces extra text."""
        authorize(monkeypatch)
        calls = {"n": 0}

        async def fake_completion(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "kind": "tool_call",
                    "name": "delete_project",
                    "arguments": {"slug": "viejo"},
                    "call_id": "c1",
                    "content": None,
                    "reasoning_content": None,
                    "thought_signature": None,
                }
            return {"kind": "text", "content": "Esto no debería verse."}

        async def fake_execute_tool(name, args, user_id, **kwargs):
            return json.dumps(
                {
                    "ok": True,
                    "confirmation_required": True,
                    "project": {"id": 7, "name": "Viejo", "slug": "viejo"},
                }
            )

        monkeypatch.setattr(handlers, "chat_completion_with_tools", fake_completion)
        monkeypatch.setattr(handlers, "execute_tool", fake_execute_tool)

        context = make_context(sample_settings)
        reply = await handlers._run_ai_tool_loop(
            client=object(),
            settings=sample_settings,
            messages=[],
            user_id=1,
            model="m",
            context=context,
            chat_id=1,
        )
        # The loop returned "" — no text from the model.
        assert reply == ""
        # The model was called only once (no second round).
        assert calls["n"] == 1
        # The confirmation buttons were sent to the user.
        assert len(context.bot.sent_messages) == 1
        assert "Eliminar definitivamente" in context.bot.sent_messages[0]["text"]


class TestForwardToolConfirmation:
    """/eliminarproyecto-style confirmation rendered for chat tools (delete_project)."""

    async def test_confirmation_sends_buttons_and_returns_feedback(self, sample_settings):
        # Arrange: a tool result that flags confirmation_required.
        context = FakeContext(sample_settings)
        tool_result = json.dumps(
            {
                "ok": False,
                "confirmation_required": True,
                "project": {"id": 7, "name": "Proyecto viejo", "slug": "proyecto-viejo"},
            }
        )

        # Act: forward the confirmation to the user.
        feedback = await handlers._forward_tool_confirmation(
            tool_result, context, chat_id=1
        )

        # Assert: the warning with both inline buttons was sent, and the
        # model receives a "wait for confirmation" message instead.
        assert len(context.bot.sent_messages) == 1
        msg = context.bot.sent_messages[0]
        assert "¿Eliminar definitivamente" in msg["text"]
        callbacks = [
            btn["callback_data"]
            for row in msg["reply_markup"].to_dict()["inline_keyboard"]
            for btn in row
        ]
        assert "delproj:7" in callbacks
        assert "delproj_cancel:7" in callbacks
        parsed = json.loads(feedback)
        assert parsed["confirmation_required"] is True
        assert parsed["ok"] is True

    async def test_noop_without_confirmation_flag(self, sample_settings):
        # Arrange: an ordinary tool result (no confirmation required).
        context = FakeContext(sample_settings)

        # Act: forward it.
        feedback = await handlers._forward_tool_confirmation(
            '{"ok": true}', context, chat_id=1
        )

        # Assert: nothing sent, returns None so the loop keeps the result.
        assert feedback is None
        assert context.bot.sent_messages == []

    async def test_missing_context_returns_error(self, sample_settings):
        # Arrange: confirmation required but no chat to deliver to.
        tool_result = json.dumps(
            {
                "ok": False,
                "confirmation_required": True,
                "project": {"id": 7, "name": "P"},
            }
        )

        # Act: forward with no context/chat.
        feedback = await handlers._forward_tool_confirmation(tool_result, None, None)

        # Assert: error JSON, no crash.
        parsed = json.loads(feedback)
        assert parsed["ok"] is False

    async def test_invalid_json_returns_none(self, sample_settings):
        # Arrange: a non-JSON tool result.
        context = FakeContext(sample_settings)

        # Act
        feedback = await handlers._forward_tool_confirmation("not json", context, 1)

        # Assert: returns None unchanged.
        assert feedback is None
        assert context.bot.sent_messages == []


class TestReminderCommands:
    async def test_remind_parses_and_schedules(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        parsed = {"datetime": None, "message": "x", "recurrence": "none"}

        # Patch the date parser to return a future date tomorrow.
        tomorrow = time.time() + 86400
        import datetime as dt

        future_iso = dt.datetime.fromtimestamp(tomorrow, tz=dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M"
        )
        parsed["datetime"] = future_iso

        async def fake_parse(**kwargs):
            return parsed

        monkeypatch.setattr(handlers, "parse_reminder_datetime", fake_parse)
        update = FakeUpdate()
        context = make_context(sample_settings, ai_client=object(), args=[
            "llamar al mecánico mañana"
        ])
        await handlers.recordar_command(update, context)
        assert "Recordatorio programado" in update.message.replies[0]["text"]
        assert len(reminders.list_pending_reminders(1)) == 1

    async def test_remind_rejects_past_date(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        past_iso = "2020-01-01T10:00"
        async def fake_parse(**kwargs):
            return {"datetime": past_iso, "message": "x", "recurrence": "none"}

        monkeypatch.setattr(handlers, "parse_reminder_datetime", fake_parse)
        update = FakeUpdate()
        context = make_context(sample_settings, ai_client=object(), args=["algo"])
        await handlers.recordar_command(update, context)
        assert "ya pasó" in update.message.replies[0]["text"]

    async def test_reminders_lists_pending(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        reminders.create_reminder(1, 100, "Llamar", time.time() + 3600, "none")
        update = FakeUpdate()
        context = make_context(sample_settings)
        await handlers.recordatorios_command(update, context)
        assert "Llamar" in update.message.replies[0]["text"]

    async def test_cancelremind(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        r = reminders.create_reminder(1, 100, "Llamar", time.time() + 3600)
        update = FakeUpdate()
        context = make_context(sample_settings, args=[str(r["id"])])
        await handlers.cancelarrecordatorio_command(update, context)
        assert "cancelado" in update.message.replies[0]["text"]
        assert reminders.list_pending_reminders(1) == []

    async def test_reminders_lists_with_inline_buttons(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        r = reminders.create_reminder(1, 100, "Llamar", time.time() + 3600, "none")
        update = FakeUpdate()
        context = make_context(sample_settings)
        await handlers.recordatorios_command(update, context)
        reply = update.message.replies[0]
        markup = reply.get("reply_markup")
        assert markup is not None
        # One row per reminder; each row carries cancel + postpone buttons.
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        callback_data = {btn.callback_data for btn in buttons}
        assert f"remind_cancel:{r['id']}" in callback_data
        assert f"remind_postpone:{r['id']}" in callback_data


class TestEditRemindCommand:
    async def test_edits_text(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        r = reminders.create_reminder(1, 100, "Llamar", time.time() + 3600)
        update = FakeUpdate()
        context = make_context(sample_settings, args=[str(r["id"]), "Llamar", "mañana"])
        await handlers.editarrecordatorio_command(update, context)
        assert "actualizado" in update.message.replies[0]["text"]
        assert "Llamar mañana" in update.message.replies[0]["text"]
        assert reminders.get_reminder_by_id(r["id"], 1)["message"] == "Llamar mañana"

    async def test_usage_when_no_text(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings, args=["5"])
        await handlers.editarrecordatorio_command(update, context)
        assert "Uso:" in update.message.replies[0]["text"]

    async def test_unknown_id(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings, args=["99999", "nuevo"])
        await handlers.editarrecordatorio_command(update, context)
        assert "No encontré" in update.message.replies[0]["text"]


class TestPostponeremindCommand:
    async def test_postpones(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        r = reminders.create_reminder(1, 100, "Llamar", time.time() + 3600)
        update = FakeUpdate()
        context = make_context(sample_settings, args=[str(r["id"]), "2h"])
        await handlers.posponerrecordatorio_command(update, context)
        assert "pospuesto" in update.message.replies[0]["text"]
        assert "2 horas" in update.message.replies[0]["text"]
        found = reminders.get_reminder_by_id(r["id"], 1)
        assert found["remind_at"] == r["remind_at"] + 7200

    async def test_invalid_duration(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        r = reminders.create_reminder(1, 100, "Llamar", time.time() + 3600)
        update = FakeUpdate()
        context = make_context(sample_settings, args=[str(r["id"]), "banana"])
        await handlers.posponerrecordatorio_command(update, context)
        assert "No entendí" in update.message.replies[0]["text"]

    async def test_unknown_id(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings, args=["99999", "30m"])
        await handlers.posponerrecordatorio_command(update, context)
        assert "No encontré" in update.message.replies[0]["text"]


class TestEditremindtimeCommand:
    async def test_reschedules(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        r = reminders.create_reminder(1, 100, "Llamar", time.time() + 3600)
        parsed = {"datetime": "2099-01-01T10:00", "message": "Llamar", "recurrence": "none"}

        async def fake_parse(**kwargs):
            return parsed

        monkeypatch.setattr(handlers, "parse_reminder_datetime", fake_parse)
        update = FakeUpdate()
        context = make_context(sample_settings, ai_client=object(), args=[str(r["id"]), "1 de enero de 2099 a las 10am"])
        await handlers.reprogramarrecordatorio_command(update, context)
        assert "reprogramado" in update.message.replies[0]["text"]
        found = reminders.get_reminder_by_id(r["id"], 1)
        assert found["remind_at"] > time.time() + 3600  # far future

    async def test_past_date_rejected(self, sample_settings, monkeypatch):
        authorize(monkeypatch)

        async def fake_parse(**kwargs):
            return {"datetime": "2020-01-01T10:00", "message": "x", "recurrence": "none"}

        monkeypatch.setattr(handlers, "parse_reminder_datetime", fake_parse)
        update = FakeUpdate()
        context = make_context(sample_settings, ai_client=object(), args=["5", "ayer"])
        await handlers.reprogramarrecordatorio_command(update, context)
        assert "ya pasó" in update.message.replies[0]["text"]

    async def test_unknown_id(self, sample_settings, monkeypatch):
        authorize(monkeypatch)

        async def fake_parse(**kwargs):
            return {"datetime": "2099-01-01T10:00", "message": "x", "recurrence": "none"}

        monkeypatch.setattr(handlers, "parse_reminder_datetime", fake_parse)
        update = FakeUpdate()
        context = make_context(sample_settings, ai_client=object(), args=["99999", "mañana"])
        await handlers.reprogramarrecordatorio_command(update, context)
        assert "No encontré" in update.message.replies[0]["text"]


class TestReminderCallback:
    async def test_cancel_button(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        r = reminders.create_reminder(1, 100, "Llamar", time.time() + 3600)
        query = FakeCallbackQuery(data=f"remind_cancel:{r['id']}", message=FakeMessage(text="lista"), user_id=1)
        update = FakeUpdate(callback_query=query)
        context = make_context(sample_settings)
        await handlers.reminder_callback(update, context)
        assert query.answered
        assert "cancelado" in query.answer_text
        assert reminders.list_pending_reminders(1) == []

    async def test_postpone_button(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        r = reminders.create_reminder(1, 100, "Llamar", time.time() + 3600)
        query = FakeCallbackQuery(data=f"remind_postpone:{r['id']}", message=FakeMessage(text="lista"), user_id=1)
        update = FakeUpdate(callback_query=query)
        context = make_context(sample_settings)
        await handlers.reminder_callback(update, context)
        assert query.answered
        assert "Pospuesto" in query.answer_text
        found = reminders.get_reminder_by_id(r["id"], 1)
        assert found["remind_at"] == r["remind_at"] + 3600

    async def test_foreign_user_rejected(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        r = reminders.create_reminder(1, 100, "Llamar", time.time() + 3600)
        query = FakeCallbackQuery(data=f"remind_cancel:{r['id']}", message=FakeMessage(text="lista"), user_id=2)
        update = FakeUpdate(callback_query=query)
        context = make_context(sample_settings)
        await handlers.reminder_callback(update, context)
        assert "ya no existe" in query.answer_text
        assert len(reminders.list_pending_reminders(1)) == 1  # not deleted

    async def test_invalid_data(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        query = FakeCallbackQuery(data="garbage", message=FakeMessage(text="lista"), user_id=1)
        update = FakeUpdate(callback_query=query)
        context = make_context(sample_settings)
        await handlers.reminder_callback(update, context)
        assert "no válido" in query.answer_text


class TestScheduledJobs:
    async def test_check_due_reminders_sends_and_marks(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "Tarea", now - 5, "none")
        context = make_context(sample_settings)
        await handlers.check_due_reminders(context)
        assert context.bot.sent_messages
        assert "Tarea" in context.bot.sent_messages[0]["text"]
        assert reminders.get_due_reminders(now) == []

    async def test_recurring_reminder_rescheduled(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "Diaria", now - 5, "daily")
        context = make_context(sample_settings)
        await handlers.check_due_reminders(context)
        assert context.bot.sent_messages
        # not marked sent — rescheduled to next occurrence
        assert reminders.get_due_reminders(now) == []

    async def test_failed_delivery_schedules_retry(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "Tarea", now - 5, "none")
        context = make_context(sample_settings)

        async def failing_send(*args, **kwargs):
            raise Exception("network down")

        monkeypatch.setattr(context.bot, "send_message", failing_send)
        await handlers.check_due_reminders(context)
        # Not sent (failure), not lost — scheduled for retry
        found = reminders.get_reminder_by_id(reminder["id"], 1)
        assert found["attempts"] == 1
        assert found["next_retry_at"] is not None
        # Withheld while in backoff window
        assert reminders.get_due_reminders(now + 5) == []

    async def test_recurring_failure_also_retries(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "Diaria", now - 5, "daily")
        context = make_context(sample_settings)

        async def failing_send(*args, **kwargs):
            raise Exception("network down")

        monkeypatch.setattr(context.bot, "send_message", failing_send)
        await handlers.check_due_reminders(context)
        found = reminders.get_reminder_by_id(reminder["id"], 1)
        assert found["attempts"] >= 1
        assert found["next_retry_at"] is not None


class TestNotifyGroupProjectChange:
    """Tests for notify_group_project_change: group project notifications."""

    async def test_sends_notification_to_group(self, sample_settings, monkeypatch):
        # Arrange: group project
        project = create_project(1, "Mi Proyecto", group_id=-1001)
        from bot.projects import add_group_member
        add_group_member(-1001, 1)
        add_group_member(-1001, 2)
        context = make_context(sample_settings)

        # Act
        await handlers.notify_group_project_change(
            context, project, "status", 1, detail=" a Completado"
        )

        # Assert: notification sent to group
        assert len(context.bot.sent_messages) == 1
        msg = context.bot.sent_messages[0]
        assert msg["chat_id"] == -1001
        assert "Mi Proyecto" in msg["text"]
        assert "cambió de estado" in msg["text"]
        assert "Completado" in msg["text"]

    async def test_no_notification_for_private_project(self, sample_settings, monkeypatch):
        # Arrange: private project (no group_id)
        project = create_project(1, "Privado")
        context = make_context(sample_settings)

        # Act
        await handlers.notify_group_project_change(
            context, project, "status", 1
        )

        # Assert: no message sent
        assert len(context.bot.sent_messages) == 0

    async def test_excludes_actor_from_notification(self, sample_settings, monkeypatch):
        # Arrange: group project with multiple members
        project = create_project(1, "Proyecto", group_id=-1001)
        from bot.projects import add_group_member
        add_group_member(-1001, 1)
        add_group_member(-1001, 2)
        context = make_context(sample_settings)

        # Act: notify excluding actor (user 1)
        await handlers.notify_group_project_change(
            context, project, "status", 1
        )

        # Assert: notification sent (actor excluded but others exist)
        assert len(context.bot.sent_messages) == 1

    async def test_failure_is_logged_not_raised(self, sample_settings, monkeypatch):
        # Arrange: group project
        project = create_project(1, "Proyecto", group_id=-1001)
        from bot.projects import add_group_member
        add_group_member(-1001, 1)
        add_group_member(-1001, 2)
        context = make_context(sample_settings)

        async def failing_send(*args, **kwargs):
            raise Exception("bot kicked")

        monkeypatch.setattr(context.bot, "send_message", failing_send)

        # Act: should not raise
        await handlers.notify_group_project_change(
            context, project, "status", 1
        )

        # Assert: no exception, test passes

    async def test_note_type_notification(self, sample_settings, monkeypatch):
        # Arrange
        project = create_project(1, "Proyecto", group_id=-1001)
        from bot.projects import add_group_member
        add_group_member(-1001, 1)
        add_group_member(-1001, 2)
        context = make_context(sample_settings)

        # Act
        await handlers.notify_group_project_change(
            context, project, "note", 1
        )

        # Assert
        assert len(context.bot.sent_messages) == 1
        assert "Nueva nota" in context.bot.sent_messages[0]["text"]

    async def test_priority_type_notification(self, sample_settings, monkeypatch):
        # Arrange
        project = create_project(1, "Proyecto", group_id=-1001)
        from bot.projects import add_group_member
        add_group_member(-1001, 1)
        add_group_member(-1001, 2)
        context = make_context(sample_settings)

        # Act
        await handlers.notify_group_project_change(
            context, project, "priority", 1, detail=" a Alta"
        )

        # Assert
        assert len(context.bot.sent_messages) == 1
        assert "cambió de prioridad" in context.bot.sent_messages[0]["text"]
        assert "Alta" in context.bot.sent_messages[0]["text"]


class TestStripNotifyMetadata:
    """Tests for _strip_notify_metadata: removes _notify from tool results."""

    def test_strips_notify_key(self):
        result = json.dumps({"ok": True, "_notify": {"type": "status"}})
        stripped = handlers._strip_notify_metadata(result)
        payload = json.loads(stripped)
        assert "_notify" not in payload
        assert payload["ok"] is True

    def test_no_notify_unchanged(self):
        result = json.dumps({"ok": True, "project": {"id": 1}})
        stripped = handlers._strip_notify_metadata(result)
        assert stripped == result

    def test_invalid_json_returned_as_is(self):
        result = "not json at all"
        stripped = handlers._strip_notify_metadata(result)
        assert stripped == result


class TestDispatchToolNotifications:
    """Tests for _dispatch_tool_notifications: dispatches group notifications."""

    async def test_dispatches_notification(self, sample_settings, monkeypatch):
        # Arrange: tool result with _notify
        project = create_project(1, "Proyecto", group_id=-1001)
        from bot.projects import add_group_member
        add_group_member(-1001, 1)
        add_group_member(-1001, 2)
        context = make_context(sample_settings)
        tool_result = json.dumps({
            "ok": True,
            "_notify": {
                "type": "status",
                "project": project,
                "actor_id": 1,
                "detail": " a Completado",
            },
        })

        # Act
        await handlers._dispatch_tool_notifications(tool_result, context)

        # Assert: notification sent
        assert len(context.bot.sent_messages) == 1

    async def test_no_dispatch_without_notify(self, sample_settings, monkeypatch):
        # Arrange: tool result without _notify
        context = make_context(sample_settings)
        tool_result = json.dumps({"ok": True, "project": {"id": 1}})

        # Act
        await handlers._dispatch_tool_notifications(tool_result, context)

        # Assert: no notification
        assert len(context.bot.sent_messages) == 0

    async def test_no_dispatch_for_private_project(self, sample_settings, monkeypatch):
        # Arrange: tool result with _notify but project has no group_id
        project = create_project(1, "Privado")
        context = make_context(sample_settings)
        tool_result = json.dumps({
            "ok": True,
            "_notify": {
                "type": "status",
                "project": project,
                "actor_id": 1,
            },
        })

        # Act
        await handlers._dispatch_tool_notifications(tool_result, context)

        # Assert: no notification
        assert len(context.bot.sent_messages) == 0

    async def test_invalid_json_no_crash(self, sample_settings, monkeypatch):
        context = make_context(sample_settings)
        await handlers._dispatch_tool_notifications("not json", context)
        assert len(context.bot.sent_messages) == 0
