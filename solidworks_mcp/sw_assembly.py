# Copyright 2026 JIALE LIU
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

"""Assembly work: components and mates.

create_new_document could already make an assembly, which was not much use
without a way to put anything in it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import math

import pythoncom
import win32com.client

from .sw_core import (
    MATE_ALIGNMENTS,
    MATE_TYPES,
    SELECTION_SCHEMA,
    apply_transform,
    byref_long,
    clear_selection,
    component_transform,
    document_type,
    document_info,
    double_array,
    flag_methods,
    iter_components,
    logger,
    mm_point,
    rebuild,
    require_assembly,
    require_document_path,
    require_selection,
    result,
    running_app,
    safe,
    to_deg,
    to_m,
    to_rad,
    to_mm,
    tool,
    value,
)


ADD_MATE_ERRORS = {
    0: "unknown error",
    1: "no error",
    2: "incorrect mate type for that selection",
    3: "incorrect alignment",
    4: "incorrect selections",
    5: "the mate would over-define the assembly",
    6: "incorrect gear ratios",
}


def _pose_matrix(pose: Any) -> list[float]:
    if not isinstance(pose, dict):
        raise ValueError("pose must contain rotation and translation_mm arrays")
    rotation, translation = pose.get("rotation"), pose.get("translation_mm")
    if not isinstance(rotation, (list, tuple)) or len(rotation) != 9:
        raise ValueError("pose.rotation must contain nine numbers")
    if not isinstance(translation, (list, tuple)) or len(translation) != 3:
        raise ValueError("pose.translation_mm must contain three numbers")
    try:
        r = [float(x) for x in rotation]
        t = [float(x) for x in translation]
    except (TypeError, ValueError) as exc:
        raise ValueError("pose values must be finite numbers") from exc
    if not all(math.isfinite(x) for x in r + t):
        raise ValueError("pose values must be finite numbers")
    for i in range(3):
        for j in range(3):
            dot = sum(r[3 * i + k] * r[3 * j + k] for k in range(3))
            if not math.isclose(dot, 1.0 if i == j else 0.0, abs_tol=1e-8):
                raise ValueError("pose.rotation must be an orthonormal rotation")
    det = (r[0] * (r[4] * r[8] - r[5] * r[7])
           - r[1] * (r[3] * r[8] - r[5] * r[6])
           + r[2] * (r[3] * r[7] - r[4] * r[6]))
    if not math.isclose(det, 1.0, abs_tol=1e-8):
        raise ValueError("pose.rotation must be right-handed")
    return r + [to_m(x) for x in t] + [1.0, 0.0, 0.0, 0.0]


def _pose_from_matrix(matrix: list[float]) -> dict[str, Any]:
    return {"rotation": matrix[:9], "translation_mm": [to_mm(x) for x in matrix[9:12]]}


def _component_health(component: Any) -> dict[str, Any]:
    health: dict[str, Any] = {
        "hidden": None, "suppression_state": "unknown", "dirty": None,
        "referenced_configuration": None, "document_configuration": None,
        "geometry_state": "unknown", "solid_bodies": None, "surface_bodies": None,
    }
    try:
        health["hidden"] = bool(flag_methods(component, "IsHidden").IsHidden(False))
    except Exception:
        pass
    try:
        state = int(value(flag_methods(component, "GetSuppression"), "GetSuppression"))
    except Exception:
        return health
    health["suppression_state"] = {
        0: "suppressed", 1: "lightweight", 2: "fully_resolved", 3: "resolved",
        4: "fully_lightweight", 5: "internal_id_mismatch",
    }.get(state, "unknown")
    health["referenced_configuration"] = safe(component, "ReferencedConfiguration")
    if state not in (2, 3):
        return health
    try:
        model = value(flag_methods(component, "GetModelDoc2"), "GetModelDoc2")
        if model is None:
            return health
        dirty = safe(model, "GetSaveFlag")
        health["dirty"] = bool(dirty) if dirty is not None else None
        if document_type(model) != 1:
            health["geometry_state"] = "not_part"
            return health
        manager = value(model, "ConfigurationManager")
        active = value(manager, "ActiveConfiguration")
        health["document_configuration"] = str(value(active, "Name"))
        if not health["referenced_configuration"] or health["referenced_configuration"] != health["document_configuration"]:
            return health
        method = flag_methods(component, "GetBodies3")
        counts = []
        for body_type in (0, 1):
            info = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None)
            bodies = method.GetBodies3(body_type, info)
            counts.append(len(bodies) if bodies is not None else 0)
        health["solid_bodies"], health["surface_bodies"] = counts
        health["geometry_state"] = "present" if sum(counts) else "empty"
    except Exception:
        pass
    return health


@tool(
    "list_components",
    "Read-only: list the components of the active assembly with their file paths and positions.",
    {
        "top_level_only": {"type": "boolean", "default": True},
        "expected_document_path": {"type": "string", "description": "Exact active assembly path; refuse a different document."},
        "include_pose": {"type": "boolean", "default": False, "description": "Return rotation and translation usable by insert_component."},
        "include_health": {"type": "boolean", "default": False, "description": "Inspect visibility, suppression, configuration, geometry and unsaved state; unknown is distinct from empty."},
        "max_components": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Maximum components to return; defaults to 100 when health is requested."},
    },
)
def list_components(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = (require_document_path(str(args["expected_document_path"]), 2)
              if "expected_document_path" in args else require_assembly())
    components = []
    entries = iter_components(doc, bool(args.get("top_level_only", True)))
    limit = args.get("max_components", 100 if args.get("include_health") else None)
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            return result(False, "max_components must be an integer between 1 and 500.")
        entries_to_read = entries[:limit]
    else:
        entries_to_read = entries
    for component in entries_to_read:
        matrix = component_transform(component)
        entry: dict[str, Any] = {
            "name": str(safe(component, "Name2", "")),
            "path": str(safe(component, "GetPathName", "")),
            "suppressed": bool(safe(component, "IsSuppressed", False)),
            "fixed": bool(safe(component, "IsFixed", False)),
        }
        if matrix is not None:
            entry["origin_mm"] = mm_point(apply_transform([0.0, 0.0, 0.0], matrix))
            if args.get("include_pose"):
                if math.isclose(matrix[12], 1.0, rel_tol=1e-9, abs_tol=1e-9):
                    entry["pose"] = _pose_from_matrix(matrix)
                else:
                    entry["pose_error"] = "Scaled transforms cannot be represented as a rigid pose."
        if args.get("include_health"):
            entry["health"] = _component_health(component)
        components.append(entry)
    extra = {"total_components": len(entries), "truncated": len(components) < len(entries)} if limit is not None else {}
    return result(True, f"Read {len(components)} components.", components=components, **extra)


@tool(
    "insert_component",
    "Insert a part or sub-assembly file into the active assembly at the given position. "
    "Position is millimetres in assembly space.",
    {
        "path": {"type": "string", "description": "Full path to the .sldprt or .sldasm file."},
        "expected_document_path": {"type": "string", "description": "Exact active assembly path; refuse a different document."},
        "x_mm": {"type": "number", "default": 0},
        "y_mm": {"type": "number", "default": 0},
        "z_mm": {"type": "number", "default": 0},
        "configuration": {"type": "string", "description": "Named configuration to insert. Defaults to the active one."},
        "pose": {
            "type": "object", "description": "Full assembly-space rigid pose; rotation is the SOLIDWORKS column-major 3x3 matrix.",
            "properties": {
                "rotation": {"type": "array", "items": {"type": "number"}, "minItems": 9, "maxItems": 9},
                "translation_mm": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
            }, "required": ["rotation", "translation_mm"],
        },
        "virtual": {"type": "boolean", "default": False, "description": "Make the inserted component virtual in the assembly."},
    },
    ["path"],
)
def insert_component(args: dict[str, Any]) -> dict[str, Any]:
    pose = args.get("pose")
    matrix = None
    if pose is not None:
        if any(k in args for k in ("x_mm", "y_mm", "z_mm")):
            return result(False, "Use either pose or x_mm/y_mm/z_mm, not both.")
        try:
            matrix = _pose_matrix(pose)
        except ValueError as exc:
            return result(False, str(exc))
    app, doc = (require_document_path(str(args["expected_document_path"]), 2)
                if "expected_document_path" in args else require_assembly())
    path = Path(str(args["path"])).expanduser().resolve()
    if not path.is_file():
        return result(False, f"No such file: {path}")
    doc_type = {".sldprt": 1, ".sldasm": 2}.get(path.suffix.lower())
    if doc_type is None:
        return result(False, "Only .sldprt and .sldasm files can be inserted as components.")
    transform = None
    if matrix is not None:
        try:
            math_util = value(flag_methods(app, "GetMathUtility"), "GetMathUtility")
            transform = flag_methods(math_util, "CreateTransform").CreateTransform(double_array(matrix))
            if transform is None:
                return result(False, "SOLIDWORKS could not construct the requested pose.")
        except Exception as exc:
            return result(False, f"SOLIDWORKS could not construct the requested pose: {exc}")

    # AddComponent5 needs the referenced document loaded; open it silently first.
    errors, warnings = byref_long(0), byref_long(0)
    try:
        app.OpenDoc6(str(path), doc_type, 1, str(args.get("configuration", "") or ""), errors, warnings)
    except Exception:
        logger.info("Silent open of %s failed; letting AddComponent5 resolve it.", path)

    existing = str(args.get("configuration", "") or "")
    component = doc.AddComponent5(
        str(path), 0, "", False, existing,
        to_m(args.get("x_mm", 0)), to_m(args.get("y_mm", 0)), to_m(args.get("z_mm", 0)),
    )
    if component is None:
        return result(False, f"SOLIDWORKS did not insert {path.name} into the assembly.")
    if matrix is not None or bool(args.get("virtual", False)):
        try:
            if matrix is not None:
                component.Transform2 = transform
            expected = matrix if matrix is not None else component_transform(component)
            if expected is None or len(expected) < 16:
                raise RuntimeError("Could not inspect the inserted component pose")
            if args.get("virtual"):
                if not flag_methods(component, "MakeVirtual2").MakeVirtual2(False):
                    raise RuntimeError("SOLIDWORKS did not make the component virtual")
            if not rebuild(doc):
                raise RuntimeError("Assembly rebuild failed after insertion")
            actual = component_transform(component)
            if actual is None or len(actual) < 16 or any(
                not math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-9) for x, y in zip(expected, actual)
            ):
                raise RuntimeError("The resulting component pose did not match the requested pose")
            if args.get("virtual") and not bool(value(flag_methods(component, "IsVirtual"), "IsVirtual")):
                raise RuntimeError("The resulting component is not virtual")
        except Exception as exc:
            return result(False, f"Component was inserted, but its requested state could not be verified: {exc}",
                          component=str(safe(component, "Name2", "")), path=str(path), may_have_changes=True)
        return result(True, f"Inserted {path.name}.", component=str(safe(component, "Name2", "")),
                      path=str(path), component_path=str(safe(component, "GetPathName", "")),
                      pose=_pose_from_matrix(actual) if actual is not None else None,
                      virtual=bool(args.get("virtual", False)))
    rebuild(doc)
    return result(
        True,
        f"Inserted {path.name}.",
        component=str(safe(component, "Name2", "")),
        path=str(path),
    )


@tool(
    "add_mate",
    "Mate two entities in the active assembly. Select exactly two faces, edges, planes, or "
    "vertices via selection, then name the mate type. Distance is millimetres, angle is degrees.",
    {
        "mate_type": {"type": "string", "enum": sorted(MATE_TYPES)},
        "selection": SELECTION_SCHEMA,
        "alignment": {"type": "string", "enum": sorted(MATE_ALIGNMENTS), "default": "closest"},
        "distance_mm": {"type": "number", "default": 0, "description": "Used by mate_type=distance."},
        "angle_deg": {"type": "number", "default": 0, "description": "Used by mate_type=angle."},
        "flip": {"type": "boolean", "default": False},
        "lock_rotation": {"type": "boolean", "default": False},
    },
    ["mate_type", "selection"],
)
def add_mate(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_assembly()
    count = require_selection(doc, args["selection"])
    mate_type = MATE_TYPES[str(args["mate_type"])]
    alignment = MATE_ALIGNMENTS[str(args.get("alignment", "closest"))]
    status = byref_long(0)

    mate = doc.AddMate5(
        mate_type, alignment, bool(args.get("flip", False)),
        to_m(args.get("distance_mm", 0)), to_m(args.get("distance_mm", 0)), to_m(args.get("distance_mm", 0)),
        1.0, 1.0,
        to_rad(args.get("angle_deg", 0)), to_rad(args.get("angle_deg", 0)), to_rad(args.get("angle_deg", 0)),
        False, bool(args.get("lock_rotation", False)), 0, status,
    )
    code = int(status.value)
    clear_selection(doc)
    rebuild(doc)
    if mate is None:
        return result(
            False,
            f"SOLIDWORKS refused the mate: {ADD_MATE_ERRORS.get(code, f'status {code}')}.",
            entities=count,
            status=code,
        )
    if code != 1:
        # An over-defining mate is still added to the tree, so reporting a
        # plain failure here would contradict what list_mates shows.
        return result(
            True,
            f"Added a {args['mate_type']} mate, but SOLIDWORKS flagged it: "
            f"{ADD_MATE_ERRORS.get(code, f'status {code}')}.",
            mate=str(safe(mate, "Name", "")),
            entities=count,
            warning=ADD_MATE_ERRORS.get(code, f"status {code}"),
            status=code,
        )
    return result(
        True,
        f"Added a {args['mate_type']} mate.",
        mate=str(safe(mate, "Name", "")),
        entities=count,
    )


@tool(
    "list_mates",
    "Read-only: list the mates of the active assembly.",
    {},
)
def list_mates(args: dict[str, Any]) -> dict[str, Any]:
    from .sw_core import as_list, feature_property, iter_feature_objects

    _, doc = require_assembly()
    reverse_types = {v: k for k, v in MATE_TYPES.items()}
    mates = []
    for feature in iter_feature_objects(doc):
        if str(feature_property(feature, "GetTypeName2", "")) != "MateGroup":
            continue
        child = safe(feature, "GetFirstSubFeature")
        while child is not None:
            definition = safe(child, "GetSpecificFeature2")
            entry: dict[str, Any] = {"name": str(feature_property(child, "Name", ""))}
            if definition is not None:
                code = safe(definition, "Type")
                if code is not None:
                    entry["mate_type"] = reverse_types.get(int(code), f"type_{int(code)}")
                distance = safe(definition, "Distance")
                if distance is not None:
                    entry["distance_mm"] = round(to_mm(float(distance)), 6)
                angle = safe(definition, "Angle")
                if angle is not None:
                    entry["angle_deg"] = round(to_deg(float(angle)), 6)
            mates.append(entry)
            child = safe(child, "GetNextSubFeature")
    return result(True, f"Read {len(mates)} mates.", mates=mates)


@tool(
    "set_component_fixed",
    "Fix or float the named components in the active assembly.",
    {
        "component_names": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "fixed": {"type": "boolean", "default": True},
    },
    ["component_names"],
)
def set_component_fixed(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_assembly()
    names = [str(name) for name in args["component_names"]]
    require_selection(doc, {"components": names})
    if bool(args.get("fixed", True)):
        doc.FixComponent()
    else:
        doc.UnfixComponent()
    clear_selection(doc)
    rebuild(doc)
    return result(True, f"Set fixed={args.get('fixed', True)} on {len(names)} components.", components=names)
