import asyncio
import sqlite3

import pytest

from src.jobs import Clip, GenerationError, JobQueue, JobStore, QueueFullError


def make_queue(tmp_path, runner, **kw):
    store = JobStore(str(tmp_path / "jobs.db"))
    defaults = dict(max_size=10, default_timeout=5,
                    generated_dir=str(tmp_path / "generated"), retention_days=14)
    defaults.update(kw)
    return store, JobQueue(store, runner, **defaults)


async def run_until_finished(store, queue, job_id, seconds=3.0):
    worker = asyncio.create_task(queue.worker_loop())
    try:
        deadline = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < deadline:
            job = store.get(job_id)
            if job.status in ("done", "error"):
                return job
            await asyncio.sleep(0.05)
        raise AssertionError("job 沒有在期限內結束")
    finally:
        worker.cancel()


async def test_success_path(tmp_path):
    async def runner(job):
        return [Clip(id="c1", status="complete", downloadable=True, filename="c1.mp3")]

    store, queue = make_queue(tmp_path, runner)
    job = queue.submit({"mode": "simple", "prompt": "x"})
    done = await run_until_finished(store, queue, job.id)
    assert done.status == "done"
    assert done.clips[0].filename == "c1.mp3"
    assert done.started_at and done.finished_at


async def test_no_downloadable_clip_is_download_failed(tmp_path):
    async def runner(job):
        return [Clip(id="c1", status="complete", downloadable=False)]

    store, queue = make_queue(tmp_path, runner)
    job = queue.submit({"mode": "simple"})
    done = await run_until_finished(store, queue, job.id)
    assert done.status == "error"
    assert done.error == "download_failed"
    assert done.clips[0].id == "c1"  # metadata 仍要留著


async def test_generation_error_code_passthrough(tmp_path):
    async def runner(job):
        raise GenerationError("not_logged_in", "請先 login")

    store, queue = make_queue(tmp_path, runner)
    job = queue.submit({})
    done = await run_until_finished(store, queue, job.id)
    assert done.status == "error"
    assert done.error == "not_logged_in"
    assert done.error_message == "請先 login"


async def test_timeout(tmp_path):
    async def runner(job):
        await asyncio.sleep(60)

    store, queue = make_queue(tmp_path, runner)
    job = queue.submit({"timeout": 1})
    done = await run_until_finished(store, queue, job.id, seconds=4.0)
    assert done.error == "generation_timeout"


async def test_unexpected_exception_is_browser_error(tmp_path):
    async def runner(job):
        raise RuntimeError("boom")

    store, queue = make_queue(tmp_path, runner)
    job = queue.submit({})
    done = await run_until_finished(store, queue, job.id)
    assert done.error == "browser_error"
    assert "boom" in done.error_message


async def test_queue_full(tmp_path):
    async def runner(job):
        await asyncio.sleep(60)

    store, queue = make_queue(tmp_path, runner, max_size=1)
    queue.submit({})
    with pytest.raises(QueueFullError):
        queue.submit({})


def test_cleanup_expired(tmp_path):
    import os
    import time as _t
    from src.jobs import cleanup_expired

    root = tmp_path / "generated"
    old = root / "oldjob"
    new = root / "newjob"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    stale = _t.time() - 15 * 86400
    os.utime(old, (stale, stale))
    cleanup_expired(str(root), 14)
    assert not old.exists()
    assert new.exists()


async def test_worker_survives_bad_timeout_param(tmp_path):
    """Regression: bad timeout param should not kill worker loop."""
    async def runner(job):
        return [Clip(id="c1", status="complete", downloadable=True, filename="c1.mp3")]

    store, queue = make_queue(tmp_path, runner)

    # Submit job with invalid timeout (list instead of int) — this will raise ValueError
    bad_job = queue.submit({"timeout": ["bogus"]})
    bad_done = await run_until_finished(store, queue, bad_job.id, seconds=3.0)

    # Bad job should end in error, not stuck in generating
    assert bad_done.status == "error"
    assert bad_done.error == "browser_error"

    # Now submit a good job — worker must survive and process it
    good_job = queue.submit({"mode": "simple"})
    good_done = await run_until_finished(store, queue, good_job.id, seconds=3.0)

    # Good job should complete successfully
    assert good_done.status == "done"
    assert good_done.clips[0].filename == "c1.mp3"


async def test_worker_survives_store_get_error(tmp_path):
    """Regression: store.get() 炸掉（例如暫時性 sqlite 錯誤）不該讓
    worker coroutine 整個掛掉，下一筆 job 還是要能正常跑完。"""
    async def runner(job):
        return [Clip(id="c1", status="complete", downloadable=True, filename="c1.mp3")]

    store, queue = make_queue(tmp_path, runner)
    real_get = store.get
    broken_ids: set[str] = set()

    def flaky_get(job_id):
        if job_id in broken_ids:
            broken_ids.discard(job_id)
            raise sqlite3.OperationalError("database is locked")
        return real_get(job_id)

    store.get = flaky_get

    bad_job = queue.submit({"mode": "simple"})
    broken_ids.add(bad_job.id)
    # 第一筆 job 因為 store.get 炸掉，這裡沒有 job 物件可以標記失敗，
    # 只確認迴圈存活、第二筆能正常跑完即可。
    good_job = queue.submit({"mode": "simple"})
    good_done = await run_until_finished(store, queue, good_job.id, seconds=3.0)

    assert good_done.status == "done"
    assert good_done.clips[0].filename == "c1.mp3"


# ── 撞到防機器人驗證碼時改派給別的帳號 ──────────────────────────────────────

async def run_all_workers(store, queue, job_id, seconds=3.0):
    """多帳號版的 run_until_finished：四條 worker_loop 一起跑。"""
    tasks = [asyncio.create_task(queue.worker_loop(i))
             for i in range(queue.worker_count)]
    try:
        deadline = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < deadline:
            job = store.get(job_id)
            if job.status in ("done", "error"):
                return job
            await asyncio.sleep(0.05)
        raise AssertionError("job 沒有在期限內結束")
    finally:
        for t in tasks:
            t.cancel()


def captcha_error():
    return GenerationError("captcha_required", "Suno 對這個帳號要求驗證碼")


async def test_busy_flag_resets_after_job(tmp_path):
    """worker 跑完一單要變回閒置，否則派工邏輯整個失效"""
    async def runner(job):
        return [Clip(id="c1", downloadable=True, filename="c1.mp3")]

    store, queue = make_queue(tmp_path, runner)
    job = queue.submit({"prompt": "x"})
    await run_until_finished(store, queue, job.id)
    assert queue._busy == [False]


async def test_captcha_redispatches_to_next_account(tmp_path):
    """0 號帳號被舉牌就改派給 1 號，整單仍然成功"""
    tried = []

    def make_runner(idx, ok):
        async def runner(job):
            tried.append(idx)
            if not ok:
                raise captcha_error()
            return [Clip(id="c1", downloadable=True, filename="c1.mp3")]
        return runner

    runners = [make_runner(0, False), make_runner(1, True)]
    store, queue = make_queue(tmp_path, runners)
    job = queue.submit({"prompt": "x"})
    done = await run_all_workers(store, queue, job.id)

    assert done.status == "done"
    assert tried == [0, 1]
    assert done.params["tried_workers"] == [0]   # 被舉牌的那個記下來
    assert done.params["worker"] == 1            # 最後是誰生出來的


async def test_captcha_on_every_account_finally_fails(tmp_path):
    """四個帳號都被舉牌才算失敗，訊息要講清楚試過哪幾個"""
    tried = []

    def make_runner(idx):
        async def runner(job):
            tried.append(idx)
            raise captcha_error()
        return runner

    runners = [make_runner(i) for i in range(4)]
    store, queue = make_queue(tmp_path, runners)
    job = queue.submit({"prompt": "x"})
    done = await run_all_workers(store, queue, job.id)

    assert done.status == "error"
    assert done.error == "captcha_required"
    assert sorted(tried) == [0, 1, 2, 3]         # 每個都真的試過一次
    assert "0" in done.error_message and "3" in done.error_message


async def test_non_captcha_errors_do_not_redispatch(tmp_path):
    """只有防機器人才換帳號。登入態過期直接失敗，不要浪費其他帳號"""
    tried = []

    def make_runner(idx):
        async def runner(job):
            tried.append(idx)
            raise GenerationError("not_logged_in", "頁面出現 Sign in")
        return runner

    runners = [make_runner(i) for i in range(4)]
    store, queue = make_queue(tmp_path, runners)
    job = queue.submit({"prompt": "x"})
    done = await run_all_workers(store, queue, job.id)

    assert done.status == "error"
    assert done.error == "not_logged_in"
    assert len(tried) == 1
