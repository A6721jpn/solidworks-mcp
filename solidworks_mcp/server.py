# Modified in the A6721jpn fork. See FORK.md for the changes.
# Copyright 2026 JIALE LIU (upstream solidworks-mcp)
# Fork additions copyright 2026 the solidworks_operation project.
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

"""A local, stdio-only MCP bridge for a running SOLIDWORKS instance.

The bridge attaches to a SOLIDWORKS session the user already has open and
drives it through the documented COM API. Unlike upstream, this fork also
offers ``run_script`` for batching operations. Caller-supplied scripts have
the server process permissions, including file and network access.
See sw_script.py.

Unit contract: every length is millimetres and every angle is degrees at the
MCP boundary, which is why the parameters are named ``*_mm`` and ``*_deg``.
The COM API underneath is metres and radians; conversion happens once, in
sw_core.

Fork additions (token efficiency and robustness)
------------------------------------------------
* Results are compact JSON (no indentation, numbers trimmed) or, for the
  snapshot tools, plain text.  SW_MCP_COMPACT=0 restores upstream output.
* Every mutating tool result carries a ``diff`` of what changed in the model
  (features, dimensions, size, volume), so the agent rarely has to re-list.
  SW_MCP_AUTO_DIFF=0 disables it.
* A modal-dialog watchdog refuses to start a call while SOLIDWORKS is stuck
  behind a dialog, and closes a dialog that appears mid-call after a grace
  period, reporting what it did.
* The application object is always late-bound (dynamic dispatch), which the
  upstream code assumes; a stale pywin32 gen_py cache otherwise breaks
  OpenDoc6 and the method flagging.

Layout
------
sw_core      COM attachment, units, feature tree, topology, selection engine, persistent refs
sw_file      session status, documents, saving, exporting, appearance, material
sw_refgeom   reference planes and axes
sw_sketch    sketches, sketch geometry, relations, dimensions
sw_feature   solid features
sw_inspect   topology listings, measurement, mass properties, screenshots
sw_assembly  components and mates
sw_drawing   drawing sheets, views, and annotation
sw_snapshot  compact model state and diffs            (fork)
sw_script    run_script, recipes, run_macro           (fork)
sw_watchdog  modal-dialog detection and dismissal     (fork)
sw_demo      the basketball demo, registered only when SW_MCP_DEMO_TOOLS is set
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from time import perf_counter
from contextlib import contextmanager
from typing import Any

import win32event
import win32api

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool

from .sw_core import HANDLERS, TOOLS, active_document, com_error, document_type, logger, result, running_app

# Importing these modules is what registers their tools into sw_core.TOOLS.
from . import sw_file  # noqa: F401
from . import sw_refgeom  # noqa: F401
from . import sw_sketch  # noqa: F401
from . import sw_sketch_edit  # noqa: F401
from . import sw_feature  # noqa: F401
from . import sw_inspect  # noqa: F401
from . import sw_assembly  # noqa: F401
from . import sw_drawing  # noqa: F401
from . import sw_drawing_geometry  # noqa: F401
from . import sw_drawing_assets  # noqa: F401
from . import sw_sheet  # noqa: F401
from . import sw_surface_ops  # noqa: F401
from . import sw_annotation_ops  # noqa: F401
from . import sw_deform
from . import sw_snapshot  # noqa: F401
from . import sw_script
from . import sw_watchdog  # noqa: F401
from . import sw_demo  # noqa: F401

from .sw_snapshot import diff_snapshots, light_snapshot
from .sw_watchdog import WATCHDOG, blocked_report

COMPACT = os.environ.get("SW_MCP_COMPACT", "1") != "0"
AUTO_DIFF = os.environ.get("SW_MCP_AUTO_DIFF", "1") != "0"

_RECIPES = sw_script._preload_recipe_index()
INSTRUCTIONS = (
    "SOLIDWORKS COM bridge; lengths in mm, angles in deg. Workflow: model_snapshot to read state; "
    "for a known sketch prefer inspect_sketch and set_dimensions for scoped reads and one-rebuild updates; "
    "one tool per change, or run_script for 3+ steps/loops; read the 'diff' each mutating result "
    "carries instead of re-listing; verify by numbers (check_errors, measure, model_diff) and use "
    "capture_screenshot only when the shape itself must be seen. selection.refs (from list_faces / "
    "list_edges) survive geometry changes; indices do not. A result with a 'dialog' entry means a "
    "modal dialog was closed mid-call: verify before continuing."
    + (" " + _RECIPES if _RECIPES else "")
)

# Tools whose result gets no auto-diff: they do not change geometry, or they
# already report the change themselves.
READ_ONLY_TOOLS = {
    "solidworks_status", "get_active_document_info", "create_new_document", "open_document", "copy_part_document",
    "save_document", "save_active_document", "export_document", "rebuild_document",
    "list_components", "list_mates", "list_sheets", "list_drawing_views", "activate_sheet",
    "activate_drawing_view", "list_faces", "list_edges", "list_vertices", "list_bodies",
    "list_features", "get_bounding_box", "get_mass_properties", "measure", "check_errors",
    "set_view", "capture_screenshot", "list_reference_planes", "list_sketches",
    "list_sketch_segments", "get_sketch_status", "list_dimensions", "set_appearance",
    "model_snapshot", "model_diff", "dismiss_dialog", "list_recipes", "get_recipe", "save_recipe",
    "list_configurations", "get_sketched_bend", "list_drawing_entities", "inspect_drawing_assets",
    "inspect_solidworks_ui", "activate_solidworks_window", "cancel_solidworks_command",
}

SCOPED_SKETCH_TOOLS = {
    "inspect_sketch", "delete_sketch_relations", "rename_dimension", "set_equation", "set_dimensions",
}

NATIVE_UI_TOOLS = {"inspect_solidworks_ui", "activate_solidworks_window", "cancel_solidworks_command"}

server = Server("solidworks-mcp", instructions=INSTRUCTIONS)

# One SOLIDWORKS session, one COM apartment: serialise tool calls so a slow
# rebuild cannot interleave with the next call's selection.
COM_LOCK = asyncio.Lock()


def compact_numbers(obj: Any) -> Any:
    """Trim floats for the wire: 3 decimals at mm scale, 6 significant digits below 1."""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return obj
        if abs(obj) >= 1.0:
            return round(obj, 3)
        if abs(obj) < 1e-12:
            return 0.0
        return float(f"{obj:.6g}")
    if isinstance(obj, dict):
        return {k: compact_numbers(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [compact_numbers(v) for v in obj]
    return obj


def _safe_light_snapshot() -> dict[str, Any] | None:
    try:
        _, doc = active_document()
        if document_type(doc) == 3:
            return None
        return light_snapshot(doc)
    except Exception:
        return None


@contextmanager
def _dispatch_mutex():
    """Serialize this bridge's MCP and CLI processes before any COM call.

    Windows mutex ownership is recursive on the same OS thread, including
    run_script calls back into dispatch. Manual CAD input is not excluded.
    """
    handle = win32event.CreateMutex(None, False, r"Local\SolidWorksMcpDispatcher")
    acquired = False
    try:
        status = win32event.WaitForSingleObject(handle, 1000)
        if status not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
            raise RuntimeError("Another Harness process is operating SOLIDWORKS; retry after it finishes.")
        acquired = True
        yield
    finally:
        if acquired:
            win32event.ReleaseMutex(handle)
        win32api.CloseHandle(handle)


def dispatch(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    started = perf_counter()
    try:
        with _dispatch_mutex():
            mutex_ms = (perf_counter() - started) * 1000
            payload = _dispatch_locked(name, arguments)
    except RuntimeError as exc:
        mutex_ms = (perf_counter() - started) * 1000
        payload = result(False, str(exc))
        payload["timing_ms"] = {"guards": 0.0, "before_snapshot": 0.0, "handler": 0.0, "after_snapshot": 0.0}
    payload["timing_ms"]["mutex"] = round(mutex_ms, 3)
    payload["timing_ms"]["total"] = round((perf_counter() - started) * 1000, 3)
    return payload


def _dispatch_locked(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Run one tool synchronously with the watchdog and auto-diff around it.

    Shared with the bench harness so measurements reflect the real server path.
    """
    started = perf_counter()
    timing = {"guards": 0.0, "before_snapshot": 0.0, "handler": 0.0, "after_snapshot": 0.0}
    guard_started = started

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        timing["total"] = (perf_counter() - started) * 1000
        payload["timing_ms"] = {key: round(value, 3) for key, value in timing.items()}
        return payload

    handler = HANDLERS.get(name)
    if handler is None:
        timing["guards"] += (perf_counter() - guard_started) * 1000
        return finish(result(False, f"Unknown tool: {name}"))

    try:
        commands = sw_deform.active_native_commands()
    except Exception as exc:
        timing["guards"] += (perf_counter() - guard_started) * 1000
        return finish(result(False, f"Could not inspect native command state safely: {exc}"))
    if commands and name not in NATIVE_UI_TOOLS:
        timing["guards"] += (perf_counter() - guard_started) * 1000
        return finish(result(False, "Finish the native Deform command before calling a model tool.",
                      active_commands=commands, command_still_open=True))
    if name == "create_curve_deform":
        try:
            pages = sw_deform.visible_property_managers()
        except Exception as exc:
            timing["guards"] += (perf_counter() - guard_started) * 1000
            return finish(result(False, f"Could not inspect native PropertyManager state safely: {exc}"))
        if pages:
            timing["guards"] += (perf_counter() - guard_started) * 1000
            return finish(result(False, "Close the existing PropertyManager before creating Deform.",
                          active_commands=pages, command_still_open=True))
    if name in NATIVE_UI_TOOLS:
        timing["guards"] += (perf_counter() - guard_started) * 1000
        handler_started = perf_counter()
        try:
            payload = handler(arguments or {})
        except Exception as exc:
            payload = result(False, str(exc))
        timing["handler"] = (perf_counter() - handler_started) * 1000
        return finish(payload if isinstance(payload, dict) else result(True, str(payload)))

    try:
        WATCHDOG.configure(running_app())
    except Exception:
        pass
    if name != "dismiss_dialog":
        blocked = blocked_report()
        if blocked is not None:
            timing["guards"] += (perf_counter() - guard_started) * 1000
            return finish(blocked)
    timing["guards"] += (perf_counter() - guard_started) * 1000

    snapshot_started = perf_counter()
    before = _safe_light_snapshot() if AUTO_DIFF and name not in READ_ONLY_TOOLS and name not in SCOPED_SKETCH_TOOLS else None
    timing["before_snapshot"] = (perf_counter() - snapshot_started) * 1000

    WATCHDOG.begin_call()
    handler_started = perf_counter()
    try:
        payload = handler(arguments or {})
    except RuntimeError as exc:
        payload = result(False, str(exc))
    except KeyError as exc:
        payload = result(False, f"Missing required argument: {exc}")
    except Exception as exc:
        payload = com_error(exc)
    finally:
        timing["handler"] = (perf_counter() - handler_started) * 1000
        events = WATCHDOG.end_call()

    if not isinstance(payload, dict):
        payload = result(True, str(payload))
    if events:
        payload["dialog"] = events
        if payload.get("ok"):
            payload["message"] = f"{payload.get('message', '')} (a modal dialog appeared and was closed; verify the result)"
    guard_started = perf_counter()
    try:
        remaining_commands = sw_deform.active_native_commands()
    except Exception as exc:
        remaining_commands = [{"inspection_error": str(exc)}]
    timing["guards"] += (perf_counter() - guard_started) * 1000
    if remaining_commands:
        payload["ok"] = False
        payload["command_still_open"] = True
        payload["active_commands"] = remaining_commands
        payload["message"] = f"{payload.get('message', '')} Native command remains open; automatic model snapshot skipped."
    if before is not None and not remaining_commands:
        snapshot_started = perf_counter()
        after = _safe_light_snapshot()
        timing["after_snapshot"] = (perf_counter() - snapshot_started) * 1000
        if after is not None and after.get("doc", {}).get("title") == before.get("doc", {}).get("title"):
            text = diff_snapshots(before, after)
            if text:
                payload["diff"] = text
            if payload.get("ok") and name in GEOMETRY_TOOLS and _volume_unchanged(before, after):
                # SOLIDWORKS reports success for a cut that never meets the
                # material or a boss that adds nothing; the numbers do not.
                payload["warning"] = (
                    "volume unchanged after this feature: it may not intersect the model "
                    "(check the sketch plane, direction, or end condition)"
                )
    return finish(payload)


# Feature tools whose success should move the volume or body count.
GEOMETRY_TOOLS = {
    "boss_extrude", "cut_extrude", "revolve", "fillet", "chamfer", "shell", "draft", "rib",
    "simple_hole", "sweep", "loft", "linear_pattern", "circular_pattern", "mirror_feature",
}


def _volume_unchanged(before: dict[str, Any], after: dict[str, Any]) -> bool:
    old, new = before.get("volume_mm3"), after.get("volume_mm3")
    if old is None or new is None:
        return False
    return abs(float(new) - float(old)) < 1e-6 and before.get("bodies") == after.get("bodies")


def render(payload: dict[str, Any]) -> list[TextContent | ImageContent]:
    image_base64 = payload.pop("_image_png_base64", None)
    text = payload.pop("_text", None)
    if text is not None:
        extras: list[str] = []
        if payload.get("diff"):
            extras.append("diff:\n" + str(payload["diff"]))
        if payload.get("dialog"):
            extras.append("dialog: " + json.dumps(payload["dialog"], ensure_ascii=False))
        body = text if not extras else text + "\n" + "\n".join(extras)
        if not payload.get("ok", True):
            body = f"ERROR: {payload.get('message', '')}\n{body}"
    elif COMPACT:
        body = json.dumps(compact_numbers(payload), ensure_ascii=False, separators=(",", ":"), default=str)
    else:
        body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    content: list[TextContent | ImageContent] = [TextContent(type="text", text=body)]
    if image_base64:
        content.append(ImageContent(type="image", data=image_base64, mimeType="image/png"))
    return content


@server.list_tools()
async def list_tools() -> list[Tool]:
    return list(TOOLS)


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent | ImageContent]:
    async with COM_LOCK:
        payload = dispatch(name, arguments)
    return render(payload)


async def main() -> None:
    logger.info("solidworks-mcp (fork) starting with %d tools; compact=%s auto_diff=%s", len(TOOLS), COMPACT, AUTO_DIFF)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run() -> None:
    """Console-script entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
