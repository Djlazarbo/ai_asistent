"""Build / release identity for Job Hunter."""
from __future__ import annotations

import os
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def get_version() -> str:
    env = (os.getenv("APP_VERSION") or "").strip()
    if env:
        return env
    path = _ROOT / "VERSION"
    if path.exists():
        return path.read_text(encoding="utf-8").splitlines()[0].strip() or "dev"
    return "dev"


def get_git_sha() -> str:
    return (os.getenv("APP_GIT_SHA") or "").strip()


def changelog_headline(version: str | None = None) -> tuple[str, str]:
    """Return (date, one-line title) for the given or current version."""
    ver = version or get_version()
    path = _ROOT / "CHANGELOG.md"
    if not path.exists():
        return "", ""
    header = re.compile(
        rf"^## \[{re.escape(ver)}\](?:\s*[—\-]\s*(\d{{4}}-\d{{2}}-\d{{2}}))?\s*$"
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        m = header.match(line)
        if not m:
            continue
        date = (m.group(1) or "").strip()
        title = ""
        for nxt in lines[i + 1 : i + 8]:
            s = nxt.strip()
            if s.startswith(">"):
                title = s.lstrip("> ").strip()
                break
            if s.startswith("## "):
                break
        return date, title
    return "", ""


def version_label() -> str:
    ver = get_version()
    sha = get_git_sha()
    return f"{ver} ({sha})" if sha else ver


def version_message() -> str:
    ver = get_version()
    date, title = changelog_headline(ver)
    sha = get_git_sha()
    lines = [f"Job Hunter  v{ver}"]
    if date or title:
        bit = " — ".join(x for x in (date, title) if x)
        lines.append(bit)
    if sha:
        lines.append(f"git {sha}")
    return "\n".join(lines)
