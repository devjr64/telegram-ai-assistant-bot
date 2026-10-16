"""Extra coverage for group parity (web_search, dolar, reminders, memory, cleanup)."""
import json
import time
import pytest
from bot import memory as mem
from bot import reminders
from bot import tools
from bot.projects import add_group_member, create_project, get_shared_projects_in_group, cleanup_group_data
from bot import summarizer
from bot.proactive import _list_group_ids

@pytest.mark.asyncio
async def test_web_search_in_group_allowed(monkeypatch):
    async def fake_search(**kwargs):
        return {"ok": True, "results": []}
    monkeypatch.setattr(tools, "web_search", fake_search)
    add_group_member(-10011, 1)
    result = await tools.execute_tool("web_search", {"query": "hola"}, 1, group_id=-10011)
    assert json.loads(result)["ok"] is True

@pytest.mark.asyncio
async def test_dolar_in_group_allowed(monkeypatch):
    async def fake_dolar():
        return {"ok": True, "oficial": 1, "paralela": 2}
    monkeypatch.setattr(tools, "get_dolar_info", fake_dolar)
    add_group_member(-10012, 1)
    result = await tools.execute_tool("dolar_rate", {}, 1, group_id=-10012)
    assert json.loads(result)["ok"] is True

@pytest.mark.asyncio
async def test_journal_append_group(monkeypatch):
    monkeypatch.setattr(tools, "_media_day", lambda s: "2026-08-20")
    add_group_member(-10013, 1)
    # need profile for author
    from bot.projects import upsert_user
    upsert_user(1, "pepe", "Pepe")
    result = await tools.execute_tool("journal_append", {"content": "reunión grupal"}, 1, group_id=-10013)
    payload = json.loads(result)
    assert payload["ok"] is True
    # check file contains @pepe
    content = mem.get_raw_content(group_id=-10013, day="2026-08-20")
    assert "@pepe" in content or "reunión" in content

@pytest.mark.asyncio
async def test_memory_read_group(monkeypatch):
    monkeypatch.setattr(tools, "_media_day", lambda s: "2026-08-21")
    gid = -10014
    add_group_member(gid, 1)
    from bot.projects import upsert_user
    upsert_user(1, "ana", "Ana")
    await tools.execute_tool("journal_append", {"content": "nota grupal"}, 1, group_id=gid)
    result = await tools.execute_tool("memory_read", {"period": "2026-08-21"}, 1, group_id=gid)
    payload = json.loads(result)
    assert payload["ok"] is True
    assert "nota grupal" in payload["content"]

@pytest.mark.asyncio
async def test_memory_recall_group(monkeypatch):
    gid = -10015
    add_group_member(gid, 1)
    mem.append_raw_entry(group_id=gid, day="2026-08-10", text="pago luz", tz_name="UTC")
    mem.save_summary(group_id=gid, level="weekly", period="2026-W33", content="pago luz semanal", range_start=1000, range_end=2000)
    result = await tools.execute_tool("memory_recall", {"query": "pago luz"}, 1, group_id=gid)
    payload = json.loads(result)
    assert payload["ok"] is True

@pytest.mark.asyncio
async def test_memory_insights_group(monkeypatch):
    monkeypatch.setattr(tools, "_media_day", lambda s: "2026-08-22")
    gid = -10016
    add_group_member(gid, 1)
    mem.append_raw_entry(group_id=gid, day="2026-08-22", text="test", tz_name="UTC")
    result = await tools.execute_tool("memory_insights", {"days": 2}, 1, group_id=gid)
    payload = json.loads(result)
    assert payload["ok"] is True
    assert "days" in payload

def test_reminders_group_shared():
    gid = -10017
    add_group_member(gid, 1)
    add_group_member(gid, 2)
    now = time.time()
    r1 = reminders.create_reminder(1, gid, "reunión", now+3600, group_id=gid, created_by=1)
    assert r1["group_id"] == gid
    lst1 = reminders.list_pending_reminders(1, group_id=gid)
    lst2 = reminders.list_pending_reminders(2, group_id=gid)
    assert len(lst1)==1 and len(lst2)==1
    # any member can edit
    assert reminders.update_reminder_text(r1["id"], 2, "reunión actualizada", group_id=gid) is True
    assert reminders.postpone_reminder(r1["id"], 2, 60, group_id=gid) is True
    assert reminders.update_reminder_time(r1["id"], 2, now+7200, group_id=gid) is True
    # any member can delete
    assert reminders.delete_reminder(r1["id"], 2, group_id=gid) is True
    assert reminders.list_pending_reminders(1, group_id=gid)==[]

def test_reminders_private_isolation():
    gid = -10018
    add_group_member(gid, 1)
    now = time.time()
    priv = reminders.create_reminder(1, 1, "priv", now+3600)
    assert reminders.list_pending_reminders(1, group_id=gid)==[]
    assert len(reminders.list_pending_reminders(1))==1
    # group lookup should not return private
    assert reminders.get_reminder_by_id_for_scope(priv["id"], 1, group_id=gid) is None
    # private lookup should not return group
    gid2 = -10019
    add_group_member(gid2, 1)
    gr = reminders.create_reminder(1, gid2, "grupal", now+3600, group_id=gid2, created_by=1)
    assert reminders.get_reminder_by_id(gr["id"], 1) is None

def test_memory_group_isolation():
    gid = -10020
    mem.append_raw_entry(group_id=gid, day="2026-08-20", text="secreto grupo", tz_name="UTC")
    mem.append_raw_entry(user_id=99, day="2026-08-20", text="secreto privado", tz_name="UTC")
    assert "secreto grupo" in mem.get_raw_content(group_id=gid, day="2026-08-20")
    assert "secreto privado" not in mem.get_raw_content(group_id=gid, day="2026-08-20")
    assert "secreto privado" in mem.get_raw_content(user_id=99, day="2026-08-20")
    # summaries isolation
    mem.save_summary(group_id=gid, level="weekly", period="2026-W33", content="grupo semanal", range_start=1000, range_end=2000)
    mem.save_summary(user_id=99, level="weekly", period="2026-W33", content="priv semanal", range_start=1000, range_end=2000)
    assert mem.get_summary_content(group_id=gid, level="weekly", period="2026-W33")=="grupo semanal"
    assert mem.get_summary_content(user_id=99, level="weekly", period="2026-W33")=="priv semanal"
    lst_g = mem.list_summaries(group_id=gid, level="weekly")
    lst_p = mem.list_summaries(user_id=99, level="weekly")
    assert len(lst_g)==1 and len(lst_p)==1

def test_cleanup_group_data():
    gid = -10021
    add_group_member(gid, 1)
    create_project(1, "ProjGrupoCleanup", group_id=gid)
    now = time.time()
    reminders.create_reminder(1, gid, "rem", now+3600, group_id=gid, created_by=1)
    mem.append_raw_entry(group_id=gid, day="2026-08-20", text="to delete", tz_name="UTC")
    mem.save_summary(group_id=gid, level="weekly", period="2026-W33", content="summary", range_start=1000, range_end=2000)
    # also history
    from bot.history import add_message
    add_message(1, "user", "hola grupo", chat_id=gid)
    cleanup_group_data(gid)
    assert get_shared_projects_in_group(gid)==[]
    assert reminders.list_pending_reminders(1, group_id=gid)==[]
    assert mem.get_raw_content(group_id=gid, day="2026-08-20")==""
    assert mem.list_summaries(group_id=gid)==[]

def test_filter_tool_definitions():
    from bot.tools import _filter_tool_definitions, PRIVATE_ONLY_TOOLS
    all_defs = _filter_tool_definitions(None)
    group_defs = _filter_tool_definitions(-10022)
    assert len(group_defs) < len(all_defs)
    blocked_names = {d["function"]["name"] for d in all_defs if d["function"]["name"] not in {x["function"]["name"] for x in group_defs}}
    assert blocked_names == PRIVATE_ONLY_TOOLS

@pytest.mark.asyncio
async def test_private_only_blocked_in_group():
    for name in ["generate_image", "set_checkin_time", "share_project"]:
        result = await tools.execute_tool(name, {}, 1, group_id=-10023)
        assert json.loads(result)["ok"] is False

def test_proactive_group_helpers():
    gid = -10024
    add_group_member(gid, 1)
    ids = _list_group_ids()
    assert gid in ids

def test_summarizer_group():
    gid = -10025
    mem.append_raw_entry(group_id=gid, day="2026-08-18", text="dia 1", tz_name="UTC")
    mem.append_raw_entry(group_id=gid, day="2026-08-19", text="dia 2", tz_name="UTC")
    src = summarizer.collect_period_sources(group_id=gid, level="weekly", period="2026-W34")
    # 2026-W34 includes 2026-08-18 .. 2026-08-24
    assert isinstance(src, list)

@pytest.mark.asyncio
async def test_generate_daily_compact_group(monkeypatch):
    gid = -10026
    day = "2026-08-18"
    mem.append_raw_entry(group_id=gid, day=day, text="palabra " * 200, tz_name="UTC")
    async def fake_call(*args, **kwargs):
        return "compacto"
    monkeypatch.setattr(summarizer, "_call_model", fake_call)
    result = await summarizer.generate_daily_compact(group_id=gid, day=day, threshold=10, client=None, model="m", tz_name="UTC")
    assert result == "compacto"
