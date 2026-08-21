import pytest
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from src.suno import (
    RawClip,
    SunoRunner,
    _CLOCK_SKEW_TOLERANCE,
    _parse_epoch,
    extract_credits,
    parse_feed_payload,
)

FIXTURE = Path(__file__).parent / "fixtures" / "feed_sample.json"


def test_parse_synthetic_nested_payload():
    payload = {
        "wrapper": {
            "clips": [
                {"id": "clip-1", "status": "streaming", "title": "A",
                 "audio_url": "https://example.com/a.mp3",
                 "metadata": {"duration": None}},
                {"id": "clip-2", "status": "complete", "title": "B",
                 "audio_url": "https://example.com/b.mp3",
                 "image_url": "https://example.com/b.jpeg",
                 "metadata": {"duration": 121.5}},
            ]
        }
    }
    clips = {c.id: c for c in parse_feed_payload(payload)}
    assert clips["clip-1"].status == "streaming"
    assert clips["clip-2"].duration == 121.5
    assert clips["clip-2"].image_url == "https://example.com/b.jpeg"


def test_parse_top_level_list():
    payload = [{"id": "x", "status": "complete", "audio_url": "u"}]
    assert parse_feed_payload(payload)[0].id == "x"


def test_parse_ignores_non_clip_dicts():
    payload = {"id": 123, "status": {"nested": True}, "other": "junk"}
    assert parse_feed_payload(payload) == []


def test_parse_real_fixture():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    clips = parse_feed_payload(payload)
    assert len(clips) >= 2
    assert any(c.status == "complete" for c in clips)
    assert all(c.id for c in clips)


def test_extract_credits_prefers_monthly_remaining():
    # free tier 帳號："credits" 恆為 0（另外購買的點數包），真正剩餘量要用
    # monthly_limit - monthly_usage 算，見 selectors.py 對 CREDITS_JSON_KEYS 的說明。
    payload = {"credits": 0, "monthly_usage": 70, "monthly_limit": 100}
    assert extract_credits(payload) == 30


def test_extract_credits_falls_back_to_literal_key_when_monthly_fields_missing():
    payload = {"credits": 42}
    assert extract_credits(payload) == 42


def test_extract_credits_non_dict_payload_returns_none():
    assert extract_credits(["not", "a", "dict"]) is None


def test_extract_credits_missing_all_keys_returns_none():
    assert extract_credits({"unrelated": 1}) is None


# ---- _parse_epoch（Task 11 修正：created_at 字串轉 epoch 秒數）----


def test_parse_epoch_valid_iso8601_with_z_suffix():
    epoch = _parse_epoch("2026-01-01T00:00:00.000Z")
    assert epoch == datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


def test_parse_epoch_none_returns_none():
    assert _parse_epoch(None) is None


def test_parse_epoch_empty_string_returns_none():
    assert _parse_epoch("") is None


def test_parse_epoch_malformed_string_returns_none():
    assert _parse_epoch("not-a-real-date") is None


def test_parse_epoch_non_string_truthy_value_returns_none():
    # created_at 來自對方 JSON payload，型別標註 str | None 只是我方期待，
    # 不是保證——這裡故意塞一個真值但非字串的 int，模擬對方回傳型別跟
    # 預期不同的情況，_parse_epoch 要能容錯而不是炸 AttributeError。
    assert _parse_epoch(1234567890) is None


# ---- _is_freshly_created（Task 11 修正：用 created_at 判斷 clip 新舊）----


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _runner_with_clip(clip_id: str, created_at: str | None) -> SunoRunner:
    runner = SunoRunner(None, None)  # browser/settings 這裡用不到，帶 None 即可
    runner._clips[clip_id] = RawClip(id=clip_id, status="complete", created_at=created_at)
    return runner


def test_is_freshly_created_true_when_created_at_at_or_after_submit_time():
    submit_time = time.time()
    runner = _runner_with_clip("new-1", created_at=_iso(submit_time + 2))
    assert runner._is_freshly_created("new-1", before=set(), submit_time=submit_time) is True


def test_is_freshly_created_true_within_clock_skew_tolerance():
    submit_time = time.time()
    # 比 submit_time 早，但還在容錯窗口內（見 _CLOCK_SKEW_TOLERANCE）
    runner = _runner_with_clip("edge-1", created_at=_iso(submit_time - _CLOCK_SKEW_TOLERANCE + 1))
    assert runner._is_freshly_created("edge-1", before=set(), submit_time=submit_time) is True


def test_is_freshly_created_false_when_created_at_older_than_submit_time():
    submit_time = time.time()
    runner = _runner_with_clip("old-1", created_at=_iso(submit_time - 3600))
    assert runner._is_freshly_created("old-1", before=set(), submit_time=submit_time) is False


def test_is_freshly_created_falls_back_to_before_set_when_created_at_missing():
    submit_time = time.time()
    runner = _runner_with_clip("no-created-at", created_at=None)
    assert runner._is_freshly_created("no-created-at", before=set(), submit_time=submit_time) is True
    assert runner._is_freshly_created(
        "no-created-at", before={"no-created-at"}, submit_time=submit_time) is False


def test_is_freshly_created_falls_back_to_before_set_when_created_at_unparseable():
    submit_time = time.time()
    runner = _runner_with_clip("bad-date", created_at="not-a-real-date")
    assert runner._is_freshly_created("bad-date", before=set(), submit_time=submit_time) is True
    assert runner._is_freshly_created(
        "bad-date", before={"bad-date"}, submit_time=submit_time) is False


def test_is_freshly_created_unknown_clip_id_returns_false():
    runner = SunoRunner(None, None)
    assert runner._is_freshly_created("missing", before=set(), submit_time=time.time()) is False


def test_captcha_required_turns_into_its_own_error_code():
    """被要求驗證碼時要講明白,不要含糊回「feed 沒出現新 clip」。"""
    import asyncio

    from src.jobs import GenerationError
    from src.suno import SunoRunner

    class FakeBrowser:
        page = None

    class FakeSettings:
        suno_url = "https://suno.com/create"

    runner = SunoRunner(FakeBrowser(), FakeSettings())
    runner.captcha_required = True

    with pytest.raises(GenerationError) as e:
        asyncio.run(runner._wait_new_ids(set(), 0.0, timeout=0.01))
    assert e.value.code == "captcha_required"

    runner.captcha_required = False
    with pytest.raises(GenerationError) as e2:
        asyncio.run(runner._wait_new_ids(set(), 0.0, timeout=0.01))
    assert e2.value.code == "submit_failed"


def test_feed_parsing_picks_up_lyrics_from_metadata_prompt():
    """Suno 把歌詞放在 clip 的 metadata.prompt，撈出來才能跟著歌一起回給使用者"""
    from src.suno import parse_feed_payload

    payload = {"clips": [{
        "id": "abc", "status": "complete", "title": "測試曲",
        "metadata": {"duration": 120.0, "tags": "warm folk",
                     "prompt": "[Verse]\n夜色很輕\n[Chorus]\n慢一點也沒關係"},
    }]}
    clips = parse_feed_payload(payload)
    assert clips[0].lyrics.startswith("[Verse]")
    assert "慢一點" in clips[0].lyrics


def test_feed_parsing_survives_missing_metadata_prompt():
    """純音樂或 Suno 沒給歌詞時是空字串，不是 None，也不該炸掉"""
    from src.suno import parse_feed_payload

    clips = parse_feed_payload({"clips": [{"id": "abc", "status": "complete"}]})
    assert clips[0].lyrics == ""
