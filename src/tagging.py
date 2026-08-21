"""把 ID3 標籤寫進下載下來的 mp3

Suno 的 CDN 原檔什麼都沒有 —— ffprobe 看只有一個 `comment=made with suno`
與 encoder，沒有標題、沒有演出者、沒有封面。網站那顆下載鈕給的是另一份加工過
的檔案，但 feed 裡沒有宣告它的網址。

既然下載當下這些資料全部都在手上（歌名、曲風、歌詞、封面），自己寫進去比去
逆向那個未公開端點穩：不依賴 Suno 的私有介面，他們改版也不會斷。

歌詞用 **USLT** 不用 SYLT：glitch-music 的 `js/id3.js` 明說不處理 SYLT，但讀
USLT，而且會把讀到的內容直接當 `lrc` 丟給 `parseLrc` —— 所以之後真要做動態
歌詞，把 LRC 格式的文字放進同一個幀就會自動生效。
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def tag_mp3(path: Path, *, title: str, artist: str = "Suno", album: str = "",
            lyrics: str = "", cover_path: Path | None = None) -> bool:
    """就地寫入 ID3 標籤，回報有沒有寫成功

    寫失敗只記一行 log。音檔本身是好的，標籤沒寫進去不該讓整單失敗。
    """
    if not path.is_file():
        log.warning("要加標籤的檔案不存在：%s", path)
        return False

    try:
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TIT2, TPE1, USLT
    except ImportError:
        log.warning("mutagen 未安裝，mp3 不會帶標籤")
        return False

    try:
        try:
            tags = ID3(str(path))
        except ID3NoHeaderError:
            tags = ID3()

        if title:
            tags.add(TIT2(encoding=3, text=title))
        if artist:
            tags.add(TPE1(encoding=3, text=artist))
        if album:
            tags.add(TALB(encoding=3, text=album))
        if lyrics:
            tags.add(USLT(encoding=3, lang="und", desc="", text=lyrics))
        if cover_path and Path(cover_path).is_file():
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3,
                          desc="Cover", data=Path(cover_path).read_bytes()))

        # v2.3 相容性最好；播放器對 v2.4 的支援參差不齊
        tags.save(str(path), v2_version=3)
        return True
    except Exception as e:
        log.warning("寫 ID3 標籤失敗 %s：%s", path.name, e)
        return False
