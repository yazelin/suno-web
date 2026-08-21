# suno-web — AI Agent 使用指引

這支服務把 Suno 網頁版的音樂生成包成 job 式 HTTP API。流程只有三步：送單拿 `job_id`、輪詢到終態、下載音檔。

先讀 [README.md](README.md) 的「帳號前提」再動手：綁的是免費帳號，一單扣 10 點、出 2 首，一個月大約 10 單。這不是免費無限的資源，別拿它跑批次。

## 一、健檢

送單之前先確認服務活著、而且登入態還在。

```bash
curl -s http://localhost:8071/api/health
```

```json
{"status": "ok", "queue_size": 0, "uptime_seconds": 7.6,
 "browser_alive": true, "logged_in": null, "credits": null}
```

判讀方式：

| 欄位 | 怎麼看 |
|---|---|
| `status` | 打得到就是 `ok`。連不上代表服務沒起來，要人工 `suno-web serve` 或 `systemctl start suno-web-api` |
| `browser_alive` | `false` 代表 Playwright 頁面掛了，重啟服務 |
| `logged_in` | `true` 才是登入態有效。`false` 要人工重跑 `suno-web login` |
| `credits` | 這個月還剩幾點。低於 10 就送不出下一單了 |
| `queue_size` | 前面還有幾單在排。單 worker，一次跑一單 |

`logged_in` 與 `credits` 是惰性觀測值：服務剛起來時兩個都是 `null`，等第一個 job 真的導覽過 Create 頁面之後才有值。看到 `null` 不代表壞掉，代表還沒有人跑過單。

`/api/health` 不需要金鑰。其他端點只要服務設過 `API_KEYS` 就都要帶 `x-api-key`。

## 二、送單與輪詢

送單（Simple 模式）：

```bash
curl -s -X POST http://localhost:8071/api/generate \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_KEY" \
  -d '{"prompt": "a warm acoustic folk song about a long train ride"}'
```

```json
{"job_id": "d9025d66e20a", "status": "queued"}
```

送單（Custom 模式，自己給歌詞）：

```bash
curl -s -X POST http://localhost:8071/api/generate \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_KEY" \
  -d '{"lyrics": "夜色壓在窗邊\n鍵盤還亮著",
       "style": "lo-fi hip hop, mellow female vocal",
       "title": "深夜寫程式"}'
```

輪詢，建議間隔 10 秒，一單通常 2 到 4 分鐘：

```bash
curl -s -H "x-api-key: YOUR_KEY" \
  http://localhost:8071/api/jobs/d9025d66e20a
```

`status` 走 `queued` 到 `generating`，最後停在 `done` 或 `error`。看到 `done` 之後把每個 `downloadable: true` 的 clip 抓下來：

```bash
curl -s -H "x-api-key: YOUR_KEY" \
  -o song.mp3 \
  http://localhost:8071/api/jobs/d9025d66e20a/files/00000000-1111-2222-3333-444444444444.mp3
```

`audio_url` 直接照 job 回應裡給的相對路徑接在服務位址後面，不要自己拼檔名。`downloadable: false` 的 clip 沒有 `audio_url`，跳過它。

音檔保留 14 天（`AUDIO_RETENTION_DAYS`），要留就自己複製走。

## 三、錯誤碼

失敗的 job 是 `status: "error"`，`error` 帶下面其中一個碼，`error_message` 帶一句說明。

| 錯誤碼 | 什麼意思 | 建議動作 |
|---|---|---|
| `queue_full` | 佇列滿了，`POST /api/generate` 直接回 HTTP 429，不會產生 job | 等前面的單跑完再送，或把 `QUEUE_MAX_SIZE` 調大 |
| `not_logged_in` | 頁面上出現 Sign in，登入態過期了 | 請人工在服務那台機器跑 `suno-web login`，agent 自己救不了這個 |
| `captcha_required` | Suno 對這個帳號要求 Cloudflare 驗證碼，程式點不過。實測綁帳號信任度：新帳號會被要求、有生成歷史的老帳號不會。**撞到時會自動改派給還沒試過的帳號重跑，四個帳號全被要求才回這個碼**，此時 `error_message` 會列出試過哪幾個。解法是用真人開的瀏覽器手動生一兩單養信任 |
| `submit_failed` | 表單填不進去、Create 按不下去，或按了之後 feed 沒出現新 clip | 先讀 `error_message`。訊息是「按了 Create 但 feed 沒出現新 clip」時，最常見的原因是帳號點數用完，查 `/api/health` 的 `credits`；訊息提到 selector 過期就是 Suno 改版了，要修 `src/selectors.py` |
| `generation_timeout` | 超過這一單的 `timeout`（預設 600 秒）還沒到終態 | 重送一次；要生長曲子就在送單時把 `timeout` 調大 |
| `clip_error` | Suno 自己把某首 clip 標成 error。這個字串出現在 clip 的 `status` 欄位，不會出現在 job 的 `error` | 看每個 clip 的 `status` 判斷是哪一首壞掉。整單如果一首都沒下載到，job 的 `error` 會是 `download_failed` |
| `download_failed` | 到了終態但一首可下載的都沒有（全部被 VIP 鎖住，或抓下來的檔案小於 1 KB） | 換個 prompt 重送。連續發生就查帳號方案有沒有變動 |
| `browser_error` | 意料外的例外，瀏覽器或頁面出事 | 查 `/api/health` 的 `browser_alive`，通常要重啟服務（`sudo systemctl restart suno-web-api`） |

另外，`POST /api/generate` 在 `prompt` 與 `lyrics`／`style` 全空時回 HTTP 400 `invalid_request`；金鑰不對回 HTTP 403 `invalid_api_key`。這兩個都不會產生 job。

## 四、prompt 建議

- **曲風、樂器、情緒這類描述用英文比較穩。** 中文也吃得下，但英文的結果比較貼近描述。
- **歌詞可以直接寫中文。** Suno 唱得出來，`lyrics` 欄位不需要翻譯。
- Simple 模式一句話就夠（`prompt`）。要控制歌詞內容才走 Custom（`lyrics`、`style`、`title`）。
- 兩種模式二選一：只要帶了 `lyrics` 或 `style`，`prompt` 就會被忽略。
- 不要把使用者的原話直接轉送。先理解意圖，再擴寫成有主體、樂器、節奏、情緒的描述。例如使用者說「做一首寫程式的背景音樂」，送出去的可以是 `a mellow lo-fi hip hop instrumental, soft rhodes piano, brushed drums, late-night focus mood`。
- `instrumental: true` 是純音樂。Custom 模式底下這個開關會切換 Suno 的 Lyrics mode，開了之後歌詞框整個從 DOM 消失，所以同一單再帶非空的 `lyrics` 會在 API 這層直接被擋下來，回 HTTP 400 `invalid_request`，不會真的送進瀏覽器流程。要純音樂就別給歌詞。
- 模型不可選，固定用頁面預設的 v4.5-all。送 `model` 欄位沒有作用。

## 五、介面設計規範

管理台在 `/admin`（`src/admin.py`）。改它或做任何新介面，一律照這條走：

**深色底，accent 用珊瑚橘或洋紅。刻意避開 gemini-web 的淺色靛藍。** 兩個服務會部署在同一台機器上，介面要能直接分辨誰是誰，不能靠讀網址。

建議的起手色票（要調整可以，但別滑回淺色靛藍）：

| 用途 | 建議值 |
|---|---|
| 頁面底色 | 暖調深色，例如 `#161110` |
| 卡片與面板 | `#211a18` |
| 主要文字 | `#f4ece8` |
| 次要文字 | `#a89b96` |
| 主要 accent（動作、進度、連結） | 珊瑚橘 `#ff6b4a` |
| 次要 accent（強調狀態、標籤） | 洋紅 `#e8407f` |

## 六、要改這個 repo 的話

- Suno 改版導致寫入流程壞掉時，只修 `src/selectors.py`。DOM selector 與 feed URL pattern 全部集中在那一檔，每個常數上面都有偵察筆記寫清楚為什麼是這個值。
- `src/suno.py` 裡的每 20 秒 reload、`created_at` 判斷新舊 clip，都是實機踩出來的 workaround，動之前先讀註解。
- 測試：`uv sync --extra dev` 之後 `uv run pytest -q`，一律用 uv，不要用 pip。
- 這是 public repo，測試 fixture 與文件不可以出現真實帳號資料（clip id、歌名一律改假值）。
- 文件與 commit 訊息一律正體中文、全形標點、不用 emoji。
