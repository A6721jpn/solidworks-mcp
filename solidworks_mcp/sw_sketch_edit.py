"""Scoped sketch inspection and native edits without changing editing context."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Callable

import pythoncom

from .sw_core import (as_list, document_path_key, empty_variant, flag_methods, rebuild,
                      require_document_path, result, safe, tool, value)
from .sw_sketch import RELATION_NAMES, RELATIONS

_NAME = {"type": "string", "minLength": 1}
_BASE = {"expected_document_path": _NAME}
_SKETCH = {**_BASE, "sketch_name": _NAME}
_SCOPE = {"type": "string", "enum": ["active", "all"], "default": "active"}


def _name(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{label} must be a nonempty exact name.")
    return raw


def _doc(args: dict[str, Any]) -> tuple[Any, Any]:
    return require_document_path(_name(args.get("expected_document_path"), "expected_document_path"))


def _configuration(doc: Any) -> str:
    return str(value(doc.ConfigurationManager.ActiveConfiguration, "Name"))


def _sketch(doc: Any, raw: Any) -> tuple[str, Any, Any]:
    name = _name(raw, "sketch_name")
    feature = flag_methods(doc, "FeatureByName").FeatureByName(name)
    if feature is None or value(feature, "GetTypeName2") not in ("ProfileFeature", "3DProfileFeature"):
        raise ValueError(f"No sketch feature named '{name}'.")
    return name, feature, value(feature, "GetSpecificFeature2")


def _context(doc: Any, sketch: str = "") -> dict[str, Any]:
    path = str(value(doc, "GetPathName") or "")
    return {"document_key": document_path_key(path) if path else "untitled:" + str(value(doc, "GetTitle")),
            "configuration": _configuration(doc), "sketch": sketch}


def _entity_identity(entity: Any, app: Any, sketch: Any, name: str) -> dict[str, Any]:
    if entity is None:
        raise ValueError("Relation includes a null definition entity.")
    ids = as_list(value(entity, "GetID"))
    if len(ids) != 2 or any(isinstance(n, bool) or not isinstance(n, int) for n in ids):
        raise ValueError("Relation entity has no supported native ID pair.")
    kind = safe(entity, "GetType")
    if isinstance(kind, int) and not isinstance(kind, bool):
        entity_type = f"segment:{kind}"
    elif all(safe(entity, axis) is not None for axis in ("X", "Y", "Z")):
        entity_type = "point"
    else:
        raise ValueError("Relation entity has no supported sketch entity type.")
    owner = value(entity, "GetSketch")
    if int(flag_methods(app, "IsSame").IsSame(owner, sketch)) != 1:
        raise ValueError("External sketch entities need persistent identities and are unsupported.")
    return {"type": entity_type, "id": ids, "sketch": name}


def _relations(app: Any, doc: Any, sketch: Any, name: str,
               kinds: set[int] | None = None) -> list[tuple[dict[str, Any], Any]]:
    manager = value(sketch, "RelationManager")
    native = as_list(flag_methods(manager, "GetRelations").GetRelations(0))
    context = _context(doc, name)
    rows = []
    for relation in native:
        kind = int(value(relation, "GetRelationType"))
        if kinds is not None and kind not in kinds:
            continue
        source = "GetDefinitionEntities"
        try:
            entities = value(relation, source)
        except Exception:
            entities = None
        if not as_list(entities):
            source = "GetEntities"
            entities = value(relation, source)
        row = {"type": kind, "type_name": RELATION_NAMES.get(kind, str(kind)), "entities_source": source}
        try:
            identities = [_entity_identity(e, app, sketch, name) for e in as_list(entities)]
            if not identities:
                raise ValueError("Relation has no definition entities.")
            row["entities"] = sorted(identities, key=lambda e: json.dumps(e, sort_keys=True))
            signature = {**context, "type": kind, "entities": row["entities"], "entities_source": source}
            row["relation_ref"] = f"rel:{kind}:" + hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        except Exception as exc:
            row.update(relation_ref=None, unsupported_identity=str(exc))
        rows.append((row, relation))
    counts: dict[str, int] = {}
    for row, _ in rows:
        ref = row["relation_ref"]
        if ref:
            counts[ref] = counts.get(ref, 0) + 1
    for row, _ in rows:
        if counts.get(row["relation_ref"], 0) > 1:
            row.update(unsupported_identity="Duplicate native relation signature.", relation_ref=None)
    return rows


def _indexed(manager: Any, member: str, index: int, text: str | None = None) -> Any:
    if hasattr(manager, "_oleobj_"):
        ole = manager._oleobj_
        code = ole.GetIDsOfNames(member)
        if text is not None:
            return ole.Invoke(code, 0, pythoncom.DISPATCH_PROPERTYPUT, False, index, text)
        return ole.Invoke(code, 0, pythoncom.DISPATCH_PROPERTYGET, True, index)
    if text is not None:
        return getattr(manager, "Set" + member)(index, text)
    return getattr(manager, member)(index)


def _equations(doc: Any) -> tuple[Any, list[dict[str, Any]]]:
    manager = value(doc, "GetEquationMgr")
    if manager is None:
        raise RuntimeError("Equation manager is unavailable.")
    count = int(value(manager, "GetCount"))
    rows = [{"index": i, "equation": str(_indexed(manager, "Equation", i))} for i in range(count)]
    return manager, rows


def _lhs(equation: str) -> str:
    match = re.match(r'^\s*"([^"\r\n]+)"\s*=([^\r\n]*)$', equation)
    if not match:
        raise ValueError('Equation must have one quoted target followed by = and an expression.')
    return match[1]


def _dimension(doc: Any, name: str) -> tuple[Any, dict[str, Any]]:
    dim = flag_methods(doc, "Parameter").Parameter(name)
    if dim is None:
        raise ValueError(f"No dimension named '{name}'.")
    kind = int(value(dim, "GetType"))
    driven = int(value(dim, "DrivenState"))
    return dim, {"full_name": "@".join(str(value(dim, "FullName")).split("@")[:2]),
                 "kind": {0: "linear", 1: "angular"}.get(kind, "unsupported"),
                 "driven": driven == 1, "driven_state": driven,
                 "system_value": float(value(dim, "SystemValue"))}


def _dimensions(doc: Any, feature: Any) -> list[dict[str, Any]]:
    rows = []
    seen = set()
    display = value(feature, "GetFirstDisplayDimension")
    while display is not None:
        dim = flag_methods(display, "GetDimension2").GetDimension2(0)
        name = "@".join(str(value(dim, "FullName")).split("@")[:2])
        if name in seen:
            raise RuntimeError("Repeated dimension during scoped inspection; enumeration is incomplete.")
        seen.add(name)
        _, row = _dimension(doc, name)
        if row["kind"] == "angular":
            row["display_value"] = f'{math.degrees(row["system_value"]):.3f}deg'
        else:
            row["display_value"] = row["system_value"] * 1000
        rows.append(row)
        display = flag_methods(feature, "GetNextDisplayDimension").GetNextDisplayDimension(display)
    return rows


def _finish(doc: Any, items: list[dict[str, Any]], error: str = "", *, attempted: bool = False,
            scope: str = "active", sketch: str = "", verify: Callable[[], None] | None = None) -> dict[str, Any]:
    rebuilt = {"attempted": False, "ok": None}
    if attempted and doc.SketchManager.ActiveSketch is None:
        rebuilt = {"attempted": True, "ok": rebuild(doc)}
        if not rebuilt["ok"]:
            error = error or "Rebuild reported a problem."
    if verify is not None:
        try:
            verify()
        except Exception as exc:
            error = error or str(exc)
    changed = any(i.get("applied") or i.get("may_have_changes") for i in items)
    try:
        context = {**_context(doc, sketch), **_editing_state(doc)}
    except Exception as exc:
        context = {"context_readback_error": str(exc)}
        error = error or "Editing context could not be verified after the operation."
    return result(not error, error or "Scoped operation completed.", partial=bool(error and changed),
                  may_have_changes=bool(error and attempted), applied=[i["target"] for i in items if i.get("applied")],
                  items=items, rebuild=rebuilt, rollback_performed=False,
                  diff={"items": items}, diff_scope={**context, "configuration_scope": scope,
                                                    "downstream_geometry_checked": False})


def _editing_state(doc: Any) -> dict[str, Any]:
    active = doc.SketchManager.ActiveSketch
    return {"sketch_open": active is not None, "dirty": bool(value(doc, "GetSaveFlag")),
            "active_sketch_status": int(value(active, "GetConstrainedStatus")) if active is not None else None}


def _reject(exc: Exception) -> dict[str, Any]:
    return result(False, str(exc), partial=False, applied=[], items=[], may_have_changes=False,
                  rollback_performed=False)


@tool("inspect_sketch", "Read one named sketch without tree traversal or changing editing context.",
      {**_SKETCH, "include_segments": {"type": "boolean", "default": False},
       "include_relations": {"type": "boolean", "default": True},
       "relation_types": {"type": "array", "items": {"type": ["integer", "string"]}},
       "include_dimensions": {"type": "boolean", "default": True},
       "include_equations": {"type": "boolean", "default": True}}, list(_SKETCH))
def inspect_sketch(args: dict[str, Any]) -> dict[str, Any]:
    try:
        app, doc = _doc(args)
        name, feature, sketch = _sketch(doc, args.get("sketch_name"))
        data = {**_context(doc, name), **_editing_state(doc),
                "native_sketch_status": int(value(sketch, "GetConstrainedStatus"))}
        if args.get("include_segments", False):
            data["segments"] = [_entity_identity(e, app, sketch, name) for e in as_list(value(sketch, "GetSketchSegments"))]
        if args.get("include_relations", True):
            kinds = args.get("relation_types")
            selected = None if kinds is None else {RELATIONS[k] if isinstance(k, str) else int(k) for k in kinds}
            data["relations"] = [row for row, _ in _relations(app, doc, sketch, name, selected)]
        if args.get("include_dimensions", True):
            data["dimensions"] = _dimensions(doc, feature)
        if args.get("include_equations", True):
            _, equations = _equations(doc)
            dependencies = {row["full_name"] for row in data.get("dimensions", [])}
            relevant: dict[int, dict[str, Any]] = {}
            for row in equations:
                tokens = set(re.findall(r'"([^"\r\n]+)"', row["equation"]))
                if tokens & dependencies or any(t.split("@")[1:2] == [name] for t in tokens):
                    relevant[row["index"]] = row
                    dependencies.update(tokens)
            while True:
                previous = len(relevant)
                for row in equations:
                    tokens = re.findall(r'"([^"\r\n]+)"', row["equation"])
                    if tokens and tokens[0] in dependencies:
                        relevant[row["index"]] = row
                        dependencies.update(tokens)
                if len(relevant) == previous:
                    break
            data["equations"] = list(relevant.values())
        return result(True, "Read scoped sketch state.", **data)
    except Exception as exc:
        return _reject(exc)


@tool("delete_sketch_relations", "Delete exact inspected relation signatures from the already open named sketch.",
      {**_SKETCH, "relation_refs": {"type": "array", "minItems": 1, "items": _NAME}},
      [*_SKETCH, "relation_refs"])
def delete_sketch_relations(args: dict[str, Any]) -> dict[str, Any]:
    items = []
    attempted = False
    try:
        refs = args.get("relation_refs")
        if not isinstance(refs, list) or not refs or any(not isinstance(r, str) for r in refs) or len(set(refs)) != len(refs):
            raise ValueError("relation_refs must be a nonempty array of distinct inspected references.")
        matches = [re.fullmatch(r"rel:(\d+):[a-f0-9]{64}", ref) for ref in refs]
        if not all(matches):
            raise ValueError("Unsupported or stale relation reference. Inspect again.")
        kinds = {int(match[1]) for match in matches}
        app, doc = _doc(args)
        name, feature, sketch = _sketch(doc, args.get("sketch_name"))
        active = doc.SketchManager.ActiveSketch
        if active is None or int(flag_methods(app, "IsSame").IsSame(active, sketch)) != 1:
            raise ValueError("The named sketch must already be open for editing.")
        before = {r["relation_ref"]: r for r, _ in _relations(app, doc, sketch, name, kinds) if r["relation_ref"]}
        if any(ref not in before for ref in refs):
            raise ValueError("Missing, stale, ambiguous, or unsupported relation reference. Inspect again.")
        manager = flag_methods(value(sketch, "RelationManager"), "DeleteRelation")
        for ref in refs:
            current = {r["relation_ref"]: (r, native) for r, native in _relations(app, doc, sketch, name, kinds) if r["relation_ref"]}
            if ref not in current or current[ref][0] != before[ref]:
                raise RuntimeError("Relation signature changed before deletion. Inspect again.")
            item = {"target": ref, "before": before[ref], "applied": False, "may_have_changes": True}
            items.append(item)
            attempted = True
            item["native_status"] = bool(manager.DeleteRelation(current[ref][1]))
            remaining = {r["relation_ref"] for r, _ in _relations(app, doc, sketch, name, kinds)}
            item.update(after=None if ref not in remaining else before[ref], applied=ref not in remaining)
            if not item["native_status"] or not item["applied"]:
                raise RuntimeError("Native relation deletion failed; remaining deletions were skipped.")
        return _finish(doc, items, attempted=attempted, sketch=name)
    except Exception as exc:
        return _finish(doc, items, str(exc), attempted=attempted, sketch=name) if attempted else _reject(exc)


@tool("rename_dimension", "Rename one dimension and verify its new full name without leaving editing mode.",
      {**_BASE, "full_name": _NAME, "new_name": _NAME}, [*_BASE, "full_name", "new_name"])
def rename_dimension(args: dict[str, Any]) -> dict[str, Any]:
    items = []
    attempted = False
    try:
        old = _name(args.get("full_name"), "full_name")
        new = _name(args.get("new_name"), "new_name")
        if any(c in new for c in '@"=\r\n'):
            raise ValueError("new_name must be a simple dimension name.")
        _, doc = _doc(args)
        dim, before = _dimension(doc, old)
        if before["full_name"] != old:
            raise ValueError("full_name must be the exact dimension@feature name.")
        target = new + "@" + old.split("@", 1)[1]
        if target != old and doc.Parameter(target) is not None:
            raise ValueError("The requested dimension name already exists.")
        item = {"target": old, "before": before, "applied": False, "may_have_changes": True}
        items.append(item)
        attempted = True
        dim.Name = new
        _, after = _dimension(doc, target)
        item.update(after=after, applied=after["full_name"] == target, native_status="name_readback")
        if not item["applied"]:
            raise RuntimeError("Dimension rename failed verification.")
        return _finish(doc, items, attempted=attempted)
    except Exception as exc:
        return _finish(doc, items, str(exc), attempted=attempted) if attempted else _reject(exc)


@tool("set_dimensions", "Prevalidate typed dimension changes, apply native scoped values, rebuild once outside sketches. "
      "Verify active values and explicitly list inactive configurations as unverified.",
      {**_BASE, "configuration_scope": _SCOPE,
       "updates": {"type": "array", "minItems": 1, "items": {"type": "object", "properties":
           {"full_name": _NAME, "value_mm": {"type": "number"}, "value_deg": {"type": "number"}}, "required": ["full_name"]}}},
      [*_BASE, "updates"])
def set_dimensions(args: dict[str, Any]) -> dict[str, Any]:
    items = []
    attempted = False
    try:
        updates = args.get("updates")
        scope = args.get("configuration_scope", "active")
        if scope not in ("active", "all") or not isinstance(updates, list) or not updates:
            raise ValueError("Pass updates and configuration_scope active or all.")
        requests = []
        names = set()
        for update in updates:
            name = _name(update.get("full_name"), "full_name")
            units = [key for key in ("value_mm", "value_deg") if key in update]
            if len(units) != 1 or isinstance(update[units[0]], bool) or not isinstance(update[units[0]], (float, int)) or not math.isfinite(update[units[0]]):
                raise ValueError("Each update needs exactly one finite value_mm or value_deg.")
            if name in names:
                raise ValueError("Duplicate dimension target.")
            names.add(name)
            unit = units[0]
            requests.append((name, unit, math.radians(update[unit]) if unit == "value_deg" else update[unit] / 1000))
        _, doc = _doc(args)
        _, equations = _equations(doc)
        controlled = {"@".join(_lhs(row["equation"]).split("@")[:2]) for row in equations}
        requested_configurations = as_list(value(doc, "GetConfigurationNames")) if scope == "all" else [_configuration(doc)]
        if not requested_configurations:
            raise ValueError("Could not read dimension configuration scope.")
        configurations = [_configuration(doc)]
        pending = [name for name in requested_configurations if name not in configurations]
        prepared = []
        for name, unit, target in requests:
            dim, before = _dimension(doc, name)
            if name != before["full_name"] or before["driven_state"] != 2 or name in controlled:
                raise ValueError(f"{name} must be an exact driving dimension without an equation.")
            if before["kind"] != {"value_mm": "linear", "value_deg": "angular"}[unit]:
                raise ValueError(f"Unit does not match native dimension kind for {name}.")
            flag_methods(dim, "SetSystemValue3", "GetSystemValue3")
            before["system_values"] = _active_dimension_values(dim)
            before["configurations"] = configurations
            before["requested_configurations"] = requested_configurations
            prepared.append((dim, name, before, target))
        def verify() -> None:
            failures = []
            for (_, name, _, target), item in zip(prepared, items):
                try:
                    dim, _ = _dimension(doc, name)
                    flag_methods(dim, "GetSystemValue3")
                    actual = _active_dimension_values(dim)
                    item.update(after={"system_values": actual, "verified_configurations": configurations,
                                       "unverified_configurations": pending},
                                applied=all(abs(v-target) <= 1e-9 for v in actual))
                    if not item["applied"]:
                        failures.append(name)
                except Exception as exc:
                    item.update(applied=False, readback_error=str(exc))
                    failures.append(name)
            if failures:
                raise RuntimeError("Dimension value failed final verification: " + ", ".join(failures))
        for dim, name, before, target in prepared:
            item = {"target": name, "before": before, "applied": False, "may_have_changes": True}
            items.append(item)
            attempted = True
            item["native_status"] = int(dim.SetSystemValue3(target, 1 if scope == "active" else 2, empty_variant()))
            actual = _active_dimension_values(dim)
            item.update(after={"system_values": actual, "verified_configurations": configurations,
                               "unverified_configurations": pending},
                        applied=all(abs(v-target) <= 1e-9 for v in actual))
            if item["native_status"] != 0 or not item["applied"]:
                raise RuntimeError(f"Native value change failed for {name}; remaining changes were skipped.")
        return _finish(doc, items, attempted=attempted, scope=scope, verify=verify)
    except Exception as exc:
        return _finish(doc, items, str(exc), attempted=attempted, scope=scope, verify=verify) if attempted else _reject(exc)


def _active_dimension_values(dim: Any) -> list[float]:
    # Inactive sketch dimensions can return zero from the native all-configurations getter.
    numbers = [float(v) for v in as_list(dim.GetSystemValue3(1, empty_variant()))]
    if len(numbers) != 1 or not all(math.isfinite(v) for v in numbers):
        raise RuntimeError("Native active-configuration dimension readback is unavailable.")
    return numbers


@tool("set_equation", "Add or compare-and-set one equation by quoted left-hand target; never save or switch configuration.",
      {**_BASE, "equation": _NAME, "expected_equation": {"type": "string"}, "configuration_scope": _SCOPE},
      [*_BASE, "equation"])
def set_equation(args: dict[str, Any]) -> dict[str, Any]:
    items = []
    attempted = False
    try:
        equation = _name(args.get("equation"), "equation")
        lhs = _lhs(equation)
        expected = args.get("expected_equation")
        if expected is not None and (not isinstance(expected, str) or _lhs(expected) != lhs):
            raise ValueError("expected_equation must address the same quoted target.")
        scope = args.get("configuration_scope", "active")
        if scope not in ("active", "all"):
            raise ValueError("configuration_scope must be active or all.")
        _, doc = _doc(args)
        manager, rows = _equations(doc)
        matches = [row for row in rows if _lhs(row["equation"]) == lhs]
        if len(matches) > 1:
            raise ValueError("Equation target is ambiguous.")
        before = matches[0] if matches else None
        if before and expected is None:
            raise ValueError("Updating an equation requires exact expected_equation text.")
        if expected is not None and (before is None or before["equation"] != expected):
            raise ValueError("Equation changed or is missing; inspect and retry.")
        configs = as_list(value(doc, "GetConfigurationNames"))
        if len(configs) > 1 and scope != "all":
            raise ValueError("Multi-configuration equations currently require explicit all scope.")
        flag_methods(manager, "Add2", "Add3", "GetConfigurationOption", "SetEquationAndConfigurationOption")
        if len(configs) > 1 and before and int(manager.GetConfigurationOption(before["index"])) not in (1, 2, 3):
            raise ValueError("Legacy equation configuration scope is unsupported; no change was made.")
        if before and str(_indexed(manager, "Equation", before["index"])) != expected:
            raise ValueError("Equation changed immediately before the write; inspect and retry.")
        item = {"target": lhs, "before": before, "applied": False, "may_have_changes": True}
        items.append(item)
        attempted = True
        if before:
            index = before["index"]
            if len(configs) > 1:
                native = int(manager.SetEquationAndConfigurationOption(index, equation, 2, empty_variant()))
            else:
                _indexed(manager, "Equation", index, equation)
                native = index
        elif len(configs) > 1:
            native = index = int(manager.Add3(-1, equation, True, 2, empty_variant()))
        else:
            native = index = int(manager.Add2(-1, equation, True))
        item["native_status"] = native
        if index < 0:
            raise RuntimeError("Native equation write failed.")
        actual = str(_indexed(manager, "Equation", index))
        evaluated = float(_indexed(manager, "Value", index))
        status = int(value(manager, "Status"))
        item.update(after={"index": index, "equation": actual, "value": evaluated, "evaluation_status": status},
                    applied=actual == equation)
        if native != index or status != index or not math.isfinite(evaluated) or not item["applied"]:
            raise RuntimeError("Equation was written but evaluation or readback failed.")
        if len(configs) > 1 and int(manager.GetConfigurationOption(index)) != 2:
            raise RuntimeError("Equation configuration assignment failed verification.")
        def verify() -> None:
            current = value(doc, "GetEquationMgr")
            actual = str(_indexed(current, "Equation", index))
            evaluated = float(_indexed(current, "Value", index))
            status = int(value(current, "Status"))
            item.update(after={"index": index, "equation": actual, "value": evaluated,
                               "evaluation_status": status, "evaluated_configuration": _configuration(doc)},
                        applied=actual == equation)
            if status != index or not math.isfinite(evaluated) or not item["applied"]:
                raise RuntimeError("Equation failed final evaluation or text verification.")
        return _finish(doc, items, attempted=attempted, scope=scope, verify=verify)
    except Exception as exc:
        if attempted and items and "after" not in items[-1]:
            try:
                _, rows = _equations(doc)
                matches = [r for r in rows if _lhs(r["equation"]) == lhs]
                items[-1].update(after=matches, applied=any(r["equation"] == equation for r in matches))
            except Exception as read_error:
                items[-1]["readback_error"] = str(read_error)
        return _finish(doc, items, str(exc), attempted=attempted, scope=scope) if attempted else _reject(exc)
