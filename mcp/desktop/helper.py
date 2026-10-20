#!/usr/bin/env python3
"""desktop helper：一支程式內完成 observe → act；AX index 不跨程序。

給 flash 級模型用的薄 CLI。每個命令輸出：
  一行 `OK ...` 或 `ERR CODE ...` 摘要
  一行 JSON 細節
失敗一律 exit 1，代碼是 typed error（APP_NOT_FOUND、APP_NOT_ACTIVE、
TARGET_NOT_FOUND、TARGET_AMBIGUOUS、AX_EMPTY、CLIPBOARD_NOT_TEXT、
DANGEROUS_KEY_REFUSED、POINT_OUTSIDE_APP、VERIFY_FAILED 等）。

`DESKTOP_DRYRUN=1` 或 `--dry-run`：只印計畫，不建立 Runtime、不送 XTEST、
不寫 clipboard；危險鍵在 dry-run 也會先拒絕。

AX 限制：v0.1.2 的 `AX.observe(max_nodes=..., max_depth=...)` 會回
`UNSUPPORTED_CAPABILITY: AX capture limits are WIP`；helper 先照要求帶限制，
原樣記錄 `limits_unsupported=true`，再用 `timeout_ms=2000/4000` fallback。
`--app` 是事後過濾 application 節點，capture 仍是 desktop-wide。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

OUT_DIR = Path("/tmp/opencode/desktop")
DESKTOP_DIR = Path(__file__).resolve().parent
LOG_DIR = DESKTOP_DIR / ".logs"

DANGEROUS_COMBOS = {
    "ctrl+alt+f1", "ctrl+alt+f2", "ctrl+alt+f3", "ctrl+alt+f4", "ctrl+alt+f5",
    "ctrl+alt+f6", "ctrl+alt+f7", "ctrl+alt+f8", "ctrl+alt+f9", "ctrl+alt+f10",
    "ctrl+alt+f11", "ctrl+alt+f12", "ctrl+alt+backspace", "super+l", "super+q",
}


class HelperError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def configure_environment() -> None:
    os.environ["DISPLAY"] = ":0"
    os.environ["XAUTHORITY"] = str(Path.home() / ".Xauthority")
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/1000/bus"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def text_preview(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= 120 else text[:117] + "..."


def log_action(row: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"actions-{datetime.now():%Y%m%d}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def receipt_dict(receipt) -> dict:
    if is_dataclass(receipt):
        return asdict(receipt)
    if hasattr(receipt, "__dict__"):
        return dict(receipt.__dict__)
    return {"repr": repr(receipt)}


def is_dry_run(args) -> bool:
    return bool(getattr(args, "dry_run", False)) or os.environ.get("DESKTOP_DRYRUN") == "1"


def _run(argv, timeout=20, **kwargs):
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=os.environ,
        timeout=timeout,
        **kwargs,
    )


def _xprop(argv) -> str:
    proc = _run(argv)
    if proc.returncode != 0:
        raise HelperError(
            "XPROP_FAILED",
            f"{' '.join(argv)} 失敗（exit {proc.returncode}）：{proc.stderr.strip()}",
        )
    return proc.stdout.strip()


def _window_id(text: str) -> str | None:
    match = re.search(r"window id # (0x[0-9a-fA-F]+)", text)
    return match.group(1) if match else None


def active_window_id() -> str | None:
    return _window_id(_xprop(["xprop", "-root", "_NET_ACTIVE_WINDOW"]))


def window_class(wid: str) -> str:
    return _xprop(["xprop", "-id", wid, "WM_CLASS"])


def ensure_active(app: str) -> tuple[str, str]:
    wid = active_window_id()
    if not wid:
        raise HelperError(
            "APP_NOT_ACTIVE",
            "找不到 _NET_ACTIVE_WINDOW；先讓自己的 app 取得前景，不要硬搶",
        )
    classes = window_class(wid)
    if app.casefold() not in classes.casefold():
        raise HelperError(
            "APP_NOT_ACTIVE",
            f"active window {wid} 不是 {app!r}；先讓自己的 app 取得前景，不要硬搶。"
            f"WM_CLASS={classes}",
            {"active_window": wid, "wm_class": classes, "app": app},
        )
    return wid, classes


def _normalized_modifiers(modifiers) -> set[str]:
    result = set()
    for modifier in modifiers or ():
        value = modifier.lower()
        if value in ("ctrl", "control"):
            result.add("ctrl")
        elif value in ("super", "meta", "win", "windows"):
            result.add("super")
        else:
            result.add(value)
    return result


def reject_dangerous_key(key: str, modifiers) -> None:
    normalized_key = key.lower()
    mods = _normalized_modifiers(modifiers)
    combo = "+".join(sorted(mods | {normalized_key}))
    details = {"key": normalized_key, "modifiers": sorted(mods), "combo": combo}
    if {"ctrl", "alt"} <= mods and normalized_key in {f"f{i}" for i in range(1, 13)}:
        raise HelperError("DANGEROUS_KEY_REFUSED", f"拒絕危險組合：{combo}", details)
    if {"ctrl", "alt"} <= mods and normalized_key in ("backspace", "back_space"):
        raise HelperError("DANGEROUS_KEY_REFUSED", f"拒絕危險組合：{combo}", details)
    if "super" in mods and normalized_key in ("l", "q"):
        raise HelperError("DANGEROUS_KEY_REFUSED", f"拒絕危險組合：{combo}", details)


async def press_key_checked(device, key: str, modifiers):
    reject_dangerous_key(key, modifiers)
    return await device.ui.input.press_key(key, modifiers=list(modifiers))


class ClipboardGuard:
    """保存／還原純文字剪貼簿；非文字剪貼簿拒絕使用。"""

    TEXT_ATOMS = {"UTF8_STRING", "text/plain", "text/plain;charset=utf-8", "STRING", "TEXT"}

    def __init__(self):
        self.text: str | None = None
        self.saved = False
        self.restored = False

    def save(self) -> None:
        targets = _run(["xclip", "-o", "-selection", "clipboard", "-t", "TARGETS"], timeout=10)
        atoms = {line.strip() for line in targets.stdout.splitlines() if line.strip()}
        if targets.returncode != 0 and not atoms:
            self.text = ""
            self.saved = True
            return
        if not (atoms & self.TEXT_ATOMS):
            raise HelperError(
                "CLIPBOARD_NOT_TEXT",
                "clipboard TARGETS 不含 text/plain 或 UTF8_STRING；拒絕覆蓋",
                {"targets": sorted(atoms)},
            )
        read = _run(["xclip", "-o", "-selection", "clipboard"], timeout=10)
        if read.returncode != 0:
            raise HelperError(
                "CLIPBOARD_NOT_TEXT",
                f"無法讀取文字剪貼簿（exit {read.returncode}）：{read.stderr.strip()}",
            )
        self.text = read.stdout
        self.saved = True

    def restore(self) -> None:
        if not self.saved:
            return
        proc = _run(
            ["xclip", "-i", "-selection", "clipboard"],
            timeout=10,
            input=self.text,
        )
        if proc.returncode != 0:
            raise HelperError(
                "CLIPBOARD_RESTORE_FAILED",
                f"還原剪貼簿失敗（exit {proc.returncode}）：{proc.stderr.strip()}",
            )
        self.saved = False
        self.restored = True


# --- AX helpers ---------------------------------------------------------------


def application_of(node, by_id):
    seen = set()
    current = node
    while current is not None and current.id not in seen:
        seen.add(current.id)
        if current.role == "application":
            return current
        current = by_id.get(current.parent_id) if current.parent_id else None
    return None


def resolve_app(ax, app_name: str):
    nodes = list(ax.tree.nodes)
    by_id = {node.id: node for node in nodes}
    apps = [
        node
        for node in nodes
        if node.role == "application" and app_name.casefold() in (node.name or "").casefold()
    ]
    if not apps:
        raise HelperError(
            "APP_NOT_FOUND",
            f"AX 樹找不到 application 名稱含 {app_name!r} 的節點",
            {"applications": [node.name for node in nodes if node.role == "application"]},
        )
    if len(apps) > 1:
        raise HelperError(
            "APP_AMBIGUOUS",
            f"application 名稱含 {app_name!r} 的節點不只一個",
            {
                "candidates": [
                    {"index": node.index, "name": node.name}
                    for node in apps
                ]
            },
        )
    app = apps[0]
    subtree = [node for node in nodes if application_of(node, by_id) is app]
    return app, subtree, nodes, by_id


def app_depth(app, subtree) -> str:
    """四級深度：empty / frame-only / chrome-only / controls。

    controls 只代表有 editable=True 的節點；menu_bar、tool_bar、filler、panel 等
    非 editable 的節點只能算 chrome-only，不能說 AX 可用。
    """
    others = [node for node in subtree if node.role != "application"]
    if not others:
        return "empty"
    if any(node.editable is True for node in others):
        return "controls"
    if all(node.role in ("frame", "window", "dialog") for node in others):
        return "frame-only"
    return "chrome-only"


def format_node(node) -> str:
    bounds = "-" if node.bounds is None else (
        f"{int(node.bounds.x)},{int(node.bounds.y)} "
        f"{int(node.bounds.width)}x{int(node.bounds.height)}"
    )
    name = (node.name or "").replace("\n", " ")
    text = (node.text or "").replace("\n", " ")[:100]
    actions = ",".join(action.id for action in node.actions) or "-"
    return (
        f"[{node.index}] {node.role} name={name!r} editable={node.editable} "
        f"text={text!r} bounds={bounds} actions={actions}"
    )


async def observe_ax(device, *, max_nodes: int, max_depth: int, timeout_ms: int):
    from mai_ui import MuError

    limits_unsupported = False
    try:
        observation = await device.ui.ax.observe(
            timeout_ms=timeout_ms, max_nodes=max_nodes, max_depth=max_depth
        )
    except MuError as exc:
        if exc.code != "UNSUPPORTED_CAPABILITY" or "AX capture limits are WIP" not in str(exc):
            raise
        limits_unsupported = True
        observation = await device.ui.ax.observe(timeout_ms=timeout_ms)
    return observation, limits_unsupported


async def open_device(runtime):
    info = await runtime.add_linux(
        name="desktop",
        display=":0",
        xauthority=str(Path.home() / ".Xauthority"),
    )
    session = runtime.session()
    return await session.register(info, name="desktop")


# --- commands -----------------------------------------------------------------


async def cmd_shot(args):
    stamp = now_stamp()
    target = Path(args.out) if args.out else OUT_DIR / f"helper-shot-{stamp}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    if is_dry_run(args):
        return (
            f"dry-run shot would capture to {target}",
            {"command": "shot", "dry_run": True, "path": str(target), "path_used": "vision"},
            [],
            {"path_used": "vision", "dry_run": True},
        )

    from mai_ui import Runtime

    async with Runtime() as runtime:
        device = await open_device(runtime)
        observation = await device.ui.vision.observe()
        if not observation.images:
            raise HelperError("CAPTURE_FAILED", "vision.observe 沒有回傳影像")
        image = observation.images[0]
        data = await image.read()
        target.write_bytes(data)
        identify = _run(
            [
                "identify",
                "-format",
                "%wx%h mean=%[fx:mean] size=%b",
                str(target),
            ],
            timeout=30,
        )
        mean = None
        match = re.search(r"mean=([0-9.eE+-]+)", identify.stdout)
        if match:
            mean = float(match.group(1))
        details = {
            "command": "shot",
            "path": str(target),
            "width": image.width,
            "height": image.height,
            "bytes": len(data),
            "mean": mean,
            "identify": (identify.stdout or identify.stderr).strip(),
            "path_used": "vision",
        }
        return (
            f"shot {target} {image.width}x{image.height} bytes={len(data)} mean={mean}",
            details,
            [],
            {"path_used": "vision"},
        )


async def cmd_ax(args):
    if args.timeout_ms not in (2000, 4000):
        raise HelperError("INVALID_ARGUMENT", "--timeout-ms 只允許 2000 或 4000")
    if is_dry_run(args):
        return (
            f"dry-run ax would observe app={args.app} max_nodes={args.max_nodes} "
            f"max_depth={args.max_depth} timeout_ms={args.timeout_ms}",
            {
                "command": "ax",
                "dry_run": True,
                "app": args.app,
                "max_nodes": args.max_nodes,
                "max_depth": args.max_depth,
                "timeout_ms": args.timeout_ms,
                "max_lines": args.max_lines,
            },
            [],
            {
                "path_used": "ax",
                "ax_timeout_ms": args.timeout_ms,
                "dry_run": True,
            },
        )

    from mai_ui import Runtime

    async with Runtime() as runtime:
        device = await open_device(runtime)
        observation, limits_unsupported = await observe_ax(
            device,
            max_nodes=args.max_nodes,
            max_depth=args.max_depth,
            timeout_ms=args.timeout_ms,
        )
        app, subtree, nodes, by_id = resolve_app(observation, args.app)
        depth = app_depth(app, subtree)
        editable_count = sum(1 for node in subtree if node.editable is True)
        if depth == "empty":
            raise HelperError(
                "AX_EMPTY",
                f"app {app.name!r} 的子樹只有 application 節點，沒有 frame/控制項",
                {"app": app.name, "ax_nodes": len(nodes), "depth": "empty", "editable_count": 0},
            )
        lines = [format_node(node) for node in subtree]
        shown = lines[: args.max_lines]
        if len(lines) > len(shown):
            shown.append(f"... truncated at {len(shown)} lines (app_nodes={len(lines)})")
        if depth in ("frame-only", "chrome-only"):
            shown.append(
                "WARN 沒有 editable 控制項；打字請用截圖座標或 clipboard，"
                "不要把這個樹當成 AX 可用。"
            )
        details = {
            "command": "ax",
            "app": app.name,
            "app_index": app.index,
            "app_nodes": len(subtree),
            "ax_nodes": len(nodes),
            "ax_truncated": observation.tree.truncated,
            "ax_timeout_ms": args.timeout_ms,
            "limits_unsupported": limits_unsupported,
            "depth": depth,
            "editable_count": editable_count,
            "max_nodes": args.max_nodes,
            "max_depth": args.max_depth,
            "lines": shown,
            "path_used": "ax",
        }
        summary = (
            f"ax app={app.name} app_nodes={len(subtree)} ax_nodes={len(nodes)} "
            f"depth={depth} editable_count={editable_count} "
            f"truncated={observation.tree.truncated} "
            f"timeout_ms={args.timeout_ms} limits_unsupported={limits_unsupported}"
        )
        return (
            summary,
            details,
            shown,
            {
                "path_used": "ax",
                "ax_timeout_ms": args.timeout_ms,
                "ax_nodes": len(nodes),
                "ax_truncated": observation.tree.truncated,
                "ax_depth": depth,
                "editable_count": editable_count,
            },
        )


async def cmd_click(args):
    use_ax = bool(args.role or args.name)
    use_point = args.x is not None or args.y is not None
    if use_ax and use_point:
        raise HelperError("INVALID_ARGUMENT", "AX 模式與座標模式不能同時使用")
    if use_ax and not (args.role and args.name):
        raise HelperError("INVALID_ARGUMENT", "AX 模式需要同時給 --role 與 --name")
    if use_point and (args.x is None or args.y is None):
        raise HelperError("INVALID_ARGUMENT", "座標模式需要同時給 --x 與 --y")
    if not use_ax and not use_point:
        raise HelperError("INVALID_ARGUMENT", "需要 --role/--name 或 --x/--y")
    if is_dry_run(args):
        plan = (
            f"dry-run click would click app={args.app} via AX role={args.role!r} name={args.name!r}"
            if use_ax
            else f"dry-run click would click app={args.app} at ({args.x}, {args.y}) via vision"
        )
        return (
            plan,
            {
                "command": "click",
                "dry_run": True,
                "app": args.app,
                "role": args.role,
                "name": args.name,
                "x": args.x,
                "y": args.y,
                "button": args.button,
                "count": args.count,
                "path_used": "ax" if use_ax else "vision",
            },
            [],
            {"path_used": "ax" if use_ax else "vision", "dry_run": True},
        )

    from mai_ui import Runtime

    active, active_class = ensure_active(args.app)
    async with Runtime() as runtime:
        device = await open_device(runtime)
        if use_ax:
            observation, limits_unsupported = await observe_ax(
                device, max_nodes=400, max_depth=8, timeout_ms=4000
            )
            app, subtree, nodes, by_id = resolve_app(observation, args.app)
            depth = app_depth(app, subtree)
            editable_count = sum(1 for node in subtree if node.editable is True)
            targets = [
                node
                for node in subtree
                if node.role == args.role
                and (node.name or "").casefold() == args.name.casefold()
            ]
            if not targets:
                raise HelperError(
                    "TARGET_NOT_FOUND",
                    f"app {app.name!r} 找不到 role={args.role!r} name={args.name!r} 的節點",
                    {
                        "app": app.name,
                        "depth": depth,
                        "editable_count": editable_count,
                    },
                )
            if len(targets) > 1:
                raise HelperError(
                    "TARGET_AMBIGUOUS",
                    f"app {app.name!r} 有多個 role={args.role!r} name={args.name!r} 節點",
                    {
                        "candidates": [
                            {
                                "index": node.index,
                                "role": node.role,
                                "name": node.name,
                                "bounds": None
                                if node.bounds is None
                                else {
                                    "x": node.bounds.x,
                                    "y": node.bounds.y,
                                    "width": node.bounds.width,
                                    "height": node.bounds.height,
                                },
                            }
                            for node in targets
                        ]
                    },
                )
            node = targets[0]
            ensure_active(args.app)
            receipt = await device.ui.ax.click(
                node, button=args.button, count=args.count
            )
            details = {
                "command": "click",
                "app": app.name,
                "role": node.role,
                "name": node.name,
                "target_index": node.index,
                "bounds": None
                if node.bounds is None
                else {
                    "x": node.bounds.x,
                    "y": node.bounds.y,
                    "width": node.bounds.width,
                    "height": node.bounds.height,
                },
                "receipt": receipt_dict(receipt),
                "path_used": "ax",
                "active_window": active,
                "active_class": active_class,
                "ax_nodes": len(nodes),
                "ax_truncated": observation.tree.truncated,
                "ax_timeout_ms": 4000,
                "limits_unsupported": limits_unsupported,
                "depth": depth,
                "editable_count": editable_count,
            }
            return (
                f"click app={app.name} role={node.role} name={node.name!r} path_used=ax",
                details,
                [],
                {
                    "path_used": "ax",
                    "ax_timeout_ms": 4000,
                    "ax_nodes": len(nodes),
                    "ax_truncated": observation.tree.truncated,
                    "ax_depth": depth,
                    "editable_count": editable_count,
                },
            )

        # 座標模式：先截圖，再用 AX frame（若拿得到）檢查座標。
        vision = await device.ui.vision.observe()
        if not vision.images:
            raise HelperError("CAPTURE_FAILED", "vision.observe 沒有回傳影像")
        image = vision.images[0]
        before = OUT_DIR / f"helper-before-{now_stamp()}.png"
        before.write_bytes(await image.read())
        frame_bounds = None
        frame_status = "unknown"
        ax_metrics = {"ax_timeout_ms": 4000, "editable_count": None}
        try:
            observation, limits_unsupported = await observe_ax(
                device, max_nodes=400, max_depth=8, timeout_ms=4000
            )
            ax_metrics.update(
                {
                    "ax_nodes": len(observation.tree.nodes),
                    "ax_truncated": observation.tree.truncated,
                    "ax_depth": None,
                    "editable_count": None,
                }
            )
            try:
                app, subtree, nodes, by_id = resolve_app(observation, args.app)
            except HelperError as exc:
                if exc.code in ("APP_NOT_FOUND", "APP_AMBIGUOUS"):
                    frame_status = f"skipped_{exc.code.lower()}"
                else:
                    raise
            else:
                ax_metrics["ax_depth"] = app_depth(app, subtree)
                ax_metrics["editable_count"] = sum(
                    1 for node in subtree if node.editable is True
                )
                frames = [
                    node.bounds
                    for node in subtree
                    if node.role == "frame" and node.bounds is not None
                ]
                if frames:
                    frame_bounds = frames[0]
                    frame_status = "checked"
                else:
                    frame_status = "no_frame_bounds"
        except Exception as exc:  # noqa: BLE001 - frame 檢查失敗時退回 active app check
            frame_status = f"check_failed:{type(exc).__name__}"
            limits_unsupported = False

        if frame_bounds is not None:
            inside = (
                frame_bounds.x <= args.x <= frame_bounds.x + frame_bounds.width
                and frame_bounds.y <= args.y <= frame_bounds.y + frame_bounds.height
            )
            if not inside:
                raise HelperError(
                    "POINT_OUTSIDE_APP",
                    f"({args.x}, {args.y}) 不在 app frame bounds 內",
                    {
                        "x": args.x,
                        "y": args.y,
                        "frame": {
                            "x": frame_bounds.x,
                            "y": frame_bounds.y,
                            "width": frame_bounds.width,
                            "height": frame_bounds.height,
                        },
                    },
                )
        if not (0 <= args.x < image.width and 0 <= args.y < image.height):
            raise HelperError(
                "POINT_OUTSIDE_SCREEN",
                f"({args.x}, {args.y}) 不在影像 {image.width}x{image.height} 內",
            )
        point = image.point(args.x, args.y)
        ensure_active(args.app)
        receipt = await device.ui.vision.click(point)
        details = {
            "command": "click",
            "app": args.app,
            "x": args.x,
            "y": args.y,
            "button": args.button,
            "count": args.count,
            "path_used": "vision",
            "screenshot": str(before),
            "based_on": getattr(receipt, "based_on", None),
            "frame_status": frame_status,
            "frame_bounds": None
            if frame_bounds is None
            else {
                "x": frame_bounds.x,
                "y": frame_bounds.y,
                "width": frame_bounds.width,
                "height": frame_bounds.height,
            },
            "active_window": active,
            "active_class": active_class,
            **ax_metrics,
            "receipt": receipt_dict(receipt),
        }
        return (
            f"click app={args.app} ({args.x}, {args.y}) path_used=vision",
            details,
            [],
            {"path_used": "vision", **ax_metrics},
        )


async def _verify_file(path: str | None, text: str) -> tuple[bool, str | None]:
    if not path:
        return False, None
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HelperError("VERIFY_FAILED", f"無法讀取 --verify-file {path}：{exc}")
    return text in content, content


async def cmd_type(args):
    if not args.text:
        raise HelperError("INVALID_ARGUMENT", "text 不能是空字串")
    if (args.role or args.name) and not (args.role and args.name):
        raise HelperError("INVALID_ARGUMENT", "--role 與 --name 要一起給")
    ascii_text = all(ord(char) < 128 for char in args.text)
    planned_path = "input.text" if ascii_text else (
        "ax.set_text" if (args.role and args.name) else "clipboard"
    )
    if is_dry_run(args):
        dry_updates = {"path_used": planned_path, "dry_run": True}
        if planned_path == "ax.set_text":
            dry_updates["ax_timeout_ms"] = args.timeout_ms
        return (
            f"dry-run type would type {text_preview(args.text)!r} app={args.app} "
            f"path={planned_path} verify_file={args.verify_file}",
            {
                "command": "type",
                "dry_run": True,
                "app": args.app,
                "text_preview": text_preview(args.text),
                "ascii": ascii_text,
                "role": args.role,
                "name": args.name,
                "path_used": planned_path,
                "verify_file": args.verify_file,
            },
            [],
            dry_updates,
        )

    from mai_ui import MuError, Runtime

    ensure_active(args.app)
    async with Runtime() as runtime:
        device = await open_device(runtime)
        if ascii_text:
            ensure_active(args.app)
            receipt = await device.ui.input.text(args.text)
            verified, content = await _verify_file(args.verify_file, args.text)
            if args.verify_file and not verified:
                raise HelperError(
                    "VERIFY_FAILED",
                    f"--verify-file {args.verify_file} 不含輸入文字",
                    {"file": args.verify_file, "content": content},
                )
            details = {
                "command": "type",
                "app": args.app,
                "text_preview": text_preview(args.text),
                "ascii": True,
                "path_used": "input.text",
                "receipt": receipt_dict(receipt),
                "verified": verified,
                "verify_file": args.verify_file,
                "note": None if args.verify_file else "未給 --verify-file，無法確認檔案內容",
            }
            return (
                f"type app={args.app} path_used=input.text verified={verified}",
                details,
                [],
                {"path_used": "input.text", "verified": verified},
            )

        ax_set_text = "skipped_no_target"
        ax_checked = False
        ax_effect = None
        limits_unsupported = False
        ax_metrics = {}
        if args.role and args.name:
            observation, limits_unsupported = await observe_ax(
                device, max_nodes=400, max_depth=8, timeout_ms=args.timeout_ms
            )
            ax_metrics = {
                "ax_timeout_ms": args.timeout_ms,
                "ax_nodes": len(observation.tree.nodes),
                "ax_truncated": observation.tree.truncated,
                "ax_depth": None,
            }
            try:
                app, subtree, nodes, by_id = resolve_app(observation, args.app)
            except HelperError as exc:
                if exc.code == "APP_AMBIGUOUS":
                    raise
                ax_set_text = "skipped_app_not_found"
            else:
                ax_metrics["ax_depth"] = app_depth(app, subtree)
                ax_metrics["editable_count"] = sum(
                    1 for node in subtree if node.editable is True
                )
                targets = [
                    node
                    for node in subtree
                    if node.role == args.role
                    and (node.name or "").casefold() == args.name.casefold()
                ]
                if not targets:
                    ax_set_text = "skipped_target_not_found"
                elif len(targets) > 1:
                    raise HelperError(
                        "TARGET_AMBIGUOUS",
                        f"app {app.name!r} 有多個 role={args.role!r} name={args.name!r} 節點",
                        {
                            "candidates": [
                                {"index": node.index, "role": node.role, "name": node.name}
                                for node in targets
                            ]
                        },
                    )
                else:
                    target = targets[0]
                    try:
                        ensure_active(args.app)
                        await device.ui.ax.set_text(target, args.text)
                        ax_checked = True
                        await asyncio.sleep(0.3)
                        recheck, _ = await observe_ax(
                            device, max_nodes=400, max_depth=8, timeout_ms=2000
                        )
                        new_target = next(
                            (node for node in recheck.tree.nodes if node.id == target.id),
                            None,
                        )
                        if new_target is None:
                            # 重新找同名節點
                            try:
                                _, re_subtree, _, _ = resolve_app(recheck, app.name)
                            except HelperError:
                                re_subtree = []
                            new_target = next(
                                (
                                    node
                                    for node in re_subtree
                                    if node.role == args.role
                                    and (node.name or "").casefold()
                                    == args.name.casefold()
                                ),
                                None,
                            )
                        ax_effect = bool(
                            new_target is not None
                            and args.text in (new_target.text or "")
                        )
                        if ax_effect:
                            ax_set_text = "success"
                        else:
                            ax_set_text = "no_effect"
                    except MuError as exc:
                        # AX set_text 失敗不是 fatal：記錄後改用 clipboard。
                        ax_set_text = f"error:{exc.code}"
                        ax_effect = False

        if ax_set_text == "success":
            verified, content = await _verify_file(args.verify_file, args.text)
            if args.verify_file and not verified:
                raise HelperError(
                    "VERIFY_FAILED",
                    f"--verify-file {args.verify_file} 不含輸入文字",
                    {"file": args.verify_file, "content": content},
                )
            details = {
                "command": "type",
                "app": args.app,
                "text_preview": text_preview(args.text),
                "ascii": False,
                "path_used": "ax.set_text",
                "ax_set_text": ax_set_text,
                "ax_checked": ax_checked,
                "ax_effect": ax_effect,
                "limits_unsupported": limits_unsupported,
                **ax_metrics,
                "verified": verified,
                "verify_file": args.verify_file,
                "note": None if args.verify_file else "未給 --verify-file，無法確認檔案內容",
            }
            return (
                f"type app={args.app} path_used=ax.set_text verified={verified}",
                details,
                [],
                {"path_used": "ax.set_text", **ax_metrics},
            )

        # clipboard fallback
        ensure_active(args.app)
        guard = ClipboardGuard()
        guard.save()
        restored = False
        try:
            write_receipt = await device.clipboard.write({"text/plain": args.text})
            ensure_active(args.app)
            ctrl_v = await press_key_checked(device, "v", ["ctrl"])
            await asyncio.sleep(0.4)
        finally:
            try:
                guard.restore()
                restored = True
            except HelperError as exc:
                restored = False
                restore_error = exc
            else:
                restore_error = None
        if not restored:
            raise HelperError(
                "CLIPBOARD_RESTORE_FAILED",
                "已用 clipboard 輸入，但還原剪貼簿失敗",
                {"clipboard_restored": False, "restore_error": str(restore_error)},
            )
        verified, content = await _verify_file(args.verify_file, args.text)
        if args.verify_file and not verified:
            raise HelperError(
                "VERIFY_FAILED",
                f"--verify-file {args.verify_file} 不含輸入文字",
                {"file": args.verify_file, "content": content},
            )
        details = {
            "command": "type",
            "app": args.app,
            "text_preview": text_preview(args.text),
            "ascii": False,
            "path_used": "clipboard",
            "ax_set_text": ax_set_text,
            "ax_checked": ax_checked,
            "ax_effect": ax_effect,
            "clipboard_write": receipt_dict(write_receipt),
            "ctrl_v": receipt_dict(ctrl_v),
            "clipboard_restored": True,
            **ax_metrics,
            "verified": verified,
            "verify_file": args.verify_file,
            "note": None if args.verify_file else "未給 --verify-file，無法確認檔案內容",
        }
        return (
            f"type app={args.app} path_used=clipboard verified={verified}",
            details,
            [],
            {"path_used": "clipboard", **ax_metrics},
        )


async def cmd_paste(args):
    if not args.text:
        raise HelperError("INVALID_ARGUMENT", "text 不能是空字串")
    if is_dry_run(args):
        return (
            f"dry-run paste would paste {text_preview(args.text)!r} app={args.app} via guarded clipboard",
            {
                "command": "paste",
                "dry_run": True,
                "app": args.app,
                "text_preview": text_preview(args.text),
                "path_used": "clipboard",
                "verify_file": args.verify_file,
            },
            [],
            {"path_used": "clipboard", "dry_run": True},
        )

    from mai_ui import Runtime

    ensure_active(args.app)
    async with Runtime() as runtime:
        device = await open_device(runtime)
        guard = ClipboardGuard()
        guard.save()
        restored = False
        restore_error = None
        try:
            write_receipt = await device.clipboard.write({"text/plain": args.text})
            ensure_active(args.app)
            ctrl_v = await press_key_checked(device, "v", ["ctrl"])
            await asyncio.sleep(0.4)
        finally:
            try:
                guard.restore()
                restored = True
            except HelperError as exc:
                restored = False
                restore_error = exc
        if not restored:
            raise HelperError(
                "CLIPBOARD_RESTORE_FAILED",
                "已用 clipboard 輸入，但還原剪貼簿失敗",
                {"clipboard_restored": False, "restore_error": str(restore_error)},
            )
        verified, content = await _verify_file(args.verify_file, args.text)
        if args.verify_file and not verified:
            raise HelperError(
                "VERIFY_FAILED",
                f"--verify-file {args.verify_file} 不含貼上文字",
                {"file": args.verify_file, "content": content},
            )
        details = {
            "command": "paste",
            "app": args.app,
            "text_preview": text_preview(args.text),
            "path_used": "clipboard",
            "clipboard_write": receipt_dict(write_receipt),
            "ctrl_v": receipt_dict(ctrl_v),
            "clipboard_restored": True,
            "verified": verified,
            "verify_file": args.verify_file,
            "note": None if args.verify_file else "未給 --verify-file，無法確認檔案內容",
        }
        return (
            f"paste app={args.app} path_used=clipboard verified={verified}",
            details,
            [],
            {"path_used": "clipboard"},
        )


async def cmd_key(args):
    modifiers = [part for part in (args.modifiers or "").split(",") if part]
    reject_dangerous_key(args.key, modifiers)
    if is_dry_run(args):
        return (
            f"dry-run key {args.key} modifiers={modifiers} app={args.app}",
            {
                "command": "key",
                "dry_run": True,
                "app": args.app,
                "key": args.key,
                "modifiers": modifiers,
                "path_used": "input.press_key",
            },
            [],
            {"path_used": "input.press_key", "dry_run": True},
        )

    from mai_ui import Runtime

    ensure_active(args.app)
    async with Runtime() as runtime:
        device = await open_device(runtime)
        ensure_active(args.app)
        receipt = await press_key_checked(device, args.key, modifiers)
        details = {
            "command": "key",
            "app": args.app,
            "key": args.key,
            "modifiers": modifiers,
            "path_used": "input.press_key",
            "receipt": receipt_dict(receipt),
        }
        return (
            f"key {args.key} modifiers={modifiers} app={args.app}",
            details,
            [],
            {"path_used": "input.press_key"},
        )


COMMANDS = {
    "shot": cmd_shot,
    "ax": cmd_ax,
    "click": cmd_click,
    "type": cmd_type,
    "paste": cmd_paste,
    "key": cmd_key,
}


def base_row(args) -> dict:
    return {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "command": args.command,
        "app": getattr(args, "app", None),
        "role": getattr(args, "role", None),
        "name": getattr(args, "name", None),
        "x": getattr(args, "x", None),
        "y": getattr(args, "y", None),
        "text_preview": text_preview(getattr(args, "text", None))
        or getattr(args, "key", None),
        "key": getattr(args, "key", None),
        "path_used": None,
        "ax_timeout_ms": None,
        "ax_nodes": None,
        "ax_truncated": None,
        "ax_depth": None,
        "editable_count": None,
        "dry_run": is_dry_run(args),
        "ok": None,
        "code": None,
        "ms": None,
        "latency_ms": None,
        "one_shot": None,
        "failure_reason": None,
    }


async def run(args) -> int:
    configure_environment()
    started = time.perf_counter()
    row = base_row(args)
    try:
        summary, details, lines, updates = await COMMANDS[args.command](args)
        row.update(updates or {})
        row.update(ok=True, one_shot=True)
        row["ms"] = round((time.perf_counter() - started) * 1000, 1)
        row["latency_ms"] = row["ms"]
        log_action(row)
        print(f"OK {summary}")
        for line in lines:
            print(line)
        print(json.dumps(details, ensure_ascii=False, default=str))
        return 0
    except HelperError as exc:
        row.update(
            ok=False,
            code=exc.code,
            failure_reason=exc.message,
            one_shot=False,
        )
        row["ms"] = round((time.perf_counter() - started) * 1000, 1)
        row["latency_ms"] = row["ms"]
        log_action(row)
        print(f"ERR {exc.code} {exc.message}")
        print(
            json.dumps(
                {
                    "command": args.command,
                    "code": exc.code,
                    "message": exc.message,
                    **exc.details,
                },
                ensure_ascii=False,
                default=str,
            )
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - 保留意外錯誤
        row.update(ok=False, code="HELPER_ERROR", failure_reason=str(exc), one_shot=False)
        row["ms"] = round((time.perf_counter() - started) * 1000, 1)
        row["latency_ms"] = row["ms"]
        log_action(row)
        print(f"ERR HELPER_ERROR {type(exc).__name__}: {exc}")
        print(
            json.dumps(
                {
                    "command": args.command,
                    "code": "HELPER_ERROR",
                    "message": str(exc),
                    "error": repr(exc),
                },
                ensure_ascii=False,
            )
        )
        traceback.print_exc(file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="只印計畫，不建立 Runtime／不送 XTEST／不寫 clipboard")

    parser = argparse.ArgumentParser(
        description="OpenGCUA desktop helper：同一個程序內 observe → act；AX index 不跨程序。",
        epilog=(
            "範例：\n"
            "  .venv/bin/python helper.py shot\n"
            "  .venv/bin/python helper.py ax --app kate --max-lines 40\n"
            "  .venv/bin/python helper.py click --app kate --role push_button --name '儲存'\n"
            "  .venv/bin/python helper.py click --app kate --x 500 --y 300\n"
            "  .venv/bin/python helper.py type 'abc' --app kate --verify-file /tmp/x.txt\n"
            "  .venv/bin/python helper.py paste '桌面測試' --app kate --verify-file /tmp/x.txt\n"
            "  .venv/bin/python helper.py key s --modifiers ctrl --app kate\n"
            "  DESKTOP_DRYRUN=1 .venv/bin/python helper.py ax --app kate\n"
            "\n"
            "ASCII 用 input.text；中文首選 ax.set_text（需 --role/--name），無效果才用 clipboard；"
            "clipboard 會先存後還原，非文字剪貼簿拒絕。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    shot = sub.add_parser("shot", parents=[common], help="截圖整個桌面")
    shot.add_argument("--out", help="輸出路徑（預設 /tmp/opencode/desktop/helper-shot-<ts>.png）")
    shot.set_defaults(func=cmd_shot)

    ax = sub.add_parser("ax", parents=[common], help="只輸出 --app 的 AX 子樹")
    ax.add_argument("--app", help="application 名稱（case-insensitive substring）")
    ax.add_argument("--max-nodes", type=int, default=400, help="要求 max_nodes（預設 400；v0.1.2 會拒絕）")
    ax.add_argument("--max-depth", type=int, default=8, help="要求 max_depth（預設 8；v0.1.2 會拒絕）")
    ax.add_argument("--timeout-ms", type=int, default=4000, choices=(2000, 4000), help="AX timeout（2000/4000）")
    ax.add_argument("--max-lines", type=int, default=80, help="最多輸出幾行（預設 80）")
    ax.set_defaults(func=cmd_ax)

    click = sub.add_parser("click", parents=[common], help="用 AX role/name 或座標點擊")
    click.add_argument("--app")
    click.add_argument("--role")
    click.add_argument("--name")
    click.add_argument("--x", type=float)
    click.add_argument("--y", type=float)
    click.add_argument("--button", default="primary", choices=("primary", "secondary", "middle"))
    click.add_argument("--count", type=int, default=1)
    click.set_defaults(func=cmd_click)

    type_p = sub.add_parser("type", parents=[common], help="輸入文字（ASCII 用 input.text；中文用 AX/clipboard）")
    type_p.add_argument("text")
    type_p.add_argument("--app")
    type_p.add_argument("--role")
    type_p.add_argument("--name")
    type_p.add_argument("--timeout-ms", type=int, default=4000, choices=(2000, 4000))
    type_p.add_argument("--verify-file")
    type_p.set_defaults(func=cmd_type)

    paste = sub.add_parser("paste", parents=[common], help="用 guarded clipboard 貼上文字")
    paste.add_argument("text")
    paste.add_argument("--app")
    paste.add_argument("--verify-file")
    paste.set_defaults(func=cmd_paste)

    key = sub.add_parser("key", parents=[common], help="按鍵／組合鍵")
    key.add_argument("key")
    key.add_argument("--modifiers", default="", help="逗號分隔：ctrl,alt,shift,super")
    key.add_argument("--app")
    key.set_defaults(func=cmd_key)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command != "shot" and not getattr(args, "app", None):
        started = time.perf_counter()
        row = base_row(args)
        row.update(
            ok=False,
            code="APP_REQUIRED",
            failure_reason="需要 --app",
            one_shot=False,
        )
        row["ms"] = round((time.perf_counter() - started) * 1000, 1)
        row["latency_ms"] = row["ms"]
        log_action(row)
        print("ERR APP_REQUIRED 需要 --app")
        print(
            json.dumps(
                {
                    "command": args.command,
                    "code": "APP_REQUIRED",
                    "message": "需要 --app",
                },
                ensure_ascii=False,
            )
        )
        return 1
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
