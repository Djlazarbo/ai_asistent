import asyncio
import asyncpg
import os
import sys
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

async def main():
    try:
        conn = await asyncpg.connect(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", 5433)),
            database=os.getenv("POSTGRES_DB", "jobhunter"),
            user=os.getenv("POSTGRES_USER", "jobhunter"),
            password=os.getenv("POSTGRES_PASSWORD"),
        )
        version = await conn.fetchval("SELECT version()")
        print(f"✅ PostgreSQL подключён: {version}")

        tables = await conn.fetch("""
            SELECT tablename FROM pg_tables
            WHERE schemaname='public'
        """)
        found = [t['tablename'] for t in tables]
        print(f"✅ Таблицы: {found}")

        expected = {"vacancies", "settings", "channels", "interview_history", "cover_examples"}
        missing = expected - set(found)
        if missing:
            print(f"⚠️  Отсутствуют таблицы: {missing}")
            print("   Запусти: python -c \"import asyncio, db_postgres; asyncio.run(db_postgres.init_db())\"")
        else:
            print("✅ Все 5 таблиц на месте")

        # Test write/read/delete
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('_check_test', 'ok') "
            "ON CONFLICT (key) DO UPDATE SET value='ok'"
        )
        val = await conn.fetchval("SELECT value FROM settings WHERE key='_check_test'")
        await conn.execute("DELETE FROM settings WHERE key='_check_test'")
        if val == 'ok':
            print("✅ Тест записи прошёл успешно")
        else:
            print("❌ Тест записи провалился")

        await conn.close()
    except Exception as e:
        print(f"❌ Ошибка: {e}")

asyncio.run(main())
