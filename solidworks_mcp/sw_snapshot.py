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

"""Compact model state for the agent.

``model_snapshot`` replaces a round of list_features / list_dimensions /
get_bounding_box / get_mass_properties / check_errors with one short text
block, and ``model_diff`` reports only what changed since the last snapshot
(or a named checkpoint).  The server also attaches a diff to every mutating
tool result, so the agent rarely has to re-list anything to know what it did.

The text form is deliberately terse: one line per feature, dimensions inline
as ``name=value`` (millimetres, or ``deg`` suffixed), two decimals, and
folders, lights, and default reference geometry hidden unless asked for.
"""

from __future__ import annotations

import os
import time
from typing import Any

from .sw_core import (
    active_document,
    byref_long,
    document_info,
    document_type,
    extension,
    feature_property,
    flag_methods,
    iter_feature_objects,
    logger,
    mm_point,
    result,
    safe,
    to_deg,
    to_mm,
    tool,
    value,
    whats_wrong,
)

# Feature types that carry no design intent for the agent.  Hidden by default.
_HIDDEN_TYPES = {
    "CommentsFolder", "FavoriteFolder", "HistoryFolder", "SelectionSetFolder", "SensorFolder",
    "DocsFolder", "DetailCabinet", "MaterialFolder", "SolidBodyFolder", "SurfaceBodyFolder",
    "EnvFolder", "AmbientLight", "DirectionLight", "PointLight", "SpotLight", "LiveSectionFolder",
    "InkMarkupFolder", "BlockFolder", "EqnFolder", "CutListFolder", "CosmosFolder",
    "ImportedDefeatureFolder", "SWMarkupFolder", "MarkupFolder", "OriginProfileFeature",
    "SketchBlockDef", "SketchBlockInst", "FtrFolder", "ViewBodyFolder", "TableFolder",
}
_REFERENCE_TYPES = {"RefPlane", "RefAxis", "CoordSys", "RefPoint"}

# Friendlier labels for the type names SOLIDWORKS reports.
_TYPE_LABELS = {
    "ProfileFeature": "Sketch", "3DProfileFeature": "3DSketch", "Extrusion": "Boss-Extrude",
    "Boss": "Boss-Extrude", "ICE": "Cut-Extrude", "Cut": "Cut-Extrude", "RevCut": "Cut-Revolve",
    "Revolution": "Revolve", "BaseBody": "ImportedBody", "RefPlane": "Plane", "RefAxis": "Axis",
    "MirrorPattern": "Mirror", "LPattern": "LinearPattern", "CirPattern": "CircularPattern",
    "Sweep": "Sweep", "SweepCut": "Cut-Sweep", "Blend": "Loft", "BlendCut": "Cut-Loft",
    "Rib": "Rib", "Draft": "Draft", "Shell": "Shell", "Fillet": "Fillet", "Chamfer": "Chamfer",
    "HoleWzd": "HoleWizard", "Hole": "SimpleHole", "MateGroup": "Mates", "Reference": "Component",
    "Combine": "Combine", "Split": "Split", "MoveCopyBody": "MoveBody", "Thicken": "Thicken",
    "Chamfer2": "Chamfer", "VarFillet": "VariableFillet", "FlatPattern": "FlatPattern",
    "SheetMetal": "SheetMetal", "SMBaseFlange": "BaseFlange", "EdgeFlange": "EdgeFlange",
}

_MAX_DIM_FEATURES = int(os.environ.get("SW_MCP_SNAPSHOT_MAX_FEATURES", "150"))

# Checkpoints and the last snapshot, keyed by document.
_CHECKPOINTS: dict[str, dict[str, dict[str, Any]]] = {}
_LAST: dict[str, dict[str, Any]] = {}


def doc_key(doc: Any) -> str:
    return str(safe(doc, "GetPathName", "") or safe(doc, "GetTitle", "") or "?")


def _label(type_name: str) -> str:
    return _TYPE_LABELS.get(type_name, type_name)


def _fmt(number: float) -> str:
    text = f"{number:.2f}"
    return text[:-3] if text.endswith(".00") else text.rstrip("0")


def feature_dimensions(feature: Any) -> dict[str, float | str]:
    """``full_name -> value`` for one feature; angles carry a ``deg`` suffix as strings."""
    dims: dict[str, float | str] = {}
    try:
        display = value(feature, "GetFirstDisplayDimension")
    except Exception:
        return dims
    guard = 0
    while display is not None and guard < 200:
        guard += 1
        try:
            dimension = flag_methods(display, "GetDimension2").GetDimension2(0)
            # FullName is "D1@BasePad@Part2.Part"; set_dimension and
            # IModelDoc2.Parameter take the first two components.
            name = "@".join(str(safe(dimension, "FullName", "") or "").split("@")[:2])
            raw = float(safe(dimension, "SystemValue", 0.0) or 0.0)
            # IDimension.GetType is swDimensionParamType_e: 0 linear, 1 angular, 2 integer.
            kind = int(safe(dimension, "GetType", 0) or 0)
            if name:
                if kind == 1:
                    dims[name] = f"{_fmt(to_deg(raw))}deg"
                elif kind == 2:
                    dims[name] = int(round(raw))
                else:
                    dims[name] = round(to_mm(raw), 3)
        except Exception:
            pass
        try:
            display = flag_methods(feature, "GetNextDisplayDimension").GetNextDisplayDimension(display)
        except Exception:
            display = None
    return dims


_SKETCH_STATUS = {
    1: "unknown", 2: "under_defined", 3: "fully_defined", 4: "over_defined",
    5: "no_solution", 6: "invalid_solution", 7: "autosolve_off",
}


def _sketch_state(feature: Any) -> str | None:
    try:
        sketch = value(feature, "GetSpecificFeature2")
        if sketch is None:
            return None
        return _SKETCH_STATUS.get(int(value(sketch, "GetConstrainedStatus")), None)
    except Exception:
        return None


def _problem_map(doc: Any) -> dict[str, str]:
    problems: dict[str, str] = {}
    try:
        for entry in whats_wrong(doc) or []:
            if isinstance(entry, dict):
                name = str(entry.get("feature") or entry.get("name") or "")
                text = str(entry.get("message") or entry.get("error") or entry.get("description") or entry)
            else:
                name, text = str(entry), ""
            if name:
                problems[name] = text[:160]
    except Exception:
        logger.info("Could not read rebuild problems for the snapshot.")
    return problems


def take_snapshot(
    doc: Any,
    *,
    include_reference: bool = False,
    include_dimensions: bool = True,
    include_problems: bool = True,
    include_mass: bool = True,
) -> dict[str, Any]:
    """Read the document into a plain dict.  Cheap enough to run around every mutating call."""
    snapshot: dict[str, Any] = {"doc": document_info(doc), "taken_at": time.time()}
    features_out: list[dict[str, Any]] = []
    hidden = 0
    objects = iter_feature_objects(doc)
    with_dims = include_dimensions and len(objects) <= _MAX_DIM_FEATURES
    for index, feature in enumerate(objects):
        type_name = str(feature_property(feature, "GetTypeName2", ""))
        if (
            type_name in _HIDDEN_TYPES
            or type_name.endswith("Folder")
            or (type_name in _REFERENCE_TYPES and not include_reference)
        ):
            hidden += 1
            continue
        entry: dict[str, Any] = {
            "i": index,
            "name": str(feature_property(feature, "Name", "")),
            "type": _label(type_name),
        }
        try:
            if bool(value(feature, "IsSuppressed")):
                entry["suppressed"] = True
        except Exception:
            pass
        if type_name == "ProfileFeature":
            state = _sketch_state(feature)
            if state:
                entry["sketch"] = state
        if with_dims:
            dims = feature_dimensions(feature)
            if dims:
                entry["dims"] = dims
        features_out.append(entry)
    snapshot["features"] = features_out
    snapshot["hidden"] = hidden
    snapshot["dims_included"] = with_dims

    kind = document_type(doc)
    box = None
    try:
        box = doc.GetPartBox(True) if kind == 1 else (doc.GetBox(0) if kind == 2 else None)
    except Exception:
        box = None
    if box is not None and len(box) >= 6:
        low, high = mm_point(box[0:3]), mm_point(box[3:6])
        snapshot["min_mm"] = [round(v, 3) for v in low]
        snapshot["max_mm"] = [round(v, 3) for v in high]
        snapshot["size_mm"] = [round(high[i] - low[i], 3) for i in range(3)]
    try:
        bodies = doc.GetBodies2(0, True) if kind == 1 else None
        snapshot["bodies"] = len(bodies) if bodies else 0
    except Exception:
        pass
    if include_mass and kind in (1, 2):
        try:
            values = extension(doc).GetMassProperties2(2, byref_long(0), False)
            if values is not None and len(values) >= 6:
                snapshot["volume_mm3"] = round(float(values[3]) * 1e9, 2)
                snapshot["area_mm2"] = round(float(values[4]) * 1e6, 2)
                snapshot["mass_g"] = round(float(values[5]) * 1000.0, 3)
        except Exception:
            pass
    if include_problems:
        snapshot["problems"] = _problem_map(doc)
    return snapshot


def render_snapshot(snapshot: dict[str, Any]) -> str:
    """The terse text form the agent reads."""
    doc = snapshot.get("doc", {})
    lines = [
        f"doc: {doc.get('title', '?')} ({doc.get('document_type', '?')}{', unsaved' if doc.get('dirty') else ''})",
    ]
    parts: list[str] = []
    if snapshot.get("size_mm"):
        parts.append("size " + " x ".join(_fmt(v) for v in snapshot["size_mm"]) + " mm")
    if snapshot.get("volume_mm3") is not None:
        parts.append(f"vol {_fmt(snapshot['volume_mm3'])} mm3")
    if snapshot.get("mass_g") is not None:
        parts.append(f"mass {_fmt(snapshot['mass_g'])} g")
    if snapshot.get("bodies") is not None:
        parts.append(f"bodies {snapshot['bodies']}")
    if parts:
        lines.append(" | ".join(parts))
    problems = snapshot.get("problems") or {}
    features = snapshot.get("features") or []
    note = "" if snapshot.get("dims_included", True) else ", dims omitted (large tree)"
    lines.append(f"features: {len(features)} shown, {snapshot.get('hidden', 0)} hidden | errors: {len(problems)}{note}")
    for entry in features:
        tags = [entry["type"]]
        if entry.get("sketch"):
            tags.append(entry["sketch"])
        if entry.get("suppressed"):
            tags.append("suppressed")
        line = f" F{entry['i']} {entry['name']} [{', '.join(tags)}]"
        dims = entry.get("dims") or {}
        if dims:
            line += " " + " ".join(f"{k}={_fmt(v) if isinstance(v, float) else v}" for k, v in dims.items())
        problem = problems.get(entry["name"])
        if problem is not None:
            line += f"  !ERR {problem}" if problem else "  !ERR"
        lines.append(line)
    for name, text in problems.items():
        if not any(f["name"] == name for f in features):
            lines.append(f" !ERR {name}: {text}")
    return "\n".join(lines)


def _dims_of(snapshot: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for entry in snapshot.get("features") or []:
        for k, v in (entry.get("dims") or {}).items():
            out[k] = v
    return out


def diff_snapshots(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Human-readable delta; empty string when nothing observable changed."""
    lines: list[str] = []
    before_features = {f["name"]: f for f in before.get("features") or []}
    after_features = {f["name"]: f for f in after.get("features") or []}
    for name, entry in after_features.items():
        if name not in before_features:
            dims = entry.get("dims") or {}
            extra = (" " + " ".join(f"{k}={_fmt(v) if isinstance(v, float) else v}" for k, v in dims.items())) if dims else ""
            lines.append(f" + F{entry['i']} {name} [{entry['type']}]{extra}")
    for name, entry in before_features.items():
        if name not in after_features:
            lines.append(f" - {name} [{entry['type']}]")
    for name, entry in after_features.items():
        old = before_features.get(name)
        if old is None:
            continue
        if old.get("sketch") != entry.get("sketch") and entry.get("sketch"):
            lines.append(f" ~ {name}: {old.get('sketch', '?')} -> {entry['sketch']}")
        if bool(old.get("suppressed")) != bool(entry.get("suppressed")):
            lines.append(f" ~ {name}: {'suppressed' if entry.get('suppressed') else 'unsuppressed'}")
    old_dims, new_dims = _dims_of(before), _dims_of(after)
    for name, new_value in new_dims.items():
        old_value = old_dims.get(name)
        if old_value is not None and old_value != new_value:
            show = lambda v: _fmt(v) if isinstance(v, float) else str(v)  # noqa: E731
            lines.append(f" ~ {name} {show(old_value)} -> {show(new_value)}")
    if before.get("size_mm") != after.get("size_mm") and after.get("size_mm"):
        old = " x ".join(_fmt(v) for v in before.get("size_mm") or []) or "none"
        new = " x ".join(_fmt(v) for v in after["size_mm"])
        lines.append(f" size {old} -> {new} mm")
    if before.get("volume_mm3") is not None and after.get("volume_mm3") is not None:
        delta = after["volume_mm3"] - before["volume_mm3"]
        if abs(delta) > 1e-6:
            lines.append(f" vol {_fmt(before['volume_mm3'])} -> {_fmt(after['volume_mm3'])} mm3 ({'+' if delta > 0 else ''}{_fmt(delta)})")
    elif after.get("volume_mm3") is not None and before.get("volume_mm3") is None:
        lines.append(f" vol {_fmt(after['volume_mm3'])} mm3")
    if before.get("bodies") != after.get("bodies") and after.get("bodies") is not None:
        lines.append(f" bodies {before.get('bodies', 0)} -> {after['bodies']}")
    old_problems, new_problems = before.get("problems") or {}, after.get("problems") or {}
    for name, text in new_problems.items():
        if name not in old_problems:
            lines.append(f" !ERR +{name}: {text}" if text else f" !ERR +{name}")
    for name in old_problems:
        if name not in new_problems:
            lines.append(f" fixed {name}")
    return "\n".join(lines)


def remember(doc: Any, snapshot: dict[str, Any], label: str | None = None) -> None:
    key = doc_key(doc)
    _LAST[key] = snapshot
    if label:
        _CHECKPOINTS.setdefault(key, {})[label] = snapshot


def last_snapshot(doc: Any) -> dict[str, Any] | None:
    return _LAST.get(doc_key(doc))


def light_snapshot(doc: Any) -> dict[str, Any]:
    """What the auto-diff compares: tree, dims, box, volume.  No rebuild, no problem scan."""
    return take_snapshot(doc, include_problems=False, include_mass=True)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@tool(
    "model_snapshot",
    "Read-only: one compact text summary of the active document -- feature tree with dimensions "
    "(mm, or 'deg'), sketch status, size, volume, mass, rebuild errors. Call this instead of "
    "list_features + list_dimensions + get_bounding_box + get_mass_properties + check_errors. "
    "Pass label to store a checkpoint that model_diff can compare against.",
    {
        "label": {"type": "string", "description": "Optional checkpoint name for model_diff(since=...)."},
        "include_reference": {"type": "boolean", "default": False, "description": "Also list planes/axes."},
        "include_dimensions": {"type": "boolean", "default": True},
        "format": {"type": "string", "enum": ["text", "json"], "default": "text"},
    },
)
def model_snapshot(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = active_document()
    snapshot = take_snapshot(
        doc,
        include_reference=bool(args.get("include_reference", False)),
        include_dimensions=bool(args.get("include_dimensions", True)),
    )
    remember(doc, snapshot, args.get("label"))
    if str(args.get("format", "text")) == "json":
        return result(True, "Snapshot taken.", snapshot=snapshot)
    text = render_snapshot(snapshot)
    if args.get("label"):
        text += f"\n(checkpoint '{args['label']}' stored)"
    return {"ok": True, "message": "Snapshot taken.", "_text": text}


@tool(
    "model_diff",
    "Read-only: what changed in the active document since the last model_snapshot (or since a "
    "named checkpoint): added/removed features, changed dimensions, sketch status, size, volume, "
    "new or fixed rebuild errors. Returns 'no change' when nothing differs.",
    {
        "since": {"type": "string", "description": "Checkpoint label from model_snapshot. Default: the last snapshot."},
        "update": {"type": "boolean", "default": True, "description": "Make the current state the new baseline."},
    },
)
def model_diff(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = active_document()
    key = doc_key(doc)
    since = args.get("since")
    baseline = _CHECKPOINTS.get(key, {}).get(str(since)) if since else _LAST.get(key)
    if baseline is None:
        return result(False, "No baseline snapshot for this document. Call model_snapshot first."
                      + (f" (no checkpoint '{since}')" if since else ""))
    current = take_snapshot(doc)
    text = diff_snapshots(baseline, current) or "no change"
    if bool(args.get("update", True)):
        _LAST[key] = current
    header = f"diff since {'checkpoint ' + str(since) if since else 'last snapshot'}:"
    return {"ok": True, "message": "Diff computed.", "_text": f"{header}\n{text}"}
