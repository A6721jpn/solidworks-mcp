"""Synchronize native sketch projections without replacing existing entities."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from .sw_core import (as_list, clear_selection, flag_methods, rebuild,
                      logger, require_document_path, result, select_object,
                      sketch_manager, value)


Key = tuple[int, tuple[int, int]]
Same = Callable[[Any, Any], Any]


@dataclass
class SketchState:
    sketch: Any
    feature: Any
    objects: dict[Key, Any]
    geometry: dict[Key, tuple]
    flags: dict[Key, bool]
    relations: tuple
    pairs: dict[Key, Key]


@dataclass
class Plan:
    add: tuple[Key, ...]
    flags: dict[Key, bool]
    unchanged: int
    protected: frozenset[Key]

    @property
    def noop(self) -> bool:
        return not self.add and not self.flags

    def summary(self) -> dict[str, Any]:
        def ids(keys):
            return [{"type": key[0], "id": list(key[1])} for key in list(keys)[:20]]
        return {"add": len(self.add), "flag_change": len(self.flags),
                "unchanged": self.unchanged, "protected": len(self.protected),
                "add_source_ids": ids(self.add), "flag_target_ids": ids(self.flags),
                "protected_target_ids": ids(sorted(self.protected)),
                "ids_limited_to": 20}


def _name(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{label} must be a nonempty exact name.")
    return raw


def _indices(raw: Any, label: str, nonempty: bool = False) -> list[int]:
    if not isinstance(raw, list) or (nonempty and not raw):
        raise ValueError(f"{label} must be an explicit {'nonempty ' if nonempty else ''}array.")
    if any(type(i) is not int or i < 0 for i in raw) or len(set(raw)) != len(raw):
        raise ValueError(f"{label} must contain unique nonnegative integer indices.")
    return raw


def _same(same: Same, first: Any, second: Any) -> bool:
    return int(same(first, second)) == 1


def _key(entity: Any) -> Key:
    kind = value(entity, "GetType")
    ids = as_list(value(entity, "GetID"))
    if type(kind) is not int or len(ids) != 2 or any(type(i) is not int for i in ids):
        raise ValueError("Entity has no supported native segment type and ID pair.")
    return kind, tuple(ids)


def _point(point: Any) -> tuple[float, ...]:
    return tuple(round(float(value(point, axis)), 12) for axis in ("X", "Y", "Z"))


def _geometry(segment: Any, kind: int) -> tuple:
    if kind not in (0, 1, 2):
        raise ValueError(f"Cannot verify geometry of native sketch segment type {kind}.")
    data = (_point(value(segment, "GetStartPoint2")),
            _point(value(segment, "GetEndPoint2")),
            round(float(value(segment, "GetLength")), 12))
    if kind == 1:
        data += (_point(value(segment, "GetCenterPoint2")),
                 round(float(value(segment, "GetRadius")), 12))
    elif kind == 2:
        data += tuple(_point(value(segment, method)) for method in
                      ("GetCenterPoint2", "GetMajorPoint2", "GetMinorPoint2"))
    return data


def _feature(doc: Any, name: str) -> Any:
    feature = flag_methods(doc, "FeatureByName").FeatureByName(name)
    if feature is None or value(feature, "GetTypeName2") != "ProfileFeature":
        raise ValueError(f"'{name}' is not a 2D sketch feature.")
    return feature


def _features(doc: Any) -> list[Any]:
    rows = []
    feature = value(doc, "FirstFeature")
    while feature is not None:
        rows.append(feature)
        feature = value(feature, "GetNextFeature")
    return rows


def _boundary(doc: Any) -> str | None:
    return next((str(value(feature, "Name")) for feature in _features(doc)
                 if bool(value(feature, "IsRolledBack"))), None)


def _errors(doc: Any) -> list[dict[str, Any]]:
    errors = []
    for feature in _features(doc):
        if not bool(value(feature, "IsSuppressed")) and not bool(value(feature, "IsRolledBack")):
            code = int(value(feature, "GetErrorCode"))
            if code:
                errors.append({"feature": str(value(feature, "Name")), "code": code})
    return errors


def _rollback(doc: Any, before: str | None) -> None:
    manager = flag_methods(doc.FeatureManager, "EditRollback")
    if not bool(manager.EditRollback(3 if before else 1, before or "")):
        raise RuntimeError(f"Could not restore rollback boundary before '{before or '<end>'}'.")
    if _boundary(doc) != before:
        raise RuntimeError("Native rollback boundary does not match the requested boundary.")


def _entity(entity: Any, same: Same, source: Any, target: Any) -> tuple:
    if entity is None:
        raise ValueError("Relation has a null or dangling definition entity.")
    owner = value(entity, "GetSketch")
    if owner is None:
        raise ValueError("Relation definition entity has no owning sketch.")
    scope = ("source" if _same(same, owner, source) else "target" if _same(same, owner, target)
             else ("other", str(value(owner, "Name"))))
    ids = as_list(value(entity, "GetID"))
    if len(ids) != 2 or any(type(i) is not int for i in ids):
        raise ValueError("Relation definition entity has no native ID pair.")
    try:
        kind = value(entity, "GetType")
    except Exception:
        kind = None
    if type(kind) is int:
        return scope, kind, tuple(ids)
    _point(entity)
    return scope, "point", tuple(ids)


def _relations(same: Same, sketch: Any, source: Any, target: Any,
               objects: dict[Key, Any]) -> tuple[tuple, dict[Key, Key]]:
    relations, pairs = [], {}
    target_provenance: dict[Key, list[tuple]] = {}
    manager = value(sketch, "RelationManager")
    is_target = _same(same, sketch, target)
    for relation in as_list(flag_methods(manager, "GetRelations").GetRelations(0)):
        kind = int(value(relation, "GetRelationType"))
        entities = tuple(sorted((_entity(entity, same, source, target) for entity in
                                 as_list(value(relation, "GetDefinitionEntities"))), key=repr))
        if not entities:
            raise ValueError("Relation has no readable definition entities.")
        relations.append((kind, entities))
        if kind == 32 and is_target:
            local = [entity for entity in entities if entity[0] == "target" and type(entity[1]) is int]
            external = [entity for entity in entities if entity[0] != "target" and type(entity[1]) is int]
            if len(entities) != 2 or len(local) != 1 or len(external) != 1:
                raise ValueError("Projection has ambiguous or dangling native provenance.")
            target_key = local[0][1:]
            target_provenance.setdefault(target_key, []).append(external[0])
            if external[0][0] == "source":
                source_key, target_key = external[0][1:], local[0][1:]
                if target_key not in objects or source_key in pairs or target_key in pairs.values():
                    raise ValueError("Projection has duplicate or missing native source/target entities.")
                pairs[source_key] = target_key
    if any(len(target_provenance[key]) != 1 for key in pairs.values()):
        raise ValueError("Managed projection has multiple native source ownership relations.")
    return tuple(sorted(relations, key=repr)), pairs


def _read(same: Same, feature: Any, sketch: Any, source: Any, target: Any,
          geometry: bool = True, relations: bool = True) -> SketchState:
    objects, fingerprints, flags = {}, {}, {}
    for segment in as_list(value(sketch, "GetSketchSegments")):
        if not _same(same, value(segment, "GetSketch"), sketch):
            raise ValueError("Segment ownership does not match its containing sketch.")
        key = _key(segment)
        if key in objects:
            raise ValueError("Duplicate native segment identity.")
        objects[key] = segment
        if geometry:
            fingerprints[key] = _geometry(segment, key[0])
        flags[key] = bool(value(segment, "ConstructionGeometry"))
    native_relations, pairs = _relations(same, sketch, source, target, objects) if relations else ((), {})
    return SketchState(sketch, feature, objects, fingerprints, flags,
                       native_relations, pairs)


def _states(same: Same, doc: Any, source_name: str, target_name: str,
            inventory: bool = False) -> tuple[SketchState, SketchState]:
    source_feature, target_feature = _feature(doc, source_name), _feature(doc, target_name)
    source = value(source_feature, "GetSpecificFeature2")
    target = value(target_feature, "GetSpecificFeature2")
    if source is None or target is None or _same(same, source, target):
        raise ValueError("Source and target must be distinct readable sketches.")
    return (_read(same, source_feature, source, source, target, geometry=not inventory, relations=not inventory),
            _read(same, target_feature, target, source, target, geometry=not inventory))


def _fingerprint(state: SketchState) -> tuple:
    return state.geometry, state.flags, state.relations


def _resolve(indices: list[int], state: SketchState, label: str) -> frozenset[Key]:
    keys = list(state.objects)
    if any(i >= len(keys) for i in indices):
        raise ValueError(f"{label} contains an out-of-range sketch segment index.")
    return frozenset(keys[i] for i in indices)


def _protected(raw: Any) -> frozenset[Key]:
    if not isinstance(raw, list):
        raise ValueError("sync.protected_target_segments must be an explicit array of native {type,id} identities.")
    keys = []
    for item in raw:
        if (not isinstance(item, dict) or set(item) != {"type", "id"}
                or type(item["type"]) is not int or item["type"] < 0
                or not isinstance(item["id"], list) or len(item["id"]) != 2
                or any(type(i) is not int for i in item["id"])):
            raise ValueError("sync.protected_target_segments requires native {type,id:[a,b]} identities; indices are unsupported.")
        keys.append((item["type"], tuple(item["id"])))
    if len(set(keys)) != len(keys):
        raise ValueError("sync.protected_target_segments contains duplicate native identities.")
    return frozenset(keys)


def _plan(source: SketchState, target: SketchState, selected: frozenset[Key],
          protected: frozenset[Key]) -> Plan:
    changes, unchanged = {}, 0
    for source_key, target_key in target.pairs.items():
        if source_key not in source.objects:
            raise ValueError("Projection source no longer belongs to the named source sketch.")
        desired = source.flags[source_key] if source_key in selected else True
        if target_key in protected:
            if source_key in selected and desired != target.flags[target_key]:
                raise ValueError("Selected source conflicts with a protected target segment.")
            continue
        if desired != target.flags[target_key]:
            changes[target_key] = desired
        else:
            unchanged += 1
    managed = set(target.pairs.values())
    for kind, entities in target.relations:
        if kind != 32 and any(entity[0] == "target" and entity[1:] in managed for entity in entities):
            if any(entity[0] != "target" for entity in entities):
                raise ValueError("Managed target has an unsupported mixed-owner relation.")
    return Plan(tuple(sorted(selected - target.pairs.keys())), changes, unchanged, protected)


def _verify(before: tuple[SketchState, SketchState], after: tuple[SketchState, SketchState],
            selected: frozenset[Key], plan: Plan) -> None:
    old_source, old_target = before
    source, target = after
    if _fingerprint(source) != _fingerprint(old_source):
        raise RuntimeError("Source sketch changed during profile synchronization.")
    if len(target.objects) != len(old_target.objects) + len(plan.add):
        raise RuntimeError("Target segment count does not match the synchronization plan.")
    for key in old_target.objects:
        if key not in target.objects or target.geometry[key] != old_target.geometry[key]:
            raise RuntimeError("An existing target identity or its geometry changed.")
        if target.flags[key] != plan.flags.get(key, old_target.flags[key]):
            raise RuntimeError("An existing target construction flag differs from the plan.")
    remaining = list(target.relations)
    for relation in old_target.relations:
        if relation not in remaining:
            raise RuntimeError("An existing target relation changed or disappeared.")
        remaining.remove(relation)
    new_targets = set(target.objects) - old_target.objects.keys()
    for _, entities in remaining:
        if any(entity[0] == "target" and entity[1:] in old_target.objects for entity in entities):
            raise RuntimeError("Native conversion added a relation to an existing target segment.")
        if not any(entity[0] == "target" and entity[1:] in new_targets for entity in entities):
            raise RuntimeError("Native conversion added an unrelated relation.")
    if not _plan(source, target, selected, plan.protected).noop:
        raise RuntimeError("Desired native projection pairs or construction flags were not established.")


def synchronize(args: dict[str, Any]) -> dict[str, Any]:
    started = perf_counter()
    doc = None
    recording = False
    grouped = False
    mutation_attempted = False
    context_changed = False
    original_boundary = None
    before = None
    plan = None
    restoration_errors = []
    phase_ms = {}
    active_phase = None
    phase_started = started

    def finish_phase() -> None:
        nonlocal active_phase
        if active_phase is not None:
            elapsed = (perf_counter() - phase_started) * 1000
            phase_ms[active_phase] = round(phase_ms.get(active_phase, 0) + elapsed, 3)
            logger.info("profile_sync %s %.3f ms", active_phase, elapsed)
            active_phase = None

    def start_phase(name: str) -> None:
        nonlocal active_phase, phase_started
        finish_phase()
        active_phase, phase_started = name, perf_counter()

    def response(ok: bool, message: str, **data: Any) -> dict[str, Any]:
        finish_phase()
        return result(ok, message, elapsed_ms=round((perf_counter() - started) * 1000, 3),
                      phase_elapsed_ms=phase_ms, **data)

    def restore_context() -> None:
        start_phase("restore_context")
        manager = sketch_manager(doc)
        if value(manager, "ActiveSketch") is not None:
            manager.InsertSketch(True)
        if value(manager, "ActiveSketch") is not None:
            raise RuntimeError("Could not close the target sketch.")
        _rollback(doc, original_boundary)
        if not rebuild(doc):
            raise RuntimeError("Native rebuild failed while restoring the original rollback boundary.")
        if _boundary(doc) != original_boundary:
            raise RuntimeError("Rebuild changed the restored rollback boundary.")
        if str(value(doc.ConfigurationManager.ActiveConfiguration, "Name")) != expected_configuration:
            raise RuntimeError("Active configuration changed during profile synchronization.")
        clear_selection(doc)
        finish_phase()

    try:
        sync = args.get("sync")
        if not isinstance(sync, dict):
            raise ValueError("sync must be an object.")
        if args.get("chain", True) is not False or args.get("inner_loops", False) is not False:
            raise ValueError("Profile sync requires chain=false and inner_loops=false.")
        if type(sync.get("apply", False)) is not bool:
            raise ValueError("sync.apply must be a boolean.")
        selection = args.get("selection")
        if not isinstance(selection, dict) or set(selection) - {"sketch_name", "sketch_segments"}:
            raise ValueError("Profile sync selection accepts only sketch_name and sketch_segments.")
        source_name = _name(selection.get("sketch_name"), "selection.sketch_name")
        selected_indices = _indices(selection.get("sketch_segments"), "selection.sketch_segments", True)
        target_name = _name(sync.get("target_sketch"), "sync.target_sketch")
        protected = _protected(sync.get("protected_target_segments"))
        expected_configuration = _name(sync.get("expected_configuration"), "sync.expected_configuration")
        rollback_before = _name(sync["rollback_before"], "sync.rollback_before") if "rollback_before" in sync else None
        app, doc = require_document_path(_name(sync.get("expected_document_path"), "sync.expected_document_path"), 1)
        same = flag_methods(app, "IsSame").IsSame
        manager = sketch_manager(doc)
        if value(manager, "ActiveSketch") is not None:
            raise ValueError("Close the active sketch before profile synchronization.")
        if str(value(doc.ConfigurationManager.ActiveConfiguration, "Name")) != expected_configuration:
            raise ValueError("Active configuration does not match sync.expected_configuration.")
        start_phase("inventory")
        original_boundary = _boundary(doc)
        before = _states(same, doc, source_name, target_name, inventory=True)
        selected = _resolve(selected_indices, before[0], "selection.sketch_segments")
        if not protected <= before[1].objects.keys():
            raise ValueError("sync.protected_target_segments includes a native identity missing from the target sketch.")
        plan = _plan(*before, selected, protected)
        if rollback_before and flag_methods(doc, "FeatureByName").FeatureByName(rollback_before) is None:
            raise ValueError("sync.rollback_before does not name an existing top-level feature.")
        finish_phase()
        if not sync.get("apply", False) or plan.noop:
            return response(True, "Profile synchronization is a no-op." if plan.noop else "Planned profile synchronization.",
                            applied=False, noop=plan.noop, plan=plan.summary(), may_have_changes=False,
                            restored=True, rollback_before=original_boundary)
        start_phase("feature_errors_before")
        if _errors(doc):
            raise ValueError("Resolve existing native feature errors before applying profile synchronization.")
        start_phase("baseline_fingerprint")
        for state in before:
            state.geometry = {key: _geometry(segment, key[0]) for key, segment in state.objects.items()}
        before[0].relations, _ = _relations(same, before[0].sketch, before[0].sketch,
                                           before[1].sketch, before[0].objects)
        finish_phase()
        ext = flag_methods(doc.Extension, "StartRecordingUndoObject", "FinishRecordingUndoObject2")
        ext.StartRecordingUndoObject()
        recording = True
        context_changed = True
        start_phase("rollback_inventory")
        if rollback_before:
            _rollback(doc, rollback_before)
        current = _states(same, doc, source_name, target_name, inventory=True)
        if (any(not _same(same, old.sketch, new.sketch)
                or old.objects.keys() != new.objects.keys() or old.flags != new.flags
                for old, new in zip(before, current))
                or before[1].relations != current[1].relations):
            raise RuntimeError("Sketch identities, flags or target relations changed after rollback; no geometry was written.")
        start_phase("projection")
        clear_selection(doc)
        if not select_object(doc, current[1].feature, append=False):
            raise RuntimeError("Could not select the target sketch.")
        manager.InsertSketch(True)
        if value(manager, "ActiveSketch") is None or not _same(same, value(manager, "ActiveSketch"), current[1].sketch):
            raise RuntimeError("Native edit state did not enter the target sketch.")
        for key, construction in plan.flags.items():
            mutation_attempted = True
            current[1].objects[key].ConstructionGeometry = construction
        if plan.add:
            clear_selection(doc)
            for key in plan.add:
                if not select_object(doc, current[0].objects[key], append=True):
                    raise RuntimeError("Could not select a missing source segment.")
            mutation_attempted = True
            if not bool(manager.SketchUseEdge3(False, False)):
                raise RuntimeError("Native projection failed; it may have created partial entities.")
            projected = _read(same, current[1].feature, current[1].sketch,
                              current[0].sketch, current[1].sketch, geometry=False)
            for key in plan.add:
                if key not in projected.pairs:
                    raise RuntimeError("A new projection has no proved native source correspondence.")
                projected.objects[projected.pairs[key]].ConstructionGeometry = current[0].flags[key]
        finish_phase()
        restore_context()
        start_phase("final_verification")
        _verify(before, _states(same, doc, source_name, target_name), selected, plan)
        start_phase("feature_errors_after")
        if _errors(doc):
            raise RuntimeError("Native features have errors after profile synchronization.")
        finish_phase()
        grouped = bool(ext.FinishRecordingUndoObject2("Synchronize projected profile", False))
        recording = False
        if not grouped:
            raise RuntimeError("SOLIDWORKS did not confirm the synchronization undo group.")
        return response(True, "Synchronized native projected profile.", applied=True, noop=False,
                        plan=plan.summary(), restored=True, may_have_changes=False,
                        rollback_before=original_boundary, undo_grouped=True)
    except Exception as exc:
        failed_phase = active_phase
        finish_phase()
        if context_changed:
            try:
                restore_context()
            except Exception as restoration:
                restoration_errors.append(str(restoration))
        if recording:
            try:
                grouped = bool(ext.FinishRecordingUndoObject2("Synchronize projected profile", False))
            except Exception as restoration:
                restoration_errors.append(f"Could not finish undo recording: {restoration}")
        undone = False
        if mutation_attempted and grouped:
            try:
                flag_methods(doc, "EditUndo2").EditUndo2(1)
                restore_context()
                start_phase("undo_verification")
                after = _states(same, doc, source_name, target_name)
                undone = (all(_fingerprint(old) == _fingerprint(new) for old, new in zip(before, after))
                          and not _errors(doc))
                if not undone:
                    restoration_errors.append("Undo did not restore the original source/target fingerprints and errors.")
            except Exception as restoration:
                restoration_errors.append(f"Could not verify undo restoration: {restoration}")
        elif context_changed and not mutation_attempted and not restoration_errors:
            try:
                start_phase("restored_fingerprint")
                after = _states(same, doc, source_name, target_name)
                if any(_fingerprint(old) != _fingerprint(new) for old, new in zip(before, after)):
                    restoration_errors.append("Restoring context did not restore the original sketch fingerprints.")
            except Exception as restoration:
                restoration_errors.append(f"Could not verify restored sketch fingerprints: {restoration}")
        if doc is not None and context_changed:
            clear_selection(doc)
        restored = not restoration_errors and (not mutation_attempted or undone)
        return response(False, str(exc), plan=plan.summary() if plan else None,
                        applied=False, noop=False, restored=restored,
                        may_have_changes=bool((mutation_attempted and not undone) or restoration_errors),
                        undo_grouped=grouped, undone=undone,
                        failed_phase=failed_phase,
                        restoration_errors=restoration_errors, rollback_before=original_boundary)
