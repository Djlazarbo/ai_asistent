"""Tests for title_filter — junk titles, IT PM gate, per-user keyword match."""
from title_filter import (
    is_it_pm_title,
    is_junk_title,
    is_search_keyword,
    vacancy_matches_user,
)

ADMIN_KWS = [
    "IT project manager",
    "технический менеджер проектов",
    "руководитель IT проектов",
    "менеджер IT проектов",
    "delivery manager",
    "technical program manager",
    "Technical Project Manager",
    "Руководитель проектов",
    "Менеджер проектов",
]

NATALI_KWS = ["marketing"]
ETORUSLAN_KWS = ["Экономическая безопасность"]


class TestJunkTitle:
    def test_cleaning(self):
        assert is_junk_title("Менеджер по клининговому проекту") is True

    def test_construction(self):
        assert is_junk_title("Менеджер строительного проекта (открытие новых филиалов)") is True

    def test_school(self):
        assert is_junk_title("Проджект-менеджер в Онлайн-школу") is True

    def test_sales(self):
        assert is_junk_title("Руководитель отдела продаж (РОП) b2b в IT Компанию") is True

    def test_account(self):
        assert is_junk_title("Аккаунт-менеджер digital-проектов") is True

    def test_food(self):
        assert is_junk_title("Руководитель проектов (пищевые товарные группы)") is True

    def test_packaging(self):
        assert is_junk_title("Менеджер инновационных проектов по упаковке / разработчик упаковки") is True

    def test_security_not_universal_junk(self):
        # Keep in pool for users who search security; PM fan-out still skips it.
        assert is_junk_title("Инженер по информационной безопасности") is False

    def test_good_tpm_not_junk(self):
        assert is_junk_title("Technical Project Manager") is False


class TestItPmTitle:
    def test_tpm(self):
        assert is_it_pm_title("Technical Project Manager") is True

    def test_it_pm(self):
        assert is_it_pm_title("IT Project Manager") is True

    def test_senior_pm_en(self):
        assert is_it_pm_title("Senior Project Manager") is True

    def test_delivery(self):
        assert is_it_pm_title("Delivery Manager") is True

    def test_ru_it(self):
        assert is_it_pm_title("Руководитель IT проектов") is True

    def test_generic_ru_pm_without_it(self):
        assert is_it_pm_title("Руководитель проектов") is False

    def test_pm_plus_product_owner(self):
        assert is_it_pm_title("Product Owner / Project Manager (SaaS)") is True

    def test_team_lead(self):
        assert is_it_pm_title("Team Lead команды разработки") is False

    def test_product_owner_only(self):
        assert is_it_pm_title("Senior Продакт менеджер") is False


class TestSearchKeyword:
    def test_duty_dropped(self):
        assert is_search_keyword("Формирование и приоритизация backlog") is False
        assert is_search_keyword("Управление сроками, качеством и рисками") is False
        assert is_search_keyword("Работа со стейкхолдерами") is False

    def test_generic_dropped(self):
        assert is_search_keyword("аккредитованная") is False
        assert is_search_keyword("рукодитель") is False
        assert is_search_keyword("информационные технологии") is False

    def test_real_kept(self):
        assert is_search_keyword("IT project manager") is True
        assert is_search_keyword("marketing") is True
        assert is_search_keyword("Экономическая безопасность") is True


class TestVacancyMatchesUser:
    def test_admin_gets_tpm(self):
        assert vacancy_matches_user(ADMIN_KWS, "Technical Project Manager") is True

    def test_admin_gets_senior_pm(self):
        assert vacancy_matches_user(ADMIN_KWS, "Senior Project Manager") is True

    def test_admin_skips_cleaning(self):
        assert vacancy_matches_user(ADMIN_KWS, "Менеджер по клининговому проекту") is False

    def test_admin_skips_marketing(self):
        assert vacancy_matches_user(ADMIN_KWS, "Маркетинг менеджер") is False

    def test_admin_skips_team_lead(self):
        assert vacancy_matches_user(ADMIN_KWS, "Team Lead команды разработки") is False

    def test_admin_skips_econ_security(self):
        assert vacancy_matches_user(ADMIN_KWS, "Экономическая безопасность") is False

    def test_admin_generic_pm_needs_it_text(self):
        assert vacancy_matches_user(
            ADMIN_KWS,
            "Руководитель проектов",
            "Ведение строительных объектов и смет",
        ) is False
        assert vacancy_matches_user(
            ADMIN_KWS,
            "Руководитель проектов",
            "Внедрение IT-систем, управление разработкой, Jira, Scrum",
        ) is True

    def test_natali_gets_marketing_not_tpm(self):
        assert vacancy_matches_user(NATALI_KWS, "Marketing manager") is True
        assert vacancy_matches_user(NATALI_KWS, "Technical Project Manager") is False

    def test_etoruslan_security_not_pm(self):
        assert vacancy_matches_user(ETORUSLAN_KWS, "Экономическая безопасность") is True
        assert vacancy_matches_user(ETORUSLAN_KWS, "IT Project Manager") is False

    def test_empty_keywords(self):
        assert vacancy_matches_user([], "IT Project Manager") is False


class TestBuildPmHhQuery:
    def test_pm_boolean_or_and_not(self):
        from title_filter import build_pm_hh_query, user_is_pm_searcher

        assert user_is_pm_searcher(ADMIN_KWS) is True
        q = build_pm_hh_query(ADMIN_KWS)
        assert "OR" in q
        assert "NAME:" in q
        assert "NOT продажам" in q
        assert "NOT маркетинг" in q

    def test_non_pm_still_detectable(self):
        from title_filter import user_is_pm_searcher

        assert user_is_pm_searcher(NATALI_KWS) is False
