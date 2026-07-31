"""
Tests for score_server.py — without a running server, using handler-level mocking.

Run: pytest test_score_server.py -v
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_request(body: dict):
    """Build a mock aiohttp Request whose .json() returns body."""
    mock_req = MagicMock()
    mock_req.json = AsyncMock(return_value=body)
    return mock_req


# ── /health ────────────────────────────────────────────────────────────────────

class TestHealthEndpoint:
    async def test_health_returns_200(self):
        from score_server import handle_health
        req = MagicMock()
        resp = await handle_health(req)
        assert resp.status == 200

    async def test_health_body(self):
        from score_server import handle_health
        req = MagicMock()
        resp = await handle_health(req)
        data = json.loads(resp.body)
        assert data["status"] == "ok"


# ── /update_score ──────────────────────────────────────────────────────────────

class TestUpdateScoreMissingId:
    async def test_empty_body_returns_400(self):
        from score_server import handle_update_score
        with patch("db_postgres.get_threshold", new_callable=AsyncMock), \
             patch("db_postgres.save_vacancy_score", new_callable=AsyncMock):
            resp = await handle_update_score(_make_request({}))
        assert resp.status == 400

    async def test_no_external_id_returns_400(self):
        from score_server import handle_update_score
        with patch("db_postgres.get_threshold", new_callable=AsyncMock), \
             patch("db_postgres.save_vacancy_score", new_callable=AsyncMock):
            resp = await handle_update_score(_make_request({"score": 9, "title": "TPM"}))
        assert resp.status == 400

    async def test_error_body_has_ok_false(self):
        from score_server import handle_update_score
        with patch("db_postgres.get_threshold", new_callable=AsyncMock), \
             patch("db_postgres.save_vacancy_score", new_callable=AsyncMock):
            resp = await handle_update_score(_make_request({}))
        data = json.loads(resp.body)
        assert data["ok"] is False

    async def test_empty_string_external_id_returns_400(self):
        from score_server import handle_update_score
        payload = {"external_id": "", "score": 9, "title": "TPM"}
        with patch("db_postgres.get_threshold", new_callable=AsyncMock), \
             patch("db_postgres.save_vacancy_score", new_callable=AsyncMock):
            resp = await handle_update_score(_make_request(payload))
        assert resp.status == 400


class TestUpdateScoreThresholdLogic:
    _BASE_PAYLOAD = {
        "external_id": "test_ext_001",
        "title": "TPM",
        "company": "Corp",
        "salary": "300k",
        "format": "remote",
        "pros": ["good"],
        "cons": [],
        "red_flags": [],
        "match_summary": "Great fit",
    }

    async def test_high_score_sets_new_status(self):
        from score_server import handle_update_score
        mock_save = AsyncMock()
        payload = {**self._BASE_PAYLOAD, "score": 9}

        with patch("db_postgres.get_threshold", new_callable=AsyncMock, return_value=7), \
             patch("db_postgres.save_vacancy_score", mock_save):
            resp = await handle_update_score(_make_request(payload))

        assert resp.status == 200
        args = mock_save.call_args[0]
        assert args[6] == "new"  # status is 7th positional arg

    async def test_low_score_sets_skipped_status(self):
        from score_server import handle_update_score
        mock_save = AsyncMock()
        payload = {**self._BASE_PAYLOAD, "score": 3}

        with patch("db_postgres.get_threshold", new_callable=AsyncMock, return_value=7), \
             patch("db_postgres.save_vacancy_score", mock_save):
            resp = await handle_update_score(_make_request(payload))

        assert resp.status == 200
        args = mock_save.call_args[0]
        assert args[6] == "skipped"

    async def test_score_equal_to_threshold_is_new(self):
        from score_server import handle_update_score
        mock_save = AsyncMock()
        payload = {**self._BASE_PAYLOAD, "score": 7}

        with patch("db_postgres.get_threshold", new_callable=AsyncMock, return_value=7), \
             patch("db_postgres.save_vacancy_score", mock_save):
            resp = await handle_update_score(_make_request(payload))

        args = mock_save.call_args[0]
        assert args[6] == "new"

    async def test_custom_threshold_respected(self):
        from score_server import handle_update_score
        mock_save = AsyncMock()
        payload = {**self._BASE_PAYLOAD, "score": 8}

        with patch("db_postgres.get_threshold", new_callable=AsyncMock, return_value=9), \
             patch("db_postgres.save_vacancy_score", mock_save):
            await handle_update_score(_make_request(payload))

        args = mock_save.call_args[0]
        assert args[6] == "skipped"  # 8 < 9

    async def test_ok_true_on_success(self):
        from score_server import handle_update_score
        payload = {**self._BASE_PAYLOAD, "score": 9}

        with patch("db_postgres.get_threshold", new_callable=AsyncMock, return_value=7), \
             patch("db_postgres.save_vacancy_score", AsyncMock()):
            resp = await handle_update_score(_make_request(payload))

        data = json.loads(resp.body)
        assert data["ok"] is True


class TestUpdateScoreSaveArgs:
    _BASE = {
        "external_id": "save_test_ext",
        "score": 8,
        "title": "Senior TPM",
        "company": "BigCorp",
        "salary": "400k",
        "format": "hybrid",
        "pros": ["great pay"],
        "cons": ["long hours"],
        "red_flags": [],
        "match_summary": "Very good fit",
    }

    async def test_save_called_once(self):
        from score_server import handle_update_score
        mock_save = AsyncMock()

        with patch("db_postgres.get_threshold", new_callable=AsyncMock, return_value=7), \
             patch("db_postgres.save_vacancy_score", mock_save):
            await handle_update_score(_make_request(self._BASE))

        mock_save.assert_called_once()

    async def test_save_receives_external_id(self):
        from score_server import handle_update_score
        mock_save = AsyncMock()

        with patch("db_postgres.get_threshold", new_callable=AsyncMock, return_value=7), \
             patch("db_postgres.save_vacancy_score", mock_save):
            await handle_update_score(_make_request(self._BASE))

        args = mock_save.call_args[0]
        assert args[0] == "save_test_ext"

    async def test_save_receives_integer_score(self):
        from score_server import handle_update_score
        mock_save = AsyncMock()

        with patch("db_postgres.get_threshold", new_callable=AsyncMock, return_value=7), \
             patch("db_postgres.save_vacancy_score", mock_save):
            await handle_update_score(_make_request(self._BASE))

        args = mock_save.call_args[0]
        assert args[1] == 8  # score

    async def test_analysis_json_contains_all_fields(self):
        from score_server import handle_update_score
        mock_save = AsyncMock()

        with patch("db_postgres.get_threshold", new_callable=AsyncMock, return_value=7), \
             patch("db_postgres.save_vacancy_score", mock_save):
            await handle_update_score(_make_request(self._BASE))

        args = mock_save.call_args[0]
        analysis = json.loads(args[5])  # analysis is 6th arg
        assert "score" in analysis
        assert "pros" in analysis
        assert "cons" in analysis
        assert "match_summary" in analysis

    async def test_none_score_becomes_skipped(self):
        from score_server import handle_update_score
        mock_save = AsyncMock()
        payload = {**self._BASE, "score": None}

        with patch("db_postgres.get_threshold", new_callable=AsyncMock, return_value=7), \
             patch("db_postgres.save_vacancy_score", mock_save):
            await handle_update_score(_make_request(payload))

        args = mock_save.call_args[0]
        assert args[6] == "skipped"
