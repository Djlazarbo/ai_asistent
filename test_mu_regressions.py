"""Multi-user regression tests (unit, no live services).

Covers the regressions from docs/GLM_PROJECT_BRIEF.md §8 + the 2026-11 fixes:
- channel overwrite on retry / source-name fallback
- monitoring default true for missing per-user setting
- backfill posts with only_chat_id
- fan-out isolation (tg channel ownership vs board keywords vs blocklist)
- db cover/interview wrappers: chat-scoped, bot.py call signatures valid
- drain helpers: pending-for-all-users scope, poison-pill skip
- searchnow: 409 "already queued" must not fall through to the light search
- tg_notify: missing module used to kill instant pushes on ImportError
"""
import asyncio
import inspect
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(__file__))


# ── channel overwrite ─────────────────────────────────────────────────────────

def test_channel_from_vacancy_never_falls_back_to_source():
    import bot

    # stored good channel wins
    assert bot._channel_from_vacancy({"channel": "@jobs", "url": ""}) == "@jobs"
    # source-like names are rejected and recovered from the t.me URL
    v = {"channel": "telegram", "url": "https://t.me/projects_jobs/151222"}
    assert bot._channel_from_vacancy(v) == "@projects_jobs"
    v = {"channel": "unknown", "url": "https://t.me/careerhub_pm/42"}
    assert bot._channel_from_vacancy(v) == "@careerhub_pm"
    # board vacancy: no channel, no t.me URL → stays empty-ish, never "@hh"
    v = {"channel": "hh", "url": "https://hh.ru/vacancy/123"}
    assert bot._channel_from_vacancy(v) == "hh"


def test_pool_upsert_sql_preserves_channel():
    """The ON CONFLICT clause must keep a good channel and ignore source names."""
    import user_data

    src = inspect.getsource(user_data)
    assert "EXCLUDED.channel IS NULL" in src
    assert "COALESCE(vacancies_pool.channel, EXCLUDED.channel)" in src


# ── monitoring default ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_is_monitoring_for_chat_defaults_true(monkeypatch):
    import db_postgres as db

    store = {111: None, 222: "false", 333: "true"}  # None == setting missing

    async def fake_conn_call(fn, *args, **kwargs):
        assert getattr(fn, "__name__", "") == "get_user_setting"
        return store.get(args[0])

    monkeypatch.setattr(db, "_conn_call", fake_conn_call)
    assert await db.is_monitoring_for_chat(111) is True   # missing → ON
    assert await db.is_monitoring_for_chat(222) is False
    assert await db.is_monitoring_for_chat(333) is True


@pytest.mark.asyncio
async def test_is_notify_new_for_chat_defaults_true(monkeypatch):
    import db_postgres as db

    async def fake_conn_call(fn, *args, **kwargs):
        return None

    monkeypatch.setattr(db, "_conn_call", fake_conn_call)
    assert await db.is_notify_new_for_chat(111) is True


# ── backfill only_chat_id ─────────────────────────────────────────────────────

class _FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_backfill_posts_with_only_chat_id(monkeypatch):
    import urllib.request
    import user_feed_backfill as ufb

    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("SCORE_URL", raising=False)
    monkeypatch.setenv("N8N_WEBHOOK_URL", "http://score:5679")

    rows = [{
        "id": "tg_1_2", "source": "telegram", "title": "PM", "company": "C",
        "salary": "", "url": "https://t.me/jobs/2", "channel": "@jobs",
        "text": "ищем PM",
    }]
    stats = await ufb.score_rows_for_user(987654, rows, notify=False)

    assert stats == {"ok": 1, "err": 0, "total": 1}
    assert len(captured) == 1
    req = captured[0]
    assert req.full_url == "http://score:5679/webhook/vacancy"
    payload = json.loads(req.data.decode())
    assert payload["only_chat_id"] == 987654
    assert payload["channel"] == "@jobs"
    assert payload["catchup"] is True and payload["notify"] is False


# ── fan-out isolation ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fanout_tg_only_to_channel_owner(monkeypatch):
    import score_fanout_opt as sfo

    monkeypatch.setattr(sfo, "filter_chats_with_profile", AsyncMock(return_value=[1, 2]))
    monkeypatch.setattr(sfo, "get_company_lists", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(
        sfo, "user_owns_channel",
        AsyncMock(side_effect=lambda cid, ch: cid == 1),
    )
    out = await sfo.filter_chats_for_vacancy(
        [1, 2], source="telegram", channel="@jobs", title="PM", text="", company="",
    )
    assert out == [1]


@pytest.mark.asyncio
async def test_fanout_boards_need_keyword_match(monkeypatch):
    import score_fanout_opt as sfo

    monkeypatch.setattr(sfo, "filter_chats_with_profile", AsyncMock(return_value=[1, 2]))
    monkeypatch.setattr(sfo, "get_company_lists", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(
        sfo, "user_has_keywords", AsyncMock(side_effect=lambda cid: cid == 1),
    )

    async def fake_setting(cid, key):
        return json.dumps(["IT project manager"]) if cid == 1 else None

    monkeypatch.setattr(sfo, "_user_setting", fake_setting)
    monkeypatch.setattr(sfo, "user_owns_channel", AsyncMock(return_value=False))

    out = await sfo.filter_chats_for_vacancy(
        [1, 2], source="hh", channel="", title="IT Project Manager", text="", company="",
    )
    assert out == [1]


@pytest.mark.asyncio
async def test_fanout_companies_block_hard_skips(monkeypatch):
    import score_fanout_opt as sfo

    monkeypatch.setattr(sfo, "filter_chats_with_profile", AsyncMock(return_value=[1]))
    monkeypatch.setattr(
        sfo, "get_company_lists", AsyncMock(return_value=(["testcorp"], [])),
    )
    out = await sfo.filter_chats_for_vacancy(
        [1], source="hh", channel="", title="IT Project Manager", text="", company="TestCorp",
    )
    assert out == []


# ── db cover/interview wrappers (shadowing regression) ────────────────────────

def test_cover_interview_signatures_not_shadowed():
    """A duplicate block used to redefine these with chat-blind signatures,
    leaking interview notes across users and corrupting saves from bot.py."""
    import db_postgres as db

    sig = inspect.signature(db.save_cover_example)
    assert list(sig.parameters) == ["external_id", "text", "status", "chat_id"]

    sig = inspect.signature(db.get_cover_examples)
    assert list(sig.parameters) == ["limit", "chat_id"]

    sig = inspect.signature(db.save_interview)
    assert list(sig.parameters) == ["company", "vacancy_id", "notes", "questions_list", "chat_id"]

    sig = inspect.signature(db.get_interview_history)
    assert list(sig.parameters) == ["limit", "chat_id"]

    # bot.py call sites must bind without TypeError (no coroutine side effects)
    inspect.signature(db.save_cover_example).bind("ext1", "letter text", chat_id=42)
    inspect.signature(db.save_interview).bind("Corp", "ext1", "notes", [], chat_id=42)
    inspect.signature(db.get_interview_history).bind(limit=10)
    inspect.signature(db.get_cover_examples).bind(chat_id=42)


@pytest.mark.asyncio
async def test_cover_interview_route_per_chat(monkeypatch):
    import db_postgres as db

    calls = []

    async def fake_conn_call(fn, *args, **kwargs):
        calls.append((getattr(fn, "__name__", ""), args))
        return []

    monkeypatch.setattr(db, "_conn_call", fake_conn_call)
    monkeypatch.setattr(db, "_mu", lambda: True)

    # explicit chat_id wins even without request context
    await db.save_cover_example("ext1", "text", chat_id=42)
    await db.get_interview_history(limit=5, chat_id=42)
    await db.save_interview("C", "ext1", "n", [], chat_id=42)
    await db.get_cover_examples(limit=5, chat_id=42)

    assert calls[0][0] == "save_user_cover_example" and calls[0][1][0] == 42
    assert calls[1][0] == "get_user_interview_history" and calls[1][1][0] == 42
    assert calls[2][0] == "save_user_interview" and calls[2][1][0] == 42
    assert calls[3][0] == "get_user_cover_examples" and calls[3][1][0] == 42

    # no chat_id anywhere → fail closed in multi-user mode
    for coro in (
        db.get_interview_history(limit=5),
        db.get_cover_examples(limit=5),
        db.save_cover_example("e", "t"),
        db.save_interview("c", "e", "n"),
    ):
        with pytest.raises(RuntimeError):
            await coro


# ── drain helpers ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pending_all_users_clears_request_scope(monkeypatch):
    import bot
    import user_data

    seen_cids = []

    async def fake_pending():
        seen_cids.append(user_data.current_chat_id())
        return []

    monkeypatch.setattr(bot.db, "get_pending_vacancies", fake_pending)
    tok = user_data.set_request_chat_id(111)  # owner token, as inside /drainpending
    try:
        out = await bot._pending_all_users()
    finally:
        user_data.reset_request_chat_id(tok)
    assert out == []
    assert seen_cids == [None]  # manual drain must see ALL users, not just owner


@pytest.mark.asyncio
async def test_drain_skip_stuck_marks_skipped_for_that_chat(monkeypatch):
    import bot
    import user_data

    updates = []

    async def fake_update(external_id, status):
        updates.append((external_id, status, user_data.current_chat_id()))

    monkeypatch.setattr(bot.db, "update_vacancy_status", fake_update)
    ok = await bot._drain_skip_stuck({"external_id": "tg_1_5"}, 777)
    assert ok is True
    assert updates == [("tg_1_5", "skipped", 777)]


# ── searchnow: 409 must not duplicate into light search ───────────────────────

@pytest.mark.asyncio
async def test_searchnow_409_queued_does_not_run_light(monkeypatch):
    import bot

    monkeypatch.setattr(bot, "_user_search_ready", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(
        bot, "_search_now_via_poller",
        AsyncMock(return_value=(False, "Поиск в очереди/выполняется: ⏳ Вы ~2-й в очереди")),
    )
    light = AsyncMock(return_value=(0, "ok"))
    monkeypatch.setattr(bot, "_search_now_light", light)

    note, ok = await bot._execute_search_now(42)
    assert ok is True
    assert "в очереди" in note
    light.assert_not_awaited()


@pytest.mark.asyncio
async def test_searchnow_light_fallback_local_cooldown(monkeypatch):
    import bot

    monkeypatch.setattr(bot, "_user_search_ready", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(
        bot, "_search_now_via_poller",
        AsyncMock(return_value=(False, "Playwright poller недоступен")),
    )
    light = AsyncMock(return_value=(3, "light ok"))
    monkeypatch.setattr(bot, "_search_now_light", light)
    bot._light_search_last_ts.clear()

    note, ok = await bot._execute_search_now(42)  # poller down → light runs once
    assert ok is True
    assert light.await_count == 1

    note2, ok2 = await bot._execute_search_now(42)  # immediately again → cooldown
    assert ok2 is False
    assert "не чаще" in note2
    assert light.await_count == 1


# ── tg_notify ─────────────────────────────────────────────────────────────────

def test_tg_notify_formats_card_with_escaping():
    import tg_notify

    text = tg_notify.format_vacancy_push(
        {
            "title": "PM <b>x</b>",
            "company": "A&B",
            "salary": "100₽",
            "url": "https://hh.ru/vacancy/1",
            "channel": "@jobs",
            "source": "telegram",
        },
        score=8,
        reason="Хороший матч",
    )
    assert "PM &lt;b&gt;x&lt;/b&gt;" in text           # title escaped, no raw HTML
    assert "A&amp;B" in text                            # company escaped
    assert "8/10" in text
    assert "https://hh.ru/vacancy/1" in text
    assert "jobs" in text                               # channel badge, no raw @


@pytest.mark.asyncio
async def test_tg_notify_send_fails_closed_without_token(monkeypatch):
    import tg_notify

    monkeypatch.setattr(tg_notify, "_bot_token", lambda: "")
    assert await tg_notify.send_vacancy_push(1, {"title": "t"}) is False


# ── search must not freeze the bot (sequential updates + inline await) ────────

def test_builder_enables_concurrent_updates():
    """Without concurrent_updates(True) PTB processes updates one-by-one:
    a 10-30 min search handler froze the whole bot for every user."""
    import inspect
    import bot

    assert "concurrent_updates(True)" in inspect.getsource(bot)


@pytest.mark.asyncio
async def test_spawn_search_runs_in_background(monkeypatch):
    import asyncio as _aio
    import bot

    bot._search_tasks.clear()
    started = _aio.Event()
    release = _aio.Event()

    async def fake_exec(chat_id, progress_callback=None):
        started.set()
        await release.wait()
        return "Поиск завершён", True

    monkeypatch.setattr(bot, "_execute_search_now", fake_exec)
    monkeypatch.setattr(bot, "_edit_search_progress", AsyncMock())
    msg = AsyncMock()

    task = bot._spawn_search(42, msg)
    # handler-style flow: _spawn_search returned without waiting for the search
    await _aio.wait_for(started.wait(), timeout=2)
    msg.edit_text.assert_not_called()  # result not delivered until search ends

    release.set()
    await _aio.wait_for(task, timeout=2)
    assert msg.edit_text.called
    assert "Поиск завершён" in str(msg.edit_text.call_args)


@pytest.mark.asyncio
async def test_spawn_search_double_click_dedup(monkeypatch):
    import asyncio as _aio
    import bot

    bot._search_tasks.clear()
    release = _aio.Event()
    calls = []

    async def fake_exec(chat_id, progress_callback=None):
        calls.append(chat_id)
        await release.wait()
        return "done", True

    monkeypatch.setattr(bot, "_execute_search_now", fake_exec)
    monkeypatch.setattr(bot, "_edit_search_progress", AsyncMock())
    msg1, msg2 = AsyncMock(), AsyncMock()

    bot._spawn_search(7, msg1)
    bot._spawn_search(7, msg2)  # second click while first still running
    await _aio.sleep(0.05)  # let the background tasks start

    assert len(calls) == 1  # no duplicate search launched
    assert msg2.edit_text.called  # user told a search is already running
    release.set()
    await _aio.sleep(0.05)
    bot._search_tasks.clear()
