# Copyright 2026 the solidworks_operation project (fork addition to solidworks-mcp).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Modal-dialog watchdog.

A modal dialog in SOLIDWORKS blocks the COM call that opened it, and with it
this whole server, until somebody clicks.  The watchdog runs on a plain
Win32 thread (no COM), notices when the SOLIDWORKS main frame becomes
disabled during a tool call, waits a grace period, and then closes the
dialog so the call can return -- reporting what it closed.  Before a call it
can also refuse to start while a dialog is already up, which is far cheaper
than deadlocking.

Only Win32 window APIs are used off the COM thread; PostMessage is safe
cross-thread.  Set SW_MCP_AUTO_DISMISS_DIALOGS=0 to only report, never close.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import win32con
import win32gui
import win32process

from .sw_core import logger, result, running_app, tool, value

_TIMEOUT_S = float(os.environ.get("SW_MCP_DIALOG_TIMEOUT_S", "8"))
_AUTO_DISMISS = os.environ.get("SW_MCP_AUTO_DISMISS_DIALOGS", "1") != "0"
_POLL_S = 0.5

_IDOK, _IDCANCEL = 1, 2


class DialogWatchdog:
    def __init__(self) -> None:
        self.pid: int | None = None
        self.main_hwnd: int | None = None
        self.events: list[dict[str, Any]] = []
        self._active = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- configuration (COM thread) -------------------------------------------------

    def configure(self, app: Any) -> None:
        """Capture the process id and main frame handle once; both are stable for the session."""
        if self.pid and self.main_hwnd and win32gui.IsWindow(self.main_hwnd):
            return
        try:
            self.pid = int(value(app, "GetProcessID"))
        except Exception:
            self.pid = None
        try:
            frame = value(app, "Frame")
            self.main_hwnd = int(value(frame, "GetHWnd"))
        except Exception:
            self.main_hwnd = None
        if self.pid and not self.main_hwnd:
            self.main_hwnd = self._find_main_window()

    def _find_main_window(self) -> int | None:
        found: list[int] = []

        def visit(hwnd: int, _: Any) -> bool:
            if win32gui.IsWindowVisible(hwnd) and self._owned_by_pid(hwnd) and not win32gui.GetWindow(hwnd, win32con.GW_OWNER):
                found.append(hwnd)
            return True

        win32gui.EnumWindows(visit, None)
        return found[0] if found else None

    def _owned_by_pid(self, hwnd: int) -> bool:
        try:
            return win32process.GetWindowThreadProcessId(hwnd)[1] == self.pid
        except Exception:
            return False

    # -- observation (any thread) -----------------------------------------------------

    def is_blocked(self) -> bool:
        if not self.main_hwnd:
            return False
        try:
            return win32gui.IsWindow(self.main_hwnd) and not win32gui.IsWindowEnabled(self.main_hwnd)
        except Exception:
            return False

    def modal_windows(self) -> list[dict[str, Any]]:
        """Visible top-level windows of the SOLIDWORKS process other than the main frame."""
        if not self.pid:
            return []
        windows: list[dict[str, Any]] = []

        def visit(hwnd: int, _: Any) -> bool:
            if hwnd == self.main_hwnd or not win32gui.IsWindowVisible(hwnd) or not self._owned_by_pid(hwnd):
                return True
            owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
            class_name = win32gui.GetClassName(hwnd)
            if class_name != "#32770" and owner != self.main_hwnd:
                return True
            windows.append({
                "hwnd": hwnd,
                "class": class_name,
                "title": win32gui.GetWindowText(hwnd),
                "text": self._dialog_text(hwnd),
            })
            return True

        win32gui.EnumWindows(visit, None)
        return windows

    @staticmethod
    def _dialog_text(hwnd: int, limit: int = 400) -> str:
        pieces: list[str] = []

        def visit(child: int, _: Any) -> bool:
            try:
                cls = win32gui.GetClassName(child)
                if cls in ("Static", "Button", "SysLink", "RichEdit20W", "Edit"):
                    text = win32gui.GetWindowText(child).strip()
                    if text:
                        pieces.append(text)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumChildWindows(hwnd, visit, None)
        except Exception:
            pass
        return " | ".join(pieces)[:limit]

    # -- actions ------------------------------------------------------------------------

    @staticmethod
    def dismiss(hwnd: int, action: str = "close") -> None:
        if action == "ok":
            win32gui.PostMessage(hwnd, win32con.WM_COMMAND, _IDOK, 0)
        elif action == "cancel":
            win32gui.PostMessage(hwnd, win32con.WM_COMMAND, _IDCANCEL, 0)
        else:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)

    # -- per-call lifecycle (COM thread) ---------------------------------------------

    def begin_call(self) -> None:
        with self._lock:
            self.events = []
        self._active.set()
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="sw-dialog-watchdog", daemon=True)
            self._thread.start()

    def end_call(self) -> list[dict[str, Any]]:
        self._active.clear()
        with self._lock:
            events, self.events = self.events, []
        return events

    def _loop(self) -> None:
        blocked_since: float | None = None
        handled: set[int] = set()
        while not self._stop.is_set():
            if not self._active.is_set():
                blocked_since = None
                handled.clear()
                time.sleep(_POLL_S)
                continue
            try:
                if self.is_blocked():
                    now = time.monotonic()
                    blocked_since = blocked_since or now
                    if now - blocked_since >= _TIMEOUT_S:
                        for window in self.modal_windows():
                            if window["hwnd"] in handled:
                                continue
                            handled.add(window["hwnd"])
                            event = {
                                "title": window["title"],
                                "text": window["text"],
                                "waited_s": round(now - blocked_since, 1),
                                "action": "closed" if _AUTO_DISMISS else "reported",
                            }
                            logger.warning("Modal dialog during tool call: %s | %s", window["title"], window["text"][:120])
                            if _AUTO_DISMISS:
                                self.dismiss(window["hwnd"], "close")
                            with self._lock:
                                self.events.append(event)
                else:
                    blocked_since = None
                    handled.clear()
            except Exception:
                logger.debug("watchdog poll failed", exc_info=True)
            time.sleep(_POLL_S)


WATCHDOG = DialogWatchdog()


def blocked_report() -> dict[str, Any] | None:
    """If SOLIDWORKS is currently stuck behind a modal dialog, describe it."""
    if not WATCHDOG.is_blocked():
        return None
    windows = WATCHDOG.modal_windows()
    summary = "; ".join(f"'{w['title']}' {w['text']}".strip() for w in windows) or "an unidentified modal window"
    return result(
        False,
        f"SOLIDWORKS is blocked by a modal dialog: {summary}. Close it (dismiss_dialog) before calling other tools.",
        dialogs=[{"title": w["title"], "text": w["text"]} for w in windows],
    )


@tool(
    "dismiss_dialog",
    "Close a modal SOLIDWORKS dialog that is blocking the session (WM_CLOSE by default, or press "
    "its OK / Cancel). Read-only tools fail with a 'blocked by a modal dialog' message when one is up.",
    {
        "action": {"type": "string", "enum": ["close", "ok", "cancel"], "default": "close"},
        "title_contains": {"type": "string", "description": "Only act on dialogs whose title contains this text."},
    },
)
def dismiss_dialog(args: dict[str, Any]) -> dict[str, Any]:
    if WATCHDOG.pid is None:
        WATCHDOG.configure(running_app())
    windows = WATCHDOG.modal_windows()
    needle = str(args.get("title_contains") or "").lower()
    if needle:
        windows = [w for w in windows if needle in (w["title"] or "").lower()]
    if not windows:
        return result(True, "No modal dialog is open.", blocked=WATCHDOG.is_blocked())
    action = str(args.get("action", "close"))
    for window in windows:
        WATCHDOG.dismiss(window["hwnd"], action)
    time.sleep(0.4)
    return result(
        True,
        f"Sent {action} to {len(windows)} dialog(s).",
        dialogs=[{"title": w["title"], "text": w["text"]} for w in windows],
        still_blocked=WATCHDOG.is_blocked(),
    )
