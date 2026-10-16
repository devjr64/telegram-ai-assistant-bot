"""Integration tests for bot.project_commands — Spanish project commands."""

import pytest

from bot import project_commands as pc
from bot.projects import (
    add_project_member,
    create_project,
    get_project_for_user,
    list_project_members,
    update_project_status,
    upsert_user,
)
from bot.reminders import upsert_user_chat

from fakes import FakeUpdate, FakeContext, FakeCallbackQuery, FakeMessage


def make_context(settings, args=None):
    return FakeContext(settings, args=args)


def authorize(monkeypatch, value=True):
    monkeypatch.setattr(pc, "is_authorized", lambda uid: value)


class TestProyectosCommand:
    async def test_no_projects_message(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings)
        await pc.proyectos_command(update, context)
        assert "Aún no tienes proyectos" in update.message.replies[0]["text"]
        assert "/nuevoproyecto" in update.message.replies[0]["text"]

    async def test_lists_projects(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        create_project(1, "Arreglar el carro")
        update = FakeUpdate()
        context = make_context(sample_settings)
        await pc.proyectos_command(update, context)
        text = update.message.replies[0]["text"]
        assert "Arreglar el carro" in text
        assert "Sin iniciar" in text
        assert "Media" in text
        assert "Último cambio de estado" in text

    async def test_filters_by_status(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        finished = create_project(1, "Terminado")
        update_project_status(finished["id"], "completado")
        create_project(1, "Activo")
        update = FakeUpdate()
        context = make_context(sample_settings, args=["completado"])
        await pc.proyectos_command(update, context)
        text = update.message.replies[0]["text"]
        assert "Terminado" in text
        assert "Activo" not in text
        assert "Completado" in text

    async def test_invalid_status_filter(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings, args=["inexistente"])
        await pc.proyectos_command(update, context)
        assert "Estado no reconocido" in update.message.replies[0]["text"]

    async def test_filters_by_status_spanish_label(self, sample_settings, monkeypatch):

        # Arrange: create projects in different states.
        authorize(monkeypatch)
        finished = create_project(1, "Terminado")
        update_project_status(finished["id"], "completado")
        create_project(1, "Activo")

        # Act: filter using the Spanish display label instead of snake_case.
        update = FakeUpdate()
        context = make_context(sample_settings, args=["Completado"])
        await pc.proyectos_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: only the completed project appears.
        assert "Terminado" in text
        assert "Activo" not in text
        assert "Completado" in text

    async def test_filters_by_multiword_status(self, sample_settings, monkeypatch):
        # Arrange: a project in "en proceso" and another in "sin iniciar".
        authorize(monkeypatch)
        started = create_project(1, "En curso")
        update_project_status(started["id"], "en_proceso")
        create_project(1, "Pendiente")

        # Act: filter with the two-word Spanish status phrase.
        update = FakeUpdate()
        context = make_context(sample_settings, args=["en", "proceso"])
        await pc.proyectos_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: only the "en proceso" project appears.
        assert "En curso" in text
        assert "Pendiente" not in text

    async def test_denies_unauthorized(self, sample_settings, monkeypatch):
        authorize(monkeypatch, value=False)
        update = FakeUpdate()
        context = make_context(sample_settings)
        await pc.proyectos_command(update, context)
        assert "no estás autorizado" in update.message.replies[0]["text"]


class TestNuevoProyectoCommand:
    async def test_creates_project(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings, args=["Comprar repuesto"])
        await pc.nuevoproyecto_command(update, context)
        text = update.message.replies[0]["text"]
        assert "creado" in text
        assert "/agregarnota" in text
        assert "/estadoproyecto" in text
        assert "/prioridadproyecto" in text

    async def test_missing_name_shows_usage(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings)
        await pc.nuevoproyecto_command(update, context)
        assert "Uso" in update.message.replies[0]["text"]
        assert "/nuevoproyecto" in update.message.replies[0]["text"]

    async def test_duplicate_project_message(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        create_project(1, "Comprar repuesto")
        update = FakeUpdate()
        context = make_context(sample_settings, args=["Comprar repuesto"])
        await pc.nuevoproyecto_command(update, context)
        text = update.message.replies[0]["text"]
        assert "Ya existe" in text
        assert "/proyecto" in text


class TestProyectoCommand:
    async def test_shows_details(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        create_project(1, "Arreglar el carro")
        update = FakeUpdate()
        context = make_context(sample_settings, args=["Arreglar el carro"])
        await pc.proyecto_command(update, context)
        text = update.message.replies[0]["text"]
        assert "Arreglar el carro" in text
        assert "Prioridad" in text
        assert "Estado" in text
        assert "Creado" in text
        assert "Último cambio de estado" in text
        assert "/agregarnota" in text
        assert "/estadoproyecto" in text
        assert "/prioridadproyecto" in text

    async def test_not_found(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings, args=["No existe"])
        await pc.proyecto_command(update, context)
        assert "No encontré" in update.message.replies[0]["text"]


class TestEstadoProyectoCommand:
    async def test_updates_status(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        create_project(1, "Arreglar el carro")
        update = FakeUpdate()
        context = make_context(
            sample_settings, args=["Arreglar", "el", "carro", "completado"]
        )
        await pc.estadoproyecto_command(update, context)
        text = update.message.replies[0]["text"]
        assert "actualizado" in text
        assert "Completado" in text

    async def test_updates_status_with_reason(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        create_project(1, "Arreglar el carro")
        update = FakeUpdate()
        context = make_context(
            sample_settings,
            args=["Arreglar", "el", "carro", "parado", "falta", "repuesto"],
        )
        await pc.estadoproyecto_command(update, context)
        text = update.message.replies[0]["text"]
        assert "Parado" in text
        assert "falta repuesto" in text

    async def test_invalid_status(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        create_project(1, "Proyecto")
        update = FakeUpdate()
        context = make_context(sample_settings, args=["Proyecto", "bogus"])
        await pc.estadoproyecto_command(update, context)
        text = update.message.replies[0]["text"]
        assert "Estados válidos" in text or "Uso" in text

    async def test_missing_status(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings, args=["Proyecto"])
        await pc.estadoproyecto_command(update, context)
        assert "Uso" in update.message.replies[0]["text"]

    async def test_updates_status_with_spanish_label(self, sample_settings, monkeypatch):
        # Arrange: a project exists and we use the Spanish display label for the status.
        authorize(monkeypatch)
        create_project(1, "Arreglar el carro")
        update = FakeUpdate()
        context = make_context(
            sample_settings, args=["Arreglar", "el", "carro", "Completado"]
        )

        # Act: change status using the human-readable label.
        await pc.estadoproyecto_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: the status is recognized and updated.
        assert "actualizado" in text
        assert "Completado" in text

    async def test_missing_project_name(self, sample_settings, monkeypatch):
        # Arrange: no project name is provided.
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings, args=["completado"])

        # Act: invoke the command with only a status.
        await pc.estadoproyecto_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: the handler asks for the project name.
        assert "Uso" in text
        assert "<nombre>" in text

    async def test_updates_status_with_multiword_status(self, sample_settings, monkeypatch):
        # Arrange: a project exists.
        authorize(monkeypatch)
        create_project(1, "Arreglar el carro")

        # Act: use the two-word Spanish status phrase "en proceso".
        update = FakeUpdate()
        context = make_context(
            sample_settings, args=["Arreglar", "el", "carro", "en", "proceso"]
        )
        await pc.estadoproyecto_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: recognized and applied.
        assert "actualizado" in text
        assert "En proceso" in text

    async def test_reactivating_urgent_beyond_limit_blocked(self, sample_settings, monkeypatch):
        # Arrange: one urgent project set to parado, then 3 urgent active ones.
        authorize(monkeypatch)
        paused = create_project(1, "Proyecto especial", priority="urgente")
        update_project_status(paused["id"], "parado", stopped_reason="espera")
        for i in range(3):
            create_project(1, f"Urgente {i}", priority="urgente")

        # Act: try to reactivate the parado urgent project.
        update = FakeUpdate()
        context = make_context(
            sample_settings, args=["Proyecto", "especial", "en", "proceso"]
        )
        await pc.estadoproyecto_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: blocked with a friendly message listing urgent projects.
        assert "3 proyectos urgentes" in text
        assert "Urgente 0" in text

    async def test_member_reactivation_blocked_by_owner_urgent_count(
        self, sample_settings, monkeypatch
    ):
        # Arrange: owner 111 has 3 urgent active; shared project is parado urgent.
        authorize(monkeypatch)
        shared = create_project(111, "Compartido", priority="urgente")
        update_project_status(shared["id"], "parado", stopped_reason="espera")
        for i in range(3):
            create_project(111, f"Urgente {i}", priority="urgente")
        add_project_member(shared["id"], 222, 111)

        # Act: member 222 (0 urgent) tries to reactivate the owner's project.
        update = FakeUpdate(user_id=222)
        context = make_context(
            sample_settings, args=["Compartido", "en", "proceso"]
        )
        await pc.estadoproyecto_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: the limit counts against the OWNER, and no false success.
        assert "3 proyectos urgentes" in text
        assert "actualizado" not in text

    async def test_member_reactivation_not_blocked_by_member_own_urgent_count(
        self, sample_settings, monkeypatch
    ):
        # Arrange: owner 111 has 0 urgent; member 222 has 3 urgent of their own.
        authorize(monkeypatch)
        shared = create_project(111, "Compartido", priority="urgente")
        update_project_status(shared["id"], "parado", stopped_reason="espera")
        add_project_member(shared["id"], 222, 111)
        for i in range(3):
            create_project(222, f"Mío urgente {i}", priority="urgente")

        # Act: member 222 reactivates the shared project.
        update = FakeUpdate(user_id=222)
        context = make_context(
            sample_settings, args=["Compartido", "en", "proceso"]
        )
        await pc.estadoproyecto_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: the member's own urgent count must not block the owner's project.
        assert "actualizado" in text
        assert "3 proyectos urgentes" not in text


class TestPrioridadProyectoCommand:
    async def test_missing_args_shows_usage(self, sample_settings, monkeypatch):
        # Arrange: command invoked without enough arguments.
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings, args=["Proyecto"])

        # Act: invoke the priority command with only the project name.
        await pc.prioridadproyecto_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: usage instructions are shown.
        assert "Uso" in text
        assert "/prioridadproyecto" in text

    async def test_updates_priority(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        create_project(1, "Proyecto")
        update = FakeUpdate()
        context = make_context(sample_settings, args=["Proyecto", "alta"])
        await pc.prioridadproyecto_command(update, context)
        text = update.message.replies[0]["text"]
        assert "actualizada" in text
        assert "Alta" in text

    async def test_invalid_priority(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        create_project(1, "Proyecto")
        update = FakeUpdate()
        context = make_context(sample_settings, args=["Proyecto", "bogus"])
        await pc.prioridadproyecto_command(update, context)
        text = update.message.replies[0]["text"]
        assert "Prioridad inválida" in text

    async def test_urgent_limit_shows_existing(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        for i in range(3):
            create_project(1, f"Urgente {i}", priority="urgente")
        normal = create_project(1, "Normal")
        update = FakeUpdate()
        context = make_context(sample_settings, args=["Normal", "urgente"])
        await pc.prioridadproyecto_command(update, context)
        text = update.message.replies[0]["text"]
        assert "3 proyectos urgentes" in text
        assert "Urgente 0" in text
        assert "Urgente 1" in text
        assert "Urgente 2" in text

    async def test_project_not_found(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings, args=["NoExiste", "alta"])
        await pc.prioridadproyecto_command(update, context)
        assert "No encontré" in update.message.replies[0]["text"]


class TestAgregarNotaCommand:
    async def test_adds_note(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        create_project(1, "Proyecto")
        update = FakeUpdate()
        context = make_context(sample_settings, args=["Proyecto", "avance", "de", "hoy"])
        await pc.agregarnota_command(update, context)
        assert "Nota agregada" in update.message.replies[0]["text"]

    async def test_missing_project(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings, args=["NoExiste", "una", "nota"])
        await pc.agregarnota_command(update, context)
        text = update.message.replies[0]["text"]
        assert "No encontré" in text
        assert "/agregarnota" in text

    async def test_adds_note_to_multiword_project(self, sample_settings, monkeypatch):
        # Arrange: a project whose name has several words.
        authorize(monkeypatch)
        create_project(1, "Arreglar el carro")

        # Act: reference it by full name in /agregarnota.
        update = FakeUpdate()
        context = make_context(
            sample_settings, args=["Arreglar", "el", "carro", "comprar", "repuesto"]
        )
        await pc.agregarnota_command(update, context)

        # Assert: the note lands on the multi-word project.
        text = update.message.replies[0]["text"]
        assert "Nota agregada" in text
        assert "Arreglar el carro" in text


class TestBuscarProyectoCommand:
    async def test_searches(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        create_project(1, "Arreglar el carro")
        update = FakeUpdate()
        context = make_context(sample_settings, args=["carro"])
        await pc.buscarproyecto_command(update, context)
        text = update.message.replies[0]["text"]
        assert "Arreglar el carro" in text
        assert "Sin iniciar" in text

    async def test_no_results(self, sample_settings, monkeypatch):
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings, args=["zzz"])
        await pc.buscarproyecto_command(update, context)
        assert "No encontré" in update.message.replies[0]["text"]


class TestCompartirCommand:
    """/compartir — the owner grants an invitee immediate access to a project."""

    @staticmethod
    def _setup(monkeypatch) -> dict:
        """Arrange an owned project plus a known invitee profile.

        User 111 owns "Proyecto" and user 222 (@mama) is a known user the
        owner can invite. Callers may add more users/memberships on top.
        """
        authorize(monkeypatch)
        upsert_user(222, "mama", "Mama")
        return create_project(111, "Proyecto")

    async def test_owner_shares_project_and_invitee_gets_access(
        self, sample_settings, monkeypatch
    ):
        # Arrange: owner 111 owns "Proyecto"; @mama (user 222) is known.
        self._setup(monkeypatch)
        update = FakeUpdate(user_id=111)
        context = make_context(sample_settings, args=["Proyecto", "@mama"])

        # Act: the owner shares the project with @mama.
        await pc.compartir_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: reply confirms sharing, and access is immediate (no acceptance).
        assert "compartido" in text
        assert get_project_for_user(222, "proyecto") is not None

    async def test_owner_gets_notification_sent(self, sample_settings, monkeypatch):
        # Arrange: @mama has an active chat with the bot (user_chats row).
        self._setup(monkeypatch)
        upsert_user_chat(222, 777)
        update = FakeUpdate(user_id=111)
        context = make_context(sample_settings, args=["Proyecto", "@mama"])

        # Act: the owner shares the project.
        await pc.compartir_command(update, context)

        # Assert: the invitee receives a proactive message mentioning the project.
        assert any(
            msg["chat_id"] == 777 and "Proyecto" in msg["text"]
            for msg in context.bot.sent_messages
        )

    async def test_non_owner_cannot_share(self, sample_settings, monkeypatch):
        # Arrange: 222 is a collaborator of 111's project; @papa is a known user.
        project = self._setup(monkeypatch)
        add_project_member(project["id"], 222, 111)
        upsert_user(333, "papa", "Papa")
        update = FakeUpdate(user_id=222)
        context = make_context(sample_settings, args=["Proyecto", "@papa"])

        # Act: the collaborator tries to share the project further.
        await pc.compartir_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: only the owner may share; @papa gains no access and 222 keeps it.
        assert "Solo el dueño" in text
        assert get_project_for_user(333, "proyecto") is None
        assert get_project_for_user(222, "proyecto") is not None

    async def test_self_share_rejected(self, sample_settings, monkeypatch):
        # Arrange: the owner has their own resolvable username.
        project = self._setup(monkeypatch)
        upsert_user(111, "jose", "Jose")
        update = FakeUpdate(user_id=111)
        context = make_context(sample_settings, args=["Proyecto", "@jose"])

        # Act: the owner tries to share the project with themselves.
        await pc.compartir_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: rejected, and no membership row was created.
        assert "contigo mismo" in text
        assert list_project_members(project["id"]) == []

    async def test_unknown_username_rejected(self, sample_settings, monkeypatch):
        # Arrange: no user named @nadie has ever spoken to the bot.
        self._setup(monkeypatch)
        update = FakeUpdate(user_id=111)
        context = make_context(sample_settings, args=["Proyecto", "@nadie"])

        # Act: the owner invites an unknown user.
        await pc.compartir_command(update, context)

        # Assert: the bot cannot resolve the username and says so.
        assert "No conozco a ese usuario" in update.message.replies[0]["text"]

    async def test_missing_args_shows_usage(self, sample_settings, monkeypatch):
        # Arrange: the command is invoked without arguments.
        self._setup(monkeypatch)
        update = FakeUpdate(user_id=111)
        context = make_context(sample_settings)

        # Act: /compartir with no project/username.
        await pc.compartir_command(update, context)

        # Assert: usage instructions are shown.
        assert "Uso" in update.message.replies[0]["text"]
        assert "/compartir" in update.message.replies[0]["text"]


class TestQuitaraccesoCommand:
    """/quitaracceso — the owner revokes a collaborator's access."""

    async def test_owner_removes_access(self, sample_settings, monkeypatch):
        # Arrange: 111 owns "Proyecto" and shared it with @mama (user 222).
        authorize(monkeypatch)
        upsert_user(222, "mama", "Mama")
        project = create_project(111, "Proyecto")
        add_project_member(project["id"], 222, 111)
        assert get_project_for_user(222, "proyecto") is not None
        update = FakeUpdate(user_id=111)
        context = make_context(sample_settings, args=["Proyecto", "@mama"])

        # Act: the owner revokes @mama's access.
        await pc.quitaracceso_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: reply confirms revocation and access is gone immediately.
        assert "Se quitó el acceso" in text
        assert get_project_for_user(222, "proyecto") is None

    async def test_non_owner_cannot_remove(self, sample_settings, monkeypatch):
        # Arrange: 222 and 333 are collaborators of 111's project.
        authorize(monkeypatch)
        upsert_user(222, "mama", "Mama")
        upsert_user(333, "papa", "Papa")
        project = create_project(111, "Proyecto")
        add_project_member(project["id"], 222, 111)
        add_project_member(project["id"], 333, 111)
        update = FakeUpdate(user_id=222)
        context = make_context(sample_settings, args=["Proyecto", "@papa"])

        # Act: collaborator 222 tries to revoke collaborator 333.
        await pc.quitaracceso_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: only the owner may revoke; nobody lost access.
        assert "Solo el dueño" in text
        assert get_project_for_user(222, "proyecto") is not None
        assert get_project_for_user(333, "proyecto") is not None

    async def test_cannot_remove_owner(self, sample_settings, monkeypatch):
        # Arrange: the owner has a resolvable username.
        authorize(monkeypatch)
        upsert_user(111, "jose", "Jose")
        create_project(111, "Proyecto")
        update = FakeUpdate(user_id=111)
        context = make_context(sample_settings, args=["Proyecto", "@jose"])

        # Act: the owner tries to revoke their own access.
        await pc.quitaracceso_command(update, context)

        # Assert: the owner's access can never be removed.
        assert "al dueño" in update.message.replies[0]["text"]

    async def test_unknown_user_rejected(self, sample_settings, monkeypatch):
        # Arrange: owner 111 with a project; @nadie is not a known user.
        authorize(monkeypatch)
        create_project(111, "Proyecto")
        update = FakeUpdate(user_id=111)
        context = make_context(sample_settings, args=["Proyecto", "@nadie"])

        # Act: the owner tries to revoke an unknown user.
        await pc.quitaracceso_command(update, context)

        # Assert: the bot cannot resolve the username and says so.
        assert "No conozco a ese usuario" in update.message.replies[0]["text"]


class TestProyectosFilters:
    """/proyectos scope filters: dueños, compartidos, invitado."""

    @staticmethod
    def _setup() -> None:
        """Arrange a sharing graph: 111 owns A (shared with 222); 222 owns B."""
        project_a = create_project(111, "Proyecto A")
        add_project_member(project_a["id"], 222, 111)
        create_project(222, "Proyecto B")

    async def test_default_lists_owned_only(self, sample_settings, monkeypatch):
        # Arrange: 222 owns B and is only a member of 111's A.
        authorize(monkeypatch)
        self._setup()
        update = FakeUpdate(user_id=222)
        context = make_context(sample_settings)

        # Act: list projects with the default (owned) scope.
        await pc.proyectos_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: only the owned project B is listed; shared A is not.
        assert "Tus proyectos" in text
        assert "Proyecto B" in text
        assert "Proyecto A" not in text

    async def test_dueños_filter(self, sample_settings, monkeypatch):
        # Arrange: the same ownership/membership graph.
        authorize(monkeypatch)
        self._setup()
        update = FakeUpdate(user_id=222)
        context = make_context(sample_settings, args=["dueños"])

        # Act: explicitly list owned projects.
        await pc.proyectos_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: only the owned project appears.
        assert "Tus proyectos" in text
        assert "Proyecto B" in text
        assert "Proyecto A" not in text

    async def test_compartidos_filter(self, sample_settings, monkeypatch):
        # Arrange: 111 shares A with one collaborator (222).
        authorize(monkeypatch)
        self._setup()
        update = FakeUpdate(user_id=111)
        context = make_context(sample_settings, args=["compartidos"])

        # Act: list projects the owner shared with others.
        await pc.proyectos_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: A appears with the member count; 222's B is not in this scope.
        assert "Proyectos compartidos por mí" in text
        assert "Proyecto A" in text
        assert "compartido con 1" in text
        assert "Proyecto B" not in text

    async def test_invitado_filter(self, sample_settings, monkeypatch):
        # Arrange: 222 owns B and is invited to 111's A.
        authorize(monkeypatch)
        self._setup()
        update = FakeUpdate(user_id=222)
        context = make_context(sample_settings, args=["invitado"])

        # Act: list projects shared with user 222.
        await pc.proyectos_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: A appears labelled with its owner; owned B is not listed.
        assert "Proyectos compartidos conmigo" in text
        assert "Proyecto A" in text
        assert "(de usuario 111)" in text
        assert "Proyecto B" not in text

    async def test_scope_plus_status(self, sample_settings, monkeypatch):
        # Arrange: 111's A is shared with 222; 222's B is completed.
        authorize(monkeypatch)
        self._setup()
        finished = create_project(222, "Terminado")
        update_project_status(finished["id"], "completado")
        update = FakeUpdate(user_id=222)
        context = make_context(sample_settings, args=["dueños", "completado"])

        # Act: owned scope combined with a status filter.
        await pc.proyectos_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: only the completed owned project appears, with the state shown.
        assert "Tus proyectos" in text
        assert "Completado" in text
        assert "Terminado" in text
        assert "Proyecto A" not in text
        assert "Proyecto B" not in text


class TestProyectoDetailShowsSharing:
    """/proyecto exposes ownership and sharing info to the right audience."""

    async def test_invitee_sees_owner(self, sample_settings, monkeypatch):
        # Arrange: 111 owns A and shared it with 222.
        authorize(monkeypatch)
        project = create_project(111, "Proyecto A")
        add_project_member(project["id"], 222, 111)
        update = FakeUpdate(user_id=222)
        context = make_context(sample_settings, args=["Proyecto A"])

        # Act: the invitee opens the project detail.
        await pc.proyecto_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: the owner is shown to the collaborator.
        assert "Dueño" in text

    async def test_owner_sees_shared_list(self, sample_settings, monkeypatch):
        # Arrange: 111 owns A and shared it with @mama (user 222).
        authorize(monkeypatch)
        upsert_user(222, "mama", "Mama")
        project = create_project(111, "Proyecto A")
        add_project_member(project["id"], 222, 111)
        update = FakeUpdate(user_id=111)
        context = make_context(sample_settings, args=["Proyecto A"])

        # Act: the owner opens the project detail.
        await pc.proyecto_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: the collaborator list names @mama.
        assert "Compartido con" in text
        assert "@mama" in text


class TestEliminarProyectoCommand:
    """/eliminarproyecto — ask the owner to confirm deleting an archived project."""

    async def test_missing_name_shows_usage(self, sample_settings, monkeypatch):
        # Arrange: an authorized user runs the command with no arguments.
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings)

        # Act: /eliminarproyecto with no project name.
        await pc.eliminarproyecto_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: usage instructions are shown.
        assert "Uso" in text
        assert "/eliminarproyecto" in text

    async def test_project_not_found(self, sample_settings, monkeypatch):
        # Arrange: an authorized user names a project that does not exist.
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings, args=["No existe"])

        # Act: /eliminarproyecto with an unknown project name.
        await pc.eliminarproyecto_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: the bot says the project was not found.
        assert "No encontré" in text

    async def test_non_owner_blocked(self, sample_settings, monkeypatch):
        # Arrange: owner 111 has an archived project shared with member 222.
        authorize(monkeypatch)
        project = create_project(111, "Proyecto A")
        update_project_status(project["id"], "archivado")
        add_project_member(project["id"], 222, 111)
        update = FakeUpdate(user_id=222)
        context = make_context(sample_settings, args=["Proyecto A"])

        # Act: the collaborator tries to delete the owner's project.
        await pc.eliminarproyecto_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: only the owner may delete; no confirmation is rendered.
        assert "Solo el dueño" in text
        assert "reply_markup" not in update.message.replies[0]

    async def test_non_archived_blocked(self, sample_settings, monkeypatch):
        # Arrange: an owned project is still completed, not archived.
        authorize(monkeypatch)
        project = create_project(1, "Terminado")
        update_project_status(project["id"], "completado")
        update = FakeUpdate()
        context = make_context(sample_settings, args=["Terminado"])

        # Act: try to delete a non-archived project.
        await pc.eliminarproyecto_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: the bot requires archiving first; no confirmation is rendered.
        assert "archivado" in text
        assert "reply_markup" not in update.message.replies[0]

    async def test_archived_project_shows_confirmation(self, sample_settings, monkeypatch):
        # Arrange: the owner has an archived project.
        authorize(monkeypatch)
        project = create_project(1, "Archivado")
        update_project_status(project["id"], "archivado")
        update = FakeUpdate()
        context = make_context(sample_settings, args=["Archivado"])

        # Act: run /eliminarproyecto on the archived project.
        await pc.eliminarproyecto_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: a confirmation prompt with both inline buttons is rendered.
        assert "¿Eliminar definitivamente" in text
        markup = update.message.replies[0]["reply_markup"]
        callbacks = [
            btn["callback_data"]
            for row in markup.to_dict()["inline_keyboard"]
            for btn in row
        ]
        assert f"delproj:{project['id']}" in callbacks
        assert f"delproj_cancel:{project['id']}" in callbacks

    async def test_denies_unauthorized(self, sample_settings, monkeypatch):
        # Arrange: the user is not authorized at all.
        authorize(monkeypatch, value=False)
        update = FakeUpdate()
        context = make_context(sample_settings, args=["Archivado"])

        # Act: run the command while unauthorized.
        await pc.eliminarproyecto_command(update, context)

        # Assert: the access gate rejects the user.
        assert "no estás autorizado" in update.message.replies[0]["text"]


class TestDeleteProjectCallback:
    """/eliminarproyecto inline buttons — confirm or cancel the deletion."""

    @staticmethod
    def _archived_project() -> dict:
        """Create and archive a project owned by user 1."""
        project = create_project(1, "Proyecto viejo")
        update_project_status(project["id"], "archivado")
        return project

    async def test_cancel_does_not_delete(self, sample_settings, monkeypatch):
        # Arrange: an archived project and a "Cancelar" button tap.
        authorize(monkeypatch)
        project = self._archived_project()
        query = FakeCallbackQuery(
            data=f"delproj_cancel:{project['id']}",
            message=FakeMessage(),
            user_id=1,
        )
        update = FakeUpdate(callback_query=query)
        context = make_context(sample_settings)

        # Act: the owner taps Cancelar.
        await pc.delete_project_callback(update, context)

        # Assert: the bot answers and edits the message but keeps the project.
        assert query.answered
        assert "Cancelado" in query.answer_text
        assert query.message.edited_messages
        assert get_project_for_user(1, project["slug"]) is not None

    async def test_confirm_deletes(self, sample_settings, monkeypatch):
        # Arrange: an archived project and a "Sí, eliminar" button tap.
        authorize(monkeypatch)
        project = self._archived_project()
        query = FakeCallbackQuery(
            data=f"delproj:{project['id']}",
            message=FakeMessage(),
            user_id=1,
        )
        update = FakeUpdate(callback_query=query)
        context = make_context(sample_settings)

        # Act: the owner confirms the deletion.
        await pc.delete_project_callback(update, context)

        # Assert: the project is gone, the bot answered and edited the message.
        assert get_project_for_user(1, project["slug"]) is None
        assert query.answered
        assert "Proyecto eliminado" in query.answer_text
        assert query.message.edited_messages

    async def test_foreign_user_rejected(self, sample_settings, monkeypatch):
        # Arrange: user 111 owns the project; user 222 taps the button.
        authorize(monkeypatch)
        project = create_project(111, "Proyecto ajeno")
        update_project_status(project["id"], "archivado")
        query = FakeCallbackQuery(
            data=f"delproj:{project['id']}",
            message=FakeMessage(),
            user_id=222,
        )
        update = FakeUpdate(callback_query=query)
        context = make_context(sample_settings)

        # Act: the foreign user taps confirm.
        await pc.delete_project_callback(update, context)

        # Assert: rejected with an alert and the project survives.
        assert query.answered
        assert query.show_alert is True
        assert get_project_for_user(111, project["slug"]) is not None

    async def test_malformed_data_alerted(self, sample_settings, monkeypatch):
        # Arrange: callback data whose id is not an integer.
        authorize(monkeypatch)
        query = FakeCallbackQuery(
            data="delproj:abc", message=FakeMessage(), user_id=1
        )
        update = FakeUpdate(callback_query=query)
        context = make_context(sample_settings)

        # Act: a malformed button is tapped.
        await pc.delete_project_callback(update, context)

        # Assert: an alert explains the invalid button.
        assert query.answered
        assert query.show_alert is True
        assert "Botón no válido." in query.answer_text

    async def test_unknown_prefix_alerted(self, sample_settings, monkeypatch):
        # Arrange: callback data with an unknown prefix.
        authorize(monkeypatch)
        query = FakeCallbackQuery(
            data="weird:1", message=FakeMessage(), user_id=1
        )
        update = FakeUpdate(callback_query=query)
        context = make_context(sample_settings)

        # Act: an unknown button is tapped.
        await pc.delete_project_callback(update, context)

        # Assert: an alert says the action is unknown.
        assert query.answered
        assert query.show_alert is True
        assert "Acción desconocida." in query.answer_text


class TestProyectosScopeFilters:
    """Tests for /proyectos scope filters: todos, mios, compartidos, invitado."""

    async def test_todos_shows_all_projects(self, sample_settings, monkeypatch):
        # Arrange: owned + shared-with-me projects
        authorize(monkeypatch)
        owned = create_project(1, "Mío")
        shared = create_project(2, "De otro")
        add_project_member(shared["id"], 1, 2)

        # Act: list with "todos" scope
        update = FakeUpdate()
        context = make_context(sample_settings, args=["todos"])
        await pc.proyectos_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: both projects appear
        assert "Mío" in text
        assert "De otro" in text

    async def test_mios_shows_only_owned(self, sample_settings, monkeypatch):
        # Arrange: owned + shared-with-me projects
        authorize(monkeypatch)
        owned = create_project(1, "Mío")
        shared = create_project(2, "De otro")
        add_project_member(shared["id"], 1, 2)

        # Act: list with "mios" scope
        update = FakeUpdate()
        context = make_context(sample_settings, args=["mios"])
        await pc.proyectos_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: only owned project appears
        assert "Mío" in text
        assert "De otro" not in text

    async def test_invitado_shows_shared_with_me(self, sample_settings, monkeypatch):
        # Arrange: owned + shared-with-me projects
        authorize(monkeypatch)
        owned = create_project(1, "Mío")
        shared = create_project(2, "De otro")
        add_project_member(shared["id"], 1, 2)

        # Act: list with "invitado" scope
        update = FakeUpdate()
        context = make_context(sample_settings, args=["invitado"])
        await pc.proyectos_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: only shared project appears, with owner label
        assert "De otro" in text
        assert "Mío" not in text

    async def test_todos_empty_message(self, sample_settings, monkeypatch):
        # Arrange: no projects at all
        authorize(monkeypatch)
        update = FakeUpdate()
        context = make_context(sample_settings, args=["todos"])
        await pc.proyectos_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: appropriate empty message
        assert "No tienes ningún proyecto" in text

    async def test_todos_header(self, sample_settings, monkeypatch):
        # Arrange: one project
        authorize(monkeypatch)
        create_project(1, "Proyecto")

        # Act
        update = FakeUpdate()
        context = make_context(sample_settings, args=["todos"])
        await pc.proyectos_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: header says "Todos tus proyectos"
        assert "Todos tus proyectos" in text

    async def test_todos_with_status_filter(self, sample_settings, monkeypatch):
        # Arrange: projects in different statuses
        authorize(monkeypatch)
        active = create_project(1, "Activo")
        update_project_status(active["id"], "en_proceso")
        done = create_project(1, "Terminado")
        update_project_status(done["id"], "completado")

        # Act: filter by status
        update = FakeUpdate()
        context = make_context(sample_settings, args=["todos", "completado"])
        await pc.proyectos_command(update, context)
        text = update.message.replies[0]["text"]

        # Assert: only completed project
        assert "Terminado" in text
        assert "Activo" not in text
