---
name: desktop-use
description: "Operate this machine's KDE/X11 desktop through the OpenGCUA Linux helper in the OpenChamber lab. Use for screenshot + coordinate clicks, keyboard shortcuts, and clipboard text (including Chinese). Machine-specific: X11 :0, helper venv, OpenGCUA v0.1.2; not for Wayland or other machines."
---

# desktop-use

OpenGCUA Linux X11 桌面操作。所有指令都在
`~/workspace/agent/openchamber-lab/mcp/desktop` 下執行。

授權／狀態：2026-10-20 使用者 Q3=A：維持 S3、不做 S5、不掛 MCP；AX 只供除錯。

## 前提（機器限定）

這個 skill **只在這一台機器**可用：KDE Plasma on X11、X 在 `:0`、
`~/.Xauthority` 存在、使用者 D-Bus 在 `/run/user/1000/bus`、OpenGCUA
`qwen-intelligence-sdk` v0.1.2、host venv 在
`~/workspace/agent/openchamber-lab/mcp/desktop/.venv`、worker venv 在
`~/.cache/mai-ui/linux/0.3/venv`。

- **不支援 Wayland**；別台機器、其他 display、其他 SDK 版本都不要直接套。
- helper 自己會設 `DISPLAY=:0`、`XAUTHORITY`、`DBUS_SESSION_BUS_ADDRESS`；
  但**用 shell 直接開 Kate／Dolphin 時仍要自己帶**這三個變數。B2 實際發生：
  漏了 `DISPLAY` 的 `setsid kate --new` 直接 core dump。
- **跑真輸入前先請使用者讓出滑鼠／鍵盤**；不要假設一般「可以跑」涵蓋桌面控制。

## 主路徑

**截圖找位置 → 座標點擊 → 鍵盤／剪貼簿。** AX 只供除錯，不是定位來源。

```bash
.venv/bin/python helper.py shot
.venv/bin/python helper.py click --app kate --x 500 --y 300
.venv/bin/python helper.py type "abc" --app kate
.venv/bin/python helper.py paste "桌面測試" --app kate
.venv/bin/python helper.py key s --modifiers ctrl --app kate
```

## 執行方式

helper 自己設定 `DISPLAY=:0`、`XAUTHORITY=$HOME/.Xauthority`、
`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`，不需要先 export。

```bash
cd ~/workspace/agent/openchamber-lab/mcp/desktop
.venv/bin/python helper.py --help
DESKTOP_DRYRUN=1 .venv/bin/python helper.py click --app kate --x 500 --y 300
```

- `shot [--out PATH]`：整桌面截圖。
- `click --app APP --x X --y Y`：主路徑座標點擊（先存動前截圖）。
- `click --app APP --role ROLE --name NAME --ax-debug`：**除錯用** AX 點擊；
  沒有 `--ax-debug` 回 `AX_DEBUG_ONLY`。
- `type TEXT --app APP [--verify-file PATH]`：ASCII 用 `input.text`；非 ASCII
  預設 clipboard。
- `type TEXT --app APP --role ROLE --name NAME --ax-debug`：**除錯用**
  `ax.set_text`，失敗才 fallback clipboard。
- `paste TEXT --app APP [--verify-file PATH]`：guarded clipboard。
- `key KEY [--modifiers ctrl,alt] --app APP`。
- `ax --app APP [--max-lines N]`：**DEBUG ONLY**，只輸出該 app 的 AX 子樹。

## 鐵律

- **observe 與 act 必須同支程式**。helper 每次呼叫都是一個新程序；`ax` 印出的
  index 只供同次閱讀，下一次 act 要用 `--x/--y`（主路徑）或除錯用的
  `--role/--name --ax-debug`。
- 一次只操作自己開的 app。先 `shot`，再 act；act 前 helper 會檢查
  `_NET_ACTIVE_WINDOW` 的 `WM_CLASS` 是否含 `--app`，不符回 `APP_NOT_ACTIVE`，
  不要硬搶焦點。
- 不碰使用者既有視窗。Kate 用 `kate --new`、Dolphin 用 `dolphin --new-window`；
  先記錄 `_NET_CLIENT_LIST_STACKING`，只操作自己開的 app。
- `DESKTOP_DRYRUN=1` 或 `--dry-run` 先看計畫；dry-run 不建 Runtime、不送 XTEST、
  不寫 clipboard。危險鍵在 dry-run 也會拒絕。
- **exit 0 不是驗收**。helper 回 `verified=False` 代表它沒讀到目標；要另一次
  `shot` 目視，或用 `--verify-file`／`cat` 比對檔案。receipt 的
  `effect=unknown` 也不能當成功。
- **真輸入前先請使用者讓出滑鼠／鍵盤**；不要假設一般「可以跑」涵蓋桌面控制。

## 輸入路徑

| 內容 | 路徑 |
|---|---|
| ASCII | `input.text`（`helper.py type`） |
| 中文 | **一律 clipboard**（`helper.py paste` 或 `type` 預設） |
| 除錯 | `--ax-debug` + `--role/--name` 才嘗試 `ax.set_text`；不建議使用 |

不可以用 `input.text` 打中文。`--ax-debug` 只供除錯，不是中文的主路徑。

## 剪貼簿格式

clipboard 文字會還原，但 **TARGETS／格式可能不完整**。helper 會回
`clipboard_restored`、`formats_preserved`、`clipboard_formats_before/after`、
`clipboard_warning` 與 `clipboard_owner_pid`。若出現
`WARN 格式剩 N 項，請重新複製一次以徹底回復`，不要說「已完全還原」；只說
`clipboard_restored=true` 與 `formats_preserved=false/true`。非文字剪貼簿回
`CLIPBOARD_NOT_TEXT`，不要覆蓋。

## AX 除錯（不是定位來源）

v0.1.2 不支援 `max_nodes`／`max_depth`；helper 先要求（預設 400/8），收到
`AX capture limits are WIP` 後用 `timeout_ms=2000/4000` fallback。`--app` 是事後
過濾，capture 仍是 desktop-wide。`ax` 的輸出與 summary 都會標 `debug_only=True`。

`ax` 的 `depth` 分級：

- `frame-only`：只有 application + frame/window/dialog。
- `chrome-only`：有 menu_bar／tool_bar／filler／panel 等，但沒有 editable 的
  textbox/text。
- `controls`：至少有一個 `editable=True` 的節點。
- `empty`：只有 application 或空。

`depth=ok` 不代表有 editable；新 helper 不再使用 `ok`，只有 `controls` 才可能有
可編輯控制項。`chrome-only` 時可以用 menu/toolbar 的 AX 點擊（除錯），但打字
要用截圖座標或 clipboard。不要說「AX 可用」；要說「partial、menu/toolbar 有、
editable 沒有」。

## 典型流程

```bash
.venv/bin/python helper.py shot
# 看截圖，決定座標
.venv/bin/python helper.py click --app kate --x 500 --y 300
.venv/bin/python helper.py type "abc" --app kate
.venv/bin/python helper.py key s --modifiers ctrl --app kate
.venv/bin/python helper.py shot   # 用截圖驗證
# 中文：
.venv/bin/python helper.py paste "桌面測試" --app kate
.venv/bin/python helper.py key s --modifiers ctrl --app kate
# 若有 --verify-file，type/paste 會直接讀檔檢查
```

## 驗收

- `helper.py type`／`paste` 的 `verified=False` 只代表 helper 沒能讀到目標，
  不代表文字有進去；**exit 0 不是驗收**。
- 驗收方式：另一次 `helper.py shot` 目視，或 `--verify-file PATH`，或
  `cat`／`xxd` 比對檔案。
- Kate 會補尾端換行；用 contains，不要 exact match。
- `formats_preserved=false` 時照 helper 的 warning 說「格式剩 N 項」，
  不要說「已完全還原」。

## 錯誤與下一步

- `APP_NOT_ACTIVE`：先讓自己的 app 取得前景，再重試；不要硬搶。
- `AX_DEBUG_ONLY`：AX 定位只供除錯；主路徑改用 `click --x/--y` 或 keyboard。
- `APP_NOT_FOUND` / `APP_AMBIGUOUS`：這是 AX 除錯路徑；主路徑用 `shot` 與座標。
- `TARGET_NOT_FOUND` / `TARGET_AMBIGUOUS` / `AX_EMPTY`：AX 除錯路徑；改用
  `shot` + `click --x/--y`。
- `CLIPBOARD_NOT_TEXT`：不要用 `paste`；改用 `type`（ASCII）或截圖。
- `CLIPBOARD_WRITE_FAILED` / `CLIPBOARD_RESTORE_FAILED`：剪貼簿沒有成功寫入／
  還原；回報真實原因，不要假裝成功。
- `VERIFY_FAILED`：檔案沒出現預期文字；先確認 app 有存檔
  （例如 `key s --modifiers ctrl`）。
- `DANGEROUS_KEY_REFUSED`：換安全按鍵；不要嘗試繞過。

## 已知限制

- **Kate 存檔會補尾端換行。** B2 觀察：`alpha` → `alpha\n`；精確字串比對要改用
  contains。
- **Dolphin 雙擊進目錄目前做不到。** `click --count 2` 只選取、不進入；不要把它
  當成可完成能力；沒有可靠替代路徑就記失敗。

## 10 次任務量測欄位

每次任務至少記：`path_used`、`ax_timeout_ms`、`one_shot`、`failure_reason`、
`latency_ms`。helper 會自動寫到 `.logs/actions-YYYYMMDD.jsonl`；回報時直接引用
這些欄位，不要用猜的。
