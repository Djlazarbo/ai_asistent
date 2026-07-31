"""
Tests for db_postgres.py — requires live PostgreSQL on localhost:5433.

Run all:          pytest test_db_postgres.py -v -m integration
Run unit only:    pytest test_db_postgres.py -v -m "not integration"
"""
import asyncio
import json
import pytest
import db_postgres as db


# ── Helpers ────────────────────────────────────────────────────────────────────

_TEST_PREFIX = "pytest_pg_"


async def _cleanup(conn, prefix=_TEST_PREFIX):
    await conn.execute(
        "DELETE FROM vacancies WHERE external_id LIKE $1", f"{prefix}%"
    )


async def _get_conn():
    pool = await db._get_pool()
    return pool


# ── Unit tests (no DB) ────────────────────────────────────────────────────────

class TestParseSalaryValue:
    def test_none_input(self):
        assert db._parse_salary_value(None) is None

    def test_empty_string(self):
        assert db._parse_salary_value("") is None

    def test_plain_number_string(self):
        result = db._parse_salary_value("300 000")
        assert result == 300000

    def test_json_from_only(self):
        result = db._parse_salary_value(json.dumps({"from": 200000, "currency": "RUR"}))
        assert result == 200000

    def test_json_to_only(self):
        result = db._parse_salary_value(json.dumps({"to": 400000, "currency": "RUR"}))
        assert result == 400000

    def test_json_from_and_to_averages(self):
        result = db._parse_salary_value(json.dumps({"from": 200000, "to": 300000}))
        assert result == 250000

    def test_json_empty_dict(self):
        result = db._parse_salary_value(json.dumps({}))
        assert result is None

    def test_ruble_sign_text(self):
        result = db._parse_salary_value("от 250 000 ₽")
        assert result == 250000

    def test_text_with_k_notation_ignored(self):
        # "300k" has no space-separated digits that match — returns None
        result = db._parse_salary_value("300k")
        assert result is None

    def test_non_salary_small_number_ignored(self):
        result = db._parse_salary_value("50")  # too small for salary range
        assert result is None

    def test_nbsp_stripped(self):
        result = db._parse_salary_value("200\xa0000")
        assert result == 200000


# ── Integration tests ─────────────────────────────────────────────────────────

@pytest.mark.integration
class TestStats30Days:
    async def test_returns_all_required_keys(self):
        db._pool = None
        stats = await db.get_stats_30days()
        required = {"new", "skipped", "filtered", "pending", "applied",
                    "interview", "offer", "rejected", "viewed", "found", "total"}
        assert required.issubset(set(stats.keys()))

    async def test_found_equals_new_plus_skipped(self):
        db._pool = None
        stats = await db.get_stats_30days()
        assert stats["found"] == stats["new"] + stats["skipped"]

    async def test_total_excludes_applied_interview(self):
        db._pool = None
        stats = await db.get_stats_30days()
        expected_total = stats["new"] + stats["skipped"] + stats["filtered"] + stats["pending"]
        assert stats["total"] == expected_total

    async def test_pending_not_in_found(self):
        """Pending vacancies (not yet scored) must NOT count as 'found'."""
        db._pool = None
        pool = await db._get_pool()
        ext_id = f"{_TEST_PREFIX}pending_check"
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)
            await conn.execute(
                "INSERT INTO vacancies (external_id, source, status, created_at, updated_at) "
                "VALUES ($1, 'test', 'pending', NOW(), NOW())",
                ext_id,
            )
        try:
            stats_before = await db.get_stats_30days()
            # Insert a pending vacancy with no score
            stats_after = await db.get_stats_30days()
            # pending should not be added to "found"
            assert stats_after["found"] == stats_after["new"] + stats_after["skipped"]
        finally:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)

    async def test_filtered_not_in_found(self):
        db._pool = None
        stats = await db.get_stats_30days()
        assert stats["found"] == stats["new"] + stats["skipped"]
        # filtered should not appear in found
        assert stats.get("filtered", 0) >= 0  # just verify it's present


@pytest.mark.integration
class TestSaveVacancyScore:
    async def test_saves_score_and_status(self):
        db._pool = None
        pool = await db._get_pool()
        ext_id = f"{_TEST_PREFIX}score_001"

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)
            await conn.execute(
                "INSERT INTO vacancies (external_id, source, status, created_at, updated_at) "
                "VALUES ($1, 'test', 'pending', NOW(), NOW())",
                ext_id,
            )

        try:
            await db.save_vacancy_score(ext_id, 9, "TPM", "Corp", "300k", "{}", "new")
            row = await db.get_vacancy_by_external(ext_id)
            assert row["score"] == 9
            assert row["status"] == "new"
            assert row["title"] == "TPM"
            assert row["company"] == "Corp"
        finally:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)

    async def test_low_score_sets_skipped(self):
        db._pool = None
        pool = await db._get_pool()
        ext_id = f"{_TEST_PREFIX}score_002"

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)
            await conn.execute(
                "INSERT INTO vacancies (external_id, source, status, created_at, updated_at) "
                "VALUES ($1, 'test', 'pending', NOW(), NOW())",
                ext_id,
            )

        try:
            await db.save_vacancy_score(ext_id, 3, "Dev", "", "", "{}", "skipped")
            row = await db.get_vacancy_by_external(ext_id)
            assert row["score"] == 3
            assert row["status"] == "skipped"
        finally:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)

    async def test_analysis_json_stored(self):
        db._pool = None
        pool = await db._get_pool()
        ext_id = f"{_TEST_PREFIX}score_003"
        analysis = json.dumps({"score": 8, "pros": ["a"], "cons": [], "match_summary": "ok"})

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)
            await conn.execute(
                "INSERT INTO vacancies (external_id, source, status, created_at, updated_at) "
                "VALUES ($1, 'test', 'pending', NOW(), NOW())",
                ext_id,
            )

        try:
            await db.save_vacancy_score(ext_id, 8, "T", "", "", analysis, "new")
            row = await db.get_vacancy_by_external(ext_id)
            loaded = json.loads(row["analysis"])
            assert loaded["score"] == 8
            assert loaded["pros"] == ["a"]
        finally:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)


@pytest.mark.integration
class TestCleanupOldVacancies:
    async def test_removes_old_pending(self):
        db._pool = None
        pool = await db._get_pool()
        ext_id = f"{_TEST_PREFIX}cleanup_pending"

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)
            await conn.execute(
                "INSERT INTO vacancies (external_id, source, status, created_at, updated_at) "
                "VALUES ($1, 'test', 'pending', NOW() - INTERVAL '40 days', NOW() - INTERVAL '40 days')",
                ext_id,
            )

        deleted = await db.cleanup_old_vacancies(days=30)
        assert deleted >= 1
        row = await db.get_vacancy_by_external(ext_id)
        assert row is None

    async def test_removes_old_skipped(self):
        db._pool = None
        pool = await db._get_pool()
        ext_id = f"{_TEST_PREFIX}cleanup_skipped"

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)
            await conn.execute(
                "INSERT INTO vacancies (external_id, source, status, created_at, updated_at) "
                "VALUES ($1, 'test', 'skipped', NOW() - INTERVAL '40 days', NOW() - INTERVAL '40 days')",
                ext_id,
            )

        await db.cleanup_old_vacancies(days=30)
        row = await db.get_vacancy_by_external(ext_id)
        assert row is None

    async def test_preserves_applied_vacancies(self):
        db._pool = None
        pool = await db._get_pool()
        ext_id = f"{_TEST_PREFIX}cleanup_applied"

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)
            await conn.execute(
                "INSERT INTO vacancies (external_id, source, status, created_at, updated_at) "
                "VALUES ($1, 'test', 'applied', NOW() - INTERVAL '40 days', NOW() - INTERVAL '40 days')",
                ext_id,
            )

        try:
            await db.cleanup_old_vacancies(days=30)
            row = await db.get_vacancy_by_external(ext_id)
            assert row is not None, "applied vacancy must not be deleted by cleanup"
        finally:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)

    async def test_preserves_recent_pending(self):
        db._pool = None
        pool = await db._get_pool()
        ext_id = f"{_TEST_PREFIX}cleanup_recent"

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)
            await conn.execute(
                "INSERT INTO vacancies (external_id, source, status, created_at, updated_at) "
                "VALUES ($1, 'test', 'pending', NOW() - INTERVAL '5 days', NOW())",
                ext_id,
            )

        try:
            await db.cleanup_old_vacancies(days=30)
            row = await db.get_vacancy_by_external(ext_id)
            assert row is not None, "recent pending must not be deleted"
        finally:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)


@pytest.mark.integration
class TestGetStats30DaysAccuracy:
    """Insert known vacancies and verify stat counts are accurate."""

    async def test_counts_new_vacancies(self):
        db._pool = None
        pool = await db._get_pool()
        ids = [f"{_TEST_PREFIX}stat_new_{i}" for i in range(3)]

        async with pool.acquire() as conn:
            for ext_id in ids:
                await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)
                await conn.execute(
                    "INSERT INTO vacancies (external_id, source, status, created_at, updated_at) "
                    "VALUES ($1, 'test', 'new', NOW(), NOW())",
                    ext_id,
                )

        try:
            stats = await db.get_stats_30days()
            assert stats["new"] >= 3
        finally:
            async with pool.acquire() as conn:
                for ext_id in ids:
                    await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)

    async def test_counts_pending_separately_from_found(self):
        db._pool = None
        pool = await db._get_pool()
        ext_id = f"{_TEST_PREFIX}stat_pending_isolation"

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)
            await conn.execute(
                "INSERT INTO vacancies (external_id, source, status, created_at, updated_at) "
                "VALUES ($1, 'test', 'pending', NOW(), NOW())",
                ext_id,
            )

        try:
            stats = await db.get_stats_30days()
            assert stats["found"] == stats["new"] + stats["skipped"]
            assert stats["pending"] >= 1
        finally:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM vacancies WHERE external_id=$1", ext_id)
