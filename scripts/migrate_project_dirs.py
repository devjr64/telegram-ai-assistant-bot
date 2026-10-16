"""One-time migration: namespace project Markdown dirs by user.

Historically the bot stored project files at ``data/projects/{slug}/``,
shared across all users. Two users could create a project with the same
name (same slug) and collide on disk. Storage is now namespaced per user
at ``data/projects/{user_id}/{slug}/``.

This script moves every existing project directory into its owner's
namespace (using ``user_id`` from ``projects.db``) and rewrites the
stored ``project_notes.content_path`` values accordingly.

Idempotent and safe to re-run: an old path that no longer exists is
skipped, and a destination that already exists is never overwritten.

Run inside the container against the live volume:

    docker compose exec telegram-bot python scripts/migrate_project_dirs.py

(Requires an image built after ``COPY scripts/ scripts/`` was added to
the Dockerfile; run it before deploying the user-namespaced code.)
"""

import logging
import os
import shutil
import sqlite3
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("migrate_project_dirs")

_DATA_DIR = os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent))
DB_PATH = Path(_DATA_DIR) / "projects.db"
OLD_PROJECTS_DIR = Path(_DATA_DIR) / "projects"


def _get_connection() -> sqlite3.Connection:
    """Open the projects DB with row access, without touching the schema."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _projects_with_notes(conn: sqlite3.Connection) -> list[dict]:
    """Return every project plus its note content_paths."""
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT p.id, p.user_id, p.slug,
                   COALESCE(n.content_path, '') AS content_path
            FROM projects p
            LEFT JOIN project_notes n ON n.project_id = p.id
            """
        )
    ]


def _migrate_dir(old: Path, new: Path) -> bool:
    """Move one project dir into its user namespace. Returns True on move."""
    if not old.exists():
        return False
    if new.exists():
        logger.warning(
            "Skipping %s: destination %s already exists", old, new
        )
        return False
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(new))
    logger.info("Moved %s -> %s", old, new)
    return True


def _migrate_content_path(
    conn: sqlite3.Connection, project_id: int, content_path: str, new_dir: Path
) -> None:
    """Rewrite a note's content_path to point at the migrated location."""
    if not content_path:
        return
    old_path = Path(content_path)
    # Notes always live at <project_dir>/notes/<note_id>.md, so only the
    # file name needs to travel with the directory into the new namespace.
    new_path = new_dir / "notes" / old_path.name
    conn.execute(
        "UPDATE project_notes SET content_path = ? WHERE project_id = ?",
        (str(new_path), project_id),
    )
    conn.commit()


def _ambiguous_slugs(rows: list[dict]) -> set[str]:
    """Slugs claimed by more than one user (impossible to disambiguate)."""
    claims: dict[str, set[int]] = {}
    for row in rows:
        claims.setdefault(row["slug"], set()).add(row["user_id"])
    return {slug for slug, users in claims.items() if len(users) > 1}


def main() -> int:
    """Run the migration. Returns 0 on success, 1 on failure."""
    if not DB_PATH.exists():
        logger.error("projects.db not found at %s", DB_PATH)
        return 1
    if not OLD_PROJECTS_DIR.exists():
        logger.info("No %s directory; nothing to migrate", OLD_PROJECTS_DIR)
        return 0

    conn = _get_connection()
    rows = _projects_with_notes(conn)
    ambiguous = _ambiguous_slugs(rows)

    moved = 0
    migrated: set[tuple[int, str]] = set()
    for row in rows:
        user_id, slug = row["user_id"], row["slug"]
        old = OLD_PROJECTS_DIR / slug
        new = OLD_PROJECTS_DIR / str(user_id) / slug

        # Only the first owner of a shared slug gets the old files; the
        # filesystem could not tell them apart before namespacing.
        if (user_id, slug) not in migrated and old.exists():
            if _migrate_dir(old, new):
                moved += 1
                migrated.add((user_id, slug))

        if new.exists():
            _migrate_content_path(conn, row["id"], row["content_path"], new)

    conn.close()
    logger.info("Migration complete: %d project dir(s) moved", moved)
    if ambiguous:
        logger.warning(
            "Ambiguous slugs (claimed by multiple users): %s. Files went to the "
            "first owner; the other users' files could not be recovered.",
            ", ".join(sorted(ambiguous)),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
