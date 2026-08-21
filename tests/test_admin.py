"""Admin webui 與動態金鑰。"""
import pytest
from fastapi.testclient import TestClient

from src import admin_db
from src.config import Settings
from src.jobs import Clip, JobQueue, JobStore
from src.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEYS", "")
    # 部署機的 .env 會被 python-dotenv 讀進來（測試常在 repo 目錄下跑），
    # 不清掉的話測試會跟著那台的設定走。
    monkeypatch.setenv("ADMIN_URL_PREFIX", "")
    monkeypatch.setenv("WORKER_COUNT", "1")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret123")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("GENERATED_DIR", str(tmp_path / "generated"))
    settings = Settings()
    monkeypatch.setattr("src.admin_db.settings", settings)
    admin_db.reset_for_tests()

    async def runner(job):
        return [Clip(id="c1", status="complete", downloadable=True,
                     filename="c1.mp3")]

    store = JobStore(str(tmp_path / "jobs.db"))
    queue = JobQueue(store, runner, max_size=10, default_timeout=5,
                     generated_dir=settings.generated_dir, retention_days=14)
    app = create_app(settings=settings, store=store, queue=queue,
                     health_extra=lambda: {"browser_alive": True,
                                           "logged_in": True, "credits": 90})
    with TestClient(app) as c:
        yield c
    admin_db.reset_for_tests()


def login(client):
    r = client.post("/admin/login",
                    data={"username": "admin", "password": "secret123"},
                    follow_redirects=False)
    assert r.status_code == 303
    return r


def test_admin_requires_login(client):
    for path in ("/admin", "/admin/keys", "/admin/history"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303
        assert "/admin/login" in r.headers["location"]


def test_wrong_password_does_not_grant_session(client):
    r = client.post("/admin/login", data={"username": "admin", "password": "nope"},
                    follow_redirects=False)
    assert "err=1" in r.headers["location"]
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_login_then_pages_render(client):
    login(client)
    body = client.get("/admin").text
    assert "剩餘點數" in body and "90" in body
    assert "近期 job" in client.get("/admin/history").text
    assert "送一單" in client.get("/admin/test").text


def test_issue_key_then_it_authorizes_the_api(client):
    login(client)
    r = client.post("/admin/keys", data={"name": "筆電 CLI"},
                    follow_redirects=False)
    raw = r.headers["location"].split("new=")[1]
    assert raw.startswith("snw_")

    # 發了金鑰之後,API 就必須帶金鑰
    assert client.post("/api/generate", json={"prompt": "x"}).status_code == 403
    ok = client.post("/api/generate", json={"prompt": "x"},
                     headers={"x-api-key": raw})
    assert ok.status_code == 200

    # 用量有記到那把金鑰上,而且原文不會再出現在頁面
    page = client.get("/admin/keys").text
    assert "筆電 CLI" in page and raw not in page

    keys = admin_db.list_api_keys()
    assert keys[0]["requests_count"] == 1


def test_disabled_key_is_rejected(client):
    login(client)
    r = client.post("/admin/keys", data={"name": "臨時"}, follow_redirects=False)
    raw = r.headers["location"].split("new=")[1]
    key_id = admin_db.list_api_keys()[0]["id"]

    client.post(f"/admin/keys/{key_id}/disable", follow_redirects=False)
    assert client.post("/api/generate", json={"prompt": "x"},
                       headers={"x-api-key": raw}).status_code == 403

    client.post(f"/admin/keys/{key_id}/enable", follow_redirects=False)
    assert client.post("/api/generate", json={"prompt": "x"},
                       headers={"x-api-key": raw}).status_code == 200


def test_history_shows_the_key_that_made_the_job(client):
    login(client)
    r = client.post("/admin/keys", data={"name": "某專案"}, follow_redirects=False)
    raw = r.headers["location"].split("new=")[1]
    client.post("/api/generate", json={"prompt": "一首測試曲"},
                headers={"x-api-key": raw})
    body = client.get("/admin/history").text
    assert "某專案" in body and "一首測試曲" in body


def test_admin_session_can_fetch_audio_but_stranger_cannot(client, tmp_path):
    """歷史頁的音檔連結是瀏覽器直接點的,不會帶 x-api-key,所以已登入的
    session 要放行;沒登入也沒金鑰的一律 403。"""
    login(client)
    r = client.post("/admin/keys", data={"name": "k"}, follow_redirects=False)
    raw = r.headers["location"].split("new=")[1]
    job_id = client.post("/api/generate", json={"prompt": "x"},
                         headers={"x-api-key": raw}).json()["job_id"]
    d = tmp_path / "generated" / job_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "c1.mp3").write_bytes(b"x" * 2048)

    assert client.get(f"/api/jobs/{job_id}/files/c1.mp3").status_code == 200

    client.cookies.clear()
    assert client.get(f"/api/jobs/{job_id}/files/c1.mp3").status_code == 403
    assert client.get(f"/api/jobs/{job_id}/files/c1.mp3",
                      headers={"x-api-key": raw}).status_code == 200


def test_key_times_render_as_local_not_raw_utc(client):
    """資料庫存 UTC ISO,頁面要轉本地時間顯示(user 在 UTC+8)。"""
    login(client)
    client.post("/admin/keys", data={"name": "時間測試"}, follow_redirects=False)
    body = client.get("/admin/keys").text
    assert "+00:00" not in body
    assert "時間測試" in body


def test_remember_me_extends_the_session_cookie(client):
    """沒勾記住我＝1 天,勾了＝30 天。"""
    short = client.post("/admin/login",
                        data={"username": "admin", "password": "secret123"},
                        follow_redirects=False)
    assert "Max-Age=86400" in short.headers["set-cookie"]

    client.cookies.clear()
    long = client.post("/admin/login",
                       data={"username": "admin", "password": "secret123",
                             "remember": "on"},
                       follow_redirects=False)
    assert f"Max-Age={30 * 86400}" in long.headers["set-cookie"]


def test_login_page_has_autofill_override_and_remember_box(client):
    body = client.get("/admin/login").text
    assert "-webkit-autofill" in body
    assert 'name="remember"' in body


def test_manual_cleanup_reports_how_many_it_deleted(client, tmp_path, monkeypatch):
    """歷史頁那顆清理按鈕:真的刪過期目錄,並把數量顯示出來。"""
    import os
    import time as _t
    login(client)
    gen = tmp_path / "generated"
    old, fresh = gen / "oldjob", gen / "freshjob"
    old.mkdir(parents=True)
    fresh.mkdir(parents=True)
    stale = _t.time() - 30 * 86400
    os.utime(old, (stale, stale))

    r = client.post("/admin/cleanup", follow_redirects=False)
    assert "swept=1" in r.headers["location"]
    assert not old.exists() and fresh.exists()
    assert "已清掉 1 個過期的 job 目錄" in client.get("/admin/history?swept=1").text


def test_overview_lists_each_account_when_multi_worker(tmp_path, monkeypatch):
    """多帳號時總覽要列出每個帳號的點數與用量;單帳號時不顯示那張表。"""
    monkeypatch.setenv("API_KEYS", "")
    monkeypatch.setenv("ADMIN_URL_PREFIX", "")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret123")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "s")
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("GENERATED_DIR", str(tmp_path / "g"))
    settings = Settings()
    monkeypatch.setattr("src.admin_db.settings", settings)
    admin_db.reset_for_tests()

    async def runner(job):
        return [Clip(id="c", status="complete", downloadable=True,
                     filename="c.mp3")]

    store = JobStore(str(tmp_path / "j.db"))
    queue = JobQueue(store, [runner, runner], max_size=10, default_timeout=5,
                     generated_dir=settings.generated_dir, retention_days=14)
    app = create_app(settings=settings, store=store, queue=queue,
                     health_extra=lambda: {
                         "browser_alive": True, "logged_in": True,
                         "credits": 400,
                         "workers": [
                             {"id": 0, "profile": "p", "browser_up": True,
                              "busy": False, "logged_in": True, "credits": 300,
                              "jobs_done": 2, "jobs_failed": 0,
                              "last_used": 1_700_000_000.0},
                             {"id": 1, "profile": "p1", "browser_up": False,
                              "busy": False, "logged_in": None, "credits": 100,
                              "jobs_done": 0, "jobs_failed": 0,
                              "last_used": None},
                         ]})
    with TestClient(app) as c:
        r = c.post("/admin/login",
                   data={"username": "admin", "password": "secret123"},
                   follow_redirects=False)
        assert r.headers["location"] == "/admin", "登入失敗就會被導去登入頁"
        page = c.get("/admin", follow_redirects=False)
        assert page.status_code == 200
        body = page.text

    assert "派工輪流" in body
    assert "300（30 單）" in body and "100（10 單）" in body
    assert "已休眠" in body   # worker 1 的瀏覽器沒起來
    admin_db.reset_for_tests()


def test_history_marks_jobs_that_were_redispatched(client, tmp_path):
    """被驗證碼擋下、換過帳號的單要在歷史頁看得出來——成功的那些畫面上跟一般
    的單長得一樣，不標的話只有 log 追得到"""
    login(client)
    client.post("/api/generate", json={"prompt": "換過帳號的曲子"})

    store = JobStore(str(tmp_path / "jobs.db"))
    job = store.list_recent(1)[0]
    job.params["tried_workers"] = [0, 2]
    store.save(job)

    body = client.get("/admin/history").text
    assert "帳號 0、2 被要求驗證碼，已改派" in body
