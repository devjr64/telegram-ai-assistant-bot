"""Project tracking: SQLite metadata + Markdown file storage.

Stores user projects and their notes in a dedicated database file
(projects.db) and mirrors the human-readable content as Markdown files
under ``data/projects/{slug}/``.

SQLite provides fast structured queries (list, search, status). The .md
files keep content human-readable and editable, and ``context.json``
gives the AI a compact summary per project.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent))
DB_PATH = Path(_DATA_DIR) / "projects.db"
PROJECTS_DIR = Path(_DATA_DIR) / "projects"

# Valid lifecycle states for a project, in natural progression order.
PROJECT_STATUSES = ("sin_iniciar", "en_proceso", "parado", "completado", "archivado")

# Valid priority levels. "media" is the default for new projects.
PROJECT_PRIORITIES = ("baja", "media", "alta", "urgente")
PROJECT_DEFAULT_PRIORITY = "media"


def _get_connection() -> sqlite3.Connection:
    """Get a connection to the projects database, ensuring the schema exists.

    Matches the history.py pattern: WAL journal mode and foreign keys
    enabled. Idempotent — safe to call on every operation.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_db(conn)
    return conn


def _ensure_priority_column(conn: sqlite3.Connection) -> None:
    """Add the priority column to legacy projects tables created before it existed.

    SQLite does not let us add a column inside CREATE TABLE for existing
    tables, so we use PRAGMA table_info to detect the missing column and
    ALTER TABLE to add it with the same default used by new tables.
    """
    columns = conn.execute("PRAGMA table_info(projects)").fetchall()
    if any(col["name"] == "priority" for col in columns):
        return
    conn.execute(
        "ALTER TABLE projects ADD COLUMN priority TEXT NOT NULL DEFAULT 'media'"
    )


def _ensure_status_columns(conn: sqlite3.Connection) -> None:
    """Add status tracking columns to legacy projects tables.

    Backward-compatible migration for tables created before ``stopped_reason``
    and ``status_changed_at`` were introduced. New tables include both columns
    in ``CREATE TABLE``; existing tables receive them via ``ALTER TABLE``.
    """
    columns = conn.execute("PRAGMA table_info(projects)").fetchall()
    names = {col["name"] for col in columns}

    if "stopped_reason" not in names:
        conn.execute("ALTER TABLE projects ADD COLUMN stopped_reason TEXT")

    if "status_changed_at" not in names:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN status_changed_at REAL NOT NULL DEFAULT 0"
        )
        conn.execute(
            "UPDATE projects SET status_changed_at = created_at"
            " WHERE status_changed_at = 0"
        )

    # Migrate legacy English status values to their Spanish equivalents.
    # Idempotent: rows already in Spanish are untouched (ELSE status).
    conn.execute(
        """
        UPDATE projects SET status = CASE status
            WHEN 'active' THEN 'sin_iniciar'
            WHEN 'in_progress' THEN 'en_proceso'
            WHEN 'on_hold' THEN 'parado'
            WHEN 'completed' THEN 'completado'
            WHEN 'archived' THEN 'archivado'
            ELSE status
        END
        """
    )


def _ensure_group_columns(conn: sqlite3.Connection) -> None:
    """Add group-related columns for group support.

    Adds ``group_id`` to projects (nullable) and ``private_chat_id`` to users
    if they don't exist yet. Also creates the group_members table.
    """
    # Add group_id to projects if missing
    project_columns = conn.execute("PRAGMA table_info(projects)").fetchall()
    if not any(col["name"] == "group_id" for col in project_columns):
        conn.execute("ALTER TABLE projects ADD COLUMN group_id INTEGER")

    # Add private_chat_id to users if missing
    user_columns = conn.execute("PRAGMA table_info(users)").fetchall()
    if not any(col["name"] == "private_chat_id" for col in user_columns):
        conn.execute("ALTER TABLE users ADD COLUMN private_chat_id INTEGER")


def _init_db(conn: sqlite3.Connection) -> None:
    """Create the projects schema if it does not already exist.

    Uses CREATE TABLE / INDEX IF NOT EXISTS, so this is idempotent.
    Commits the schema so subsequent statements see the tables.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'sin_iniciar',
            description TEXT DEFAULT '',
            priority TEXT NOT NULL DEFAULT 'media',
            created_at REAL NOT NULL,
            status_changed_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            stopped_reason TEXT,
            group_id INTEGER,
            UNIQUE (user_id, slug)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL
                REFERENCES projects(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL,
            summary TEXT NOT NULL,
            content_path TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL
                REFERENCES projects(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL,
            invited_by INTEGER NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE (project_id, user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            private_chat_id INTEGER,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            first_seen_at REAL NOT NULL,
            UNIQUE (group_id, user_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notes_project ON project_notes(project_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_members_project ON project_members(project_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_members_user ON project_members(user_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)"
    )
    _ensure_priority_column(conn)
    _ensure_status_columns(conn)
    _ensure_group_columns(conn)
    # Create group-related indexes after ensuring columns exist
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_projects_group ON projects(group_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_group_members_group ON group_members(group_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_group_members_user ON group_members(user_id)"
    )
    conn.commit()


def slugify(name: str) -> str:
    """Convert a project name into a URL/filesystem-safe slug."""
    value = name.lower().strip()
    value = re.sub(r"[^a-z0-9\s-]", "", value)
    value = re.sub(r"[\s_]+", "-", value)
    return value or "project"


# --- Project CRUD (SQLite) -------------------------------------------------


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a project row into a plain dict."""
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "slug": row["slug"],
        "name": row["name"],
        "status": row["status"],
        "description": row["description"],
        "priority": row["priority"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "status_changed_at": row["status_changed_at"],
        "stopped_reason": row["stopped_reason"],
        "group_id": row["group_id"],
    }


def create_project(
    user_id: int,
    name: str,
    description: str = "",
    priority: str = PROJECT_DEFAULT_PRIORITY,
    group_id: int | None = None,
) -> dict | None:
    """Create a project with an auto-generated unique slug.

    Returns the new project dict on success, ``None`` if the slug already
    exists, or an error dict ``{"ok": False, "error": ..., "existing": ...}``
    when trying to create an urgent project while the user already has 3
    urgent active projects.

    When ``group_id`` is provided, the project is associated with that Telegram
    group and will be visible to all group members.
    """
    if priority not in PROJECT_PRIORITIES:
        return {"ok": False, "error": f"Prioridad inválida: {priority}"}

    slug = slugify(name)
    now = time.time()

    if priority == "urgente":
        can_urgent, existing = can_mark_urgent(user_id)
        if not can_urgent:
            return {
                "ok": False,
                "error": "Ya tienes 3 proyectos urgentes activos. Baja la prioridad de uno antes de crear otro.",
                "existing": existing,
            }

    with _get_connection() as conn:
        existing_slug = conn.execute(
            "SELECT id FROM projects WHERE user_id = ? AND slug = ?",
            (user_id, slug),
        ).fetchone()
        if existing_slug:
            return None

        cur = conn.execute(
            "INSERT INTO projects (user_id, slug, name, status, description, priority,"
            " created_at, updated_at, status_changed_at, group_id)"
            " VALUES (?, ?, ?, 'sin_iniciar', ?, ?, ?, ?, ?, ?)",
            (user_id, slug, name, description, priority, now, now, now, group_id),
        )
        conn.commit()

    _create_project_dir(user_id, slug, name, description, priority, now)
    return get_project_by_slug(user_id, slug)


def update_project_status(
    project_id: int, new_status: str, stopped_reason: str | None = None
) -> bool:
    """Update a project's status and status change timestamp.

    ``stopped_reason`` is only stored when the new status is ``parado``.
    Changing to any other status clears a previously stored reason.
    A no-op status change keeps the previous ``status_changed_at``.

    Refuses to reactivate an urgent project (move it to ``sin_iniciar`` or
    ``en_proceso``) when the user already has 3 urgent active projects.
    Returns True on success.
    """
    if new_status not in PROJECT_STATUSES:
        return False

    now = time.time()
    reason = stopped_reason if new_status == "parado" else None

    with _get_connection() as conn:
        row = conn.execute(
            "SELECT user_id, slug, status, priority, status_changed_at"
            " FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if not row:
            return False

        # Reactivating an urgent project would add a 4th urgent active project.
        if (
            new_status in ACTIVE_STATUSES
            and row["status"] not in ACTIVE_STATUSES
            and row["priority"] == "urgente"
        ):
            can, _ = can_activate_urgent(row["user_id"], project_id)
            if not can:
                return False

        status_changed_at = now if new_status != row["status"] else row["status_changed_at"]
        cur = conn.execute(
            "UPDATE projects SET status = ?, updated_at = ?, status_changed_at = ?,"
            " stopped_reason = ? WHERE id = ?",
            (new_status, now, status_changed_at, reason, project_id),
        )
        conn.commit()

    updated = get_project_by_slug(row["user_id"], row["slug"])
    if updated:
        _write_context_json(row["user_id"], row["slug"], updated)
    return cur.rowcount > 0


def update_project_description(project_id: int, description: str) -> bool:
    """Update a project's description. Returns True on success."""
    with _get_connection() as conn:
        cur = conn.execute(
            "UPDATE projects SET description = ?, updated_at = ? WHERE id = ?",
            (description, time.time(), project_id),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_project(project_id: int, actor_id: int) -> dict:
    """Delete an archived project, returning ``{"ok": ...}``.

    Only the owner (``actor_id == projects.user_id``) may delete, and only
    when the project is archived — active projects must be archived first so
    a project in use cannot be destroyed accidentally. Ownership and status
    are enforced here at the data layer as defense in depth (mirroring
    ``remove_project_member``), so no caller can delete another user's
    project even if a UI-layer check is bypassed. Deleting the row cascades
    to ``project_notes`` and ``project_members`` via FK ON DELETE CASCADE;
    the Markdown directory is removed afterwards via ``_remove_project_dir``.
    """
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT user_id, slug, status FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "Proyecto no encontrado"}
        if row["user_id"] != actor_id:
            return {
                "ok": False,
                "error": "Solo el dueño del proyecto puede eliminarlo",
            }
        if row["status"] != "archivado":
            return {
                "ok": False,
                "error": "Solo puedes eliminar un proyecto archivado. Archívalo primero con /estadoproyecto <nombre> archivado.",
            }
        # Capture before the row is gone: file cleanup needs owner + slug.
        user_id = row["user_id"]
        slug = row["slug"]
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()

    if cur.rowcount == 0:
        return {"ok": False, "error": "Proyecto no encontrado"}

    _remove_project_dir(user_id, slug)
    logger.info(
        "Deleted archived project %r (id=%d, user=%d)", slug, project_id, user_id
    )
    return {"ok": True}


# --- Priority helpers ------------------------------------------------------


ACTIVE_STATUSES = ("sin_iniciar", "en_proceso")


def _list_urgent_active_projects(
    user_id: int, exclude_id: int | None = None
) -> list[dict]:
    """Return a user's urgent active projects, optionally excluding one."""
    sql = (
        "SELECT * FROM projects WHERE user_id = ? AND priority = ?"
        " AND status IN (?, ?)"
    )
    params = [user_id, "urgente", *ACTIVE_STATUSES]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    sql += " ORDER BY updated_at DESC"
    with _get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_urgent_active_projects(user_id: int) -> int:
    """Count how many active projects are marked as urgent for a user."""
    return len(_list_urgent_active_projects(user_id))


def can_mark_urgent(user_id: int) -> tuple[bool, list[dict]]:
    """Check whether the user can mark another project as urgent.

    Returns ``(True, [])`` when under the limit of 3 urgent active projects,
    or ``(False, list_of_existing_urgent_projects)`` when the limit is reached.
    """
    existing = _list_urgent_active_projects(user_id)
    return len(existing) < 3, existing


def can_activate_urgent(
    user_id: int, project_id: int
) -> tuple[bool, list[dict]]:
    """Check whether an urgent project may be moved to an active status.

    Reactivating an urgent project (from ``parado``/``completado``/``archivado``
    to ``sin_iniciar``/``en_proceso``) would add it to the urgent-active set.
    Returns ``(True, [])`` when the limit of 3 is not exceeded, or
    ``(False, existing_urgent_projects)`` when it would be.
    """
    existing = _list_urgent_active_projects(user_id, exclude_id=project_id)
    return len(existing) < 3, existing


def update_project_priority(project_id: int, priority: str) -> dict:
    """Update a project's priority.

    Returns ``{"ok": True, "project": ...}`` on success, or an error dict
    with the current urgent projects when the limit of 3 urgent active
    projects would be exceeded.
    """
    if priority not in PROJECT_PRIORITIES:
        return {"ok": False, "error": f"Prioridad inválida: {priority}"}

    with _get_connection() as conn:
        row = conn.execute(
            "SELECT user_id, slug FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    if not row:
        return {"ok": False, "error": "Proyecto no encontrado"}

    user_id = row["user_id"]
    slug = row["slug"]

    if priority == "urgente":
        existing = _list_urgent_active_projects(user_id, exclude_id=project_id)
        if len(existing) >= 3:
            return {
                "ok": False,
                "error": "Ya tienes 3 proyectos urgentes activos. Baja la prioridad de uno antes de continuar.",
                "existing": existing,
            }

    with _get_connection() as conn:
        conn.execute(
            "UPDATE projects SET priority = ?, updated_at = ? WHERE id = ?",
            (priority, time.time(), project_id),
        )
        conn.commit()

    updated = get_project_by_slug(user_id, slug)
    if updated:
        _write_context_json(user_id, slug, updated)
    return {"ok": True, "project": updated}


def add_project_note(
    project_id: int, user_id: int, summary: str, content: str = ""
) -> dict | None:
    """Add a note to a project, writing content to a .md file.

    Returns the note dict, or None if the project does not exist.
    """
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT slug, user_id FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not row:
            return None
        slug = row["slug"]
        owner_id = row["user_id"]

        cur = conn.execute(
            "INSERT INTO project_notes (project_id, user_id, summary, content_path, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (project_id, user_id, summary, "", time.time()),
        )
        conn.commit()
        note_id = cur.lastrowid

    path = _write_note_md(owner_id, slug, note_id, summary, content)
    with _get_connection() as conn:
        conn.execute(
            "UPDATE project_notes SET content_path = ? WHERE id = ?",
            (str(path), note_id),
        )
        conn.commit()

    return {
        "id": note_id,
        "project_id": project_id,
        "user_id": user_id,
        "summary": summary,
        "content_path": str(path),
    }


def list_projects(user_id: int, status_filter: str | None = None) -> list[dict]:
    """List a user's projects, optionally filtered by status."""
    with _get_connection() as conn:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM projects WHERE user_id = ? AND status = ?"
                " ORDER BY updated_at DESC",
                (user_id, status_filter),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM projects WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def search_projects(
    user_id: int, query: str, group_id: int | None = None
) -> list[dict]:
    """Search projects the user can access by name or description (LIKE).

    A collaborator can find projects shared with them too, so the search
    covers both owned projects and membership rows. The JOIN can produce
    one row per project membership; GROUP BY deduplicates, and DISTINCT
    applies before ORDER BY on projects.id.

    When ``group_id`` is provided, only projects in that group are searched.
    When ``group_id`` is None, only private projects are searched.
    """
    pattern = f"%{query}%"
    with _get_connection() as conn:
        if group_id is not None:
            rows = conn.execute(
                "SELECT DISTINCT p.* FROM projects p"
                " LEFT JOIN project_members m ON m.project_id = p.id"
                " WHERE (p.user_id = ? OR m.user_id = ?)"
                " AND p.group_id = ?"
                " AND (p.name LIKE ? OR p.description LIKE ?)"
                " ORDER BY p.updated_at DESC",
                (user_id, user_id, group_id, pattern, pattern),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT p.* FROM projects p"
                " LEFT JOIN project_members m ON m.project_id = p.id"
                " WHERE (p.user_id = ? OR m.user_id = ?)"
                " AND p.group_id IS NULL"
                " AND (p.name LIKE ? OR p.description LIKE ?)"
                " ORDER BY p.updated_at DESC",
                (user_id, user_id, pattern, pattern),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_project_by_slug(user_id: int, slug: str) -> dict | None:
    """Fetch a project by slug, scoped to the user."""
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE user_id = ? AND slug = ?",
            (user_id, slug),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_project_notes(project_id: int) -> list[dict]:
    """Return a project's notes, oldest first."""
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM project_notes WHERE project_id = ? ORDER BY created_at ASC",
            (project_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "project_id": r["project_id"],
            "user_id": r["user_id"],
            "summary": r["summary"],
            "content_path": r["content_path"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# --- Project sharing: user resolution -------------------------------------


def _normalize_username(username: str) -> str:
    """Canonicalize a Telegram username for storage and lookup.

    Telegram usernames are case-insensitive and are usually written with
    a leading '@' when shared between people. Storing a canonical form
    (trimmed, lowercased, without '@') keeps upsert_user() and
    resolve_username() consistent, so a search for "@MiUsuario" finds the
    same row as "miusuario".
    """
    value = username.strip().lower()
    if value.startswith("@"):
        value = value[1:]
    return value


def upsert_user(
    user_id: int, username: str | None, first_name: str | None,
    private_chat_id: int | None = None,
) -> None:
    """Record or refresh a Telegram user's profile in the users table.

    Called on every update from Telegram (message, voice, photo) so the
    table always has fresh data to resolve @username mentions during
    project sharing. The ON CONFLICT upsert keeps one row per user_id;
    a None username or first_name (users can remove their username)
    simply overwrites the previous value.

    When ``private_chat_id`` is provided (from private chats), it is stored
    so reminders can be delivered to the user's private chat even when
    requests come from groups.
    """
    normalized = _normalize_username(username) if username else None
    now = time.time()
    with _get_connection() as conn:
        # Check if user exists
        existing = conn.execute(
            "SELECT user_id, private_chat_id FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if existing:
            # Update existing user
            update_fields = {
                "username": normalized,
                "first_name": first_name,
                "updated_at": now,
            }
            # Only update private_chat_id if provided and not None
            if private_chat_id is not None:
                update_fields["private_chat_id"] = private_chat_id

            set_clause = ", ".join(f"{k} = ?" for k in update_fields)
            values = list(update_fields.values()) + [user_id]
            conn.execute(
                f"UPDATE users SET {set_clause} WHERE user_id = ?",
                values,
            )
        else:
            # Insert new user
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, private_chat_id, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, normalized, first_name, private_chat_id, now),
            )
        conn.commit()


def resolve_username(username: str) -> int | None:
    """Resolve a Telegram @username to a user_id, or None if unknown.

    Used when a project owner invites a collaborator: the owner types
    @usuario and we look it up in the users table populated by
    upsert_user(). COLLATE NOCASE makes the comparison case-insensitive
    and _normalize_username() strips whitespace and a leading '@', so any
    spelling of the username matches.

    The lookup is deterministic: if two rows ever share a username (a
    user renamed and a new user took the old handle before the stale row
    was refreshed), the most recently seen profile wins, so an invitation
    can never be routed to an arbitrary/unknown account.
    """
    normalized = _normalize_username(username)
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM users WHERE username = ? COLLATE NOCASE"
            " ORDER BY updated_at DESC LIMIT 1",
            (normalized,),
        ).fetchone()
    return row["user_id"] if row else None


def get_user_profile(user_id: int) -> dict | None:
    """Return a user's recorded profile (username, first_name) or None.

    Used to render a friendly owner label (@username or first name)
    instead of exposing the raw Telegram ID to collaborators.
    """
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT username, first_name FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return {"username": row["username"], "first_name": row["first_name"]}


# --- Project sharing: membership & authorization (ACL) -----------------------


def get_project_for_user(
    user_id: int, slug: str, group_id: int | None = None
) -> dict | None:
    """Return a project only when the user is its owner or an invited member.

    ACL gate for every project read. Ownership lives in ``projects.user_id``
    and invitations in ``project_members``, so a collaborator sees a project
    only if a membership row exists for them — there is no ID-based fallback
    that could leak another user's project.

    When ``group_id`` is provided, only projects associated with that group
    are returned (group context). When ``group_id`` is ``None``, only private
    projects (group_id IS NULL) are returned (private context). This ensures
    complete scope isolation between private and group chats.

    Mirrors ``_find_project``: tries the literal slug first, then the
    slugified form (``lower()`` + spaces to hyphens), so callers can pass
    either the raw name or the canonical slug.
    """
    candidates = (slug, slug.lower().replace(" ", "-"))
    with _get_connection() as conn:
        for candidate in candidates:
            # Owner lookup — scoped by context
            if group_id is not None:
                row = conn.execute(
                    "SELECT * FROM projects"
                    " WHERE user_id = ? AND slug = ? AND group_id = ?",
                    (user_id, candidate, group_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM projects"
                    " WHERE user_id = ? AND slug = ? AND group_id IS NULL",
                    (user_id, candidate),
                ).fetchone()
            if row:
                return _row_to_dict(row)

            # Member lookup — scoped by context
            if group_id is not None:
                row = conn.execute(
                    "SELECT p.* FROM projects p"
                    " JOIN project_members m ON m.project_id = p.id"
                    " WHERE m.user_id = ? AND p.slug = ? AND p.group_id = ?"
                    " ORDER BY p.updated_at DESC LIMIT 1",
                    (user_id, candidate, group_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT p.* FROM projects p"
                    " JOIN project_members m ON m.project_id = p.id"
                    " WHERE m.user_id = ? AND p.slug = ? AND p.group_id IS NULL"
                    " ORDER BY p.updated_at DESC LIMIT 1",
                    (user_id, candidate),
                ).fetchone()
            if row:
                return _row_to_dict(row)
    return None


def is_owner(user_id: int, project_id: int) -> bool:
    """True when the user is the project's owner (projects.user_id matches)."""
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user_id),
        ).fetchone()
    return row is not None


def is_project_member(user_id: int, project_id: int) -> bool:
    """True when the user has an invitation row for the project.

    Membership is the only way a non-owner gains access; checking it
    separately lets callers distinguish "not found" from "no permission".
    """
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM project_members WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        ).fetchone()
    return row is not None


def add_project_member(project_id: int, user_id: int, invited_by: int) -> dict:
    """Invite a collaborator to a project, returning ``{"ok": ...}``.

    The data layer enforces ownership: ``invited_by`` must be the
    project's owner (``projects.user_id``), otherwise a bug in any caller
    could let a collaborator add other users to a project they do not
    own. Self-invitation is rejected here too, so no caller can grant a
    user access to their own project or forge an invitation. The
    UNIQUE(project_id, user_id) constraint is pre-checked to return a
    friendly Spanish error instead of surfacing a raw IntegrityError.
    """
    if user_id == invited_by:
        return {
            "ok": False,
            "error": "No puedes compartir un proyecto contigo mismo",
        }
    now = time.time()
    with _get_connection() as conn:
        project = conn.execute(
            "SELECT user_id FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not project:
            return {"ok": False, "error": "Proyecto no encontrado"}
        if invited_by != project["user_id"]:
            return {
                "ok": False,
                "error": "Solo el dueño del proyecto puede compartirlo",
            }
        existing = conn.execute(
            "SELECT 1 FROM project_members WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        ).fetchone()
        if existing:
            return {
                "ok": False,
                "error": "El usuario ya es miembro de este proyecto",
            }
        cur = conn.execute(
            "INSERT INTO project_members (project_id, user_id, invited_by, created_at)"
            " VALUES (?, ?, ?, ?)",
            (project_id, user_id, invited_by, now),
        )
        member_id = cur.lastrowid
        conn.commit()
    return {
        "ok": True,
        "member": {
            "id": member_id,
            "project_id": project_id,
            "user_id": user_id,
            "invited_by": invited_by,
            "created_at": now,
        },
    }


def remove_project_member(project_id: int, user_id: int, actor_id: int) -> dict:
    """Remove a collaborator's access, returning ``{"ok": ...}``.

    Only the project owner (``actor_id == projects.user_id``) may revoke
    access — enforced here at the data layer as defense in depth, so no
    caller can accidentally let a collaborator remove other members.
    Deletes only from ``project_members``; the project row itself is
    never touched, so revoking access can never destroy the project.
    """
    with _get_connection() as conn:
        project = conn.execute(
            "SELECT user_id FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not project:
            return {"ok": False, "error": "Proyecto no encontrado"}
        if actor_id != project["user_id"]:
            return {
                "ok": False,
                "error": "Solo el dueño del proyecto puede quitar acceso",
            }
        cur = conn.execute(
            "DELETE FROM project_members WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        )
        conn.commit()
    if cur.rowcount == 0:
        return {"ok": False, "error": "Ese usuario no tiene acceso a este proyecto"}
    return {"ok": True}


def list_project_members(project_id: int) -> list[dict]:
    """List a project's collaborators with their profile info, oldest first.

    LEFT JOIN keeps the row even when the invitee has no profile in the
    users table yet (they may never have messaged the bot); username and
    first_name are then None.
    """
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT m.user_id, u.username, u.first_name, m.invited_by, m.created_at"
            " FROM project_members m"
            " LEFT JOIN users u ON u.user_id = m.user_id"
            " WHERE m.project_id = ?"
            " ORDER BY m.created_at ASC",
            (project_id,),
        ).fetchall()
    return [
        {
            "user_id": r["user_id"],
            "username": r["username"],
            "first_name": r["first_name"],
            "invited_by": r["invited_by"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def get_member_count(project_id: int) -> int:
    """Count a project's collaborators (excludes the owner by design)."""
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM project_members WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    return int(row["n"])


def list_owned_projects(
    user_id: int, status_filter: str | None = None
) -> list[dict]:
    """List the projects a user owns, optionally filtered by status.

    The ownership scope (``projects.user_id``) — distinct from
    ``list_shared_with_me`` (projects where the user is a member) and
    ``list_shared_by_me`` (owned projects shared with others).
    """
    with _get_connection() as conn:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM projects WHERE user_id = ? AND status = ?"
                " ORDER BY updated_at DESC",
                (user_id, status_filter),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM projects WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_shared_with_me(user_id: int) -> list[dict]:
    """List projects shared with the user (projects where they are a member).

    Each dict carries ``owner_id`` (``projects.user_id`` is the owner), so
    the UI can display who shared the project.
    """
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT p.* FROM projects p"
            " JOIN project_members m ON m.project_id = p.id"
            " WHERE m.user_id = ?"
            " ORDER BY p.updated_at DESC",
            (user_id,),
        ).fetchall()
    return [{**_row_to_dict(r), "owner_id": r["user_id"]} for r in rows]


def list_shared_by_me(user_id: int) -> list[dict]:
    """List owned projects that have at least one collaborator.

    GROUP BY deduplicates projects with several members. ``member_count``
    is added per project so the UI can show the number of people with
    access without a separate query per row (N+1 is fine at this scale).
    """
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT p.* FROM projects p"
            " JOIN project_members m ON m.project_id = p.id"
            " WHERE p.user_id = ?"
            " GROUP BY p.id"
            " ORDER BY p.updated_at DESC",
            (user_id,),
        ).fetchall()
    return [
        {**_row_to_dict(r), "member_count": get_member_count(r["id"])}
        for r in rows
    ]


# --- Markdown file storage ---------------------------------------------------


def _project_dir(user_id: int, slug: str) -> Path:
    """Return the directory for a user's project Markdown files.

    The user_id segment isolates files on disk, so two users can create a
    project with the same name (same slug) without writing to the same
    directory — mirroring the user_id scoping already enforced in SQLite.
    """
    return PROJECTS_DIR / str(user_id) / slug


def _remove_project_dir(user_id: int, slug: str) -> None:
    """Remove a project's Markdown directory from disk.

    Deleting the database row alone would orphan the overview.md/notes
    files; this helper removes the whole per-user project folder. The
    resolved path is guarded against PROJECTS_DIR so a corrupted slug or
    user_id can never escape the data directory (path traversal defense).
    A missing directory is not an error — e.g. legacy projects created
    before Markdown storage — so removal silently no-ops.
    """
    directory = _project_dir(user_id, slug)
    projects_root = PROJECTS_DIR.resolve()
    if not directory.resolve().is_relative_to(projects_root):
        logger.warning(
            "Refusing to remove path outside PROJECTS_DIR: %s", directory
        )
        return
    shutil.rmtree(directory, ignore_errors=True)


def _create_project_dir(
    user_id: int, slug: str, name: str, description: str, priority: str, created_at: float
) -> None:
    """Create the project folder with overview.md, notes/, and context.json."""
    directory = _project_dir(user_id, slug)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "notes").mkdir(exist_ok=True)

    overview = f"# {name}\n\n{description}\n" if description else f"# {name}\n"
    (directory / "overview.md").write_text(overview, encoding="utf-8")

    _write_context_json(user_id, slug, {
        "slug": slug,
        "name": name,
        "status": "sin_iniciar",
        "priority": priority,
        "created_at": created_at,
        "status_changed_at": created_at,
    })


def _write_context_json(user_id: int, slug: str, metadata: dict) -> None:
    """Write the AI-friendly compact context file for a project."""
    context = {
        "slug": metadata.get("slug", slug),
        "name": metadata.get("name", slug),
        "status": metadata.get("status", "sin_iniciar"),
        "priority": metadata.get("priority", PROJECT_DEFAULT_PRIORITY),
        "created_at": metadata.get("created_at"),
        "status_changed_at": metadata.get("status_changed_at"),
    }
    stopped_reason = metadata.get("stopped_reason")
    if stopped_reason:
        context["stopped_reason"] = stopped_reason
    path = _project_dir(user_id, slug) / "context.json"
    path.write_text(json.dumps(context, indent=2), encoding="utf-8")


def write_overview(user_id: int, slug: str, content: str) -> None:
    """Overwrite the project's overview.md with the given content."""
    path = _project_dir(user_id, slug) / "overview.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_note_md(
    user_id: int, slug: str, note_id: int, summary: str, content: str
) -> Path:
    """Write a note's Markdown file and return its path."""
    notes_dir = _project_dir(user_id, slug) / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    body = content if content else summary
    path = notes_dir / f"{note_id}.md"
    path.write_text(body, encoding="utf-8")
    return path


def read_project_files(user_id: int, slug: str) -> dict:
    """Read all Markdown content for a user's project as a dict of file -> text."""
    directory = _project_dir(user_id, slug)
    result: dict[str, str] = {}
    for path in sorted(directory.rglob("*.md")):
        result[str(path.relative_to(directory))] = path.read_text(
            encoding="utf-8"
        )
    return result


# --- Group support functions -----------------------------------------------


def add_group_member(group_id: int, user_id: int) -> None:
    """Record a user as a member of a Telegram group (UPSERT).

    Called on every update from a group so the table tracks which users
    have been seen in each group. Used when creating group projects to
    auto-add all members as collaborators.

    When a new user is added, they are also added as a collaborator to
    all existing projects in that group.
    """
    now = time.time()
    with _get_connection() as conn:
        # Check if this is a new member
        existing = conn.execute(
            "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone()
        
        conn.execute(
            """
            INSERT INTO group_members (group_id, user_id, first_seen_at)
            VALUES (?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                first_seen_at = group_members.first_seen_at
            """,
            (group_id, user_id, now),
        )
        conn.commit()
        
        # If new member, add them to all existing group projects
        if existing is None:
            _add_user_to_group_projects(conn, group_id, user_id, now)


def get_group_members(group_id: int) -> list[int]:
    """Return all user_ids that have been seen in a group."""
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT user_id FROM group_members WHERE group_id = ?",
            (group_id,),
        ).fetchall()
    return [row["user_id"] for row in rows]


def _add_user_to_group_projects(
    conn: sqlite3.Connection, group_id: int, user_id: int, now: float
) -> None:
    """Add a user as a collaborator to all projects in a group.

    Called when a new user joins a group so they can see existing projects
    in their private chat.
    """
    # Get all projects in this group
    projects = conn.execute(
        "SELECT id, user_id as owner_id FROM projects WHERE group_id = ?",
        (group_id,),
    ).fetchall()
    
    for project in projects:
        # Skip if user is the owner
        if project["owner_id"] == user_id:
            continue
        
        # Check if already a member
        existing = conn.execute(
            "SELECT 1 FROM project_members WHERE project_id = ? AND user_id = ?",
            (project["id"], user_id),
        ).fetchone()
        
        if existing is None:
            conn.execute(
                "INSERT INTO project_members (project_id, user_id, invited_by, created_at)"
                " VALUES (?, ?, ?, ?)",
                (project["id"], user_id, project["owner_id"], now),
            )
    
    conn.commit()


def list_group_projects_for_user(user_id: int) -> list[dict]:
    """Return all group projects where the user is a member or owner.
    
    Used to show group projects in the user's private chat.
    """
    with _get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT p.* FROM projects p
            WHERE p.group_id IS NOT NULL
            AND (
                p.user_id = ?
                OR p.id IN (
                    SELECT pm.project_id FROM project_members pm
                    WHERE pm.user_id = ?
                )
            )
            ORDER BY p.updated_at DESC
            """,
            (user_id, user_id),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_shared_projects_in_group(group_id: int) -> list[dict]:
    """Return projects that are shared among members of a group.

    A project is considered "shared in group" when:
    1. It has group_id matching the given group_id, AND
    2. At least one of its members (owner or invited) is also a member of the group

    This ensures only projects relevant to the group are shown.
    """
    with _get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT p.* FROM projects p
            WHERE p.group_id = ?
            AND (
                p.user_id IN (SELECT user_id FROM group_members WHERE group_id = ?)
                OR p.id IN (
                    SELECT pm.project_id FROM project_members pm
                    WHERE pm.user_id IN (SELECT user_id FROM group_members WHERE group_id = ?)
                )
            )
            ORDER BY p.updated_at DESC
            """,
            (group_id, group_id, group_id),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_private_chat_id(user_id: int) -> int | None:
    """Return the user's private chat ID, or None if never chatted privately.

    Used when creating reminders from group context to ensure reminders
    are delivered to the user's private chat, not the group.
    """
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT private_chat_id FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row["private_chat_id"] if row else None


def get_group_project_notify_targets(
    project_id: int, exclude_user_id: int | None = None
) -> list[int]:
    """Return user_ids who should be notified of a group project change.

    Returns all group members who are also project members (or the owner),
    excluding the actor who made the change (they already see the result).
    Only relevant for group projects (projects with group_id set).
    """
    with _get_connection() as conn:
        project = conn.execute(
            "SELECT group_id, user_id FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if not project or project["group_id"] is None:
            return []

        group_id = project["group_id"]

        # Get all group members
        group_members = conn.execute(
            "SELECT user_id FROM group_members WHERE group_id = ?",
            (group_id,),
        ).fetchall()
        member_ids = {row["user_id"] for row in group_members}

        # Also include the owner if they're in the group
        owner_id = project["user_id"]
        member_ids.add(owner_id)

        notify_targets = list(member_ids)

        # Exclude the actor
        if exclude_user_id is not None:
            notify_targets = [uid for uid in notify_targets if uid != exclude_user_id]

        return notify_targets


def get_project_owner_name(user_id: int) -> str:
    """Return a display name for the project owner (for notifications).

    Prefers @username, falls back to first name, then a generic label.
    """
    profile = get_user_profile(user_id)
    if profile and profile.get("username"):
        return f"@{profile['username']}"
    if profile and profile.get("first_name"):
        return profile["first_name"]
    return "Alguien"


def create_group_project(
    owner_id: int,
    group_id: int,
    name: str,
    description: str = "",
    priority: str = PROJECT_DEFAULT_PRIORITY,
) -> dict | None:
    """Create a project in a group context and auto-add all group members.

    The project gets group_id set, and all members of the group are
    automatically added as collaborators (except the owner).
    """
    # Create the project with group_id
    project = create_project(owner_id, name, description, priority, group_id=group_id)
    if project is None or (isinstance(project, dict) and not project.get("ok", True)):
        return project

    # Auto-add all group members as collaborators
    members = get_group_members(group_id)
    for member_id in members:
        if member_id != owner_id:
            add_project_member(project["id"], member_id, owner_id)

    return project


def migrate_group_projects_to_private(group_id: int) -> None:
    """Legacy: convert group projects to private (kept for back-compat)."""
    # Retained as alias for old call sites; delegates to cleanup_group_data with preserve flag
    cleanup_group_data(group_id, delete_projects=False)


def cleanup_group_data(group_id: int, delete_projects: bool = True) -> None:
    """Borrado total de datos de un grupo (spec 7.5).

    Elimina reminders, proyectos, días/summaries, chat_history y dirs de
    memoria. Es irreversible y best-effort.
    """
    # Projects DB: reminders, projects, members, group_members
    with _get_connection() as conn:
        try:
            # reminders
            conn.execute("DELETE FROM reminders WHERE group_id = ?", (group_id,))
        except Exception:
            pass  # column may not exist yet in tests without migration — ignore
        # projects
        projects = conn.execute(
            "SELECT id, user_id, slug FROM projects WHERE group_id = ?",
            (group_id,),
        ).fetchall()
        for proj in projects:
            if delete_projects:
                conn.execute("DELETE FROM projects WHERE id = ?", (proj["id"],))
                conn.execute("DELETE FROM project_members WHERE project_id = ?", (proj["id"],))
                _remove_project_dir(proj["user_id"], proj["slug"])
            else:
                # legacy migrate mode
                conn.execute("UPDATE projects SET group_id = NULL WHERE id = ?", (proj["id"],))
                conn.execute("DELETE FROM project_members WHERE project_id = ?", (proj["id"],))
                _remove_project_dir(proj["user_id"], proj["slug"])
        conn.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
        conn.commit()
        if projects:
            logger.info(
                "%s %d group projects (group_id=%d)",
                "Deleted" if delete_projects else "Migrated",
                len(projects),
                group_id,
            )
    # Memory DB: days, summaries
    try:
        from bot.memory import DB_PATH as MEMORY_DB_PATH, MEMORY_DIR
        import sqlite3 as _sqlite3
        import shutil as _shutil

        mconn = _sqlite3.connect(str(MEMORY_DB_PATH))
        mconn.execute("DELETE FROM days WHERE group_id = ?", (group_id,))
        mconn.execute("DELETE FROM summaries WHERE group_id = ?", (group_id,))
        mconn.commit()
        mconn.close()
        _shutil.rmtree(MEMORY_DIR / f"group_{group_id}", ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 best-effort
        logger.warning("cleanup_group_data memory failed for group %d: %s", group_id, exc)
    # History DB: chat_history
    try:
        from bot.history import _get_connection as _get_history_conn

        with _get_history_conn() as hconn:
            hconn.execute("DELETE FROM chat_history WHERE chat_id = ?", (group_id,))
            hconn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("cleanup_group_data history failed for group %d: %s", group_id, exc)

