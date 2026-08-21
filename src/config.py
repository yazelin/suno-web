"""環境變數設定"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_DEFAULT_DATA_DIR = str(Path.home() / ".suno-web")


def _bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _int(val: str | None, default: int) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


class Settings:
    """服務設定，從環境變數讀取"""

    def __init__(self) -> None:
        # 瀏覽器
        self.headless: bool = _bool(os.getenv("HEADLESS"), False)
        self.profile_dir: str = os.getenv(
            "PROFILE_DIR", str(Path(_DEFAULT_DATA_DIR) / "profiles")
        )
        self.suno_url: str = os.getenv("SUNO_URL", "https://suno.com/create")
        # 真 Chrome 的執行檔。刻意不用 Playwright 內建的 Chromium：那個過不了
        # Suno 的 Turnstile 驗證，理由見 src/browser.py 的模組說明。
        self.chrome_binary: str = os.getenv("CHROME_BINARY", "google-chrome")
        # 預設保留 Chrome 的沙箱。只有在沙箱起不來的機器才打開這個（例如把
        # deb 解到家目錄、chrome-sandbox 沒有 root 的 setuid 位元），代價是
        # 少一層隔離，所以不當預設。
        self.chrome_no_sandbox: bool = _bool(os.getenv("CHROME_NO_SANDBOX"), False)

        # 多帳號：一個 worker 綁一個 Suno 帳號（一個 profile 目錄）
        self.worker_count: int = _int(os.getenv("WORKER_COUNT"), 1)
        # 瀏覽器閒置這麼久就關掉。這服務量小、閒置時間長，常駐四個 Chrome
        # 要吃掉近 5 GB；隨用隨開只多花約 10 到 15 秒，相對 2 到 4 分鐘的
        # 生成可以忽略。設 0 表示永不關閉。
        # 派工模式：credits＝挑剩餘點數最多的帳號（預設，帳號點數不平均時
        # 才不會有人先見底）；round-robin＝單純輪流。點數讀不到時 credits
        # 模式本來就會退回輪流，這個變數是給「連退回都不想要」的情況用的。
        self.dispatch_mode: str = os.getenv("DISPATCH_MODE", "credits")
        self.idle_shutdown_minutes: int = _int(os.getenv("IDLE_SHUTDOWN_MINUTES"), 10)

        # 服務
        self.host: str = os.getenv("HOST", "0.0.0.0")
        self.port: int = _int(os.getenv("PORT"), 8071)
        self.queue_max_size: int = _int(os.getenv("QUEUE_MAX_SIZE"), 10)
        self.default_timeout: int = _int(os.getenv("DEFAULT_TIMEOUT"), 600)

        # API 金鑰（逗號分隔多組，完全沒設＝不驗證）
        _keys = os.getenv("API_KEYS", "")
        self.api_keys: set[str] = {k.strip() for k in _keys.split(",") if k.strip()}


        # Admin webui
        self.admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
        self.admin_password: str = os.getenv("ADMIN_PASSWORD", "change-me")
        self.admin_session_secret: str = os.getenv(
            "ADMIN_SESSION_SECRET", "dev-only-session-secret")
        # 反代到子路徑時要設（例如 nginx 的 location /suno-web/），頁面連結
        # 才會帶對前綴。直接打 8071 就留空。
        self.admin_url_prefix: str = os.getenv("ADMIN_URL_PREFIX", "").rstrip("/")
        self.admin_db_path: str = os.getenv(
            "ADMIN_DB_PATH", str(Path(_DEFAULT_DATA_DIR) / "admin.db"))

        # 資料落點
        self.data_dir: str = _DEFAULT_DATA_DIR
        self.generated_dir: str = os.getenv(
            "GENERATED_DIR", str(Path(_DEFAULT_DATA_DIR) / "generated")
        )
        self.audio_retention_days: int = _int(os.getenv("AUDIO_RETENTION_DAYS"), 14)

        # 寫進下載檔 ID3 的署名。artist 是「演出者」，Suno 官方下載寫的是帳號
        # 擁有者，所以這裡預設留空、由各自部署決定；沒設就退回 "Suno"。
        # 工具名放 TENC（encoded by），那才是它該待的欄位。
        self.tag_artist: str = os.getenv("TAG_ARTIST", "").strip() or "Suno"
        self.tag_encoder: str = os.getenv("TAG_ENCODER", "").strip() or "suno-web"


settings = Settings()


def get_worker_profile_dir(worker_id: int) -> str:
    """第幾個帳號用哪個 profile 目錄。命名沿用 gemini-web：worker 0 用
    profiles/，之後是 profiles-1、profiles-2……"""
    base = Path(settings.profile_dir)
    return str(base) if worker_id == 0 else str(base.parent / f"{base.name}-{worker_id}")
