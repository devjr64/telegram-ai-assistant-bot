"""Unit tests for bot.tools — tool dispatch always returns JSON strings."""

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot import memory as memory_store
from bot import proactive
from bot import reminders
from bot import tools
from bot.projects import (
    add_project_member,
    create_project,
    get_project_for_user,
    update_project_status,
    upsert_user,
)


class TestUnknownTool:
    async def test_unknown_tool_returns_error_json(self):
        result = await tools.execute_tool("does_not_exist", {}, 1)
        payload = json.loads(result)
        assert payload["ok"] is False
        assert "unknown tool" in payload["error"]


class TestCreateProjectTool:
    async def test_creates_project(self):
        result = await tools.execute_tool(
            "create_project", {"name": "Arreglar el carro", "description": "x"}, 1
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["project"]["slug"] == "arreglar-el-carro"

    async def test_duplicate_returns_error(self):
        create_project(1, "Arreglar el carro")
        result = await tools.execute_tool(
            "create_project", {"name": "Arreglar el carro"}, 1
        )
        payload = json.loads(result)
        assert payload["ok"] is False

    async def test_create_with_priority(self):
        result = await tools.execute_tool(
            "create_project",
            {"name": "Proyecto prioritario", "priority": "alta"},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["project"]["priority"] == "alta"

    async def test_create_urgent_respects_limit(self):
        for i in range(3):
            create_project(1, f"Urgente {i}", priority="urgente")
        result = await tools.execute_tool(
            "create_project", {"name": "Urgente 3", "priority": "urgente"}, 1
        )
        payload = json.loads(result)
        assert payload["ok"] is False
        assert len(payload["existing"]) == 3

    async def test_missing_name_returns_error(self):
        # _dispatch raises KeyError; execute_tool must convert it to JSON.
        result = await tools.execute_tool("create_project", {}, 1)
        payload = json.loads(result)
        assert payload["ok"] is False

    async def test_create_with_invalid_priority_returns_error(self):
        # Arrange: invalid priority value.
        args = {"name": "Proyecto X", "priority": "invalida"}

        # Act: attempt to create a project with an unsupported priority.
        result = await tools.execute_tool("create_project", args, 1)
        payload = json.loads(result)

        # Assert: the underlying create_project rejects unknown priorities.
        assert payload["ok"] is False
        assert "Prioridad inválida" in payload["error"]


class TestProjectTools:
    async def test_update_status(self):
        project = create_project(1, "Proyecto")
        result = await tools.execute_tool(
            "update_project_status",
            {"slug": "proyecto", "status": "completado"},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["project"]["status"] == "completado"

    async def test_update_status_with_stopped_reason(self):
        project = create_project(1, "Proyecto")
        result = await tools.execute_tool(
            "update_project_status",
            {"slug": "proyecto", "status": "parado", "stopped_reason": "Esperando repuestos"},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["project"]["status"] == "parado"
        assert payload["project"]["stopped_reason"] == "Esperando repuestos"

    async def test_update_status_reactivating_urgent_beyond_limit_returns_error(self):
        # Arrange: one urgent project set to parado, then 3 urgent active ones.
        paused = create_project(1, "Parado urgente", priority="urgente")
        update_project_status(paused["id"], "parado", stopped_reason="espera")
        for i in range(3):
            create_project(1, f"Urgente {i}", priority="urgente")

        # Act: try to reactivate the parado urgent project via the tool.
        result = await tools.execute_tool(
            "update_project_status",
            {"slug": "parado-urgente", "status": "en_proceso"},
            1,
        )
        payload = json.loads(result)

        # Assert: rejected and lists the existing urgent projects.
        assert payload["ok"] is False
        assert "3 proyectos urgentes" in payload["error"]
        assert len(payload["existing"]) == 3

    async def test_update_status_member_urgent_counts_against_owner(self):
        # Arrange: owner 111 has 3 urgent active; shared project is parado urgent;
        # member 222 has no urgent projects of their own.
        shared = create_project(111, "Compartido", priority="urgente")
        update_project_status(shared["id"], "parado", stopped_reason="espera")
        for i in range(3):
            create_project(111, f"Urgente {i}", priority="urgente")
        add_project_member(shared["id"], 222, 111)

        # Act: member 222 reactivates the shared project via the tool.
        result = await tools.execute_tool(
            "update_project_status",
            {"slug": "compartido", "status": "en_proceso"},
            222,
        )
        payload = json.loads(result)

        # Assert: the 3-urgent limit counts against the OWNER, not the member.
        assert payload["ok"] is False
        assert "3 proyectos urgentes" in payload["error"]

    async def test_update_status_member_own_urgent_count_does_not_block(self):
        # Arrange: owner 111 has 0 urgent; member 222 has 3 urgent of their own.
        shared = create_project(111, "Compartido", priority="urgente")
        update_project_status(shared["id"], "parado", stopped_reason="espera")
        add_project_member(shared["id"], 222, 111)
        for i in range(3):
            create_project(222, f"Mío urgente {i}", priority="urgente")

        # Act: member 222 reactivates the shared project via the tool.
        result = await tools.execute_tool(
            "update_project_status",
            {"slug": "compartido", "status": "en_proceso"},
            222,
        )
        payload = json.loads(result)

        # Assert: the member's own urgent count must not block the owner's project.
        assert payload["ok"] is True
        assert payload["project"]["status"] == "en_proceso"

    async def test_update_status_not_found(self):
        result = await tools.execute_tool(
            "update_project_status", {"slug": "nope", "status": "sin_iniciar"}, 1
        )
        payload = json.loads(result)
        assert payload["ok"] is False

    async def test_update_status_invalid_status_returns_error(self):
        # Arrange: a project exists and we request an unsupported status.
        create_project(1, "Proyecto")
        args = {"slug": "proyecto", "status": "activo"}

        # Act: attempt to update to an invalid status.
        result = await tools.execute_tool("update_project_status", args, 1)
        payload = json.loads(result)

        # Assert: dispatch rejects statuses outside PROJECT_STATUSES.
        assert payload["ok"] is False
        assert "Estado inválido" in payload["error"]

    async def test_update_priority(self):
        project = create_project(1, "Proyecto")
        result = await tools.execute_tool(
            "update_project_priority",
            {"slug": "proyecto", "priority": "alta"},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["project"]["priority"] == "alta"

    async def test_update_priority_to_urgent_respects_limit(self):
        for i in range(3):
            create_project(1, f"Urgente {i}", priority="urgente")
        normal = create_project(1, "Normal")
        result = await tools.execute_tool(
            "update_project_priority",
            {"slug": "normal", "priority": "urgente"},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is False
        assert len(payload["existing"]) == 3

    async def test_update_priority_not_found(self):
        result = await tools.execute_tool(
            "update_project_priority", {"slug": "nope", "priority": "alta"}, 1
        )
        payload = json.loads(result)
        assert payload["ok"] is False

    async def test_update_priority_invalid_priority_returns_error(self):
        # Arrange: a project exists and we request an unsupported priority.
        create_project(1, "Proyecto")
        args = {"slug": "proyecto", "priority": "muy-alta"}

        # Act: attempt to update to an invalid priority.
        result = await tools.execute_tool("update_project_priority", args, 1)
        payload = json.loads(result)

        # Assert: dispatch rejects priorities outside PROJECT_PRIORITIES.
        assert payload["ok"] is False
        assert "Prioridad inválida" in payload["error"]

    async def test_add_note(self):
        project = create_project(1, "Proyecto")
        result = await tools.execute_tool(
            "add_project_note",
            {"slug": "proyecto", "summary": "avance", "content": "detalle"},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is True

    async def test_list_projects(self):
        create_project(1, "Alpha")
        create_project(1, "Beta")
        result = await tools.execute_tool("list_projects", {}, 1)
        payload = json.loads(result)
        assert payload["ok"] is True
        assert len(payload["projects"]) == 2

    async def test_list_projects_with_spanish_status_filter(self):
        # Arrange: two projects in different Spanish states.
        create_project(1, "Alpha")
        finished = create_project(1, "Beta")
        update_project_status(finished["id"], "completado")

        # Act: list only completed projects.
        result = await tools.execute_tool(
            "list_projects", {"status_filter": "completado"}, 1
        )
        payload = json.loads(result)

        # Assert: only the matching project is returned.
        assert payload["ok"] is True
        assert len(payload["projects"]) == 1
        assert payload["projects"][0]["slug"] == "beta"

    async def test_list_projects_with_invalid_status_filter_returns_error(self):
        # Arrange: request a status that is not part of PROJECT_STATUSES.
        args = {"status_filter": "active"}

        # Act: attempt to list with an invalid filter.
        result = await tools.execute_tool("list_projects", args, 1)
        payload = json.loads(result)

        # Assert: dispatch validates the filter before querying the DB.
        assert payload["ok"] is False
        assert "Estado inválido" in payload["error"]

    async def test_list_projects_returns_bullet_format(self):
        # Arrange: two projects in different states, one urgent.
        create_project(1, "Alpha")
        in_progress = create_project(1, "Beta")
        update_project_status(in_progress["id"], "en_proceso", stopped_reason=None)
        urgent = create_project(1, "Gamma", priority="urgente")
        update_project_status(urgent["id"], "parado", stopped_reason="espera")

        # Act: list projects through the tool.
        result = await tools.execute_tool("list_projects", {}, 1)
        payload = json.loads(result)
        formatted = payload["formatted"]

        # Assert: a grouped bullet list, never a Markdown table.
        assert formatted.startswith("📂 Tienes 3 proyecto(s):")
        assert "• Alpha" in formatted
        assert "• Beta" in formatted
        assert "• Gamma" in formatted
        assert "|" not in formatted

    async def test_list_projects_format_groups_by_status(self):
        # Arrange: one project per state.
        create_project(1, "Sin empezar")
        in_progress = create_project(1, "En curso")
        update_project_status(in_progress["id"], "en_proceso")
        done = create_project(1, "Terminado")
        update_project_status(done["id"], "completado")

        # Act: list all projects.
        result = await tools.execute_tool("list_projects", {}, 1)
        payload = json.loads(result)
        formatted = payload["formatted"]

        # Assert: each state appears as its own emoji-headed group.
        assert "⚪ Sin iniciar (1):" in formatted
        assert "🟢 En proceso (1):" in formatted
        assert "✅ Completado (1):" in formatted
        assert "Sin empezar" in formatted
        assert "En curso" in formatted
        assert "Terminado" in formatted

    async def test_list_projects_empty_format(self):
        # Act: list projects when none exist.
        result = await tools.execute_tool("list_projects", {}, 1)
        payload = json.loads(result)

        # Assert: friendly empty message, no pipes.
        assert "No tienes proyectos." in payload["formatted"]
        assert "|" not in payload["formatted"]

    async def test_list_projects_format_includes_priority_label(self):
        # Arrange: one high-priority project.
        create_project(1, "Cocina", priority="alta")

        # Act: list projects.
        result = await tools.execute_tool("list_projects", {}, 1)
        payload = json.loads(result)

        # Assert: the human-readable priority label is included per bullet.
        assert "• Cocina — Prioridad: Alta" in payload["formatted"]

    async def test_search_projects(self):
        create_project(1, "Arreglar el carro")
        result = await tools.execute_tool("search_projects", {"query": "carro"}, 1)
        payload = json.loads(result)
        assert len(payload["projects"]) == 1
        assert "• Arreglar el carro" in payload["formatted"]
        assert "|" not in payload["formatted"]

    async def test_search_projects_empty_format(self):
        result = await tools.execute_tool("search_projects", {"query": "zzz"}, 1)
        payload = json.loads(result)
        assert len(payload["projects"]) == 0
        assert "No tienes proyectos." in payload["formatted"]

    async def test_get_project(self):
        project = create_project(1, "Proyecto")
        result = await tools.execute_tool("get_project", {"slug": "proyecto"}, 1)
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["project"]["id"] == project["id"]
        assert payload["files"]["overview.md"]

    async def test_get_project_returns_new_fields(self):
        # Arrange: create a project and stop it with a reason.
        project = create_project(1, "Proyecto detallado", priority="alta")
        update_project_status(project["id"], "parado", stopped_reason="Esperando permiso")

        # Act: fetch the full project details through the tool.
        result = await tools.execute_tool("get_project", {"slug": "proyecto-detallado"}, 1)
        payload = json.loads(result)

        # Assert: the response includes priority, Spanish status, timestamps and reason.
        assert payload["ok"] is True
        returned = payload["project"]
        assert returned["priority"] == "alta"
        assert returned["status"] == "parado"
        assert returned["stopped_reason"] == "Esperando permiso"
        assert returned["created_at"] > 0
        assert returned["status_changed_at"] > 0


class TestDeleteProjectTool:
    async def test_owner_request_requires_confirmation(self, isolated_data_dir):
        # Arrange: the owner creates and archives a project.
        project = create_project(1, "Proyecto")
        update_project_status(project["id"], "archivado")

        # Act: the owner asks to delete it via the tool.
        result = await tools.execute_tool(
            "delete_project", {"slug": "proyecto"}, 1
        )
        payload = json.loads(result)

        # Assert: no direct deletion — confirmation is requested and the
        # project (DB row + Markdown dir) is left untouched until the user
        # confirms with the inline buttons.
        assert payload["ok"] is False
        assert payload["confirmation_required"] is True
        assert payload["project"]["id"] == project["id"]
        assert payload["project"]["name"] == "Proyecto"
        assert get_project_for_user(1, "proyecto") is not None
        project_dir = isolated_data_dir / "projects" / "1" / "proyecto"
        assert project_dir.exists()

    async def test_non_owner_blocked(self):
        # Arrange: owner 1 archives a project and invites 222 as a member,
        # so 222 can see the project but does not own it.
        project = create_project(1, "Proyecto")
        update_project_status(project["id"], "archivado")
        add_project_member(project["id"], 222, 1)

        # Act: the collaborator tries to delete the project.
        result = await tools.execute_tool(
            "delete_project", {"slug": "proyecto"}, 222
        )
        payload = json.loads(result)

        # Assert: only the owner can delete — rejected, no confirmation
        # requested, and the row survives.
        assert payload["ok"] is False
        assert "dueño" in payload["error"]
        assert payload.get("confirmation_required") is not True
        assert get_project_for_user(1, "proyecto") is not None

    async def test_non_archived_blocked(self):
        # Arrange: a completed project that was never archived.
        project = create_project(1, "Proyecto")
        update_project_status(project["id"], "completado")

        # Act: the owner tries to delete it directly.
        result = await tools.execute_tool(
            "delete_project", {"slug": "proyecto"}, 1
        )
        payload = json.loads(result)

        # Assert: only archived projects can be deleted — parseable JSON,
        # and no confirmation is requested for a non-archived project.
        assert payload["ok"] is False
        assert "archivado" in payload["error"]
        assert payload.get("confirmation_required") is not True

    async def test_unknown_slug_not_found(self):
        # Act: delete a project that does not exist.
        result = await tools.execute_tool(
            "delete_project", {"slug": "no-existe"}, 1
        )
        payload = json.loads(result)

        # Assert: reported as not found, never a raw exception.
        assert payload["ok"] is False
        assert "Proyecto no encontrado" in payload["error"]

    async def test_tool_definition_exists(self):
        # Arrange: locate the delete_project tool definition.
        definition = next(
            t["function"]
            for t in tools.TOOL_DEFINITIONS
            if t["function"]["name"] == "delete_project"
        )
        props = definition["parameters"]["properties"]

        # Act & Assert: the definition requires a slug and stresses the guardrails.
        assert "slug" in props
        assert "slug" in definition["parameters"]["required"]


class TestToolDefinitions:
    async def test_create_project_definition_supports_priority(self):
        # Arrange: locate the create_project tool definition.
        definition = next(
            t["function"] for t in tools.TOOL_DEFINITIONS if t["function"]["name"] == "create_project"
        )
        props = definition["parameters"]["properties"]

        # Act & Assert: the definition accepts Spanish priorities and mentions the urgent limit.
        assert "priority" in props
        assert props["priority"]["enum"] == ["baja", "media", "alta", "urgente"]
        assert "urgente" in props["priority"]["description"].lower()
        assert "name" in definition["parameters"]["required"]

    async def test_update_project_status_definition_supports_stopped_reason(self):
        # Arrange: locate the update_project_status tool definition.
        definition = next(
            t["function"] for t in tools.TOOL_DEFINITIONS if t["function"]["name"] == "update_project_status"
        )
        props = definition["parameters"]["properties"]

        # Act & Assert: the definition uses Spanish statuses and exposes stopped_reason.
        assert props["status"]["enum"] == ["sin_iniciar", "en_proceso", "parado", "completado", "archivado"]
        assert "stopped_reason" in props
        assert "parado" in props["stopped_reason"]["description"].lower()

    async def test_update_project_priority_definition_exists(self):
        # Arrange: locate the update_project_priority tool definition.
        definition = next(
            t["function"] for t in tools.TOOL_DEFINITIONS if t["function"]["name"] == "update_project_priority"
        )
        props = definition["parameters"]["properties"]

        # Act & Assert: the definition accepts Spanish priorities and requires both fields.
        assert props["priority"]["enum"] == ["baja", "media", "alta", "urgente"]
        assert {"slug", "priority"} <= set(definition["parameters"]["required"])

    async def test_list_projects_definition_instructs_formatted(self):
        # Arrange: locate the list_projects tool definition.
        definition = next(
            t["function"] for t in tools.TOOL_DEFINITIONS if t["function"]["name"] == "list_projects"
        )

        # Act & Assert: the model is told to echo `formatted` and avoid Markdown tables.
        assert "formatted" in definition["description"].lower()
        assert "Markdown table" in definition["description"]

    async def test_search_projects_definition_instructs_formatted(self):
        # Arrange: locate the search_projects tool definition.
        definition = next(
            t["function"] for t in tools.TOOL_DEFINITIONS if t["function"]["name"] == "search_projects"
        )

        # Act & Assert: same `formatted` instruction as list_projects.
        assert "formatted" in definition["description"].lower()
        assert "Markdown table" in definition["description"]


class TestCreateReminderTool:
    async def test_creates_reminder(self, monkeypatch):
        monkeypatch.setattr(tools, "get_chat_for_user", lambda uid: 100)
        result = await tools.execute_tool(
            "create_reminder",
            {"message": "Llamar", "datetime": "2026-08-13T10:00"},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is True

    async def test_no_chat_returns_error(self, monkeypatch):
        monkeypatch.setattr(tools, "get_chat_for_user", lambda uid: None)
        result = await tools.execute_tool(
            "create_reminder",
            {"message": "Llamar", "datetime": "2026-08-13T10:00"},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is False

    async def test_bad_datetime_returns_error(self, monkeypatch):
        monkeypatch.setattr(tools, "get_chat_for_user", lambda uid: 100)
        result = await tools.execute_tool(
            "create_reminder",
            {"message": "Llamar", "datetime": "not-a-date"},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is False


class TestReminderTools:
    async def test_list_reminders_returns_pending(self):
        now = time.time()
        reminders.create_reminder(1, 100, "tarea", now + 3600, "none")
        result = await tools.execute_tool("list_reminders", {}, 1)
        payload = json.loads(result)
        assert payload["ok"] is True
        assert len(payload["reminders"]) == 1
        assert payload["reminders"][0]["message"] == "tarea"

    async def test_list_reminders_returns_bullet_format(self):
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "tarea", now + 3600, "daily")
        result = await tools.execute_tool("list_reminders", {}, 1)
        payload = json.loads(result)
        formatted = payload["formatted"]
        # A bullet list, not a Markdown table.
        assert formatted.startswith("🔔 Tienes 1 recordatorio(s):")
        assert f"[ID {reminder['id']}] tarea" in formatted
        assert "|" not in formatted
        assert "cada día" in formatted
        assert "próximo:" in formatted

    async def test_list_reminders_empty_format(self):
        result = await tools.execute_tool("list_reminders", {}, 1)
        payload = json.loads(result)
        assert "No tienes recordatorios pendientes" in payload["formatted"]

    async def test_list_reminders_only_own(self):
        now = time.time()
        reminders.create_reminder(1, 100, "mía", now + 3600)
        reminders.create_reminder(2, 200, "de otro", now + 3600)
        result = await tools.execute_tool("list_reminders", {}, 1)
        payload = json.loads(result)
        assert payload["ok"] is True
        assert len(payload["reminders"]) == 1
        assert payload["reminders"][0]["message"] == "mía"

    async def test_cancel_reminder_ok(self):
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "tarea", now + 3600)
        result = await tools.execute_tool(
            "cancel_reminder", {"reminder_id": reminder["id"]}, 1
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert reminders.list_pending_reminders(1) == []

    async def test_cancel_reminder_not_owned(self):
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "tarea", now + 3600)
        result = await tools.execute_tool(
            "cancel_reminder", {"reminder_id": reminder["id"]}, 2
        )
        payload = json.loads(result)
        assert payload["ok"] is False
        assert len(reminders.list_pending_reminders(1)) == 1

    async def test_cancel_reminder_missing(self):
        result = await tools.execute_tool("cancel_reminder", {"reminder_id": 99999}, 1)
        payload = json.loads(result)
        assert payload["ok"] is False

    async def test_edit_reminder_ok(self):
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "tarea", now + 3600)
        result = await tools.execute_tool(
            "edit_reminder",
            {"reminder_id": reminder["id"], "message": "tarea actualizada"},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["reminder"]["message"] == "tarea actualizada"
        found = reminders.get_reminder_by_id(reminder["id"], 1)
        assert found["message"] == "tarea actualizada"

    async def test_edit_reminder_not_owned(self):
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "tarea", now + 3600)
        result = await tools.execute_tool(
            "edit_reminder",
            {"reminder_id": reminder["id"], "message": "hack"},
            2,
        )
        payload = json.loads(result)
        assert payload["ok"] is False
        found = reminders.get_reminder_by_id(reminder["id"], 1)
        assert found["message"] == "tarea"

    async def test_edit_reminder_empty_message(self):
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "tarea", now + 3600)
        result = await tools.execute_tool(
            "edit_reminder",
            {"reminder_id": reminder["id"], "message": "   "},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is False

    async def test_reschedule_reminder_ok(self):
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "tarea", now + 3600)
        result = await tools.execute_tool(
            "reschedule_reminder",
            {"reminder_id": reminder["id"], "datetime": "2099-06-15T17:00"},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        found = reminders.get_reminder_by_id(reminder["id"], 1)
        assert found["remind_at"] > now + 3600 * 24  # far future

    async def test_reschedule_reminder_bad_datetime(self):
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "tarea", now + 3600)
        result = await tools.execute_tool(
            "reschedule_reminder",
            {"reminder_id": reminder["id"], "datetime": "not-a-date"},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is False

    async def test_postpone_reminder_ok(self):
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "tarea", now + 3600)
        before = reminder["remind_at"]
        result = await tools.execute_tool(
            "postpone_reminder",
            {"reminder_id": reminder["id"], "minutes": 120},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["minutes"] == 120
        found = reminders.get_reminder_by_id(reminder["id"], 1)
        assert found["remind_at"] == before + 7200

    async def test_postpone_reminder_invalid_minutes(self):
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "tarea", now + 3600)
        result = await tools.execute_tool(
            "postpone_reminder",
            {"reminder_id": reminder["id"], "minutes": -5},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is False

    async def test_reminder_edit_tools_not_owned(self):
        now = time.time()
        reminder = reminders.create_reminder(1, 100, "tarea", now + 3600)
        for name, args in (
            ("reschedule_reminder", {"reminder_id": reminder["id"], "datetime": "2099-01-01T10:00"}),
            ("postpone_reminder", {"reminder_id": reminder["id"], "minutes": 30}),
        ):
            result = await tools.execute_tool(name, args, 2)
            payload = json.loads(result)
            assert payload["ok"] is False


class TestWebSearchTool:
    async def test_returns_search_result(self, monkeypatch):
        async def fake_search(**kwargs):
            return {"ok": True, "results": [{"title": "T", "href": "https://x"}]}

        monkeypatch.setattr(tools, "web_search", fake_search)
        result = await tools.execute_tool("web_search", {"query": "hola"}, 1)
        payload = json.loads(result)
        assert payload["ok"] is True


class TestDolarTool:
    async def test_returns_rate(self, monkeypatch):
        async def fake_dolar():
            return {"ok": True, "oficial": 76.5, "paralela": 78.2}

        monkeypatch.setattr(tools, "get_dolar_info", fake_dolar)
        result = await tools.execute_tool("dolar_rate", {}, 1)
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["oficial"] == 76.5


class TestGenerateImageTool:
    async def test_missing_gemini_key_returns_error(self, monkeypatch):
        monkeypatch.setattr(tools, "build_client_for_provider", lambda s, p: None)
        result = await tools.execute_tool(
            "generate_image", {"prompt": "un gato"}, 1
        )
        payload = json.loads(result)
        assert payload["ok"] is False
        assert "GEMINI_API_KEY" in payload["error"]

    async def test_missing_prompt_returns_error(self, monkeypatch):
        result = await tools.execute_tool("generate_image", {}, 1)
        payload = json.loads(result)
        assert payload["ok"] is False
        assert "prompt" in payload["error"]


class TestShareProjectTool:
    async def test_share_grants_immediate_access(self):
        # Arrange: owner 111 has a project; invitee 222 is a known user.
        create_project(111, "Proyecto")
        upsert_user(222, "mama", "Mama")

        # Act: the owner shares the project with @mama.
        result = await tools.execute_tool(
            "share_project", {"slug": "proyecto", "username": "@mama"}, 111
        )
        payload = json.loads(result)

        # Assert: sharing succeeds and 222 can already read the project.
        assert payload["ok"] is True
        assert get_project_for_user(222, "proyecto") is not None

    async def test_non_owner_cannot_share(self):
        # Arrange: 111 owns a project and invites 222 as a collaborator;
        # 222 can see the project but is not its owner.
        create_project(111, "Proyecto")
        upsert_user(222, "mama", "Mama")
        await tools.execute_tool(
            "share_project", {"slug": "proyecto", "username": "@mama"}, 111
        )

        # Act: 222 (a member, not the owner) tries to share further.
        result = await tools.execute_tool(
            "share_project", {"slug": "proyecto", "username": "@tia"}, 222
        )
        payload = json.loads(result)

        # Assert: only the owner can share — RBAC rejected.
        assert payload["ok"] is False
        assert "dueño" in payload["error"]

    async def test_self_share_rejected(self):
        # Arrange: owner 111 exists in the users table under their username.
        create_project(111, "Proyecto")
        upsert_user(111, "jose", "Jose")

        # Act: the owner tries to share the project with themselves.
        result = await tools.execute_tool(
            "share_project", {"slug": "proyecto", "username": "@jose"}, 111
        )
        payload = json.loads(result)

        # Assert: self-invitation is refused.
        assert payload["ok"] is False
        assert "contigo mismo" in payload["error"]

    async def test_unknown_username(self):
        # Arrange: owner 111 has a project; @nadie never messaged the bot.
        create_project(111, "Proyecto")

        # Act: the owner shares with an unknown username.
        result = await tools.execute_tool(
            "share_project", {"slug": "proyecto", "username": "@nadie"}, 111
        )
        payload = json.loads(result)

        # Assert: resolution failure is reported in Spanish.
        assert payload["ok"] is False
        assert "Usuario no encontrado" in payload["error"]

    async def test_returns_parseable_json(self):
        # Arrange: owner with a project; known invitee and own username.
        create_project(111, "Proyecto")
        upsert_user(222, "mama", "Mama")
        upsert_user(111, "jose", "Jose")

        # Act: exercise the success, unknown-user and self-share paths.
        results = [
            await tools.execute_tool(
                "share_project", {"slug": "proyecto", "username": "@mama"}, 111
            ),
            await tools.execute_tool(
                "share_project", {"slug": "proyecto", "username": "@nadie"}, 111
            ),
            await tools.execute_tool(
                "share_project", {"slug": "proyecto", "username": "@jose"}, 111
            ),
        ]

        # Assert: every response is a JSON object carrying the ok field.
        for result in results:
            payload = json.loads(result)
            assert isinstance(payload, dict)
            assert "ok" in payload


class TestUnshareProjectTool:
    async def test_owner_removes_access(self):
        # Arrange: 111 owns a project and shares it with @mama (222).
        create_project(111, "Proyecto")
        upsert_user(222, "mama", "Mama")
        await tools.execute_tool(
            "share_project", {"slug": "proyecto", "username": "@mama"}, 111
        )

        # Act: the owner revokes @mama's access.
        result = await tools.execute_tool(
            "unshare_project", {"slug": "proyecto", "username": "@mama"}, 111
        )
        payload = json.loads(result)

        # Assert: access is revoked — 222 can no longer read the project.
        assert payload["ok"] is True
        assert get_project_for_user(222, "proyecto") is None

    async def test_non_owner_cannot_unshare(self):
        # Arrange: 222 is a collaborator (member, not owner) of the project.
        create_project(111, "Proyecto")
        upsert_user(222, "mama", "Mama")
        await tools.execute_tool(
            "share_project", {"slug": "proyecto", "username": "@mama"}, 111
        )

        # Act: 222 tries to revoke access.
        result = await tools.execute_tool(
            "unshare_project", {"slug": "proyecto", "username": "@mama"}, 222
        )
        payload = json.loads(result)

        # Assert: only the owner can revoke access.
        assert payload["ok"] is False
        assert "dueño" in payload["error"]

    async def test_cannot_unshare_owner(self):
        # Arrange: owner 111 is a known user under their own username.
        create_project(111, "Proyecto")
        upsert_user(111, "jose", "Jose")

        # Act: the owner tries to unshare their own account.
        result = await tools.execute_tool(
            "unshare_project", {"slug": "proyecto", "username": "@jose"}, 111
        )
        payload = json.loads(result)

        # Assert: the owner can never be removed from their own project.
        assert payload["ok"] is False
        assert "al dueño" in payload["error"]

    async def test_unshare_non_member(self):
        # Arrange: 333 is a known user but was never invited to the project.
        create_project(111, "Proyecto")
        upsert_user(333, "tia", "Tia")

        # Act: the owner tries to revoke someone who has no access.
        result = await tools.execute_tool(
            "unshare_project", {"slug": "proyecto", "username": "@tia"}, 111
        )
        payload = json.loads(result)

        # Assert: revoking a non-member is reported as no access.
        assert payload["ok"] is False
        assert "no tiene acceso" in payload["error"]


class TestListProjectMembersTool:
    async def test_empty_list(self):
        # Arrange: a project with no collaborators.
        create_project(111, "Proyecto")

        # Act: list the project's members.
        result = await tools.execute_tool("list_project_members", {"slug": "proyecto"}, 111)
        payload = json.loads(result)

        # Assert: no members, friendly empty format.
        assert payload["ok"] is True
        assert payload["members"] == []
        assert "no tiene colaboradores" in payload["formatted"]

    async def test_lists_members(self):
        # Arrange: 111 shares "Proyecto" with @mama (222).
        create_project(111, "Proyecto")
        upsert_user(222, "mama", "Mama")
        await tools.execute_tool(
            "share_project", {"slug": "proyecto", "username": "@mama"}, 111
        )

        # Act: list the project's members.
        result = await tools.execute_tool("list_project_members", {"slug": "proyecto"}, 111)
        payload = json.loads(result)

        # Assert: the invitee appears with their profile and @username.
        assert payload["ok"] is True
        member = next(m for m in payload["members"] if m["user_id"] == 222)
        assert member["username"] == "mama"
        assert "@mama" in payload["formatted"]


class TestProjectToolsScopedByACL:
    async def _share_with_mama(self):
        """Arrange: 111 owns "Proyecto" and invites @mama (222)."""
        create_project(111, "Proyecto")
        upsert_user(222, "mama", "Mama")
        await tools.execute_tool(
            "share_project", {"slug": "proyecto", "username": "@mama"}, 111
        )

    async def test_get_project_member_can_read_shared(self):
        # Arrange: 111 shares the project with 222.
        await self._share_with_mama()

        # Act: the invited member fetches the project details.
        result = await tools.execute_tool("get_project", {"slug": "proyecto"}, 222)
        payload = json.loads(result)

        # Assert: the member sees the full project.
        assert payload["ok"] is True
        assert payload["project"]["name"] == "Proyecto"
        assert payload["project"]["slug"] == "proyecto"

    async def test_get_project_non_member_not_found(self):
        # Arrange: 111 owns a project; 333 is not a member.
        create_project(111, "Proyecto")

        # Act: a non-member asks for the project by slug.
        result = await tools.execute_tool("get_project", {"slug": "proyecto"}, 333)
        payload = json.loads(result)

        # Assert: no leakage — reported as not found, no project data.
        assert payload["ok"] is False
        assert "Proyecto no encontrado" in payload["error"]
        assert "project" not in payload

    async def test_list_projects_includes_shared_for_member(self):
        # Arrange: 111 shares the project with 222.
        await self._share_with_mama()

        # Act: the invited member lists their projects.
        result = await tools.execute_tool("list_projects", {}, 222)
        payload = json.loads(result)

        # Assert: the shared project is visible with role "member" and marked.
        assert payload["ok"] is True
        shared = [p for p in payload["projects"] if p["slug"] == "proyecto"]
        assert len(shared) == 1
        assert shared[0]["role"] == "member"
        assert "compartido" in payload["formatted"]

    async def test_list_projects_non_member_only_owned(self):
        # Arrange: 111 owns a project; 333 is not a member.
        create_project(111, "Proyecto")

        # Act: a non-member lists their projects.
        result = await tools.execute_tool("list_projects", {}, 333)
        payload = json.loads(result)

        # Assert: 111's project never leaks into 333's list.
        assert payload["ok"] is True
        assert all(p["slug"] != "proyecto" for p in payload["projects"])

    async def test_update_status_member_can_edit_shared(self):
        # Arrange: 111 shares the project with 222.
        await self._share_with_mama()

        # Act: the invited member updates the shared project's status.
        result = await tools.execute_tool(
            "update_project_status",
            {"slug": "proyecto", "status": "completado"},
            222,
        )
        payload = json.loads(result)

        # Assert: collaborators have full edit access to shared projects.
        assert payload["ok"] is True
        assert payload["project"]["status"] == "completado"

    async def test_update_status_non_member_rejected(self):
        # Arrange: 111 owns a project; 333 is not a member.
        create_project(111, "Proyecto")

        # Act: a non-member tries to modify the project's status.
        result = await tools.execute_tool(
            "update_project_status",
            {"slug": "proyecto", "status": "completado"},
            333,
        )
        payload = json.loads(result)

        # Assert: no leakage — rejected as not found.
        assert payload["ok"] is False
        assert "Proyecto no encontrado" in payload["error"]


# --- Memory tools (journal_append, memory_read, memory_recall, memory_insights) ---
#
# These tests exercise the 4 long-term memory tools through execute_tool
# (dispatch-level, matching the rest of this file) plus the pure helper
# functions they build on. The autouse `isolated_data_dir` fixture points
# bot.memory at a fresh tmp directory per test, so seeding happens via
# bot.memory directly (append_raw_entry / save_summary / save_daily_compact)
# and never touches a real user journal. Fixed timestamps and the
# America/Caracas timezone keep everything deterministic; `_media_day` is
# monkeypatched to a fixed "2026-08-14" where the handler resolves "today".

_MEM_TZ = ZoneInfo("America/Caracas")
_MEM_TS = datetime(2026, 8, 14, 15, 45, tzinfo=_MEM_TZ).timestamp()


class TestMemoryHelperFunctions:
    """Pure helpers used by the memory tools: level guessing and scoring."""

    def test_guess_memory_level_for_all_shapes(self):
        # Day, ISO week, quarter and year keys map to their journal levels.
        assert tools._guess_memory_level("2026-08-14") == "day"
        assert tools._guess_memory_level("2026-W33") == "weekly"
        assert tools._guess_memory_level("2026-Q3") == "quarterly"
        assert tools._guess_memory_level("2026") == "yearly"

    def test_guess_memory_level_rejects_garbage(self):
        assert tools._guess_memory_level("") is None
        assert tools._guess_memory_level("agosto") is None
        assert tools._guess_memory_level("2026-08") is None
        assert tools._guess_memory_level("2026-W") is None

    def test_guess_memory_level_is_shape_only(self):
        # The guess is shape-only by design; calendar validity is re-checked
        # by bot.memory when content is actually read (tested below).
        assert tools._guess_memory_level("2026-13-01") == "day"

    def test_keyword_hits_counts_meaningful_tokens(self):
        # "¿cuándo fue mi último viaje a Panamá?" reduces to {"último",
        # "panamá"} — cuándo/fue/mi/viaje/a are stopwords — and the accented
        # token matches the summary's "Panamá" case-insensitively.
        assert tools._keyword_hits(
            "¿cuándo fue mi último viaje a Panamá?", "Viaje a Panamá en agosto"
        ) == 1

    def test_keyword_hits_matches_partial_phrase(self):
        # "pagué la luz" reduces to {"pagué"} (luz is too short), so a day
        # mentioning either meaningful token scores without a phrase match.
        assert tools._keyword_hits("pagué la luz", "pagué la luz") == 1

    def test_keyword_hits_ignores_stopwords_and_short_tokens(self):
        assert tools._keyword_hits("el día", "el día") == 0  # all stopwords
        assert tools._keyword_hits("luz", "luz") == 0  # 3 chars < minimum


class TestJournalAppendTool:
    async def test_appends_to_todays_journal(self, monkeypatch):
        # Arrange: freeze "today" so the stored day is deterministic.
        monkeypatch.setattr(tools, "_media_day", lambda settings: "2026-08-14")

        # Act: append one entry through the dispatch layer.
        result = await tools.execute_tool(
            "journal_append", {"content": "comí arepa"}, 1
        )
        payload = json.loads(result)

        # Assert: written to today's bitácora, with metadata in the JSON.
        assert payload["ok"] is True
        assert payload["day"] == "2026-08-14"
        assert payload["entry_count"] == 1
        raw = memory_store.get_raw_content(1, "2026-08-14")
        assert "comí arepa" in raw
        assert payload["word_count"] == memory_store.count_words(raw)

    async def test_empty_content_returns_spanish_error(self):
        # Act: append an empty (whitespace-only) entry.
        result = await tools.execute_tool("journal_append", {"content": "   "}, 1)
        payload = json.loads(result)

        # Assert: rejected with a Spanish error, nothing stored.
        assert payload["ok"] is False
        assert "vacío" in payload["error"]

    async def test_topic_prefix_stored(self, monkeypatch):
        # Arrange: freeze today and pass a valid topic.
        monkeypatch.setattr(tools, "_media_day", lambda settings: "2026-08-14")

        # Act: append with a "finanzas" topic.
        result = await tools.execute_tool(
            "journal_append", {"content": "compré la luz", "topic": "finanzas"}, 1
        )
        payload = json.loads(result)

        # Assert: the raw line carries the [topic] prefix.
        assert payload["ok"] is True
        assert "[finanzas] compré la luz" in memory_store.get_raw_content(
            1, "2026-08-14"
        )

    async def test_unknown_topic_dropped_without_error(self, monkeypatch):
        # Arrange: freeze today and pass a topic outside _JOURNAL_TOPICS.
        monkeypatch.setattr(tools, "_media_day", lambda settings: "2026-08-14")

        # Act: the soft validation should drop the topic, not fail the append.
        result = await tools.execute_tool(
            "journal_append", {"content": "algo notable", "topic": "hackeo"}, 1
        )
        payload = json.loads(result)

        # Assert: entry stored without the bogus prefix.
        assert payload["ok"] is True
        raw = memory_store.get_raw_content(1, "2026-08-14")
        assert "algo notable" in raw
        assert "[hackeo]" not in raw

    async def test_entry_count_increments_across_appends(self, monkeypatch):
        # Arrange: freeze today.
        monkeypatch.setattr(tools, "_media_day", lambda settings: "2026-08-14")

        # Act: append twice on the same day.
        await tools.execute_tool("journal_append", {"content": "primera"}, 1)
        result = await tools.execute_tool("journal_append", {"content": "segunda"}, 1)
        payload = json.loads(result)

        # Assert: both entries are counted in the day's metadata.
        assert payload["ok"] is True
        assert payload["entry_count"] == 2
        raw = memory_store.get_raw_content(1, "2026-08-14")
        assert raw.count("\n") == 2


class TestMemoryReadTool:
    def _seed_day(self, user_id: int = 1, day: str = "2026-08-14") -> None:
        memory_store.append_raw_entry(
            user_id, day, "comí arepa", ts=_MEM_TS, tz_name="America/Caracas"
        )

    async def test_day_auto_returns_raw_when_no_compact(self):
        # Arrange: a raw day with no compact version.
        self._seed_day()

        # Act: read the day with level auto.
        result = await tools.execute_tool("memory_read", {"period": "2026-08-14"}, 1)
        payload = json.loads(result)

        # Assert: the raw journal is the fallback content.
        assert payload["ok"] is True
        assert payload["level"] == "day"
        assert "comí arepa" in payload["content"]

    async def test_day_auto_prefers_daily_compact(self):
        # Arrange: a raw day plus a compact version.
        self._seed_day()
        memory_store.save_daily_compact(1, "2026-08-14", "Día: arepa y café.")

        # Act: read the day with level auto.
        result = await tools.execute_tool("memory_read", {"period": "2026-08-14"}, 1)
        payload = json.loads(result)

        # Assert: the compact wins over the raw log.
        assert payload["ok"] is True
        assert payload["content"] == "Día: arepa y café."

    async def test_daily_level_reads_only_compact(self):
        # Arrange: a raw day with NO compact.
        self._seed_day()

        # Act: request level "daily" explicitly.
        result = await tools.execute_tool(
            "memory_read", {"period": "2026-08-14", "level": "daily"}, 1
        )
        payload = json.loads(result)

        # Assert: no fallback to raw — missing compact yields empty + note.
        assert payload["ok"] is True
        assert payload["content"] == ""
        assert "note" in payload

    async def test_raw_level_reads_raw_ignoring_compact(self):
        # Arrange: a day with both raw content and a compact.
        self._seed_day()
        memory_store.save_daily_compact(1, "2026-08-14", "Día compacto")

        # Act: request level "raw" explicitly.
        result = await tools.execute_tool(
            "memory_read", {"period": "2026-08-14", "level": "raw"}, 1
        )
        payload = json.loads(result)

        # Assert: the raw Markdown is returned, not the compact.
        assert payload["ok"] is True
        assert payload["content"].startswith("- [")
        assert "comí arepa" in payload["content"]

    async def test_weekly_returns_summary_content(self):
        # Arrange: a stored weekly summary.
        memory_store.save_summary(1, "weekly", "2026-W33", "Semana de viajes y pagos")

        # Act: read the week with level auto.
        result = await tools.execute_tool("memory_read", {"period": "2026-W33"}, 1)
        payload = json.loads(result)

        # Assert: the summary content is returned.
        assert payload["ok"] is True
        assert payload["level"] == "weekly"
        assert payload["content"] == "Semana de viajes y pagos"

    async def test_quarterly_returns_summary_content(self):
        # Arrange: a stored quarterly summary.
        memory_store.save_summary(1, "quarterly", "2026-Q3", "Trimestre intenso")

        # Act: read the quarter with level auto.
        result = await tools.execute_tool("memory_read", {"period": "2026-Q3"}, 1)
        payload = json.loads(result)

        # Assert: the summary content is returned.
        assert payload["ok"] is True
        assert payload["level"] == "quarterly"
        assert payload["content"] == "Trimestre intenso"

    async def test_yearly_returns_summary_content(self):
        # Arrange: a stored yearly summary.
        memory_store.save_summary(1, "yearly", "2026", "Año de viajes")

        # Act: read the year with level auto.
        result = await tools.execute_tool("memory_read", {"period": "2026"}, 1)
        payload = json.loads(result)

        # Assert: the summary content is returned.
        assert payload["ok"] is True
        assert payload["level"] == "yearly"
        assert payload["content"] == "Año de viajes"

    @pytest.mark.parametrize(
        "period,level",
        [
            ("2026-08-14", "day"),
            ("2026-W33", "weekly"),
            ("2026-Q3", "quarterly"),
            ("2026", "yearly"),
        ],
    )
    async def test_auto_guesses_level_for_all_shapes(self, period, level):
        # Act: read an empty period with level auto.
        result = await tools.execute_tool("memory_read", {"period": period}, 1)
        payload = json.loads(result)

        # Assert: the shape decides the level even when nothing is stored.
        assert payload["ok"] is True
        assert payload["level"] == level
        assert payload["content"] == ""
        assert "note" in payload

    async def test_missing_day_returns_ok_with_note(self):
        # Act: read a day that was never written.
        result = await tools.execute_tool("memory_read", {"period": "2026-08-14"}, 1)
        payload = json.loads(result)

        # Assert: ok True, empty content and a Spanish note (no raw error).
        assert payload["ok"] is True
        assert payload["content"] == ""
        assert "No hay entradas" in payload["note"]

    async def test_missing_summary_returns_ok_with_note(self):
        # Act: read a week that has no summary yet.
        result = await tools.execute_tool("memory_read", {"period": "2026-W33"}, 1)
        payload = json.loads(result)

        # Assert: ok True, empty content and a "no summary yet" note.
        assert payload["ok"] is True
        assert payload["content"] == ""
        assert "no existe un resumen" in payload["note"]

    async def test_invalid_period_shape_returns_error(self):
        # Act: a period string with no recognizable shape.
        result = await tools.execute_tool("memory_read", {"period": "agosto"}, 1)
        payload = json.loads(result)

        # Assert: rejected with a Spanish explanation of valid shapes.
        assert payload["ok"] is False
        assert "No pude identificar el periodo" in payload["error"]

    async def test_invalid_calendar_day_returns_error(self):
        # Act: a well-shaped but calendar-invalid day (Feb 30), explicit level.
        result = await tools.execute_tool(
            "memory_read", {"period": "2026-02-30", "level": "raw"}, 1
        )
        payload = json.loads(result)

        # Assert: the storage layer's ValueError becomes a JSON error.
        assert payload["ok"] is False
        assert "Periodo inválido" in payload["error"]

    async def test_unknown_level_returns_error(self):
        # Act: an unsupported explicit level.
        result = await tools.execute_tool(
            "memory_read", {"period": "2026-08-14", "level": "bogus"}, 1
        )
        payload = json.loads(result)

        # Assert: rejected before touching the storage layer.
        assert payload["ok"] is False
        assert "Nivel desconocido" in payload["error"]


class TestMemoryRecallTool:
    def _seed_raw(self, day: str, text: str, user_id: int = 1) -> None:
        memory_store.append_raw_entry(
            user_id, day, text, ts=_MEM_TS, tz_name="America/Caracas"
        )

    async def test_recall_finds_summary_match_top_down(self):
        # Arrange: a yearly summary mentioning Panamá, plus a weekly summary
        # that does not match the query.
        memory_store.save_summary(
            1, "yearly", "2026", "Viaje a Panamá en agosto. Último viaje largo."
        )
        memory_store.save_summary(1, "weekly", "2026-W33", "Semana tranquila")

        # Act: ask about the last trip to Panamá.
        result = await tools.execute_tool(
            "memory_recall", {"query": "¿cuándo fue mi último viaje a Panamá?"}, 1
        )
        payload = json.loads(result)

        # Assert: the top-down search scores the yearly summary (yearly is
        # the first level scanned) and returns it with navigation pointers.
        assert payload["ok"] is True
        assert len(payload["matches"]) == 1
        match = payload["matches"][0]
        assert match["level"] == "yearly"
        assert match["period"] == "2026"
        assert "Panamá" in match["content"]
        assert match["pointer"] == ["2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"]

    async def test_recall_falls_back_to_raw_when_summaries_do_not_match(self):
        # Arrange: a raw day mentioning the payment; no summaries at all.
        self._seed_raw("2026-08-14", "pagué la luz hoy")

        # Act: search within a range that covers the day.
        result = await tools.execute_tool(
            "memory_recall",
            {"query": "pagué la luz", "date_from": "2026-08-01", "date_to": "2026-08-20"},
            1,
        )
        payload = json.loads(result)

        # Assert: the raw day is the fallback match, flagged level "raw".
        assert payload["ok"] is True
        raw_matches = [m for m in payload["matches"] if m["level"] == "raw"]
        assert len(raw_matches) == 1
        assert raw_matches[0]["period"] == "2026-08-14"
        assert "pagué" in raw_matches[0]["content"]

    async def test_recall_respects_date_bounds(self):
        # Arrange: two raw days, both matching the query, outside and inside
        # the requested range.
        self._seed_raw("2026-08-10", "pago la luz")
        self._seed_raw("2026-08-20", "pago el agua")

        # Act: restrict the search to 08-08..08-12.
        result = await tools.execute_tool(
            "memory_recall",
            {"query": "pago", "date_from": "2026-08-08", "date_to": "2026-08-12"},
            1,
        )
        payload = json.loads(result)

        # Assert: the entry outside the range is never returned.
        assert payload["ok"] is True
        periods = [m["period"] for m in payload["matches"]]
        assert periods == ["2026-08-10"]

    async def test_recall_no_match_returns_ok_with_note(self):
        # Act: search for something never written.
        result = await tools.execute_tool(
            "memory_recall", {"query": "zapato naranja"}, 1
        )
        payload = json.loads(result)

        # Assert: ok True with an empty match list and a Spanish note.
        assert payload["ok"] is True
        assert payload["matches"] == []
        assert "No encontré nada" in payload["note"]

    async def test_recall_empty_query_returns_error(self):
        # Act: a whitespace-only query.
        result = await tools.execute_tool("memory_recall", {"query": "   "}, 1)
        payload = json.loads(result)

        # Assert: rejected with a Spanish error.
        assert payload["ok"] is False
        assert "query" in payload["error"]


class TestMemoryInsightsTool:
    def _seed_raw(self, day: str, text: str, user_id: int = 1) -> None:
        memory_store.append_raw_entry(
            user_id, day, text, ts=_MEM_TS, tz_name="America/Caracas"
        )

    def _current_week(self) -> str:
        """Return the week key the handler computes for the bot's timezone.

        Mirrors _handle_memory_insights exactly (load_settings().timezone +
        real now) so the assertion stays deterministic even if the ambient
        TIMEZONE env var differs from America/Caracas.
        """
        tz = ZoneInfo(tools.load_settings().timezone)
        return memory_store.week_key(datetime.now(tz))

    async def test_returns_recent_days_and_week(self, monkeypatch):
        # Arrange: freeze today and seed two days inside the default window.
        monkeypatch.setattr(tools, "_media_day", lambda settings: "2026-08-14")
        self._seed_raw("2026-08-10", "corrí 5km")
        self._seed_raw("2026-08-14", "nadé en la tarde")

        # Act: request insights with defaults.
        result = await tools.execute_tool("memory_insights", {}, 1)
        payload = json.loads(result)

        # Assert: the days are listed chronologically with content, plus the
        # current ISO week key (computed from the bot's timezone).
        assert payload["ok"] is True
        assert [d["day"] for d in payload["days"]] == ["2026-08-10", "2026-08-14"]
        assert "nadé en la tarde" in payload["days"][1]["content"]
        assert payload["week"] == self._current_week()

    async def test_days_back_param_respected(self, monkeypatch):
        # Arrange: freeze today; seed one day inside and one outside the
        # default 7-day window.
        monkeypatch.setattr(tools, "_media_day", lambda settings: "2026-08-14")
        self._seed_raw("2026-08-01", "nota vieja")
        self._seed_raw("2026-08-10", "nota reciente")

        # Act: default window (7 days back).
        default = json.loads(await tools.execute_tool("memory_insights", {}, 1))

        # Assert: only the recent day is included by default.
        assert [d["day"] for d in default["days"]] == ["2026-08-10"]

        # Act: widen the window to 20 days back.
        wide = json.loads(
            await tools.execute_tool("memory_insights", {"days": 20}, 1)
        )

        # Assert: both days fall inside the wider window.
        assert [d["day"] for d in wide["days"]] == ["2026-08-01", "2026-08-10"]

    async def test_entries_older_than_window_excluded(self, monkeypatch):
        # Arrange: freeze today; seed only an entry far outside the window.
        monkeypatch.setattr(tools, "_media_day", lambda settings: "2026-08-14")
        self._seed_raw("2026-07-01", "muy antiguo")

        # Act: request insights with the default window.
        result = await tools.execute_tool("memory_insights", {}, 1)
        payload = json.loads(result)

        # Assert: the old entry is excluded — days is empty, week still there.
        assert payload["ok"] is True
        assert payload["days"] == []
        assert "week" in payload

    async def test_empty_journal_returns_days_empty_and_week(self, monkeypatch):
        # Arrange: freeze today; no entries at all.
        monkeypatch.setattr(tools, "_media_day", lambda settings: "2026-08-14")

        # Act: request insights.
        result = await tools.execute_tool("memory_insights", {}, 1)
        payload = json.loads(result)

        # Assert: ok True, no days, but the week key and empty summary remain.
        assert payload["ok"] is True
        assert payload["days"] == []
        assert payload["week"] == self._current_week()
        assert payload["week_summary"] == ""

    async def test_week_summary_included_when_exists(self, monkeypatch):
        # Arrange: freeze today and store the current week's summary.
        monkeypatch.setattr(tools, "_media_day", lambda settings: "2026-08-14")
        week = self._current_week()
        memory_store.save_summary(1, "weekly", week, "Semana activa")

        # Act: request insights.
        result = await tools.execute_tool("memory_insights", {}, 1)
        payload = json.loads(result)

        # Assert: the current week's summary is attached to the payload.
        assert payload["ok"] is True
        assert payload["week"] == week
        assert payload["week_summary"] == "Semana activa"


class TestMemoryToolsDispatch:
    """Dispatch-level: every memory tool returns a parseable JSON object."""

    async def test_execute_tool_journal_append_returns_parseable_json(self, monkeypatch):
        # Arrange: freeze today.
        monkeypatch.setattr(tools, "_media_day", lambda settings: "2026-08-14")

        # Act: run journal_append through execute_tool (not the handler).
        result = await tools.execute_tool("journal_append", {"content": "x"}, 1)
        payload = json.loads(result)

        # Assert: a JSON object with the ok field.
        assert isinstance(payload, dict)
        assert payload["ok"] is True

    async def test_execute_tool_memory_read_returns_parseable_json(self):
        result = await tools.execute_tool("memory_read", {"period": "2026-08-14"}, 1)
        payload = json.loads(result)
        assert isinstance(payload, dict)
        assert payload["ok"] is True

    async def test_execute_tool_memory_recall_returns_parseable_json(self):
        result = await tools.execute_tool("memory_recall", {"query": "viaje"}, 1)
        payload = json.loads(result)
        assert isinstance(payload, dict)
        assert payload["ok"] is True

    async def test_execute_tool_memory_insights_returns_parseable_json(self):
        result = await tools.execute_tool("memory_insights", {}, 1)
        payload = json.loads(result)
        assert isinstance(payload, dict)
        assert payload["ok"] is True

    async def test_execute_tool_set_checkin_time_returns_parseable_json(self):
        result = await tools.execute_tool(
            "set_checkin_time", {"period": "morning", "time": "09:30"}, 1
        )
        payload = json.loads(result)
        assert isinstance(payload, dict)
        assert payload["ok"] is True
        assert payload["morning"] == "09:30"


class TestSetCheckinTimeTool:
    """set_checkin_time: per-user time, validation, and isolation."""

    async def test_handler_sets_morning_time(self):
        result = await tools._handle_set_checkin_time(
            {"period": "morning", "time": "07:15"}, 1
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["time"] == "07:15"

    async def test_handler_rejects_bad_time(self):
        result = await tools._handle_set_checkin_time(
            {"period": "morning", "time": "99:99"}, 1
        )
        payload = json.loads(result)
        assert payload["ok"] is False
        assert "Hora inválida" in payload["error"]

    async def test_handler_rejects_bad_period(self):
        result = await tools._handle_set_checkin_time(
            {"period": "tarde", "time": "09:00"}, 1
        )
        payload = json.loads(result)
        assert payload["ok"] is False
        assert "Periodo inválido" in payload["error"]

    async def test_set_checkin_time_is_scoped_to_user(self, sample_settings):
        await tools.execute_tool(
            "set_checkin_time", {"period": "morning", "time": "09:30"}, 1
        )
        # user 2 still sees the global defaults (08:00 / 20:00).
        assert proactive.get_checkin_times(2, sample_settings) == (
            "08:00",
            "20:00",
        )

    def test_tool_is_registered_in_definitions(self):
        names = [t["function"]["name"] for t in tools.TOOL_DEFINITIONS]
        assert "set_checkin_time" in names


class TestMemoryToolIsolation:
    """user A's memory tools never return user B's data."""

    async def test_journal_append_does_not_leak_to_other_user(self, monkeypatch):
        # Arrange: freeze today and append as user 1.
        monkeypatch.setattr(tools, "_media_day", lambda settings: "2026-08-14")
        await tools.execute_tool("journal_append", {"content": "secreto de A"}, 1)

        # Act & Assert: user 1 sees it, user 2's journal stays empty.
        assert "secreto de A" in memory_store.get_raw_content(1, "2026-08-14")
        assert memory_store.get_raw_content(2, "2026-08-14") == ""

    async def test_memory_read_as_other_user_returns_empty(self):
        # Arrange: user 1 has a raw entry; user 2 does not.
        memory_store.append_raw_entry(
            1, "2026-08-14", "secreto de A", ts=_MEM_TS, tz_name="America/Caracas"
        )

        # Act: user 2 reads the same day.
        result = await tools.execute_tool(
            "memory_read", {"period": "2026-08-14", "level": "raw"}, 2
        )
        payload = json.loads(result)

        # Assert: no leakage — empty content with a note, not user 1's text.
        assert payload["ok"] is True
        assert payload["content"] == ""
        assert "note" in payload

    async def test_memory_recall_handler_with_other_user_id_does_not_leak(self):
        # Arrange: user 1's raw journal mentions a payment.
        memory_store.append_raw_entry(
            1, "2026-08-14", "pagué la luz", ts=_MEM_TS, tz_name="America/Caracas"
        )

        # Act: call the handler directly with user_id=2 — user_id always comes
        # from the Telegram update (the handler's second arg), never from the
        # model args, so a different caller must see nothing.
        result = await tools._handle_memory_recall(
            {"query": "pagué", "date_from": "2026-08-01", "date_to": "2026-08-20"},
            2,
        )
        payload = json.loads(result)

        # Assert: no matches for user 2, no content leaked.
        assert payload["ok"] is True
        assert payload["matches"] == []

    async def test_memory_insights_does_not_leak_other_users_entries(self, monkeypatch):
        # Arrange: freeze today; user 1 has an entry, user 2 has none.
        monkeypatch.setattr(tools, "_media_day", lambda settings: "2026-08-14")
        memory_store.append_raw_entry(
            1, "2026-08-14", "secreto de A", ts=_MEM_TS, tz_name="America/Caracas"
        )

        # Act: user 2 requests insights.
        result = await tools.execute_tool("memory_insights", {}, 2)
        payload = json.loads(result)

        # Assert: user 2 sees no days.
        assert payload["ok"] is True
        assert payload["days"] == []


class TestMemoryToolDefinitions:
    """The 4 memory tools are wired into TOOL_DEFINITIONS with the right params."""

    async def test_journal_append_definition_requires_content(self):
        # Arrange: locate the journal_append tool definition.
        definition = next(
            t["function"]
            for t in tools.TOOL_DEFINITIONS
            if t["function"]["name"] == "journal_append"
        )
        props = definition["parameters"]["properties"]

        # Act & Assert: content is required, topic is an optional enum-like field.
        assert "content" in definition["parameters"]["required"]
        assert "topic" in props

    async def test_memory_read_definition_requires_period(self):
        definition = next(
            t["function"]
            for t in tools.TOOL_DEFINITIONS
            if t["function"]["name"] == "memory_read"
        )
        assert "period" in definition["parameters"]["required"]
        assert "level" in definition["parameters"]["properties"]

    async def test_memory_recall_definition_requires_query(self):
        definition = next(
            t["function"]
            for t in tools.TOOL_DEFINITIONS
            if t["function"]["name"] == "memory_recall"
        )
        assert "query" in definition["parameters"]["required"]
        assert "date_from" in definition["parameters"]["properties"]
        assert "date_to" in definition["parameters"]["properties"]

    async def test_memory_insights_definition_has_days(self):
        definition = next(
            t["function"]
            for t in tools.TOOL_DEFINITIONS
            if t["function"]["name"] == "memory_insights"
        )
        assert "days" in definition["parameters"]["properties"]


class TestListProjectsScope:
    """Tests for list_projects tool with scope parameter."""

    async def test_scope_all_returns_all_projects(self):
        # Arrange: owned + shared projects
        owned = create_project(1, "Mío")
        other = create_project(2, "De otro")
        add_project_member(other["id"], 1, 2)

        # Act: list with scope "all"
        result = await tools.execute_tool(
            "list_projects", {"scope": "all"}, 1
        )
        payload = json.loads(result)

        # Assert: both projects returned
        assert payload["ok"] is True
        names = [p["name"] for p in payload["projects"]]
        assert "Mío" in names
        assert "De otro" in names

    async def test_scope_owned_returns_only_owned(self):
        # Arrange: owned + shared projects
        owned = create_project(1, "Mío")
        other = create_project(2, "De otro")
        add_project_member(other["id"], 1, 2)

        # Act: list with scope "owned"
        result = await tools.execute_tool(
            "list_projects", {"scope": "owned"}, 1
        )
        payload = json.loads(result)

        # Assert: only owned project
        assert payload["ok"] is True
        names = [p["name"] for p in payload["projects"]]
        assert "Mío" in names
        assert "De otro" not in names

    async def test_scope_shared_with_me(self):
        # Arrange: owned + shared projects
        owned = create_project(1, "Mío")
        other = create_project(2, "De otro")
        add_project_member(other["id"], 1, 2)

        # Act: list with scope "shared_with_me"
        result = await tools.execute_tool(
            "list_projects", {"scope": "shared_with_me"}, 1
        )
        payload = json.loads(result)

        # Assert: only shared project
        assert payload["ok"] is True
        names = [p["name"] for p in payload["projects"]]
        assert "De otro" in names
        assert "Mío" not in names

    async def test_default_scope_is_all(self):
        # Arrange: owned + shared projects
        owned = create_project(1, "Mío")
        other = create_project(2, "De otro")
        add_project_member(other["id"], 1, 2)

        # Act: list without scope (default)
        result = await tools.execute_tool("list_projects", {}, 1)
        payload = json.loads(result)

        # Assert: all projects (same as scope "all")
        assert payload["ok"] is True
        names = [p["name"] for p in payload["projects"]]
        assert "Mío" in names
        assert "De otro" in names

    async def test_list_projects_definition_has_scope(self):
        definition = next(
            t["function"]
            for t in tools.TOOL_DEFINITIONS
            if t["function"]["name"] == "list_projects"
        )
        assert "scope" in definition["parameters"]["properties"]


class TestNotificationMetadata:
    """Tests for _notify metadata in tool results for group projects."""

    async def test_update_status_includes_notify_for_group_project(self):
        # Arrange: group project
        project = create_project(1, "Grupo", group_id=-1001)

        # Act: update status (pass group_id so the tool can find the project)
        result = await tools.execute_tool(
            "update_project_status",
            {"slug": "grupo", "status": "completado"},
            1,
            group_id=-1001,
        )
        payload = json.loads(result)

        # Assert: _notify metadata present
        assert payload["ok"] is True
        assert "_notify" in payload
        assert payload["_notify"]["type"] == "status"
        assert payload["_notify"]["project"]["group_id"] == -1001

    async def test_update_status_no_notify_for_private_project(self):
        # Arrange: private project
        project = create_project(1, "Privado")

        # Act: update status
        result = await tools.execute_tool(
            "update_project_status",
            {"slug": "privado", "status": "completado"},
            1,
        )
        payload = json.loads(result)

        # Assert: no _notify metadata
        assert payload["ok"] is True
        assert "_notify" not in payload

    async def test_add_note_includes_notify_for_group_project(self):
        # Arrange: group project
        project = create_project(1, "Grupo", group_id=-1001)

        # Act: add note (pass group_id so the tool can find the project)
        result = await tools.execute_tool(
            "add_project_note",
            {"slug": "grupo", "summary": "test note"},
            1,
            group_id=-1001,
        )
        payload = json.loads(result)

        # Assert: _notify metadata present
        assert payload["ok"] is True
        assert "_notify" in payload
        assert payload["_notify"]["type"] == "note"

    async def test_strip_notify_metadata(self):
        # Arrange: tool result with _notify
        result_json = json.dumps(
            {"ok": True, "project": {"id": 1}, "_notify": {"type": "status"}},
            ensure_ascii=False,
        )

        # Act
        from bot.handlers import _strip_notify_metadata
        stripped = _strip_notify_metadata(result_json)

        # Assert: _notify removed
        payload = json.loads(stripped)
        assert "_notify" not in payload
        assert payload["ok"] is True

    async def test_strip_notify_metadata_no_notify(self):
        # Arrange: tool result without _notify
        result_json = json.dumps({"ok": True, "project": {"id": 1}})

        # Act
        from bot.handlers import _strip_notify_metadata
        stripped = _strip_notify_metadata(result_json)

        # Assert: unchanged
        assert stripped == result_json

    async def test_strip_notify_metadata_invalid_json(self):
        # Arrange: invalid JSON
        result_json = "not json"

        # Act
        from bot.handlers import _strip_notify_metadata
        stripped = _strip_notify_metadata(result_json)

        # Assert: returned as-is
        assert stripped == result_json


class TestResolveSlugScopeIsolation:
    """_resolve_slug respects scope isolation between private and group."""

    def test_private_context_finds_private_project(self):
        create_project(1, "Proyecto Privado")
        result = tools._resolve_slug(1, "proyecto-privado", group_id=None)
        assert result is not None
        assert result["group_id"] is None

    def test_private_context_does_not_find_group_project(self):
        create_project(1, "Proyecto Grupo", group_id=-1001)
        result = tools._resolve_slug(1, "proyecto-grupo", group_id=None)
        assert result is None

    def test_group_context_finds_group_project(self):
        create_project(1, "Proyecto Grupo", group_id=-1001)
        result = tools._resolve_slug(1, "proyecto-grupo", group_id=-1001)
        assert result is not None
        assert result["group_id"] == -1001

    def test_group_context_does_not_find_private_project(self):
        create_project(1, "Proyecto Privado")
        result = tools._resolve_slug(1, "proyecto-privado", group_id=-1001)
        assert result is None

    def test_wrong_group_does_not_find_project(self):
        create_project(1, "Proyecto", group_id=-1001)
        result = tools._resolve_slug(1, "proyecto", group_id=-2002)
        assert result is None


class TestToolScopeIsolation:
    """Tool execution respects scope isolation between private and group."""

    async def test_update_status_private_context(self):
        """update_project_status in private context finds private project."""
        create_project(1, "Privado")
        result = await tools.execute_tool(
            "update_project_status",
            {"slug": "privado", "status": "completado"},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is True

    async def test_update_status_group_context(self):
        """update_project_status in group context finds group project."""
        create_project(1, "Grupo", group_id=-1001)
        result = await tools.execute_tool(
            "update_project_status",
            {"slug": "grupo", "status": "completado"},
            1,
            group_id=-1001,
        )
        payload = json.loads(result)
        assert payload["ok"] is True

    async def test_update_status_wrong_context_fails(self):
        """update_project_status in wrong context returns not found."""
        create_project(1, "Grupo", group_id=-1001)
        result = await tools.execute_tool(
            "update_project_status",
            {"slug": "grupo", "status": "completado"},
            1,
        )
        payload = json.loads(result)
        assert payload["ok"] is False
        assert "no encontrado" in payload["error"].lower()

    async def test_add_note_group_context(self):
        """add_project_note in group context finds group project."""
        create_project(1, "Grupo", group_id=-1001)
        result = await tools.execute_tool(
            "add_project_note",
            {"slug": "grupo", "summary": "test note"},
            1,
            group_id=-1001,
        )
        payload = json.loads(result)
        assert payload["ok"] is True

    async def test_get_project_group_context(self):
        """get_project in group context finds group project."""
        create_project(1, "Grupo", group_id=-1001)
        result = await tools.execute_tool(
            "get_project",
            {"slug": "grupo"},
            1,
            group_id=-1001,
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["project"]["group_id"] == -1001
