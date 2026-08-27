"""Job 資料模型、SQLite 儲存、佇列與 worker"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import threading
import time
import uuid
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


@dataclass
class Clip:
    id: str
    title: str = ""
    status: str = ""              # Suno 端狀態：complete / error / ...
    duration: float | None = None
    downloadable: bool = False
    filename: str | None = None            # 已落地音檔檔名（generated/<job_id>/ 下）
    image_filename: str | None = None
    lyrics: str = ""                       # Suno 實際唱的歌詞（來自 clip 的 metadata.prompt）

    def to_api(self, job_id: str) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id, "title": self.title, "status": self.status,
            "duration": self.duration, "downloadable": self.downloadable,
        }
        if self.filename:
            d["audio_url"] = f"/api/jobs/{job_id}/files/{self.filename}"
        if self.image_filename:
            d["image_url"] = f"/api/jobs/{job_id}/files/{self.image_filename}"
        if self.lyrics:
            d["lyrics"] = self.lyrics
        return d


@dataclass
class Job:
    id: str
    status: str = "queued"        # queued / generating / done / error
    params: dict = field(default_factory=dict)
    clips: list[Clip] = field(default_factory=list)
    error: str | None = None
    error_message: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None

    def to_api(self) -> dict[str, Any]:
        elapsed = None
        if self.started_at:
            elapsed = round((self.finished_at or time.time()) - self.started_at, 1)
        return {
            "job_id": self.id, "status": self.status,
            "clips": [c.to_api(self.id) for c in self.clips],
            "error": self.error, "error_message": self.error_message,
            "elapsed_seconds": elapsed,
        }


# 保留幾筆 job 記錄。管理台歷史頁只看 200 筆，留 1000 筆已經很寬鬆。
_KEEP_JOBS = 1000
# 一單要多少點（實測：一單 10 點、出 4 首）
CREDITS_PER_JOB = 10

# 換帳號重試已停用。
#
# 舊版對 `captcha_required` 換帳號，前提是「Suno 對這個帳號要求驗證碼」。
# 2026-08-27 實測推翻了那個前提：`/api/c/check` 對**所有人**都回
# required:true，真人開的、當下生得出歌的瀏覽器也一樣。所以那不是帳號層級
# 的問題，換帳號救不了 —— 08-22 那次「兩分鐘內 12 次失敗」就是一單被輪過
# 四個帳號、每個都撞同一道牆，把一次故障放大成四次。
#
# 真的遇到帳號層級的錯誤碼再把這裡打開，並在上面寫清楚憑什麼認定它是
# 帳號層級的。設成 None 代表任何錯誤都不換帳號。
_REDISPATCH_CODE: str | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    params TEXT NOT NULL,
    clips TEXT NOT NULL,
    error TEXT,
    error_message TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
)
"""


class JobStore:
    """SQLite job 記錄。服務重啟後查舊 job 不會 404。"""

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def create(self, params: dict) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], params=params, created_at=time.time())
        self.save(job)
        self.prune(_KEEP_JOBS)
        return job

    def prune(self, keep: int) -> int:
        """只留最新 keep 筆 job 記錄，回報刪了幾筆。

        每次建立 job 時順手跑一次（做法同 gemini-web 的 requests 表）：不裁切
        的話這張表會一直長，管理台的歷史頁反正也只看最近 200 筆。音檔另外由
        cleanup_expired 依保留天數處理，兩者互不影響。
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM jobs WHERE id NOT IN"
                " (SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)",
                (keep,))
            self._conn.commit()
            return cur.rowcount or 0

    def save(self, job: Job) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                (job.id, job.status,
                 json.dumps(job.params, ensure_ascii=False),
                 json.dumps([c.__dict__ for c in job.clips], ensure_ascii=False),
                 job.error, job.error_message,
                 job.created_at, job.started_at, job.finished_at),
            )
            self._conn.commit()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,status,params,clips,error,error_message,"
                "created_at,started_at,finished_at FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return Job(
            id=row[0], status=row[1], params=json.loads(row[2]),
            clips=[Clip(**c) for c in json.loads(row[3])],
            error=row[4], error_message=row[5],
            created_at=row[6], started_at=row[7], finished_at=row[8],
        )

    def fail_unfinished(self, message: str) -> int:
        """服務重啟後，把所有還沒跑到終態的 job 一律標記失敗。Queue 是純記憶體
        佇列，重啟後這些 job 永遠不會再被排到，不處理的話 client 會永遠輪詢
        不到終態。回傳受影響的筆數。"""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status='error', error='browser_error', "
                "error_message=?, finished_at=? "
                "WHERE status NOT IN ('done','error')",
                (message, now),
            )
            self._conn.commit()
            return cur.rowcount

    def list_recent(self, limit: int = 100) -> list[Job]:
        """給 admin History 頁用：最近的 job，新的在前。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,status,params,clips,error,error_message,"
                "created_at,started_at,finished_at FROM jobs"
                " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [
            Job(id=r[0], status=r[1], params=json.loads(r[2]),
                clips=[Clip(**c) for c in json.loads(r[3])],
                error=r[4], error_message=r[5],
                created_at=r[6], started_at=r[7], finished_at=r[8])
            for r in rows
        ]


class GenerationError(Exception):
    """runner 拋的可分類錯誤，code 會進 job.error"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message


class QueueFullError(Exception):
    pass


Runner = Callable[["Job"], Awaitable[list[Clip]]]


class JobQueue:
    """一個 worker 一條佇列，同一個 worker 內序列處理。

    每個 worker 綁一個 Suno 帳號。派工規則見 `_pick`（預設點數優先，輪流只
    在點數同分或還沒觀測到點數的帳號之間用）。輪到的那個正忙就順延給下一個
    閒著的，都在忙才排進最短的那條佇列。

    帳號被 Suno 舉了防機器人驗證碼時，這一單會自動改派給還沒試過的帳號，
    見 `_requeue_other`。

    同一個帳號不能同時跑兩單：一個 worker 只有一個瀏覽器分頁，而輪詢期間
    會定期 reload 它，兩單並行會互相把頁面導覽掉。
    """

    def __init__(self, store: JobStore, runner: Runner | list[Runner], *,
                 max_size: int, default_timeout: int,
                 generated_dir: str, retention_days: int,
                 credits_of: Callable[[int], int | None] | None = None,
                 dispatch_mode: str = "credits") -> None:
        self._store = store
        self._runners: list[Runner] = (list(runner) if isinstance(runner, list)
                                       else [runner])
        n = len(self._runners)
        # max_size 是整體上限，平均分給每條佇列（至少 1）
        per_queue = max(1, max_size // n)
        self._queues: list[asyncio.Queue[str]] = [
            asyncio.Queue(maxsize=per_queue) for _ in range(n)]
        self._busy = [False] * n
        self._rr = 0
        self._credits_of = credits_of
        self._dispatch_mode = dispatch_mode
        self._default_timeout = default_timeout
        self._generated_dir = generated_dir
        self._retention_days = retention_days

    @property
    def worker_count(self) -> int:
        return len(self._runners)

    @property
    def queue_size(self) -> int:
        return sum(q.qsize() for q in self._queues)

    def _idle_candidates(self) -> list[int]:
        return [i for i in range(len(self._queues))
                if not self._busy[i] and not self._queues[i].full()]

    def _next_by_rotation(self, candidates: list[int]) -> int:
        n = len(self._queues)
        for offset in range(n):
            idx = (self._rr + offset) % n
            if idx in candidates:
                self._rr = (idx + 1) % n
                return idx
        return candidates[0]

    def _pick(self) -> int:
        """挑一個 worker。

        預設「點數優先」：帳號的月配額常常不一樣（實測 40／100／300／300），
        單純輪流會讓點數少的先見底、變成失敗來源。挑剩餘點數最多的，四個
        帳號才會一起接近底線。

        點數要跑過一單才觀測得到，所以還沒有數字的帳號用輪流去發掘；已知
        點數不足一單（少於 10 點）的帳號會被讓到未知帳號後面。
        都在忙時排進最短的佇列。
        """
        candidates = self._idle_candidates()
        if not candidates:
            idx = min(range(len(self._queues)), key=lambda i: self._queues[i].qsize())
            self._rr = (idx + 1) % len(self._queues)
            return idx
        if self._dispatch_mode != "credits" or self._credits_of is None:
            return self._next_by_rotation(candidates)

        known = [(self._credits_of(i), i) for i in candidates]
        known = [(c, i) for c, i in known if isinstance(c, int)]
        unknown = [i for i in candidates
                   if not isinstance(self._credits_of(i), int)]
        if known:
            best_credits = max(c for c, _ in known)
            if best_credits >= CREDITS_PER_JOB or not unknown:
                # 點數一樣多的帳號之間再輪流，免得永遠只用同一個
                top = [i for c, i in known if c == best_credits]
                return self._next_by_rotation(top)
        return self._next_by_rotation(unknown or candidates)

    def _requeue_other(self, job: Job, failed_index: int) -> bool:
        """把這一單改派給還沒試過的帳號，回報有沒有排出去。

        只給帳號層級的問題用（目前沒有任何錯誤碼符合，見 `_REDISPATCH_CODE`
        的說明）。試過的帳號記在
        `job.params["tried_workers"]` 並且跟著 job 存進 DB，所以最多試到帳號
        用完就一定收斂，不會在佇列之間繞圈。
        """
        tried = set(job.params.get("tried_workers") or [])
        tried.add(failed_index)
        candidates = [i for i in range(len(self._queues))
                      if i not in tried and not self._queues[i].full()]
        if not candidates:
            job.params["tried_workers"] = sorted(tried)
            return False

        idx = self._next_by_rotation(candidates)
        job.params["tried_workers"] = sorted(tried)
        job.params["worker"] = idx
        job.status = "queued"
        job.started_at = None
        self._store.save(job)
        self._queues[idx].put_nowait(job.id)
        return True

    def submit(self, params: dict) -> Job:
        if all(q.full() for q in self._queues):
            raise QueueFullError()
        idx = self._pick()
        if self._queues[idx].full():
            raise QueueFullError()
        params = dict(params, worker=idx)
        job = self._store.create(params)
        self._queues[idx].put_nowait(job.id)
        return job

    async def worker_loop(self, index: int = 0) -> None:
        queue = self._queues[index]
        runner = self._runners[index]
        while True:
            job_id = await queue.get()
            self._busy[index] = True
            job: Job | None = None
            try:
                job = self._store.get(job_id)
                if job is None:
                    continue
                job.status = "generating"
                job.started_at = time.time()
                self._store.save(job)
                cleanup_expired(self._generated_dir, self._retention_days)
                timeout = int(job.params.get("timeout") or self._default_timeout)
                try:
                    clips = await asyncio.wait_for(runner(job), timeout=timeout)
                    job.clips = clips
                    if not any(c.downloadable for c in clips):
                        raise GenerationError("download_failed", "一首可下載的都沒有")
                    job.status = "done"
                except GenerationError as e:
                    if _REDISPATCH_CODE and e.code == _REDISPATCH_CODE:
                        if self._requeue_other(job, index):
                            log.warning(
                                "job %s：帳號 %s 被要求防機器人驗證碼，改派給帳號 %s",
                                job.id, index, job.params["worker"])
                            continue
                        tried = job.params.get("tried_workers") or [index]
                        log.warning("job %s：帳號 %s 全被要求驗證碼，這一單失敗",
                                    job.id, "、".join(str(i) for i in tried))
                        e.message = (f"{e.message} 已試過帳號 "
                                     f"{'、'.join(str(i) for i in tried)}，全部都被要求。")
                    job.status = "error"
                    job.error = e.code
                    job.error_message = e.message or None
                except asyncio.TimeoutError:
                    job.status = "error"
                    job.error = "generation_timeout"
                except Exception as e:  # 意料外的一律歸 browser_error
                    job.status = "error"
                    job.error = "browser_error"
                    job.error_message = str(e)[:500]
                job.finished_at = time.time()
                self._store.save(job)
            except Exception as e:
                # job 可能連 self._store.get() 都還沒成功（例如暫時性 sqlite
                # 錯誤），此時沒有 job 物件可以標記失敗，只能放過這一筆、讓
                # 迴圈繼續存活等下一筆，不能讓 worker coroutine 整個掛掉
                # （否則佇列從此永久卡死，需要重啟服務才能恢復）。
                if job is not None:
                    job.status = "error"
                    job.error = "browser_error"
                    job.error_message = str(e)[:500]
                    job.finished_at = time.time()
                    try:
                        self._store.save(job)
                    except Exception:
                        pass
            finally:
                # 沒放回去的話 _idle_candidates() 永遠回空list，_pick 每次都
                # 掉進「都在忙」的分支挑最短佇列，等於一直打同一個帳號。
                self._busy[index] = False


def cleanup_expired(generated_dir: str, retention_days: int) -> int:
    """刪掉超過保留天數的 job 目錄，回報刪了幾個。生成前順手呼叫，不另開排程。"""
    root = Path(generated_dir)
    if not root.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86400
    deleted = 0
    for child in root.iterdir():
        if child.is_dir() and child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            deleted += 1
    return deleted
