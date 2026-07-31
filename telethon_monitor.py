"""
Real-time Telegram channel monitor via Telethon.
On startup fetches the last `catchup_limit` messages from each channel (catchup),
then listens for new messages in real time.
"""
import asyncio
import concurrent.futures
import functools
import os
import socket
import sys

import requests
import yaml
from loguru import logger
from telethon import TelegramClient, events
from telethon.errors import ChannelPrivateError, UsernameNotOccupiedError
from telethon.sessions import SQLiteSession
from dotenv import load_dotenv

load_dotenv()

import db_postgres as db


class WALSession(SQLiteSession):
    """SQLiteSession with WAL journal mode and busy timeout to prevent locking errors."""
    def __init__(self, session_id=None):
        super().__init__(session_id)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.commit()


_lock_socket: socket.socket | None = None


def _acquire_single_instance_lock(port: int = 47892) -> bool:
    """Bind to a local port as a single-instance lock. Returns False if already running."""
    global _lock_socket
    _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _lock_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        _lock_socket.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False

_http_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="n8n-send")

with open("config.yaml", encoding="utf-8") as f:
    _config = yaml.safe_load(f)

API_ID = _config["telegram"]["api_id"]
API_HASH = _config["telegram"]["api_hash"]
N8N_WEBHOOK = _config["n8n"]["webhook_url"]
CATCHUP_LIMIT = _config["telegram"].get("catchup_limit", 50)


SESSION_FILE = "telethon_session"

client = TelegramClient(WALSession(SESSION_FILE), API_ID, API_HASH)

_active_channels: set[str] = set()


def _post_sync(url: str, payload: dict) -> int:
    """Blocking HTTP POST — runs in thread pool to avoid asyncio transport issues."""
    resp = requests.post(url, json=payload, timeout=15, proxies={})
    return resp.status_code


async def _send_to_n8n(payload: dict, retries: int = 3):
    loop = asyncio.get_event_loop()
    for attempt in range(1, retries + 1):
        try:
            status = await loop.run_in_executor(
                _http_pool, functools.partial(_post_sync, N8N_WEBHOOK, payload)
            )
            if status in (200, 201):
                return
            logger.warning(f"n8n returned {status} (attempt {attempt})")
        except Exception as e:
            logger.error(f"n8n send error ({type(e).__name__}, attempt {attempt}): {e}")
        if attempt < retries:
            await asyncio.sleep(3 * attempt)  # 3s, 6s backoff
    logger.error(f"n8n: giving up after {retries} attempts")


_SPAM_PATTERNS = (
    "тебя заблокировали",
    "тебе заблокували",
    "you have been blocked",
    "вас заблокировали",
    "⛔",
    "заблокирован",
    "спасибо за подписку",
    "subscribe to",
    "подпишитесь на",
    "forwarded from",
    "реклама",
    "рекламный пост",
)


async def _process_message(chat_username: str, chat_id: int, msg) -> bool:
    """Save and forward one message. Returns True if it was new."""
    text = msg.text or msg.message or ""
    if not text.strip():
        return False

    text_lower = text.lower()
    if any(p in text_lower for p in _SPAM_PATTERNS):
        logger.debug(f"Skipped spam message {msg.id} from {chat_username}")
        return False

    external_id = f"tg_{chat_id}_{msg.id}"
    if await db.vacancy_exists(external_id):
        return False

    url = f"https://t.me/{chat_username}/{msg.id}" if chat_username else ""
    profile = await db.get_active_profile() or {}

    payload = {
        "source": "telegram",
        "channel": f"@{chat_username}" if chat_username else str(chat_id),
        "message_text": text,
        "message_id": str(msg.id),
        "date": msg.date.isoformat() if msg.date else "",
        "url": url,
        "external_id": external_id,
        "active_profile": profile,
    }

    await db.insert_vacancy({
        "source": "telegram",
        "external_id": external_id,
        "channel": payload["channel"],
        "text": text,
        "url": url,
        "status": "pending",
    })

    await _send_to_n8n(payload)
    return True


async def _catchup_channel(username: str):
    """Fetch the last CATCHUP_LIMIT messages from a channel and forward new ones."""
    try:
        entity = await client.get_entity(username)
        chat_username = entity.username or ""
        chat_id = entity.id

        messages = await client.get_messages(entity, limit=CATCHUP_LIMIT)
        new_count = 0
        for msg in messages:
            if await _process_message(chat_username, chat_id, msg):
                new_count += 1
                await asyncio.sleep(2)  # не спамить n8n — Ollama занята ~30-60с

        logger.info(f"Catchup {username}: {new_count} new из {len(messages)} сообщений")
    except (ChannelPrivateError, UsernameNotOccupiedError) as e:
        logger.warning(f"Catchup недоступен {username}: {e}")
    except Exception as e:
        logger.error(f"Catchup ошибка {username}: {e}")


async def _subscribe_channel(username: str):
    if username in _active_channels:
        return
    try:
        await client.get_entity(username)
        _active_channels.add(username)
        logger.info(f"Subscribed to {username}")
    except (ChannelPrivateError, UsernameNotOccupiedError) as e:
        logger.warning(f"Cannot subscribe to {username}: {e}")
    except Exception as e:
        logger.error(f"Error subscribing to {username}: {e}")


async def refresh_channels():
    channels = await db.get_active_channels()
    for ch in channels:
        await _subscribe_channel(ch)


@client.on(events.NewMessage())
async def on_new_message(event):
    if not event.is_channel:
        return

    chat = await event.get_chat()
    username = f"@{chat.username}" if chat.username else str(chat.id)

    if username not in _active_channels:
        return

    if not await db.is_monitoring_active():
        return

    processed = await _process_message(chat.username or "", chat.id, event.message)
    if processed:
        logger.info(f"New message from {username} → forwarded")


async def _periodic_refresh(interval: int = 60):
    while True:
        await asyncio.sleep(interval)
        await refresh_channels()


async def main():
    if not _acquire_single_instance_lock():
        logger.error("Another telethon_monitor instance is already running. Exiting.")
        sys.exit(1)

    await db.init_db()
    await client.start()
    logger.info("Telethon monitor started")

    channels = await db.get_active_channels()

    # Сначала подписываемся
    for ch in channels:
        await _subscribe_channel(ch)

    # Потом догоняем историю
    logger.info(f"Catchup: последние {CATCHUP_LIMIT} сообщений из {len(channels)} каналов...")
    for ch in channels:
        await _catchup_channel(ch)
    logger.info("Catchup завершён. Слушаю новые сообщения.")

    asyncio.create_task(_periodic_refresh(60))

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
