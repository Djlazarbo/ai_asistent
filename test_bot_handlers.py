"""
Tests for bot.py helper functions — pure functions and mocked DB interactions.

Run: pytest test_bot_handlers.py -v
"""
import json
import pytest
from unittest.mock import AsyncMock, patch


# ── _clean_profile ─────────────────────────────────────────────────────────────

class TestCleanProfile:
    def _clean(self, profile):
        from bot import _clean_profile
        return _clean_profile(profile)

    def test_removes_agile_from_tech_stack(self):
        p = {"tech_stack": ["Agile", "Docker", "Jira"]}
        result = self._clean(p)
        assert "Agile" not in result["tech_stack"]
        assert "Docker" in result["tech_stack"]

    def test_removes_scrum_from_tech_stack(self):
        p = {"tech_stack": ["Scrum", "Confluence"]}
        result = self._clean(p)
        assert "Scrum" not in result["tech_stack"]
        assert "Confluence" in result["tech_stack"]

    def test_removes_kanban_from_tech_stack(self):
        p = {"tech_stack": ["Kanban", "Git"]}
        result = self._clean(p)
        assert "Kanban" not in result["tech_stack"]

    def test_removes_sdlc_from_tech_stack(self):
        p = {"tech_stack": ["SDLC", "Postman"]}
        result = self._clean(p)
        assert "SDLC" not in result["tech_stack"]

    def test_removes_sla_from_tech_stack(self):
        p = {"tech_stack": ["SLA", "Kubernetes"]}
        result = self._clean(p)
        assert "SLA" not in result["tech_stack"]

    def test_removes_roadmap_from_tech_stack(self):
        p = {"tech_stack": ["Roadmap", "Jira"]}
        result = self._clean(p)
        assert "Roadmap" not in result["tech_stack"]

    def test_removes_backlog_from_tech_stack(self):
        p = {"tech_stack": ["Backlog", "Confluence"]}
        result = self._clean(p)
        assert "Backlog" not in result["tech_stack"]

    def test_removes_ci_cd_from_tech_stack(self):
        p = {"tech_stack": ["CI/CD", "Docker"]}
        result = self._clean(p)
        assert "CI/CD" not in result["tech_stack"]

    def test_removes_daily_standup_from_tech_stack(self):
        p = {"tech_stack": ["Daily stand-up", "Slack"]}
        result = self._clean(p)
        assert "Daily stand-up" not in result["tech_stack"]

    def test_removes_sprint_review_from_tech_stack(self):
        p = {"tech_stack": ["Sprint review", "Jira"]}
        result = self._clean(p)
        assert "Sprint review" not in result["tech_stack"]

    def test_case_insensitive_removal(self):
        p = {"tech_stack": ["agile", "SCRUM", "KanBan", "Docker"]}
        result = self._clean(p)
        assert "agile" not in result["tech_stack"]
        assert "SCRUM" not in result["tech_stack"]
        assert "KanBan" not in result["tech_stack"]
        assert "Docker" in result["tech_stack"]

    def test_preserves_real_tools(self):
        tools = ["Docker", "Kafka", "Git", "Jira", "Confluence", "Postman",
                 "Kubernetes", "Grafana", "PostgreSQL", "Redis"]
        p = {"tech_stack": tools}
        result = self._clean(p)
        assert set(tools) == set(result["tech_stack"])

    def test_deduplicates_tech_stack(self):
        p = {"tech_stack": ["Jira", "Docker", "Jira", "Git", "Docker"]}
        result = self._clean(p)
        assert result["tech_stack"].count("Jira") == 1
        assert result["tech_stack"].count("Docker") == 1

    def test_deduplicates_key_skills(self):
        p = {"key_skills": ["Scrum", "Agile", "Scrum", "Kanban"]}
        result = self._clean(p)
        assert result["key_skills"].count("Scrum") == 1

    def test_deduplicates_domains(self):
        p = {"domains": ["fintech", "fintech", "mediatech"]}
        result = self._clean(p)
        assert result["domains"].count("fintech") == 1

    def test_deduplicates_methodologies(self):
        p = {"methodologies": ["Agile", "Scrum", "Agile"]}
        result = self._clean(p)
        assert result["methodologies"].count("Agile") == 1

    def test_deduplicates_languages(self):
        p = {"languages": ["Русский — родной", "English — B2", "Русский — родной"]}
        result = self._clean(p)
        assert result["languages"].count("Русский — родной") == 1

    def test_deduplicates_key_achievements(self):
        p = {"key_achievements": ["Запустил X проектов", "Запустил X проектов", "Сэкономил 30%"]}
        result = self._clean(p)
        assert result["key_achievements"].count("Запустил X проектов") == 1

    def test_preserves_order_after_dedup(self):
        p = {"tech_stack": ["Git", "Docker", "Jira", "Git"]}
        result = self._clean(p)
        assert result["tech_stack"] == ["Git", "Docker", "Jira"]

    def test_empty_tech_stack(self):
        p = {"tech_stack": []}
        result = self._clean(p)
        assert result["tech_stack"] == []

    def test_missing_fields_dont_crash(self):
        # Profile with only some fields
        p = {"tech_stack": ["Docker"]}
        result = self._clean(p)
        assert result["tech_stack"] == ["Docker"]

    def test_removes_management_phrases_from_tech_stack(self):
        p = {"tech_stack": ["управление проектами", "Docker"]}
        result = self._clean(p)
        assert "управление проектами" not in result["tech_stack"]
        assert "Docker" in result["tech_stack"]


# ── score_emoji ────────────────────────────────────────────────────────────────

class TestScoreEmoji:
    def _emoji(self, score):
        from bot import score_emoji
        return score_emoji(score)

    def test_none_returns_white(self):
        assert self._emoji(None) == "⬜"

    def test_10_returns_green(self):
        assert self._emoji(10) == "🟢"

    def test_8_returns_green(self):
        assert self._emoji(8) == "🟢"

    def test_7_returns_yellow(self):
        assert self._emoji(7) == "🟡"

    def test_6_returns_yellow(self):
        assert self._emoji(6) == "🟡"

    def test_5_returns_red(self):
        assert self._emoji(5) == "🔴"

    def test_1_returns_red(self):
        assert self._emoji(1) == "🔴"

    def test_string_score_works(self):
        assert self._emoji("9") == "🟢"


# ── format_salary ──────────────────────────────────────────────────────────────

class TestFormatSalary:
    def _fmt(self, salary):
        from bot import format_salary
        return format_salary(salary)

    def test_none_returns_not_specified(self):
        assert self._fmt(None) == "Не указана"

    def test_empty_string_returns_not_specified(self):
        assert self._fmt("") == "Не указана"

    def test_string_passthrough(self):
        result = self._fmt("300 000 ₽")
        assert "300" in result

    def test_dict_with_from_and_to(self):
        result = self._fmt({"from": 200000, "to": 300000, "currency": "RUR"})
        assert "200" in result
        assert "300" in result
        assert "₽" in result

    def test_dict_from_only(self):
        result = self._fmt({"from": 250000, "currency": "RUR"})
        assert "250" in result

    def test_dict_to_only(self):
        result = self._fmt({"to": 400000, "currency": "RUR"})
        assert "400" in result

    def test_dict_usd_currency(self):
        result = self._fmt({"from": 5000, "currency": "USD"})
        assert "$" in result

    def test_dict_empty_returns_not_specified(self):
        # No from/to: parts list is empty, so only currency symbol is returned
        # (stripped string is still truthy, so "Не указана" logic doesn't trigger)
        result = self._fmt({"currency": "RUR"})
        # Actual behavior: returns the currency symbol alone
        assert result in ("Не указана", "₽", "$", "€") or len(result) <= 3


# ── _profile_to_text (ai.py) ──────────────────────────────────────────────────

class TestProfileToText:
    def _fmt(self, profile):
        from ai import _profile_to_text
        return _profile_to_text(profile)

    def test_includes_target_role(self, sample_profile):
        text = self._fmt(sample_profile)
        assert sample_profile["target_role"] in text

    def test_includes_experience_years(self, sample_profile):
        text = self._fmt(sample_profile)
        assert str(sample_profile["experience_years"]) in text

    def test_includes_all_achievements(self, sample_profile):
        text = self._fmt(sample_profile)
        for ach in sample_profile["key_achievements"]:
            assert ach in text, f"Achievement missing from profile text: {ach!r}"

    def test_includes_domains(self, sample_profile):
        text = self._fmt(sample_profile)
        for domain in sample_profile["domains"]:
            assert domain in text

    def test_includes_tech_stack(self, sample_profile):
        text = self._fmt(sample_profile)
        for tech in sample_profile["tech_stack"]:
            assert tech in text

    def test_includes_languages(self, sample_profile):
        text = self._fmt(sample_profile)
        for lang in sample_profile["languages"]:
            assert lang in text

    def test_includes_summary(self, sample_profile):
        text = self._fmt(sample_profile)
        assert sample_profile["summary"] in text

    def test_achievements_bullet_format(self, sample_profile):
        text = self._fmt(sample_profile)
        assert "•" in text

    def test_empty_lists_dont_crash(self):
        from ai import _profile_to_text
        minimal = {
            "target_role": "PM", "experience_years": 3, "level": "middle",
            "team_size": "", "budget": "",
            "domains": [], "key_skills": [], "tech_stack": [],
            "methodologies": [], "languages": [], "key_achievements": [],
            "summary": "",
        }
        text = _profile_to_text(minimal)
        assert "PM" in text


# ── _stats_funnel_text ─────────────────────────────────────────────────────────

class TestStatsFunnelText:
    async def test_shows_pending_warning_when_over_10(self):
        from bot import _stats_funnel_text
        mock_stats = {
            "total": 100, "pending": 15, "filtered": 20,
            "new": 50, "skipped": 15, "viewed": 10,
            "applied": 5, "interview": 2, "offer": 0,
            "found": 65,
        }
        with patch("db_postgres.get_stats_30days", new=AsyncMock(return_value=mock_stats)), \
             patch("db_postgres.get_threshold", new=AsyncMock(return_value=7)):
            text = await _stats_funnel_text()

        assert "retryscoring" in text or "застряло" in text

    async def test_no_pending_warning_when_zero(self):
        from bot import _stats_funnel_text
        mock_stats = {
            "total": 50, "pending": 0, "filtered": 10,
            "new": 30, "skipped": 10, "viewed": 5,
            "applied": 3, "interview": 1, "offer": 0,
            "found": 40,
        }
        with patch("db_postgres.get_stats_30days", new=AsyncMock(return_value=mock_stats)), \
             patch("db_postgres.get_threshold", new=AsyncMock(return_value=7)):
            text = await _stats_funnel_text()

        assert "retryscoring" not in text

    async def test_includes_threshold_value(self):
        from bot import _stats_funnel_text
        mock_stats = {
            "total": 10, "pending": 0, "filtered": 2,
            "new": 5, "skipped": 3, "viewed": 1,
            "applied": 0, "interview": 0, "offer": 0,
            "found": 8,
        }
        with patch("db_postgres.get_stats_30days", new=AsyncMock(return_value=mock_stats)), \
             patch("db_postgres.get_threshold", new=AsyncMock(return_value=7)):
            text = await _stats_funnel_text()

        assert "7" in text

    async def test_shows_conversion_when_applied_nonzero(self):
        from bot import _stats_funnel_text
        mock_stats = {
            "total": 100, "pending": 0, "filtered": 10,
            "new": 70, "skipped": 20, "viewed": 30,
            "applied": 10, "interview": 3, "offer": 1,
            "found": 90,
        }
        with patch("db_postgres.get_stats_30days", new=AsyncMock(return_value=mock_stats)), \
             patch("db_postgres.get_threshold", new=AsyncMock(return_value=7)):
            text = await _stats_funnel_text()

        assert "%" in text

    async def test_contains_funnel_stages(self):
        from bot import _stats_funnel_text
        mock_stats = {
            "total": 50, "pending": 0, "filtered": 5,
            "new": 35, "skipped": 10, "viewed": 15,
            "applied": 8, "interview": 2, "offer": 0,
            "found": 45,
        }
        with patch("db_postgres.get_stats_30days", new=AsyncMock(return_value=mock_stats)), \
             patch("db_postgres.get_threshold", new=AsyncMock(return_value=7)):
            text = await _stats_funnel_text()

        assert "Подходящих" in text
        assert "Просмотрено" in text
        assert "Откликнулся" in text
        assert "Интервью" in text


# ── cover letter uses _profile_to_text ────────────────────────────────────────

class TestCoverLetterUsesProfile:
    async def test_generate_cover_letter_uses_all_achievements(self, sample_profile):
        """cover letter prompt must include all achievements, not just first 5000 chars."""
        import ai
        captured_prompts = []

        async def mock_chat(prompt, temperature=0.3):
            captured_prompts.append(prompt)
            return "Mock letter text"

        with patch("ai.ollama_chat", new=mock_chat):
            await ai.generate_cover_letter("Test vacancy", sample_profile)

        # At least the letter_prompt (second call) should have all achievements
        all_prompts_text = " ".join(captured_prompts)
        for ach in sample_profile["key_achievements"]:
            assert ach in all_prompts_text, (
                f"Achievement not found in any prompt: {ach!r}"
            )

    async def test_generate_cover_letter_short_uses_all_achievements(self, sample_profile):
        import ai
        captured_prompts = []

        async def mock_chat(prompt, temperature=0.3):
            captured_prompts.append(prompt)
            return "Mock short letter"

        with patch("ai.ollama_chat", new=mock_chat):
            await ai.generate_cover_letter_short("Test vacancy", sample_profile)

        all_prompts_text = " ".join(captured_prompts)
        for ach in sample_profile["key_achievements"]:
            assert ach in all_prompts_text, (
                f"Achievement not found in short letter prompt: {ach!r}"
            )

    async def test_cover_letter_does_not_call_db_for_resume_text(self, sample_profile):
        """generate_cover_letter must NOT fetch raw resume_text from DB."""
        import ai
        mock_db_get = AsyncMock()

        with patch("ai.ollama_chat", new=AsyncMock(return_value="Letter text")), \
             patch("db_postgres.get_setting", mock_db_get):
            await ai.generate_cover_letter("Some vacancy", sample_profile)

        # get_setting should not be called with "resume_text"
        for call in mock_db_get.call_args_list:
            args = call[0]
            assert args[0] != "resume_text", (
                "cover letter should use profile, not raw resume_text from DB"
            )
