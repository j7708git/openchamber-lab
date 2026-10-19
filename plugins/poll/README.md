# poll plugin — 多模型諮詢

一次邀請多個模型，對**同一個問題**各發表一次性意見，意見回到**同一個 session**
由原本的 agent 綜合。這不是 multi-run（分頭「做」），是分頭「說」。

- 意見一律走 OpenCode 的 `ctx.generate.text`：**不開 session、不帶工具、不進歷史**。
- 意見是純資料、不碰專案；融合者固定是原 session 的 agent。

## 用法

### 1. `/poll`（使用者觸發）

在輸入框打：

    /poll 這個 API 要不要拆成兩個端點？
    /poll [opencode-go/deepseek-v4.1-flash,opencode-go/qwen3.8-flash] 這個 API 要不要拆成兩個端點？

- 開頭 `[...]`（逗號分隔、不要空格）是**這次**要邀請的模型清單；省略就用設定檔的預設名單。
- 其餘全部文字就是問題。問題為空時，plugin 只回一則白話提示，不會呼叫任何模型。
- 各模型的意見會以「多方意見轉述」的格式送回**同一個 session**，請原 agent 逐點評後綜合。

### 2. `poll_ask`（agent 觸發）

agent 可自行呼叫工具 `poll_ask`：

```json
{ "question": "這個 API 要不要拆成兩個端點？", "models": ["opencode-go/qwen3.8-flash"] }
```

- `question`（必填）：要問的問題。
- `models`（選填）：這次要邀請的模型清單，省略就用預設名單。
- 回傳值是 markdown：每顆模型一段署名意見（失敗／逾時／跳過會如實標記），
  末尾附一行提醒「這些是其他模型的一次性意見，請逐點評後再決定採納或捨棄」。

## 安裝與設定

在工作區的 `opencode.jsonc` 加一筆 `plugins`（路徑相對於該設定檔所在目錄）：

```jsonc
{
  "plugins": [
    {
      "package": "../openchamber-lab/plugins/poll",
      "options": {
        "models": [
          "opencode-go/deepseek-v4.1-flash",
          "opencode-go/qwen3.8-flash",
          "opencode-go/glm-5.3-flash"
        ],
        "maxWords": 150,
        "timeoutMs": 60000,
        "allowExpensive": []
      }
    }
  ]
}
```

執行期直接載入 TypeScript，不需要 build、不需要 `npm install`；
`devDependencies` 只放 `@opencode/plugin` 型別。

### options 欄位

| 欄位 | 型別 | 預設 | 說明 |
| --- | --- | --- | --- |
| `models` | `string[]` | 內建三顆 `opencode-go` 中低價模型 | 預設名單，格式 `providerID/id`；以第一段 `/` 切出 providerID，其餘為 id |
| `maxWords` | `number` | `150` | 每顆模型意見的長度上限（寫進指示文字，非硬性截斷） |
| `timeoutMs` | `number` | `60000` | 單顆模型的軟性逾時（毫秒） |
| `allowExpensive` | `string[]` | `[]` | 允許在非明示指名時也呼叫的貴模型清單 |
| `expensiveInputCostPerMToken` | `number` | `1` | 貴模型判定門檻：input 成本（USD per 1M tokens）大於等於此值即視為貴模型 |

options 整個缺省（或個別欄位缺省）時一律回退內建預設。`options.models` 明確給成空陣列，
代表「沒有預設名單」，此時 `/poll` 沒帶 `[...]` 只會回提示，不會呼叫模型。

## 隱私提醒

- `/poll` 與 `poll_ask` 會把**問題全文**送給你設定的多個模型供應商；不要在問題裡
  放憑證、個資或未公開的敏感內容。
- 其他模型的回覆會以「轉述意見」回到原 session。它們是**不可信內容**，請逐點評
  後再決定是否採用，不要直接照做。

## 貴模型防呆

呼叫前會用 `ctx.model.list()` 的成本資訊判斷每顆模型的 input 單價：

- **input 成本 ≥ `expensiveInputCostPerMToken`（預設 1 USD per 1M tokens）→ 貴模型。**
- 貴模型**只在**下列兩種情況才會被呼叫：
  1. 使用者**當次明示指名**（`/poll [...]` 的參數，或 `poll_ask` 的 `models` 參數）；或
  2. 該模型列在 `allowExpensive` 清單裡。
- 否則該顆模型會被跳過，並在意見訊息中列出 `skipped` 與原因（不會靜默消失）。
- **取不到成本資訊的模型不擋**（無法判斷就不擋），此行為如實記錄在
  `project-notes` 的執行期實測筆記中。

## 卸載

移除 `opencode.jsonc` 裡的那筆 `plugins` 項目即可（也就是把 `{ "package": ".../plugins/poll", ... }`
整個物件刪掉）。plugin 的 setup 只註冊一個 command 與一個 tool，沒有其他副作用；
移除後 `/poll` 與 `poll_ask` 就不再出現。也可以只把 `options` 改小（例如 `models: []`）來停用預設名單，
但完整卸載請移除該項目。

## 已知限制

- **軟性逾時**：`timeoutMs` 用 `Promise.race` 實作，底層模型請求**不會被中斷**，
  只是逾時後就標記為 `timeout` 不再等它；該請求仍可能在背景完成。
- **沒有自動重試**：單顆失敗或逾時就只標記自己，不會重試、不會換模型。
- **不做權限提問**：plugin 不會跳出權限詢問。
- **意見佔 context 預算**：意見會進到原 session 的歷史，長對話請適時 `/compact`；
  以 `maxWords` 控制單顆長度，但那是給模型的指示，不是硬性截斷。
- **不做深問**：模型只被問一次，沒有工具、沒有追問、看不到專案內容。
- **成本資訊取不到時不擋貴模型**：若 `ctx.model.list()` 沒有該模型的成本資料，
  防呆不會生效（寧可少擋也不要誤擋）。
- **variant（如思考等級）不寫死**在 plugin 指示文字裡，由 agent 定義或使用者指定。
- **沒有單次模型數上限**：明示指名可以一次帶多顆模型並行呼叫；成本與延遲會隨
  數量增加，請自行克制。
