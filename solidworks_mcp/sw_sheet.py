"""Configuration-scoped preparation of part bodies for drawings.

Rectangular reconstruction is an explicit design operation, not a geometric
flattening algorithm. Source bodies are retained. Nothing here saves a file.
"""

from __future__ import annotations

from contextlib import contextmanager
import math
from typing import Any, Iterator

import pythoncom
from win32com.client import VARIANT

from .sw_core import (
    active_document, as_list, double_array, extension, flag_methods, nothing,
    require_document_path, result, tool, value,
)


PATH = {"type": "string", "description": "Expected active part document key from info (saved path or untitled:<exact title>)."}
NAME = {"type": "string", "minLength": 1}
VECTOR = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
SPECIFY_CONFIGURATIONS = 3


def _name(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{label} must be a nonempty name.")
    return raw


def _number(raw: Any, label: str, positive: bool = False) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{label} must be a finite number.")
    number = float(raw)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{label} must be finite" + (" and positive." if positive else "."))
    return number


@tool(
    "create_sketched_bend",
    "Create one native sheet-metal sketched bend from an existing planar line sketch and an interior "
    "point on its stationary planar sheet-metal face. Uses a centerline bend and a custom "
    "K factor. The existing sheet-metal body must already exist. No save or rollback.",
    {"expected_document_path": PATH, "sketch_name": NAME, "feature_name": NAME,
     "stationary_point_mm": VECTOR, "angle_deg": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 180},
     "inner_radius_mm": {"type": "number", "exclusiveMinimum": 0},
     "reverse_direction": {"type": "boolean", "default": True},
     "k_factor": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5}},
    ["expected_document_path", "sketch_name", "feature_name", "stationary_point_mm", "angle_deg", "inner_radius_mm"],
)
def create_sketched_bend(args: dict[str, Any]) -> dict[str, Any]:
    feature = None
    doc = None
    readback = None
    try:
        name = _name(args.get("feature_name"), "feature_name")
        sketch_name = _name(args.get("sketch_name"), "sketch_name")
        point = _vector(args.get("stationary_point_mm"), "stationary_point_mm")
        angle = _number(args.get("angle_deg"), "angle_deg", True)
        radius = _number(args.get("inner_radius_mm"), "inner_radius_mm", True)
        k = _number(args.get("k_factor", 0.5), "k_factor")
        reverse = args.get("reverse_direction", True)
        if angle >= 180 or not 0 <= k <= 1 or not isinstance(reverse, bool):
            raise ValueError("Require angle_deg < 180, k_factor in [0,1], and a boolean reverse_direction.")
        _, doc = require_document_path(args["expected_document_path"], 1)
        if value(value(doc, "SketchManager"), "ActiveSketch") is not None:
            raise ValueError("Close the active sketch before creating a bend.")
        _unused_feature_name(doc, name)
        sketches = [f for f in _features(doc) if str(value(f, "Name")) == sketch_name]
        if len(sketches) != 1 or str(value(sketches[0], "GetTypeName2")) != "ProfileFeature":
            raise ValueError("sketch_name must identify one existing planar sketch.")
        segments = [s for s in as_list(value(value(sketches[0], "GetSpecificFeature2"), "GetSketchSegments"))
                    if not value(s, "ConstructionGeometry")]
        if len(segments) != 1 or int(value(segments[0], "GetType")) != 0:
            raise ValueError("The bend sketch must contain exactly one ordinary straight line.")
        solids = as_list(flag_methods(doc, "GetBodies2").GetBodies2(0, False))
        if len(solids) != 1 or not value(solids[0], "IsSheetMetal"):
            raise ValueError("Require exactly one native sheet-metal solid.")
        flag_methods(doc, "ClearSelection2").ClearSelection2(True)
        if not flag_methods(sketches[0], "Select2").Select2(False, 0):
            raise RuntimeError("Could not select the bend sketch.")
        p = [x / 1000 for x in point]
        if not extension(doc).SelectByID2("", "FACE", *p, True, 0, nothing(), 0):
            raise ValueError("No stationary face at stationary_point_mm.")
        face = value(doc, "SelectionManager").GetSelectedObject6(2, -1)
        if int(value(value(face, "GetSurface"), "Identity")) != 4001 or not value(value(face, "GetBody"), "IsSheetMetal"):
            raise ValueError("The stationary point must select a planar sheet-metal face.")
        closest = flag_methods(face, "GetClosestPointOn").GetClosestPointOn(*p)
        if closest is None or math.dist(p, closest[:3]) > 1e-8:
            raise ValueError("stationary_point_mm must lie on the stationary face.")
        manager = flag_methods(value(doc, "FeatureManager"), "CreateDefinition", "CreateCustomBendAllowance", "CreateFeature")
        data = manager.CreateDefinition(35)
        data.BendAngle = math.radians(angle)
        data.BendRadius = radius / 1000
        data.PositionType = 4
        data.ReverseDirection = reverse
        data.UseDefaultBendRadius = False
        data.UseGaugeTable = False
        allowance = manager.CreateCustomBendAllowance()
        allowance.KFactor = k
        flag_methods(data, "SetCustomBendAllowance").SetCustomBendAllowance(allowance)
        feature = manager.CreateFeature(data)
        if feature is None:
            raise RuntimeError("SOLIDWORKS did not create the sketched bend.")
        _rename(feature, name)
        flag_methods(doc, "ForceRebuild3").ForceRebuild3(False)
        flag_methods(doc, "ClearSelection2").ClearSelection2(True)
        readback = _read_sketched_bend(doc, feature)
        if readback["native_error"] or readback["rolled_back"]:
            raise RuntimeError(f"The new bend has native error {readback['native_error']} or remains rolled back.")
        expected = {"angle_deg": angle, "inner_radius_mm": radius, "k_factor": k,
                    "reverse_direction": reverse, "position_type": 4,
                    "use_default_bend_radius": False, "use_default_bend_allowance": False,
                    "use_gauge_table": False}
        mismatches = []
        for key, requested in expected.items():
            actual = readback[key]
            matches = (actual == requested if isinstance(requested, bool) else
                       actual is not None and math.isclose(actual, requested, rel_tol=1e-7, abs_tol=1e-7))
            if not matches:
                mismatches.append(key)
        if mismatches:
            raise RuntimeError("Created bend native readback differs from the request: " + ", ".join(mismatches) + ".")
        return result(True, "Created native sketched bend.", **readback,
                      diff={"added_feature": name, "type": "SM3dBend", "sketch": sketch_name})
    except Exception as exc:
        failure = _failure(exc, feature)
        if readback is not None:
            failure["data"]["native_readback"] = readback
        return failure
    finally:
        if doc is not None:
            flag_methods(doc, "ClearSelection2").ClearSelection2(True)


def _read_sketched_bend(doc: Any, feature: Any) -> dict[str, Any]:
    if str(value(feature, "GetTypeName2")) != "SM3dBend":
        raise ValueError("feature_name must identify a native SM3dBend feature.")
    data = value(feature, "GetDefinition")
    if data is None:
        raise RuntimeError("The native sketched-bend definition is unavailable.")
    use_default_allowance = bool(value(data, "UseDefaultBendAllowance"))
    allowance = value(data, "GetCustomBendAllowance")
    if allowance is None and not use_default_allowance:
        raise RuntimeError("The native custom bend allowance is unavailable.")
    allowance_type = int(value(allowance, "Type")) if allowance is not None else None
    custom_k = _number(value(allowance, "KFactor"), "native KFactor") if allowance_type == 2 else None
    return {
        "feature": str(value(feature, "Name")), "type": "SM3dBend",
        "configuration": _configurations(doc)[1],
        "angle_deg": math.degrees(_number(value(data, "BendAngle"), "native BendAngle")),
        "inner_radius_mm": _number(value(data, "BendRadius"), "native BendRadius") * 1000,
        "reverse_direction": bool(value(data, "ReverseDirection")),
        "position_type": int(value(data, "PositionType")),
        "use_default_bend_radius": bool(value(data, "UseDefaultBendRadius")),
        "use_default_bend_allowance": use_default_allowance,
        "use_gauge_table": bool(value(data, "UseGaugeTable")),
        "custom_bend_allowance": {"type": allowance_type, "k_factor": custom_k},
        "k_factor": custom_k if not use_default_allowance else None,
        "k_factor_source": "default_unavailable" if use_default_allowance else "custom",
        "native_error": int(value(feature, "GetErrorCode")),
        "rolled_back": bool(value(feature, "IsRolledBack")),
        "stationary_reference": {
            "available": False,
            "face_ref": None, "point_bend_sketch_mm": None,
            "reason": "Requires native selection access, which marks the document dirty.",
        },
    }


@tool(
    "get_sketched_bend",
    "Read the actual native SM3dBend definition in the active configuration. No save, rebuild, "
    "configuration switch, or selection clearing. Custom allowance values are distinguished "
    "from effective defaults; unavailable stationary references are explicit.",
    {"expected_document_path": PATH, "feature_name": NAME},
    ["expected_document_path", "feature_name"],
)
def get_sketched_bend(args: dict[str, Any]) -> dict[str, Any]:
    try:
        name = _name(args.get("feature_name"), "feature_name")
        _, doc = require_document_path(args["expected_document_path"], 1)
        if value(value(doc, "SketchManager"), "ActiveSketch") is not None:
            raise ValueError("Close the active sketch before inspecting a bend.")
        matches = [feature for feature in _features(doc) if str(value(feature, "Name")) == name]
        if len(matches) != 1:
            raise ValueError(f"Feature '{name}' must resolve uniquely; found {len(matches)}.")
        return result(True, "Read native sketched bend.", **_read_sketched_bend(doc, matches[0]))
    except Exception as exc:
        return _failure(exc)


def _vector(raw: Any, label: str) -> list[float]:
    if not isinstance(raw, (tuple, list)) or len(raw) != 3:
        raise ValueError(f"{label} must contain exactly three numbers.")
    return [_number(n, label) for n in raw]


def _configurations(doc: Any) -> tuple[list[str], str]:
    names = [str(n) for n in as_list(value(doc, "GetConfigurationNames"))]
    if not names or len(names) != len(set(names)):
        raise RuntimeError("Configuration names are missing or ambiguous.")
    manager = value(doc, "ConfigurationManager")
    current = str(value(value(manager, "ActiveConfiguration"), "Name"))
    if current not in names:
        raise RuntimeError("The active configuration could not be identified.")
    return names, current


def _activate(doc: Any, configuration: str) -> None:
    if not flag_methods(doc, "ShowConfiguration2").ShowConfiguration2(configuration):
        raise RuntimeError(f"Could not activate configuration '{configuration}'.")
    if _configurations(doc)[1] != configuration:
        raise RuntimeError(f"Configuration '{configuration}' did not become active.")


@contextmanager
def _configuration(doc: Any, target: str) -> Iterator[None]:
    names, original = _configurations(doc)
    if target not in names:
        raise ValueError(f"Configuration '{target}' does not exist.")
    try:
        if target != original:
            _activate(doc, target)
        yield
    finally:
        if _configurations(doc)[1] != original:
            _activate(doc, original)


def _features(doc: Any) -> list[Any]:
    features = []
    feature = value(doc, "FirstFeature")
    while feature is not None:
        features.append(feature)
        feature = value(feature, "GetNextFeature")
    return features


def _unused_feature_name(doc: Any, name: str) -> None:
    if any(str(value(f, "Name")) == name for f in _features(doc)):
        raise ValueError(f"Feature '{name}' already exists; no feature was changed.")


def _bodies(doc: Any, names: list[str]) -> list[Any]:
    available = as_list(flag_methods(doc, "GetBodies2").GetBodies2(-1, False))
    bodies = []
    for name in names:
        matches = [b for b in available if str(value(b, "Name")) == name]
        if len(matches) != 1:
            raise ValueError(f"Body '{name}' must resolve uniquely; found {len(matches)}.")
        bodies.append(matches[0])
    return bodies


def _body_box_mm(body: Any) -> list[float]:
    box = [_number(float(n) * 1000, "bounding box") for n in as_list(value(body, "GetBodyBox"))]
    if len(box) != 6:
        raise RuntimeError("A body's bounding box is unavailable.")
    return box


def _verify_translation(doc: Any, expected: list[list[float]]) -> list[list[float]]:
    actual = [_body_box_mm(b) for b in as_list(flag_methods(doc, "GetBodies2").GetBodies2(-1, False))]
    unmatched = list(actual)
    for box in expected:
        match = next((i for i, candidate in enumerate(unmatched)
                      if all(math.isclose(a, b, rel_tol=1e-7, abs_tol=1e-3)
                             for a, b in zip(box, candidate))), None)
        if match is None:
            raise RuntimeError("Body geometry does not match the requested translation (bounding-box tolerance 0.001 mm).")
        unmatched.pop(match)
    if unmatched:
        raise RuntimeError("The body count changed during the requested translation.")
    return actual


def _suppression(feature: Any, names: list[str]) -> dict[str, bool]:
    raw = flag_methods(feature, "IsSuppressed2").IsSuppressed2(SPECIFY_CONFIGURATIONS, _configuration_array(names))
    states = as_list(raw)
    if len(states) != len(names):
        raise RuntimeError("Could not read feature suppression in every configuration.")
    return dict(zip(names, map(bool, states)))


def _configuration_array(names: list[str]) -> Any:
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_BSTR, tuple(names))


def _isolate(feature: Any, names: list[str], target: str) -> dict[str, bool]:
    feature = flag_methods(feature, "SetSuppression2")
    others = [n for n in names if n != target]
    if others and not feature.SetSuppression2(0, SPECIFY_CONFIGURATIONS, _configuration_array(others)):
        raise RuntimeError("Could not suppress the new feature in the other configurations.")
    if not feature.SetSuppression2(1, SPECIFY_CONFIGURATIONS, _configuration_array([target])):
        raise RuntimeError("Could not enable the new feature in the drawing configuration.")
    states = _suppression(feature, names)
    if any(states[n] != (n != target) for n in names):
        raise RuntimeError("New feature suppression does not match the drawing-only scope.")
    return states


def _rename(feature: Any, name: str) -> None:
    feature.Name = name
    if str(value(feature, "Name")) != name:
        raise RuntimeError(f"Could not assign feature name '{name}'.")


def _failure(exc: Exception, feature: Any = None) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if feature is not None:
        try:
            data["remaining_feature"] = str(value(feature, "Name"))
        except Exception:
            data["remaining_feature"] = "Created feature; name unavailable."
        data["rollback_performed"] = False
    return result(False, str(exc), **data)


@tool(
    "list_configurations",
    "Read configuration names and the current active configuration without changing it. "
    "include_suppression optionally reads every feature's state in every configuration.",
    {"include_suppression": {"type": "boolean", "default": False}},
)
def list_configurations(args: dict[str, Any]) -> dict[str, Any]:
    try:
        _, doc = active_document()
        names, current = _configurations(doc)
        data: dict[str, Any] = {"configurations": names, "active_configuration": current}
        if args.get("include_suppression", False):
            data["features"] = [
                {"name": str(value(f, "Name")), "suppressed": _suppression(f, names)}
                for f in _features(doc)
            ]
        return result(True, "Read configurations.", **data)
    except Exception as exc:
        return _failure(exc)


@tool(
    "set_configuration", "Activate a named configuration in the explicitly identified part; no save.",
    {"expected_document_path": PATH, "name": NAME}, ["expected_document_path", "name"],
)
def set_configuration(args: dict[str, Any]) -> dict[str, Any]:
    try:
        name = _name(args.get("name"), "name")
        _, doc = require_document_path(args["expected_document_path"], 1)
        names, original = _configurations(doc)
        if name not in names:
            raise ValueError(f"Configuration '{name}' does not exist.")
        if name != original:
            _activate(doc, name)
        return result(True, "Activated configuration.", active_configuration=name,
                      diff={"previous_configuration": original, "active_configuration": name})
    except Exception as exc:
        return _failure(exc)


@tool(
    "set_configuration_features",
    "Set suppression of uniquely named existing features only in the specified configuration. "
    "Resolve all names before mutation, verify suppression in every configuration, and restore "
    "the caller configuration. No save or rollback is promised.",
    {"expected_document_path": PATH, "configuration": NAME,
     "feature_names": {"type": "array", "items": NAME, "minItems": 1, "uniqueItems": True},
     "suppressed": {"type": "boolean"}},
    ["expected_document_path", "configuration", "feature_names", "suppressed"],
)
def set_configuration_features(args: dict[str, Any]) -> dict[str, Any]:
    attempted = []
    try:
        target = _name(args.get("configuration"), "configuration")
        raw_names = args.get("feature_names")
        if not isinstance(raw_names, list) or not raw_names:
            raise ValueError("feature_names must be a nonempty list of unique names.")
        feature_names = [_name(n, "feature_names") for n in raw_names]
        if len(feature_names) != len(set(feature_names)):
            raise ValueError("feature_names must contain unique names.")
        suppressed = args.get("suppressed")
        if not isinstance(suppressed, bool):
            raise ValueError("suppressed must be a boolean.")
        _, doc = require_document_path(args["expected_document_path"], 1)
        names, original = _configurations(doc)
        if target not in names:
            raise ValueError(f"Configuration '{target}' does not exist.")
        available = _features(doc)
        features = []
        for name in feature_names:
            matches = [f for f in available if str(value(f, "Name")) == name]
            if len(matches) != 1:
                raise ValueError(f"Feature '{name}' must resolve uniquely; found {len(matches)}.")
            features.append(matches[0])
        before = {name: _suppression(f, names) for name, f in zip(feature_names, features)}
        with _configuration(doc, target):
            for name, feature in zip(feature_names, features):
                attempted.append(name)
                if not flag_methods(feature, "SetSuppression2").SetSuppression2(
                        0 if suppressed else 1, SPECIFY_CONFIGURATIONS, _configuration_array([target])):
                    raise RuntimeError(f"Could not set suppression for feature '{name}'.")
            after = {name: _suppression(f, names) for name, f in zip(feature_names, features)}
            for name in feature_names:
                expected = {**before[name], target: suppressed}
                if after[name] != expected:
                    raise RuntimeError(f"Feature '{name}' suppression does not match the requested configuration scope.")
        return result(True, "Updated feature suppression in the specified configuration.",
                      configuration=target, active_configuration=original, suppressed=after,
                      diff={"configuration": target, "feature_names": feature_names,
                            "suppressed": suppressed})
    except Exception as exc:
        if attempted:
            return result(False, str(exc), attempted_features=attempted, rollback_performed=False)
        return _failure(exc)


@tool(
    "create_drawing_configuration",
    "Create a derived drawing configuration from an explicit existing source and restore the "
    "previous active configuration. An existing name is returned without mutation. No save.",
    {"expected_document_path": PATH, "name": NAME, "source_configuration": NAME},
    ["expected_document_path", "name", "source_configuration"],
)
def create_drawing_configuration(args: dict[str, Any]) -> dict[str, Any]:
    try:
        name = _name(args.get("name"), "name")
        source = _name(args.get("source_configuration"), "source_configuration")
        _, doc = require_document_path(args["expected_document_path"], 1)
        names, original = _configurations(doc)
        if source not in names:
            raise ValueError(f"Source configuration '{source}' does not exist.")
        if name in names:
            return result(True, "Configuration already exists; no changes made.", configuration=name,
                          created=False, active_configuration=original)
        with _configuration(doc, source):
            manager = flag_methods(value(doc, "ConfigurationManager"), "AddConfiguration2")
            created = manager.AddConfiguration2(name, "Drawing preparation", "", 0, source, "", True)
            if created is None or name not in _configurations(doc)[0]:
                raise RuntimeError("SOLIDWORKS did not create the drawing configuration.")
        return result(True, "Created drawing configuration.", configuration=name, created=True,
                      source_configuration=source, active_configuration=original,
                      diff={"added_configuration": name})
    except Exception as exc:
        return _failure(exc)


@tool(
    "move_bodies_for_drawing",
    "Translate explicitly named bodies with a native Move/Copy Body feature in the given drawing "
    "configuration. Suppress the new feature in every other configuration and restore the active "
    "configuration. Existing feature names are refused. No save or rollback is promised.",
    {"expected_document_path": PATH, "configuration": NAME,
     "body_names": {"type": "array", "items": NAME, "minItems": 1, "uniqueItems": True},
     "translation_mm": VECTOR, "feature_name": NAME},
    ["expected_document_path", "configuration", "body_names", "translation_mm", "feature_name"],
)
def move_bodies_for_drawing(args: dict[str, Any]) -> dict[str, Any]:
    feature = None
    try:
        target = _name(args.get("configuration"), "configuration")
        name = _name(args.get("feature_name"), "feature_name")
        raw_names = args.get("body_names")
        if not isinstance(raw_names, list) or not raw_names:
            raise ValueError("body_names must be a nonempty list of unique names.")
        body_names = [_name(n, "body_names") for n in raw_names]
        if len(body_names) != len(set(body_names)):
            raise ValueError("body_names must contain unique names.")
        translation = _vector(args.get("translation_mm"), "translation_mm")
        _, doc = require_document_path(args["expected_document_path"], 1)
        names, original = _configurations(doc)
        _unused_feature_name(doc, name)
        with _configuration(doc, target):
            bodies = _bodies(doc, body_names)
            all_bodies = as_list(flag_methods(doc, "GetBodies2").GetBodies2(-1, False))
            expected_boxes = []
            for body in all_bodies:
                box = _body_box_mm(body)
                if str(value(body, "Name")) in body_names:
                    box = [coordinate + translation[i % 3] for i, coordinate in enumerate(box)]
                expected_boxes.append(box)
            flag_methods(doc, "ClearSelection2").ClearSelection2(True)
            try:
                for body in bodies:
                    if not extension(doc).SelectByID2(str(value(body, "Name")), "SOLIDBODY",
                                                     0, 0, 0, True, 1, nothing(), 0):
                        raise RuntimeError("Could not select all named bodies.")
                manager = flag_methods(value(doc, "FeatureManager"), "InsertMoveCopyBody2")
                feature = manager.InsertMoveCopyBody2(
                    *(n / 1000 for n in translation), 0., 0., 0., 0., 0., 0., 0., False, 1)
                if feature is None:
                    raise RuntimeError("SOLIDWORKS did not create the move feature.")
                states = _isolate(feature, names, target)
                _rename(feature, name)
                verified_boxes = _verify_translation(doc, expected_boxes)
            finally:
                flag_methods(doc, "ClearSelection2").ClearSelection2(True)
        return result(True, "Created drawing-only body translation.", feature=name,
                      active_configuration=original, suppressed=states,
                      bounding_boxes_mm=verified_boxes, verification_tolerance_mm=0.001,
                      diff={"added_feature": name, "configuration": target,
                            "body_names": body_names, "translation_mm": translation})
    except Exception as exc:
        return _failure(exc, feature)


def _volume_mm3(body: Any) -> float:
    mass = as_list(flag_methods(body, "GetMassProperties").GetMassProperties(1.0))
    if len(mass) < 4:
        raise RuntimeError("Body volume is unavailable.")
    return _number(float(mass[3]) * 1e9, "body volume", True)


def _verify_box(body: Any, origin: list[float], dimensions: list[float]) -> dict[str, Any]:
    box = [_number(float(n) * 1000, "bounding box") for n in as_list(value(body, "GetBodyBox"))]
    if len(box) != 6:
        raise RuntimeError("The new body's bounding box is unavailable.")
    width, length, thickness = dimensions
    expected = [origin[0] - width / 2, origin[1] - length / 2, origin[2],
                origin[0] + width / 2, origin[1] + length / 2, origin[2] + thickness]
    volume = _volume_mm3(body)
    if not all(math.isclose(a, b, rel_tol=1e-7, abs_tol=1e-5) for a, b in zip(box, expected)):
        raise RuntimeError("New body position or XYZ dimensions differ from the requested box; no rotation applied.")
    if not math.isclose(volume, width * length * thickness, rel_tol=1e-7, abs_tol=1e-6):
        raise RuntimeError("New body volume differs from the requested rectangular sheet.")
    return {"bounding_box_mm": box, "volume_mm3": volume, "dimensions_mm": dimensions}


@tool(
    "create_flat_sheet_body",
    "Create an explicitly approved rectangular reconstruction of a named imported silicone body, "
    "not a general flatten operation. The source remains untouched. Calling this tool asserts "
    "that rectangular reconstruction was approved. origin_mm is the center of the bottom face, "
    "width is world X, length world Y, thickness world +Z. length_mode=explicit requires length_mm; "
    "preserve_volume forbids length_mm and derives length from source volume/(width*thickness). "
    "New feature is enabled only in the explicit drawing configuration. Restore caller configuration; "
    "no save. Verification failures report any remaining feature without claiming rollback.",
    {"expected_document_path": PATH, "configuration": NAME, "source_body_name": NAME,
     "width_mm": {"type": "number", "exclusiveMinimum": 0},
     "thickness_mm": {"type": "number", "exclusiveMinimum": 0}, "origin_mm": VECTOR,
     "feature_name": NAME, "length_mm": {"type": "number", "exclusiveMinimum": 0},
     "length_mode": {"type": "string", "enum": ["explicit", "preserve_volume"], "default": "explicit"}},
    ["expected_document_path", "configuration", "source_body_name", "width_mm", "thickness_mm",
     "origin_mm", "feature_name"],
)
def create_flat_sheet_body(args: dict[str, Any]) -> dict[str, Any]:
    feature = None
    try:
        target = _name(args.get("configuration"), "configuration")
        name = _name(args.get("feature_name"), "feature_name")
        source_name = _name(args.get("source_body_name"), "source_body_name")
        width = _number(args.get("width_mm"), "width_mm", True)
        thickness = _number(args.get("thickness_mm"), "thickness_mm", True)
        origin = _vector(args.get("origin_mm"), "origin_mm")
        mode = args.get("length_mode", "explicit")
        if mode == "explicit":
            length = _number(args.get("length_mm"), "length_mm", True)
        elif mode == "preserve_volume":
            if args.get("length_mm") is not None:
                raise ValueError("preserve_volume forbids length_mm; choose one length policy.")
            length = None
        else:
            raise ValueError("length_mode must be explicit or preserve_volume.")
        app, doc = require_document_path(args["expected_document_path"], 1)
        names, original = _configurations(doc)
        _unused_feature_name(doc, name)
        with _configuration(doc, target):
            source = _bodies(doc, [source_name])[0]
            if length is None:
                length = _number(_volume_mm3(source) / (width * thickness), "derived length_mm", True)
            dimensions = [width, length, thickness]
            parameters = [*(n / 1000 for n in origin), 0., 0., 1.,
                          width / 1000, length / 1000, thickness / 1000]
            modeler = flag_methods(value(app, "GetModeler"), "CreateBodyFromBox3")
            temporary = modeler.CreateBodyFromBox3(double_array(parameters))
            if temporary is None:
                raise RuntimeError("SOLIDWORKS did not create a temporary rectangular body.")
            _verify_box(temporary, origin, dimensions)
            feature = flag_methods(doc, "CreateFeatureFromBody3").CreateFeatureFromBody3(temporary, False, 0)
            if feature is None:
                raise RuntimeError("SOLIDWORKS did not create the rectangular sheet feature.")
            states = _isolate(feature, names, target)
            _rename(feature, name)
            faces = as_list(value(feature, "GetFaces"))
            if not faces:
                raise RuntimeError("The created feature has no readable faces for body verification.")
            verification = _verify_box(value(faces[0], "GetBody"), origin, dimensions)
        return result(True, "Created drawing-only rectangular reconstruction; source body retained.",
                      feature=name, source_body_name=source_name, length_mode=mode,
                      active_configuration=original, suppressed=states, **verification,
                      diff={"added_feature": name, "configuration": target})
    except Exception as exc:
        return _failure(exc, feature)
