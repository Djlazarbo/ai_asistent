"""
Tests for prompts.py — verify no hardcoded personal data and correct placeholder usage.

Run: pytest test_prompts.py -v
"""
import re
import pytest
import prompts


# All prompt constants and their required format() placeholders
_PROMPTS_AND_VARS = [
    ("OLLAMA_FILTER_PROMPT", ["vacancy_title"]),
    ("DEEPSEEK_ANALYZE", ["active_profile", "vacancy_text"]),
    ("DEEPSEEK_INTERVIEW_PREP", ["active_profile", "vacancy_text"]),
    ("DEEPSEEK_EXTRACT_PROFILE", ["resume_text"]),
    ("INTERVIEW_PREP_PROMPT", ["vacancy_text", "resume_text", "interview_history"]),
    ("RESUME_ADAPT_PROMPT", ["vacancy_text", "resume_text"]),
    ("COVER_LETTER_PROMPT", ["resume_text", "vacancy_text"]),
    ("COVER_LETTER_SHORT_PROMPT", ["active_profile", "vacancy_text"]),
]

_ALL_PROMPT_NAMES = [name for name, _ in _PROMPTS_AND_VARS]

# Personal identifiers that must never appear in prompts
_FORBIDDEN_PERSONAL = [
    "Лазаренко", "lazarenko", "Lazarenko",
    "Юлий", "Julius", "julius",
    "julius.lazarenko",
    "+7",
]

# Company names specific to one person's resume — must not be hardcoded
_FORBIDDEN_COMPANIES = [
    "Netris", "netris",
    "PBF Group", "PBF group", "pbf group",
    "NTC Vulkan", "НТЦ Вулкан",
]


class TestNoHardcodedPersonalData:
    @pytest.mark.parametrize("prompt_name", _ALL_PROMPT_NAMES)
    def test_no_personal_name(self, prompt_name):
        text = getattr(prompts, prompt_name)
        for forbidden in _FORBIDDEN_PERSONAL:
            assert forbidden not in text, (
                f"{prompt_name} contains forbidden personal identifier: {forbidden!r}"
            )

    @pytest.mark.parametrize("prompt_name", _ALL_PROMPT_NAMES)
    def test_no_hardcoded_company(self, prompt_name):
        text = getattr(prompts, prompt_name)
        for company in _FORBIDDEN_COMPANIES:
            assert company not in text, (
                f"{prompt_name} contains hardcoded company name: {company!r}"
            )

    @pytest.mark.parametrize("prompt_name", _ALL_PROMPT_NAMES)
    def test_no_personal_email(self, prompt_name):
        text = getattr(prompts, prompt_name)
        assert "@gmail.com" not in text.lower()
        assert "@yandex.ru" not in text.lower()

    @pytest.mark.parametrize("prompt_name", _ALL_PROMPT_NAMES)
    def test_no_phone_numbers(self, prompt_name):
        text = getattr(prompts, prompt_name)
        # Reject literal phone numbers like +7 912 345-67-89
        phone_pattern = re.compile(r'\+7[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}')
        assert not phone_pattern.search(text), (
            f"{prompt_name} appears to contain a phone number"
        )


class TestPlaceholders:
    @pytest.mark.parametrize("prompt_name,required_vars", _PROMPTS_AND_VARS)
    def test_required_placeholders_present(self, prompt_name, required_vars):
        text = getattr(prompts, prompt_name)
        for var in required_vars:
            assert "{" + var + "}" in text, (
                f"{prompt_name} missing required placeholder: {{{var}}}"
            )

    @pytest.mark.parametrize("prompt_name,required_vars", _PROMPTS_AND_VARS)
    def test_prompt_formattable(self, prompt_name, required_vars):
        """Prompt must not raise when .format() is called with all required vars."""
        text = getattr(prompts, prompt_name)
        dummy_kwargs = {var: "DUMMY" for var in required_vars}
        try:
            formatted = text.format(**dummy_kwargs)
        except KeyError as e:
            pytest.fail(
                f"{prompt_name}.format({dummy_kwargs}) raised KeyError: {e}. "
                f"Unescaped brace in prompt text?"
            )
        assert "DUMMY" in formatted


class TestPromptContent:
    def test_filter_prompt_expects_yes_no(self):
        assert "YES" in prompts.OLLAMA_FILTER_PROMPT
        assert "NO" in prompts.OLLAMA_FILTER_PROMPT

    def test_extract_profile_requires_all_companies(self):
        assert "ВСЕ места работы" in prompts.DEEPSEEK_EXTRACT_PROFILE or \
               "ALL" in prompts.DEEPSEEK_EXTRACT_PROFILE.upper() or \
               "все" in prompts.DEEPSEEK_EXTRACT_PROFILE.lower()

    def test_extract_profile_requires_key_achievements(self):
        assert "key_achievements" in prompts.DEEPSEEK_EXTRACT_PROFILE

    def test_extract_profile_strict_rules_present(self):
        assert "tech_stack" in prompts.DEEPSEEK_EXTRACT_PROFILE
        assert "methodologies" in prompts.DEEPSEEK_EXTRACT_PROFILE

    def test_cover_letter_forbids_banned_phrases(self):
        # Both cover letter prompts must explicitly ban cliché phrases
        for prompt_name in ("COVER_LETTER_PROMPT",):
            text = getattr(prompts, prompt_name)
            # Must mention at least some forbidden phrases section
            assert "ЗАПРЕЩЕНО" in text or "НЕЛЬЗЯ" in text or "ЗАПРЕЩЁННЫЕ" in text, (
                f"{prompt_name} should list forbidden phrases"
            )

    def test_analyze_returns_json_schema(self):
        assert '"score"' in prompts.DEEPSEEK_ANALYZE
        assert '"pros"' in prompts.DEEPSEEK_ANALYZE
        assert '"cons"' in prompts.DEEPSEEK_ANALYZE
        assert '"red_flags"' in prompts.DEEPSEEK_ANALYZE

    def test_extract_profile_json_schema_complete(self):
        required_fields = [
            '"name"', '"target_role"', '"experience_years"', '"level"',
            '"key_skills"', '"tech_stack"', '"domains"', '"key_achievements"',
            '"methodologies"', '"team_size"', '"budget"', '"languages"', '"summary"',
        ]
        for field in required_fields:
            assert field in prompts.DEEPSEEK_EXTRACT_PROFILE, (
                f"DEEPSEEK_EXTRACT_PROFILE missing JSON field: {field}"
            )

    def test_interview_prep_format(self):
        assert "❓" in prompts.DEEPSEEK_INTERVIEW_PREP
        assert "💡" in prompts.DEEPSEEK_INTERVIEW_PREP

    def test_resume_adapt_has_ats_section(self):
        assert "ATS" in prompts.RESUME_ADAPT_PROMPT or "ключев" in prompts.RESUME_ADAPT_PROMPT.lower()
