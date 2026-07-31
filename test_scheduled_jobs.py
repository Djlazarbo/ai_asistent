"""
Tests for scheduled jobs and retry logic in bot.py.

Run: pytest test_scheduled_jobs.py -v
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ── _do_retry_scoring ─────────────────────────────────────────────────────────

class TestDoRetryScoring:
    def _make_vacancy(self, ext_id: str, **kwargs) -> dict:
        return {
            "external_id": ext_id,
            "source": "hh",
            "title": "TPM",
            "company": "Corp",
            "salary": "300k",
            "url": "https://hh.ru/1",
            "text": "PM role description",
            **kwargs,
        }

    async def test_returns_zero_when_no_pending(self):
        from bot import _do_retry_scoring
        with patch("db_postgres.get_pending_vacancies", new=AsyncMock(return_value=[])), \
             patch("db_postgres.get_active_profile", new=AsyncMock(return_value={})):
            result = await _do_retry_scoring()
        assert result == {"total": 0, "sent": 0}

    async def test_sends_all_pending_with_batch_zero(self):
        from bot import _do_retry_scoring
        vacancies = [self._make_vacancy(f"ext_{i}") for i in range(5)]
        mock_send = AsyncMock()

        with patch("db_postgres.get_pending_vacancies", new=AsyncMock(return_value=vacancies)), \
             patch("db_postgres.get_active_profile", new=AsyncMock(return_value={})), \
             patch("hh_poller._send_to_n8n", mock_send):
            result = await _do_retry_scoring(batch=0)

        assert result["total"] == 5
        assert result["sent"] == 5
        assert mock_send.call_count == 5

    async def test_batch_limits_sends(self):
        from bot import _do_retry_scoring
        vacancies = [self._make_vacancy(f"ext_{i}") for i in range(10)]
        mock_send = AsyncMock()

        with patch("db_postgres.get_pending_vacancies", new=AsyncMock(return_value=vacancies)), \
             patch("db_postgres.get_active_profile", new=AsyncMock(return_value={})), \
             patch("hh_poller._send_to_n8n", mock_send):
            result = await _do_retry_scoring(batch=3)

        # Implementation slices pending before tracking total, so total == batch
        assert result["sent"] == 3
        assert result["total"] == 3
        assert mock_send.call_count == 3

    async def test_payload_includes_description_field(self):
        from bot import _do_retry_scoring
        vacancy = self._make_vacancy("test_desc", text="Vacancy description text")
        captured = []

        async def capture_send(payload):
            captured.append(payload)

        with patch("db_postgres.get_pending_vacancies", new=AsyncMock(return_value=[vacancy])), \
             patch("db_postgres.get_active_profile", new=AsyncMock(return_value={})), \
             patch("hh_poller._send_to_n8n", capture_send):
            await _do_retry_scoring()

        assert len(captured) == 1
        assert "description" in captured[0], "payload must include 'description' for n8n"

    async def test_payload_includes_message_text_field(self):
        from bot import _do_retry_scoring
        vacancy = self._make_vacancy("test_msg_text", text="Vacancy text content")
        captured = []

        async def capture_send(payload):
            captured.append(payload)

        with patch("db_postgres.get_pending_vacancies", new=AsyncMock(return_value=[vacancy])), \
             patch("db_postgres.get_active_profile", new=AsyncMock(return_value={})), \
             patch("hh_poller._send_to_n8n", capture_send):
            await _do_retry_scoring()

        assert "message_text" in captured[0], "payload must include 'message_text' for telegram poller"

    async def test_description_and_message_text_equal_vacancy_text(self):
        from bot import _do_retry_scoring
        vacancy = self._make_vacancy("test_text_match", text="My vacancy content")
        captured = []

        async def capture_send(payload):
            captured.append(payload)

        with patch("db_postgres.get_pending_vacancies", new=AsyncMock(return_value=[vacancy])), \
             patch("db_postgres.get_active_profile", new=AsyncMock(return_value={})), \
             patch("hh_poller._send_to_n8n", capture_send):
            await _do_retry_scoring()

        p = captured[0]
        assert p["description"] == "My vacancy content"
        assert p["message_text"] == "My vacancy content"

    async def test_payload_includes_external_id(self):
        from bot import _do_retry_scoring
        vacancy = self._make_vacancy("my_ext_123")
        captured = []

        async def capture_send(payload):
            captured.append(payload)

        with patch("db_postgres.get_pending_vacancies", new=AsyncMock(return_value=[vacancy])), \
             patch("db_postgres.get_active_profile", new=AsyncMock(return_value={})), \
             patch("hh_poller._send_to_n8n", capture_send):
            await _do_retry_scoring()

        assert captured[0]["external_id"] == "my_ext_123"
        assert captured[0]["id"] == "my_ext_123"

    async def test_payload_includes_active_profile(self):
        from bot import _do_retry_scoring
        profile = {"name": "Test", "target_role": "PM"}
        vacancy = self._make_vacancy("profile_test")
        captured = []

        async def capture_send(payload):
            captured.append(payload)

        with patch("db_postgres.get_pending_vacancies", new=AsyncMock(return_value=[vacancy])), \
             patch("db_postgres.get_active_profile", new=AsyncMock(return_value=profile)), \
             patch("hh_poller._send_to_n8n", capture_send):
            await _do_retry_scoring()

        assert captured[0]["active_profile"] == profile

    async def test_send_error_doesnt_abort_others(self):
        from bot import _do_retry_scoring
        vacancies = [self._make_vacancy(f"err_{i}") for i in range(3)]
        call_count = 0

        async def flaky_send(payload):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("n8n timeout")

        with patch("db_postgres.get_pending_vacancies", new=AsyncMock(return_value=vacancies)), \
             patch("db_postgres.get_active_profile", new=AsyncMock(return_value={})), \
             patch("hh_poller._send_to_n8n", flaky_send):
            result = await _do_retry_scoring()

        # 3 total, 2 sent successfully (1 errored)
        assert result["total"] == 3
        assert result["sent"] == 2

    async def test_none_profile_treated_as_empty_dict(self):
        from bot import _do_retry_scoring
        vacancy = self._make_vacancy("no_profile")
        captured = []

        async def capture_send(payload):
            captured.append(payload)

        with patch("db_postgres.get_pending_vacancies", new=AsyncMock(return_value=[vacancy])), \
             patch("db_postgres.get_active_profile", new=AsyncMock(return_value=None)), \
             patch("hh_poller._send_to_n8n", capture_send):
            result = await _do_retry_scoring()

        assert result["sent"] == 1
        assert captured[0]["active_profile"] == {}


# ── retry_scoring_job ─────────────────────────────────────────────────────────

class TestRetryScoringJob:
    def _make_context(self):
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        return ctx

    async def test_notifies_when_sent_gt_zero(self):
        from bot import retry_scoring_job
        ctx = self._make_context()

        with patch("bot._do_retry_scoring", new=AsyncMock(return_value={"total": 5, "sent": 3})):
            await retry_scoring_job(ctx)

        ctx.bot.send_message.assert_called_once()
        msg_text = ctx.bot.send_message.call_args[1]["text"]
        assert "3" in msg_text

    async def test_no_notification_when_sent_zero(self):
        from bot import retry_scoring_job
        ctx = self._make_context()

        with patch("bot._do_retry_scoring", new=AsyncMock(return_value={"total": 0, "sent": 0})):
            await retry_scoring_job(ctx)

        ctx.bot.send_message.assert_not_called()

    async def test_calls_do_retry_scoring_with_score_batch(self):
        from bot import retry_scoring_job, _SCORE_BATCH
        ctx = self._make_context()
        mock_retry = AsyncMock(return_value={"total": 0, "sent": 0})

        with patch("bot._do_retry_scoring", mock_retry):
            await retry_scoring_job(ctx)

        mock_retry.assert_called_once_with(batch=_SCORE_BATCH)

    async def test_exception_is_caught_silently(self):
        from bot import retry_scoring_job
        ctx = self._make_context()

        with patch("bot._do_retry_scoring", new=AsyncMock(side_effect=Exception("DB down"))):
            # Must not raise
            await retry_scoring_job(ctx)

    async def test_notification_mentions_total_count(self):
        from bot import retry_scoring_job
        ctx = self._make_context()

        with patch("bot._do_retry_scoring", new=AsyncMock(return_value={"total": 20, "sent": 10})):
            await retry_scoring_job(ctx)

        msg_text = ctx.bot.send_message.call_args[1]["text"]
        assert "20" in msg_text

    async def test_sends_to_owner_chat_id(self):
        from bot import retry_scoring_job, OWNER_CHAT_ID
        ctx = self._make_context()

        with patch("bot._do_retry_scoring", new=AsyncMock(return_value={"total": 5, "sent": 2})):
            await retry_scoring_job(ctx)

        kwargs = ctx.bot.send_message.call_args[1]
        assert kwargs["chat_id"] == OWNER_CHAT_ID
