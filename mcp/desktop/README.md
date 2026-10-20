# mcp/desktop — OpenGCUA Linux X11 adapter

把 [OpenGCUA](https://github.com/Tongyi-MAI/OpenGCUA) 的 `qwen-intelligence-sdk`
v0.1.2 Linux X11 adapter 裝在本機，並提供一支 smoke 腳本。
本目錄目前**不是** MCP server；「動」只有 smoke 內部的受控 XTEST 測試。

- 看：`ui.vision.observe()` 抓 X11 畫面。
- 讀：`ui.ax.observe()` 透過 AT-SPI 取無障礙節點樹。
- 動：只對腳本自己開的 Kate 視窗送 XTEST；每次輸入前都過前景 gate。

## 目錄

```text
mcp/desktop/
├── README.md
├── scripts/smoke.py     # --phase see / read / act / all
├── .venv/               # host Python 3.12（不進版控）
├── .logs/               # 每次 smoke 的 JSONL（不進版控）
└── qwen_intelligence_sdk-0.1.2-*.whl
```

## 安裝

系統依賴（Ubuntu 22.04 X11）：

```bash
sudo apt-get install -y python3-venv python3-pyatspi python3-gi \
  gir1.2-atspi-2.0 libxcb1 libx11-6 xclip
```

建立 host 環境並安裝 wheel：

```bash
cd ~/workspace/agent/openchamber-lab/mcp/desktop
uv venv --python 3.12 .venv

curl -L -o SHA256SUMS \
  https://github.com/Tongyi-MAI/OpenGCUA/releases/download/v0.1.2/SHA256SUMS
curl -L -o qwen_intelligence_sdk-0.1.2-cp312-cp312-manylinux_2_28_x86_64.whl \
  https://github.com/Tongyi-MAI/OpenGCUA/releases/download/v0.1.2/qwen_intelligence_sdk-0.1.2-cp312-cp312-manylinux_2_28_x86_64.whl
grep 'cp312-cp312-manylinux' SHA256SUMS | sha256sum -c -

uv pip install --python .venv/bin/python \
  ./qwen_intelligence_sdk-0.1.2-cp312-cp312-manylinux_2_28_x86_64.whl
```

建立 worker 私有環境（官方指令）：

```bash
export DISPLAY=:0
export XAUTHORITY="$HOME/.Xauthority"
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus"

.venv/bin/mu setup linux --python /usr/bin/python3
.venv/bin/mu doctor linux
```

`mu setup linux` 會：

1. 把 worker 檔與 `requirements.lock` 複製到 `~/.cache/mai-ui/linux/0.3/`。
2. 用 `/usr/bin/python3 -m venv --system-site-packages` 建立
   `~/.cache/mai-ui/linux/0.3/venv`。
3. 只在該 venv 內安裝 `python-xlib==0.33`、`six`、`Pillow==11.3.0`。
   不會把套件裝進系統 Python。

`mu doctor linux` 的 `state` 應為 `ready`，且 `checks.accessibility.state`
應為 `ready`。

## 兩個 Python 的分工

| 角色 | 路徑 | Python | 用途 |
|---|---|---|---|
| host | `mcp/desktop/.venv` | 3.12（uv 管理） | 執行 SDK、Rust runtime 與 `mu` CLI；wheel 是 cp312 |
| worker | `~/.cache/mai-ui/linux/0.3/venv` | 系統 3.10 | X11 擷取與 AT-SPI；`--system-site-packages` 取得 `pyatspi` / `gi`，再裝 `python-xlib`、`six`、`Pillow` |

**不要**在 `mu doctor` 或 `add_linux()` 傳 `--python /usr/bin/python3`。
系統 Python 沒有 `Xlib`，`mu doctor linux --python /usr/bin/python3` 會以
`SETUP_FAILED: No module named 'Xlib'`、`state: needs_setup` 失敗（exit 1）。
worker 的預設 interpreter 就是上面的私有 venv，讓它保持預設即可。

## 為什麼鎖 v0.1.2

- 這個版本提供 `cp312` manylinux wheel，安裝時不需要 Rust/Cargo。
- 本目錄的 smoke 是在 v0.1.2（SDK `api_version` 0.3、worker protocol 0.3）上驗證的。
- wheel 以 `SHA256SUMS` 核對後才安裝；升級版本時應重跑 `--phase see` 與
  `--phase read`，不要假設 API 不變。

## 跑 smoke

```bash
cd ~/workspace/agent/openchamber-lab/mcp/desktop

.venv/bin/python scripts/smoke.py --phase see
.venv/bin/python scripts/smoke.py --phase read
# 只讀 Kate／Dolphin，跳過 LibreOffice：
.venv/bin/python scripts/smoke.py --phase read --apps kate,dolphin
.venv/bin/python scripts/smoke.py --phase act     # 會送 XTEST，先讀下面警告
# see + read；不含 act
.venv/bin/python scripts/smoke.py --phase all
```

腳本會自己設定 `DISPLAY=:0`、`XAUTHORITY=$HOME/.Xauthority` 與
`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`，不依賴呼叫者的環境。

> **`--phase act` 會送真正的 XTEST 點擊與鍵盤事件。**
> 它只操作自己開的 Kate（`/tmp/opencode/desktop/act-kate.txt`）。每次輸入前都檢查
> `_NET_ACTIVE_WINDOW` 是 Kate、WM 標題含 `act-kate.txt`、
> `_NET_WM_STATE_FOCUSED`，且 Kate 是最後一個 `_NET_WM_WINDOW_TYPE_NORMAL`
> 視窗。Plasma panel 是 `DOCK`，通常會排在 `_NET_CLIENT_LIST_STACKING` 最後，
> 因此 log 會同時記 `kate_last_strict`（字面最後一個）與 `kate_last_normal`
> （最後一個 NORMAL 應用視窗）；gate 採用後者。原始 stacking 會完整寫進 log。
> gate 不通過就停止，不硬搶焦點。
>
> 執行時桌面必須空閒，不要同時操作鍵盤滑鼠；不要在別人的應用視窗上跑。act 不會碰
> 既有的 Dolphin、Konsole 或其他使用者視窗，結束只關自己開的 Kate。額外診斷的
> clipboard 路線會先保存系統剪貼簿、用完還原；非文字剪貼簿會拒絕
> （`CLIPBOARD_NOT_TEXT`），不會覆蓋。

輸出：

- `/tmp/opencode/desktop/see-<timestamp>.png`：整個 X11 畫面。
- `/tmp/opencode/desktop/ax-<timestamp>.txt`：完整 AX 文字（**可能含其他視窗內容**，
  只留在本機）。
- `/tmp/opencode/desktop/ax-windows-<timestamp>.txt`：由 AX 節點推出的視窗清單。
- `/tmp/opencode/desktop/ax-<app>-<timestamp>.txt`：Kate / Dolphin / LibreOffice
  子樹前 20 行。
- `mcp/desktop/.logs/smoke-<timestamp>.jsonl`：`add_linux`、`vision.observe`、
  `ax.observe`、`windows.list` 等呼叫的耗時與結果。
- `/tmp/opencode/desktop/windows-before-<timestamp>.json`、
  `windows-after-<timestamp>.json`：window guard 的 before／after 快照。
- act：`act-before-<timestamp>.png`、`act-after-en-<timestamp>.png`、
  `act-after-zh-<timestamp>.png`、`act-after-extra-<timestamp>.png`、
  `act-cursor-before.png`、`act-cursor-after.png`、
  `act-kate.txt`、`act-kate-en.txt`、`act-kate-zh.txt`、
  `act-kate-zh-ax.txt`、`act-kate-zh-clipboard.txt`、
  `act-ax-kate-<timestamp>.txt`（只含 Kate 節點摘要，不存整個 desktop AX 文字）、
  `mcp/desktop/.logs/act-<timestamp>.jsonl`。

`read` 不做任何點擊或鍵盤輸入，也不會關閉既有視窗。Kate 與 Dolphin 都是單一
實例：若 `pgrep -x kate` / `pgrep -x dolphin` 已有程序，腳本會跳過啟動，只沿用
既有視窗做 AX 讀取，不傳路徑、不切換資料夾，也不會改寫 `kate-scratch.txt`。
只有沒有既有實例時，才會啟動自己的 Kate（`--new` + scratch 檔）與 Dolphin
（`--new-window` + `/tmp/opencode/desktop`）。LibreOffice 仍以隔離的
`-env:UserInstallation` profile 啟動。

`read` 的 `--apps`（預設 `kate,dolphin,libreoffice`）決定要啟動／讀哪些 app。
被跳過的 app 會寫 `skipped（--apps 未包含）`，不列入「子樹非空」判定。

`read` 也有 window guard：

- 開 app 前寫 `windows-before-<timestamp>.json`，關掉自己開的 app 後寫
  `windows-after-<timestamp>.json`。快照來自 `_NET_CLIENT_LIST_STACKING`，每個
  id 記錄 `WM_CLASS`、`WM_NAME`、`_NET_WM_PID`。
- 每個 before id 都必須在 after 存在，且 `WM_CLASS`／`WM_NAME` 不變；before 沒有
  的 id 可以出現（那是自己開的），但自己的 app 必須在 after 前關掉。輸出
  `WINDOW-GUARD PASS`／`FAIL`；FAIL 會列出消失或改名的 id。
- 清理只用 `os.killpg(os.getpgid(proc.pid), SIGTERM)`（必要時 SIGKILL）關閉自己
  launch 的 process group，不使用 `pkill kate`／`pkill dolphin` 這類全域指令。

### AX 擷取限制與 fallback

Linux adapter 沒有 app-scoped AX；`--apps`（以及任何 `--app`）只是事後過濾
`application` 節點，不是 SDK 的 app scope。smoke 的主觀測要求
`max_nodes=1200, max_depth=10`，初始 warm-up 要求 `300/6`。但 v0.1.2 的
`AX.observe` 只要收到 `max_nodes` 或 `max_depth` 就回
`UNSUPPORTED_CAPABILITY: AX capture limits are WIP`；因此 smoke 會先照要求帶限制，
收到這個特定錯誤後記錄 `limits_unsupported`，改用 `timeout_ms=4000` 的相容
fallback（初始 `timeout_ms=2000`）。這是 timeout 上限，不是 2048／無上限；完整
全樹 7.5–13.5 秒不得成為任何路徑的預設值。未來 helper 的 `ax` 預設應使用更小的
`400/8`。

### 文字輸入的三條路徑

- ASCII → `ui.input.text`（Linux 只保證 group 1 unshifted/shifted 的 ASCII）。
- 中文 → `ui.ax.set_text` 首選；若沒有可觀察效果 → clipboard paste 兜底。
- 兜底 → clipboard paste，必須先 save、用完 restore。
- 不可以用 `ui.input.text` 打中文。

`ClipboardGuard` 用 `xclip -o -selection clipboard -t TARGETS` 判斷是否為文字；
沒有 `text/plain`／`UTF8_STRING` 就拒絕，回 `CLIPBOARD_NOT_TEXT`，不覆蓋。save
後在 `finally` 用 `xclip -i -selection clipboard` 還原；原本是空字串就還原成空。

`DESKTOP_DRYRUN=1 ... --phase act` 只印計畫（要開哪個 app、點哪裡、打什麼、按
什麼），不建立 Runtime、不送 XTEST、不寫 clipboard。所有 `press_key` 前會檢查
黑名單：`ctrl+alt+f1..f12`、`ctrl+alt+backspace`、`super+l`、`super+q`（大小寫與
`ctrl`/`control`、`super`/`meta`/`win` 正規化）。

`act` 的流程：開自己的 Kate → 動前截圖 → AX 找 Kate frame／editable 節點 →
前景 gate → 用動前影像產生 `PointRef` 點一次 → 輸入 `abc` → Ctrl+S → 存
`act-kate-en.txt` → 嘗試 `input.text("桌面測試")`（預期失敗）→ 額外診斷
`ax.set_text` 與 clipboard → 動後截圖與游標裁切 → 只關自己的 Kate。

## 已知限制（Linux adapter v0.1.2）

- **沒有 `device.list_windows` / `apps`。** `Device.windows` 與 `apps` 在 Linux
  是未實作的 namespace，`desktop.windows.list()` 會回
  `UNSUPPORTED_CAPABILITY: windows is WIP in the Python/Android release`。
  視窗清單只能從 AX 樹的 `frame` / `window` / `dialog` 節點往上找
  `application` 推導；AX 沒有 `app_id` / `window_id`。
- **AX 擷取有上限，且 v0.1.2 不支援 `max_nodes`／`max_depth`。** helper 最多回
  2048 個節點，超過就 `partial` / `truncated`；`AX.observe()` 只要帶
  `max_nodes` 或 `max_depth` 就會回
  `UNSUPPORTED_CAPABILITY: AX capture limits are WIP`。smoke 先要求限制，收到這個
  錯誤後改用 timeout fallback（主觀測 4000 ms、初始 2000 ms）；這不是 2048／
  無上限。桌面應用很多時，較晚出現的視窗仍可能不在快照裡。
- **`ui.input.text()` 只支援基本 keysym。** Linux 實作是逐字元轉 keysym，只涵蓋
  group 1 的 unshifted / shifted。實測 `"abc"` 成功；`"桌面測試"` 回
  `UNSUPPORTED_CAPABILITY: Key/text is not mapped by the active layout: '桌'`。
- **`ui.ax.set_text()` 對 Kate 沒有可觀察效果。** Kate 的 AX 樹只露出
  `visible=false`、沒有 bounds 的 textbox；對它呼叫 `ax.set_text("桌面測試")`
  回報 `execution: completed`、`error: null`，但 Ctrl+S 後檔案仍是原本的
  `abc`。不要把它當成中文輸入的可靠路徑。
- **clipboard 可以貼中文，但不算 `ui.input.text`。** 額外診斷用
  `clipboard.write({"text/plain": "桌面測試"})` + Ctrl+A / Ctrl+V / Ctrl+S，
  檔案可得到 `桌面測試`；這是繞過 `input.text` 的替代路線，不代表 keysym
  路徑支援中文。
- **XTEST 只支援前景輸入。** worker 對 `delivery="background"` 直接回
  `UNSUPPORTED_CAPABILITY: Linux input is foreground only`。
- **只支援 X11。** Wayland 不支援；`DISPLAY`、`XAUTHORITY` 與圖形 session 的
  D-Bus 必須指向同一個桌面。
- **AT-SPI 名稱可能落後 X11 標題。** 視窗標題已在 X11 改變時，AX 節點名稱不一定
  立即同步。

## 疑難排解

- `mu setup linux` 出現 `ensurepip is not available`：系統缺 `python3-venv`。
  安裝後重跑 `mu setup`（會重建 worker venv）。
- `mu doctor linux` 回 `needs_setup` 且 `checks.error` 是
  `No module named 'Xlib'`：doctor 被指向系統 Python。改用預設（私有 venv）。
- accessibility 不是 `ready`：確認 `python3-pyatspi`、`python3-gi`、
  `gir1.2-atspi-2.0` 已安裝，且 `DBUS_SESSION_BUS_ADDRESS` 指向圖形 session 的
  使用者 bus。
