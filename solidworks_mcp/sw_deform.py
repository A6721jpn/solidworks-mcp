"""One version-scoped native Deform transaction, without pointer input."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math
import ntpath
import re
import time
from typing import Any

import win32api
import win32con
import win32gui
import win32process
import pythoncom
from win32com.client import VARIANT

from .sw_core import (
    as_list, document_path_key, flag_methods, require_document_path,
    result, select_by_id, select_object, tool, value,
)
from .sw_sheet import _body_box_mm, _configurations, _features, _rename, _unused_feature_name


@dataclass(frozen=True)
class CurvePair:
    source_sketch: str
    target_sketch: str


@dataclass(frozen=True)
class DeformRequest:
    expected_document_path: str
    configuration: str
    body_name: str
    curve_pairs: tuple[CurvePair, ...]
    shape_accuracy: float
    fixed_edges: bool
    feature_name: str
    validate_only: bool = False

    @classmethod
    def parse(cls, args: dict[str, Any]) -> "DeformRequest":
        def name(raw: Any, label: str) -> str:
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError(f"{label} must be a nonempty string.")
            return raw

        path = name(args.get("expected_document_path"), "expected_document_path")
        if not ntpath.isabs(path) or not ntpath.splitdrive(path)[0] or ntpath.splitext(path)[1].lower() != ".sldprt":
            raise ValueError("expected_document_path must be an absolute saved .SLDPRT path.")
        pairs = args.get("curve_pairs")
        if not isinstance(pairs, list) or not pairs:
            raise ValueError("curve_pairs must be a nonempty ordered array.")
        parsed = []
        for pair in pairs:
            if not isinstance(pair, dict) or set(pair) != {"source_sketch", "target_sketch"}:
                raise ValueError("Each curve pair requires source_sketch and target_sketch only.")
            parsed.append(CurvePair(name(pair["source_sketch"], "source_sketch"),
                                    name(pair["target_sketch"], "target_sketch")))
        accuracy = args.get("shape_accuracy")
        if (isinstance(accuracy, bool) or not isinstance(accuracy, (float, int))
                or not math.isfinite(accuracy) or not 0 <= accuracy <= 1):
            raise ValueError("shape_accuracy must be a finite fraction between 0 and 1.")
        for label in ("fixed_edges", "validate_only"):
            if not isinstance(args.get(label, False if label == "validate_only" else None), bool):
                raise ValueError(f"{label} must be boolean.")
        return cls(path, name(args.get("configuration"), "configuration"),
                   name(args.get("body_name"), "body_name"), tuple(parsed), float(accuracy),
                   args["fixed_edges"], name(args.get("feature_name"), "feature_name"),
                   args.get("validate_only", False))


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_send = _user32.SendMessageTimeoutW
_send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                  wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
_send.restype = wintypes.LPARAM
_query_image = ctypes.WinDLL("kernel32", use_last_error=True).QueryFullProcessImageNameW
_query_image.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_query_image.restype = wintypes.BOOL


def _message(hwnd: int, message: int, wparam: int = 0, lparam: int = 0) -> int:
    answer = ctypes.c_size_t()
    if not _send(hwnd, message, wparam, lparam, 0x2 | 0x20, 1500, ctypes.byref(answer)):
        raise RuntimeError(f"Native control message timed out or failed for window {hwnd}.")
    return ctypes.c_ssize_t(answer.value).value


def _text(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(2048)
    _message(hwnd, 0x000D, len(buf), ctypes.addressof(buf))
    return buf.value


def _children(frame: int) -> list[dict[str, Any]]:
    controls = []

    def visit(hwnd: int, _: Any) -> bool:
        if win32gui.IsWindowVisible(hwnd):
            controls.append({"hwnd": hwnd, "parent": win32gui.GetParent(hwnd),
                             "class": win32gui.GetClassName(hwnd), "id": win32gui.GetDlgCtrlID(hwnd),
                             "enabled": bool(win32gui.IsWindowEnabled(hwnd))})
        return True

    win32gui.EnumChildWindows(frame, visit, None)
    return controls


def _is_solidworks_process(pid: int) -> bool:
    handle = win32api.OpenProcess(0x1000, False, pid)
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not _query_image(int(handle), 0, buffer, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        return ntpath.basename(buffer.value).lower() == "sldworks.exe"
    finally:
        handle.Close()


def native_frames() -> list[dict[str, Any]]:
    """No COM access. Safe before the dispatcher's first model read."""
    frames = []

    def visit(hwnd: int, _: Any) -> bool:
        if (not win32gui.IsWindowVisible(hwnd) or win32gui.GetWindow(hwnd, win32con.GW_OWNER)
                or "SOLIDWORKS" not in win32gui.GetWindowText(hwnd).upper()):
            return True
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        try:
            if _is_solidworks_process(pid):
                frames.append({"frame": hwnd, "pid": pid, "title": win32gui.GetWindowText(hwnd)})
        except Exception as exc:
            raise RuntimeError(f"Could not verify candidate SOLIDWORKS process {pid}: {exc}") from exc
        return True

    win32gui.EnumWindows(visit, None)
    return frames


def active_native_commands() -> list[dict[str, Any]]:
    commands = []
    for frame in native_frames():
        controls = _children(frame["frame"])
        markers = [c for c in controls if c["id"] == 6771 and c["class"] == "Button"]
        if markers:
            commands.append({**frame, "command": "Deform", "ambiguous": len(markers) != 1})
    return commands


def visible_property_managers() -> list[dict[str, Any]]:
    """Only a creation precondition; never operates an unrelated native page."""
    pages = []
    for frame in native_frames():
        controls = _children(frame["frame"])
        for cancel in (c for c in controls if c["id"] == 4903 and c["class"] == "Button"):
            siblings = [c for c in controls if c["parent"] == cancel["parent"] and c["class"] == "Button"]
            headers = [c for c in siblings if c["id"] == 99]
            if any(c["id"] == 4900 for c in siblings) and len(headers) == 1:
                title = _text(headers[0]["hwnd"])
                if title:
                    pages.append({**frame, "property_manager": title})
    return pages


@dataclass(frozen=True)
class Owner:
    frame: int
    pid: int
    document_path: str
    caption: str
    revision: str

    @classmethod
    def capture(cls, app: Any, path: str) -> "Owner":
        revision = str(value(app, "RevisionNumber"))
        if revision.split(".")[0] != "34":
            raise RuntimeError(f"Deform native controls support revision 34 only, got {revision}.")
        hwnd = int(value(value(app, "Frame"), "GetHWnd"))
        pid = int(value(app, "GetProcessID"))
        if win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
            raise RuntimeError("SOLIDWORKS frame and process do not match.")
        return cls(hwnd, pid, document_path_key(path), win32gui.GetWindowText(hwnd), revision)

    def check_frame(self) -> None:
        if (not win32gui.IsWindow(self.frame)
                or win32process.GetWindowThreadProcessId(self.frame)[1] != self.pid):
            raise RuntimeError("SOLIDWORKS frame ownership was lost.")

    def check(self) -> None:
        self.check_frame()
        # The full path was proved by COM before opening. A caption change invalidates that proof.
        caption = win32gui.GetWindowText(self.frame)
        if caption.replace("*", "") != self.caption.replace("*", ""):
            raise RuntimeError(f"Active SOLIDWORKS document caption changed during Deform: expected {self.caption!r}, got {caption!r}.")


_OWNERS: dict[tuple[int, int], Owner] = {}
_CONTROL_MAP = {
    "curve_mode": (6771, "Button"), "preview": (7170, "Button"),
    "fixed_edges": (7047, "Button"), "match_none": (7965, "Button"),
    "stiffness_middle": (6802, "Button"), "add_pair": (8101, "Button"),
    "accept": (4900, "Button"), "cancel": (4903, "Button"),
    "pair_label": (8100, "Static"),
    "previous_pair": (8098, "Button"), "next_pair": (8099, "Button"),
}
# Verified against the revision 34 three-pair configure/cancel fixture.
_SELECTION_MAP = {"source": (2652, "ListBox"), "target": (2558, "ListBox"),
                  "body": (2656, "ListBox"), "shape_accuracy": (6779, "msctls_trackbar32")}


class DeformPanel:
    def __init__(self, owner: Owner):
        self.owner = owner

    def controls(self) -> list[dict[str, Any]]:
        self.owner.check()
        controls = _children(self.owner.frame)
        markers = [c for c in controls if (c["id"], c["class"]) == _CONTROL_MAP["curve_mode"]]
        if len(markers) != 1:
            raise RuntimeError(f"Expected one native Deform panel, found {len(markers)}.")
        panel = markers[0]["parent"]
        if win32gui.GetClassName(panel) != "#32770":
            raise RuntimeError("Unsupported native Deform panel ancestry.")
        return controls

    def control(self, role: str) -> dict[str, Any]:
        controls = self.controls()
        control_id, klass = {**_CONTROL_MAP, **_SELECTION_MAP}[role]
        found = [c for c in controls if c["id"] == control_id and c["class"] == klass]
        if len(found) != 1:
            raise RuntimeError(f"Native Deform control '{role}' is missing or ambiguous.")
        marker = next(c for c in controls if (c["id"], c["class"]) == _CONTROL_MAP["curve_mode"])
        if role not in {"accept", "cancel"} and found[0]["parent"] != marker["parent"]:
            raise RuntimeError(f"Unexpected parent of Deform control '{role}'.")
        if role in {"accept", "cancel"}:
            sheet = win32gui.GetParent(win32gui.GetParent(marker["parent"]))
            if found[0]["parent"] != sheet or win32gui.GetClassName(sheet) != "AfxWnd140u":
                raise RuntimeError("Deform confirmation button does not belong to the identified page.")
        if win32process.GetWindowThreadProcessId(found[0]["hwnd"])[1] != self.owner.pid:
            raise RuntimeError("Native control belongs to another process.")
        return found[0]

    def click(self, role: str) -> None:
        control = self.control(role)
        if not control["enabled"]:
            raise RuntimeError(f"Deform control '{role}' is disabled.")
        if role in {"accept", "cancel"}:
            win32gui.PostMessage(control["hwnd"], 0x00F5, 0, 0)
        else:
            _message(control["hwnd"], 0x00F5)

    def checked(self, role: str) -> bool:
        return _message(self.control(role)["hwnd"], 0x00F0) == 1

    def set_checked(self, role: str, wanted: bool) -> None:
        if self.checked(role) != wanted:
            self.click(role)
        if self.checked(role) != wanted:
            raise RuntimeError(f"Native Deform '{role}' did not retain the requested setting.")

    def readback(self) -> dict[str, Any]:
        controls = self.controls()
        observed = []
        ids = {c[0] for c in (*_CONTROL_MAP.values(), *_SELECTION_MAP.values())}
        ids.update({2652, 2558, 2656, 2654, 2651, 6779, 7201})
        for c in controls:
            if c["id"] not in ids:
                continue
            row = {**c, "text": _text(c["hwnd"])}
            if c["class"] == "Button":
                row["check"] = _message(c["hwnd"], 0x00F0)
            elif c["class"] == "msctls_trackbar32":
                row.update(minimum=_message(c["hwnd"], 0x0401),
                           maximum=_message(c["hwnd"], 0x0402), position=_message(c["hwnd"], 0x0400))
            elif c["class"] == "ListBox":
                row["count"] = self.list_count(c)
                row["text_available"] = False
            observed.append(row)
        return {"frame": self.owner.frame, "pid": self.owner.pid, "revision": self.owner.revision,
                "command": "Deform", "controls": observed}

    @staticmethod
    def list_count(control: dict[str, Any]) -> int:
        hwnd = control["hwnd"]
        count = _message(hwnd, 0x018B)
        if not 0 <= count <= 128:
            raise RuntimeError("Unsupported Deform list count.")
        # These controls are owner-drawn without LBS_HASSTRINGS. LB_GETTEXT
        # would return item-data pointers, not labels. Entity proof uses COM.
        return count

    def set_accuracy(self, fraction: float) -> dict[str, Any]:
        control = self.control("shape_accuracy")
        hwnd = control["hwnd"]
        low, high = _message(hwnd, 0x0401), _message(hwnd, 0x0402)
        if high <= low or high > 65535 or low < 0:
            raise RuntimeError("Unsupported Deform accuracy slider range.")
        position = round(low + fraction * (high - low))
        _message(hwnd, 0x0405, 1, position)
        _message(control["parent"], 0x0114, 4 | (position << 16), hwnd)
        _message(control["parent"], 0x0114, 8, hwnd)
        actual = _message(hwnd, 0x0400)
        if actual != position:
            raise RuntimeError("Native accuracy position differs after notification.")
        return {"minimum": low, "maximum": high, "position": actual,
                "fraction": (actual - low) / (high - low)}

    def is_open(self) -> bool:
        self.owner.check_frame()
        return any(c["id"] == 6771 and c["class"] == "Button" for c in _children(self.owner.frame))

    def close(self, accept: bool) -> None:
        self.click("accept" if accept else "cancel")
        deadline = time.monotonic() + (60 if accept else 3)
        while self.is_open() and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.is_open():
            raise RuntimeError("Deform command did not close after the native button action.")


_NAME = {"type": "string", "minLength": 1}
_PATH = {"type": "string", "description": "Exact active saved part path. No document switching."}


@tool("inspect_solidworks_ui", "Read native SOLIDWORKS frame, active Deform command and control values without COM model snapshots.")
def inspect_solidworks_ui(args: dict[str, Any]) -> dict[str, Any]:
    frames = native_frames()
    for frame in frames:
        markers = [c for c in _children(frame["frame"]) if c["id"] == 6771 and c["class"] == "Button"]
        frame["command"] = "Deform" if markers else None
        if markers:
            owner = Owner(frame["frame"], frame["pid"], "", frame["title"], "unverified")
            frame["readback"] = DeformPanel(owner).readback()
    return result(True, "Native SOLIDWORKS UI state read without model access.", frames=frames)


def _activate_frame(owner: Owner) -> dict[str, Any]:
    attempts, cleanup_errors = [], []
    state = {"foreground": False, "foreground_frame": None,
             "attempts": attempts, "cleanup_errors": cleanup_errors}

    def attempt(stage: str, action: Any = None) -> bool:
        observed = {"stage": stage}
        try:
            if action is not None:
                action()
        except Exception as exc:
            observed["error"] = str(exc)
        try:
            state["foreground_frame"] = int(win32gui.GetForegroundWindow())
            state["foreground"] = state["foreground_frame"] == owner.frame
            observed["foreground_frame"] = state["foreground_frame"]
        except Exception as exc:
            state["foreground"], state["foreground_frame"] = False, None
            observed["readback_error"] = str(exc)
        attempts.append(observed)
        return state["foreground"]

    try:
        owner.check()
    except Exception as exc:
        attempts.append({"stage": "ownership_guard", "error": str(exc)})
        return state
    if attempt("initial_readback"):
        return state
    if attempt("restore", lambda: win32gui.ShowWindow(owner.frame, win32con.SW_RESTORE)):
        return state
    if attempt("set_foreground", lambda: win32gui.SetForegroundWindow(owner.frame)):
        return state
    attached = []
    try:
        owner.check()
        win32gui.PeekMessage(None, 0, 0, win32con.PM_NOREMOVE)
        current_thread = win32api.GetCurrentThreadId()
        target_thread = win32process.GetWindowThreadProcessId(owner.frame)[0]
        foreground = win32gui.GetForegroundWindow()
        foreground_thread = win32process.GetWindowThreadProcessId(foreground)[0] if foreground else 0
        for thread in dict.fromkeys((foreground_thread, target_thread)):
            if thread and thread != current_thread:
                try:
                    win32process.AttachThreadInput(current_thread, thread, True)
                    attached.append(thread)
                    attempts.append({"stage": "attach_thread_input", "thread": thread})
                except Exception as exc:
                    attempts.append({"stage": "attach_thread_input", "thread": thread, "error": str(exc)})
        owner.check()
        if not attempt("bring_to_top", lambda: win32gui.BringWindowToTop(owner.frame)):
            attempt("attached_set_foreground", lambda: win32gui.SetForegroundWindow(owner.frame))
    except Exception as exc:
        attempts.append({"stage": "attached_activation", "error": str(exc)})
    finally:
        for thread in reversed(attached):
            try:
                win32process.AttachThreadInput(current_thread, thread, False)
            except Exception as exc:
                cleanup_errors.append({"stage": "detach_thread_input", "thread": thread, "error": str(exc)})
    attempt("final_readback")
    return state


@tool("activate_solidworks_window", "Activate the SOLIDWORKS frame after checking the exact active document. Does not switch documents.",
      {"expected_document_path": _PATH}, ["expected_document_path"])
def activate_solidworks_window(args: dict[str, Any]) -> dict[str, Any]:
    app, _ = require_document_path(args["expected_document_path"])
    owner = Owner.capture(app, args["expected_document_path"])
    _OWNERS[(owner.pid, owner.frame)] = owner
    readback = _activate_frame(owner)
    return result(readback["foreground"] and not readback["cleanup_errors"],
                  "SOLIDWORKS foreground activation checked.", frame=owner.frame, pid=owner.pid, **readback)


@tool("cancel_solidworks_command", "Cancel only the exact Deform command after light full-path, PID/frame and native-panel guards. Bypasses model geometry snapshots. Never sends Escape.",
      {"expected_document_path": _PATH, "expected_command": {"type": "string", "enum": ["Deform"]},
       "expected_frame": {"type": "integer"}, "expected_pid": {"type": "integer"}},
      ["expected_document_path", "expected_command", "expected_frame", "expected_pid"])
def cancel_solidworks_command(args: dict[str, Any]) -> dict[str, Any]:
    stage, cancellation_attempted = "identifying_command", False
    readback, cleanup_errors = {}, []
    command_open = None
    try:
        if args["expected_command"] != "Deform":
            raise ValueError("Only the exact Deform command is supported.")
        commands = active_native_commands()
        matches = [c for c in commands if c["pid"] == args["expected_pid"] and c["frame"] == args["expected_frame"]]
        if len(matches) != 1 or matches[0]["ambiguous"]:
            return result(False, "The exact requested native Deform command was not identified.",
                          may_have_changes=False, command_still_open=bool(matches),
                          stage=stage, cancellation_attempted=False, model_unchanged_verified=False)
        stage = "verifying_document"
        app, _ = require_document_path(args["expected_document_path"], 1)
        owner = Owner.capture(app, args["expected_document_path"])
        if (owner.pid, owner.frame) != (args["expected_pid"], args["expected_frame"]):
            raise RuntimeError("COM document belongs to a different SOLIDWORKS frame/process.")
        _OWNERS[(owner.pid, owner.frame)] = owner
        stage = "reading_panel"
        panel = DeformPanel(owner)
        if not panel.is_open():
            return result(True, "No Deform command remains open.", command_still_open=False,
                          model_unchanged_verified=False)
        readback = panel.readback()
        stage, cancellation_attempted = "cancelling", True
        panel.close(False)
        stage = "verifying_closed"
        if panel.is_open():
            raise RuntimeError("Deform command remains open after cancellation.")
        return result(True, "Deform command cancelled without model access or saving.",
                      command_still_open=False, readback=readback, model_unchanged_verified=False)
    except Exception as exc:
        cleanup_errors.append({"stage": stage, "error": str(exc)})
        try:
            observed = inspect_solidworks_ui({})["data"]["frames"]
            readback["failure_native_frames"] = observed
            command_open = any(frame["frame"] == args["expected_frame"]
                               and frame["pid"] == args["expected_pid"]
                               and frame["command"] == "Deform" for frame in observed)
        except Exception as read_error:
            cleanup_errors.append({"stage": "native_reinspection", "error": str(read_error)})
        return result(False, f"Deform cancellation failed during {stage}: {exc}",
                      stage=stage, attempted_cancellation_stage=stage,
                      cancellation_attempted=cancellation_attempted,
                      command_still_open=command_open, may_have_changes=True,
                      readback=readback, cleanup_errors=cleanup_errors, model_unchanged_verified=False)


def _model_readback(doc: Any) -> dict[str, Any]:
    bodies = as_list(flag_methods(doc, "GetBodies2").GetBodies2(0, False))
    summaries = []
    for body in bodies:
        mass = list(flag_methods(body, "GetMassProperties").GetMassProperties(1.0))
        vertices = sorted([list(value(vertex, "GetPoint"))
                           for vertex in as_list(value(body, "GetVertices"))])
        summaries.append({"name": str(value(body, "Name")), "box_mm": _body_box_mm(body),
                          "volume_mm3": float(mass[3]) * 1e9, "vertices_m": vertices,
                          "fault_count": int(value(body, "Check2"))})
    return {"features": [{"name": str(value(f, "Name")), "type": str(value(f, "GetTypeName2"))}
                          for f in _features(doc)], "bodies": summaries}


def _geometry(model: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(({key: val for key, val in body.items() if key != "name"}
                   for body in model["bodies"]), key=repr)


def _same_bodies(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> bool:
    if len(before) != len(after):
        return False
    for old, new in zip(before, after):
        if ({key: val for key, val in old.items() if key != "volume_mm3"}
                != {key: val for key, val in new.items() if key != "volume_mm3"}):
            return False
        if not math.isclose(old["volume_mm3"], new["volume_mm3"], rel_tol=1e-12, abs_tol=1e-9):
            return False
    return True


def _same_model_state(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return before["features"] == after["features"] and _same_bodies(before["bodies"], after["bodies"])


def _prepare(doc: Any, request: DeformRequest) -> tuple[Any, list[tuple[Any, Any]]]:
    if _configurations(doc)[1] != request.configuration:
        raise ValueError("Requested configuration must already be active; no implicit switch is performed.")
    _unused_feature_name(doc, request.feature_name)
    bodies = as_list(flag_methods(doc, "GetBodies2").GetBodies2(0, False))
    matched_bodies = [body for body in bodies if str(value(body, "Name")) == request.body_name]
    if len(matched_bodies) != 1:
        raise ValueError("body_name must identify exactly one solid body.")
    features = _features(doc)

    def segment(name: str) -> Any:
        found = [f for f in features if str(value(f, "Name")) == name]
        if len(found) != 1 or str(value(found[0], "GetTypeName2")) not in {"ProfileFeature", "3DProfileFeature"}:
            raise ValueError(f"'{name}' must resolve uniquely to a 2D or 3D sketch.")
        sketch = value(found[0], "GetSpecificFeature2")
        segments = as_list(value(sketch, "GetSketchSegments"))
        if len(segments) != 1 or value(segments[0], "GetCurve") is None:
            raise ValueError(f"Sketch '{name}' must contain exactly one curve segment.")
        return segments[0]

    return matched_bodies[0], [(segment(pair.source_sketch), segment(pair.target_sketch)) for pair in request.curve_pairs]


def _configure(panel: DeformPanel, app: Any, doc: Any, request: DeformRequest,
               body: Any, segments: list[tuple[Any, Any]]) -> dict[str, Any]:
    panel.set_checked("curve_mode", True)
    panel.set_checked("preview", False)
    panel.set_checked("fixed_edges", request.fixed_edges)
    panel.set_checked("match_none", True)
    accuracy = panel.set_accuracy(request.shape_accuracy)
    manager = flag_methods(value(doc, "SelectionManager"), "GetSelectedObjectCount2", "GetSelectedObject6")
    flag_methods(app, "IsSame")

    def identity(role: str, mark: int, entity: Any) -> dict[str, Any]:
        count = panel.list_count(panel.control(role))
        selected_count = int(manager.GetSelectedObjectCount2(mark))
        if count != 1 or selected_count != 1:
            raise RuntimeError(f"Native {role} slot requires one entity; list={count}, selection={selected_count}.")
        actual = manager.GetSelectedObject6(1, mark)
        equality = int(app.IsSame(entity, actual)) if actual is not None else -1
        if equality != 1:
            raise RuntimeError(f"Native {role} selection identity differs or is unknown ({equality}).")
        return {"native_list_count": count, "selected_count": selected_count, "object_equality": equality}

    body_readback = identity("body", 2048, body)
    pairs = []
    for index, (pair, (source, target)) in enumerate(zip(request.curve_pairs, segments)):
        if index:
            panel.click("add_pair")
        for role, entity, mark, sketch in (("source", source, 4, pair.source_sketch),
                                           ("target", target, 8, pair.target_sketch)):
            panel.owner.check()
            if not select_object(doc, entity, mark, True):
                raise RuntimeError(f"Could not select Deform {role} sketch '{sketch}'.")
            identity(role, mark, entity)
        label = _text(panel.control("pair_label")["hwnd"])
        numbers = re.findall(r"\[(\d+)\]", label)
        if numbers != [str(index + 1)]:
            raise RuntimeError(f"Native Deform pair counter did not advance to {index + 1}: {label!r}.")
        pairs.append({"source": identity("source", 4, source),
                      "target": identity("target", 8, target),
                      "native_pair_label": label})
    for index in reversed(range(len(segments))):
        if index < len(segments) - 1:
            panel.click("previous_pair")
        label = _text(panel.control("pair_label")["hwnd"])
        if re.findall(r"\[(\d+)\]", label) != [str(index + 1)]:
            raise RuntimeError("Stored Deform pair index could not be verified.")
        source, target = segments[index]
        pairs[index]["stored_readback"] = {"source": identity("source", 4, source),
                                           "target": identity("target", 8, target),
                                           "native_pair_label": label}
    for role, wanted in (("curve_mode", True), ("preview", False), ("fixed_edges", request.fixed_edges),
                          ("match_none", True)):
        if panel.checked(role) != wanted:
            raise RuntimeError(f"Native Deform '{role}' changed while selecting curve pairs.")
    if _message(panel.control("shape_accuracy")["hwnd"], 0x0400) != accuracy["position"]:
        raise RuntimeError("Native Deform accuracy changed while selecting curve pairs.")
    return {"body": body_readback, "curve_pairs": pairs, "accuracy": accuracy,
            "stiffness": {"modified": False, "verified": False}, "panel": panel.readback()}


@tool("create_curve_deform", "Create one native curve-to-curve Deform in the active saved part/configuration from ordered single-segment sketches. Revision 34 only. Owns and closes the entire native command. No saving, rollback, or automatic retry. validate_only configures, reads back, and cancels.",
      {"expected_document_path": _PATH, "configuration": _NAME, "body_name": _NAME,
       "curve_pairs": {"type": "array", "minItems": 1, "items": {"type": "object",
                       "properties": {"source_sketch": _NAME, "target_sketch": _NAME},
                       "required": ["source_sketch", "target_sketch"], "additionalProperties": False}},
       "shape_accuracy": {"type": "number", "minimum": 0, "maximum": 1},
       "fixed_edges": {"type": "boolean"}, "feature_name": _NAME,
       "validate_only": {"type": "boolean", "default": False}},
      ["expected_document_path", "configuration", "body_name", "curve_pairs", "shape_accuracy", "fixed_edges", "feature_name"])
def create_curve_deform(args: dict[str, Any]) -> dict[str, Any]:
    stage, doc, panel, before = "validating", None, None, None
    attempted, accepting, command_open = False, False, False
    readback, artifacts, cleanup_errors = {}, [], []
    error = None
    try:
        request = DeformRequest.parse(args)
        if active_native_commands():
            raise RuntimeError("An existing Deform command must be closed before creation.")
        if visible_property_managers():
            raise RuntimeError("An existing PropertyManager must be closed before creation.")
        app, doc = require_document_path(request.expected_document_path, 1)
        body, segments = _prepare(doc, request)
        owner = Owner.capture(app, request.expected_document_path)
        panel = DeformPanel(owner)
        _OWNERS[(owner.pid, owner.frame)] = owner
        before = _model_readback(doc)
        if any(body["fault_count"] for body in before["bodies"]):
            raise ValueError("Input model has invalid solid bodies.")
        stage, attempted = "opening", True
        flag_methods(doc, "ClearSelection2").ClearSelection2(True)
        if not select_by_id(doc, request.body_name, "SOLIDBODY", append=False, mark=0):
            raise RuntimeError("Could not preselect the exact body before opening Deform.")
        if not flag_methods(app, "RunCommand").RunCommand(454, ""):
            raise RuntimeError("SOLIDWORKS did not open the Deform command.")
        stage = "configuring"
        readback = _configure(panel, app, doc, request, body, segments)
        _, current = require_document_path(request.expected_document_path, 1)
        if _configurations(current)[1] != request.configuration:
            raise RuntimeError("Active configuration changed before Deform acceptance.")
        stage = "cancelling" if request.validate_only else "accepting"
        accepting = not request.validate_only
        panel.close(accepting)
        stage = "verifying"
        after = _model_readback(doc)
        readback["model_before"], readback["model_after"] = before, after
        old_names = {f["name"] for f in before["features"]}
        artifacts = [f for f in after["features"] if f["name"] not in old_names]
        if request.validate_only:
            if not _same_model_state(before, after):
                raise RuntimeError("Cancel did not preserve the observed feature and body state.")
            readback["model_unchanged_verified"] = True
        else:
            new_deforms = [f for f in artifacts if f["type"] == "Deform"]
            before_count = sum(f["type"] == "Deform" for f in before["features"])
            after_count = sum(f["type"] == "Deform" for f in after["features"])
            if len(new_deforms) != 1 or after_count != before_count + 1:
                raise RuntimeError("Expected exactly one newly created native Deform feature.")
            feature = next(f for f in _features(doc) if str(value(f, "Name")) == new_deforms[0]["name"])
            warning = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)
            code = flag_methods(feature, "GetErrorCode2").GetErrorCode2(warning)
            if isinstance(code, tuple):
                code = code[0]
            readback["feature_error_code"] = int(code)
            if int(code) != 0:
                raise RuntimeError(f"Native Deform reports feature error/warning {code}.")
            if (len(before["bodies"]) != len(after["bodies"])
                    or any(body["fault_count"] for body in after["bodies"])):
                raise RuntimeError("Deform did not preserve solid-body count and validity.")
            _rename(feature, request.feature_name)
            artifacts[artifacts.index(new_deforms[0])] = {"name": str(value(feature, "Name")), "type": "Deform"}
            readback["feature"] = {"name": str(value(feature, "Name")), "type": str(value(feature, "GetTypeName2"))}
            readback["geometry_changed"] = not _same_bodies(_geometry(before), _geometry(after))
        stage = "validated" if request.validate_only else "succeeded"
    except Exception as exc:
        error = str(exc)
    finally:
        if attempted and panel is not None:
            try:
                command_open = panel.is_open()
                if command_open:
                    try:
                        readback["failure_panel"] = panel.readback()
                    except Exception as exc:
                        cleanup_errors.append(f"Panel readback: {exc}")
                    panel.close(False)
                command_open = panel.is_open()
            except Exception as exc:
                command_open = True
                cleanup_errors.append(f"Owned command cancellation: {exc}")
        if attempted and doc is not None and not command_open:
            try:
                flag_methods(doc, "ClearSelection2").ClearSelection2(True)
                if error and before is not None:
                    after = _model_readback(doc)
                    readback["model_after_failure"] = after
                    old_names = {f["name"] for f in before["features"]}
                    artifacts = [f for f in after["features"] if f["name"] not in old_names]
                    readback["model_unchanged_verified"] = _same_model_state(before, after)
            except Exception as exc:
                cleanup_errors.append(f"Model reconciliation: {exc}")
    ok = error is None and not cleanup_errors and not command_open
    readback["comparison_tolerances"] = {"volume_absolute_mm3": 1e-9, "volume_relative": 1e-12,
                                        "other_observed_fields": "exact"}
    changed = accepting or bool(artifacts) or command_open
    return result(ok, "Native Deform transaction completed without saving." if ok else (error or "Deform cleanup failed."),
                  stage=stage, may_have_changes=changed, partial_artifacts=artifacts,
                  command_still_open=command_open, cleanup_errors=cleanup_errors,
                  rollback_performed=False, readback=readback)
