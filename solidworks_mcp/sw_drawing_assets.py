"""Named drawing notes, title fields, and native revision tables.

Snapshots describe observable API fields, not a claim of visual equivalence.
Source drawings must already be open and are never activated or edited.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import tempfile
from typing import Any

import pythoncom
from win32com.client import dynamic

from .sw_core import (
    as_list, clear_selection, document_path_key, document_type, flag_methods,
    nothing, open_document_by_path, require_document_path, result, safe,
    to_m, tool, value,
)


_FORMAT_FIELDS = (
    "TypefaceName", "CharHeight", "CharHeightInPts", "Bold", "Italic",
    "Underline", "Strikeout", "WidthFactor", "CharSpacingFactor",
    "Escapement", "BackWards", "UpsideDown", "ObliqueAngle",
)
_NOTE_FIELDS = ("Angle", "AllUpperCase", "BehindSheet")
_TABLE_FIELDS = (
    "AnchorType", "Anchored", "BorderLineWeight", "GridLineWeight",
    "BorderLineWeightCustom", "GridLineWeightCustom", "Title", "TitleVisible",
)


def _call(obj: Any, member: str, *args: Any) -> Any:
    return getattr(flag_methods(obj, member), member)(*args)


def _read(obj: Any, member: str, *args: Any) -> Any:
    return _call(obj, member, *args) if args else value(obj, member)


def _indexed_property(obj: Any, member: str, *args: Any) -> Any:
    if not hasattr(obj, "_oleobj_"):
        return getattr(obj, member)(*args)
    ole = obj._oleobj_
    raw = ole.Invoke(ole.GetIDsOfNames(member), 0, pythoncom.DISPATCH_PROPERTYGET, True, *args)
    return dynamic.Dispatch(raw) if isinstance(raw, dynamic.PyIDispatchType) else raw


def _optional(obj: Any, member: str, unsupported: list[str], label: str, *args: Any) -> Any:
    try:
        return _read(obj, member, *args)
    except Exception:
        unsupported.append(label)
        return None


def _format(fmt: Any) -> dict[str, Any]:
    fields = {key: safe(fmt, key) for key in _FORMAT_FIELDS}
    return {key: val for key, val in fields.items() if val is not None}


def _position(annotation: Any) -> list[float]:
    return [round(float(v) * 1000, 6) for v in as_list(value(annotation, "GetPosition"))]


@dataclass
class NoteAsset:
    note: Any
    annotation: Any
    snapshot: dict[str, Any]
    formats: list[Any]


def _note_asset(note: Any, sheet: str, view: str) -> NoteAsset:
    annotation = value(note, "GetAnnotation")
    unsupported: list[str] = []
    raw = _optional(note, "PropertyLinkedText", unsupported, "raw_text")
    count = _optional(annotation, "GetTextFormatCount", unsupported, "formats.count")
    formats = []
    use_doc = []
    for index in range(int(count or 0)):
        fmt = _optional(annotation, "GetTextFormat", unsupported, f"formats[{index}]", index)
        formats.append(fmt)
        use_doc.append(_optional(annotation, "GetUseDocTextFormat", unsupported,
                                 f"formats[{index}].use_document", index))
    runs = []
    text_count = _optional(note, "GetTextCount", unsupported, "runs.count")
    for index in range(int(text_count or 0)):
        runs.append({
            "text": _optional(note, "GetTextAtIndex", unsupported, f"runs[{index}].text", index + 1),
            "font": _optional(note, "GetTextFontAtIndex", unsupported, f"runs[{index}].font", index),
            "height_m": _optional(note, "GetTextHeightAtIndex", unsupported, f"runs[{index}].height_m", index),
        })
    snapshot = {
        "name": str(value(annotation, "GetName") or ""), "sheet": sheet, "view": view,
        "text": str(value(note, "GetText") or ""), "raw_text": raw,
        "position_mm": _position(annotation),
        "owner_type": _optional(annotation, "OwnerType", unsupported, "owner_type"),
        "layer": _optional(annotation, "Layer", unsupported, "layer"),
        "justification": _optional(note, "GetTextJustification", unsupported, "justification"),
        "multiple_fonts": _optional(note, "HasMultipleFonts", unsupported, "multiple_fonts"),
        "formats": [_format(fmt) if fmt is not None else None for fmt in formats],
        "use_document_formats": use_doc, "runs": runs,
        "attributes": {name: _optional(note, name, unsupported, f"attributes.{name}")
                       for name in _NOTE_FIELDS},
        "unsupported_fields": unsupported,
    }
    return NoteAsset(note, annotation, snapshot, formats)


def _notes(doc: Any, sheet_name: str | None = None, note_names: set[str] | None = None) -> list[NoteAsset]:
    assets = []
    for group in as_list(value(doc, "GetViews")):
        views = [dynamic.Dispatch(v) if isinstance(v, dynamic.PyIDispatchType) else v
                 for v in as_list(group)]
        if not views:
            continue
        name = str(value(views[0], "GetName2") or "")
        if sheet_name is not None and name != sheet_name:
            continue
        for view in views:
            view_name = str(value(view, "GetName2") or "")
            for note in as_list(value(view, "GetNotes")):
                if (note_names is not None
                        and str(value(value(note, "GetAnnotation"), "GetName")) not in note_names):
                    continue
                assets.append(_note_asset(note, name, view_name))
    return assets


def _current_sheet(doc: Any) -> tuple[Any, str]:
    sheet = value(doc, "GetCurrentSheet")
    return sheet, str(value(sheet, "GetName"))


def _source(app: Any, target_path: str, source_path: str, sheet_name: str | None) -> tuple[Any, Any, str]:
    if document_path_key(target_path) == document_path_key(source_path):
        raise ValueError("Source and target documents must differ.")
    doc = open_document_by_path(app, source_path)
    if document_type(doc) != 3:
        raise ValueError("Source must be an already-open drawing.")
    if sheet_name:
        if sheet_name not in as_list(value(doc, "GetSheetNames")):
            raise ValueError(f"Source sheet '{sheet_name}' does not exist.")
        return doc, _indexed_property(doc, "Sheet", sheet_name), sheet_name
    sheet, name = _current_sheet(doc)
    return doc, sheet, name


def _different(expected: Any, actual: Any, path: str = "") -> list[str]:
    if isinstance(expected, dict):
        return [mismatch for key, val in expected.items()
                for mismatch in _different(val, actual.get(key) if isinstance(actual, dict) else None,
                                           f"{path}.{key}".strip("."))]
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return [path]
        return [mismatch for i, val in enumerate(expected)
                for mismatch in _different(val, actual[i], f"{path}[{i}]")]
    if isinstance(expected, (int, float)) and not isinstance(expected, bool) and isinstance(actual, (int, float)):
        return [] if math.isclose(expected, actual, abs_tol=1e-7, rel_tol=1e-7) else [path]
    return [] if expected == actual else [path]


def _note_contract(snapshot: dict[str, Any], position: list[float] | None = None) -> dict[str, Any]:
    fields = ("name", "owner_type", "raw_text", "layer", "justification", "multiple_fonts", "formats", "use_document_formats", "runs", "attributes")
    contract = {key: snapshot[key] for key in fields if snapshot[key] is not None}
    # Linked display text can resolve differently in the destination document.
    if snapshot["raw_text"] is None:
        contract["text"] = snapshot["text"]
    else:
        contract["runs"] = [{key: val for key, val in run.items() if key != "text"}
                            for run in snapshot["runs"]]
    contract["position_mm"] = position or snapshot["position_mm"]
    return contract


def _set_note_content(asset: NoteAsset, text: str) -> None:
    if asset.snapshot["raw_text"] is not None:
        setattr(asset.note, "PropertyLinkedText", text)
    elif _call(asset.note, "SetText", text) is False:
        raise RuntimeError("SetText rejected note text.")
    for index, fmt in enumerate(asset.formats):
        use_doc = asset.snapshot["use_document_formats"][index]
        if fmt is not None and _call(asset.annotation, "SetTextFormat", index, bool(use_doc), fmt) is False:
            raise RuntimeError(f"SetTextFormat rejected format {index}.")


def _copy_note_properties(source: NoteAsset, target: NoteAsset, position: list[float]) -> None:
    _set_note_content(target, source.snapshot["raw_text"] if source.snapshot["raw_text"] is not None
                      else source.snapshot["text"])
    for index, fmt in enumerate(source.formats):
        use_doc = source.snapshot["use_document_formats"][index]
        if fmt is not None and _call(target.annotation, "SetTextFormat", index, bool(use_doc), fmt) is False:
            raise RuntimeError(f"SetTextFormat rejected source format {index}.")
    if source.snapshot["layer"] is not None:
        target.annotation.Layer = source.snapshot["layer"]
    if source.snapshot["justification"] is not None:
        _call(target.note, "SetTextJustification", source.snapshot["justification"])
    for name, val in source.snapshot["attributes"].items():
        if val is not None:
            setattr(target.note, name, val)
    if _call(target.annotation, "SetPosition2", *(float(v) / 1000 for v in position)) is False:
        raise RuntimeError("SetPosition2 rejected the note position.")


def _checked_select_delete(doc: Any, table: Any) -> None:
    clear_selection(doc)
    try:
        feature = value(value(table, "RevisionTableFeature"), "GetFeature")
        if not _call(feature, "Select2", False, 0):
            raise RuntimeError("Could not select the exact target revision-table feature.")
        extension = value(doc, "Extension")
        if not _call(extension, "DeleteSelection2", 0):
            raise RuntimeError("Could not delete the selected target annotation.")
    finally:
        clear_selection(doc)


@dataclass
class TableAsset:
    table: Any
    snapshot: dict[str, Any]
    formats: list[list[Any]]


def _table_asset(table: Any) -> TableAsset:
    unsupported: list[str] = []
    rows, columns = int(value(table, "RowCount")), int(value(table, "ColumnCount"))
    annotation = value(table, "GetAnnotation")
    header_style = _optional(table, "GetHeaderStyle", unsupported, "header_style")
    header_count = _optional(table, "GetHeaderCount", unsupported, "header_count")
    header_rows = set()
    if header_style == 1:
        header_rows = set(range(int(header_count or 0)))
    elif header_style == 2:
        header_rows = set(range(rows - int(header_count or 0), rows))
    cells, fonts, formats, revisions, use_document = [], [], [], [], []
    for row in range(rows):
        cells.append([str(_indexed_property(table, "Text", row, column) or "") for column in range(columns)])
        formats.append([_optional(table, "GetCellTextFormat", unsupported,
                                  f"fonts[{row}][{column}]", row, column) for column in range(columns)])
        fonts.append([_format(fmt) if fmt is not None else None for fmt in formats[-1]])
        use_document.append([_optional(table, "GetCellUseDocTextFormat", unsupported,
                                       f"use_document_formats[{row}][{column}]", row, column)
                             for column in range(columns)])
        if row in header_rows:
            continue
        revision_id = _optional(table, "GetIdForRowNumber", unsupported, f"revision_rows[{row}]", row)
        if revision_id is not None and int(revision_id) >= 0:
            revision = _optional(table, "GetRevisionForId", unsupported, f"revision_rows[{row}].revision",
                                 int(revision_id))
            if revision:
                revisions.append({"row": row, "id": int(revision_id), "revision": str(revision)})
    snapshot = {
        "name": str(value(annotation, "GetName") or ""), "rows": rows, "columns": columns,
        "cells": cells, "fonts": fonts, "use_document_formats": use_document,
        "column_types": [_optional(table, "GetColumnType2", unsupported, f"column_types[{c}]", c, True)
                         for c in range(columns)],
        "column_custom_properties": [_optional(table, "GetColumnCustomProperty", unsupported,
                                               f"column_custom_properties[{c}]", c) for c in range(columns)],
        "column_widths_m": [_optional(table, "GetColumnWidth", unsupported, f"column_widths_m[{c}]", c)
                            for c in range(columns)],
        "row_heights_m": [_optional(table, "GetRowHeight", unsupported, f"row_heights_m[{r}]", r)
                          for r in range(rows)],
        "header_style": header_style, "header_count": header_count,
        "position_mm": _position(annotation), "revision_rows": revisions,
        "current_revision": _optional(table, "CurrentRevision", unsupported, "current_revision"),
        "attributes": {name: _optional(table, name, unsupported, f"attributes.{name}")
                       for name in _TABLE_FIELDS},
        "unsupported_fields": unsupported,
    }
    return TableAsset(table, snapshot, formats)


def _table_contract(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: ([{"row": item["row"], "revision": item["revision"]} for item in val]
                  if key == "revision_rows" else val)
            for key, val in snapshot.items() if key not in ("name", "unsupported_fields")}


def _revision_table(sheet: Any) -> Any:
    return value(sheet, "RevisionTable")


def _raw_cell_put(table: Any, row: int, column: int, text: str) -> None:
    ole = table._oleobj_
    dispid = ole.GetIDsOfNames("Text")
    ole.Invoke(dispid, 0, pythoncom.DISPATCH_PROPERTYPUT, 0, row, column, text)


_PATH_PROPERTY = {"type": "string", "description": "Active drawing document_key (saved path or untitled:<exact title>)."}


@tool("inspect_drawing_assets", "Read exact note names, linked text, title fields, and native revision table metadata. No saves.",
      {"expected_document_path": _PATH_PROPERTY}, ["expected_document_path"])
def inspect_drawing_assets(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_document_path(args["expected_document_path"], 3)
    notes = _notes(doc)
    tables = []
    sheets = []
    for name in as_list(value(doc, "GetSheetNames")):
        sheet = _indexed_property(doc, "Sheet", str(name))
        sheets.append({"name": str(name), "sheet_format_path": safe(sheet, "GetTemplateName"),
                       "properties": as_list(safe(sheet, "GetProperties2")),
                       "custom_property_view": safe(sheet, "CustomPropertyView")})
        table = _revision_table(sheet)
        if table is not None:
            tables.append({"sheet": str(name), **_table_asset(table).snapshot})
    return result(True, "Drawing assets inspected; unsupported API fields are listed explicitly.",
                  notes=[note.snapshot for note in notes], revision_tables=tables, sheets=sheets,
                  title_fields=[note.snapshot for note in notes if note.snapshot["owner_type"] == 2],
                  fidelity_scope="Observable fields only; vendor attributes and rendered appearance are not certified.")


@tool("copy_drawing_note", "Copy one exact named sheet-owned note from an already-open drawing to the active sheet. Preserve raw links and supported formats; view-attached and template notes require their own editing workflow. No saves.",
      {"expected_document_path": _PATH_PROPERTY, "source_document_path": {"type": "string"},
       "source_note_name": {"type": "string"}, "source_sheet": {"type": "string"},
       "target_note_name": {"type": "string"}, "target_x_mm": {"type": "number"},
       "target_y_mm": {"type": "number"}, "replace_existing": {"type": "boolean", "default": False}},
      ["expected_document_path", "source_document_path", "source_note_name", "target_x_mm", "target_y_mm"])
def copy_drawing_note(args: dict[str, Any]) -> dict[str, Any]:
    app, doc = require_document_path(args["expected_document_path"], 3)
    source_doc, _, source_sheet = _source(app, args["expected_document_path"], args["source_document_path"],
                                        args.get("source_sheet"))
    sources = _notes(source_doc, source_sheet, {args["source_note_name"]})
    if len(sources) != 1:
        return result(False, "Source note name must match exactly one note.", matches=len(sources))
    source = sources[0]
    if source.snapshot["owner_type"] != 1:
        return result(False, "Source must be a sheet-owned note; view attachments and template fields cannot be copied as sheet notes.",
                      changed=False, source=source.snapshot)
    sheet, sheet_name = _current_sheet(doc)
    name = args.get("target_note_name") or args["source_note_name"]
    position = [float(args["target_x_mm"]), float(args["target_y_mm"]), 0.0]
    if not all(math.isfinite(v) for v in position):
        raise ValueError("Note coordinates must be finite millimetres.")
    targets = _notes(doc, note_names={name})
    if len(targets) > 1 or (targets and targets[0].snapshot["sheet"] != sheet_name):
        return result(False, "Target note name is ambiguous or belongs to another sheet.", target_note_name=name)
    if targets and targets[0].snapshot["owner_type"] != 1:
        return result(False, "The named target belongs to a view or sheet format; it cannot be replaced as a sheet note.", changed=False)
    contract = _note_contract(source.snapshot, position)
    contract["name"] = name
    if targets:
        mismatch = _different(contract, _note_contract(targets[0].snapshot))
        if not mismatch:
            return result(True, "Matching named note already exists.", changed=False, note=targets[0].snapshot,
                          unsupported_fields=source.snapshot["unsupported_fields"], fidelity_scope="Observable fields only.")
        if not args.get("replace_existing", False):
            return result(False, "Named note differs; explicit replace_existing is required.", mismatch_fields=mismatch,
                          before=targets[0].snapshot, source=source.snapshot)
    try:
        initial_template_mode = not bool(value(doc, "GetEditSheet"))
    except Exception:
        return result(False, "Cannot read the initial sheet editing mode.", changed=False,
                      unsupported_fields=["GetEditSheet"])
    partial = []
    outcome = None
    try:
        if initial_template_mode and _read(doc, "EditSheet") is False:
            raise RuntimeError("Drawing refused to enter sheet editing mode.")
        if targets:
            target = targets[0]
            partial.append({"kind": "modified_note", "name": name})
        else:
            clear_selection(doc)
            note = _call(doc, "InsertNote", source.snapshot["raw_text"] if source.snapshot["raw_text"] is not None
                         else source.snapshot["text"])
            if note is None:
                raise RuntimeError("InsertNote returned no note.")
            partial.append({"kind": "created_note", "name": name})
            target = _note_asset(note, sheet_name, sheet_name)
            partial[-1]["actual_name"] = target.snapshot["name"]
            if target.snapshot["owner_type"] != 1:
                target.annotation.OwnerType = 1
                target.annotation.Owner = sheet
            actual_owner = value(target.annotation, "Owner")
            if (int(value(target.annotation, "OwnerType")) != 1
                    or str(value(actual_owner, "GetName")) != sheet_name):
                raise RuntimeError("The created note could not be bound to the exact target sheet.")
            if _call(target.annotation, "SetName", name) is False:
                partial[-1]["actual_name"] = str(value(target.annotation, "GetName"))
                raise RuntimeError("The exact target note name could not be assigned.")
        _copy_note_properties(source, target, position)
        after = _note_asset(target.note, sheet_name, target.snapshot["view"]).snapshot
        mismatch = _different(contract, _note_contract(after))
        outcome = result(not mismatch, "Note copied and observable fields compared." if not mismatch else
                         "Note changed, but some observable fields differ.", changed=True, source=source.snapshot,
                         note=after, mismatch_fields=mismatch, unsupported_fields=source.snapshot["unsupported_fields"],
                         partial_artifacts=partial if mismatch else [], fidelity_scope="Observable fields only; vendor attributes are unverified.")
    except Exception as exc:
        outcome = result(False, str(exc), partial_artifacts=partial, source=source.snapshot,
                         rollback_performed=False)
    finally:
        try:
            if initial_template_mode != (not bool(value(doc, "GetEditSheet"))):
                restore = "EditTemplate" if initial_template_mode else "EditSheet"
                if _read(doc, restore) is False:
                    raise RuntimeError(f"{restore} returned false.")
            if initial_template_mode != (not bool(value(doc, "GetEditSheet"))):
                raise RuntimeError("Sheet mode readback differs from the initial mode.")
        except Exception as exc:
            outcome = result(False, f"Note processed, but restoring sheet mode failed: {exc}",
                             partial_artifacts=partial, rollback_performed=False)
    return outcome


@tool("set_title_block_fields", "Set existing notes by exact annotation name on the active sheet. Preflight all fields. Blank strings clear text. No inferred values or saves.",
      {"expected_document_path": _PATH_PROPERTY, "fields": {"type": "object", "additionalProperties": {"type": "string"}}},
      ["expected_document_path", "fields"])
def set_title_block_fields(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_document_path(args["expected_document_path"], 3)
    fields = args["fields"]
    if not isinstance(fields, dict) or not all(isinstance(name, str) and name and isinstance(text, str)
                                               for name, text in fields.items()):
        raise ValueError("fields must map nonempty exact annotation names to string values.")
    _, sheet_name = _current_sheet(doc)
    notes = _notes(doc, sheet_name, set(fields))
    matches = {name: [note for note in notes if note.snapshot["name"] == name] for name in fields}
    invalid = {name: len(found) for name, found in matches.items() if len(found) != 1}
    if invalid:
        return result(False, "All field names must match one note before any changes.", invalid_fields=invalid, changed=False)
    uncertain = [name for name, found in matches.items() if found[0].snapshot["owner_type"] is None]
    if uncertain:
        return result(False, "Cannot determine whether fields belong to the sheet format.", unsupported_fields=uncertain, changed=False)
    mixed = [name for name, found in matches.items() if found[0].snapshot["multiple_fonts"]
             and fields[name] != found[0].snapshot["raw_text"]]
    if mixed:
        return result(False, "Mixed-font title fields require explicit formatted text editing; plain replacement would lose run formatting.",
                      unsupported_fields=[f"{name}.mixed_font_text_replacement" for name in mixed], changed=False)
    try:
        initial_template_mode = not bool(value(doc, "GetEditSheet"))
    except Exception:
        return result(False, "Cannot read the initial sheet editing mode.", changed=False,
                      unsupported_fields=["GetEditSheet"])
    changed, after = [], []
    template_mode = initial_template_mode
    outcome = None
    try:
        for name, text in fields.items():
            asset = matches[name][0]
            format_note = asset.snapshot["owner_type"] == 2
            if format_note != template_mode:
                if _read(doc, "EditTemplate" if format_note else "EditSheet") is False:
                    raise RuntimeError("Drawing refused to enter the required sheet editing mode.")
                template_mode = format_note
            raw = asset.snapshot["raw_text"]
            if (raw if raw is not None else asset.snapshot["text"]) == text:
                continue
            changed.append(name)
            _set_note_content(asset, text)
            snapshot = _note_asset(asset.note, sheet_name, asset.snapshot["view"]).snapshot
            expected = {"raw_text" if raw is not None else "text": text,
                        "formats": asset.snapshot["formats"]}
            mismatch = _different(expected, snapshot)
            after.append(snapshot)
            if mismatch:
                outcome = result(False, "Fields changed, but readback differs.", changed_fields=changed,
                                 mismatch_fields=[f"{name}.{field}" for field in mismatch], notes=after,
                                 rollback_performed=False)
                break
        if outcome is None:
            outcome = result(True, "Existing title fields updated without saving.", changed=bool(changed),
                             changed_fields=changed, notes=after,
                             unsupported_fields={name: found[0].snapshot["unsupported_fields"] for name, found in matches.items()})
    except Exception as exc:
        outcome = result(False, str(exc), changed_fields=changed, partial_artifacts=changed, rollback_performed=False)
    finally:
        if template_mode != initial_template_mode:
            try:
                restore = "EditTemplate" if initial_template_mode else "EditSheet"
                if _read(doc, restore) is False:
                    raise RuntimeError(f"{restore} returned false.")
                if (not bool(value(doc, "GetEditSheet"))) != initial_template_mode:
                    raise RuntimeError("Sheet mode readback differs from the initial mode.")
            except Exception as exc:
                outcome = result(False, f"Fields processed, but restoring sheet mode failed: {exc}",
                                 changed_fields=changed, partial_artifacts=changed, notes=after,
                                 rollback_performed=False)
    return outcome


def _populate_revision_table(source: TableAsset, target: Any) -> None:
    existing = _table_asset(target)
    if (source.snapshot["header_style"] is not None and source.snapshot["header_count"] is not None
            and (source.snapshot["header_style"], source.snapshot["header_count"]) !=
            (existing.snapshot["header_style"], existing.snapshot["header_count"])):
        if _call(target, "SetHeader", source.snapshot["header_style"], source.snapshot["header_count"]) is False:
            raise RuntimeError("Native revision header style could not be restored.")
    for row in existing.snapshot["revision_rows"]:
        if _call(target, "DeleteRevision", row["id"], False) is False:
            raise RuntimeError("Cannot clear revisions created by the template.")
    revisions = source.snapshot["revision_rows"]
    if len(revisions) > 1 and revisions[0]["revision"] == source.snapshot["current_revision"]:
        revisions = list(reversed(revisions))
    for row in revisions:
        revision_id = _call(target, "AddRevision", row["revision"])
        if revision_id is None or int(revision_id) < 0:
            raise RuntimeError(f"AddRevision rejected '{row['revision']}'.")
    if int(value(target, "RowCount")) != source.snapshot["rows"] or int(value(target, "ColumnCount")) != source.snapshot["columns"]:
        raise RuntimeError("Template and revision entries produced different table dimensions.")
    for column, custom_property in enumerate(source.snapshot["column_custom_properties"]):
        if custom_property:
            if _call(target, "SetColumnCustomProperty", column, custom_property) is False:
                raise RuntimeError(f"Column {column} custom property could not be restored.")
    for row, cells in enumerate(source.snapshot["cells"]):
        for column, text in enumerate(cells):
            _raw_cell_put(target, row, column, text)
            fmt = source.formats[row][column]
            use_doc = source.snapshot["use_document_formats"][row][column]
            if fmt is not None and _call(target, "SetCellTextFormat", row, column, bool(use_doc), fmt) is False:
                raise RuntimeError(f"Cell [{row},{column}] text format could not be restored.")
    for column, width in enumerate(source.snapshot["column_widths_m"]):
        if width is not None:
            _call(target, "SetColumnWidth", column, width, 0)
    for row, height in enumerate(source.snapshot["row_heights_m"]):
        if height is not None:
            _call(target, "SetRowHeight", row, height, 0)
    # The native template owns column types, anchor, and border settings.
    # Readback below rejects a template that did not preserve their semantics.


@tool("copy_revision_table", "Copy a native revision table from an already-open drawing using a temporary native template. Compare semantic columns, revision metadata and formatting. No saves.",
      {"expected_document_path": _PATH_PROPERTY, "source_document_path": {"type": "string"},
       "source_sheet": {"type": "string"}, "replace_existing": {"type": "boolean", "default": False}},
      ["expected_document_path", "source_document_path"])
def copy_revision_table(args: dict[str, Any]) -> dict[str, Any]:
    app, doc = require_document_path(args["expected_document_path"], 3)
    _, source_sheet, _ = _source(app, args["expected_document_path"], args["source_document_path"], args.get("source_sheet"))
    source_table = _revision_table(source_sheet)
    if source_table is None:
        return result(False, "Source sheet has no native revision table.", changed=False)
    source = _table_asset(source_table)
    target_sheet, target_sheet_name = _current_sheet(doc)
    target_table = _revision_table(target_sheet)
    before = _table_asset(target_table).snapshot if target_table is not None else None
    required = [field for field in source.snapshot["unsupported_fields"]
                if field.startswith(("column_types", "column_custom_properties", "revision_rows"))
                or field in ("current_revision", "attributes.AnchorType", "attributes.Anchored")]
    if required:
        return result(False, "Cannot copy a revision table without its semantic metadata.",
                      changed=False, unsupported_fields=required, source=source.snapshot)
    if before is not None:
        mismatch = _different(_table_contract(source.snapshot), _table_contract(before))
        if not mismatch:
            return result(True, "Matching native revision table already exists.", changed=False, table=before,
                          unsupported_fields=source.snapshot["unsupported_fields"], fidelity_scope="Observable fields only.")
        if not args.get("replace_existing", False):
            return result(False, "Native revision table differs; explicit replace_existing is required.",
                          changed=False, mismatch_fields=mismatch, source=source.snapshot, before=before)
    partial = []
    try:
        with tempfile.TemporaryDirectory(prefix="sw-revision-") as folder:
            template = Path(folder) / "source.sldrevtbt"
            if _call(source_table, "SaveAsTemplate", str(template)) is False or not template.is_file():
                raise RuntimeError("Source SaveAsTemplate did not create a native revision template.")
            if target_table is not None:
                if int(value(target_table, "Type")) != 3:
                    raise RuntimeError("Refusing to delete a table that is not a revision block.")
                partial.append({"kind": "revision_table_deletion_attempt", "sheet": target_sheet_name, "name": before["name"]})
                _checked_select_delete(doc, target_table)
                partial[-1]["kind"] = "deleted_revision_table"
            attrs = source.snapshot["attributes"]
            position = source.snapshot["position_mm"]
            clear_selection(doc)
            target_table = _call(target_sheet, "InsertRevisionTable2", bool(attrs["Anchored"]),
                                 to_m(position[0]), to_m(position[1]), int(attrs["AnchorType"]),
                                 str(template), 3, False)
            if target_table is None:
                raise RuntimeError("InsertRevisionTable2 returned no native table.")
            partial.append({"kind": "created_revision_table", "sheet": target_sheet_name,
                            "name": str(value(value(target_table, "GetAnnotation"), "GetName"))})
            _populate_revision_table(source, target_table)
            after = _table_asset(target_table).snapshot
            mismatch = _different(_table_contract(source.snapshot), _table_contract(after))
            return result(not mismatch, "Native revision table copied and observable fields compared." if not mismatch
                          else "Native revision table changed, but observable fields differ.", changed=True,
                          source=source.snapshot, before=before, table=after, mismatch_fields=mismatch,
                          unsupported_fields=source.snapshot["unsupported_fields"],
                          partial_artifacts=partial if mismatch else [],
                          fidelity_scope="Observable fields only; vendor attributes, revision symbols and rendered appearance are unverified.")
    except Exception as exc:
        return result(False, str(exc), source=source.snapshot, before=before,
                      partial_artifacts=partial, rollback_performed=False)
