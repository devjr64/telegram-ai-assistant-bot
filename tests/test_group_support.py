"""Tests for group support functionality."""

import json
import time

import pytest

from bot import projects
from bot.tools import execute_tool, GROUP_TOOLS, PRIVATE_TOOLS


class TestGroupMembers:
    def test_add_and_get_group_members(self):
        projects.add_group_member(100, 1)
        projects.add_group_member(100, 2)
        projects.add_group_member(100, 3)

        members = projects.get_group_members(100)
        assert sorted(members) == [1, 2, 3]

    def test_add_group_member_idempotent(self):
        projects.add_group_member(100, 1)
        projects.add_group_member(100, 1)  # duplicate

        members = projects.get_group_members(100)
        assert members == [1]

    def test_get_group_members_empty(self):
        assert projects.get_group_members(999) == []


class TestSharedProjectsInGroup:
    def test_returns_group_projects(self):
        # Create a project with group_id
        project = projects.create_project(1, "Proyecto Grupo", group_id=100)
        assert project is not None
        assert project["group_id"] == 100

        # Add user as group member
        projects.add_group_member(100, 1)

        # Should return the project
        shared = projects.get_shared_projects_in_group(100)
        assert len(shared) == 1
        assert shared[0]["id"] == project["id"]

    def test_excludes_private_projects(self):
        # Create a private project (no group_id)
        project = projects.create_project(1, "Proyecto Privado")
        assert project["group_id"] is None

        projects.add_group_member(100, 1)

        # Should not return private projects
        shared = projects.get_shared_projects_in_group(100)
        assert len(shared) == 0

    def test_excludes_other_groups_projects(self):
        # Create project in group 200
        project = projects.create_project(1, "Proyecto Grupo 200", group_id=200)
        projects.add_group_member(100, 1)

        # Query group 100 - should not find it
        shared = projects.get_shared_projects_in_group(100)
        assert len(shared) == 0

    def test_returns_projects_with_members_in_group(self):
        # Create project with user 1 as owner
        project = projects.create_project(1, "Proyecto", group_id=100)
        projects.add_project_member(project["id"], 2, 1)

        # Add both users to group
        projects.add_group_member(100, 1)
        projects.add_group_member(100, 2)

        shared = projects.get_shared_projects_in_group(100)
        assert len(shared) == 1


class TestPrivateChatId:
    def test_get_private_chat_id_returns_none_for_unknown(self):
        assert projects.get_private_chat_id(999) is None

    def test_upsert_user_stores_private_chat_id(self):
        projects.upsert_user(1, "testuser", "Test", private_chat_id=12345)
        assert projects.get_private_chat_id(1) == 12345

    def test_upsert_user_updates_private_chat_id(self):
        projects.upsert_user(1, "testuser", "Test", private_chat_id=12345)
        projects.upsert_user(1, "testuser", "Test", private_chat_id=67890)
        assert projects.get_private_chat_id(1) == 67890

    def test_upsert_user_preserves_existing_private_chat_id(self):
        projects.upsert_user(1, "testuser", "Test", private_chat_id=12345)
        # Update without private_chat_id should preserve it
        projects.upsert_user(1, "testuser", "Test Updated")
        assert projects.get_private_chat_id(1) == 12345


class TestCreateGroupProject:
    def test_creates_project_with_group_id(self):
        projects.add_group_member(100, 1)
        projects.add_group_member(100, 2)

        project = projects.create_group_project(1, 100, "Proyecto Grupo")
        assert project is not None
        assert project["group_id"] == 100
        assert project["user_id"] == 1

    def test_auto_adds_group_members(self):
        projects.add_group_member(100, 1)
        projects.add_group_member(100, 2)
        projects.add_group_member(100, 3)

        project = projects.create_group_project(1, 100, "Proyecto Grupo")

        # Owner should not be added as member
        members = projects.list_project_members(project["id"])
        member_ids = [m["user_id"] for m in members]
        assert 2 in member_ids
        assert 3 in member_ids
        assert 1 not in member_ids  # owner is not a member


class TestMigrateGroupProjectsToPrivate:
    def test_migrates_projects_to_private(self):
        # Create group project
        project = projects.create_project(1, "Proyecto", group_id=100)
        projects.add_project_member(project["id"], 2, 1)
        projects.add_group_member(100, 1)
        projects.add_group_member(100, 2)

        # Migrate
        projects.migrate_group_projects_to_private(100)

        # Project should now be private
        updated = projects.get_project_by_slug(1, "proyecto")
        assert updated is not None
        assert updated["group_id"] is None

        # Members should be removed
        members = projects.list_project_members(project["id"])
        assert len(members) == 0

        # Group members should be cleaned up
        assert projects.get_group_members(100) == []


class TestGroupToolsScope:
    def test_group_tools_are_subset_of_all_tools(self):
        # GROUP_TOOLS should only contain tools that make sense in group context
        assert "create_project" in GROUP_TOOLS
        assert "list_projects" in GROUP_TOOLS
        assert "create_reminder" in GROUP_TOOLS

    def test_private_tools_not_in_group(self):
        # Only curated private-only tools are blocked in group (v1 whitelist)
        assert "generate_image" in PRIVATE_TOOLS
        assert "set_checkin_time" in PRIVATE_TOOLS
        assert "delete_project" in PRIVATE_TOOLS
        assert "share_project" in PRIVATE_TOOLS
        assert "unshare_project" in PRIVATE_TOOLS
        assert "list_project_members" in PRIVATE_TOOLS
        # Parity tools are now allowed in group
        assert "journal_append" not in PRIVATE_TOOLS
        assert "memory_recall" not in PRIVATE_TOOLS
        assert "list_reminders" not in PRIVATE_TOOLS
        assert "web_search" not in PRIVATE_TOOLS
        assert "dolar_rate" not in PRIVATE_TOOLS


@pytest.mark.asyncio
class TestExecuteToolGroupScope:
    async def test_blocks_private_tools_in_group(self):
        result = await execute_tool(
            "generate_image",
            {"prompt": "test"},
            user_id=1,
            group_id=100,
        )
        data = json.loads(result)
        assert data["ok"] is False
        assert "privados" in data["error"]

    async def test_allows_group_tools_in_group(self):
        # list_projects should work in group context
        result = await execute_tool(
            "list_projects",
            {},
            user_id=1,
            group_id=100,
        )
        data = json.loads(result)
        assert data["ok"] is True

    async def test_create_reminder_in_group_delivers_in_group(self):
        # In group, create_reminder should succeed without private_chat_id (delivers in group)
        from bot.projects import add_group_member
        add_group_member(100, 1)
        result = await execute_tool(
            "create_reminder",
            {"message": "test", "datetime": "2026-12-25T10:00"},
            user_id=1,
            group_id=100,
        )
        data = json.loads(result)
        assert data["ok"] is True
        assert data["reminder"]["group_id"] == 100
        assert data["reminder"]["chat_id"] == 100

    async def test_create_reminder_uses_group_id_even_with_private_chat(self):
        # With private_chat_id, group still wins (delivery in group)
        from bot.projects import add_group_member
        add_group_member(100, 1)
        result = await execute_tool(
            "create_reminder",
            {"message": "test reminder", "datetime": "2026-12-25T10:00"},
            user_id=1,
            group_id=100,
            private_chat_id=12345,
        )
        data = json.loads(result)
        assert data["ok"] is True
        assert data["reminder"]["chat_id"] == 100

    async def test_tools_work_normally_without_group_id(self):
        # Tools should work normally when no group_id is provided
        result = await execute_tool(
            "list_projects",
            {},
            user_id=1,
        )
        data = json.loads(result)
        assert data["ok"] is True


class TestCreateProjectWithGroupId:
    def test_creates_project_with_group_id(self):
        project = projects.create_project(1, "Proyecto", group_id=100)
        assert project is not None
        assert project["group_id"] == 100

    def test_creates_private_project_when_no_group_id(self):
        project = projects.create_project(1, "Proyecto Privado")
        assert project is not None
        assert project["group_id"] is None
