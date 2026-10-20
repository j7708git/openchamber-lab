# deepseek-websearch plugin — DeepSeek 網頁搜尋 provider

把 **DeepSeek 官方 Search API** 接成 OpenCode v2 的 websearch provider。

- provider `id`：`deepseek`；顯示名稱：`DeepSeek`。
- 註冊後會出現在 `GET /api/websearch/provider`，以及 OpenChamber 的
  **設定 → 網頁搜尋** 清單，可直接選用。
- plugin **只註冊 provider**，不改其他行為、不自動把 DeepSeek 設成預設。

## 運作方式

每次 websearch 查詢會 `POST https://api.deepseek.com/v1/tools/search`，請求帶
`Authorization: Bearer $DEEPSEEK_API_KEY` 與 `Content-Type: application/json`，
body 為 `query`、`count`、`need_content`，有設定 `localization` 時才多送該欄位。

DeepSeek 的 `results[]` 會映射成 OpenCode 的 `Result`：

| DeepSeek 欄位 | OpenCode | 規則 |
| --- | --- | --- |
| `url` | `url` | 必填；缺欄位、非字串或空字串的結果**整筆跳過** |
| `title` | `title` | 非空字串才放 |
| `summary` | `content` | 第一優先（約 600–800 字，官方說這是給模型讀的） |
| `snippet` | `content` | `summary` 空時才用（約 100–200 字） |
| `content` | `content` | 僅 `needContent: true` 時使用；預設 `need_content:false` 時上游必為空字串 |
| `date` | `time.published` | `Date.parse(date)` 的 epoch **毫秒**；`null`／無法解析時給 `time: {}` |
| `rerank_score`／`site_name`／`authority`／… | — | 丟棄，保留 API 原順序 |

其他行為：

- `query` trim 後為空，或超過 128 個 Unicode 字元 → **直接拋錯，不送出請求**。
- HTTP 200 但 `results` 是空陣列／缺欄位 → 回 `[]`，不是錯誤（DeepSeek 也不計費）。
- 401／403：不重試，拋「金鑰無效或沒有權限」。
- 402：不重試，拋「餘額不足」。
- 408／429／5xx：最多重試 `maxRetries` 次（預設 2）。429 優先讀 `Retry-After`
  （秒數或 HTTP-date，單次等待上限 5 秒），其餘用 0.5s → 1s → … 的指數退避；
  等待可被上層取消訊號打斷。用完仍失敗 → 拋錯（429 會註明是**帳號層級限速**）。
- 逾時：整次搜尋（含重試與退避）預設 15 秒，可用 `timeoutMs` 調整。
- 任何錯誤都只帶可讀原因，**不含金鑰或完整 header**；空結果不會被當成成功以外的狀態。

## 金鑰來源與順序

每次 `execute` 都會重新解析金鑰，順序如下：

1. 環境變數 `DEEPSEEK_API_KEY`（trim 後非空者優先）。
2. OpenChamber credential store 裡 `deepseek` integration 的 active 金鑰
   （`key` 型；透過 `ctx.integration.connection.active("deepseek")` +
   `resolve()` 取得）。

兩者都沒有時，plugin 本身仍會正常載入；等到真的被選用查詢時才拋錯，訊息會提示
這兩種設定方式。API 層會把它收斂成 HTTP 503（body 只有
`Web search request failed: deepseek`），實際原因只會出現在 server log。

- 環境變數是 **server 啟動時的快照**：改了 `DEEPSEEK_API_KEY` 要重啟 server 才生效。
- credential store 是每次查詢即時讀取，日常使用建議把金鑰放那裡。
- **金鑰不要寫進 `opencode.json(c)` 或任何專案檔案**；本 README 只會出現變數名稱。

## 安裝位置

把整個資料夾放在：

    plugins/deepseek-websearch/

執行期由 OpenCode 直接載入 TypeScript，**不需要 build**。`package.json` 的
`devDependencies`（`@opencode/plugin`、`typescript`）只用於型別檢查，不會成為執行期依賴；
`npm install` 只在你想要跑 `tsc` 時才需要。

## 在 `opencode.json(c)` 啟用

在工作區的 `opencode.jsonc`（或 `opencode.json`）的 `plugins` 陣列加入一筆：

```jsonc
{
  "plugins": [
    {
      "package": "../openchamber-lab/plugins/deepseek-websearch",
      "options": {
        "count": 10,
        "needContent": false,
        "timeoutMs": 15000,
        "maxRetries": 2
      }
    }
  ]
}
```

`package` 的相對路徑**以這份 config 檔所在目錄為基準**（以上例而言，config 在
`<工作區>/.opencode/`，plugin 在 `<工作區>/openchamber-lab/plugins/deepseek-websearch/`）。
若把 plugin 放到別的位置，請改成從 config 檔目錄算起的相對路徑，或使用絕對路徑。
`options` 可以整個省略，全部走預設值。

## 選項

| 欄位 | 型別 | 預設 | 說明 |
| --- | --- | --- | --- |
| `count` | `number` | `10` | 回傳筆數，夾在 1–50；不影響單次計價 |
| `needContent` | `boolean` | `false` | 是否要求上游回傳完整頁面內容；`true` 時回應較大、較慢，且 `content` 只在 `summary`／`snippet` 都空時才採用 |
| `localization` | `string` | 不送 | 原樣送給 DeepSeek 的 `localization` 參數（官方文件描述為 IP 字串；一般不需要設定） |
| `timeoutMs` | `number` | `15000` | 整次搜尋（含所有重試與退避）的逾時毫秒數 |
| `maxRetries` | `number` | `2` | 408／429／5xx 的最大重試次數（不含第一次請求）；`0` 代表不重試 |

選項缺省或格式不對時回退預設值；`count` 會自動夾到合法範圍。

## 怎麼設為預設

plugin **不會**自動設定預設 provider。要用 DeepSeek 當預設，二選一：

1. 在 config 加：

   ```jsonc
   {
     "websearch": { "provider": "deepseek" }
   }
   ```

2. 在 OpenChamber **設定 → 網頁搜尋** 選 `DeepSeek`（服務端會記住這個選擇）。

設成預設後，所有沒有指定 `providerID` 的 websearch 都會走 DeepSeek；不想用時把
`websearch.provider` 改掉或在設定頁換回其他 provider 即可。

## 計價與限速（摘要）

以下數字來自 DeepSeek 官方文件（2026-10 查詢），實際以官方公告為準：

- **US$0.75 / 1,000 次成功請求**。
- 只有 HTTP 200 且 `results` 非空才計費；空結果、失敗、逾時都**不計費**。
- `count` 不影響單價（一次請求就是一次）。
- Search 限速 **100 QPS，帳號層級**（所有 API key 共用，與模型併發分開）；
  超過會回 HTTP 429。

## 隱私提醒

- `query` 會完整送到 DeepSeek；不要在查詢裡放憑證、個資或未公開的敏感內容。
- 搜尋結果（`title`／`content`）是外部網頁內容，屬於**不可信資料**；引用前請自行判斷。

## 已知限制

- **`content` 很長**：預設使用 `summary`（約 600–800 字），多筆結果會佔用不少 context；
  可用 `count` 控制筆數。
- **429 是帳號層級**：同一帳號下所有使用者的請求共用 100 QPS，不是單一 key 的問題。
- **沒有快取**：同一個查詢重複送出就是重複計費。
- **query 不截斷**：超過 128 字元直接失敗，避免悄悄改變查詢語意；請在呼叫端縮短。
- **不做 rerank 調參／多輪搜尋**：完全採用 API 回傳的順序與分數。
- **錯誤細節不進 HTTP 回應**：OpenCode 只會回 503 與通用訊息，詳細原因要看 server log。
- **provider 只在載入它的 location 出現**：plugin 由哪個 config 載入，就只在該
  location 的 provider 清單中；換工作區／location 時要各自設定。
- **`need_content:true` 的回應大小與延遲未定**：官方未給上限，請謹慎開啟。
- **不自動設預設**：避免覆蓋使用者在 config 或設定頁的選擇。

## 疑難排解

| 症狀 | 可能原因與處理 |
| --- | --- |
| 設定頁看不到 `DeepSeek` | plugin 沒被載入：確認 `plugins` 項目在該 location 的 config、`package` 路徑以 config 目錄為基準且正確；server log 應有 `[deepseek-websearch] provider 已註冊…`。必要時對 `GET /api/websearch/provider` 確認有沒有 `{"id":"deepseek","name":"DeepSeek"}`。 |
| 查詢回 503、log 說沒有金鑰 | `DEEPSEEK_API_KEY` 沒設（或 server 啟動後才改，需重啟），且 credential store 沒有 `deepseek` 金鑰；照上面的「金鑰來源」設定。 |
| 401／403 | 金鑰無效、貼錯或沒有權限（403 也可能是上游 Fetch 安全政策）；重新產生或重新貼上金鑰。 |
| 402 | 帳號餘額不足，先到 DeepSeek 平台儲值。 |
| 429 | 帳號層級 100 QPS 超限；降低呼叫頻率或稍後再試。plugin 已自動做有限次退避重試。 |
| 查詢回 503，但 log 有別的原因 | 看 server log 的 `[deepseek-websearch] 搜尋失敗：…` 一行（不會含金鑰）；client 端只會看到通用訊息。 |
| 改了金鑰卻沒生效 | 環境變數要重啟 server；credential store 則是下一次查詢就生效。 |

## 開發時的型別檢查

```bash
npm install --no-audit --no-fund
npx tsc --noEmit index.ts search.ts \
  --module esnext --target es2022 --moduleResolution bundler --skipLibCheck
```

`search.ts` 是純函式、不 import `@opencode/*`，可以直接用 Node 22 的 TypeScript type
stripping 或 Bun import 來測（金鑰請用假值，不要放真金鑰）。
