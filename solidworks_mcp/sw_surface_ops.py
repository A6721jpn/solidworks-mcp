"""Single native surface commands with explicit document and configuration scope."""
from __future__ import annotations

import math
import re
from typing import Any

from .sw_core import (
    apply_selection, as_list, flag_methods, require_document_path, resolve_ref,
    result, safe, to_m, to_mm, tool, value,
)
from .sw_sheet import (
    _body_box_mm, _configuration, _configurations, _features, _isolate,
    _name, _number, _rename, _unused_feature_name, _volume_mm3,
)


_PATH = {"type": "string", "description": "Exact active part document_key; no implicit document switch."}
_NAME = {"type": "string", "minLength": 1}
_DIRECTIONS = {"side1": 0, "side2": 1, "both": 2}


def _boolean(raw: Any, label: str) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f"{label} must be boolean.")
    return raw


def _clear(doc: Any) -> None:
    flag_methods(doc, "ClearSelection2").ClearSelection2(True)


def _find_feature(doc: Any, name: str) -> Any:
    found = [feature for feature in _features(doc) if str(value(feature, "Name")) == name]
    if len(found) != 1:
        raise ValueError(f"Feature '{name}' must resolve uniquely; found {len(found)}.")
    return found[0]


def _feature_bodies(feature: Any, kind: int) -> list[Any]:
    bodies: dict[str, Any] = {}
    for face in as_list(value(feature, "GetFaces")):
        body = value(face, "GetBody")
        if int(value(body, "GetType")) != kind:
            raise RuntimeError("Feature contains a body of the wrong type.")
        bodies.setdefault(str(value(body, "Name")), body)
    return list(bodies.values())


def _body_summary(body: Any, kind: int) -> dict[str, Any]:
    summary = {"name": str(value(body, "Name")), "body_type": kind,
               "bounding_box_mm": _body_box_mm(body)}
    if kind == 0:
        summary["volume_mm3"] = _volume_mm3(body)
    else:
        area = sum(float(value(face, "GetArea")) for face in as_list(value(body, "GetFaces"))) * 1e6
        if not math.isfinite(area) or area <= 0:
            raise RuntimeError("Resulting sheet body has no positive readable area.")
        summary["area_mm2"] = area
    return summary


def _new_names(doc: Any, before: set[str]) -> list[str]:
    return [str(value(feature, "Name")) for feature in _features(doc)
            if str(value(feature, "Name")) not in before]


def _native_length(feature: Any, definition: Any, member: str, expected: float) -> tuple[float, str]:
    raw = safe(definition, member) if definition is not None else None
    if raw is not None:
        measured = to_mm(_number(float(raw), f"native {member}"))
        source = f"definition.{member}"
    else:
        display = safe(feature, "GetFirstDisplayDimension")
        if display is None:
            raise RuntimeError("Native feature length is unavailable for verification.")
        dimension = flag_methods(display, "GetDimension2").GetDimension2(0)
        measured = to_mm(float(value(dimension, "SystemValue")))
        source = "first_display_dimension"
    if not math.isfinite(measured) or not math.isclose(abs(measured), expected, rel_tol=1e-7, abs_tol=1e-6):
        raise RuntimeError(f"Native feature length {measured:g} mm differs from requested {expected:g} mm.")
    return measured, source


def _native_setting(definition: Any, member: str, expected: Any,
                    unverified: list[str]) -> Any:
    actual = safe(definition, member) if definition is not None else None
    if actual is None:
        unverified.append(member)
        return None
    actual = bool(actual) if isinstance(expected, bool) else int(actual)
    if actual != expected:
        raise RuntimeError(f"Native {member} differs: requested {expected!r}, got {actual!r}.")
    return actual


def _finish(doc: Any, original: str | None, attempted: bool, artifacts: list[str],
            payload: dict[str, Any] | None, error: Exception | None,
            before: set[str] | None) -> dict[str, Any]:
    cleanup_error = None
    if doc is not None:
        try:
            _clear(doc)
        except Exception as exc:
            cleanup_error = str(exc)
    current = None
    if doc is not None and original is not None:
        try:
            current = _configurations(doc)[1]
        except Exception:
            pass
    restored = original is None or current == original
    if error is None and cleanup_error is None and restored:
        return result(True, "Native surface operation completed without saving.", changed=True,
                      active_configuration=current, **(payload or {}))
    if attempted and doc is not None and before is not None and not artifacts:
        try:
            artifacts = _new_names(doc, before)
        except Exception:
            pass
    message = str(error) if error is not None else (
        "Configuration restoration could not be verified." if not restored else "Selection cleanup failed.")
    if error is not None and error.__context__ is not None:
        message += f" Previous failure: {error.__context__}"
    if cleanup_error:
        message += f" Selection cleanup failed: {cleanup_error}"
    return result(False, message, may_have_changes=attempted or not restored,
                  partial_artifacts=[{"kind": "feature", "name": name} for name in artifacts],
                  active_configuration=current, configuration_restored=restored,
                  rollback_performed=False, readback=payload)


@tool("offset_surface", "Create one native offset surface from explicit face refs in a named configuration. Zero offset copies faces. No backside inference, thickening, saving or document switching.",
      {"expected_document_path": _PATH, "configuration": _NAME,
       "face_refs": {"type": "array", "minItems": 1, "uniqueItems": True,
                     "items": {"type": "string", "pattern": "^f[0-9]+$"}},
       "offset_mm": {"type": "number", "minimum": 0}, "reverse": {"type": "boolean"},
       "feature_name": _NAME},
      ["expected_document_path", "configuration", "face_refs", "offset_mm", "reverse", "feature_name"])
def offset_surface(args: dict[str, Any]) -> dict[str, Any]:
    doc, original, before, payload, error = None, None, None, None, None
    attempted, artifacts = False, []
    try:
        target = _name(args.get("configuration"), "configuration")
        name = _name(args.get("feature_name"), "feature_name")
        distance = _number(args.get("offset_mm"), "offset_mm")
        if distance < 0:
            raise ValueError("offset_mm must be nonnegative; use reverse to choose the native side.")
        reverse = _boolean(args.get("reverse"), "reverse")
        refs = args.get("face_refs")
        if (not isinstance(refs, list) or not refs or len(refs) != len(set(refs))
                or not all(isinstance(ref, str) and re.fullmatch(r"f[0-9]+", ref) for ref in refs)):
            raise ValueError("face_refs must be a nonempty unique list of face reference handles.")
        _, doc = require_document_path(args["expected_document_path"], 1)
        names, original = _configurations(doc)
        _unused_feature_name(doc, name)
        with _configuration(doc, target):
            for ref in refs:
                face = resolve_ref(doc, ref)
                if value(face, "GetSurface") is None or float(value(face, "GetArea")) <= 0:
                    raise ValueError(f"Ref '{ref}' is not a readable face.")
            _clear(doc)
            if apply_selection(doc, {"refs": refs}) != len(refs):
                raise RuntimeError("Could not select every requested face.")
            before = {str(value(feature, "Name")) for feature in _features(doc)}
            attempted = True
            try:
                flag_methods(doc, "InsertOffsetSurface").InsertOffsetSurface(to_m(distance), reverse)
            finally:
                artifacts = _new_names(doc, before)
            candidates = []
            for created_name in artifacts:
                feature = _find_feature(doc, created_name)
                if as_list(safe(feature, "GetFaces")):
                    candidates.append(feature)
            if len(candidates) != 1:
                raise RuntimeError(f"Expected one new geometric surface feature, found {len(candidates)}.")
            feature = candidates[0]
            if not _feature_bodies(feature, 1):
                raise RuntimeError("The created surface has no sheet body.")
            _rename(feature, name)
            artifacts = _new_names(doc, before)
            states = _isolate(feature, names, target)
            feature = _find_feature(doc, name)
            bodies = _feature_bodies(feature, 1)
            definition = safe(feature, "GetDefinition")
            measured, source = _native_length(feature, definition, "Distance", distance)
            unverified: list[str] = []
            actual_reverse = _native_setting(definition, "Flip", reverse, unverified)
            payload = {"feature": name, "configuration": target, "face_refs": refs,
                       "requested": {"offset_mm": distance, "reverse": reverse},
                       "offset_mm": measured, "reverse": actual_reverse,
                       "sheet_bodies": [_body_summary(body, 1) for body in bodies],
                       "suppressed": states, "verification": {"length_source": source,
                           "unverified_native_fields": unverified,
                           "physical_side_inferred": False}}
    except Exception as exc:
        error = exc
    return _finish(doc, original, attempted, artifacts, payload, error, before)


@tool("thicken_surface", "Thicken one existing single-sheet feature with explicit native side and merge behavior in a named configuration. Thickness maps to SOLIDWORKS's native Thickness setting, including for both sides. No offset creation, backside inference or save.",
      {"expected_document_path": _PATH, "configuration": _NAME,
       "surface_feature_name": _NAME, "thickness_mm": {"type": "number", "exclusiveMinimum": 0},
       "direction": {"type": "string", "enum": list(_DIRECTIONS)},
       "merge": {"type": "boolean"}, "feature_name": _NAME},
      ["expected_document_path", "configuration", "surface_feature_name", "thickness_mm", "direction", "merge", "feature_name"])
def thicken_surface(args: dict[str, Any]) -> dict[str, Any]:
    doc, original, before, payload, error = None, None, None, None, None
    attempted, artifacts = False, []
    try:
        target = _name(args.get("configuration"), "configuration")
        name = _name(args.get("feature_name"), "feature_name")
        source_name = _name(args.get("surface_feature_name"), "surface_feature_name")
        thickness = _number(args.get("thickness_mm"), "thickness_mm", True)
        direction = args.get("direction")
        if not isinstance(direction, str) or direction not in _DIRECTIONS:
            raise ValueError("direction must be side1, side2, or both.")
        merge = _boolean(args.get("merge"), "merge")
        _, doc = require_document_path(args["expected_document_path"], 1)
        names, original = _configurations(doc)
        _unused_feature_name(doc, name)
        with _configuration(doc, target):
            surface = _find_feature(doc, source_name)
            sources = _feature_bodies(surface, 1)
            if len(sources) != 1:
                raise ValueError(f"Source must contain exactly one sheet body; found {len(sources)}.")
            source_body_name = str(value(sources[0], "Name"))
            before_count = len(as_list(flag_methods(doc, "GetBodies2").GetBodies2(0, False)))
            _clear(doc)
            if not flag_methods(surface, "Select2").Select2(False, 1):
                raise RuntimeError("Could not select the surface feature with mark 1.")
            before = {str(value(feature, "Name")) for feature in _features(doc)}
            attempted = True
            try:
                feature = flag_methods(value(doc, "FeatureManager"), "FeatureBossThicken").FeatureBossThicken(
                    to_m(thickness), _DIRECTIONS[direction], 0, False, merge, False, True)
            finally:
                artifacts = _new_names(doc, before)
            if feature is None:
                raise RuntimeError("SOLIDWORKS did not return a thicken feature.")
            _rename(feature, name)
            artifacts = _new_names(doc, before)
            states = _isolate(feature, names, target)
            feature = _find_feature(doc, name)
            bodies = _feature_bodies(feature, 0)
            if not bodies:
                raise RuntimeError("The thicken feature has no resulting solid body.")
            summaries = [_body_summary(body, 0) for body in bodies]
            after_count = len(as_list(flag_methods(doc, "GetBodies2").GetBodies2(0, False)))
            if not merge and after_count != before_count + len(bodies):
                raise RuntimeError("Separate-body thickening did not preserve the expected solid-body count.")
            definition = safe(feature, "GetDefinition")
            measured, length_source = _native_length(feature, definition, "Thickness", thickness)
            unverified: list[str] = []
            actual_side = _native_setting(definition, "ThicknessSide", _DIRECTIONS[direction], unverified)
            actual_merge = _native_setting(definition, "Merge", merge, unverified)
            payload = {"feature": name, "configuration": target, "source_feature": source_name,
                       "requested": {"thickness_mm": thickness, "direction": direction, "merge": merge},
                       "source_body": source_body_name, "thickness_mm": measured,
                       "direction": direction if actual_side is not None else None, "merge": actual_merge,
                       "solid_bodies": summaries, "solid_count_before": before_count,
                       "solid_count_after": after_count, "suppressed": states,
                       "verification": {"length_source": length_source,
                           "unverified_native_fields": unverified,
                           "physical_side_inferred": False}}
    except Exception as exc:
        error = exc
    return _finish(doc, original, attempted, artifacts, payload, error, before)
