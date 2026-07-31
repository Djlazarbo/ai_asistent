"""Single source of truth for AI agent dispatch.

Reads agents.yaml for defaults; runtime overrides come from
db_postgres.settings.agent_overrides (JSON).
"""
import asyncio
import os
from typing import Optional

import httpx
import yaml
from dotenv import load_dotenv
from loguru import logger

load_dotenv()


def _agents_config_path() -> str:
    explicit = os.getenv("AGENTS_CONFIG")
    if explicit:
        return explicit
    if os.getenv("APP_ENV", "stage").lower() == "prod":
        return "agents.prod.yaml"
    return "agents.yaml"


_AGENTS_PATH = _agents_config_path()
with open(_AGENTS_PATH, encoding="utf-8") as f:
    _cfg = yaml.safe_load(f)
logger.info("Loaded agent config from {}", _AGENTS_PATH)

PROVIDERS: dict = _cfg["providers"]
AGENTS: dict = _cfg["agents"]


def agent_roles() -> list[str]:
    return list(AGENTS.keys())


def provider_models(provider: str) -> list[str]:
    return list(PROVIDERS[provider].get("models", []))


def provider_names() -> list[str]:
    return list(PROVIDERS.keys())


async def get_agent_config(role: str) -> dict:
    """Effective config for role = YAML defaults merged with DB overrides."""
    if role not in AGENTS:
        raise KeyError(f"Unknown agent role: {role}")
    cfg = dict(AGENTS[role])
    import db_postgres as _db
    overrides = await _db.get_agent_overrides()
    if role in overrides:
        cfg.update(overrides[role])
    return cfg


async def list_agents_effective() -> list[dict]:
    """All agents with their currently-effective config (for menu display)."""
    out = []
    for role in AGENTS:
        eff = await get_agent_config(role)
        out.append({"role": role, **eff})
    return out


# ── Provider implementations ───────────────────────────────────────────────────

async def _call_deepseek(prompt: str, model: str, temperature: float) -> str:
    cfg = PROVIDERS["deepseek"]
    api_key = os.getenv(cfg["api_key_env"], "")
    if not api_key:
        raise RuntimeError(f"{cfg['api_key_env']} not set in environment / .env")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_err = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=cfg.get("timeout", 300)) as client:
                resp = await client.post(cfg["base_url"], json=payload, headers=headers)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    logger.warning(f"DeepSeek {resp.status_code} attempt {attempt+1}, retrying")
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            last_err = e
            if attempt < 2:
                await asyncio.sleep(3 * (attempt + 1))
    raise last_err


async def _call_ollama(prompt: str, model: str, temperature: float) -> str:
    cfg = PROVIDERS["ollama"]
    url = f"{cfg['base_url']}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature},
    }
    last_err = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=cfg.get("timeout", 300)) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 500 and attempt < 2:
                    logger.warning(f"Ollama 500 attempt {attempt+1}, retrying")
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.json().get("response", "").strip()
        except Exception as e:
            last_err = e
            if attempt < 2:
                await asyncio.sleep(3 * (attempt + 1))
    raise last_err


_DISPATCH = {"deepseek": _call_deepseek, "ollama": _call_ollama}


async def call_agent(role: str, prompt: str, temperature: Optional[float] = None) -> str:
    """Dispatch a prompt to the configured provider+model for `role`."""
    cfg = await get_agent_config(role)
    provider = cfg["provider"]
    model = cfg["model"]
    temp = temperature if temperature is not None else cfg.get("temperature", 0.3)
    fn = _DISPATCH.get(provider)
    if fn is None:
        raise ValueError(f"Unknown provider: {provider}")
    return await fn(prompt, model, temp)
