"""Tests for companies_want / companies_block helpers."""
from score_fanout_opt import apply_company_want_boost, company_matches_list


class TestCompanyMatch:
    def test_block_substring(self):
        assert company_matches_list("ООО Авито", ["Авито"]) is True
        assert company_matches_list("Яндекс", ["Авито"]) is False

    def test_empty(self):
        assert company_matches_list("", ["Авито"]) is False
        assert company_matches_list("Авито", []) is False


class TestWantBoost:
    def test_boost_cap(self):
        assert apply_company_want_boost(9, boosted=True, amount=2) == 10
        assert apply_company_want_boost(6, boosted=True, amount=1) == 7
        assert apply_company_want_boost(6, boosted=False) == 6
        assert apply_company_want_boost(None, boosted=True) is None
