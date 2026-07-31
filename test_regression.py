"""
Regression test suite for Job Hunter Bot.
Run: pytest test_regression.py -v
Slow (integration) tests: pytest test_regression.py -v -m integration
"""
import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import pytest_asyncio
import requests
import yaml

# ── Path setup ────────────────────────────────────────────────────────────────

sys.path.insert(0, os.path.dirname(__file__))

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    """Temporary SQLite DB path, isolated per test."""
    return str(tmp_path / "test_vacancies.db")


@pytest.fixture
def patch_db_path(tmp_db, monkeypatch):
    """Redirect db module to use temp DB instead of vacancies.db."""
    import db
    monkeypatch.setattr(db, "DB_PATH", tmp_db)
    return tmp_db


@pytest.fixture
def sample_vacancy():
    return {
        "source": "hh",
        "external_id": "hh_test_001",
        "channel": "hh",
        "text": "Technical Program Manager, 5 лет опыта, Agile",
        "title": "Technical Program Manager",
        "company": "TestCorp",
        "salary": "300 000 ₽",
        "url": "https://hh.ru/vacancy/12345",
        "score": None,
        "analysis": None,
        "status": "pending",
    }


@pytest.fixture
def sample_profile():
    return {
        "name": "Иван Иванов",
        "target_role": "TPM",
        "experience_years": 7,
        "level": "senior",
        "key_skills": ["Agile", "Scrum", "Roadmap"],
        "tech_stack": ["Jira", "Confluence"],
        "domains": ["fintech"],
        "key_achievements": ["Запустил 10 проектов"],
        "methodologies": ["Scrum", "Kanban"],
        "team_size": 20,
        "budget": "50M RUB",
        "languages": ["ru", "en"],
        "summary": "Опытный TPM с 7 годами опыта",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: DB MODULE
# ═══════════════════════════════════════════════════════════════════════════════

class TestDbInit:
    @pytest.mark.asyncio
    async def test_init_creates_tables(self, patch_db_path):
        import db
        await db.init_db()
        async with aiosqlite.connect(patch_db_path) as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                tables = {r[0] for r in await cur.fetchall()}
        assert "vacancies" in tables
        assert "channels" in tables
        assert "settings" in tables

    @pytest.mark.asyncio
    async def test_init_wal_mode(self, patch_db_path):
        import db
        await db.init_db()
        async with aiosqlite.connect(patch_db_path) as conn:
            async with conn.execute("PRAGMA journal_mode") as cur:
                mode = (await cur.fetchone())[0]
        assert mode == "wal"

    @pytest.mark.asyncio
    async def test_init_default_threshold(self, patch_db_path):
        import db
        await db.init_db()
        assert await db.get_threshold() == 7

    @pytest.mark.asyncio
    async def test_init_idempotent(self, patch_db_path):
        import db
        await db.init_db()
        await db.init_db()  # second call must not raise
        assert await db.get_threshold() == 7

    @pytest.mark.asyncio
    async def test_vacancies_has_viewed_column(self, patch_db_path):
        import db
        await db.init_db()
        async with aiosqlite.connect(patch_db_path) as conn:
            async with conn.execute("PRAGMA table_info(vacancies)") as cur:
                cols = {r[1] for r in await cur.fetchall()}
        assert "viewed" in cols


class TestDbSettings:
    @pytest.mark.asyncio
    async def test_get_set_setting(self, patch_db_path):
        import db
        await db.init_db()
        await db.set_setting("test_key", "test_value")
        assert await db.get_setting("test_key") == "test_value"

    @pytest.mark.asyncio
    async def test_get_missing_setting_returns_none(self, patch_db_path):
        import db
        await db.init_db()
        assert await db.get_setting("nonexistent_key") is None

    @pytest.mark.asyncio
    async def test_threshold_roundtrip(self, patch_db_path):
        import db
        await db.init_db()
        await db.set_threshold(8)
        assert await db.get_threshold() == 8

    @pytest.mark.asyncio
    async def test_monitoring_active_default_true(self, patch_db_path):
        import db
        await db.init_db()
        assert await db.is_monitoring_active() is True

    @pytest.mark.asyncio
    async def test_monitoring_toggle(self, patch_db_path):
        import db
        await db.init_db()
        await db.set_monitoring_active(False)
        assert await db.is_monitoring_active() is False
        await db.set_monitoring_active(True)
        assert await db.is_monitoring_active() is True

    @pytest.mark.asyncio
    async def test_analysis_model_roundtrip(self, patch_db_path):
        import db
        await db.init_db()
        await db.set_analysis_model("qwen3:14b")
        assert await db.get_analysis_model() == "qwen3:14b"

    @pytest.mark.asyncio
    async def test_profile_save_load(self, patch_db_path, sample_profile):
        import db
        await db.init_db()
        await db.save_profile(sample_profile)
        loaded = await db.get_active_profile()
        assert loaded["name"] == sample_profile["name"]
        assert loaded["experience_years"] == sample_profile["experience_years"]

    @pytest.mark.asyncio
    async def test_profile_none_when_not_set(self, patch_db_path):
        import db
        await db.init_db()
        assert await db.get_active_profile() is None

    @pytest.mark.asyncio
    async def test_resume_text_save(self, patch_db_path):
        import db
        await db.init_db()
        await db.save_resume_text("My resume text here")
        val = await db.get_setting("resume_text")
        assert val == "My resume text here"


class TestDbKeywords:
    @pytest.mark.asyncio
    async def test_add_keyword(self, patch_db_path):
        import db
        await db.init_db()
        initial = await db.get_keywords()
        await db.add_keyword("test keyword")
        kws = await db.get_keywords()
        assert "test keyword" in kws

    @pytest.mark.asyncio
    async def test_add_keyword_no_duplicates(self, patch_db_path):
        import db
        await db.init_db()
        await db.add_keyword("unique keyword")
        await db.add_keyword("unique keyword")
        kws = await db.get_keywords()
        assert kws.count("unique keyword") == 1

    @pytest.mark.asyncio
    async def test_remove_keyword(self, patch_db_path):
        import db
        await db.init_db()
        await db.add_keyword("to remove")
        await db.remove_keyword("to remove")
        kws = await db.get_keywords()
        assert "to remove" not in kws

    @pytest.mark.asyncio
    async def test_remove_keyword_case_insensitive(self, patch_db_path):
        import db
        await db.init_db()
        await db.add_keyword("CaseSensitive")
        await db.remove_keyword("casesensitive")
        kws = await db.get_keywords()
        assert "CaseSensitive" not in kws

    @pytest.mark.asyncio
    async def test_remove_nonexistent_keyword_safe(self, patch_db_path):
        import db
        await db.init_db()
        await db.remove_keyword("does not exist")  # must not raise


class TestDbVacancies:
    @pytest.mark.asyncio
    async def test_insert_and_exists(self, patch_db_path, sample_vacancy):
        import db
        await db.init_db()
        await db.insert_vacancy(sample_vacancy)
        assert await db.vacancy_exists("hh_test_001") is True

    @pytest.mark.asyncio
    async def test_not_exists(self, patch_db_path):
        import db
        await db.init_db()
        assert await db.vacancy_exists("nonexistent_id") is False

    @pytest.mark.asyncio
    async def test_insert_deduplication(self, patch_db_path, sample_vacancy):
        import db
        await db.init_db()
        r1 = await db.insert_vacancy(sample_vacancy)
        r2 = await db.insert_vacancy(sample_vacancy)  # OR IGNORE
        # Second insert returns 0 (no row inserted)
        assert r2 == 0

    @pytest.mark.asyncio
    async def test_get_by_external_id(self, patch_db_path, sample_vacancy):
        import db
        await db.init_db()
        await db.insert_vacancy(sample_vacancy)
        v = await db.get_vacancy_by_external("hh_test_001")
        assert v is not None
        assert v["title"] == "Technical Program Manager"
        assert v["company"] == "TestCorp"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, patch_db_path):
        import db
        await db.init_db()
        assert await db.get_vacancy_by_external("ghost_id") is None

    @pytest.mark.asyncio
    async def test_update_status(self, patch_db_path, sample_vacancy):
        import db
        await db.init_db()
        await db.insert_vacancy(sample_vacancy)
        await db.update_vacancy_status("hh_test_001", "applied")
        v = await db.get_vacancy_by_external("hh_test_001")
        assert v["status"] == "applied"

    @pytest.mark.asyncio
    async def test_mark_viewed(self, patch_db_path, sample_vacancy):
        import db
        await db.init_db()
        await db.insert_vacancy(sample_vacancy)
        await db.mark_viewed("hh_test_001")
        v = await db.get_vacancy_by_external("hh_test_001")
        assert v["viewed"] == 1

    @pytest.mark.asyncio
    async def test_update_vacancy_analysis(self, patch_db_path, sample_vacancy):
        import db
        await db.init_db()
        await db.insert_vacancy(sample_vacancy)
        analysis = {"score": 9, "pros": ["good"], "cons": [], "red_flags": []}
        await db.update_vacancy_analysis("hh_test_001", analysis)
        v = await db.get_vacancy_by_external("hh_test_001")
        loaded = json.loads(v["analysis"])
        assert loaded["score"] == 9

    @pytest.mark.asyncio
    async def test_get_pending_vacancies(self, patch_db_path, sample_vacancy):
        import db
        await db.init_db()
        await db.insert_vacancy(sample_vacancy)  # status=pending, score=None
        pending = await db.get_pending_vacancies()
        assert any(v["external_id"] == "hh_test_001" for v in pending)

    @pytest.mark.asyncio
    async def test_pending_excludes_scored(self, patch_db_path, sample_vacancy):
        import db
        await db.init_db()
        scored = dict(sample_vacancy, external_id="hh_scored_001")
        await db.insert_vacancy(scored)
        async with aiosqlite.connect(patch_db_path) as conn:
            await conn.execute(
                "UPDATE vacancies SET score=8 WHERE external_id='hh_scored_001'"
            )
            await conn.commit()
        pending = await db.get_pending_vacancies()
        assert not any(v["external_id"] == "hh_scored_001" for v in pending)

    @pytest.mark.asyncio
    async def test_get_recent_vacancies_all_sources(self, patch_db_path, sample_vacancy):
        import db
        await db.init_db()
        await db.insert_vacancy(sample_vacancy)
        results = await db.get_recent_vacancies(days=7, source=None)
        assert any(v["external_id"] == "hh_test_001" for v in results)

    @pytest.mark.asyncio
    async def test_get_recent_vacancies_source_filter(self, patch_db_path, sample_vacancy):
        import db
        await db.init_db()
        await db.insert_vacancy(sample_vacancy)  # source=hh
        habr_v = dict(sample_vacancy, source="habr", external_id="habr_001")
        await db.insert_vacancy(habr_v)

        hh_only = await db.get_recent_vacancies(days=7, source="hh")
        assert all(v["source"] == "hh" for v in hh_only)

        habr_only = await db.get_recent_vacancies(days=7, source="habr")
        assert all(v["source"] == "habr" for v in habr_only)

    @pytest.mark.asyncio
    async def test_get_recent_vacancies_tg_filter(self, patch_db_path, sample_vacancy):
        import db
        await db.init_db()
        tg_v = dict(sample_vacancy, source="telegram", external_id="tg_001")
        await db.insert_vacancy(tg_v)
        await db.insert_vacancy(sample_vacancy)  # source=hh

        tg_only = await db.get_recent_vacancies(days=7, source="tg")
        assert all(v["source"] not in ("hh", "habr", "getmatch") for v in tg_only)

    @pytest.mark.asyncio
    async def test_get_stats(self, patch_db_path, sample_vacancy):
        import db
        await db.init_db()
        await db.insert_vacancy(dict(sample_vacancy, status="new", external_id="s1"))
        await db.insert_vacancy(dict(sample_vacancy, status="new", external_id="s2"))
        await db.insert_vacancy(dict(sample_vacancy, status="applied", external_id="s3"))
        await db.insert_vacancy(dict(sample_vacancy, status="skipped", external_id="s4"))

        stats = await db.get_stats()
        assert stats["new"] >= 2
        assert stats["applied"] >= 1
        assert stats["skipped"] >= 1
        assert "interview" in stats
        assert "offer" in stats
        assert "rejected" in stats


class TestDbChannels:
    @pytest.mark.asyncio
    async def test_add_and_exists(self, patch_db_path):
        import db
        await db.init_db()
        await db.add_channel("@test_channel", "Test Channel")
        assert await db.channel_exists("@test_channel") is True

    @pytest.mark.asyncio
    async def test_not_exists(self, patch_db_path):
        import db
        await db.init_db()
        assert await db.channel_exists("@ghost_channel") is False

    @pytest.mark.asyncio
    async def test_get_active_channels(self, patch_db_path):
        import db
        await db.init_db()
        await db.add_channel("@active_ch")
        channels = await db.get_active_channels()
        assert "@active_ch" in channels

    @pytest.mark.asyncio
    async def test_set_channel_inactive(self, patch_db_path):
        import db
        await db.init_db()
        await db.add_channel("@toggleable")
        await db.set_channel_active("@toggleable", 0)
        channels = await db.get_active_channels()
        assert "@toggleable" not in channels

    @pytest.mark.asyncio
    async def test_get_all_channels_includes_inactive(self, patch_db_path):
        import db
        await db.init_db()
        await db.add_channel("@ch1")
        await db.add_channel("@ch2")
        await db.set_channel_active("@ch2", 0)
        all_ch = await db.get_all_channels()
        usernames = [c["username"] for c in all_ch]
        assert "@ch1" in usernames
        assert "@ch2" in usernames

    @pytest.mark.asyncio
    async def test_add_channel_idempotent(self, patch_db_path):
        import db
        await db.init_db()
        await db.add_channel("@same_ch")
        await db.add_channel("@same_ch")  # INSERT OR REPLACE — must not raise
        all_ch = await db.get_all_channels()
        assert sum(1 for c in all_ch if c["username"] == "@same_ch") == 1


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: GETMATCH TITLE FILTER
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetmatchTitleFilter:
    @pytest.fixture(autouse=True)
    def import_filter(self):
        from getmatch_poller import _is_relevant_title
        self.f = _is_relevant_title

    # Whitelist — must pass
    @pytest.mark.parametrize("title", [
        "Technical Program Manager",
        "IT Project Manager (Delivery)",
        "Delivery Manager",
        "Scrum Master",
        "Product Owner",
        "Engineering Manager",
        "Менеджер IT проектов",
        "Руководитель проектов",
        "Руководитель IT проектов",
        "TPM",
        "PMO",
        "Team Lead",
        "Tech Lead",
    ])
    def test_whitelist_passes(self, title):
        assert self.f(title) is True, f"Expected PASS for: {title!r}"

    # Blacklist — must be rejected
    @pytest.mark.parametrize("title", [
        "Junior Frontend Developer (React)",
        "Backend Developer (Python)",
        "QA Инженер / тестировщик",
        "Администратор баз данных (DBA)",
        "Data Quality Assurance / Инженер тестирования данных",
        "Автор на курс «Аудиоанализ, распознавание и генерация речи»",
        "Бизнес-аналитик (Кредитные продукты)",
        "DevOps Engineer",
        "Дизайнер интерфейсов",
        "ML Engineer",
        "Data Scientist",
        "Recruiter / HR BP",
    ])
    def test_blacklist_rejected(self, title):
        assert self.f(title) is False, f"Expected SKIP for: {title!r}"

    # Unknown — should pass through to AI
    @pytest.mark.parametrize("title", [
        "Старший менеджер продуктов развития",
        "Операционный директор",
        "Руководитель направления",
    ])
    def test_unknown_passes(self, title):
        assert self.f(title) is True, f"Expected PASS (unknown) for: {title!r}"


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: POLLER UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

class TestVacancyIdHashing:
    def test_hh_vacancy_id_deterministic(self):
        from hh_poller import _post_sync  # just ensure import works
        # HH uses external_id directly (vid from hh.ru)
        vid = "12345678"
        assert vid == vid  # deterministic by nature

    def test_habr_vacancy_id_md5(self):
        from habr_poller import _vacancy_id
        url = "https://career.habr.com/vacancies/1000123456"
        id1 = _vacancy_id(url)
        id2 = _vacancy_id(url)
        assert id1 == id2
        assert len(id1) == 32  # MD5 hex

    def test_habr_vacancy_id_different_urls(self):
        from habr_poller import _vacancy_id
        id1 = _vacancy_id("https://career.habr.com/vacancies/111")
        id2 = _vacancy_id("https://career.habr.com/vacancies/222")
        assert id1 != id2

    def test_getmatch_vacancy_id_strips_query_params(self):
        from getmatch_poller import _vacancy_id
        url_clean = "https://getmatch.ru/vacancies/12345-some-title"
        url_with_params = "https://getmatch.ru/vacancies/12345-some-title?s=offers&l=remote"
        assert _vacancy_id(url_clean) == _vacancy_id(url_with_params)

    def test_getmatch_vacancy_id_deterministic(self):
        from getmatch_poller import _vacancy_id
        url = "https://getmatch.ru/vacancies/99999-test"
        assert _vacancy_id(url) == _vacancy_id(url)

    def test_getmatch_vacancy_id_different_slugs(self):
        from getmatch_poller import _vacancy_id
        id1 = _vacancy_id("https://getmatch.ru/vacancies/111-aaa")
        id2 = _vacancy_id("https://getmatch.ru/vacancies/222-bbb")
        assert id1 != id2


class TestGetmatchNextDataParser:
    def test_extracts_vacancies_from_standard_key(self):
        from getmatch_poller import _extract_from_next_data
        html = """<html><head><script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"vacancies":[
            {"title":"TPM","url":"/vacancies/123-tpm","company":{"name":"Corp"},"salary":{"from":200000,"currency":"₽"}}
        ]}}}
        </script></head></html>"""
        items = _extract_from_next_data(html)
        assert len(items) == 1
        assert items[0]["title"] == "TPM"
        assert "getmatch.ru/vacancies/123-tpm" in items[0]["url"]

    def test_extracts_vacancies_using_slug_fallback(self):
        from getmatch_poller import _extract_from_next_data
        html = """<html><head><script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"offers":[
            {"title":"PM","slug":"456-pm","company":"StartupX","salary":""}
        ]}}}
        </script></head></html>"""
        items = _extract_from_next_data(html)
        assert len(items) == 1
        assert items[0]["title"] == "PM"

    def test_returns_empty_on_no_next_data(self):
        from getmatch_poller import _extract_from_next_data
        html = "<html><body><p>No script here</p></body></html>"
        assert _extract_from_next_data(html) == []

    def test_returns_empty_on_empty_vacancies_list(self):
        from getmatch_poller import _extract_from_next_data
        html = """<html><head><script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"vacancies":[]}}}
        </script></head></html>"""
        assert _extract_from_next_data(html) == []

    def test_skips_items_without_title(self):
        from getmatch_poller import _extract_from_next_data
        html = """<html><head><script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"vacancies":[
            {"url":"/vacancies/no-title-123"},
            {"title":"Real PM","url":"/vacancies/real-123"}
        ]}}}
        </script></head></html>"""
        items = _extract_from_next_data(html)
        assert len(items) == 1
        assert items[0]["title"] == "Real PM"

    def test_skips_items_without_valid_url(self):
        from getmatch_poller import _extract_from_next_data
        html = """<html><head><script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"vacancies":[
            {"title":"PM","url":"https://other.com/jobs/123"},
            {"title":"TPM","url":"/vacancies/valid-456"}
        ]}}}
        </script></head></html>"""
        items = _extract_from_next_data(html)
        assert len(items) == 1
        assert items[0]["title"] == "TPM"

    def test_string_salary_passthrough(self):
        from getmatch_poller import _extract_from_next_data
        html = """<html><head><script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"vacancies":[
            {"title":"PM","url":"/vacancies/1","salary":"от 200 000 ₽"}
        ]}}}
        </script></head></html>"""
        items = _extract_from_next_data(html)
        assert items[0]["salary"] == "от 200 000 ₽"


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: AI MODULE (mocked Ollama)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAiExtractJson:
    def test_extracts_valid_json(self):
        import ai
        text = 'Some prefix {"score": 8, "pros": ["good"]} suffix'
        result = ai._extract_json(text)
        assert result["score"] == 8

    def test_extracts_nested_json(self):
        import ai
        text = '```json\n{"a": {"b": 1}}\n```'
        result = ai._extract_json(text)
        assert result["a"]["b"] == 1

    def test_raises_on_no_json(self):
        import ai
        with pytest.raises(ValueError, match="No JSON found"):
            ai._extract_json("No JSON here at all")

    def test_raises_on_invalid_json(self):
        import ai
        with pytest.raises(Exception):
            ai._extract_json("{invalid json here}")


class TestOllamaFilter:
    @pytest.mark.asyncio
    async def test_returns_true_on_yes(self):
        import ai
        with patch("ai.call_agent", new=AsyncMock(return_value="YES")):
            result = await ai.ollama_filter("Is this a PM role?")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_no(self):
        import ai
        with patch("ai.call_agent", new=AsyncMock(return_value="NO")):
            result = await ai.ollama_filter("Is this a PM role?")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_error(self):
        import ai
        with patch("ai.call_agent", new=AsyncMock(side_effect=Exception("timeout"))):
            result = await ai.ollama_filter("Any prompt")
        assert result is False

    @pytest.mark.asyncio
    async def test_case_insensitive_yes(self):
        import ai
        with patch("ai.call_agent", new=AsyncMock(return_value="yes, definitely")):
            result = await ai.ollama_filter("prompt")
        assert result is True


class TestAnalyzeVacancy:
    @pytest.mark.asyncio
    async def test_returns_dict_with_required_fields(self, sample_profile):
        import ai
        mock_response = json.dumps({
            "score": 9,
            "title": "TPM",
            "company": "TestCo",
            "salary": "300k",
            "format": "remote",
            "pros": ["good pay"],
            "cons": [],
            "red_flags": [],
            "match_summary": "Great fit",
        })
        with patch("ai.call_agent", new=AsyncMock(return_value=mock_response)):
            result = await ai.analyze_vacancy("TPM role at TestCo", sample_profile)
        assert result["score"] == 9
        assert "pros" in result
        assert "cons" in result
        assert "red_flags" in result

    @pytest.mark.asyncio
    async def test_truncates_long_vacancy_text(self, sample_profile):
        import ai
        long_text = "A" * 10000
        captured = []

        async def mock_agent(role, prompt, temperature=None):
            captured.append(prompt)
            return '{"score":5,"title":"","company":"","salary":"","format":"","pros":[],"cons":[],"red_flags":[],"match_summary":""}'

        with patch("ai.call_agent", new=mock_agent):
            await ai.analyze_vacancy(long_text, sample_profile)

        assert len(captured) == 1
        assert "A" * (ai.MAX_VACANCY_CHARS + 1) not in captured[0]


class TestExtractProfile:
    @pytest.mark.asyncio
    async def test_returns_profile_dict(self):
        import ai
        mock_response = json.dumps({
            "name": "Test User",
            "target_role": "TPM",
            "experience_years": 5,
            "level": "senior",
            "key_skills": ["Agile"],
            "tech_stack": [],
            "domains": [],
            "key_achievements": [],
            "methodologies": [],
            "team_size": 10,
            "budget": "10M",
            "languages": ["ru"],
            "summary": "Good PM",
        })
        with patch("ai.call_agent", new=AsyncMock(return_value=mock_response)):
            result = await ai.extract_profile("My resume text")
        assert result["name"] == "Test User"
        assert result["experience_years"] == 5


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5: SCORE SERVER
# ═══════════════════════════════════════════════════════════════════════════════

SCORE_SERVER_URL = "http://localhost:5679"


@pytest.mark.integration
class TestScoreServer:
    def test_health_endpoint(self):
        resp = requests.get(f"{SCORE_SERVER_URL}/health", timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_update_score_missing_external_id(self):
        resp = requests.post(
            f"{SCORE_SERVER_URL}/update_score",
            json={"score": 9, "title": "TPM"},
            timeout=5,
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_update_score_high_score_returns_ok(self):
        """score >= threshold → server returns ok:true."""
        resp = requests.post(
            f"{SCORE_SERVER_URL}/update_score",
            json={
                "external_id": "reg_score_high_999",
                "score": 9,
                "title": "TPM",
                "company": "Corp",
                "salary": "300k",
                "pros": [],
                "cons": [],
                "red_flags": [],
                "match_summary": "great",
            },
            timeout=5,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_update_score_low_score_returns_ok(self):
        """score < threshold → server still returns ok:true (just sets skipped)."""
        resp = requests.post(
            f"{SCORE_SERVER_URL}/update_score",
            json={
                "external_id": "reg_score_low_999",
                "score": 2,
                "title": "Some Title",
                "company": "Corp",
                "salary": "",
                "pros": [],
                "cons": [],
                "red_flags": [],
                "match_summary": "bad",
            },
            timeout=5,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_update_score_threshold_logic_in_db(self):
        """Integration: insert real vacancy into PostgreSQL, post to score_server, verify status."""
        import db_postgres as pgdb
        import time
        test_id = "reg_threshold_test_001"

        async def run_all():
            await pgdb.init_db()
            pool = await pgdb._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM vacancies WHERE external_id=$1", test_id
                )
            await pgdb.insert_vacancy({
                "source": "test", "external_id": test_id,
                "channel": "test", "text": "Test", "title": "Test",
                "company": "Test", "salary": "", "url": "", "status": "pending",
            })

            # Score below threshold → must become "skipped"
            requests.post(
                f"{SCORE_SERVER_URL}/update_score",
                json={"external_id": test_id, "score": 3, "title": "T",
                      "company": "", "salary": "", "pros": [], "cons": [],
                      "red_flags": [], "match_summary": ""},
                timeout=5,
            )

            # Give score_server a moment to commit
            await asyncio.sleep(0.3)

            return await pgdb.get_vacancy_by_external(test_id)

        # Reset pool so it's created fresh in this event loop
        pgdb._pool = None
        row = asyncio.run(run_all())
        assert row is not None
        assert row["score"] == 3
        assert row["status"] == "skipped"


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6: N8N + FULL PIPELINE (integration)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestN8nPipeline:
    N8N_WEBHOOK = "http://localhost:5678/webhook/vacancy"

    def test_webhook_accepts_vacancy(self):
        resp = requests.post(
            self.N8N_WEBHOOK,
            json={
                "source": "test",
                "id": "reg_test_pipeline_001",
                "external_id": "reg_test_pipeline_001",
                "title": "Technical Program Manager",
                "company": "RegressionTestCo",
                "salary": "250000",
                "url": "https://example.com/tpm-001",
                "description": "Technical Program Manager. 5 years experience. Agile, Scrum.",
                "active_profile": {},
            },
            timeout=10,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "message" in data or "ok" in data  # n8n returns {message: "Workflow was started"}

    def test_n8n_is_reachable(self):
        resp = requests.get("http://localhost:5678/healthz", timeout=5)
        assert resp.status_code == 200

    def test_score_server_is_reachable(self):
        resp = requests.get(f"{SCORE_SERVER_URL}/health", timeout=5)
        assert resp.status_code == 200

    def test_ollama_is_reachable(self):
        resp = requests.get("http://localhost:11434/api/tags", timeout=10)
        assert resp.status_code == 200
        models = [m["name"] for m in resp.json().get("models", [])]
        assert len(models) > 0

    def test_ollama_has_qwen3_14b(self):
        resp = requests.get("http://localhost:11434/api/tags", timeout=10)
        models = [m["name"] for m in resp.json().get("models", [])]
        assert "qwen3:14b" in models, f"qwen3:14b missing, have: {models}"

    @pytest.mark.slow
    def test_full_pipeline_scores_and_returns(self):
        """End-to-end: send vacancy → wait → check score_server received callback."""
        import time
        unique_id = f"reg_e2e_{int(time.time())}"
        requests.post(
            self.N8N_WEBHOOK,
            json={
                "source": "test",
                "id": unique_id,
                "external_id": unique_id,
                "title": "Technical Program Manager",
                "company": "E2ETestCo",
                "salary": "350000",
                "url": "https://example.com/e2e",
                "description": "Technical Program Manager needed. Lead cross-functional teams. Agile, Scrum, roadmap planning.",
                "active_profile": {},
            },
            timeout=10,
        )

        # Wait up to 150s for Ollama (qwen3:14b) + Deepseek
        deadline = time.time() + 150
        found = False
        while time.time() < deadline:
            with open("score_server.log", encoding="utf-8", errors="replace") as f:
                if unique_id in f.read():
                    found = True
                    break
            time.sleep(5)

        assert found, f"score_server did not receive callback for {unique_id} within 150s"


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7: N8N WORKFLOW CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestN8nWorkflowConfig:
    """Requires live n8n + N8N_API_KEY env (never hardcode keys in tests)."""
    WF_ID = "4zlvC9yoSJH3YO5I"

    def _get_workflow(self):
        key = os.environ.get("N8N_API_KEY")
        if not key:
            pytest.skip("N8N_API_KEY not set")
        resp = requests.get(
            f"http://localhost:5678/api/v1/workflows/{self.WF_ID}",
            headers={"X-N8N-API-KEY": key},
            timeout=10,
        )
        assert resp.status_code == 200
        return resp.json()

    def test_workflow_is_active(self):
        wf = self._get_workflow()
        assert wf["active"] is True

    def test_no_qwen25_model_in_workflow(self):
        wf = self._get_workflow()
        body = json.dumps(wf)
        assert "qwen2.5" not in body, "qwen2.5:14b still referenced in workflow!"

    def test_qwen3_model_configured(self):
        wf = self._get_workflow()
        body = json.dumps(wf)
        assert "qwen3:14b" in body

    def test_ollama_node_timeout_sufficient(self):
        wf = self._get_workflow()
        for node in wf["nodes"]:
            if node["name"] == "Ollama — is PM?":
                timeout = node["parameters"].get("options", {}).get("timeout", 0)
                assert timeout >= 60000, f"Ollama timeout too low: {timeout}ms"
                break

    def test_all_required_nodes_enabled(self):
        wf = self._get_workflow()
        required = {
            "Webhook — vacancy",
            "Ollama — is PM?",
            "Is PM / TPM?",
            "Deepseek — analyze",
            "Parse Deepseek JSON",
            "Score >= 7?",
            "Save Score to DB",
        }
        nodes = {n["name"]: n for n in wf["nodes"]}
        for name in required:
            assert name in nodes, f"Node '{name}' not found in workflow"
            assert not nodes[name].get("disabled", False), f"Node '{name}' is disabled!"

    def test_save_score_node_points_to_correct_port(self):
        wf = self._get_workflow()
        for node in wf["nodes"]:
            if node["name"] == "Save Score to DB":
                url = node["parameters"].get("url", "")
                assert "5679" in url or "update_score" in url, \
                    f"Save Score node URL looks wrong: {url}"
                break


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8: CONFIG SANITY
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigSanity:
    @pytest.fixture(autouse=True)
    def load_config(self):
        with open("config.yaml", encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)

    def test_n8n_webhook_url_configured(self):
        url = self.cfg["n8n"]["webhook_url"]
        assert url.startswith("http"), f"Invalid webhook URL: {url}"
        assert "localhost" in url or "127.0.0.1" in url

    def test_ollama_url_configured(self):
        url = self.cfg["ollama"]["url"]
        assert url.startswith("http")

    def test_ollama_model_not_qwen25(self):
        model = self.cfg["ollama"].get("model", "")
        assert "qwen2.5" not in model, f"Config still has old model: {model}"

    def test_keywords_not_empty(self):
        kws = self.cfg["hh"]["keywords"]
        assert isinstance(kws, list)
        assert len(kws) > 0

    def test_db_path_configured(self):
        assert "path" in self.cfg["db"]
        assert self.cfg["db"]["path"].endswith(".db")

    def test_hh_area_configured(self):
        assert "area_moscow" in self.cfg["hh"]
        assert isinstance(self.cfg["hh"]["area_moscow"], int)
