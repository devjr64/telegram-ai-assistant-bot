"""Shared time helpers for the bot's configured timezone.

Several modules need "today's date in the bot's timezone" (handlers,
tools, proactive). Keeping that logic here makes it single-source:
``today_key(settings)`` is the canonical way to get today's YYYY-MM-DD
key, and ``day_key(dt)`` is its generic datetime form.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.config import Settings


def day_key(dt: datetime) -> str:
    """Return the YYYY-MM-DD period key for ``dt``."""
    return dt.strftime("%Y-%m-%d")


def today_key(settings: Settings) -> str:
    """Return today's YYYY-MM-DD key in the bot's configured timezone."""
    return day_key(datetime.now(ZoneInfo(settings.timezone)))
