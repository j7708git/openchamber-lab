# openchamber-lab 規範

- 本專案所有 OpenChamber extension／OpenCode plugin 的研究與開發都在此目錄內。
- 一個擴充 = `plugins/<kebab>/`、`extensions/<kebab>/` 或 `mcp/<kebab>/`（本地 stdio MCP server，自帶 `README.md`）。
- 實作前先讀 `plugins/<名稱>/README.md` 與對應的任務卡；不確定 API 時查本機
  `@opencode/plugin` 型別或官方文件，不要憑記憶。
- 公開知識寫這個 repo 的 `docs/`；內部工作紀錄寫
  `project-notes/projects/openchamber-lab/`。兩者不要混。
- `.local/`、`.openchamber/`、`.tmp/`、`.scratch/` 是內部或暫存，不進版控。
- 文件與 commit message 用繁體中文，識別字用英文。
