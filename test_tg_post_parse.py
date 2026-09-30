"""Tests for Telegram title extraction and digest filter."""
from tg_post_parse import extract_tg_title, is_digest_post


class TestDigest:
    def test_digest_keyword(self):
        assert is_digest_post("Подборка вакансий для QA на этой неделе\n1. ...\n2. ...") is True

    def test_normal_vacancy(self):
        text = (
            "Ищем Technical Project Manager\n\n"
            "Обязанности: вести релизы, Jira, Scrum.\n"
            "https://hh.ru/vacancy/1"
        )
        assert is_digest_post(text) is False

    def test_many_links(self):
        text = "Смотрите:\n" + "\n".join(f"https://t.me/x/{i}" for i in range(6))
        text = text + (" слово" * 80)
        assert is_digest_post(text) is True


class TestTitle:
    def test_bold(self):
        assert extract_tg_title("**IT Project Manager**\n\nУдалёнка") == "IT Project Manager"

    def test_first_line(self):
        assert extract_tg_title("Руководитель IT проектов\nЗарплата договорная") == (
            "Руководитель IT проектов"
        )

    def test_skip_link_first(self):
        t = extract_tg_title("https://t.me/x/1\nProject Manager в финтех")
        assert t == "Project Manager в финтех"

    def test_empty(self):
        assert extract_tg_title("") == ""
