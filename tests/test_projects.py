"""Unit tests for bot.projects — SQLite CRUD + Markdown mirror."""

import json
import sqlite3
import time

from bot import projects


class TestSlugify:
    def test_basic_slug(self):
        assert projects.slugify("Arreglar el carro") == "arreglar-el-carro"

    def test_strips_special_chars(self):
        assert projects.slugify("Café 100%!") == "caf-100"

    def test_empty_becomes_project(self):
        assert projects.slugify("   ") == "project"

    def test_underscores_are_stripped(self):
        # Underscore is not in [a-z0-9\s-], so it is removed entirely.
        assert projects.slugify("mi_proyecto") == "miproyecto"


class TestCreateProject:
    def test_creates_and_returns_project(self):
        project = projects.create_project(1, "Arreglar el carro", "Cambiar cauchos")
        assert project is not None
        assert project["slug"] == "arreglar-el-carro"
        assert project["status"] == "sin_iniciar"
        assert project["user_id"] == 1

    def test_duplicate_slug_returns_none(self):
        projects.create_project(1, "Arreglar el carro")
        assert projects.create_project(1, "arreglar-el-carro") is None

    def test_same_slug_different_user_ok(self):
        projects.create_project(1, "Arreglar el carro")
        assert projects.create_project(2, "Arreglar el carro") is not None

    def test_creates_markdown_mirror(self, isolated_data_dir):
        projects.create_project(1, "Mi proyecto")
        base = isolated_data_dir / "projects" / "1" / "mi-proyecto"
        assert (base / "overview.md").exists()
        assert (base / "context.json").exists()
        assert (base / "notes").is_dir()


class TestUpdateProject:
    def test_update_status_valid(self):
        project = projects.create_project(1, "Proyecto")
        assert projects.update_project_status(project["id"], "completado") is True
        updated = projects.get_project_by_slug(1, "proyecto")
        assert updated["status"] == "completado"

    def test_update_status_invalid(self):
        project = projects.create_project(1, "Proyecto")
        assert projects.update_project_status(project["id"], "bogus") is False

    def test_update_status_missing_project(self):
        assert projects.update_project_status(999, "sin_iniciar") is False

    def test_update_description(self):
        project = projects.create_project(1, "Proyecto")
        assert projects.update_project_description(project["id"], "nueva desc") is True
        updated = projects.get_project_by_slug(1, "proyecto")
        assert updated["description"] == "nueva desc"


class TestDeleteProject:
    """delete_project: only the owner may delete an archived project."""

    def test_owner_deletes_archived_project(self, isolated_data_dir):
        # Arrange: an archived project with a note and a collaborator.
        project = projects.create_project(1, "Proyecto")
        projects.update_project_status(project["id"], "archivado")
        projects.add_project_note(project["id"], 1, "resumen", "contenido")
        projects.add_project_member(project["id"], 2, 1)
        assert projects.get_project_by_slug(1, "proyecto") is not None

        # Act: the owner deletes it.
        result = projects.delete_project(project["id"], 1)

        # Assert: ok, the DB row is gone and children cascaded.
        assert result == {"ok": True}
        assert projects.get_project_by_slug(1, "proyecto") is None
        assert projects.get_project_notes(project["id"]) == []
        assert projects.list_project_members(project["id"]) == []
        # The Markdown mirror directory is removed from disk too.
        project_dir = isolated_data_dir / "projects" / "1" / "proyecto"
        assert not project_dir.exists()

    def test_non_owner_delete_blocked(self):
        # Arrange: user 1 owns an archived project.
        project = projects.create_project(1, "Proyecto")
        projects.update_project_status(project["id"], "archivado")

        # Act: user 2 (not the owner) tries to delete it.
        result = projects.delete_project(project["id"], 2)

        # Assert: blocked and the row remains.
        assert result["ok"] is False
        assert "dueño" in result["error"]
        assert projects.get_project_by_slug(1, "proyecto") is not None

    def test_non_archived_project_delete_blocked(self):
        # Arrange: a completed (not archived) project.
        project = projects.create_project(1, "Proyecto")
        projects.update_project_status(project["id"], "completado")

        # Act: the owner tries to delete it anyway.
        result = projects.delete_project(project["id"], 1)

        # Assert: blocked — only archived projects can be deleted — row remains.
        assert result["ok"] is False
        assert "archivado" in result["error"]
        assert projects.get_project_by_slug(1, "proyecto") is not None

    def test_missing_project_returns_not_found(self):
        # Act: delete a project id that does not exist.
        result = projects.delete_project(999, 1)

        # Assert
        assert result == {"ok": False, "error": "Proyecto no encontrado"}

    def test_second_delete_of_deleted_project_fails_gracefully(self):
        # Arrange: an archived project that is deleted once.
        project = projects.create_project(1, "Proyecto")
        projects.update_project_status(project["id"], "archivado")
        first = projects.delete_project(project["id"], 1)
        assert first["ok"] is True

        # Act: try to delete the same project again.
        second = projects.delete_project(project["id"], 1)

        # Assert: graceful failure — the row is already gone.
        assert second["ok"] is False
        assert second["error"] == "Proyecto no encontrado"


class TestNotes:
    def test_add_note_returns_dict(self):
        project = projects.create_project(1, "Proyecto")
        note = projects.add_project_note(project["id"], 1, "resumen", "detalle")
        assert note is not None
        assert note["summary"] == "resumen"
        assert note["project_id"] == project["id"]

    def test_add_note_missing_project(self):
        assert projects.add_project_note(999, 1, "resumen") is None

    def test_note_content_written_to_md(self, isolated_data_dir):
        project = projects.create_project(1, "Proyecto")
        projects.add_project_note(project["id"], 1, "resumen", "contenido real")
        files = projects.read_project_files(1, "proyecto")
        note_files = [k for k in files if k.startswith("notes/")]
        assert len(note_files) == 1
        assert files[note_files[0]] == "contenido real"

    def test_get_project_notes(self):
        project = projects.create_project(1, "Proyecto")
        projects.add_project_note(project["id"], 1, "primera")
        projects.add_project_note(project["id"], 1, "segunda")
        notes = projects.get_project_notes(project["id"])
        assert len(notes) == 2
        assert [n["summary"] for n in notes] == ["primera", "segunda"]


class TestListAndSearch:
    def test_list_projects(self):
        projects.create_project(1, "Alpha")
        projects.create_project(1, "Beta")
        projects.create_project(2, "Gamma")
        assert len(projects.list_projects(1)) == 2

    def test_list_by_status(self):
        a = projects.create_project(1, "Alpha")
        b = projects.create_project(1, "Beta")
        projects.update_project_status(b["id"], "completado")
        active = projects.list_projects(1, "sin_iniciar")
        done = projects.list_projects(1, "completado")
        assert [p["slug"] for p in active] == ["alpha"]
        assert [p["slug"] for p in done] == ["beta"]

    def test_search_projects_by_name(self):
        projects.create_project(1, "Arreglar el carro")
        projects.create_project(1, "Remodelar la casa")
        results = projects.search_projects(1, "carro")
        assert len(results) == 1
        assert results[0]["name"] == "Arreglar el carro"

    def test_search_projects_by_description(self):
        projects.create_project(1, "Proyecto", "hablamos de gatos")
        results = projects.search_projects(1, "gatos")
        assert len(results) == 1

    def test_search_isolated_per_user(self):
        projects.create_project(1, "Arreglar el carro")
        assert projects.search_projects(2, "carro") == []

    def test_search_includes_shared_projects(self):
        # Arrange: user 2 is a collaborator on user 1's project.
        project = projects.create_project(1, "Arreglar el carro")
        projects.add_project_member(project["id"], 2, 1)

        # Act: user 2 searches — the shared project must be found.
        results = projects.search_projects(2, "carro")

        assert len(results) == 1
        assert results[0]["name"] == "Arreglar el carro"

    def test_search_does_not_include_other_users_shared_projects(self):
        # Arrange: user 1 shares with 2; user 3 is not involved.
        project = projects.create_project(1, "Proyecto secreto")
        projects.add_project_member(project["id"], 2, 1)

        # Assert: user 3 never sees it, even by search.
        assert projects.search_projects(3, "secreto") == []


class TestReadFiles:
    def test_read_project_files_empty_for_unknown(self, isolated_data_dir):
        assert projects.read_project_files(1, "no-existe") == {}


class TestFileIsolationPerUser:
    """Markdown storage is namespaced per user: projects/{user_id}/{slug}."""

    def test_same_slug_different_users_separate_dirs(self, isolated_data_dir):
        projects.create_project(1, "Arreglar el carro")
        projects.create_project(2, "Arreglar el carro")
        dir1 = isolated_data_dir / "projects" / "1" / "arreglar-el-carro"
        dir2 = isolated_data_dir / "projects" / "2" / "arreglar-el-carro"
        assert (dir1 / "overview.md").exists()
        assert (dir2 / "overview.md").exists()

    def test_same_slug_files_do_not_collide(self, isolated_data_dir):
        p1 = projects.create_project(1, "Arreglar el carro")
        p2 = projects.create_project(2, "Arreglar el carro")
        projects.add_project_note(p1["id"], 1, "nota de usuario 1", "contenido 1")
        projects.add_project_note(p2["id"], 2, "nota de usuario 2", "contenido 2")

        files1 = projects.read_project_files(1, "arreglar-el-carro")
        files2 = projects.read_project_files(2, "arreglar-el-carro")
        note1 = next(v for k, v in files1.items() if k.startswith("notes/"))
        note2 = next(v for k, v in files2.items() if k.startswith("notes/"))
        assert note1 == "contenido 1"
        assert note2 == "contenido 2"

    def test_read_project_files_scoped_to_user(self, isolated_data_dir):
        projects.create_project(1, "Arreglar el carro")
        assert projects.read_project_files(1, "arreglar-el-carro") != {}
        # A different user cannot read the same slug's files.
        assert projects.read_project_files(2, "arreglar-el-carro") == {}


class TestPriority:
    def test_project_priorities_defined(self):
        assert projects.PROJECT_PRIORITIES == ("baja", "media", "alta", "urgente")

    def test_default_priority_is_media(self):
        project = projects.create_project(1, "Proyecto")
        assert project["priority"] == "media"

    def test_create_project_with_priority_persists(self):
        project = projects.create_project(1, "Proyecto", priority="alta")
        assert project["priority"] == "alta"

    def test_create_urgent_projects_up_to_limit(self):
        for i in range(3):
            project = projects.create_project(1, f"Urgente {i}", priority="urgente")
            assert project["slug"] == f"urgente-{i}"
        assert projects.count_urgent_active_projects(1) == 3

    def test_create_fourth_urgent_project_fails_with_existing(self):
        urgent = [projects.create_project(1, f"Urgente {i}", priority="urgente") for i in range(3)]
        result = projects.create_project(1, "Urgente 3", priority="urgente")
        assert result["ok"] is False
        assert len(result["existing"]) == 3
        assert {p["slug"] for p in result["existing"]} == {p["slug"] for p in urgent}
        assert projects.count_urgent_active_projects(1) == 3

    def test_can_mark_urgent_under_limit(self):
        assert projects.can_mark_urgent(1) == (True, [])

    def test_can_mark_urgent_at_limit(self):
        for i in range(3):
            projects.create_project(1, f"Urgente {i}", priority="urgente")
        allowed, existing = projects.can_mark_urgent(1)
        assert allowed is False
        assert len(existing) == 3

    def test_update_project_priority(self):
        project = projects.create_project(1, "Proyecto")
        result = projects.update_project_priority(project["id"], "alta")
        assert result["ok"] is True
        assert result["project"]["priority"] == "alta"

    def test_update_project_priority_to_urgent_respects_limit(self):
        for i in range(3):
            projects.create_project(1, f"Urgente {i}", priority="urgente")
        normal = projects.create_project(1, "Normal")
        result = projects.update_project_priority(normal["id"], "urgente")
        assert result["ok"] is False
        assert len(result["existing"]) == 3

    def test_update_project_priority_after_freeing_slot(self):
        urgent = [projects.create_project(1, f"Urgente {i}", priority="urgente") for i in range(3)]
        normal = projects.create_project(1, "Normal")
        projects.update_project_priority(urgent[0]["id"], "media")
        result = projects.update_project_priority(normal["id"], "urgente")
        assert result["ok"] is True
        assert result["project"]["priority"] == "urgente"

    def test_update_project_priority_invalid(self):
        project = projects.create_project(1, "Proyecto")
        result = projects.update_project_priority(project["id"], "inexistente")
        assert result["ok"] is False

    def test_update_project_priority_missing_project(self):
        result = projects.update_project_priority(999, "alta")
        assert result["ok"] is False

    def test_count_urgent_active_ignores_completed(self):
        active = projects.create_project(1, "Activo", priority="urgente")
        completed = projects.create_project(1, "Completado", priority="urgente")
        projects.update_project_status(completed["id"], "completado")
        assert projects.count_urgent_active_projects(1) == 1

    def test_context_json_includes_priority(self, isolated_data_dir):
        projects.create_project(1, "Proyecto", priority="alta")
        context_path = isolated_data_dir / "projects" / "1" / "proyecto" / "context.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        assert context["priority"] == "alta"


class TestStatusMigration:
    """Projects start as sin_iniciar and can move through all Spanish statuses."""

    def test_new_project_has_sin_iniciar_status(self):
        # Arrange & Act
        project = projects.create_project(1, "Nuevo proyecto")

        # Assert
        assert project["status"] == "sin_iniciar"

    def test_update_status_to_each_valid_status(self):
        # Arrange
        for status in projects.PROJECT_STATUSES:
            project = projects.create_project(1, f"Proyecto {status}")

            # Act
            result = projects.update_project_status(project["id"], status)
            updated = projects.get_project_by_slug(1, project["slug"])

            # Assert
            assert result is True
            assert updated["status"] == status

    def test_invalid_status_is_rejected(self):
        # Arrange
        project = projects.create_project(1, "Proyecto")

        # Act
        result = projects.update_project_status(project["id"], "cancelado")

        # Assert
        assert result is False


class TestStoppedReason:
    """stopped_reason is only stored for the parado status."""

    def test_stopped_reason_is_stored_for_parado(self):
        # Arrange
        project = projects.create_project(1, "Proyecto")
        reason = "Esperando repuestos"

        # Act
        projects.update_project_status(project["id"], "parado", stopped_reason=reason)
        updated = projects.get_project_by_slug(1, "proyecto")

        # Assert
        assert updated["status"] == "parado"
        assert updated["stopped_reason"] == reason

    def test_stopped_reason_is_ignored_for_non_parado_statuses(self):
        # Arrange
        project = projects.create_project(1, "Proyecto")

        # Act
        projects.update_project_status(project["id"], "en_proceso", stopped_reason="no aplica")
        updated = projects.get_project_by_slug(1, "proyecto")

        # Assert
        assert updated["status"] == "en_proceso"
        assert updated["stopped_reason"] is None

    def test_stopped_reason_is_cleared_when_leaving_parado(self):
        # Arrange
        project = projects.create_project(1, "Proyecto")
        projects.update_project_status(project["id"], "parado", stopped_reason="Esperando repuestos")

        # Act
        projects.update_project_status(project["id"], "en_proceso")
        updated = projects.get_project_by_slug(1, "proyecto")

        # Assert
        assert updated["status"] == "en_proceso"
        assert updated["stopped_reason"] is None


class TestStatusChangedAt:
    """status_changed_at is set on creation and only changes with status updates."""

    def test_status_changed_at_is_set_on_creation(self):
        # Arrange
        before = time.time()

        # Act
        project = projects.create_project(1, "Proyecto")

        # Assert
        assert project["status_changed_at"] is not None
        assert before <= project["status_changed_at"] <= time.time()

    def test_status_changed_at_updates_on_status_change(self):
        # Arrange
        project = projects.create_project(1, "Proyecto")
        original_ts = project["status_changed_at"]
        time.sleep(0.01)

        # Act
        projects.update_project_status(project["id"], "en_proceso")
        updated = projects.get_project_by_slug(1, "proyecto")

        # Assert
        assert updated["status_changed_at"] > original_ts

    def test_status_changed_at_not_updated_on_description_change(self):
        # Arrange
        project = projects.create_project(1, "Proyecto")
        original_ts = project["status_changed_at"]
        time.sleep(0.01)

        # Act
        projects.update_project_description(project["id"], "nueva descripcion")
        updated = projects.get_project_by_slug(1, "proyecto")

        # Assert
        assert updated["description"] == "nueva descripcion"
        assert updated["status_changed_at"] == original_ts

    def test_status_changed_at_not_updated_on_priority_change(self):
        # Arrange
        project = projects.create_project(1, "Proyecto")
        original_ts = project["status_changed_at"]
        time.sleep(0.01)

        # Act
        projects.update_project_priority(project["id"], "alta")
        updated = projects.get_project_by_slug(1, "proyecto")

        # Assert
        assert updated["priority"] == "alta"
        assert updated["status_changed_at"] == original_ts


class TestContextJsonMirror:
    """context.json reflects priority, status, status_changed_at and stopped_reason."""

    def _load_context(self, isolated_data_dir, slug):
        path = isolated_data_dir / "projects" / "1" / slug / "context.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_context_includes_status_and_status_changed_at(self, isolated_data_dir):
        # Arrange
        project = projects.create_project(1, "Proyecto")

        # Act
        projects.update_project_status(project["id"], "en_proceso")

        # Assert
        context = self._load_context(isolated_data_dir, "proyecto")
        assert context["status"] == "en_proceso"
        assert isinstance(context["status_changed_at"], float)
        assert context["status_changed_at"] >= project["created_at"]

    def test_context_includes_stopped_reason_for_parado(self, isolated_data_dir):
        # Arrange
        project = projects.create_project(1, "Proyecto")
        reason = "Esperando aprobacion"

        # Act
        projects.update_project_status(project["id"], "parado", stopped_reason=reason)

        # Assert
        context = self._load_context(isolated_data_dir, "proyecto")
        assert context["status"] == "parado"
        assert context["stopped_reason"] == reason

    def test_context_omits_stopped_reason_when_not_parado(self, isolated_data_dir):
        # Arrange
        project = projects.create_project(1, "Proyecto")

        # Act
        projects.update_project_status(project["id"], "completado")

        # Assert
        context = self._load_context(isolated_data_dir, "proyecto")
        assert context["status"] == "completado"
        assert "stopped_reason" not in context


class TestUrgentPriorityLimitExcludesSelf:
    """update_project_priority must not count the project being updated."""

    def test_update_priority_to_urgent_excludes_the_updated_project(self):
        # Arrange: 3 urgent active projects plus one normal project
        for i in range(3):
            projects.create_project(1, f"Urgente {i}", priority="urgente")
        normal = projects.create_project(1, "Normal", priority="media")

        # Act: updating the normal project to urgent should fail (3 others already urgent)
        result = projects.update_project_priority(normal["id"], "urgente")

        # Assert
        assert result["ok"] is False
        assert len(result["existing"]) == 3
        assert normal["slug"] not in {p["slug"] for p in result["existing"]}

    def test_renewing_urgent_priority_on_same_project_allowed(self):
        # Arrange: fill the urgent slot, including the target project itself
        for i in range(2):
            projects.create_project(1, f"Otro {i}", priority="urgente")
        target = projects.create_project(1, "Objetivo", priority="urgente")

        # Act: re-applying urgent to the same project should succeed
        result = projects.update_project_priority(target["id"], "urgente")

        # Assert
        assert result["ok"] is True
        assert result["project"]["priority"] == "urgente"

    def test_create_project_rejects_invalid_priority(self):
        # Arrange & Act
        result = projects.create_project(1, "Proyecto", priority="critica")

        # Assert
        assert result["ok"] is False
        assert "critica" in result["error"]

    def test_update_priority_rejects_invalid_priority(self):
        # Arrange
        project = projects.create_project(1, "Proyecto")

        # Act
        result = projects.update_project_priority(project["id"], "critica")

        # Assert
        assert result["ok"] is False


class TestLegacyStatusMigration:
    """Existing rows with English statuses are migrated to Spanish."""

    def test_legacy_english_statuses_are_migrated(self, isolated_data_dir):
        # Arrange: build a legacy table with old English status values and
        # reconnect so the ALTER/migration path in _init_db actually runs.
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(isolated_data_dir / "data.db")
        conn.execute(
            "CREATE TABLE projects ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id INTEGER NOT NULL,"
            " slug TEXT NOT NULL,"
            " name TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'active',"
            " description TEXT DEFAULT '',"
            " created_at REAL NOT NULL,"
            " updated_at REAL NOT NULL,"
            " UNIQUE (user_id, slug))"
        )
        conn.execute(
            "INSERT INTO projects (user_id, slug, name, status, created_at, updated_at)"
            " VALUES (1, 'carro', 'El carro', 'active', 100.0, 100.0)"
        )
        conn.execute(
            "INSERT INTO projects (user_id, slug, name, status, created_at, updated_at)"
            " VALUES (1, 'tesis', 'La tesis', 'on_hold', 200.0, 200.0)"
        )
        conn.commit()
        conn.close()

        # Act: re-open the DB through the module (triggers migration).
        migrated = projects.get_project_by_slug(1, "carro")
        held = projects.get_project_by_slug(1, "tesis")

        # Assert: English values became their Spanish equivalents.
        assert migrated is not None
        assert migrated["status"] == "sin_iniciar"
        assert migrated["priority"] == "media"
        assert migrated["status_changed_at"] == 100.0
        assert held["status"] == "parado"

    def test_spanish_statuses_are_not_double_migrated(self, isolated_data_dir):
        # Arrange: a project in a Spanish status.
        project = projects.create_project(1, "Proyecto")

        # Act: open/close the DB multiple times (migration runs each time).
        for _ in range(3):
            projects.get_project_by_slug(1, "proyecto")

        # Assert: status unchanged (migration is idempotent).
        current = projects.get_project_by_slug(1, "proyecto")
        assert current["status"] == "sin_iniciar"


class TestStatusReactivationUrgentLimit:
    """Moving an urgent project back to an active status must respect the limit."""

    def test_reactivating_urgent_beyond_limit_rejected(self):
        # Arrange: one urgent project set to parado, then 3 urgent active ones.
        paused = projects.create_project(1, "Parado urgente", priority="urgente")
        projects.update_project_status(paused["id"], "parado", stopped_reason="espera")
        for i in range(3):
            projects.create_project(1, f"Urgente {i}", priority="urgente")

        # Act: try to reactivate the parado urgent project.
        ok = projects.update_project_status(paused["id"], "en_proceso")

        # Assert: rejected — would create a 4th urgent active project.
        assert ok is False
        current = projects.get_project_by_slug(1, "parado-urgente")
        assert current["status"] == "parado"

    def test_reactivating_urgent_when_slot_available_allowed(self):
        # Arrange: one urgent project set to parado, then 2 urgent active ones.
        paused = projects.create_project(1, "Parado urgente", priority="urgente")
        projects.update_project_status(paused["id"], "parado", stopped_reason="espera")
        for i in range(2):
            projects.create_project(1, f"Urgente {i}", priority="urgente")

        # Act: reactivating is allowed (only 2 active urgent remain).
        ok = projects.update_project_status(paused["id"], "en_proceso")

        # Assert: succeeded.
        assert ok is True
        current = projects.get_project_by_slug(1, "parado-urgente")
        assert current["status"] == "en_proceso"

    def test_reactivating_non_urgent_not_blocked(self):
        # Arrange: 3 urgent active projects and one normal parado project.
        for i in range(3):
            projects.create_project(1, f"Urgente {i}", priority="urgente")
        paused = projects.create_project(1, "Parado normal", priority="media")
        projects.update_project_status(paused["id"], "parado")

        # Act: a normal (non-urgent) project can always be reactivated.
        ok = projects.update_project_status(paused["id"], "sin_iniciar")

        # Assert: succeeded.
        assert ok is True

    def test_can_activate_urgent_helper(self):
        # Arrange: 3 urgent active projects.
        for i in range(3):
            projects.create_project(1, f"Urgente {i}", priority="urgente")

        # Act & Assert: no more urgent activations allowed.
        can, existing = projects.can_activate_urgent(1, 999999)
        assert can is False
        assert len(existing) == 3


class TestNoOpStatusTimestamp:
    """A no-op status change must not bump status_changed_at."""

    def test_noop_status_change_keeps_timestamp(self):
        # Arrange: create and change status once.
        project = projects.create_project(1, "Proyecto")
        projects.update_project_status(project["id"], "en_proceso")
        first_change = projects.get_project_by_slug(1, "proyecto")["status_changed_at"]

        # Act: set the same status again.
        projects.update_project_status(project["id"], "en_proceso")

        # Assert: status_changed_at unchanged.
        current = projects.get_project_by_slug(1, "proyecto")
        assert current["status_changed_at"] == first_change

    def test_real_status_change_bumps_timestamp(self):
        # Arrange: create and change status once.
        project = projects.create_project(1, "Proyecto")
        projects.update_project_status(project["id"], "parado", stopped_reason="x")
        first_change = projects.get_project_by_slug(1, "proyecto")["status_changed_at"]

        # Act: change to a different status.
        projects.update_project_status(project["id"], "completado")

        # Assert: status_changed_at advanced.
        current = projects.get_project_by_slug(1, "proyecto")
        assert current["status_changed_at"] >= first_change


# --- Project sharing: users table --------------------------------------------


class TestUsersTable:
    """upsert_user records profiles; resolve_username maps @username → user_id."""

    def test_upsert_user_inserts(self):
        projects.upsert_user(1, "@mama", "Mama")
        assert projects.resolve_username("@mama") == 1

    def test_upsert_user_updates_username(self):
        # Arrange: record the user under one username.
        projects.upsert_user(1, "@mama", "Mama")

        # Act: the same user changes their Telegram username.
        projects.upsert_user(1, "@nueva", "Mama")

        # Assert: only the latest username resolves; the old one is gone.
        assert projects.resolve_username("@mama") is None
        assert projects.resolve_username("@nueva") == 1

    def test_upsert_user_none_username(self):
        # Act: a user without a Telegram username must not crash the upsert.
        projects.upsert_user(1, None, "X")

        # Assert: the row exists with username NULL and first_name stored.
        conn = sqlite3.connect(str(projects.DB_PATH))
        try:
            row = conn.execute(
                "SELECT username, first_name FROM users WHERE user_id = 1"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] is None
        assert row[1] == "X"

    def test_resolve_username_case_insensitive(self):
        projects.upsert_user(1, "Mama", "Mama")
        assert projects.resolve_username("mama") == 1

    def test_resolve_username_unknown_returns_none(self):
        assert projects.resolve_username("nadie") is None

    def test_resolve_username_handles_at_symbol(self):
        projects.upsert_user(1, "usuario", "U")
        assert projects.resolve_username("@Usuario") == 1

    def test_resolve_username_collision_favors_freshest(self):
        # Arrange: user 1 used to be @casa but renamed (stale row persists);
        # user 2 later took the @casa handle and messaged the bot.
        projects.upsert_user(1, "@casa", "Antiguo")
        projects.upsert_user(1, "@nuevo", "Antiguo")
        projects.upsert_user(2, "@casa", "Nuevo")

        # Act/Assert: the freshest profile wins, so an invitation can never
        # be routed to an arbitrary/unknown account.
        assert projects.resolve_username("@casa") == 2
        assert projects.get_user_profile(1) == {
            "username": "nuevo",
            "first_name": "Antiguo",
        }


# --- Project sharing: ACL gate ------------------------------------------------


class TestGetProjectForUser:
    """get_project_for_user is the ACL gate: owner or invited member only."""

    def test_owner_sees_project(self):
        projects.create_project(1, "Arreglar el carro")
        project = projects.get_project_for_user(1, "arreglar-el-carro")
        assert project is not None
        assert project["slug"] == "arreglar-el-carro"

    def test_member_sees_shared_project(self):
        # Arrange: user 1 owns a project and invites user 2.
        project = projects.create_project(1, "Arreglar el carro")
        projects.add_project_member(project["id"], 2, 1)

        # Act & Assert: user 2 (invited) sees the project; user_id is the owner's.
        shared = projects.get_project_for_user(2, "arreglar-el-carro")
        assert shared is not None
        assert shared["slug"] == "arreglar-el-carro"
        assert shared["user_id"] == 1

    def test_non_member_gets_none(self):
        # SECURITY: a user with no ownership or membership row must not see it.
        projects.create_project(1, "Arreglar el carro")
        assert projects.get_project_for_user(3, "arreglar-el-carro") is None

    def test_unknown_slug_returns_none(self):
        projects.create_project(1, "Arreglar el carro")
        assert projects.get_project_for_user(1, "no-existe") is None

    def test_literal_name_resolves(self):
        # Callers may pass the raw name ("Mi Proyecto") instead of the slug.
        projects.create_project(1, "Mi Proyecto")
        project = projects.get_project_for_user(1, "Mi Proyecto")
        assert project is not None
        assert project["slug"] == "mi-proyecto"


class TestOwnershipAndMembership:
    def test_is_owner_true_false(self):
        # Arrange: user 1 owns the project, user 2 does not.
        project = projects.create_project(1, "Proyecto")

        # Act & Assert
        assert projects.is_owner(1, project["id"]) is True
        assert projects.is_owner(2, project["id"]) is False
        assert projects.is_owner(1, 999) is False

    def test_is_project_member_true_false(self):
        # Arrange: project owned by 1; 2 is invited, 3 is not.
        project = projects.create_project(1, "Proyecto")
        projects.add_project_member(project["id"], 2, 1)

        # Act & Assert: the owner is not a "member" row; only invited users are.
        assert projects.is_project_member(2, project["id"]) is True
        assert projects.is_project_member(3, project["id"]) is False
        assert projects.is_project_member(1, project["id"]) is False
        assert projects.is_project_member(2, 999) is False


# --- Project sharing: invitations ----------------------------------------------


class TestAddProjectMember:
    def test_add_member_ok(self):
        # Arrange: user 1 owns a project.
        project = projects.create_project(1, "Proyecto")

        # Act: user 1 invites user 2.
        result = projects.add_project_member(project["id"], 2, 1)

        # Assert: ok, and user 2 immediately sees the project (no acceptance flow).
        assert result["ok"] is True
        assert projects.get_project_for_user(2, "proyecto") is not None

    def test_duplicate_member_rejected(self):
        # Arrange: user 2 is already a member.
        project = projects.create_project(1, "Proyecto")
        projects.add_project_member(project["id"], 2, 1)

        # Act: invite the same user again.
        result = projects.add_project_member(project["id"], 2, 1)

        # Assert: friendly Spanish error instead of a raw IntegrityError.
        assert result["ok"] is False
        assert "ya es miembro" in result["error"]

    def test_missing_project_rejected(self):
        result = projects.add_project_member(999, 2, 1)
        assert result["ok"] is False
        assert "no encontrado" in result["error"]

    def test_self_invitation_rejected(self):
        # SECURITY-CRITICAL: nobody can grant themselves access to a project.

        # 1. The owner inviting themselves is rejected (they already own it).
        project = projects.create_project(1, "Proyecto")
        result_owner = projects.add_project_member(project["id"], 1, 1)
        assert result_owner["ok"] is False
        assert "contigo mismo" in result_owner["error"]

        # 2. A non-owner (user 2) trying to add themselves is rejected too.
        result_intruder = projects.add_project_member(project["id"], 2, 2)
        assert result_intruder["ok"] is False
        assert "contigo mismo" in result_intruder["error"]

        # 3. After the attempt, user 2 still has NO access — no row leaked.
        assert projects.get_project_for_user(2, "proyecto") is None

    def test_non_owner_cannot_add_members(self):
        # SECURITY: a collaborator cannot invite others to a project they
        # do not own (privilege escalation via invitation is blocked).
        project = projects.create_project(1, "Proyecto")
        projects.add_project_member(project["id"], 2, 1)

        # Act: member 2 tries to add user 3 (invited_by=2, not the owner).
        result = projects.add_project_member(project["id"], 3, 2)

        assert result["ok"] is False
        assert "dueño" in result["error"]
        assert projects.get_project_for_user(3, "proyecto") is None


class TestRemoveProjectMember:
    def test_remove_member(self):
        # Arrange: user 2 is a member and can see the project.
        project = projects.create_project(1, "Proyecto")
        projects.add_project_member(project["id"], 2, 1)
        assert projects.get_project_for_user(2, "proyecto") is not None

        # Act: owner revokes access.
        removed = projects.remove_project_member(project["id"], 2, actor_id=1)

        # Assert: revocation is immediate; the ex-member no longer sees it.
        assert removed["ok"] is True
        assert projects.get_project_for_user(2, "proyecto") is None

    def test_remove_unknown_returns_false(self):
        project = projects.create_project(1, "Proyecto")
        result = projects.remove_project_member(project["id"], 999, actor_id=1)
        assert result["ok"] is False
        assert "no tiene acceso" in result["error"]
        missing = projects.remove_project_member(999, 2, actor_id=1)
        assert missing["ok"] is False
        assert "no encontrado" in missing["error"]

    def test_non_owner_cannot_remove(self):
        # Security: a collaborator cannot revoke another member's access.
        project = projects.create_project(1, "Proyecto")
        projects.add_project_member(project["id"], 2, 1)
        projects.add_project_member(project["id"], 3, 1)

        result = projects.remove_project_member(project["id"], 3, actor_id=2)

        assert result["ok"] is False
        assert "dueño" in result["error"]
        assert projects.get_project_for_user(3, "proyecto") is not None

    def test_remove_does_not_delete_project(self):
        # Arrange: project with one member.
        project = projects.create_project(1, "Proyecto")
        projects.add_project_member(project["id"], 2, 1)

        # Act: remove the only member.
        projects.remove_project_member(project["id"], 2, actor_id=1)

        # Assert: only the membership row was deleted; the project survives.
        assert projects.get_project_by_slug(1, "proyecto") is not None
        assert projects.get_project_for_user(1, "proyecto") is not None


# --- Project sharing: listing ---------------------------------------------------


class TestListProjectMembers:
    def test_list_empty(self):
        project = projects.create_project(1, "Proyecto")
        assert projects.list_project_members(project["id"]) == []

    def test_list_with_members(self):
        # Arrange: user 1 invites user 2, whose profile exists in the users table.
        project = projects.create_project(1, "Proyecto")
        projects.upsert_user(2, "@mama", "Mama")
        projects.add_project_member(project["id"], 2, 1)

        # Act
        members = projects.list_project_members(project["id"])

        # Assert: the row carries the resolved profile plus invitation metadata.
        assert len(members) == 1
        assert members[0]["user_id"] == 2
        assert members[0]["username"] == "mama"
        assert members[0]["first_name"] == "Mama"
        assert members[0]["invited_by"] == 1


class TestMemberCount:
    def test_zero_then_increments(self):
        # Arrange: a project with no collaborators yet.
        project = projects.create_project(1, "Proyecto")
        assert projects.get_member_count(project["id"]) == 0

        # Act: invite two collaborators.
        projects.add_project_member(project["id"], 2, 1)
        assert projects.get_member_count(project["id"]) == 1
        projects.add_project_member(project["id"], 3, 1)

        # Assert: each invite increments the count.
        assert projects.get_member_count(project["id"]) == 2


class TestListScopes:
    """owned / shared-with-me / shared-by-me are three disjoint scopes."""

    def test_list_owned_projects(self):
        # Arrange: user 1 owns "Mio" and is only a member of user 2's project.
        projects.create_project(1, "Mio")
        other = projects.create_project(2, "De otro")
        projects.add_project_member(other["id"], 1, 2)

        # Act & Assert: shared-with-me projects are NOT owned projects.
        owned = projects.list_owned_projects(1)
        assert [p["slug"] for p in owned] == ["mio"]

    def test_list_owned_projects_with_status_filter(self):
        # Arrange
        a = projects.create_project(1, "Alpha")
        b = projects.create_project(1, "Beta")
        projects.update_project_status(b["id"], "completado")

        # Act
        active = projects.list_owned_projects(1, "sin_iniciar")
        done = projects.list_owned_projects(1, "completado")

        # Assert
        assert [p["slug"] for p in active] == ["alpha"]
        assert [p["slug"] for p in done] == ["beta"]

    def test_list_shared_with_me(self):
        # Arrange: user 1 shares a project with user 2.
        project = projects.create_project(1, "Proyecto")
        projects.add_project_member(project["id"], 2, 1)

        # Act
        shared = projects.list_shared_with_me(2)

        # Assert: user 2 sees it, carrying the owner_id for the UI.
        assert len(shared) == 1
        assert shared[0]["slug"] == "proyecto"
        assert shared[0]["owner_id"] == 1

    def test_list_shared_by_me(self):
        # Arrange: user 1 shares one project with two collaborators.
        project = projects.create_project(1, "Proyecto")
        projects.add_project_member(project["id"], 2, 1)
        projects.add_project_member(project["id"], 3, 1)

        # Act
        shared = projects.list_shared_by_me(1)

        # Assert: exactly one project, with the member count attached.
        assert len(shared) == 1
        assert shared[0]["slug"] == "proyecto"
        assert shared[0]["member_count"] == 2

    def test_scopes_do_not_cross(self):
        # Arrange: three unrelated sharing graphs.
        # User 1 owns "Mio" and shares it with user 2.
        mine = projects.create_project(1, "Mio")
        projects.add_project_member(mine["id"], 2, 1)

        # User 3 owns "De 3" and shares it with user 4 — not with user 1.
        theirs = projects.create_project(3, "De 3")
        projects.add_project_member(theirs["id"], 4, 3)

        # User 5 owns "Otro" and shares it with user 3 — not with user 2.
        other = projects.create_project(5, "Otro")
        projects.add_project_member(other["id"], 3, 5)

        # Act: query user 1's shared-by-me scope.
        by_me = projects.list_shared_by_me(1)

        # Assert: user 3's and user 5's projects are not part of user 1's scope.
        assert [p["slug"] for p in by_me] == ["mio"]

        # Act: query user 2's shared-with-me scope.
        with_me = projects.list_shared_with_me(2)

        # Assert: "Otro" (shared with user 3) does not leak into user 2's scope.
        assert [p["slug"] for p in with_me] == ["mio"]


class TestGetGroupProjectNotifyTargets:
    """get_group_project_notify_targets: returns group members to notify."""

    def test_returns_group_members_excluding_actor(self):
        # Arrange: create a group project with owner + members
        project = projects.create_project(1, "Proyecto", group_id=-1001)
        projects.add_project_member(project["id"], 2, 1)
        projects.add_project_member(project["id"], 3, 1)
        projects.add_group_member(-1001, 1)
        projects.add_group_member(-1001, 2)
        projects.add_group_member(-1001, 3)

        # Act: get targets excluding the owner (actor)
        targets = projects.get_group_project_notify_targets(
            project["id"], exclude_user_id=1
        )

        # Assert: owner excluded, members 2 and 3 included
        assert sorted(targets) == [2, 3]

    def test_returns_empty_for_private_project(self):
        # Arrange: a private project (no group_id)
        project = projects.create_project(1, "Privado")

        # Act
        targets = projects.get_group_project_notify_targets(project["id"])

        # Assert
        assert targets == []

    def test_includes_owner_if_in_group(self):
        # Arrange: owner is also in the group
        project = projects.create_project(1, "Proyecto", group_id=-1001)
        projects.add_group_member(-1001, 1)

        # Act: get all targets (no exclusion)
        targets = projects.get_group_project_notify_targets(project["id"])

        # Assert: owner is included
        assert 1 in targets

    def test_excludes_actor_not_in_members(self):
        # Arrange: group project with members, actor not in group_members
        project = projects.create_project(1, "Proyecto", group_id=-1001)
        projects.add_project_member(project["id"], 2, 1)
        projects.add_group_member(-1001, 2)

        # Act: exclude user 1 (owner/actor, not in group_members)
        targets = projects.get_group_project_notify_targets(
            project["id"], exclude_user_id=1
        )

        # Assert: only member 2
        assert targets == [2]


class TestGetProjectOwnerName:
    """get_project_owner_name: returns display name for notifications."""

    def test_username(self):
        projects.upsert_user(1, "jose", "Jose")
        assert projects.get_project_owner_name(1) == "@jose"

    def test_first_name_only(self):
        projects.upsert_user(1, None, "María")
        assert projects.get_project_owner_name(1) == "María"

    def test_unknown_user(self):
        assert projects.get_project_owner_name(99999) == "Alguien"


# --- Scope isolation: private vs group -------------------------------------------


class TestScopeIsolation:
    """Scope isolation: private and group projects must never mix."""

    def test_different_projects_in_different_contexts(self):
        """Different project names can exist in private and group."""
        private = projects.create_project(1, "Proyecto Privado")
        group = projects.create_project(1, "Proyecto del Grupo", group_id=-1001)

        assert private is not None
        assert group is not None
        assert private["id"] != group["id"]
        assert private["group_id"] is None
        assert group["group_id"] == -1001

    def test_private_context_finds_only_private(self):
        """get_project_for_user with group_id=None finds only private projects."""
        projects.create_project(1, "Proyecto Privado")
        projects.create_project(1, "Proyecto del Grupo", group_id=-1001)

        # Private context: finds the private project
        found = projects.get_project_for_user(1, "proyecto-privado", group_id=None)
        assert found is not None
        assert found["group_id"] is None

    def test_group_context_finds_only_group(self):
        """get_project_for_user with group_id finds only group projects."""
        projects.create_project(1, "Proyecto Privado")
        projects.create_project(1, "Proyecto del Grupo", group_id=-1001)

        # Group context: finds the group project
        found = projects.get_project_for_user(1, "proyecto-del-grupo", group_id=-1001)
        assert found is not None
        assert found["group_id"] == -1001

    def test_private_context_does_not_find_group_project(self):
        """Private context cannot see group projects."""
        projects.create_project(1, "Proyecto Grupo", group_id=-1001)

        found = projects.get_project_for_user(1, "proyecto-grupo", group_id=None)
        assert found is None

    def test_group_context_does_not_find_private_project(self):
        """Group context cannot see private projects."""
        projects.create_project(1, "Proyecto Privado")

        found = projects.get_project_for_user(1, "proyecto-privado", group_id=-1001)
        assert found is None

    def test_wrong_group_does_not_find_project(self):
        """A project in group A is not visible from group B."""
        projects.create_project(1, "Proyecto", group_id=-1001)

        found = projects.get_project_for_user(1, "proyecto", group_id=-2002)
        assert found is None

    def test_search_private_context(self):
        """search_projects with group_id=None finds only private projects."""
        projects.create_project(1, "Arreglar carro")
        projects.create_project(1, "Comprar repuesto", group_id=-1001)

        results = projects.search_projects(1, "carro", group_id=None)
        assert len(results) == 1
        assert results[0]["group_id"] is None

    def test_search_group_context(self):
        """search_projects with group_id finds only group projects."""
        projects.create_project(1, "Arreglar carro")
        projects.create_project(1, "Comprar repuesto", group_id=-1001)

        results = projects.search_projects(1, "repuesto", group_id=-1001)
        assert len(results) == 1
        assert results[0]["group_id"] == -1001

    def test_member_can_find_group_project_in_group_context(self):
        """Invited member can find the project in group context."""
        project = projects.create_project(1, "Proyecto", group_id=-1001)
        projects.add_project_member(project["id"], 2, 1)

        found = projects.get_project_for_user(2, "proyecto", group_id=-1001)
        assert found is not None

    def test_member_cannot_find_group_project_in_private_context(self):
        """Invited member cannot find group project from private context."""
        project = projects.create_project(1, "Proyecto", group_id=-1001)
        projects.add_project_member(project["id"], 2, 1)

        found = projects.get_project_for_user(2, "proyecto", group_id=None)
        assert found is None

    def test_get_shared_projects_in_group_isolation(self):
        """get_shared_projects_in_group only returns projects for that group."""
        projects.create_project(1, "Grupo A Project", group_id=-1001)
        projects.create_project(1, "Grupo B Project", group_id=-2002)
        projects.create_project(1, "Privado")
        # Owner must be a group member for the project to appear
        projects.add_group_member(-1001, 1)
        projects.add_group_member(-2002, 1)

        group_a = projects.get_shared_projects_in_group(-1001)
        assert len(group_a) == 1
        assert group_a[0]["group_id"] == -1001

    def test_list_group_projects_for_user_isolation(self):
        """list_group_projects_for_user only returns group projects."""
        projects.create_project(1, "Privado")
        projects.create_project(1, "Grupo A", group_id=-1001)
        projects.create_project(1, "Grupo B", group_id=-2002)

        group_projects = projects.list_group_projects_for_user(1)
        assert len(group_projects) == 2
        assert all(p["group_id"] is not None for p in group_projects)
