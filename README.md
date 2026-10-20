# openchamber-lab

自製 OpenChamber／OpenCode 擴充的實驗室。2026-10-19 清空重來，目前保留
poll plugin 與 deepseek-websearch plugin。

## 內容

- `plugins/poll/`：多模型諮詢。`/poll` 指令與 `poll_ask` 工具，讓多顆模型對
  同一個問題各發表一次性意見，意見回到原 session 由原 agent 綜合。
- `plugins/deepseek-websearch/`：把 DeepSeek 官方 Search API 接成 OpenCode v2 的
  websearch provider（`id: deepseek`、顯示名稱 `DeepSeek`），會在 OpenChamber
  「設定 → 網頁搜尋」出現並可選用。

## 載入方式

工作區根 `.opencode/opencode.jsonc` 的 `plugins` 項目指向
`../openchamber-lab/plugins/poll` 與 `../openchamber-lab/plugins/deepseek-websearch`；
設定變更會熱重載，不需要 build。

## 文件

- `plugins/poll/README.md`：用法、options、貴模型防呆與已知限制。
- `plugins/deepseek-websearch/README.md`：金鑰來源、安裝與啟用、選項、計價與疑難排解。

內部工作紀錄（計畫、進度、研究、驗收）在 private repo
`project-notes/projects/openchamber-lab/`，不放在這個公開 repo。
