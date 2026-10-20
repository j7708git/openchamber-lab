# desktop-use

OpenGCUA Linux X11 桌面操作。所有指令都在
`~/workspace/agent/openchamber-lab/mcp/desktop` 下執行。

授權來源：2026-10-20 使用者 Q1=A 回覆（接受 v0.1.2 的 time-bounded AX fallback）。

## 執行方式

helper 自己設定 `DISPLAY=:0`、`XAUTHORITY=$HOME/.Xauthority`、
`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`，不需要先 export。

```bash
cd ~/workspace/agent/openchamber-lab/mcp/desktop
.venv/bin/python helper.py --help
DESKTOP_DRYRUN=1 .venv/bin/python helper.py ax --app kate
```

- `shot [--out PATH]`：整桌面截圖。
- `ax --app APP [--max-lines N]`：只輸出該 app 的 AX 子樹。
- `click --app APP --role ROLE --name NAME`：AX 點擊。
- `click --app APP --x X --y Y`：座標點擊（先存動前截圖）。
- `type TEXT --app APP [--role ROLE --name NAME] [--verify-file PATH]`
- `paste TEXT --app APP [--verify-file PATH]`
- `key KEY --modifiers ctrl,alt --app APP`

## 鐵律

- **observe 與 act 必須同支程式**。helper 每次呼叫都是一個新程序；`ax` 印出的
  index 只供同次閱讀，下一次 `click`／`type` 要用 `--role/--name` 或 `--x/--y`。
- 一次只操作自己開的 app。先 `shot`，再 `ax`，再 act；act 前 helper 會檢查
  `_NET_ACTIVE_WINDOW` 的 `WM_CLASS` 是否含 `--app`，不符回 `APP_NOT_ACTIVE`，
  不要硬搶焦點。
- 不碰使用者既有視窗。Kate 用 `kate --new`、Dolphin 用 `dolphin --new-window`；
  先記錄 `_NET_CLIENT_LIST_STACKING`，只操作自己開的 app。
- `DESKTOP_DRYRUN=1` 或 `--dry-run` 先看計畫；dry-run 不建 Runtime、不送 XTEST、
  不寫 clipboard。危險鍵在 dry-run 也會拒絕。

## 輸入路徑

| 內容 | 路徑 |
|---|---|
| ASCII | `ui.input.text`（`helper.py type`） |
| 中文 | 首選 `ui.ax.set_text`（`type` 加 `--role/--name`）；無效果才 clipboard |
| 兜底 | clipboard paste（`helper.py paste` 或 `type` 自動 fallback） |

不可以用 `input.text` 打中文。clipboard 一定先存後還原；非文字剪貼簿回
`CLIPBOARD_NOT_TEXT`，不要覆蓋。

## AX 限制

v0.1.2 不支援 `max_nodes`／`max_depth`；helper 先要求（預設 400/8），收到
`AX capture limits are WIP` 後用 `timeout_ms=2000/4000` fallback。`--app` 是事後
過濾，capture 仍是 desktop-wide。

`ax` 的 `depth` 分級：

- `frame-only`：只有 application + frame/window/dialog。
- `chrome-only`：有 menu_bar／tool_bar／filler／panel 等，但沒有 editable 的
  textbox/text。
- `controls`：至少有一個 `editable=True` 的節點。
- `empty`：只有 application 或空。

`depth=ok` 不代表有 editable；新 helper 不再使用 `ok`，只有 `controls` 才可能有可編輯控制項。
`chrome-only` 時可以用 menu/toolbar 的 AX 點擊，但打字要用截圖座標或
clipboard。不要說「AX 可用」；要說「partial、menu/toolbar 有、editable 沒有」。

## 典型流程

```bash
.venv/bin/python helper.py shot
.venv/bin/python helper.py ax --app kate --max-lines 40
.venv/bin/python helper.py click --app kate --role push_button --name "儲存"
.venv/bin/python helper.py type "abc" --app kate
.venv/bin/python helper.py key s --modifiers ctrl --app kate
.venv/bin/python helper.py shot   # 用截圖驗證
# 若有 --verify-file，type/paste 會直接讀檔檢查
```

## 錯誤與下一步

- `APP_NOT_FOUND` / `APP_AMBIGUOUS`：先 `ax` 看 application 名稱；不夠唯一就換更精確的 app 字串。
- `APP_NOT_ACTIVE`：先讓自己的 app 取得前景，再重試；不要硬搶。
- `TARGET_NOT_FOUND`：用 `ax` 找 role/name，或改用 `shot` + `click --x/--y`。
- `TARGET_AMBIGUOUS`：改用更精確的 name，或座標。
- `AX_EMPTY` / `depth=frame-only`：沒有控制項可定位；改用截圖座標，並在紀錄寫「frame 層級」。
- `CLIPBOARD_NOT_TEXT`：不要用 `paste`；改用 `type`（ASCII）或 AX。
- `VERIFY_FAILED`：檔案沒出現預期文字；先確認 app 有存檔（例如 `key s --modifiers ctrl`）。
- `DANGEROUS_KEY_REFUSED`：換安全按鍵；不要嘗試繞過。

## 10 次任務量測欄位

每次任務至少記：`path_used`、`ax_timeout_ms`、`one_shot`、`failure_reason`、
`latency_ms`。helper 會自動寫到 `.logs/actions-YYYYMMDD.jsonl`；回報時直接引用
這些欄位，不要用猜的。
