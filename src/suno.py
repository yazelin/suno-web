"""Suno 頁面流程：寫入走 UI、讀取走網路側錄"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from . import selectors
from .browser import BrowserManager
from .config import Settings
from .jobs import Clip, GenerationError, Job
from .tagging import tag_mp3

TERMINAL_STATUSES = {"complete", "error"}
_MAX_TRACKED_CLIPS = 500
# 判斷 clip 是不是這次 job 才產生的容錯窗口（秒）：我方送出 Create 的時間點
# 跟 Suno 伺服器記的 created_at 之間可能有幾秒鐘的時鐘差 / 網路延遲，抓寬鬆一點
# 避免真正剛生成的 clip 因為差個一兩秒被誤判成「舊的」而漏掉。
_CLOCK_SKEW_TOLERANCE = 15.0


@dataclass
class RawClip:
    id: str
    title: str = ""
    status: str = ""
    duration: float | None = None
    audio_url: str | None = None
    image_url: str | None = None
    created_at: str | None = None  # Suno 回傳的 ISO8601 UTC 字串（見 _wait_new_ids）
    lyrics: str = ""               # Suno 把歌詞放在 metadata.prompt
    tags: str = ""                 # 曲風，Suno 放在 metadata.tags


def parse_feed_payload(payload: Any) -> list[RawClip]:
    """在任意巢狀 JSON 裡撈出 clip 物件（有 id + status 字串的 dict）。
    容錯設計：Suno 改包裝層不影響，只要 clip 本體還有 id/status。"""
    found: dict[str, RawClip] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("id"), str) and isinstance(node.get("status"), str):
                meta = node.get("metadata")
                duration = node.get("duration")
                if duration is None and isinstance(meta, dict):
                    duration = meta.get("duration")
                found[node["id"]] = RawClip(
                    id=node["id"],
                    title=node.get("title") or "",
                    status=node["status"],
                    duration=float(duration) if duration else None,
                    audio_url=node.get("audio_url") or None,
                    image_url=node.get("image_url") or None,
                    created_at=node.get("created_at") or None,
                    lyrics=(meta.get("prompt") or "") if isinstance(meta, dict) else "",
                    tags=(meta.get("tags") or "") if isinstance(meta, dict) else "",
                )
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return list(found.values())


def extract_credits(payload: Any) -> int | None:
    """優先算免費方案真正剩餘量（monthly_limit - monthly_usage）：對 free tier
    帳號，CREDITS_JSON_KEYS 指到的 "credits" 欄位其實是另外購買的點數包餘額，
    恆為 0（見 selectors.py 對 CREDITS_URL_SUBSTRINGS 的偵察筆記），使用者真正
    在意的「這個月還能生成幾首」要用 monthly_limit 減 monthly_usage。這兩個
    欄位同一份 payload 就有，算起來零成本，優先用；算不出來（欄位缺漏、型別
    不對，例如付費方案可能沒有月配額概念）才退回原本字面 key 查找。"""
    if not isinstance(payload, dict):
        return None
    limit = payload.get(selectors.CREDITS_MONTHLY_LIMIT_KEY)
    usage = payload.get(selectors.CREDITS_MONTHLY_USAGE_KEY)
    if isinstance(limit, (int, float)) and isinstance(usage, (int, float)):
        return int(limit) - int(usage)
    for key in selectors.CREDITS_JSON_KEYS:
        val = payload.get(key)
        if isinstance(val, (int, float)):
            return int(val)
    return None


def _parse_epoch(created_at: str | None) -> float | None:
    """把 clip 的 created_at（ISO8601 UTC，例如 "2026-08-20T10:00:00.000Z"）
    轉成 epoch 秒數，供 _wait_new_ids 判斷「這筆是不是這次 job 才生出來的」。
    解析失敗（欄位缺漏、型別不對、或格式跟預期不同）回 None，呼叫端要能
    容錯——型別檢查特別列出來是因為這個值來自對方 JSON payload，型別標註
    是 str | None 只是我方期待，不是保證，防禦一下避免 .replace() 對非
    字串值炸 AttributeError。"""
    if not isinstance(created_at, str) or not created_at:
        return None
    try:
        return datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class SunoRunner:
    """把一個 job 跑完：填表單、按 Create、側錄 feed、下載音檔。"""

    def __init__(self, browser: BrowserManager, settings: Settings) -> None:
        self._browser = browser
        self._settings = settings
        self._clips: dict[str, RawClip] = {}
        self._sniffing = False
        self._sniffer_tasks: set = set()
        self.logged_in: bool | None = None
        self.last_credits: int | None = None
        # Suno 按下 Create 前會先問要不要驗證碼。required=true 代表會跳出
        # 要人點的 Turnstile，程式化點擊過不了，生成請求根本送不出去。
        # 實測這是綁帳號信任度的：有生成歷史的老帳號 false，新帳號 true。
        self.captcha_required: bool | None = None

    # ---- 側錄 ----

    def _install_sniffer(self, page: Page) -> None:
        """asyncio.create_task() 建立的 task 若沒有任何地方保留參照，事件迴圈
        只認一個弱參照，垃圾回收有機會在 task 跑到一半時就把它清掉、任務
        中止但不拋錯，等於側錄悄悄漏資料。用 self._sniffer_tasks 這個 set
        保留強參照，跑完（或出例外）用 done-callback 自行從 set 移除。"""
        if self._sniffing:
            return

        def _handle(response) -> None:
            task = asyncio.create_task(self._on_response(response))
            self._sniffer_tasks.add(task)
            task.add_done_callback(self._sniffer_tasks.discard)

        page.on("response", _handle)
        self._sniffing = True

    async def _on_response(self, response) -> None:
        url = response.url
        if "/api/c/check" in url:
            try:
                body = await response.json()
            except Exception:
                body = {}
            if isinstance(body, dict) and "required" in body:
                self.captcha_required = bool(body["required"])
        is_feed = any(s in url for s in selectors.FEED_URL_SUBSTRINGS)
        is_credits = any(s in url for s in selectors.CREDITS_URL_SUBSTRINGS)
        if not (is_feed or is_credits):
            return
        try:
            payload = await response.json()
        except Exception:
            return
        if is_feed:
            for rc in parse_feed_payload(payload):
                self._clips[rc.id] = rc
            while len(self._clips) > _MAX_TRACKED_CLIPS:
                self._clips.pop(next(iter(self._clips)))
        if is_credits:
            credits = extract_credits(payload)
            if credits is not None:
                self.last_credits = credits

    # ---- 主流程 ----

    async def run(self, job: Job) -> list[Clip]:
        page = self._browser.page
        self._install_sniffer(page)
        await self._ensure_on_create_page(page)
        before = set(self._clips.keys())
        await self._fill_form(page, job.params)
        # Task 11 實機踩到的坑：帳號的 feed（含歷史舊 clip）常常不是在頁面剛
        # load 完就側錄得到，而是要等到按下 Create 之後、Suno 前端才一次把
        # 「整份清單」重新打過來——這代表上面 before 這個快照常常是空的或不
        # 完整，若只靠「id 不在 before 裡」判斷新舊，帳號歷史上幾十首舊歌會
        # 被整批誤判成這次 job 剛生出來的，連帶被誤下載。改記錄「按 Create
        # 前」的時間點，_wait_new_ids 用 clip 的 created_at（Suno 伺服器時間）
        # 是否晚於這個時間點來判斷才是這次 job 真正生出來的，不再單靠
        # before 集合。
        submit_time = time.time()
        await self._click_create(page)
        new_ids = await self._wait_new_ids(before, submit_time)
        raws = await self._wait_terminal(page, new_ids)
        return await self._download_all(job.id, raws)

    async def _ensure_on_create_page(self, page: Page) -> None:
        """每個 job 開始一定重新 goto 一次：拿到全新表單（Simple 分頁、純音樂
        關閉、欄位皆空的預設狀態），同時清掉上一個 job 殘留的表單狀態。
        Controller ruling 1：刻意蓋掉 brief 原本「if not page.url.startswith(...)
        才導覽」的條件式邏輯，因為那樣同一個 page 物件在跑第二個 job 時可能
        還停在第一個 job 填到一半、甚至已按過 Create 的頁面上，狀態不乾淨。"""
        await page.goto(self._settings.suno_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        if await page.locator(selectors.LOGGED_OUT_MARKER).count() > 0:
            self.logged_in = False
            raise GenerationError("not_logged_in", "Suno 未登入，請先跑 suno-web login")
        self.logged_in = True

    async def _fill_form(self, page: Page, params: dict) -> None:
        try:
            if params["mode"] == "custom":
                await page.locator(selectors.CUSTOM_TAB).first.click()
                # Controller ruling 2（custom+instrumental 分支）+ Task 10
                # 實機追加發現：這裡的「Lyrics mode」radiogroup 選到哪個選項
                # 會被 Suno 存成帳號的表單草稿，不像 Simple 模式的
                # INSTRUMENTAL_TOGGLE 那樣每次全新頁面必重置——上一個 job（或
                # 上一次人工操作）如果留在 Instrumental，這次全新 goto 的頁面
                # 照樣是 Instrumental，此時 LYRICS_TEXTAREA 甚至整個不在 DOM
                # 上（不是隱藏，是不存在），要嘛填不進歌詞、要嘛誤生成成純
                # 音樂。所以無論 want 是 True 還是 False 都要明確選一次目標
                # 選項，且必須排在填 LYRICS_TEXTAREA 之前。詳見 selectors.py
                # 對 LYRICS_MODE_INSTRUMENTAL 的說明。
                await self._set_lyrics_mode(page, bool(params.get("instrumental")))
                if params["lyrics"]:
                    await page.locator(selectors.LYRICS_TEXTAREA).first.fill(params["lyrics"])
                if params["style"]:
                    await page.locator(selectors.STYLES_INPUT).first.fill(params["style"])
                if params["title"]:
                    # Controller ruling 3：TITLE_INPUT 在 DOM 上有兩份（響應式
                    # 版面各一份），.first 是隱藏的那份、.last 才可見可填——
                    # 見 selectors.py 對 TITLE_INPUT 的重驗筆記。實測不需要
                    # 展開任何「More Options」收合區塊，Task 9 那個假設是誤判。
                    await page.locator(selectors.TITLE_INPUT).last.fill(params["title"])
            else:
                await page.locator(selectors.SIMPLE_TAB).first.click()
                await page.locator(selectors.PROMPT_TEXTAREA).first.fill(params["prompt"])
                if params.get("instrumental"):
                    # Controller ruling 2：新分頁預設一定是關閉純音樂，且這顆
                    # 按鈕讀不到 aria-checked/aria-pressed/data-state 任何狀態
                    # 屬性（見 selectors.py），want=True 時直接點一次即可，
                    # 不用先讀狀態比對（brief 原本那段邏輯永遠讀到 None，
                    # 等於永遠不會真的點擊）。Task 10 實機驗證過：即使上一個
                    # job 留著這顆開著沒關，下一次全新頁面還是會重置回關閉，
                    # 跟 Advanced 分頁的 Lyrics mode 不同，所以這裡維持「只在
                    # want=True 時點」不用額外處理 want=False 的情況。
                    await page.locator(selectors.INSTRUMENTAL_TOGGLE).first.click()
        except GenerationError:
            raise
        except Exception as e:
            raise GenerationError("submit_failed",
                                  f"表單操作失敗（selector 可能過期）: {e}") from e

    async def _set_lyrics_mode(self, page: Page, want_instrumental: bool) -> None:
        """明確把 Advanced/Custom 模式的「Lyrics mode」radiogroup 切到目標選項
        （Instrumental 或 Write），不管目前（可能是上個 job 或人工操作留下的）
        狀態是什麼。這個 radiogroup 有正常 aria-checked，先讀狀態、已經對就
        跳過不點。實際點擊用 JS el.click() 直接觸發 DOM click 事件、不做座標
        命中測試——這顆按鈕在 production 預設 1280x720 viewport 下常被浮動的
        側欄 resize handle 蓋住，Playwright 座標式 click()（含 force=True）
        實測會被攔截、或點到蓋住的元素而沒有真正選取。已實機驗證兩個方向
        （選取 Instrumental / 切回 Write）都能正確反映在 aria-checked。"""
        target_selector = (selectors.LYRICS_MODE_INSTRUMENTAL if want_instrumental
                           else selectors.LYRICS_MODE_WRITE)
        radio = page.locator(target_selector).first
        await radio.scroll_into_view_if_needed()
        if await radio.get_attribute("aria-checked") == "true":
            return
        handle = await radio.element_handle()
        await page.evaluate("el => el.click()", handle)
        await page.wait_for_timeout(300)
        if await radio.get_attribute("aria-checked") != "true":
            label = "Instrumental" if want_instrumental else "Write"
            raise GenerationError(
                "submit_failed",
                f"切換 Lyrics mode 到 {label} 失敗（radiogroup 可能改版）",
            )

    async def _click_create(self, page: Page) -> None:
        try:
            await page.locator(selectors.CREATE_BUTTON).first.click()
        except Exception as e:
            raise GenerationError("submit_failed", f"按不到 Create: {e}") from e

    def _is_freshly_created(self, clip_id: str, before: set[str],
                            submit_time: float) -> bool:
        """判斷一個目前追蹤到的 clip 是不是這次 job 才生出來的。優先看
        created_at（伺服器時間，不受「before 快照抓得早不早」影響，見 run()
        說明）；created_at 缺漏或解析失敗（理論上不該發生，但欄位畢竟來自
        對方，防禦一下）才退回舊邏輯：不在 before 快照裡就當新的。"""
        rc = self._clips.get(clip_id)
        if rc is None:
            return False
        epoch = _parse_epoch(rc.created_at)
        if epoch is not None:
            return epoch >= submit_time - _CLOCK_SKEW_TOLERANCE
        return clip_id not in before

    async def _wait_new_ids(self, before: set[str], submit_time: float,
                            timeout: float = 90.0) -> set[str]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            new = {cid for cid in self._clips
                   if self._is_freshly_created(cid, before, submit_time)}
            if new:
                await asyncio.sleep(3)  # 同單 clip 幾乎同時出現，多等一拍收齊
                return {cid for cid in self._clips
                        if self._is_freshly_created(cid, before, submit_time)}
            await asyncio.sleep(0.5)
        if self.captcha_required:
            raise GenerationError(
                "captcha_required",
                "Suno 對這個帳號要求 Cloudflare 驗證碼（/api/c/check 回 "
                "required:true），程式點不過那道勾選框，生成送不出去。"
                "實測這綁帳號信任度：先用真人開的瀏覽器手動生一兩單，"
                "之後通常就會變成不要求。")
        raise GenerationError("submit_failed", "按了 Create 但 feed 沒出現新 clip")

    async def _wait_terminal(self, page: Page, ids: set[str],
                             refresh_interval: float = 20.0) -> list[RawClip]:
        # 整體 timeout 由 JobQueue 的 asyncio.wait_for 控，這裡只管輪詢。
        #
        # Task 11 實機踩到的坑：剛按下 Create 後，Suno 前端通常只主動打
        # 一兩次 /api/feed/v3（一次帶出帳號歷史、一次帶出新 clip 剛進
        # streaming 狀態），之後就不再重打這隻 API 了——真正的
        # streaming -> complete 轉換，前端另外走什麼即時管道（很可能是
        # WebSocket / SSE）我們的 response sniffer 攔不到（Playwright 的
        # page.on("response") 只看得到 HTTP 回應）。純被動側錄會導致這裡
        # 永遠等不到終態，只能被外層 JobQueue 的 600s job timeout 強制中止
        # ——即使歌曲其實已經在 Suno 那邊生成完成。實測（2026-08-20）：兩個
        # 新 clip 卡在 streaming 超過 560 秒都沒等到任何新的 feed 回應，
        # 但用另一支腳本重新導覽頁面後立刻讀到兩者皆已 complete。修法：
        # 每隔 refresh_interval 秒主動 reload 一次頁面，強迫瀏覽器重新打
        # 一次 feed，才能真的觀察到狀態變化；reload 失敗（暫時性網路問題）
        # 不中止等待，下一輪再試。
        last_refresh = time.time()
        while True:
            raws = [self._clips[i] for i in ids if i in self._clips]
            if raws and all(r.status in TERMINAL_STATUSES for r in raws):
                return raws
            if time.time() - last_refresh >= refresh_interval:
                try:
                    await page.reload(wait_until="domcontentloaded")
                except Exception:
                    pass
                last_refresh = time.time()
            await asyncio.sleep(2)

    # ---- 下載 ----

    async def _download_all(self, job_id: str, raws: list[RawClip]) -> list[Clip]:
        out_dir = Path(self._settings.generated_dir) / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        clips: list[Clip] = []
        for rc in raws:
            clip = Clip(id=rc.id, title=rc.title, status=rc.status,
                        duration=rc.duration, lyrics=rc.lyrics)
            if rc.status == "complete" and rc.audio_url:
                if await self._download(rc.audio_url, out_dir / f"{rc.id}.mp3"):
                    clip.downloadable = True
                    clip.filename = f"{rc.id}.mp3"
                if rc.image_url and await self._download(
                        rc.image_url, out_dir / f"{rc.id}.jpeg"):
                    clip.image_filename = f"{rc.id}.jpeg"
                # Suno 的 CDN 原檔什麼標籤都沒有，自己補上。下游（CLI、API、
                # 呼叫端下載）拿到的就都是自帶歌名／曲風／歌詞／封面的檔案。
                if clip.downloadable:
                    tag_mp3(
                        out_dir / f"{rc.id}.mp3",
                        title=rc.title or "Suno",
                        album=rc.tags,
                        lyrics=rc.lyrics,
                        cover_path=(out_dir / clip.image_filename)
                        if clip.image_filename else None,
                    )
            clips.append(clip)
        return clips

    async def _download(self, url: str, dest: Path) -> bool:
        try:
            resp = await self._browser.context.request.get(url)
            if resp.status != 200:
                return False
            body = await resp.body()
            if len(body) < 1024:  # VIP 擋下的常是短錯誤頁，不是音檔
                return False
            dest.write_bytes(body)
            return True
        except Exception:
            return False
