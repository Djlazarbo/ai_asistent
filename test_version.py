"""Tests for version.py release identity."""
import os
from pathlib import Path

import version as v


def test_version_file():
    assert v.get_version() == Path("VERSION").read_text(encoding="utf-8").strip()


def test_env_overrides_file(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "9.9.9")
    assert v.get_version() == "9.9.9"


def test_changelog_headline():
    date, title = v.changelog_headline("1.2.0")
    assert date == "2026-09-30"
    assert "follow-up" in title.lower() or "поиск" in title.lower()


def test_version_message_contains_version():
    msg = v.version_message()
    assert "1.2.0" in msg
    assert "Job Hunter" in msg
