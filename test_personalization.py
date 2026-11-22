"""Week-1 personalization tests: wish profile, keyword feedback, instant ears,
digest scheduling, periodic backfill, salary wish in search params."""
import asyncio
import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(__file__))


# ── keyword feedback ──────────────────────────────────────────────────────────

def test_explain_keyword_rejects_duty_phrase():
    from title_filter import explain_keyword

    status, reason = explain_keyword("Управление it проектами")
    assert status == "rejected"
    assert "обязанност" in reason or "роль" in reason


def test_explain_keyword_rejects_short():
    from title_filter import explain_keyword

    status, _ = explain_keyword("PM")
    assert status == "rejected"


def test_explain_keyword_flags_broad():
    from title_filter import explain_keyword

    for kw in ("Руководитель отдела", "Менеджер проектов", "Руководитель проекта"):
        status, reason = explain_keyword(kw)
        assert status == "broad", kw
        assert "широк" in reason


def test_explain_keyword_ok_for_specific():
    from title_filter import explain_keyword

    for kw in ("IT project manager", "Руководитель IT проектов", "Technical Project Manager"):
        status, _ = explain_keyword(kw)
        assert status == "ok", kw


def test_keywords_feedback_matches_is_search_keyword():
    from title_filter import is_search_keyword, keywords_feedback

    kws = ["IT project manager", "Управление проектами", "Руководитель отдела", "q"]
    fb = keywords_feedback(kws)
    assert len(fb) == 4
    # anything explain_keyword rejects must also be dropped by the search filter
    for k, status, _ in fb:
        if status == "rejected":
            assert not is_search_keyword(k)


# ── wish profile ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wish_profile_defaults_and_validation(monkeypatch):
    import db_postgres as db

    store = {}

    async def fake_get_setting(chat_id, key):
        return store.get(key)

    async def fake_set_setting(chat_id, key, value):
        store[key] = value

    async def fake_get_profile(chat_id):
        return {"target_role": "Руководитель IT проектов"}

    async def fake_conn_call(fn, *args, **kwargs):
        name = getattr(fn, "__name__", "")
        if name == "get_user_setting":
            return store.get(args[1])
        if name == "set_user_setting":
            store[args[1]] = args[2]
            return None
        if name == "get_user_profile":
            return {"target_role": "Руководитель IT проектов"}
        raise AssertionError(f"unexpected {name}")

    monkeypatch.setattr(db, "_conn_call", fake_conn_call)

    w = await db.get_wish_profile_for_chat(42)
    assert w["wish_format"] == "any" and w["wish_experience"] == "any"
    assert w["wish_role"] == "Руководитель IT проектов"  # default from resume
    assert w["wish_salary_min"] is None

    await db.set_wish_for_chat(42, "wish_salary_min", "от 250")
    assert store["wish_salary_min"] == "250"
    await db.set_wish_for_chat(42, "wish_format", "remote")
    assert store["wish_format"] == "remote"
    await db.set_wish_for_chat(42, "wish_format", "bogus")  # invalid → any
    assert store["wish_format"] == "any"
    await db.set_wish_for_chat(42, "wish_experience", "moreThan6")
    assert store["wish_experience"] == "moreThan6"
    await db.set_wish_for_chat(42, "wish_salary_max", "")  # clear
    assert store["wish_salary_max"] == ""
    with pytest.raises(ValueError):
        await db.set_wish_for_chat(42, "wish_nope", "x")

    w2 = await db.get_wish_profile_for_chat(42)
    assert w2["wish_salary_min"] == 250
    assert w2["wish_experience"] == "moreThan6"


# ── ears instant ──────────────────────────────────────────────────────────────

def test_ears_allowed_includes_instant():
    import db_postgres as db

    assert "instant" in db._EARS_ALLOWED


def test_salary_range_parser():
    import bot

    assert bot._parse_salary_range("от 250 до 400") == (250, 400)
    assert bot._parse_salary_range("250-400") == (250, 400)
    assert bot._parse_salary_range("от 250") == (250, None)
    assert bot._parse_salary_range("до 400") == (None, 400)
    assert bot._parse_salary_range("нет цифр") == (None, None)


def test_digest_due_skips_instant():
    from datetime import datetime, timedelta, timezone

    import bot

    msk = timezone(timedelta(hours=3))
    now = datetime(2026, 11, 22, 10, 0, tzinfo=msk)
    last = now - timedelta(hours=5)
    assert bot._digest_due("instant", last, now) is False
    assert bot._digest_due("hourly", last, now) is True
    assert bot._digest_due("hourly", now - timedelta(minutes=30), now) is False


# ── instant push path in score_server ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_instant_push_sends_and_hourly_does_not(monkeypatch):
    import score_server
    import tg_notify

    sent = []

    async def fake_send(chat_id, vacancy, score=None, reason="", vacancy_id=None):
        sent.append((chat_id, score))
        return True

    async def freq_for(chat_id):
        return {"1": "instant", "2": "hourly"}[str(chat_id)]

    monkeypatch.setattr(tg_notify, "send_vacancy_push", fake_send)
    m = type(score_server.db)("m")  # placeholder, attrs patched below
    monkeypatch.setattr(
        score_server.db, "is_notify_new_for_chat", AsyncMock(return_value=True), raising=False
    )
    monkeypatch.setattr(
        score_server.db, "get_ears_freq_for_chat", AsyncMock(side_effect=freq_for), raising=False
    )
    monkeypatch.setattr(
        score_server.db, "get_quiet_hours_for_chat", AsyncMock(return_value="off"), raising=False
    )
    monkeypatch.setattr(
        score_server.db, "get_companies_block_for_chat", AsyncMock(return_value=[]), raising=False
    )

    vac = {"title": "IT PM", "company": "C", "url": "https://hh.ru/vacancy/1", "salary": ""}
    await score_server._jh_notify_if_new(1, vac, "new", score=8, reason="ok")   # instant → send
    await score_server._jh_notify_if_new(2, dict(vac), "new", score=8, reason="ok")  # hourly → skip
    assert sent == [(1, 8)]


# ── periodic backfill ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backfill_job_only_ready_monitoring_users(monkeypatch):
    import bot

    called = []

    async def fake_ready(cid):
        return (cid != 3, "hint")

    async def fake_monitoring(cid):
        return cid != 2  # user 2 ready but monitoring off

    async def fake_backfill(cid, days=1, limit=20, notify=False):
        called.append((cid, days, limit, notify))
        return {"ok": 1, "err": 0, "total": 1, "matched": 1}

    import user_feed_backfill as ufb

    monkeypatch.setattr(bot.db, "list_active_user_chat_ids", AsyncMock(return_value=[1, 2, 3]))
    monkeypatch.setattr(bot, "_user_search_ready", fake_ready)
    monkeypatch.setattr(bot.db, "is_monitoring_for_chat", fake_monitoring)
    monkeypatch.setattr(ufb, "backfill_user_feed", fake_backfill)
    import bot as bot_mod
    # backfill_job imports backfill_user_feed from the module at call time
    monkeypatch.setattr("user_feed_backfill.backfill_user_feed", fake_backfill)

    class _Ctx:
        pass

    await bot_mod.backfill_job(_Ctx())
    assert called == [(1, 1, 20, False)]  # only user 1: ready + monitoring


# ── wish filters in poller search configs ─────────────────────────────────────

def test_filter_configs_by_wish():
    import hh_poller

    configs = [
        "&area=1&experience=between1And3",
        "&area=1&experience=between3And6",
        "&area=1&experience=moreThan6",
        "&schedule=remote&experience=between1And3",
        "&schedule=remote&experience=moreThan6",
        "&area=1&work_format=HYBRID&experience=moreThan6",
    ]
    out = hh_poller._filter_configs_by_wish(configs, {"wish_experience": "moreThan6"})
    assert out and all("experience=moreThan6" in c for c in out)

    out = hh_poller._filter_configs_by_wish(configs, {"wish_format": "remote"})
    assert out and all("schedule=remote" in c for c in out)

    out = hh_poller._filter_configs_by_wish(configs, {"wish_format": "hybrid"})
    assert out and all("work_format=HYBRID" in c for c in out)

    # empty result falls back to the full grid
    out = hh_poller._filter_configs_by_wish(
        ["&schedule=remote&experience=between1And3"], {"wish_experience": "moreThan6"}
    )
    assert out == ["&schedule=remote&experience=between1And3"]

    assert hh_poller._filter_configs_by_wish(configs, {}) == configs
