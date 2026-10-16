"""Fail-secure access control for the Telegram bot.

Only users whose Telegram ID appears in ``AUTHORIZED_USERS`` may interact
with the bot. When the setting is missing, empty, or invalid the bot
denies everyone rather than accidentally opening access.
"""

from bot.config import load_settings

# Authorized Telegram user IDs, loaded once at import time. An empty set
# means nobody is authorized (fail-secure).
_authorized_users: frozenset[int] = load_settings().authorized_users


def is_authorized(
    user_id: int,
    authorized_users: set[int] | None = None,
) -> bool:
    """Return True only when ``user_id`` is in the authorized set.

    ``authorized_users`` overrides the config-loaded set, which keeps the
    function pure and trivially testable; when omitted it defaults to the
    module-level set loaded from ``AUTHORIZED_USERS`` at import time.

    Fail-secure: missing, empty, or invalid configuration yields an empty
    set and denies every user.
    """
    allowed = (
        authorized_users if authorized_users is not None else _authorized_users
    )
    return user_id in allowed
