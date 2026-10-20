#!/usr/bin/env python3
"""OpenGCUA Linux X11 adapter smoke：驗證「看」、「讀」與受控的「動」。

- see：ui.vision.observe()，存 PNG，並用 ImageMagick identify 檢查不是全黑。
- read：ui.ax.observe()，存 AX 文字、列出視窗、比對三個應用程式的子樹。
- act：只對本腳本自己開的 Kate 送 XTEST；每個輸入前都檢查前景 gate，
  目標座標只來自該視窗的 AX 節點或 frame bounds，不做全屏掃描。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path

OUT_DIR = Path("/tmp/opencode/desktop")
DESKTOP_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = DESKTOP_DIR / ".logs"

# 三個 smoke 應用程式；比對 application 節點名稱用（casefold）。
APP_MATCHERS = {
    "kate": ("kate",),
    "dolphin": ("dolphin",),
    "libreoffice": ("soffice", "libreoffice", "writer"),
}


def configure_environment() -> None:
    """不依賴呼叫者：自己指定 X11 與使用者 session bus。"""
    os.environ["DISPLAY"] = ":0"
    os.environ["XAUTHORITY"] = str(Path.home() / ".Xauthority")
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/1000/bus"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


class Recorder:
    """每次 SDK 呼叫一行 JSONL。"""

    def __init__(self, stamp: str, prefix: str = "smoke") -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOG_DIR / f"{prefix}-{stamp}.jsonl"

    def add(self, phase: str, call: str, ms: float, ok: bool, **extra) -> None:
        row = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "phase": phase,
            "call": call,
            "ms": round(ms, 1),
            "ok": ok,
        }
        row.update(extra)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


async def timed(rec: Recorder, phase: str, call: str, awaitable, **extra):
    started = time.perf_counter()
    try:
        value = await awaitable
    except Exception as exc:  # noqa: BLE001 - 原樣記錄後重新拋出
        ms = (time.perf_counter() - started) * 1000
        rec.add(phase, call, ms, False, code=getattr(exc, "code", None), error=str(exc), **extra)
        raise
    ms = (time.perf_counter() - started) * 1000
    rec.add(phase, call, ms, True, **extra)
    return value, ms


def screen_dimensions() -> tuple[int, int] | None:
    proc = subprocess.run(
        ["xdpyinfo"], capture_output=True, text=True, env=os.environ, timeout=30
    )
    if proc.returncode != 0:
        print(f"xdpyinfo 失敗（exit {proc.returncode}）：{proc.stderr.strip()}")
        return None
    match = re.search(r"dimensions:\s+(\d+)x(\d+)", proc.stdout)
    if not match:
        print("xdpyinfo 輸出找不到 dimensions:")
        return None
    return int(match.group(1)), int(match.group(2))


def identify(path: Path) -> str:
    proc = subprocess.run(
        [
            "identify",
            "-format",
            "%wx%h mean=%[fx:mean] min=%[fx:minima] max=%[fx:maxima] size=%b\n",
            str(path),
        ],
        capture_output=True,
        text=True,
        env=os.environ,
        timeout=60,
    )
    if proc.returncode != 0:
        return f"identify 失敗（exit {proc.returncode}）：{proc.stderr.strip()}"
    return (proc.stdout + proc.stderr).strip()


def launch_apps(apps: set[str]) -> list[tuple[str, subprocess.Popen]]:
    """只啟動 `apps` 指定的程式；既有 Dolphin/Kate 只讀不啟動。

    Dolphin 與 Kate 都是單一實例：直接帶路徑啟動會沿用既有視窗，可能切換資料夾
    或改寫正在看的檔案。因此先用 `pgrep -x` 偵測；已有實例就跳過啟動，僅讓後續
    AX 讀取使用既有視窗。沒有既有實例時，Kate 用 `--new` 強制新實體、Dolphin 用
    `--new-window` 強制新視窗；LibreOffice 仍用隔離的 UserInstallation profile。
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    def running(name: str) -> bool:
        # pgrep -x 只比對程序名稱，避免誤抓命令列含 kate/dolphin 的其他程序。
        return (
            subprocess.run(
                ["pgrep", "-x", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )

    env = {
        **os.environ,
        "DISPLAY": ":0",
        "XAUTHORITY": str(Path.home() / ".Xauthority"),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
    }
    commands: list[tuple[str, list[str]]] = []

    if "kate" not in apps:
        print("Kate：skipped（--apps 未包含）")
    elif running("kate"):
        print("Kate 已在執行；跳過啟動，僅使用既有視窗做 AX 讀取")
    else:
        scratch = OUT_DIR / "kate-scratch.txt"
        scratch.write_text("OpenGCUA smoke AX probe\n第二行：只讀不點。\n", encoding="utf-8")
        commands.append(("kate", ["kate", "--new", str(scratch)]))

    if "dolphin" not in apps:
        print("Dolphin：skipped（--apps 未包含）")
    elif running("dolphin"):
        print("Dolphin 已在執行；跳過啟動，僅使用既有視窗做 AX 讀取")
    else:
        commands.append(("dolphin", ["dolphin", "--new-window", str(OUT_DIR)]))

    if "libreoffice" not in apps:
        print("LibreOffice：skipped（--apps 未包含）")
    else:
        commands.append(
            (
                "libreoffice",
                [
                    "soffice",
                    "--writer",
                    "--norestore",
                    "--nofirststartwizard",
                    f"-env:UserInstallation=file://{OUT_DIR}/lo-profile",
                ],
            )
        )

    launched: list[tuple[str, subprocess.Popen]] = []
    for label, argv in commands:
        try:
            proc = subprocess.Popen(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            print(f"啟動 {label} 失敗：{exc}")
            continue
        launched.append((label, proc))
        print(f"已啟動 {label}（pid={proc.pid}）：{' '.join(argv)}")
    return launched


def describe_node(node) -> str:
    bounds = node.bounds
    if bounds is None:
        where = "-"
    else:
        where = f"{int(bounds.x)},{int(bounds.y)} {int(bounds.width)}x{int(bounds.height)}"
    parts = [f"[{node.index}]", node.role]
    if node.name:
        parts.append(f"name={node.name!r}")
    if node.text:
        text = node.text.replace("\n", "\\n")
        parts.append(f"text={text[:80]!r}")
    parts.append(f"bounds={where}")
    return " ".join(parts)


def application_of(node, by_id):
    """往上找 role=application 的祖先（含自己）。Linux AX 沒有 app_id。"""
    seen = set()
    current = node
    while current is not None and current.id not in seen:
        seen.add(current.id)
        if current.role == "application":
            return current
        current = by_id.get(current.parent_id) if current.parent_id else None
    return None


def analyze_windows(ax) -> tuple[list[dict], dict[str, int]]:
    nodes = list(ax.tree.nodes)
    by_id = {node.id: node for node in nodes}
    windows: list[dict] = []
    for node in nodes:
        if node.role not in ("frame", "window", "dialog"):
            continue
        app = application_of(node, by_id)
        windows.append(
            {
                "app": app.name if app else None,
                "role": node.role,
                "title": node.name,
                "bounds": None
                if node.bounds is None
                else [
                    int(node.bounds.x),
                    int(node.bounds.y),
                    int(node.bounds.width),
                    int(node.bounds.height),
                ],
            }
        )

    per_app: dict[str, int] = {}
    for label in APP_MATCHERS:
        per_app[label] = 0
    for label, keywords in APP_MATCHERS.items():
        apps = [
            node
            for node in nodes
            if node.role == "application"
            and any(key in (node.name or "").casefold() for key in keywords)
        ]
        for app in apps:
            per_app[label] += sum(1 for node in nodes if application_of(node, by_id) is app)
    return windows, per_app


def write_app_subtree(label: str, ax, stamp: str, by_id) -> tuple[Path, list[str], bool]:
    nodes = list(ax.tree.nodes)
    keywords = APP_MATCHERS[label]
    apps = [
        node
        for node in nodes
        if node.role == "application"
        and any(key in (node.name or "").casefold() for key in keywords)
    ]
    lines: list[str] = []
    non_empty = False
    if not apps:
        lines.append(f"{label}: 空（AX 樹中找不到 application 節點）")
    else:
        for app in apps:
            subtree = [node for node in nodes if application_of(node, by_id) is app]
            lines.append(f"app={app.name!r} subtree_nodes={len(subtree)}")
            if len(subtree) <= 1:
                lines.append(f"app={app.name!r} 空（僅 application 節點，無子節點）")
                continue
            non_empty = True
            for node in subtree[:20]:
                lines.append(describe_node(node))
    path = OUT_DIR / f"ax-{label}-{stamp}.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, lines, non_empty


# --- act phase helpers：只操作自己開的 Kate 視窗 ---------------------------------


def _run(argv, timeout=20):
    return subprocess.run(argv, capture_output=True, text=True, env=os.environ, timeout=timeout)


def _xprop(argv):
    proc = _run(argv)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv)} 失敗（exit {proc.returncode}）：{proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _window_ids(text):
    match = re.search(r"window id # (.*)$", text)
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def active_window():
    ids = _window_ids(_xprop(["xprop", "-root", "_NET_ACTIVE_WINDOW"]))
    return ids[0] if ids else None


def stacking_windows():
    return _window_ids(_xprop(["xprop", "-root", "_NET_CLIENT_LIST_STACKING"]))


def window_props(wid):
    return _xprop(
        [
            "xprop",
            "-id",
            wid,
            "_NET_WM_NAME",
            "WM_NAME",
            "WM_CLASS",
            "_NET_WM_WINDOW_TYPE",
            "_NET_WM_STATE",
            "_NET_WM_PID",
        ]
    )


def prop_value(props, name):
    for line in props.splitlines():
        if line.startswith(name + "("):
            return line.split(" = ", 1)[1].strip() if " = " in line else ""
    return ""


def window_title(props):
    for key in ("_NET_WM_NAME", "WM_NAME"):
        value = prop_value(props, key)
        if value:
            return value.strip('"')
    return ""


def is_normal_window(props):
    types = prop_value(props, "_NET_WM_WINDOW_TYPE")
    return "NORMAL" in types and "DOCK" not in types and "DESKTOP" not in types


def is_focused(props):
    return "FOCUSED" in prop_value(props, "_NET_WM_STATE")


def snapshot_windows(path: Path) -> list[dict]:
    """記錄目前所有 managed window 的 id／WM_CLASS／WM_NAME／_NET_WM_PID。"""
    windows: list[dict] = []
    for wid in stacking_windows():
        try:
            props = window_props(wid)
            windows.append(
                {
                    "id": wid,
                    "wm_class": prop_value(props, "WM_CLASS"),
                    "wm_name": prop_value(props, "WM_NAME"),
                    "net_wm_name": prop_value(props, "_NET_WM_NAME"),
                    "pid": prop_value(props, "_NET_WM_PID"),
                    "types": prop_value(props, "_NET_WM_WINDOW_TYPE"),
                }
            )
        except Exception as exc:  # noqa: BLE001 - 單一視窗查詢失敗仍要留下紀錄
            windows.append({"id": wid, "error": f"{type(exc).__name__}: {exc}"})
    data = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "windows": windows,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return windows


def compare_windows(before: list[dict], after: list[dict]) -> dict:
    """before 的每個 id 都必須存在，且 WM_CLASS／WM_NAME 不變。"""
    after_by_id = {window["id"]: window for window in after}
    before_ids = {window["id"] for window in before}
    missing = [window for window in before if window["id"] not in after_by_id]
    changed = []
    for window in before:
        current = after_by_id.get(window["id"])
        if current is None:
            continue
        diffs = {}
        for key in ("wm_class", "wm_name"):
            if current.get(key) != window.get(key):
                diffs[key] = {"before": window.get(key), "after": current.get(key)}
        if diffs:
            changed.append({"id": window["id"], "diffs": diffs})
    new = [window for window in after if window["id"] not in before_ids]
    return {
        "ok": not missing and not changed,
        "missing": missing,
        "changed": changed,
        "new": new,
    }


def _process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def close_launched(launched: list[tuple[str, subprocess.Popen]], timeout: float = 2.0) -> dict:
    """只關閉本腳本 launch 的 process group；不用全域 pkill。"""
    details = [
        {
            "label": label,
            "pid": proc.pid,
            "pgid": None,
            "term_sent": False,
            "kill_sent": False,
            "exited": False,
            "group_alive": None,
        }
        for label, proc in launched
    ]
    for detail, (_, proc) in zip(details, launched):
        try:
            detail["pgid"] = os.getpgid(proc.pid)
        except ProcessLookupError:
            # start_new_session=True：session leader 的 PGID 就是它的 PID；
            # 即使 leader 先退出，子程序仍留在同一個 process group。
            detail["pgid"] = proc.pid

    for detail in details:
        if detail["pgid"] is None:
            continue
        try:
            os.killpg(detail["pgid"], signal.SIGTERM)
            detail["term_sent"] = True
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + timeout
    for detail, (_, proc) in zip(details, launched):
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass

    for detail in details:
        if detail["pgid"] is None:
            continue
        if _process_group_alive(detail["pgid"]):
            try:
                os.killpg(detail["pgid"], signal.SIGKILL)
                detail["kill_sent"] = True
            except ProcessLookupError:
                pass

    deadline = time.monotonic() + 2.0
    for detail, (_, proc) in zip(details, launched):
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass

    for detail in details:
        alive = detail["pgid"] is not None and _process_group_alive(detail["pgid"])
        detail["group_alive"] = alive
        detail["exited"] = not alive
    return {"ok": all(detail["exited"] for detail in details), "apps": details}


def foreground_gate(kate_id: str) -> dict:
    """前景 gate：Kate 必須是 active、focused、且是最後一個 NORMAL 視窗。

    Plasma 的 panel/dock 一定會排在 stacking 最後，因此 gate 以「最後一個
    _NET_WM_WINDOW_TYPE_NORMAL 視窗」判定；原始 stacking 會完整記錄。
    """
    started = time.perf_counter()
    active = active_window()
    stacking = stacking_windows()
    kate_props = window_props(kate_id)
    normal = []
    for wid in stacking:
        try:
            if is_normal_window(window_props(wid)):
                normal.append(wid)
        except Exception:
            continue
    result = {
        "ms": round((time.perf_counter() - started) * 1000, 1),
        "active": active,
        "kate_id": kate_id,
        "title": window_title(kate_props),
        "stacking": stacking,
        "normal": normal,
        "kate_last_normal": bool(normal) and normal[-1] == kate_id,
        "kate_last_strict": bool(stacking) and stacking[-1] == kate_id,
        "focused": is_focused(kate_props),
    }
    result["ok"] = (
        result["active"] == kate_id
        and result["kate_last_normal"]
        and result["focused"]
        and "act-kate.txt" in result["title"]
    )
    return result


def log_gate(rec: Recorder, label: str, gate: dict) -> None:
    rec.add(
        "act",
        f"gate.{label}",
        gate["ms"],
        gate["ok"],
        active=gate["active"],
        kate_id=gate["kate_id"],
        title=gate["title"],
        stacking=gate["stacking"],
        normal=gate["normal"],
        kate_last_normal=gate["kate_last_normal"],
        kate_last_strict=gate["kate_last_strict"],
        focused=gate["focused"],
    )
    last_normal = gate["normal"][-1] if gate["normal"] else None
    print(
        f"[gate {label}] ok={gate['ok']} active={gate['active']} kate={gate['kate_id']} "
        f"title={gate['title']!r} focused={gate['focused']} "
        f"last_normal={last_normal} kate_last_normal={gate['kate_last_normal']} "
        f"strict_last={gate['kate_last_strict']}"
    )
    if not gate["ok"]:
        raise RuntimeError(
            "前景 gate 不通過，停止送出任何 XTEST："
            f"active={gate['active']} kate={gate['kate_id']} "
            f"kate_last_normal={gate['kate_last_normal']} focused={gate['focused']} "
            f"title={gate['title']!r} stacking={gate['stacking']}"
        )


def receipt_dict(receipt):
    if dataclasses.is_dataclass(receipt):
        return dataclasses.asdict(receipt)
    if hasattr(receipt, "__dict__"):
        return dict(receipt.__dict__)
    return {"repr": repr(receipt)}


def receipt_error(info):
    return info.get("error") if isinstance(info, dict) else None


async def gated_receipt(rec, kate_id, label, func, *args, phase="act", **kwargs):
    """先過前景 gate，再送 XTEST；回傳 (receipt dict, ms)。"""
    gate = foreground_gate(kate_id)
    log_gate(rec, label, gate)
    receipt, ms = await timed(rec, phase, label, func(*args, **kwargs))
    info = receipt_dict(receipt)
    rec.add(phase, f"{label}.receipt", 0.0, receipt_error(info) is None, receipt=info)
    return info, ms


def pgrep_exact(name: str) -> list[str]:
    proc = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def launch_kate(path: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "DISPLAY": ":0",
        "XAUTHORITY": str(Path.home() / ".Xauthority"),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
    }
    return subprocess.Popen(
        ["kate", str(path)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def find_window_with_title(fragment: str, timeout: float = 20.0) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for wid in stacking_windows():
                props = window_props(wid)
                if fragment in window_title(props) and "kate" in props:
                    return wid
        except Exception:
            pass
        time.sleep(0.5)
    return None


def node_children(nodes):
    children = defaultdict(list)
    for node in nodes:
        if node.parent_id:
            children[node.parent_id].append(node)
    return children


def subtree_of(root, children):
    ordered, queue, seen = [], [root], set()
    while queue:
        node = queue.pop(0)
        if node.id in seen:
            continue
        seen.add(node.id)
        ordered.append(node)
        queue.extend(children.get(node.id, []))
    return ordered


def bounds_dict(bounds):
    if bounds is None:
        return None
    return {"x": bounds.x, "y": bounds.y, "width": bounds.width, "height": bounds.height}


async def capture_vision(rec, phase, label, device, stamp, filename):
    observation, ms = await timed(rec, phase, label, device.ui.vision.observe())
    print(f"{label} {ms:.1f} ms status={observation.status} images={len(observation.images)}")
    if not observation.images:
        raise RuntimeError(f"{label} 沒有回傳影像")
    path = OUT_DIR / filename
    for index, image in enumerate(observation.images):
        data = await image.read()
        target = path if index == 0 else OUT_DIR / f"{path.stem}-{index}.png"
        target.write_bytes(data)
        print(f"  {target} {image.width}x{image.height} bytes={len(data)}")
    return observation, path


def crop_around(path_in: Path, path_out: Path, px: int, py: int, size: int = 80) -> None:
    out = subprocess.run(
        ["identify", "-format", "%w %h", str(path_in)],
        capture_output=True,
        text=True,
        env=os.environ,
        timeout=30,
    )
    width, height = (int(value) for value in out.stdout.split())
    x0 = max(0, px - size // 2)
    y0 = max(0, py - size // 2)
    w = min(size, width - x0)
    h = min(size, height - y0)
    subprocess.run(
        ["convert", str(path_in), "-crop", f"{w}x{h}+{x0}+{y0}", "+repage", str(path_out)],
        check=True,
        env=os.environ,
    )
    print(f"裁切 {path_in.name} -> {path_out.name}（{w}x{h}+{x0}+{y0}）")


def compare_ae(path_a: Path, path_b: Path) -> str:
    proc = subprocess.run(
        ["compare", "-metric", "AE", str(path_a), str(path_b), "null:"],
        capture_output=True,
        text=True,
        env=os.environ,
    )
    value = (proc.stderr or proc.stdout).strip()
    # ImageMagick 6 以 stderr 回報；有差異時 exit=1 屬正常。
    print(f"compare -metric AE {path_a.name} {path_b.name} -> {value}（exit {proc.returncode}）")
    return value


async def observe_ax(
    rec: Recorder,
    phase: str,
    label: str,
    device,
    *,
    timeout_ms: int,
    max_nodes: int,
    max_depth: int,
    fallback_timeout_ms: int,
):
    """先要求 max_nodes/max_depth；v0.1.2 不支援時，用有界的 timeout fallback。

    已確認 v0.1.2 的 `AX.observe` 只要收到 max_nodes 或 max_depth 就會立刻丟
    `UNSUPPORTED_CAPABILITY: AX capture limits are WIP`。這是安裝版本的實際限制；
    fallback 只用 timeout_ms 限制擷取時間，不要求 2048 或無上限。
    """
    from mai_ui import MuError

    try:
        observation, ms = await timed(
            rec,
            phase,
            label,
            device.ui.ax.observe(
                timeout_ms=timeout_ms, max_nodes=max_nodes, max_depth=max_depth
            ),
            max_nodes=max_nodes,
            max_depth=max_depth,
        )
        return observation, ms, False
    except MuError as exc:
        if exc.code != "UNSUPPORTED_CAPABILITY" or "AX capture limits are WIP" not in str(exc):
            raise
        rec.add(
            phase,
            f"{label}.limits_unsupported",
            0.0,
            False,
            code=exc.code,
            error=str(exc),
            requested_max_nodes=max_nodes,
            requested_max_depth=max_depth,
        )
        print(
            f"{label}: v0.1.2 不支援 max_nodes/max_depth（{exc.code}: {exc}）；"
            f"改用 timeout_ms={fallback_timeout_ms} 的相容 fallback"
        )
        observation, ms = await timed(
            rec,
            phase,
            f"{label}.fallback",
            device.ui.ax.observe(timeout_ms=fallback_timeout_ms),
            fallback_timeout_ms=fallback_timeout_ms,
        )
        return observation, ms, True


class ClipboardNotText(RuntimeError):
    """剪貼簿不是純文字；拒絕覆蓋。"""


class ClipboardGuard:
    """保存／還原純文字剪貼簿；非文字剪貼簿拒絕使用。"""

    TEXT_ATOMS = {
        "UTF8_STRING",
        "text/plain",
        "text/plain;charset=utf-8",
        "STRING",
        "TEXT",
    }

    def __init__(self):
        self.text: str | None = None
        self.saved = False

    def save(self) -> None:
        targets = subprocess.run(
            ["xclip", "-o", "-selection", "clipboard", "-t", "TARGETS"],
            capture_output=True,
            text=True,
            env=os.environ,
            timeout=10,
        )
        atoms = {line.strip() for line in targets.stdout.splitlines() if line.strip()}
        if targets.returncode != 0 and not atoms:
            # 空剪貼簿：視為空字串，restore() 會還原成空。
            self.text = ""
            self.saved = True
            return
        if not (atoms & self.TEXT_ATOMS):
            raise ClipboardNotText(
                "CLIPBOARD_NOT_TEXT: clipboard TARGETS 不含 text/plain 或 UTF8_STRING"
            )
        read = subprocess.run(
            ["xclip", "-o", "-selection", "clipboard"],
            capture_output=True,
            text=True,
            env=os.environ,
            timeout=10,
        )
        if read.returncode != 0:
            raise ClipboardNotText(
                "CLIPBOARD_NOT_TEXT: 無法讀取文字剪貼簿"
                f"（exit {read.returncode}）：{read.stderr.strip()}"
            )
        self.text = read.stdout
        self.saved = True

    def restore(self) -> None:
        if not self.saved:
            return
        subprocess.run(
            ["xclip", "-i", "-selection", "clipboard"],
            input=self.text,
            text=True,
            env=os.environ,
            timeout=10,
            check=True,
        )
        self.saved = False


def _normalized_modifiers(modifiers) -> set[str]:
    normalized = set()
    for modifier in modifiers or ():
        value = modifier.lower()
        if value in ("ctrl", "control"):
            normalized.add("ctrl")
        elif value in ("super", "meta", "win", "windows"):
            normalized.add("super")
        else:
            normalized.add(value)
    return normalized


def reject_dangerous_key(key: str, modifiers) -> None:
    """拒絕黑名單組合；大小寫與 ctrl/control、super/meta/win 正規化。"""
    normalized_key = key.lower()
    mods = _normalized_modifiers(modifiers)
    combo = "+".join(sorted(mods | {normalized_key}))
    if {"ctrl", "alt"} <= mods and normalized_key in {f"f{index}" for index in range(1, 13)}:
        raise RuntimeError(f"DANGEROUS_KEY_REFUSED: {combo}")
    if {"ctrl", "alt"} <= mods and normalized_key in ("backspace", "back_space"):
        raise RuntimeError(f"DANGEROUS_KEY_REFUSED: {combo}")
    if "super" in mods and normalized_key in ("l", "q"):
        raise RuntimeError(f"DANGEROUS_KEY_REFUSED: {combo}")


async def press_key_checked(device, key: str, *, modifiers=()):
    reject_dangerous_key(key, modifiers)
    return await device.ui.input.press_key(key, modifiers=list(modifiers))


async def run_see(rec: Recorder, stamp: str) -> dict:
    from mai_ui import Runtime

    result = {"phase": "see", "ok": False, "errors": []}
    print("=== phase see：ui.vision.observe ===")
    try:
        async with Runtime() as runtime:
            info, ms = await timed(
                rec,
                "see",
                "add_linux",
                runtime.add_linux(
                    name="desktop",
                    display=":0",
                    xauthority=str(Path.home() / ".Xauthority"),
                ),
            )
            print(f"add_linux {ms:.1f} ms -> {info.id}")

            session = runtime.session()
            device, ms = await timed(
                rec, "see", "register", session.register(info, name="desktop")
            )
            print(f"register {ms:.1f} ms -> {device.resource_id}")

            observation, ms = await timed(
                rec, "see", "vision.observe", device.ui.vision.observe()
            )
            print(
                f"vision.observe {ms:.1f} ms status={observation.status} "
                f"images={len(observation.images)}"
            )
            if not observation.images:
                raise RuntimeError("vision.observe 沒有回傳任何影像")

            saved: list[Path] = []
            for index, image in enumerate(observation.images):
                data = await image.read()
                suffix = "" if index == 0 else f"-{index}"
                path = OUT_DIR / f"see-{stamp}{suffix}.png"
                path.write_bytes(data)
                saved.append(path)
                print(
                    f"影像 {path}：{image.width}x{image.height} "
                    f"bytes={len(data)} mime={image.mime_type} label={image.label!r}"
                )
                print(f"identify：{identify(path)}")

            primary = saved[0]
            dims = screen_dimensions()
            identify_text = identify(primary)
            mean_match = re.search(r"mean=([0-9.eE+-]+)", identify_text)
            mean = float(mean_match.group(1)) if mean_match else None
            if dims is not None:
                print(f"xdpyinfo dimensions：{dims[0]}x{dims[1]}")
                if observation.images[0].width != dims[0] or observation.images[0].height != dims[1]:
                    raise RuntimeError(
                        "截圖尺寸與 xdpyinfo 不一致："
                        f"{observation.images[0].width}x{observation.images[0].height} != {dims}"
                    )
            else:
                result["errors"].append("xdpyinfo 無法取得 dimensions，解析度未驗證")
                print("警告：xdpyinfo 無法取得 dimensions，解析度未驗證")
            if mean is None:
                raise RuntimeError(f"無法從 identify 輸出解析 mean：{identify_text}")
            if mean == 0:
                raise RuntimeError("截圖 mean=0（全黑）")

            result.update(
                ok=True,
                image=str(primary),
                width=observation.images[0].width,
                height=observation.images[0].height,
                bytes=primary.stat().st_size,
                mean=mean,
                identify=identify_text,
                screen=dims,
            )
    except Exception as exc:  # noqa: BLE001 - 保留原始錯誤
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        print("see 失敗：")
        traceback.print_exc()
    return result


async def run_read(rec: Recorder, stamp: str, apps: set[str]) -> dict:
    from mai_ui import MuError, Runtime

    result = {"phase": "read", "ok": False, "errors": [], "apps_requested": sorted(apps)}
    print("=== phase read：ui.ax.observe + windows.list ===")
    before_path = OUT_DIR / f"windows-before-{stamp}.json"
    after_path = OUT_DIR / f"windows-after-{stamp}.json"
    result["windows_before_file"] = str(before_path)
    result["windows_after_file"] = str(after_path)

    try:
        before = snapshot_windows(before_path)
        print(f"WINDOW-SNAPSHOT before: {before_path}（{len(before)} windows）")
    except Exception as exc:  # noqa: BLE001 - 沒有 before snapshot 就不能做 guard
        result["errors"].append(f"snapshot before: {type(exc).__name__}: {exc}")
        print(f"WINDOW-SNAPSHOT before 失敗：{type(exc).__name__}: {exc}")
        return result

    launched: list[tuple[str, subprocess.Popen]] = []
    try:
        async with Runtime() as runtime:
            info, ms = await timed(
                rec,
                "read",
                "add_linux",
                runtime.add_linux(
                    name="desktop",
                    display=":0",
                    xauthority=str(Path.home() / ".Xauthority"),
                ),
            )
            print(f"add_linux {ms:.1f} ms -> {info.id}")

            session = runtime.session()
            device, ms = await timed(
                rec, "read", "register", session.register(info, name="desktop")
            )
            print(f"register {ms:.1f} ms -> {device.resource_id}")

            # 先 observe 一次，把可被 kill 的 AT-SPI helper 拉起來。
            try:
                baseline, ms, used_fallback = await observe_ax(
                    rec,
                    "read",
                    "ax.observe.initial",
                    device,
                    timeout_ms=20000,
                    max_nodes=300,
                    max_depth=6,
                    fallback_timeout_ms=2000,
                )
                result["initial_ax"] = {
                    "ms": round(ms, 1),
                    "nodes": len(baseline.tree.nodes),
                    "status": baseline.tree.status,
                    "truncated": baseline.tree.truncated,
                    "limits_fallback": used_fallback,
                }
                print(
                    f"初始 ax.observe {ms:.1f} ms nodes={len(baseline.tree.nodes)} "
                    f"status={baseline.tree.status} truncated={baseline.tree.truncated} "
                    f"limits_fallback={used_fallback}"
                )
            except MuError as exc:
                print(f"初始 ax.observe 失敗（仍繼續）：{exc.code}: {exc}")
                result["errors"].append(f"initial ax.observe: {exc.code}: {exc}")

            launched = launch_apps(apps)
            result["launched"] = [
                {"label": label, "pid": proc.pid} for label, proc in launched
            ]
            if not launched:
                print("沒有新啟動的 app（可能在既有視窗）；仍嘗試 AX 讀取")
            wait_s = 8
            print(f"等待 {wait_s} 秒讓視窗與 AT-SPI 穩定……")
            await asyncio.sleep(wait_s)

            ax = None
            try:
                ax, ms, used_fallback = await observe_ax(
                    rec,
                    "read",
                    "ax.observe",
                    device,
                    timeout_ms=20000,
                    max_nodes=1200,
                    max_depth=10,
                    fallback_timeout_ms=4000,
                )
                result["ax"] = {
                    "ms": round(ms, 1),
                    "nodes": len(ax.tree.nodes),
                    "status": ax.tree.status,
                    "truncated": ax.tree.truncated,
                    "limits_fallback": used_fallback,
                    "id": ax.id,
                }
                print(
                    f"ax.observe {ms:.1f} ms nodes={len(ax.tree.nodes)} "
                    f"status={ax.tree.status} truncated={ax.tree.truncated} "
                    f"id={ax.id} limits_fallback={used_fallback}"
                )
            except MuError as exc:
                print(f"ax.observe 失敗：{exc.code}: {exc}")
                result["errors"].append(f"ax.observe: {exc.code}: {exc}")
            if ax is None:
                raise RuntimeError("主 ax.observe 失敗，沒有 AX 資料")

            ax_text = str(ax.text)
            ax_path = OUT_DIR / f"ax-{stamp}.txt"
            ax_path.write_text(ax_text, encoding="utf-8")
            print(f"AX 文字：{ax_path}（{len(ax_text)} chars）")
            print(f"AX 首行：{ax_text.splitlines()[0] if ax_text else '（空）'}")

            # 預期 Linux 沒有 device.list_windows；原樣記錄 MuError。
            started = time.perf_counter()
            try:
                inventory = await device.windows.list()
                ms = (time.perf_counter() - started) * 1000
                rec.add("read", "windows.list", ms, True, windows=len(inventory.windows))
                print(f"desktop.windows.list() 成功：{len(inventory.windows)} 個視窗")
                result["windows_list"] = {
                    "ok": True,
                    "count": len(inventory.windows),
                    "windows": [
                        {
                            "id": item.id,
                            "app_id": item.app_id,
                            "title": item.title,
                            "bounds": item.bounds,
                        }
                        for item in inventory.windows
                    ],
                }
            except MuError as exc:
                ms = (time.perf_counter() - started) * 1000
                message = str(exc)
                prefix = exc.code + ": "
                if message.startswith(prefix):
                    message = message[len(prefix) :]
                rec.add("read", "windows.list", ms, False, code=exc.code, error=message)
                print(f"desktop.windows.list() -> MuError code={exc.code} message={message!r}")
                result["windows_list"] = {
                    "ok": False,
                    "code": exc.code,
                    "message": message,
                    "raw": str(exc),
                }

            nodes = list(ax.tree.nodes)
            by_id = {node.id: node for node in nodes}
            windows, per_app = analyze_windows(ax)
            window_path = OUT_DIR / f"ax-windows-{stamp}.txt"
            window_lines = [
                f"# AX windows role=frame/window/dialog (nodes={len(nodes)}, "
                f"truncated={ax.tree.truncated})"
            ]
            for item in windows:
                title = (item["title"] or "").replace("\n", " ")
                bounds = "-" if item["bounds"] is None else (
                    f"{item['bounds'][0]},{item['bounds'][1]} "
                    f"{item['bounds'][2]}x{item['bounds'][3]}"
                )
                window_lines.append(
                    f"app={item['app']} role={item['role']} title={title!r} bounds={bounds}"
                )
            window_path.write_text("\n".join(window_lines) + "\n", encoding="utf-8")
            print(f"AX 視窗清單：{window_path}（{len(windows)} 個 frame/window/dialog）")
            for line in window_lines:
                print(f"  {line}")
            result["window_count"] = len(windows)
            result["window_file"] = str(window_path)

            app_results = {}
            for label in APP_MATCHERS:
                if label not in apps:
                    path = OUT_DIR / f"ax-{label}-{stamp}.txt"
                    path.write_text(f"{label}: skipped（--apps 未包含）\n", encoding="utf-8")
                    app_results[label] = {
                        "file": str(path),
                        "nodes": 0,
                        "non_empty": False,
                        "skipped": True,
                    }
                    print(f"--- {label}: skipped（--apps 未包含）---")
                    continue
                path, lines, non_empty = write_app_subtree(label, ax, stamp, by_id)
                app_results[label] = {
                    "file": str(path),
                    "nodes": per_app[label],
                    "non_empty": non_empty,
                }
                print(f"--- {label} 子樹（前 20 行，nodes={per_app[label]}）---")
                for line in lines[:20]:
                    print(f"  {line}")
                if not non_empty:
                    print(f"  {label}: 空")

            included_keys = [key for label in apps for key in APP_MATCHERS[label]]
            appeared_in_windows = any(
                item["app"] is not None
                and any(key in item["app"].casefold() for key in included_keys)
                for item in windows
            )
            any_non_empty = any(
                value["non_empty"]
                for value in app_results.values()
                if not value.get("skipped")
            )
            print(f"我們開的 app 是否出現在視窗清單：{appeared_in_windows}")
            print(f"至少一個 app 子樹非空：{any_non_empty}")
            result["apps"] = app_results
            result["appeared_in_windows"] = appeared_in_windows
            result["any_non_empty"] = any_non_empty
            if not appeared_in_windows or not any_non_empty:
                raise RuntimeError("沒有任何由本腳本啟動的應用程式出現在 AX 視窗清單或子樹為空")
            result.update(ok=True, ax_file=str(ax_path), ax_id=ax.id)
    except Exception as exc:  # noqa: BLE001 - 保留原始錯誤
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        print("read 失敗：")
        traceback.print_exc()
    finally:
        # 只關閉本腳本 launch 的 process group，不用全域 pkill。
        cleanup = close_launched(launched)
        result["cleanup"] = cleanup
        print(f"清理自己開的 app：{json.dumps(cleanup, ensure_ascii=False)}")

        # 等 WM 把視窗移除後再做 after snapshot。
        await asyncio.sleep(0.5)
        after: list[dict] = []
        try:
            after = snapshot_windows(after_path)
            print(f"WINDOW-SNAPSHOT after: {after_path}（{len(after)} windows）")
        except Exception as exc:  # noqa: BLE001 - snapshot 失敗要讓 guard FAIL
            result["errors"].append(f"snapshot after: {type(exc).__name__}: {exc}")
            print(f"WINDOW-SNAPSHOT after 失敗：{type(exc).__name__}: {exc}")

        guard = (
            compare_windows(before, after)
            if after
            else {"ok": False, "missing": [], "changed": [], "new": [], "error": "no after snapshot"}
        )
        result["window_guard"] = guard
        if guard["ok"]:
            print("WINDOW-GUARD PASS")
        else:
            result["ok"] = False
            print("WINDOW-GUARD FAIL")
            print(f"  missing: {json.dumps(guard['missing'], ensure_ascii=False)}")
            print(f"  changed: {json.dumps(guard['changed'], ensure_ascii=False)}")
            print(f"  new (allowed): {json.dumps(guard['new'], ensure_ascii=False)}")
            result["errors"].append("WINDOW-GUARD FAIL")
        if not cleanup["ok"]:
            result["ok"] = False
            result["errors"].append("cleanup FAIL: process group still alive")

    rec.add(
        "read",
        "summary.window_guard",
        0.0,
        bool(guard.get("ok")),
        before=str(before_path),
        after=str(after_path),
        missing=guard.get("missing", []),
        changed=guard.get("changed", []),
        new=guard.get("new", []),
    )
    rec.add(
        "read",
        "summary.cleanup",
        0.0,
        bool(cleanup.get("ok")),
        apps=cleanup.get("apps", []),
    )
    rec.add(
        "read",
        "summary.read",
        0.0,
        bool(result.get("ok")),
        log=str(rec.path),
        errors=result.get("errors", []),
    )
    return result


async def run_act(rec: Recorder, stamp: str) -> dict:
    """只對本腳本自己開的 Kate 視窗送 XTEST；每個輸入前都過前景 gate。"""
    from mai_ui import MuError, Runtime

    result = {"phase": "act", "ok": False, "errors": [], "stamp": stamp, "steps": {}}
    kate_path = OUT_DIR / "act-kate.txt"
    kate_proc = None
    kate_id = None

    if os.environ.get("DESKTOP_DRYRUN") == "1":
        plan = {
            "phase": "act",
            "ok": True,
            "dry_run": True,
            "kate_launch": f"kate --new {kate_path}",
            "steps": [
                "vision.observe → 動前截圖",
                "ax.observe(max_nodes=1200, max_depth=10, timeout_ms=20000)"
                "（v0.1.2 不支援限制時 fallback timeout_ms=4000）",
                "目標：editable 且有 bounds 的節點中心；否則 Kate frame 中心，"
                "y 限制在 frame.y+120 .. frame.bottom-20",
                "vision.click(point) 一次（PointRef 來自動前影像）",
                'input.text("abc") → input.press_key("s", ["ctrl"])',
                'input.text("桌面測試")：預期 UNSUPPORTED_CAPABILITY，只記錄不重試',
                'extra：ax.set_text("桌面測試")；clipboard paste（ClipboardGuard 先存後還原）',
                "動後截圖、cursor 80x80 裁切、compare -metric AE",
                "只關閉本腳本開的 Kate",
            ],
            "note": "只印計畫；沒有建立 Runtime、沒有送 XTEST、沒有寫 clipboard",
        }
        print("DESKTOP_DRYRUN=1：只印 act 計畫，不執行任何動作")
        print(f"  開 app：{plan['kate_launch']}")
        for step in plan["steps"]:
            print(f"  - {step}")
        return plan

    try:
        # 前置：若已有 Kate/LibreOffice，代表不是本腳本可安全辨識的視窗，直接停止。
        existing_kate = pgrep_exact("kate")
        existing_lo = pgrep_exact("soffice.bin") + pgrep_exact("oosplash")
        print(f"前置程序檢查：kate={existing_kate} soffice={existing_lo}")
        if existing_kate or existing_lo:
            raise RuntimeError(
                "已有 Kate/LibreOffice 程序；act phase 不啟動、不送任何事件，請先清理自己的視窗"
            )

        kate_path.write_bytes(b"")
        kate_proc = launch_kate(kate_path)
        print(f"已啟動 Kate pid={kate_proc.pid}（空白檔 {kate_path}）")
        kate_id = find_window_with_title("act-kate.txt", timeout=20)
        if not kate_id:
            raise RuntimeError("20 秒內找不到標題含 act-kate.txt 的 Kate 視窗")
        print(f"Kate window id={kate_id}")
        result["kate_id"] = kate_id
        result["kate_pid"] = kate_proc.pid

        async with Runtime() as runtime:
            info, ms = await timed(
                rec,
                "act",
                "add_linux",
                runtime.add_linux(
                    name="desktop",
                    display=":0",
                    xauthority=str(Path.home() / ".Xauthority"),
                ),
            )
            print(f"add_linux {ms:.1f} ms -> {info.id}")
            session = runtime.session()
            device, ms = await timed(
                rec, "act", "register", session.register(info, name="desktop")
            )
            print(f"register {ms:.1f} ms -> {device.resource_id}")

            # 1. 動前截圖
            before_obs, before_path = await capture_vision(
                rec, "act", "vision.observe.before", device, stamp, f"act-before-{stamp}.png"
            )
            image = before_obs.images[0]
            result["before"] = str(before_path)

            # 2. AX 觀測：找 Kate frame 與可編輯節點
            ax, ms, used_fallback = await observe_ax(
                rec,
                "act",
                "ax.observe",
                device,
                timeout_ms=20000,
                max_nodes=1200,
                max_depth=10,
                fallback_timeout_ms=4000,
            )
            result["ax_limits_fallback"] = used_fallback
            print(
                f"ax.observe {ms:.1f} ms nodes={len(ax.tree.nodes)} "
                f"status={ax.tree.status} truncated={ax.tree.truncated} "
                f"limits_fallback={used_fallback}"
            )
            # 刻意不存整個 desktop 的 AX 文字：那會包含其他視窗（例如終端機）的內容。
            # 只留 Kate 自己的節點摘要，見下方 act-ax-kate-*.txt。

            nodes = list(ax.tree.nodes)
            by_id = {node.id: node for node in nodes}
            children = node_children(nodes)
            apps = [
                node
                for node in nodes
                if node.role == "application" and "kate" in (node.name or "").casefold()
            ]
            frames = [
                node
                for node in nodes
                if node.role == "frame"
                and "act-kate.txt" in (node.name or "")
                and application_of(node, by_id) in apps
            ]
            if not frames:
                raise RuntimeError("AX 樹中找不到標題含 act-kate.txt 的 Kate frame")
            frame = frames[0]
            if frame.bounds is None:
                raise RuntimeError("Kate frame 沒有 bounds，無法安全定位")
            frame_bounds = frame.bounds
            candidates = [
                node
                for node in subtree_of(frame, children)
                if node is not frame
                and node.role in ("textbox", "text")
                and node.editable is True
            ]
            with_bounds = [
                node
                for node in candidates
                if node.bounds is not None and node.bounds.width > 0 and node.bounds.height > 0
            ]
            editable_target = candidates[0] if candidates else None
            target = (
                max(with_bounds, key=lambda node: node.bounds.width * node.bounds.height)
                if with_bounds
                else None
            )
            if target is not None:
                bounds = target.bounds
                px = bounds.x + bounds.width / 2
                py = bounds.y + bounds.height / 2
                source = f"editable {target.role} index={target.index}"
            else:
                px = frame_bounds.x + frame_bounds.width / 2
                py = frame_bounds.y + frame_bounds.height / 2
                min_y = frame_bounds.y + 120
                max_y = frame_bounds.y + frame_bounds.height - 20
                py = min(max(py, min_y), max_y)
                source = (
                    f"frame index={frame.index} 中心（無 editable+bounds；"
                    f"y 限制 {min_y:.0f}..{max_y:.0f}）"
                )
            px, py = int(round(px)), int(round(py))
            if not (
                frame_bounds.x <= px <= frame_bounds.x + frame_bounds.width
                and frame_bounds.y <= py <= frame_bounds.y + frame_bounds.height
            ):
                raise RuntimeError(f"點擊點 ({px}, {py}) 不在 Kate frame bounds 內")
            if not (0 <= px < image.width and 0 <= py < image.height):
                raise RuntimeError(f"點擊點 ({px}, {py}) 不在動前影像 {image.width}x{image.height} 內")

            target_info = {
                "frame": {
                    "index": frame.index,
                    "name": frame.name,
                    "bounds": bounds_dict(frame_bounds),
                },
                "editable_candidates": [
                    {
                        "index": node.index,
                        "role": node.role,
                        "bounds": bounds_dict(node.bounds),
                        "editable": node.editable,
                    }
                    for node in candidates
                ],
                "chosen": source,
                "bounds": bounds_dict(target.bounds if target is not None else frame_bounds),
                "screen": [px, py],
                "editable_target": None
                if editable_target is None
                else {
                    "index": editable_target.index,
                    "role": editable_target.role,
                    "bounds": bounds_dict(editable_target.bounds),
                },
            }
            result["target"] = target_info
            print(f"目標選擇：{source}")
            print(f"  frame bounds={bounds_dict(frame_bounds)}")
            print(f"  editable candidates={json.dumps(target_info['editable_candidates'], ensure_ascii=False)}")
            print(f"  screen point=({px}, {py})")

            # 只存 Kate 自己的 AX 摘要；不含其他應用程式的節點文字。
            ax_summary_path = OUT_DIR / f"act-ax-kate-{stamp}.txt"
            summary_lines = [
                "# Kate-only AX summary；完整 desktop AX 文字刻意不存（可能含其他視窗內容）",
                f"observation={ax.id} status={ax.tree.status} truncated={ax.tree.truncated}",
                "applications="
                + json.dumps(
                    [{"index": app.index, "name": app.name} for app in apps],
                    ensure_ascii=False,
                ),
                describe_node(frame),
            ]
            summary_lines.extend(describe_node(node) for node in candidates)
            summary_lines.append(
                f"chosen={source} screen=({px}, {py}) "
                f"bounds={bounds_dict(target.bounds if target is not None else frame_bounds)}"
            )
            ax_summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
            result["ax_file"] = str(ax_summary_path)
            print(f"Kate-only AX 摘要：{ax_summary_path}")

            # 3. 用這張 vision observation 產生 PointRef，再點一次
            point = image.point(px, py)
            rec.add("act", "vision.point", 0.0, True, x=px, y=py, source=source)
            click_info, click_ms = await gated_receipt(
                rec, kate_id, "vision.click", device.ui.vision.click, point
            )
            result["click"] = click_info
            print(f"vision.click {click_ms:.1f} ms receipt={json.dumps(click_info, ensure_ascii=False)}")
            if receipt_error(click_info):
                raise RuntimeError(f"vision.click 回報錯誤：{click_info['error']}")

            await asyncio.sleep(0.4)
            result["active_after_click"] = active_window()
            print(f"點擊後 _NET_ACTIVE_WINDOW={result['active_after_click']}")
            after_click_obs, after_click_path = await capture_vision(
                rec, "act", "vision.observe.after_click", device, stamp, f"act-after-click-{stamp}.png"
            )

            # 4. 英文輸入 + Ctrl+S
            abc_info, abc_ms = await gated_receipt(
                rec, kate_id, "input.text.abc", device.ui.input.text, "abc"
            )
            result["text_abc"] = abc_info
            print(f"input.text('abc') {abc_ms:.1f} ms receipt={json.dumps(abc_info, ensure_ascii=False)}")
            if receipt_error(abc_info):
                raise RuntimeError(f"input.text('abc') 回報錯誤：{abc_info['error']}")
            await asyncio.sleep(0.3)

            save_info, save_ms = await gated_receipt(
                rec,
                kate_id,
                "input.press_key.ctrl_s",
                press_key_checked,
                device,
                "s",
                modifiers=["ctrl"],
            )
            result["save_en"] = save_info
            print(f"Ctrl+S {save_ms:.1f} ms receipt={json.dumps(save_info, ensure_ascii=False)}")
            await asyncio.sleep(0.5)

            content = kate_path.read_text(encoding="utf-8", errors="replace")
            en_path = OUT_DIR / "act-kate-en.txt"
            shutil.copyfile(kate_path, en_path)
            english_ok = "abc" in content
            result["steps"]["english"] = {
                "ok": english_ok,
                "content": content,
                "file": str(en_path),
            }
            print(f"act-kate.txt={content!r}；act-kate-en.txt={en_path}；含 abc={english_ok}")
            if not english_ok:
                probe_gate = foreground_gate(kate_id)
                result["english_failure_gate"] = probe_gate
                print(
                    "英文輸入未落檔，前景 gate："
                    + json.dumps(probe_gate, ensure_ascii=False)
                )
                await capture_vision(
                    rec, "act", "vision.observe.save_failed", device, stamp, f"act-save-failed-{stamp}.png"
                )
                raise RuntimeError("input.text('abc') 後 Ctrl+S 沒有把 abc 寫入 act-kate.txt")

            # 6. 動後（英文）截圖
            after_en_obs, after_en_path = await capture_vision(
                rec, "act", "vision.observe.after_en", device, stamp, f"act-after-en-{stamp}.png"
            )
            result["after_en"] = str(after_en_path)

            # 7. 中文 required test：只記錄實際結果，不掩蓋失敗
            # 輸出路徑：ASCII 用 input.text；中文首選 ax.set_text，無效果才用
            # ClipboardGuard 保護的 clipboard paste。input.text 只保證 ASCII。
            zh = {"attempted": True, "ok": False}
            try:
                zh_info, zh_ms = await gated_receipt(
                    rec, kate_id, "input.text.zh", device.ui.input.text, "桌面測試"
                )
                zh["receipt"] = zh_info
                print(
                    f"input.text('桌面測試') {zh_ms:.1f} ms "
                    f"receipt={json.dumps(zh_info, ensure_ascii=False)}"
                )
                if receipt_error(zh_info):
                    zh["error"] = receipt_error(zh_info)
                else:
                    await asyncio.sleep(0.3)
                    zh_save, _ = await gated_receipt(
                        rec,
                        kate_id,
                        "input.press_key.ctrl_s_zh",
                        press_key_checked,
                        device,
                        "s",
                        modifiers=["ctrl"],
                    )
                    zh["save"] = zh_save
                    await asyncio.sleep(0.5)
                    zh_content = kate_path.read_text(encoding="utf-8", errors="replace")
                    zh_path = OUT_DIR / "act-kate-zh.txt"
                    shutil.copyfile(kate_path, zh_path)
                    zh.update(content=zh_content, file=str(zh_path), ok="桌面測試" in zh_content)
            except MuError as exc:
                zh.update(code=exc.code, message=str(exc), raw=str(exc))
                print(f"input.text('桌面測試') -> MuError code={exc.code} message={str(exc)!r}")
            result["steps"]["chinese_input_text"] = zh
            await asyncio.sleep(0.3)
            after_zh_obs, after_zh_path = await capture_vision(
                rec, "act", "vision.observe.after_zh", device, stamp, f"act-after-zh-{stamp}.png"
            )
            result["after_zh"] = str(after_zh_path)

            # 8. 額外診斷（明確不計入 required gate）
            extra = {"note": "額外診斷，不計入 required gate"}
            if zh.get("ok"):
                extra["skipped"] = "中文 input.text 已成功，不需要額外診斷"
            else:
                # a. AX set_text：用剛才找到的 editable 節點（bounds 可為 None）
                ax_set = None
                if editable_target is not None:
                    try:
                        set_info, set_ms = await gated_receipt(
                            rec,
                            kate_id,
                            "ax.set_text.zh",
                            device.ui.ax.set_text,
                            editable_target,
                            "桌面測試",
                        )
                        ax_set = {"ok": receipt_error(set_info) is None, "receipt": set_info}
                        print(
                            f"ax.set_text('桌面測試') {set_ms:.1f} ms "
                            f"receipt={json.dumps(set_info, ensure_ascii=False)}"
                        )
                        if ax_set["ok"]:
                            await asyncio.sleep(0.3)
                            ax_save, _ = await gated_receipt(
                                rec,
                                kate_id,
                                "input.press_key.ctrl_s_ax",
                                press_key_checked,
                                device,
                                "s",
                                modifiers=["ctrl"],
                            )
                            ax_set["save"] = ax_save
                            await asyncio.sleep(0.5)
                            ax_content = kate_path.read_text(encoding="utf-8", errors="replace")
                            ax_path_out = OUT_DIR / "act-kate-zh-ax.txt"
                            shutil.copyfile(kate_path, ax_path_out)
                            ax_set.update(
                                content=ax_content,
                                file=str(ax_path_out),
                                zh_ok="桌面測試" in ax_content,
                            )
                    except MuError as exc:
                        ax_set = {
                            "ok": False,
                            "code": exc.code,
                            "message": str(exc),
                            "raw": str(exc),
                        }
                        print(f"ax.set_text('桌面測試') -> MuError code={exc.code} message={str(exc)!r}")
                else:
                    ax_set = {"ok": False, "skipped": "AX 樹沒有 editable 節點"}
                extra["ax_set_text"] = ax_set

                # b. clipboard：獨立第二條路；先用 ClipboardGuard 保存，finally 一定還原。
                clip = {}
                guard = ClipboardGuard()
                try:
                    guard.save()
                    clip["saved"] = True
                except ClipboardNotText as exc:
                    clip.update(ok=False, code="CLIPBOARD_NOT_TEXT", message=str(exc))
                    print(f"clipboard 路徑拒絕：{exc}")
                else:
                    try:
                        clip_info, clip_ms = await gated_receipt(
                            rec,
                            kate_id,
                            "clipboard.write.zh",
                            device.clipboard.write,
                            {"text/plain": "桌面測試"},
                        )
                        clip["write"] = clip_info
                        print(
                            f"clipboard.write('桌面測試') {clip_ms:.1f} ms "
                            f"receipt={json.dumps(clip_info, ensure_ascii=False)}"
                        )
                        await asyncio.sleep(0.2)
                        for key, label in (("a", "ctrl_a"), ("v", "ctrl_v"), ("s", "ctrl_s")):
                            key_info, _ = await gated_receipt(
                                rec,
                                kate_id,
                                f"input.press_key.{label}",
                                press_key_checked,
                                device,
                                key,
                                modifiers=["ctrl"],
                            )
                            clip[label] = key_info
                            await asyncio.sleep(0.2)
                        await asyncio.sleep(0.5)
                        clip_content = kate_path.read_text(encoding="utf-8", errors="replace")
                        clip_path = OUT_DIR / "act-kate-zh-clipboard.txt"
                        shutil.copyfile(kate_path, clip_path)
                        shutil.copyfile(kate_path, OUT_DIR / "act-kate-zh.txt")
                        clip.update(
                            content=clip_content,
                            file=str(clip_path),
                            zh_ok="桌面測試" in clip_content,
                        )
                    except MuError as exc:
                        clip.update(ok=False, code=exc.code, message=str(exc), raw=str(exc))
                        print(f"clipboard 路徑 -> MuError code={exc.code} message={str(exc)!r}")
                    finally:
                        try:
                            guard.restore()
                            clip["restored"] = True
                        except Exception as exc:  # noqa: BLE001 - restore 失敗要留下紀錄
                            clip["restore_error"] = f"{type(exc).__name__}: {exc}"
                            print(f"clipboard restore 失敗：{clip['restore_error']}")
                extra["clipboard"] = clip
            result["extra"] = extra
            await capture_vision(
                rec, "act", "vision.observe.after_extra", device, stamp, f"act-after-extra-{stamp}.png"
            )

            # 9. 游標裁切與差異像素
            cursor_before = OUT_DIR / "act-cursor-before.png"
            cursor_after = OUT_DIR / "act-cursor-after.png"
            crop_around(before_path, cursor_before, px, py)
            crop_around(after_click_path, cursor_after, px, py)
            result["cursor"] = {
                "before": str(cursor_before),
                "after": str(cursor_after),
                "compare_ae": compare_ae(cursor_before, cursor_after),
                "crop_center": [px, py],
                "crop_size": 80,
            }

            result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - 保留原始錯誤
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        print("act 失敗：")
        traceback.print_exc()
    finally:
        # 只關本腳本自己開的 Kate。
        if kate_proc is not None:
            try:
                kate_proc.terminate()
                kate_proc.wait(timeout=5)
            except Exception:
                try:
                    kate_proc.kill()
                    kate_proc.wait(timeout=5)
                except Exception:
                    pass
        remaining_kate = pgrep_exact("kate")
        dolphin = subprocess.run(
            ["ps", "-p", "530339", "-o", "pid="], capture_output=True, text=True
        ).stdout.strip()
        result["cleanup"] = {
            "kate_remaining": remaining_kate,
            "dolphin_530339_alive": dolphin == "530339",
        }
        print(
            "清理：kate 剩餘程序="
            f"{remaining_kate}；Dolphin(530339) alive={dolphin == '530339'}"
        )
    return result


async def main_async(phase: str, apps: set[str]) -> int:
    configure_environment()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    rec = Recorder(stamp, prefix="act" if phase == "act" else "smoke")
    print(f"log: {rec.path}")
    print(
        "env: DISPLAY=:0 XAUTHORITY="
        f"{os.environ['XAUTHORITY']} DBUS_SESSION_BUS_ADDRESS={os.environ['DBUS_SESSION_BUS_ADDRESS']}"
    )

    results = []
    if phase in ("see", "all"):
        results.append(await run_see(rec, stamp))
    if phase in ("read", "all"):
        results.append(await run_read(rec, stamp, apps))
    if phase == "act":
        results.append(await run_act(rec, stamp))

    print("=== summary ===")
    for result in results:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    ok = all(result.get("ok") for result in results)
    print(f"RESULT {'PASS' if ok else 'FAIL'} phase={phase} log={rec.path}")
    rec.add(
        phase,
        "summary.result",
        0.0,
        ok,
        log=str(rec.path),
        phases=[result.get("phase") for result in results],
        errors=[error for result in results for error in result.get("errors", [])],
    )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("see", "read", "act", "all"),
        default="all",
        help="see=截圖；read=AX 觀測；act=只對自己開的 Kate 送 XTEST；all=see+read（不含 act）",
    )
    parser.add_argument(
        "--apps",
        default="kate,dolphin,libreoffice",
        help="read 要啟動/讀取的 app，逗號分隔（kate,dolphin,libreoffice）",
    )
    args = parser.parse_args()
    apps = {part.strip().lower() for part in args.apps.split(",") if part.strip()}
    unknown = apps - set(APP_MATCHERS)
    if unknown:
        parser.error(f"--apps 含未知項目：{', '.join(sorted(unknown))}")
    if not apps:
        parser.error("--apps 至少要有一個 app")
    return asyncio.run(main_async(args.phase, apps))


if __name__ == "__main__":
    sys.exit(main())
