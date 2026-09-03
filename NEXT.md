# Next

跨檔案、要決定或要人工做的事放這裡。逐項的技術細節在 `docs/acceptance-2026-08-20.md`（八節，記錄每個實測結論），動這條線之前先讀它。

## 現況

> **2026-09-03：需要付費帳號，而且生成目前是壞的。**
> Suno 把免費層改成終身只能下載 7 首（yazelin 回報），四個免費帳號一輩子加起來 28 首，
> 本服務原本「用免費月配額生歌」的前提沒有了。加上底下那條沒查完的 Turnstile 故障，
> 這條線先停著。程式本身沒問題，換付費帳號、把 Turnstile 那條查完就能用。
> 下面的待辦除了最後兩條，都是免費帳號時代的，換方案之後要重新量。


服務跑在 192.168.11.11:8071，systemd `suno-web-api`（enabled）。對外走 nginx 的 `https://ching-tech.ddns.net/suno-web/`，管理台在 `/suno-web/admin`。四個 Suno 帳號、瀏覽器隨用隨開、派工點數優先。額度以實測為準（見待辦第一條），不要在這裡寫死數字——之前寫的 65 單跟待辦裡的 40 單對不起來，就是寫死害的。

## 待辦

- [x] ~~舊歌被當成新歌回報~~ → **我讀錯欄位，不成立**（2026-08-24 更正）。
      `jobs` 表的 `created_at` 是**送單時間**不是完成時間。那 11 單是 18 秒內被送出，
      然後排隊執行，各跑了 137 到 275 秒才完成，後面的排到 415 秒。全部 done 單共回
      55 個 clip、**跨單重複 0 個**——真的是整份 feed 被誤收的話，單與單之間會大量重疊。
      教訓：拿時間戳推論之前先確認那一欄的語意。
- [x] ~~撞驗證碼換帳號重試沒擋住~~ → **前提是錯的，已修（PR #7）**。
      `/api/c/check` 對**所有人**都回 `required: true`，包含真人開的、當下生得出歌的
      瀏覽器（2026-08-27 yazelin 在自己的 DevTools console 實測）。舊版把「等不到新
      clip」＋「required:true」報成 captcha_required，等於任何失敗都被貼上驗證碼的標籤，
      並觸發換帳號重試，把一次故障放大成四次。現在改成看「生成請求有沒有真的送出去」
      分三類，換帳號重試停用。
- [ ] **真正的故障還沒查出來（已停工，2026-09-03）。** 按下 Create 之後 `/api/c/check`
      有發、回 200，印出 `captcha required, awaiting verification`，然後永遠停在那裡，
      `generate` 一次都沒送出去（等滿 300 秒也沒有）。
      已排除的變因與唯一剩下的線索寫在 README 的「生成目前是壞的」那一節，要接手先讀那裡，
      不要從頭再排一遍。停工的理由是免費層已經不可行，投報比不成立。
- [ ] **換到付費帳號之後要重量的東西**：一單幾點、月配額多少、下載有沒有額外限制、
      v4.5-all 以外的模型能不能選、wav 下載是否解鎖。上面那些數字全是免費方案時代的。

- [ ] **加一支 `GET /api/credits`。** `/api/health` 的 credits 只在該 worker 瀏覽器活著時
      才有值，閒置十分鐘關掉之後永遠是 null，所以平常查不到餘額，每次都要 ssh 進去跑探測
      腳本（2026-08-22、08-24 各被問一次，這就是該收成 API 的訊號）。
      做法：依序喚醒四個 worker、讀點數、關掉，回一份 JSON，結果快取十分鐘避免每次開四個
      Chrome。順便讓管理台總覽頁直接顯示。
- [ ] **整體失敗率 45%**（38 單裡 17 失敗，2026-08-24 統計）。集中在上面兩個事件，但這個
      數字本身該掛個監看，不要等使用者來說「怎麼都做不出來」。

- [ ] 四個帳號的餘額不平均。**2026-08-22 09:xx 實測：帳號 0＝50、1＝50、2＝100、3＝200，合計 400 點，還能生 40 單。**
      （量法：在 .11 跑 `cd ~/suno-web && ./.venv/bin/python -c 'import asyncio,sys;sys.path.insert(0,".");from src.cli import _verify_login;[asyncio.run(_verify_login(w)) for w in range(4)]'`。
      開無頭瀏覽器載入 create 頁、側錄 credits API 就有值，不生歌、不燒額度，四個約兩分鐘。
      `/api/health` 的 credits 只有在該 worker 的瀏覽器活著時才有值，閒置關掉之後是 null。）
      哪天真的見底，要補就再開一個免費帳號，`ssh -X` 到 .11 跑 `uv run suno-web login -w 4`，記得**真人手動生一單**開通（推銷彈窗 + `/api/c/check` 的 `required:true` 都靠這一步解掉），然後把 `.env` 的 `WORKER_COUNT` 加一並重啟。
- [ ] 每月配額何時重置還沒定案，但**有一筆反證**：這份待辦 08-21 03:09 寫的是「帳號 0 只剩 20 點」，08-22 實測是 50 點，中間沒有人去加值。
      所以重置點不是月初，比較像各帳號按自己的註冊日滾動。要確定就固定每天量一次上面那個指令，看哪一天跳回滿額。
      在確定之前，派工的「還能生幾單」估算可能會在重置當天失準。
- [ ] `LOGGED_OUT_MARKER` 還沒在真的登出畫面上正面驗證過。登入態哪天過期時，順手確認 job 正確回的是 `not_logged_in`，而非含糊的 `submit_failed`。
- [ ] `.11` 的 Chrome 是把 deb 解在家目錄的，所以 `.env` 設了 `CHROME_NO_SANDBOX=true`。哪天用 apt 正式裝了 Chrome，把那行拿掉恢復沙箱。
- [ ] 音檔目前留 14 天（`AUDIO_RETENTION_DAYS`）。四帳號跑滿一個月約 260 首、每首 2 到 4 MB，磁碟用量到時候看一眼再決定要不要縮短。

## 刻意不做

- 瀏覽器自動自癒：V1 沒有。替代做法是外部監控打 `/api/health` 看 `browser_alive`，false 就 `systemctl restart suno-web-api`。將來要補時，記得一併處理 `SunoRunner._sniffing` 這個旗標（重啟瀏覽器後 sniffer 不會自動裝到新分頁上）。
- Suno 的 extend／cover／persona／stems：scope 大、UI 更複雜，每個都要重新偵察。
  **但 2026-08-24 探到一件事：免費帳號的 Create 頁上 `Add audio - Browse, upload, or
  record audio` 不是 disabled 狀態**，入口進得去。按下去之後會不會中途跳付費牆還沒驗。
  這目前是唯一真的能做「拿一段旋律去改」的路——gemini-web 那條實測無效（檔案掛得上去，
  但產出跟參考毫無關聯，見 [[project_gemini_web_service]]）。
- wav 下載、官方 API fallback：前者要 Pro，後者 Suno 沒有給消費者的官方 API。

## 動手前要知道的三件事

1. **瀏覽器層必須是真 Chrome + CDP**，不能改回 Playwright 內建的 Chromium，也不能讓 Playwright 去啟動 Chrome。理由與對照實驗在 acceptance 第五節。
2. **profile 不要跨機複製**。登入態長在哪台就在哪台用，兩台共用同一個帳號會被 Suno 輪換 token 踢掉。
3. **Suno 改版時只修 `src/selectors.py`**。selector 與 feed 的 URL pattern 全部集中在那一檔，每個常數旁邊都有「這是畫面上的什麼、為什麼是這個值」的註解。
- [ ] generate 連三單 captcha_unsolved(Turnstile 600010,worker 瀏覽器熱著也一樣,請求沒送出去不扣點);systemd 重啟要 sudo 密碼所以沒重啟過,先重啟再驗;BGM 這次已改走 gemini-web /api/music(Lyria)。另:gemini-web worker 2 音樂固定 27.4s 失敗(Gemini 回 error),音樂派工避開它
