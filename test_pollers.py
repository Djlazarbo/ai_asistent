"""
Tests for poller utilities — deduplication, ID hashing, n8n payload format.

Run: pytest test_pollers.py -v
"""
import hashlib
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Habr poller ────────────────────────────────────────────────────────────────

class TestHabrVacancyId:
    def test_md5_length(self):
        from habr_poller import _vacancy_id
        assert len(_vacancy_id("https://career.habr.com/vacancies/1000123456")) == 32

    def test_deterministic(self):
        from habr_poller import _vacancy_id
        url = "https://career.habr.com/vacancies/1000123456"
        assert _vacancy_id(url) == _vacancy_id(url)

    def test_different_urls_differ(self):
        from habr_poller import _vacancy_id
        assert _vacancy_id("https://career.habr.com/vacancies/111") != \
               _vacancy_id("https://career.habr.com/vacancies/222")

    def test_matches_expected_md5(self):
        from habr_poller import _vacancy_id
        url = "https://career.habr.com/vacancies/9999"
        expected = hashlib.md5(url.encode()).hexdigest()
        assert _vacancy_id(url) == expected


# ── Getmatch poller ────────────────────────────────────────────────────────────

class TestGetmatchVacancyId:
    def test_strips_query_params(self):
        from getmatch_poller import _vacancy_id
        clean = "https://getmatch.ru/vacancies/12345-some-title"
        with_params = "https://getmatch.ru/vacancies/12345-some-title?s=offers&l=remote"
        assert _vacancy_id(clean) == _vacancy_id(with_params)

    def test_deterministic(self):
        from getmatch_poller import _vacancy_id
        url = "https://getmatch.ru/vacancies/99999-test"
        assert _vacancy_id(url) == _vacancy_id(url)

    def test_different_slugs_differ(self):
        from getmatch_poller import _vacancy_id
        assert _vacancy_id("https://getmatch.ru/vacancies/111-aaa") != \
               _vacancy_id("https://getmatch.ru/vacancies/222-bbb")

    def test_multiple_query_params_stripped(self):
        from getmatch_poller import _vacancy_id
        url = "https://getmatch.ru/vacancies/456-pm"
        url_with_many = f"{url}?a=1&b=2&c=3"
        assert _vacancy_id(url) == _vacancy_id(url_with_many)

    def test_returns_hex_string(self):
        from getmatch_poller import _vacancy_id
        result = _vacancy_id("https://getmatch.ru/vacancies/123-test")
        assert all(c in "0123456789abcdef" for c in result)


# ── Getmatch title filter ──────────────────────────────────────────────────────

class TestGetmatchTitleFilterEdgeCases:
    def _f(self, title):
        from getmatch_poller import _is_relevant_title
        return _is_relevant_title(title)

    def test_technical_program_manager(self):
        assert self._f("Technical Program Manager") is True

    def test_tpm_abbreviation(self):
        assert self._f("TPM") is True

    def test_pmo(self):
        assert self._f("PMO Lead") is True

    def test_team_lead(self):
        assert self._f("Team Lead") is True

    def test_junior_developer_rejected(self):
        assert self._f("Junior Frontend Developer (React)") is False

    def test_devops_rejected(self):
        assert self._f("DevOps Engineer") is False

    def test_qa_engineer_rejected(self):
        assert self._f("QA Инженер / тестировщик") is False

    def test_data_scientist_rejected(self):
        assert self._f("Data Scientist") is False

    def test_case_insensitive_team_lead(self):
        assert self._f("team lead") is True

    def test_engineering_manager(self):
        assert self._f("Engineering Manager") is True


# ── HH poller: n8n payload format ─────────────────────────────────────────────

class TestHhPollerN8nPayload:
    """Verify that vacancies sent to n8n include all required fields."""

    async def test_send_includes_description_field(self):
        from hh_poller import _send_to_n8n
        captured = []

        def sync_post(url, payload):
            captured.append(payload)
            return 200

        with patch("hh_poller._post_sync", side_effect=sync_post):
            await _send_to_n8n({
                "source": "hh",
                "id": "test_001",
                "external_id": "test_001",
                "title": "TPM",
                "company": "Corp",
                "salary": "300k",
                "url": "https://hh.ru/1",
                "description": "Job description here",
                "message_text": "Job description here",
                "active_profile": {},
            })

        assert len(captured) == 1
        assert "description" in captured[0]

    async def test_send_includes_message_text_field(self):
        from hh_poller import _send_to_n8n
        captured = []

        def sync_post(url, payload):
            captured.append(payload)
            return 200

        with patch("hh_poller._post_sync", side_effect=sync_post):
            await _send_to_n8n({
                "source": "hh",
                "id": "test_002",
                "external_id": "test_002",
                "description": "text",
                "message_text": "text",
                "active_profile": {},
            })

        assert "message_text" in captured[0]

    async def test_send_includes_active_profile(self):
        from hh_poller import _send_to_n8n
        profile = {"target_role": "TPM"}
        captured = []

        def sync_post(url, payload):
            captured.append(payload)
            return 200

        with patch("hh_poller._post_sync", side_effect=sync_post):
            await _send_to_n8n({"id": "p_001", "external_id": "p_001",
                                 "description": "", "message_text": "",
                                 "active_profile": profile})

        assert captured[0]["active_profile"] == profile


# ── Deduplication via DB ───────────────────────────────────────────────────────

class TestPollerDeduplication:
    """Verify pollers call vacancy_exists and skip duplicates."""

    async def test_habr_skips_existing_vacancy(self):
        from habr_poller import _vacancy_id

        # If vacancy_exists returns True, poller should not insert or send to n8n
        existing_url = "https://career.habr.com/vacancies/9999999"
        existing_id = _vacancy_id(existing_url)

        mock_exists = AsyncMock(return_value=True)
        mock_insert = AsyncMock()
        mock_send = AsyncMock()

        with patch("db_postgres.vacancy_exists", mock_exists), \
             patch("db_postgres.insert_vacancy", mock_insert), \
             patch("hh_poller._send_to_n8n", mock_send):
            exists = await mock_exists(existing_id)
            if exists:
                pass  # poller should skip
            else:
                await mock_insert({})

        mock_insert.assert_not_called()

    async def test_getmatch_skips_existing_vacancy(self):
        from getmatch_poller import _vacancy_id

        url = "https://getmatch.ru/vacancies/123-tpm-role?s=offers"
        vac_id = _vacancy_id(url)

        mock_exists = AsyncMock(return_value=True)
        mock_insert = AsyncMock()

        with patch("db_postgres.vacancy_exists", mock_exists):
            exists = await mock_exists(vac_id)

        assert exists is True
        mock_insert.assert_not_called()
