"""Tests for follow-up reminder dismiss/snooze logic."""
from datetime import datetime, timedelta

from followup_meta import (
    mark_dismissed,
    mark_reminded,
    mark_snoozed,
    parse_follow_up_days,
    parse_followup_meta,
    should_send_followup_reminder,
)


class TestFollowupMeta:
    def test_parse_empty(self):
        assert parse_followup_meta(None) == {}
        assert parse_followup_meta("") == {}
        assert parse_followup_meta("not-json") == {}

    def test_parse_valid(self):
        raw = '{"123": {"dismissed": true}}'
        assert parse_followup_meta(raw)["123"]["dismissed"] is True

    def test_dismissed_blocks(self):
        assert should_send_followup_reminder({"dismissed": True}) is False

    def test_snooze_future_blocks(self):
        future = (datetime.utcnow() + timedelta(days=3)).isoformat()
        assert should_send_followup_reminder({"snooze_until": future}) is False

    def test_snooze_past_allows(self):
        past = (datetime.utcnow() - timedelta(days=1)).isoformat()
        assert should_send_followup_reminder({"snooze_until": past}) is True

    def test_reminded_today_blocks(self):
        now = datetime.utcnow()
        assert should_send_followup_reminder({"last_reminded_at": now.isoformat()}, now=now) is False

    def test_reminded_yesterday_allows(self):
        now = datetime.utcnow()
        yday = (now - timedelta(days=1)).isoformat()
        assert should_send_followup_reminder({"last_reminded_at": yday}, now=now) is True

    def test_mark_dismissed(self):
        meta = mark_dismissed({}, "abc")
        assert meta["abc"]["dismissed"] is True
        assert should_send_followup_reminder(meta["abc"]) is False

    def test_mark_snoozed(self):
        now = datetime(2026, 9, 30, 12, 0, 0)
        meta = mark_snoozed({}, "abc", days=7, now=now)
        assert meta["abc"]["snooze_until"].startswith("2026-10-07")
        assert should_send_followup_reminder(meta["abc"], now=now) is False
        assert should_send_followup_reminder(meta["abc"], now=now + timedelta(days=8)) is True

    def test_mark_reminded(self):
        now = datetime(2026, 9, 30, 12, 0, 0)
        meta = mark_reminded({}, "abc", now=now)
        assert should_send_followup_reminder(meta["abc"], now=now) is False

    def test_follow_up_days(self):
        assert parse_follow_up_days("3") == 3
        assert parse_follow_up_days("99") == 3  # out of range → default
        assert parse_follow_up_days(None) == 3
        assert parse_follow_up_days("7", default=5) == 7
