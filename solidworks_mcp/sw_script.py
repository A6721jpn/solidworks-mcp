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

"""Batch execution: many operations in one round trip.

``run_script`` executes a Python snippet inside the server process with the
live COM objects and every registered tool bound as plain functions.  Three
feature operations that would cost three model turns become one.  Snippets
that work can be saved as recipes and re-run with parameters.

This is deliberately the one tool that runs caller-supplied code; the upstream
server does not.  It stays inside the SOLIDWORKS API surface by convention,
not by sandboxing -- the server is a local, single-user process.  Disable it
entirely with SW_MCP_ALLOW_SCRIPTS=0.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from .sw_core import (
    HANDLERS,
    OUTPUT_ROOT,
    active_document,
    apply_selection,
    byref_long,
    clear_selection,
    dimension_dialog_suppressed,
    extension,
    feature_manager,
    logger,
    rebuild,
    result,
    running_app,
    sketch_manager,
    to_deg,
    to_m,
    to_mm,
    to_rad,
    tool,
    value,
    whats_wrong,
)

ALLOW_SCRIPTS = os.environ.get("SW_MCP_ALLOW_SCRIPTS", "1") != "0"
RECIPE_DIR = OUTPUT_ROOT / "recipes"
_MAX_OUTPUT = int(os.environ.get("SW_MCP_SCRIPT_OUTPUT_CHARS", "6000"))
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,63}$")


def jsonable(obj: Any, depth: int = 0) -> Any:
    """Make a script's return value safe to serialise."""
    if depth > 6:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return f"<{len(obj)} bytes>"
    if isinstance(obj, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in list(obj.items())[:200]}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v, depth + 1) for v in list(obj)[:200]]
    if hasattr(obj, "_oleobj_"):
        name = None
        for member in ("Name", "GetTitle", "FullName"):
            try:
                name = value(obj, member)
                if name:
                    break
            except Exception:
                continue
        return f"<COM {name}>" if name else "<COM object>"
    return str(obj)


class _Timeout(Exception):
    pass


def _build_env(params: dict[str, Any] | None = None) -> dict[str, Any]:
    app = running_app()
    doc = None
    try:
        _, doc = active_document()
    except RuntimeError:
        pass
    from . import sw_core, sw_snapshot
    from .sw_watchdog import WATCHDOG, blocked_report

    def call(tool_name: str, /, **kwargs: Any) -> dict[str, Any]:
        handler = HANDLERS.get(tool_name)
        if handler is None:
            raise KeyError(f"unknown tool {tool_name}")
        blocked = blocked_report()
        if blocked is not None:
            return blocked
        cursor = WATCHDOG.event_cursor()
        payload = handler(kwargs)
        events = WATCHDOG.events_since(cursor)
        if events:
            payload = dict(payload)
            payload["dialog"] = events
        return payload

    def snapshot(**kwargs: Any) -> str:
        _, current = active_document()
        return sw_snapshot.render_snapshot(sw_snapshot.take_snapshot(current, **kwargs))

    def select(spec: dict[str, Any], mark: int = 0, append: bool = False) -> int:
        _, current = active_document()
        return apply_selection(current, spec, mark, append)

    env: dict[str, Any] = {
        "__name__": "__sw_script__",
        "sw": app,
        "doc": doc,
        "ext": extension(doc) if doc is not None else None,
        "fm": feature_manager(doc) if doc is not None else None,
        "skm": sketch_manager(doc) if doc is not None else None,
        "core": sw_core,
        "tools": HANDLERS,
        "call": call,
        "snapshot": snapshot,
        "select": select,
        "clear_selection": lambda: clear_selection(active_document()[1]),
        "rebuild": lambda: rebuild(active_document()[1]),
        "whats_wrong": lambda: whats_wrong(active_document()[1]),
        "byref_long": byref_long,
        "value": value,
        "mm": to_m, "to_mm": to_mm, "deg": to_rad, "to_deg": to_deg,
        "params": dict(params or {}),
        "result": None,
    }
    conflicts = set(params or {}).intersection(env)
    if conflicts:
        raise ValueError(f"Script parameters cannot shadow built-in globals: {sorted(conflicts)}")
    env.update(params or {})
    return env


def execute(code: str, timeout_s: float, params: dict[str, Any] | None = None, label: str = "<run_script>") -> dict[str, Any]:
    if not ALLOW_SCRIPTS:
        return result(False, "Script execution is disabled (SW_MCP_ALLOW_SCRIPTS=0).")
    try:
        compiled = compile(code, label, "exec")
    except SyntaxError as exc:
        return result(False, f"SyntaxError: {exc.msg} (line {exc.lineno})", line=exc.lineno)

    if params is not None and not isinstance(params, dict):
        return result(False, "params must be an object.")
    try:
        env = _build_env(params)
    except ValueError as exc:
        return result(False, str(exc))
    buffer = io.StringIO()
    deadline = time.monotonic() + max(1.0, float(timeout_s))

    def tracer(frame: Any, event: str, arg: Any) -> Any:
        # Only Python-level statements can be interrupted; a blocked COM call
        # cannot, which is what the dialog watchdog is for.
        if event == "line" and time.monotonic() > deadline:
            raise _Timeout()
        return tracer

    started = time.monotonic()
    ok, message, error = True, "Script finished.", None
    old_trace = sys.gettrace()
    try:
        sys.settrace(tracer)
        # Dimensions created from a script would otherwise pop the modal
        # "Modify" box and wedge the call, exactly as the dimension tools guard.
        with dimension_dialog_suppressed(env["sw"]), contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            exec(compiled, env)
    except _Timeout:
        ok, message = False, f"Script exceeded {timeout_s}s and was stopped between statements."
    except Exception as exc:
        ok, message = False, f"{type(exc).__name__}: {exc}"
        frames = traceback.extract_tb(exc.__traceback__)
        mine = [f for f in frames if f.filename == label]
        error = "\n".join(f"line {f.lineno}: {f.line}" for f in mine[-4:]) or traceback.format_exc(limit=3)
        logger.info("run_script failed: %s", message)
    finally:
        sys.settrace(old_trace)

    output = buffer.getvalue()
    if len(output) > _MAX_OUTPUT:
        output = "...[truncated]...\n" + output[-_MAX_OUTPUT:]
    payload = result(ok, message, seconds=round(time.monotonic() - started, 2))
    data = payload.setdefault("data", {})
    if output.strip():
        data["stdout"] = output
    if env.get("result") is not None:
        data["result"] = jsonable(env["result"])
    if error:
        data["error"] = error
    return payload


_SCRIPT_DESCRIPTION = (
    "Run a Python snippet inside the server against the live SOLIDWORKS session: one round trip "
    "for many operations. Use it whenever a task needs 3+ tool calls, loops, or arithmetic. "
    "Globals: sw (ISldWorks), doc (active IModelDoc2), ext, fm (FeatureManager), skm (SketchManager), "
    "call(tool_name, **args) to run any tool of this server (e.g. call('boss_extrude', depth_mm=20)), "
    "select(selection_spec), snapshot(), rebuild(), whats_wrong(), mm(x)->metres, to_mm, deg(x)->rad, "
    "to_deg, core (helper module), print() is captured. Assign to `result` to return a value. "
    "params is an optional object, also exposed as non-reserved globals. "
    "COM units are metres/radians; tools take mm/deg. The result carries a diff of what changed."
)


@tool(
    "run_script",
    _SCRIPT_DESCRIPTION,
    {
        "code": {"type": "string", "description": "Python source. Keep it short; use call() for tools."},
        "timeout_s": {"type": "number", "default": 120},
        "params": {"type": "object", "additionalProperties": True, "description": "Data for the snippet; may not shadow built-in script globals."},
    },
    ["code"],
)
def run_script(args: dict[str, Any]) -> dict[str, Any]:
    return execute(str(args["code"]), float(args.get("timeout_s", 120)), args.get("params"))


# --------------------------------------------------------------------------
# Recipes: snippets that worked, kept for reuse
# --------------------------------------------------------------------------


def _recipe_path(name: str) -> Path:
    if not _NAME_RE.match(name):
        raise RuntimeError("Recipe names are letters, digits, '_' or '-', up to 64 characters.")
    return RECIPE_DIR / f"{name}.py"


def _recipe_meta(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    description, params = "", ""
    match = re.match(r'^\s*"""(.*?)"""', text, re.S)
    if match:
        block = match.group(1).strip().splitlines()
        description = block[0].strip() if block else ""
        for line in block[1:]:
            if line.strip().lower().startswith("params:"):
                params = line.split(":", 1)[1].strip()
    return {"name": path.stem, "description": description, "params": params}


@tool(
    "save_recipe",
    "Store a run_script snippet that worked as a named recipe for reuse with run_recipe. "
    "Reference parameters in the code as plain names (they arrive as globals) and list them.",
    {
        "name": {"type": "string"},
        "code": {"type": "string"},
        "description": {"type": "string", "description": "One line: what it does and what it needs selected/open."},
        "params": {"type": "string", "description": "Comma-separated parameter names with defaults, e.g. 'depth_mm=10, radius_mm=2'."},
        "overwrite": {"type": "boolean", "default": False},
    },
    ["name", "code", "description"],
)
def save_recipe(args: dict[str, Any]) -> dict[str, Any]:
    path = _recipe_path(str(args["name"]))
    if path.exists() and not bool(args.get("overwrite", False)):
        return result(False, f"Recipe '{path.stem}' exists; pass overwrite=true to replace it.")
    RECIPE_DIR.mkdir(parents=True, exist_ok=True)
    header = f'"""{args["description"].strip()}\nparams: {str(args.get("params") or "").strip()}\n"""\n'
    path.write_text(header + str(args["code"]).rstrip() + "\n", encoding="utf-8")
    return result(True, f"Saved recipe '{path.stem}'.", path=str(path))


@tool("list_recipes", "Read-only: list saved run_script recipes with their descriptions and parameters.", {})
def list_recipes(args: dict[str, Any]) -> dict[str, Any]:
    if not RECIPE_DIR.is_dir():
        return result(True, "No recipes saved yet.", recipes=[])
    recipes = [_recipe_meta(p) for p in sorted(RECIPE_DIR.glob("*.py"))]
    return result(True, f"{len(recipes)} recipes.", recipes=recipes)


@tool("get_recipe", "Read-only: return the source of a saved recipe.", {"name": {"type": "string"}}, ["name"])
def get_recipe(args: dict[str, Any]) -> dict[str, Any]:
    path = _recipe_path(str(args["name"]))
    if not path.is_file():
        return result(False, f"No recipe named '{path.stem}'.")
    return result(True, f"Recipe '{path.stem}'.", code=path.read_text(encoding="utf-8"))


@tool(
    "run_recipe",
    "Run a saved recipe with parameters (they become globals in the snippet, also available as `params`).",
    {
        "name": {"type": "string"},
        "params": {"type": "object", "additionalProperties": True},
        "timeout_s": {"type": "number", "default": 120},
    },
    ["name"],
)
def run_recipe(args: dict[str, Any]) -> dict[str, Any]:
    path = _recipe_path(str(args["name"]))
    if not path.is_file():
        return result(False, f"No recipe named '{path.stem}'.")
    params = args.get("params") or {}
    if not isinstance(params, dict):
        return result(False, "params must be an object.")
    payload = execute(path.read_text(encoding="utf-8"), float(args.get("timeout_s", 120)), params, label=f"<recipe {path.stem}>")
    payload.setdefault("data", {})["recipe"] = path.stem
    return payload


@tool(
    "run_macro",
    "Run an existing SOLIDWORKS macro file (.swp VBA or .dll) by module and procedure name.",
    {
        "path": {"type": "string"},
        "module": {"type": "string", "description": "VBA module name, e.g. 'Module1' or 'Macro1'."},
        "procedure": {"type": "string", "default": "main"},
        "unload_after": {"type": "boolean", "default": True},
    },
    ["path", "module"],
)
def run_macro(args: dict[str, Any]) -> dict[str, Any]:
    app = running_app()
    path = Path(str(args["path"])).expanduser().resolve()
    if not path.is_file():
        return result(False, f"No such macro file: {path}")
    error = byref_long(0)
    # swRunMacroOption_e: 1 = unload after run, 0 = default.
    ok = bool(app.RunMacro2(str(path), str(args["module"]), str(args.get("procedure", "main")),
                            1 if bool(args.get("unload_after", True)) else 0, error))
    code = int(error.value) if error.value is not None else 0
    return result(ok and code == 0, "Macro ran." if ok and code == 0 else f"Macro failed (error code {code}).", error_code=code)


def _preload_recipe_index() -> str:
    """Short recipe index for the server instructions, so the model knows they exist."""
    try:
        if RECIPE_DIR.is_dir():
            names = [p.stem for p in sorted(RECIPE_DIR.glob("*.py"))][:20]
            if names:
                return "Saved recipes: " + ", ".join(names) + "."
    except Exception:
        pass
    return ""
