"""
Shared pytest fixtures for Job Hunter Bot test suite.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))


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
        "key_skills": ["Управление проектами", "Scrum", "Roadmap"],
        "tech_stack": ["Jira", "Confluence", "Docker"],
        "domains": ["fintech", "mediatech"],
        "key_achievements": [
            "Запустил 10 проектов на время и в рамках бюджета",
            "Снизил time-to-market на 25% за счёт CI/CD",
            "Масштабировал команду с 5 до 20 человек",
        ],
        "methodologies": ["Scrum", "Kanban"],
        "team_size": "20",
        "budget": "50M RUB",
        "languages": ["Русский — родной", "English — B2"],
        "summary": "Опытный TPM с 7 годами опыта в fintech и mediatech.",
    }


@pytest.fixture
def sample_resume_text():
    return (
        "Иванов Иван Иванович\n"
        "TPM / Project Manager\n\n"
        "Опыт работы:\n\n"
        "ООО «Финтех Компания» (2020-2024)\n"
        "Technical Program Manager\n"
        "- Управлял 15 проектами одновременно\n"
        "- Снизил time-to-market на 30%\n\n"
        "ООО «Медиа Платформа» (2017-2020)\n"
        "Project Manager\n"
        "- Запустил стриминговый сервис с нуля\n"
        "- Команда 20 человек, бюджет 100M RUB\n"
    )
