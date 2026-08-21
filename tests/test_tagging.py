"""ID3 標籤 — 讓下載下來的 mp3 自帶歌名、演出者、曲風、歌詞與封面

Suno 的 CDN 原檔什麼都沒有（只有一個 comment 與 encoder），但網站下載鈕給的
是加工過的版本。我們自己組，資料在下載當下全部都在手上。
"""

from pathlib import Path

import pytest

from src.tagging import tag_mp3


@pytest.fixture
def mp3(tmp_path):
    p = tmp_path / "song.mp3"
    p.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 2048)   # 假的 frame header，夠 mutagen 寫標籤
    return p


def _read(path):
    from mutagen.id3 import ID3
    return ID3(str(path))


class TestTagMp3:
    def test_writes_title_artist_album(self, mp3):
        assert tag_mp3(mp3, title="貓貓進行曲", album="cute quirky march")
        t = _read(mp3)
        assert t["TIT2"].text[0] == "貓貓進行曲"
        assert t["TPE1"].text[0] == "Suno"
        assert t["TALB"].text[0] == "cute quirky march"

    def test_writes_lyrics_into_uslt(self, mp3):
        """USLT 而不是 SYLT：glitch-music 的 id3.js 明說不處理 SYLT，但讀 USLT"""
        tag_mp3(mp3, title="x", lyrics="[Verse]\n第一句")
        t = _read(mp3)
        uslt = [f for k, f in t.items() if k.startswith("USLT")]
        assert uslt and "第一句" in uslt[0].text

    def test_embeds_cover(self, mp3, tmp_path):
        cover = tmp_path / "c.jpeg"
        cover.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
        tag_mp3(mp3, title="x", cover_path=cover)
        t = _read(mp3)
        apic = [f for k, f in t.items() if k.startswith("APIC")]
        assert apic and apic[0].mime == "image/jpeg"
        assert apic[0].type == 3          # front cover
        assert apic[0].data == b"\xff\xd8\xff\xe0fakejpeg"

    def test_missing_cover_is_not_an_error(self, mp3, tmp_path):
        assert tag_mp3(mp3, title="x", cover_path=tmp_path / "nope.jpeg")
        assert _read(mp3)["TIT2"].text[0] == "x"

    def test_empty_fields_are_skipped(self, mp3):
        """沒有曲風就不要寫一個空的 TALB 進去"""
        tag_mp3(mp3, title="x", album="", lyrics="")
        t = _read(mp3)
        assert "TALB" not in t
        assert not [k for k in t if k.startswith("USLT")]

    def test_missing_file_returns_false(self, tmp_path):
        assert tag_mp3(tmp_path / "nope.mp3", title="x") is False


class TestSignature:
    def test_artist_comes_from_config(self, mp3, monkeypatch):
        from src import config
        monkeypatch.setattr(config.settings, "tag_artist", "林亞澤")
        tag_mp3(mp3, title="x")
        assert _read(mp3)["TPE1"].text[0] == "林亞澤"

    def test_explicit_artist_wins(self, mp3, monkeypatch):
        from src import config
        monkeypatch.setattr(config.settings, "tag_artist", "設定檔的值")
        tag_mp3(mp3, title="x", artist="呼叫端指定的")
        assert _read(mp3)["TPE1"].text[0] == "呼叫端指定的"

    def test_tool_name_goes_in_tenc_not_artist(self, mp3, monkeypatch):
        """工具名該待在 TENC（encoded by），不是演出者欄位"""
        from src import config
        monkeypatch.setattr(config.settings, "tag_artist", "林亞澤")
        monkeypatch.setattr(config.settings, "tag_encoder", "suno-web")
        tag_mp3(mp3, title="x")
        t = _read(mp3)
        assert t["TPE1"].text[0] == "林亞澤"
        assert t["TENC"].text[0] == "suno-web"
