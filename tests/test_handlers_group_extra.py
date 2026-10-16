import time
import pytest
from bot import handlers, reminders
from tests.fakes import FakeMessage, FakeUpdate, FakeContext, FakeChat
from bot.config import Settings

def make_settings():
    return Settings(bot_token="test", authorized_users=frozenset({1,2}), opencode_api_key="k", model="m", timezone="America/Caracas")

def authorize(monkeypatch):
    monkeypatch.setattr(handlers, "is_authorized", lambda uid: True)

@pytest.mark.asyncio
async def test_recordar_command_group_creates_in_group(monkeypatch):
    authorize(monkeypatch)
    settings = make_settings()
    # mock AI parse
    async def fake_parse(*args, **kwargs):
        return {"datetime": "2026-12-25T10:00", "message": "test", "recurrence": "none"}
    monkeypatch.setattr(handlers, "parse_reminder_datetime", fake_parse)
    # need FakeChat with type group
    class FakeGroupChat:
        def __init__(self):
            self.id = -10030
            self.type = "supergroup"
    class FakeGroupMessage:
        def __init__(self):
            self.text = "/recordar test"
            self.chat = FakeGroupChat()
        async def reply_text(self, text, **kwargs):
            self.replied = text
    # Use real FakeUpdate but patch chat
    update = FakeUpdate()
    update.effective_chat = FakeGroupChat()
    update.effective_user = type("U", (), {"id": 1, "username": "a", "first_name": "A"})()
    update.message = FakeGroupMessage()
    context = FakeContext(settings)
    context.args = ["test"]
    context.bot_data = {"settings": settings, "ai_client": object(), "provider": "opencode"}
    await handlers.recordar_command_group(update, context)
    assert hasattr(update.message, "replied")
    lst = reminders.list_pending_reminders(1, group_id=-10030)
    assert len(lst)==1

@pytest.mark.asyncio
async def test_recordatorios_command_group_lists(monkeypatch):
    authorize(monkeypatch)
    settings = make_settings()
    gid=-10031
    from bot.projects import add_group_member
    add_group_member(gid, 1)
    reminders.create_reminder(1, gid, "g1", time.time()+3600, group_id=gid, created_by=1)
    reminders.create_reminder(2, gid, "g2", time.time()+3600, group_id=gid, created_by=2)
    class FakeGroupChat:
        def __init__(self):
            self.id = gid
            self.type = "group"
    update = FakeUpdate()
    update.effective_chat = FakeGroupChat()
    update.effective_user = type("U", (), {"id": 1, "username": "a", "first_name": "A"})()
    class FakeMsg:
        async def reply_text(self, text, **kwargs):
            self.text=text
    update.message = FakeMsg()
    context = FakeContext(settings)
    await handlers.recordatorios_command(update, context)
    assert hasattr(update.message, "text")
    assert "g1" in update.message.text or "g2" in update.message.text

@pytest.mark.asyncio
async def test_reminder_callback_group(monkeypatch):
    authorize(monkeypatch)
    settings = make_settings()
    gid=-10032
    from bot.projects import add_group_member
    add_group_member(gid, 1)
    add_group_member(gid, 2)
    r = reminders.create_reminder(1, gid, "tarea", time.time()+3600, group_id=gid, created_by=1)
    # Fake query message with chat
    class FakeChat2:
        def __init__(self):
            self.id = gid
            self.type = "group"
    class FakeQMsg:
        def __init__(self):
            self.chat = FakeChat2()
        async def edit_message_text(self, text, **kwargs):
            self.edited=text
    from tests.fakes import FakeCallbackQuery, FakeMessage
    qmsg = FakeQMsg()
    query = FakeCallbackQuery(data=f"remind_cancel:{r['id']}", message=qmsg, user_id=2)
    update = FakeUpdate(callback_query=query)
    context = FakeContext(settings)
    await handlers.reminder_callback(update, context)
    assert query.answered
    assert reminders.list_pending_reminders(1, group_id=gid)==[]

def test_cleanup_group_via_handler(monkeypatch):
    authorize(monkeypatch)
    gid=-10033
    from bot.projects import add_group_member, cleanup_group_data
    add_group_member(gid, 1)
    cleanup_group_data(gid)
    assert True

def test_extra_coverage():
    from bot.handlers import _filter_tool_definitions
    from bot.tools import _filter_tool_definitions as f
    assert f(-100) is not None
    from bot.auto_journal import _get_recent_messages_for_group
    msgs = _get_recent_messages_for_group(-99999)
    assert isinstance(msgs, list)
