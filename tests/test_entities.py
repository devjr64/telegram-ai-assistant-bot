"""Tests for the entity tracking system."""

import json
import time

import pytest

from bot import entities


class TestCreateEntity:
    """Tests for create_entity function."""

    def test_creates_person_entity(self, tmp_path):
        """Can create a person entity."""
        entity = entities.create_entity(
            user_id=12345,
            entity_type="person",
            name="María",
            alias="Mari",
            metadata={"relación": "esposa"},
        )
        assert entity is not None
        assert entity["name"] == "María"
        assert entity["alias"] == "Mari"
        assert entity["entity_type"] == "person"
        assert entity["status"] == "active"
        assert entity["metadata"]["relación"] == "esposa"

    def test_creates_place_entity(self, tmp_path):
        """Can create a place entity."""
        entity = entities.create_entity(
            user_id=12345,
            entity_type="place",
            name="Hospital Central",
            metadata={"dirección": "Av. Principal"},
        )
        assert entity is not None
        assert entity["entity_type"] == "place"

    def test_creates_case_entity(self, tmp_path):
        """Can create a case entity."""
        entity = entities.create_entity(
            user_id=12345,
            entity_type="case",
            name="Llevar documentos al banco",
        )
        assert entity is not None
        assert entity["entity_type"] == "case"

    def test_creates_item_entity(self, tmp_path):
        """Can create an item entity."""
        entity = entities.create_entity(
            user_id=12345,
            entity_type="item",
            name="Pasaporte",
        )
        assert entity is not None
        assert entity["entity_type"] == "item"

    def test_returns_existing_entity_on_duplicate(self, tmp_path):
        """Returns existing entity when creating duplicate."""
        entity1 = entities.create_entity(
            user_id=12345,
            entity_type="person",
            name="María",
        )
        entity2 = entities.create_entity(
            user_id=12345,
            entity_type="person",
            name="María",
        )
        assert entity1["id"] == entity2["id"]

    def test_rejects_invalid_type(self, tmp_path):
        """Returns None for invalid entity type."""
        entity = entities.create_entity(
            user_id=12345,
            entity_type="invalid",
            name="Test",
        )
        assert entity is None

    def test_isolated_per_user(self, tmp_path):
        """Entities are isolated per user."""
        entities.create_entity(111, "person", "María")
        entities.create_entity(222, "person", "Juan")

        user1_entities = entities.list_entities(111)
        user2_entities = entities.list_entities(222)

        assert len(user1_entities) == 1
        assert len(user2_entities) == 1
        assert user1_entities[0]["name"] == "María"
        assert user2_entities[0]["name"] == "Juan"


class TestUpdateEntity:
    """Tests for update_entity function."""

    def test_updates_name(self, tmp_path):
        """Can update entity name."""
        entity = entities.create_entity(12345, "person", "María")
        updated = entities.update_entity(entity["id"], 12345, name="María García")
        assert updated["name"] == "María García"

    def test_updates_alias(self, tmp_path):
        """Can update entity alias."""
        entity = entities.create_entity(12345, "person", "María")
        updated = entities.update_entity(entity["id"], 12345, alias="Mari")
        assert updated["alias"] == "Mari"

    def test_updates_metadata(self, tmp_path):
        """Can update entity metadata."""
        entity = entities.create_entity(12345, "person", "María")
        updated = entities.update_entity(
            entity["id"], 12345, metadata={"teléfono": "+58 412 1234567"}
        )
        assert updated["metadata"]["teléfono"] == "+58 412 1234567"

    def test_updates_status(self, tmp_path):
        """Can update entity status."""
        entity = entities.create_entity(12345, "case", "Cita médica")
        updated = entities.update_entity(entity["id"], 12345, status="archived")
        assert updated["status"] == "archived"

    def test_rejects_invalid_status(self, tmp_path):
        """Returns None for invalid status."""
        entity = entities.create_entity(12345, "case", "Cita médica")
        updated = entities.update_entity(entity["id"], 12345, status="invalid")
        assert updated is None

    def test_returns_none_for_nonexistent(self, tmp_path):
        """Returns None when entity doesn't exist."""
        updated = entities.update_entity(99999, 12345, name="Test")
        assert updated is None


class TestGetEntity:
    """Tests for get_entity function."""

    def test_returns_entity(self, tmp_path):
        """Returns entity by ID."""
        created = entities.create_entity(12345, "person", "María")
        fetched = entities.get_entity(created["id"], 12345)
        assert fetched is not None
        assert fetched["name"] == "María"

    def test_returns_none_for_nonexistent(self, tmp_path):
        """Returns None when entity doesn't exist."""
        fetched = entities.get_entity(99999, 12345)
        assert fetched is None

    def test_isolated_per_user(self, tmp_path):
        """Cannot access another user's entity."""
        entity = entities.create_entity(111, "person", "María")
        fetched = entities.get_entity(entity["id"], 222)
        assert fetched is None


class TestListEntities:
    """Tests for list_entities function."""

    def test_lists_all_entities(self, tmp_path):
        """Lists all active entities for a user."""
        entities.create_entity(12345, "person", "María")
        entities.create_entity(12345, "place", "Hospital")

        result = entities.list_entities(12345)
        assert len(result) == 2

    def test_filters_by_type(self, tmp_path):
        """Can filter entities by type."""
        entities.create_entity(12345, "person", "María")
        entities.create_entity(12345, "place", "Hospital")

        people = entities.list_entities(12345, entity_type="person")
        places = entities.list_entities(12345, entity_type="place")

        assert len(people) == 1
        assert len(places) == 1

    def test_excludes_archived(self, tmp_path):
        """Excludes archived entities by default."""
        entity = entities.create_entity(12345, "case", "Cita médica")
        entities.update_entity(entity["id"], 12345, status="archived")

        active = entities.list_entities(12345, status="active")
        archived = entities.list_entities(12345, status="archived")

        assert len(active) == 0
        assert len(archived) == 1


class TestSearchEntities:
    """Tests for search_entities function."""

    def test_finds_by_name(self, tmp_path):
        """Finds entities by name."""
        entities.create_entity(12345, "person", "María García")
        entities.create_entity(12345, "person", "Juan Pérez")

        results = entities.search_entities(12345, "María")
        assert len(results) == 1
        assert results[0]["name"] == "María García"

    def test_finds_by_alias(self, tmp_path):
        """Finds entities by alias."""
        entities.create_entity(12345, "person", "María García", alias="Mari")

        results = entities.search_entities(12345, "Mari")
        assert len(results) == 1

    def test_returns_empty_when_no_match(self, tmp_path):
        """Returns empty list when no match."""
        entities.create_entity(12345, "person", "María")

        results = entities.search_entities(12345, "Pedro")
        assert results == []


class TestArchiveEntity:
    """Tests for archive_entity function."""

    def test_archives_entity(self, tmp_path):
        """Can archive an entity."""
        entity = entities.create_entity(12345, "case", "Cita médica")
        archived = entities.archive_entity(entity["id"], 12345)
        assert archived["status"] == "archived"


class TestEntityNotes:
    """Tests for entity notes functions."""

    def test_add_note(self, tmp_path):
        """Can add a note to an entity."""
        entity = entities.create_entity(12345, "case", "Llevar documentos")
        note = entities.add_entity_note(entity["id"], 12345, "Me dijeron que lleve el viernes")
        assert note is not None
        assert note["content"] == "Me dijeron que lleve el viernes"

    def test_add_note_to_nonexistent_entity(self, tmp_path):
        """Cannot add note to nonexistent entity."""
        note = entities.add_entity_note(99999, 12345, "Test")
        assert note is None

    def test_add_empty_note(self, tmp_path):
        """Cannot add empty note."""
        entity = entities.create_entity(12345, "case", "Test")
        note = entities.add_entity_note(entity["id"], 12345, "  ")
        assert note is None

    def test_get_notes(self, tmp_path):
        """Can get notes for an entity."""
        entity = entities.create_entity(12345, "case", "Test")
        entities.add_entity_note(entity["id"], 12345, "Nota 1")
        entities.add_entity_note(entity["id"], 12345, "Nota 2")

        notes = entities.get_entity_notes(entity["id"], 12345)
        assert len(notes) == 2
        # Newest first
        assert notes[0]["content"] == "Nota 2"
        assert notes[1]["content"] == "Nota 1"

    def test_get_entity_with_notes(self, tmp_path):
        """Can get entity with its notes."""
        entity = entities.create_entity(12345, "case", "Test")
        entities.add_entity_note(entity["id"], 12345, "Nota 1")

        result = entities.get_entity_with_notes(entity["id"], 12345)
        assert result is not None
        assert result["name"] == "Test"
        assert len(result["notes"]) == 1
