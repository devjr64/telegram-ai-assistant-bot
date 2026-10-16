"""Entity tracking: people, places, cases, and items.

Provides structured storage for tracking real-world entities that the user
mentions in conversations. Unlike the free-text journal, entities are
queryable and maintain state over time (e.g., a case can be "active" or
"archived", a person can have associated notes).

Storage: SQLite database (entities.db) with two tables:
- entities: core entity data (type, name, alias, metadata, status)
- entity_notes: follow-up notes tied to specific entities
"""

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent))
DB_PATH = Path(_DATA_DIR) / "entities.db"

# Entity types
ENTITY_TYPES = ("person", "place", "case", "item")

# Entity statuses
ENTITY_STATUSES = ("active", "archived")


def _get_connection() -> sqlite3.Connection:
    """Get a connection to the entities database, creating tables if needed."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_db(conn)
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    """Create the entities schema if it does not already exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            entity_type TEXT NOT NULL,
            name TEXT NOT NULL,
            alias TEXT,
            metadata TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(user_id, entity_type, name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (entity_id) REFERENCES entities(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_user ON entities(user_id, entity_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_notes_entity ON entity_notes(entity_id)"
    )
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a database row to a dictionary."""
    d = dict(row)
    # Parse metadata JSON
    if d.get("metadata"):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except json.JSONDecodeError:
            d["metadata"] = {}
    else:
        d["metadata"] = {}
    return d


def create_entity(
    user_id: int,
    entity_type: str,
    name: str,
    alias: str | None = None,
    metadata: dict | None = None,
) -> dict | None:
    """Create a new entity. Returns the created entity or None on error.

    If an entity with the same (user_id, entity_type, name) exists,
    returns the existing entity instead of creating a duplicate.
    """
    if entity_type not in ENTITY_TYPES:
        logger.warning("Invalid entity type: %s", entity_type)
        return None

    now = time.time()
    metadata_json = json.dumps(metadata) if metadata else None

    with _get_connection() as conn:
        # Check if entity already exists
        existing = conn.execute(
            "SELECT * FROM entities WHERE user_id = ? AND entity_type = ? AND name = ?",
            (user_id, entity_type, name),
        ).fetchone()
        if existing:
            return _row_to_dict(existing)

        cursor = conn.execute(
            "INSERT INTO entities (user_id, entity_type, name, alias, metadata, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            (user_id, entity_type, name, alias, metadata_json, now, now),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM entities WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def update_entity(
    entity_id: int,
    user_id: int,
    name: str | None = None,
    alias: str | None = None,
    metadata: dict | None = None,
    status: str | None = None,
) -> dict | None:
    """Update an entity's fields. Returns the updated entity or None."""
    updates = []
    params = []

    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if alias is not None:
        updates.append("alias = ?")
        params.append(alias)
    if metadata is not None:
        updates.append("metadata = ?")
        params.append(json.dumps(metadata))
    if status is not None:
        if status not in ENTITY_STATUSES:
            logger.warning("Invalid status: %s", status)
            return None
        updates.append("status = ?")
        params.append(status)

    if not updates:
        return get_entity(entity_id, user_id)

    updates.append("updated_at = ?")
    params.append(time.time())
    params.extend([entity_id, user_id])

    with _get_connection() as conn:
        conn.execute(
            f"UPDATE entities SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
            params,
        )
        conn.commit()

    return get_entity(entity_id, user_id)


def get_entity(entity_id: int, user_id: int) -> dict | None:
    """Get an entity by ID. Returns None if not found."""
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM entities WHERE id = ? AND user_id = ?",
            (entity_id, user_id),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_entities(
    user_id: int,
    entity_type: str | None = None,
    status: str = "active",
) -> list[dict]:
    """List entities for a user, optionally filtered by type and status."""
    sql = "SELECT * FROM entities WHERE user_id = ? AND status = ?"
    params: list = [user_id, status]

    if entity_type:
        sql += " AND entity_type = ?"
        params.append(entity_type)

    sql += " ORDER BY updated_at DESC"

    with _get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    return [_row_to_dict(row) for row in rows]


def search_entities(user_id: int, query: str) -> list[dict]:
    """Search entities by name or alias (case-insensitive)."""
    pattern = f"%{query}%"
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM entities WHERE user_id = ? "
            "AND (name LIKE ? OR alias LIKE ?) "
            "ORDER BY updated_at DESC",
            (user_id, pattern, pattern),
        ).fetchall()

    return [_row_to_dict(row) for row in rows]


def archive_entity(entity_id: int, user_id: int) -> dict | None:
    """Archive an entity (soft delete). Returns the archived entity."""
    return update_entity(entity_id, user_id, status="archived")


def add_entity_note(entity_id: int, user_id: int, content: str) -> dict | None:
    """Add a follow-up note to an entity. Returns the note dict."""
    if not content.strip():
        return None

    # Verify entity exists and belongs to user
    entity = get_entity(entity_id, user_id)
    if not entity:
        return None

    now = time.time()
    with _get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO entity_notes (entity_id, user_id, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (entity_id, user_id, content.strip(), now),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM entity_notes WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row) if row else None


def get_entity_notes(
    entity_id: int,
    user_id: int,
    limit: int = 50,
) -> list[dict]:
    """Get notes for an entity, newest first."""
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM entity_notes "
            "WHERE entity_id = ? AND user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (entity_id, user_id, limit),
        ).fetchall()

    return [dict(row) for row in rows]


def get_entity_with_notes(entity_id: int, user_id: int) -> dict | None:
    """Get an entity with its recent notes."""
    entity = get_entity(entity_id, user_id)
    if not entity:
        return None

    entity["notes"] = get_entity_notes(entity_id, user_id, limit=10)
    return entity
