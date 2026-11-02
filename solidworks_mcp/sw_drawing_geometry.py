"""View-scoped geometry and measured reference dimensions for drawing workflows."""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from .sw_core import (
    as_list, clear_selection, dimension_dialog_suppressed, double_array,
    extension, flag_methods, nothing, open_document_by_path, rebuild,
    require_document_path, result, safe, to_m, to_mm, tool, value, document_path_key,
)
from .sw_drawing import _iter_views, _view_entry, require_drawing


EXPECTED = {"type": "string", "description": "Exact document_key from get_active_document_info, a path or untitled:<exact title>. No implicit switch."}
_DIMENSION_METHODS = {
    "horizontal": "AddHorizontalDimension2", "vertical": "AddVerticalDimension2",
    "diameter": "AddDiameterDimension2", "radius": "AddRadialDimension2",
}


def finite(value_: Any, label: str, positive: bool = False) -> float:
    n = float(value_)
    if not math.isfinite(n) or (positive and n <= 0):
        raise RuntimeError(f"{label} must be {'positive and ' if positive else ''}finite.")
    return n


def point(values: Any, count: int, label: str) -> list[float]:
    if not isinstance(values, (list, tuple)) or len(values) != count:
        raise RuntimeError(f"{label} must contain {count} numbers.")
    return [finite(v, label) for v in values]


def drawing(args: dict[str, Any]) -> tuple[Any, Any]:
    app, doc = require_document_path(str(args["expected_document_path"]), 3)
    return app, flag_methods(doc, "ActivateView", "GetFirstView", "GetCurrentSheet")


def find_view(doc: Any, name: str) -> Any:
    matches = [v for v in _iter_views(doc) if str(safe(v, "GetName2", safe(v, "Name", ""))) == name]
    if len(matches) != 1 or not str(safe(matches[0], "GetReferencedModelName", "")):
        raise RuntimeError(f"Expected one model view named '{name}', found {len(matches)}.")
    return flag_methods(matches[0], "GetVisibleEntities2", "SelectEntity")


def model_to_sheet(p_mm: list[float], transform: list[float]) -> list[float]:
    if len(transform) < 13:
        raise RuntimeError("Missing drawing ModelToViewTransform.")
    p = [to_m(v) for v in p_mm]
    return [to_mm(sum(p[j] * transform[j * 3 + i] for j in range(3)) * transform[12] + transform[9+i])
            for i in range(3)]


def _xyz(obj: Any) -> list[float]:
    return [round(to_mm(v), 6) for v in value(obj, "GetPoint")]


def _edge_selector(edge: Any, component: str) -> dict[str, Any] | None:
    curve = value(edge, "GetCurve")
    a, b = safe(edge, "GetStartVertex"), safe(edge, "GetEndVertex")
    endpoints = sorted([_xyz(a), _xyz(b)]) if a is not None and b is not None else None
    selector: dict[str, Any] = {"component": component}
    if bool(safe(curve, "IsLine", False)) and endpoints:
        selector.update(kind="line", start_mm=endpoints[0], end_mm=endpoints[1])
    elif bool(safe(curve, "IsCircle", False)):
        params = list(value(curve, "CircleParams"))
        selector.update(kind="circle", center_mm=[round(to_mm(v), 6) for v in params[:3]],
                        axis=[round(float(v), 8) for v in params[3:6]], radius_mm=round(to_mm(params[6]), 6))
        if endpoints:
            selector.update(start_mm=endpoints[0], end_mm=endpoints[1])
    else:
        return None
    return selector


def catalog(view: Any) -> list[tuple[dict[str, Any], Any]]:
    rows = []
    for component in as_list(value(view, "GetVisibleComponents")):
        name = str(safe(component, "Name2", ""))
        for edge in as_list(view.GetVisibleEntities2(component, 1)):
            desc = _edge_selector(edge, name)
            if desc is not None:
                rows.append((desc, edge))
    return rows


def _near(a: Any, b: Any, tolerance: float) -> bool:
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_near(x, y, tolerance) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a)-float(b)) <= tolerance
    return a == b


def match_selector(actual: dict[str, Any], wanted: dict[str, Any], tolerance: float) -> bool:
    if wanted.get("kind") not in {"line", "circle"}:
        raise RuntimeError("Geometry selector kind must be line or circle.")
    required = {"kind", "start_mm", "end_mm"} if wanted["kind"] == "line" else {"kind", "center_mm", "radius_mm"}
    if not required.issubset(wanted):
        raise RuntimeError(f"Incomplete {wanted['kind']} selector: missing {sorted(required-set(wanted))}.")
    return all(k in actual and _near(actual[k], v, tolerance) for k, v in wanted.items() if k != "endpoint_mm")


def resolve_entity(rows: list[tuple[dict[str, Any], Any]], selector: dict[str, Any], tolerance: float) -> Any:
    candidates = [(s, e) for s, e in rows if match_selector(s, selector, tolerance)]
    if len(candidates) != 1:
        raise RuntimeError(f"Geometry selector matched {len(candidates)} visible edges; expected exactly one.")
    _, edge = candidates[0]
    if "endpoint_mm" in selector:
        wanted = point(selector["endpoint_mm"], 3, "endpoint_mm")
        vertices = [v for v in [safe(edge, "GetStartVertex"), safe(edge, "GetEndVertex")] if v is not None]
        matches = [v for v in vertices if _near(_xyz(v), wanted, tolerance)]
        if len(matches) != 1:
            raise RuntimeError("Endpoint is missing or ambiguous on the selected edge.")
        return matches[0]
    return edge


def visible_bounds(view: Any, rows: list[tuple[dict[str, Any], Any]]) -> list[float]:
    tr = list(value(value(view, "ModelToViewTransform"), "ArrayData"))
    pts = []
    for desc, edge in rows:
        curve = value(edge, "GetCurve")
        interval = safe(edge, "GetCurveParams2")
        if desc["kind"] == "circle" and interval is not None and len(interval) >= 8:
            curve = flag_methods(curve, "Evaluate2")
            lo, hi = float(interval[6]), float(interval[7])
            for i in range(129):
                p = curve.Evaluate2(lo+(hi-lo)*i/128, 0)
                pts.append(model_to_sheet([to_mm(v) for v in p[:3]], tr))
        else:
            for key in ("start_mm", "end_mm"):
                if key in desc:
                    pts.append(model_to_sheet(desc[key], tr))
    if not pts:
        raise RuntimeError("No visible line/arc geometry bounds are available.")
    return [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)]


@tool("list_drawing_entities", "Read-only: visible line/circle selectors in model millimetres for one drawing view. Selectors survive enumeration changes; include view/configuration context when reusing them.",
      {"expected_document_path": EXPECTED, "view_name": {"type": "string"}},
      ["expected_document_path", "view_name"])
def list_drawing_entities(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = drawing(args)
    view = find_view(doc, str(args["view_name"]))
    rows = catalog(view)
    return result(True, f"Read {len(rows)} visible edges.", view_name=args["view_name"],
                  configuration=str(value(view, "ReferencedConfiguration")),
                  model_path=str(value(view, "GetReferencedModelName")),
                  entities=[s for s, _ in rows], visible_bounds_mm=visible_bounds(view, rows),
                  bounds_tolerance_mm=0.1)


def dimensions(view: Any) -> list[Any]:
    found = []
    d = safe(view, "GetFirstDisplayDimension")
    while d is not None:
        found.append(flag_methods(d, "GetDimension2", "GetText", "SetText", "SetPrecision3"))
        d = safe(d, "GetNext")
    return found


def _delete_dimension(doc: Any, dim: Any) -> bool:
    name = str(value(flag_methods(dim, "GetDimension2").GetDimension2(0), "FullName"))
    ext = extension(doc)
    clear_selection(doc)
    return bool(ext.SelectByID2(name, "DIMENSION", 0, 0, 0, False, 0, nothing(), 0) and ext.DeleteSelection2(0))


@tool("add_drawing_dimension", "Add one associative reference dimension to visible geometry in a named view. Refuse ambiguous selectors, stale configuration, duplicate names, or a measured value that differs from expected_value_mm. Never change the part dimensions.",
      {"expected_document_path": EXPECTED, "view_name": {"type": "string"},
       "configuration": {"type": "string"}, "name": {"type": "string"},
       "kind": {"type": "string", "enum": list(_DIMENSION_METHODS)},
       "entities": {"type": "array", "minItems": 1, "maxItems": 2, "items": {"type": "object"}},
       "at_mm": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number"}},
       "expected_value_mm": {"type": "number"}, "tolerance_mm": {"type": "number", "default": 0.00001},
       "prefix": {"type": "string", "default": ""}, "precision": {"type": "integer", "minimum": 0, "maximum": 8}},
      ["expected_document_path", "view_name", "configuration", "name", "kind", "entities", "at_mm", "expected_value_mm"])
def add_drawing_dimension(args: dict[str, Any]) -> dict[str, Any]:
    app, doc = drawing(args)
    view = find_view(doc, str(args["view_name"]))
    config = str(value(view, "ReferencedConfiguration"))
    if config != str(args["configuration"]):
        raise RuntimeError(f"View configuration changed: expected '{args['configuration']}', got '{config}'.")
    name = str(args["name"])
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
        raise RuntimeError("Dimension name must be 1-64 ASCII letters, digits, or underscores, beginning with a letter.")
    for existing in dimensions(view):
        if str(safe(existing.GetDimension2(0), "Name", "")).split('@')[0] == name:
            return result(False, f"Dimension '{name}' already exists. Inspect or update it explicitly; no duplicate created.", conflict=True)
    expected = finite(args["expected_value_mm"], "expected_value_mm", True)
    tolerance = finite(args.get("tolerance_mm", .00001), "tolerance_mm", True)
    at = point(args["at_mm"], 2, "at_mm")
    selectors = args["entities"]
    if not isinstance(selectors, list) or not 1 <= len(selectors) <= 2:
        raise RuntimeError("Select one or two entities.")
    kind = str(args["kind"])
    if kind not in _DIMENSION_METHODS:
        raise RuntimeError("Unknown dimension kind.")
    precision = args.get("precision")
    if precision is not None and (isinstance(precision, bool) or int(precision) != precision or not 0 <= int(precision) <= 8):
        raise RuntimeError("precision must be an integer from 0 to 8.")
    rows = catalog(view)
    resolved = [resolve_entity(rows, s, tolerance) for s in selectors]
    method = _DIMENSION_METHODS[kind]
    flag_methods(doc, method)
    clear_selection(doc)
    try:
        for i, entity in enumerate(resolved):
            if not view.SelectEntity(entity, i > 0):
                raise RuntimeError("Could not select the dimension geometry in its drawing view.")
        with dimension_dialog_suppressed(app):
            display = getattr(doc, method)(to_m(at[0]), to_m(at[1]), 0.0)
        if display is None:
            raise RuntimeError("SolidWorks did not create the drawing dimension.")
        flag_methods(display, "GetDimension2", "GetText", "SetText", "SetPrecision3")
        param = display.GetDimension2(0)
        measured = to_mm(value(param, "SystemValue"))
        if abs(measured-expected) > tolerance:
            removed = _delete_dimension(doc, display)
            return result(False, f"Measured {measured:g} mm, expected {expected:g} mm.",
                          measured_mm=measured, removed=removed,
                          created_dimension=str(safe(param, "FullName", "")))
        param.Name = name
        symbol = str(display.GetText(1) or "")
        display.SetText(1, str(args.get("prefix", "")) + symbol)
        if precision is not None and display.SetPrecision3(int(precision), int(precision), int(precision), int(precision)) != 0:
            raise RuntimeError("Dimension created, but precision could not be set.")
        rebuild(doc)
        if str(value(param, "Name")).split('@')[0] != name:
            raise RuntimeError("Dimension created, but its requested name did not persist.")
        return result(True, "Created measured drawing dimension.", dimension=str(value(param, "FullName")),
                      measured_mm=to_mm(value(param, "SystemValue")), prefix=display.GetText(1))
    finally:
        clear_selection(doc)


@tool("set_drawing_sheet", "Set the sheet scale and optionally reuse an existing sheet-format file. Does not copy title metadata, save, or resize drawing text. Set each view scale explicitly with set_drawing_view.",
      {"expected_document_path": EXPECTED, "scale_numerator": {"type": "number"},
       "scale_denominator": {"type": "number", "default": 1},
       "template_path": {"type": "string"},
       "replace_format": {"type": "boolean", "default": False,
                          "description": "Explicitly authorize replacing a different sheet format and its existing format notes. Same-format calls never reload."}},
      ["expected_document_path", "scale_numerator"])
def set_drawing_sheet(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = drawing(args)
    num = finite(args["scale_numerator"], "scale_numerator", True)
    den = finite(args.get("scale_denominator", 1), "scale_denominator", True)
    template = args.get("template_path")
    if template and (Path(str(template)).suffix.lower() != '.slddrt' or not Path(str(template)).is_file()):
        raise RuntimeError("template_path must be an existing .slddrt file.")
    sheet = flag_methods(value(doc, "GetCurrentSheet"), "SetProperties", "SetTemplateName", "ReloadTemplate", "SetScale")
    current_template = str(safe(sheet, "GetTemplateName", ""))
    replacing = bool(template and document_path_key(str(template)) != document_path_key(current_template))
    if replacing and not bool(args.get("replace_format", False)):
        return result(False, "A different sheet format would replace its existing notes. Pass replace_format=true only when authorized.",
                      changed=False, current_template=current_template, requested_template=str(template))
    if replacing:
        p = list(value(sheet, "GetProperties"))
        sheet.SetProperties(int(p[0]), 12, num, den, bool(p[4]), float(p[5]), float(p[6]))
        sheet.SetTemplateName(str(template))
        status = int(sheet.ReloadTemplate(False))
        if status != 0:
            return result(False, "Sheet format reload failed.", reload_status=status, partial=True)
    if not sheet.SetScale(num, den, False, False):
        raise RuntimeError("SolidWorks did not set the sheet scale.")
    rebuild(doc)
    actual = list(value(sheet, "GetProperties"))
    ok = abs(actual[2]/actual[3]-num/den) < 1e-9
    return result(ok, "Updated drawing sheet." if ok else "Sheet scale readback differs.",
                  scale=[actual[2], actual[3]], template=str(safe(sheet, "GetTemplateName", "")))


def box_in_region(box: list[float], region: list[float], clearance: float = 0) -> bool:
    return (box[0] >= region[0]+clearance and box[1] >= region[1]+clearance
            and box[2] <= region[2]-clearance and box[3] <= region[3]-clearance)


@tool("place_drawing_view", "Place a view by the center of its visible geometry, avoiding hidden-body GetOutline bounds. Refuse an oversized or alignment-constrained placement. Text/leader collision checks still require final visual review.",
      {"expected_document_path": EXPECTED, "view_name": {"type": "string"},
       "center_mm": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number"}},
       "region_mm": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "number"}},
       "clearance_mm": {"type": "number", "default": 3}},
      ["expected_document_path", "view_name", "center_mm", "region_mm"])
def place_drawing_view(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = drawing(args)
    view = find_view(doc, str(args["view_name"]))
    center = point(args["center_mm"], 2, "center_mm")
    region = point(args["region_mm"], 4, "region_mm")
    if region[2] <= region[0] or region[3] <= region[1]:
        raise RuntimeError("region_mm is [xmin,ymin,xmax,ymax] with positive extents.")
    clearance = finite(args.get("clearance_mm", 3), "clearance_mm")
    if clearance < 0:
        raise RuntimeError("clearance_mm cannot be negative.")
    before = visible_bounds(view, catalog(view))
    delta = [center[i]-(before[i]+before[i+2])/2 for i in range(2)]
    wanted = [before[i]+delta[i%2] for i in range(4)]
    if not box_in_region(wanted, region, clearance):
        return result(False, "Requested scale/placement does not fit its reserved region; nothing moved.",
                      wanted_bounds_mm=wanted, region_mm=region)
    pos = list(value(view, "Position"))
    view.Position = double_array([pos[i]+to_m(delta[i]) for i in range(2)])
    rebuild(doc)
    after = visible_bounds(view, catalog(view))
    achieved = [(after[i]+after[i+2])/2 for i in range(2)]
    ok = all(abs(achieved[i]-center[i]) <= .1 for i in range(2)) and box_in_region(after, region, max(0, clearance-.1))
    return result(ok, "Placed drawing view." if ok else "View alignment prevented the requested placement.",
                  before_mm=before, after_mm=after, actual_center_mm=achieved, visual_review_required=True)
