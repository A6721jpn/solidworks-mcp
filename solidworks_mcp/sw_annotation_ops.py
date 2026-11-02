"""Single drawing-text operations with exact targets and before-value guards."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Iterator

from .sw_core import rebuild, require_document_path, result, tool, value
from . import sw_drawing_assets as assets


_PATH = {"type": "string", "minLength": 1,
         "description": "Exact active drawing document_key; no implicit document activation."}
_NAME = {"type": "string", "minLength": 1}
_TEXT = {"type": "string"}
_NOTE_STRUCTURE = ("name", "sheet", "view", "owner_type", "position_mm", "layer",
                   "justification", "attributes")
_REVISION_NUMBER_COLUMN = 302


def _name(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{label} must be a nonempty exact name.")
    return raw


def _text(raw: Any, label: str) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{label} must be a string; an empty string clears text.")
    return raw


def _index(raw: Any, label: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"{label} must be a nonnegative integer.")
    return raw


def _drawing(args: dict[str, Any]) -> tuple[Any, Any]:
    _, doc = require_document_path(_name(args.get("expected_document_path"), "expected_document_path"), 3)
    sheet, current = assets._current_sheet(doc)
    requested = _name(args.get("sheet_name"), "sheet_name")
    if requested != current:
        raise ValueError(f"Current sheet mismatch: expected '{requested}', got '{current}'.")
    return doc, sheet


def _sheet_mode(doc: Any) -> bool:
    mode = value(doc, "GetEditSheet")
    if mode is None:
        raise RuntimeError("Cannot read the drawing's current editing mode.")
    return bool(mode)


def _set_mode(doc: Any, sheet_mode: bool) -> None:
    member = "EditSheet" if sheet_mode else "EditTemplate"
    if assets._read(doc, member) is False or _sheet_mode(doc) != sheet_mode:
        raise RuntimeError(f"Drawing refused {member}.")


@contextmanager
def _editing(doc: Any, initial: bool, desired: bool) -> Iterator[None]:
    try:
        if initial != desired:
            _set_mode(doc, desired)
        yield
    finally:
        try:
            if _sheet_mode(doc) != initial:
                _set_mode(doc, initial)
            if _sheet_mode(doc) != initial:
                raise RuntimeError("Editing mode differs from its initial state.")
        except Exception as exc:
            raise RuntimeError(f"Restoring drawing editing mode failed: {exc}") from exc


def _failure(exc: Exception, attempted: bool, artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    return result(False, str(exc), may_have_changes=attempted, partial_artifacts=artifacts,
                  rollback_performed=False)


def _named_note(doc: Any, sheet: str, name: str) -> assets.NoteAsset:
    matches = assets._notes(doc, sheet, {name})
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one note named '{name}' on '{sheet}', found {len(matches)}.")
    return matches[0]


@tool(
    "set_drawing_note_text",
    "Set one exact named note's complete native PropertyLinkedText, including caller-supplied "
    "formatting tags and property links. Compare the prior raw text, rebuild and reacquire before "
    "readback. Preserve structural attributes and restore edit mode. No saves or inferred content.",
    {"expected_document_path": _PATH, "sheet_name": _NAME, "note_name": _NAME,
     "expected_raw_text": _TEXT, "raw_text": _TEXT},
    ["expected_document_path", "sheet_name", "note_name", "expected_raw_text", "raw_text"],
)
def set_drawing_note_text(args: dict[str, Any]) -> dict[str, Any]:
    attempted, artifacts = False, []
    try:
        expected = _text(args.get("expected_raw_text"), "expected_raw_text")
        requested = _text(args.get("raw_text"), "raw_text")
        name = _name(args.get("note_name"), "note_name")
        doc, _ = _drawing(args)
        sheet = args["sheet_name"]
        note = _named_note(doc, sheet, name)
        before = note.snapshot
        if before["raw_text"] is None:
            return result(False, "Native raw note text is unavailable.", changed=False,
                          unsupported_fields=["raw_text"])
        if before["owner_type"] not in (0, 1, 2):
            return result(False, "The note owner cannot be edited by this drawing operation.", changed=False)
        required = [field for field in before["unsupported_fields"]
                    if field in {"owner_type", "layer", "justification"}
                    or field.startswith("attributes.")]
        required.extend(key for key in ("layer", "justification") if before[key] is None and key not in required)
        if len(before["position_mm"]) != 3:
            required.append("position_mm")
        required.extend(f"attributes.{key}" for key, val in before["attributes"].items()
                        if val is None and f"attributes.{key}" not in required)
        if required:
            return result(False, "Cannot verify note structural attributes.", changed=False,
                          unsupported_fields=required)
        if before["raw_text"] == requested:
            return result(True, "The note already has the requested raw text.", changed=False, note=before)
        if before["raw_text"] != expected:
            return result(False, "Note raw text changed since it was inspected.", changed=False,
                          expected_raw_text=expected, actual_raw_text=before["raw_text"])
        initial = _sheet_mode(doc)
        attempted = True
        with _editing(doc, initial, before["owner_type"] != 2):
            artifacts.append({"kind": "note_text_write", "sheet": sheet, "name": name})
            note.note.PropertyLinkedText = requested
            if not rebuild(doc):
                raise RuntimeError("Note text was written, but rebuilding the drawing failed.")
            after = _named_note(doc, sheet, name).snapshot
            contract = {key: before[key] for key in _NOTE_STRUCTURE}
            contract["raw_text"] = requested
            mismatch = assets._different(contract, after)
            if mismatch:
                outcome = result(False, "Note changed, but readback differs.", changed=True,
                                 mismatch_fields=mismatch, before=before, note=after,
                                 partial_artifacts=artifacts, may_have_changes=True,
                                 rollback_performed=False)
            else:
                outcome = result(True, "Named note raw text updated and structural attributes verified.",
                                 changed=True, before=before, note=after,
                                 verification_scope="Native raw text and structural fields; formatting is supplied by raw_text, rendered appearance is unverified.")
        return outcome
    except Exception as exc:
        return _failure(exc, attempted, artifacts)


def _native_revision_table(sheet: Any, name: str) -> assets.TableAsset:
    table = assets._revision_table(sheet)
    if table is None or int(value(table, "Type")) != 3:
        raise ValueError("The current sheet has no native revision table.")
    asset = assets._table_asset(table)
    if asset.snapshot["name"] != name:
        raise ValueError(f"Revision table name mismatch: expected '{name}', got '{asset.snapshot['name']}'.")
    return asset


def _unread_fields(data: Any, path: str = "") -> list[str]:
    if data is None:
        return [path]
    if isinstance(data, dict):
        return [field for key, val in data.items()
                for field in _unread_fields(val, f"{path}.{key}".strip('.'))]
    if isinstance(data, list):
        return [field for index, val in enumerate(data)
                for field in _unread_fields(val, f"{path}[{index}]".strip('.'))]
    return []


@tool(
    "set_revision_table_cell",
    "Edit exactly one native revision data cell. Resolve the row by native revision ID and label; "
    "guard the column type and prior text. Refuse revision-number and property-bound cells. "
    "Verify all other cells and native metadata after rebuild. No saves or inferred dates/approvals.",
    {"expected_document_path": _PATH, "sheet_name": _NAME, "table_name": _NAME,
     "revision_id": {"type": "integer", "minimum": 0}, "expected_revision": _NAME,
     "column": {"type": "integer", "minimum": 0},
     "expected_column_type": {"type": "integer", "minimum": 0},
     "expected_text": _TEXT, "text": _TEXT},
    ["expected_document_path", "sheet_name", "table_name", "revision_id", "expected_revision",
     "column", "expected_column_type", "expected_text", "text"],
)
def set_revision_table_cell(args: dict[str, Any]) -> dict[str, Any]:
    attempted, artifacts = False, []
    try:
        name = _name(args.get("table_name"), "table_name")
        revision = _name(args.get("expected_revision"), "expected_revision")
        revision_id = _index(args.get("revision_id"), "revision_id")
        column = _index(args.get("column"), "column")
        column_type = _index(args.get("expected_column_type"), "expected_column_type")
        expected = _text(args.get("expected_text"), "expected_text")
        requested = _text(args.get("text"), "text")
        doc, sheet = _drawing(args)
        before_asset = _native_revision_table(sheet, name)
        before = before_asset.snapshot
        unavailable = sorted(set(before["unsupported_fields"] + _unread_fields(before)))
        if unavailable:
            return result(False, "Cannot verify the native revision table metadata.", changed=False,
                          unsupported_fields=unavailable)
        matches = [entry for entry in before["revision_rows"] if entry["id"] == revision_id]
        if len(matches) != 1 or matches[0]["revision"] != revision:
            return result(False, "Native revision ID or label changed since inspection.", changed=False,
                          revision_rows=before["revision_rows"])
        row = matches[0]["row"]
        if column >= before["columns"]:
            raise ValueError("Column is outside the native revision table.")
        actual_type = before["column_types"][column]
        if actual_type != column_type:
            return result(False, "Column type changed since inspection.", changed=False,
                          expected_column_type=column_type, actual_column_type=actual_type)
        if actual_type == _REVISION_NUMBER_COLUMN:
            return result(False, "Revision identifiers require a native revision operation, not a text edit.", changed=False)
        if before["column_custom_properties"][column]:
            return result(False, "Property-bound revision columns must be edited through their property.", changed=False)
        actual = before["cells"][row][column]
        if actual == requested:
            return result(True, "The revision cell already has the requested text.", changed=False,
                          revision_id=revision_id, row=row, column=column, table=before)
        if actual != expected:
            return result(False, "Revision cell text changed since inspection.", changed=False,
                          expected_text=expected, actual_text=actual)
        initial = _sheet_mode(doc)
        attempted = True
        with _editing(doc, initial, True):
            artifacts.append({"kind": "revision_cell_write", "sheet": args["sheet_name"], "table": name,
                              "revision_id": revision_id, "row": row, "column": column})
            assets._raw_cell_put(before_asset.table, row, column, requested)
            if not rebuild(doc):
                raise RuntimeError("Cell text was written, but rebuilding the drawing failed.")
            fresh_sheet, current = assets._current_sheet(doc)
            if current != args["sheet_name"]:
                raise RuntimeError("The active sheet changed during the cell edit.")
            after = _native_revision_table(fresh_sheet, name).snapshot
            contract = deepcopy(before)
            contract["cells"][row][column] = requested
            mismatch = assets._different(contract, after)
            if mismatch:
                outcome = result(False, "Cell changed, but native table readback differs.", changed=True,
                                 mismatch_fields=mismatch, before=before, table=after,
                                 partial_artifacts=artifacts, may_have_changes=True, rollback_performed=False)
            else:
                outcome = result(True, "Revision cell updated and native table metadata verified.", changed=True,
                                 revision_id=revision_id, row=row, column=column, before=before, table=after)
        return outcome
    except Exception as exc:
        return _failure(exc, attempted, artifacts)
