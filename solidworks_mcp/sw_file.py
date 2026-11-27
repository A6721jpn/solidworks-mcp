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

"""Session status, document lifecycle, saving, exporting, and appearance."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from collections import Counter
import math
import re
import unicodedata

import pythoncom
import win32com.client

from .sw_core import (
    as_list,
    active_document,
    byref_long,
    clear_selection,
    document_info,
    document_type,
    document_path_key,
    double_array,
    EXPORT_EXTENSIONS,
    extension,
    flag_methods,
    invoke_no_arg,
    iter_feature_objects,
    logger,
    nothing,
    open_document_by_path,
    OUTPUT_ROOT,
    rebuild,
    require_part,
    require_document_path,
    result,
    running_app,
    discover_template,
    tool,
    value,
    whats_wrong,
)


def validated_output_path(path: str, allowed_extensions: set[str], allow_overwrite: bool = False) -> Path:
    candidate = Path(path).expanduser()
    # A relative path means "inside the output root", which is where everything
    # has to land anyway; the server has no meaningful working directory.
    if not candidate.is_absolute():
        candidate = OUTPUT_ROOT / candidate
    output = candidate.resolve()
    if output.suffix.lower() not in allowed_extensions:
        extensions = ", ".join(sorted(allowed_extensions))
        raise RuntimeError(f"Unsupported output extension. Allowed: {extensions}.")
    try:
        output.relative_to(OUTPUT_ROOT)
    except ValueError as exc:
        raise RuntimeError(f"For safety, output path must be under {OUTPUT_ROOT}.") from exc
    if output.exists() and not allow_overwrite:
        raise RuntimeError(f"Refusing to overwrite existing output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def _save_as(doc: Any, path: str) -> bool:
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    errors, warnings = byref_long(0), byref_long(0)

    # SOLIDWORKS refuses SaveAs onto a path that is already open — including
    # this very document's own path — so that case is a plain Save.
    current = str(value(doc, "GetPathName") or "")
    if current and Path(current).resolve() == output:
        try:
            if bool(doc.Save3(1, errors, warnings)) and int(errors.value) == 0:
                return True
        except Exception:
            logger.info("Save3 on the document's own path failed; trying SaveAs")

    try:
        if bool(extension(doc).SaveAs(str(output), 0, 0, nothing(), errors, warnings)) and int(errors.value) == 0:
            return True
    except Exception:
        logger.info("Extension.SaveAs failed; falling back to ModelDoc2.SaveAs")
    try:
        return bool(doc.SaveAs(str(output)))
    except Exception:
        return False


@tool(
    "solidworks_status",
    "Read-only: verify attachment to the running SOLIDWORKS instance and report active-document status.",
    {},
)
def solidworks_status(args: dict[str, Any]) -> dict[str, Any]:
    app = running_app()
    active = app.ActiveDoc
    payload = result(
        True,
        "Attached to the running SOLIDWORKS session.",
        version=str(value(app, "RevisionNumber")),
        document_count=int(value(app, "GetDocumentCount")),
        output_root=str(OUTPUT_ROOT),
    )
    if active is not None:
        payload["data"]["active_document"] = document_info(active)
    return payload


@tool(
    "get_active_document_info",
    "Read-only: return the active SOLIDWORKS document title, path, type, and unsaved state.",
    {},
)
def get_active_document_info(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = active_document()
    return result(True, "Read active document.", document=document_info(doc))


@tool(
    "inspect_document_references",
    "Read the saved model references of a drawing without opening it or switching documents.",
    {"path": {"type": "string", "description": "Absolute path to a saved SOLIDWORKS drawing."}},
    ["path"],
)
def inspect_document_references(args: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(args["path"])).expanduser()
    if not path.is_absolute() or path.suffix.lower() != ".slddrw":
        raise RuntimeError("Provide an absolute path to a saved SOLIDWORKS drawing.")
    if not path.is_file():
        raise RuntimeError(f"Drawing does not exist: {path}")

    app = running_app()
    flag_methods(app, "GetDocumentDependencies2")
    raw = app.GetDocumentDependencies2(str(path), False, False, False)
    if raw is not None and not isinstance(raw, (tuple, list)):
        raise RuntimeError("SOLIDWORKS returned incomplete drawing reference data.")
    entries = list(raw) if raw is not None else []
    if len(entries) % 2 or any(not isinstance(item, str) or not item for item in entries):
        raise RuntimeError("SOLIDWORKS returned incomplete drawing reference data.")
    references = [
        {"name": entries[i], "path": entries[i + 1], "exists": Path(entries[i + 1]).is_file()}
        for i in range(0, len(entries), 2)
    ]
    message = "Read saved drawing references." if references else "SOLIDWORKS returned no saved drawing references."
    return result(True, message, drawing_path=str(path), references=references)


@tool(
    "replace_drawing_reference",
    "Replace one saved part reference in a closed drawing and verify the saved paths. "
    "This changes the drawing file; reopen it separately to check its views and dimensions.",
    {
        "drawing_path": {"type": "string", "description": "Absolute path to the closed drawing to change."},
        "old_reference_path": {"type": "string", "description": "Expected saved path of the part being replaced."},
        "new_reference_path": {"type": "string", "description": "Absolute path to the existing replacement part."},
    },
    ["drawing_path", "old_reference_path", "new_reference_path"],
)
def replace_drawing_reference(args: dict[str, Any]) -> dict[str, Any]:
    drawing = Path(str(args["drawing_path"])).expanduser()
    old = Path(str(args["old_reference_path"])).expanduser()
    new = Path(str(args["new_reference_path"])).expanduser()
    if not drawing.is_absolute() or drawing.suffix.lower() != ".slddrw" or not drawing.is_file():
        raise RuntimeError("Provide an absolute path to an existing drawing.")
    if any(not path.is_absolute() or path.suffix.lower() != ".sldprt" for path in (old, new)):
        raise RuntimeError("Both reference paths must be absolute SOLIDWORKS part paths.")
    if not new.is_file():
        raise RuntimeError(f"Replacement part does not exist: {new}")
    old_key, new_key = document_path_key(str(old)), document_path_key(str(new))
    if old_key == new_key:
        raise RuntimeError("The old and new reference paths must be different.")

    app = flag_methods(running_app(), "GetOpenDocumentByName", "ReplaceReferencedDocument")
    if app.GetOpenDocumentByName(str(drawing)) is not None:
        raise RuntimeError("Close the drawing before replacing its saved reference.")
    before = inspect_document_references({"path": str(drawing)})["data"]["references"]
    old_matches = [ref for ref in before if document_path_key(ref["path"]) == old_key]
    if len(old_matches) != 1:
        raise RuntimeError("The expected old part reference was not found exactly once.")
    if any(document_path_key(ref["path"]) == new_key for ref in before):
        raise RuntimeError("The replacement part is already referenced by the drawing.")

    expected = Counter(document_path_key(ref["path"]) for ref in before)
    expected[old_key] -= 1
    if expected[old_key] == 0:
        del expected[old_key]
    expected[new_key] += 1
    try:
        replaced = bool(app.ReplaceReferencedDocument(str(drawing), old_matches[0]["path"], str(new)))
        after = inspect_document_references({"path": str(drawing)})["data"]["references"]
        actual = Counter(document_path_key(ref["path"]) for ref in after)
        replacement_exists = any(document_path_key(ref["path"]) == new_key and ref["exists"] for ref in after)
        if not replaced or actual != expected or not replacement_exists:
            return result(False, "The drawing reference change could not be verified.",
                          drawing_path=str(drawing), may_have_changes=True, references=after)
    except Exception as exc:
        return result(False, f"The drawing reference change could not be verified: {exc}",
                      drawing_path=str(drawing), may_have_changes=True)
    return result(True, "Replaced the saved reference. Reopen the drawing to check views and dimensions.",
                  drawing_path=str(drawing), old_reference_path=old_matches[0]["path"],
                  new_reference_path=str(new), references=after)


@tool(
    "create_new_document",
    "Create a new SOLIDWORKS part, assembly, or drawing from the configured default template.",
    {"kind": {"type": "string", "enum": ["part", "assembly", "drawing"]}},
    ["kind"],
)
def create_new_document(args: dict[str, Any]) -> dict[str, Any]:
    return new_document(str(args["kind"]))


def new_document(kind: str) -> dict[str, Any]:
    app = running_app()
    preference = {"part": 1, "assembly": 2, "drawing": 3}.get(kind)
    if preference is None:
        return result(False, "kind must be part, assembly, or drawing.")
    template = app.GetUserPreferenceStringValue(preference)
    if not template or not Path(str(template)).is_file():
        fallback = discover_template(kind)
        if fallback is None:
            return result(
                False,
                f"No default {kind} template is configured in SOLIDWORKS and none could be found on disk. "
                "Set one under Tools > Options > File Locations, or point SW_MCP_TEMPLATE_DIR at your templates.",
            )
        template = str(fallback)
    doc = app.NewDocument(template, 0, 0.0, 0.0)
    if doc is None:
        return result(False, f"SOLIDWORKS did not create a {kind} document.")
    return result(True, f"Created new {kind} document.", document=document_info(doc))


@tool(
    "open_document",
    "Open a native SOLIDWORKS or STEP file. With reload=true, reload the exact active "
    "native document from disk only if it has no unsaved changes.",
    {"path": {"type": "string"},
     "reload": {"type": "boolean", "default": False,
                "description": "Force-reload the exact active native document; refuses unsaved changes."}},
    ["path"],
)
def open_document(args: dict[str, Any]) -> dict[str, Any]:
    app = running_app()
    path = Path(str(args["path"])).expanduser().resolve()
    if not path.is_file():
        return result(False, f"No such file: {path}")
    doc_type = {".sldprt": 1, ".sldasm": 2, ".slddrw": 3}.get(path.suffix.lower())
    if args.get("reload", False):
        if doc_type not in (1, 2):
            return result(False, "Reload is supported only for active native part and assembly documents.")
        doc = app.ActiveDoc
        if doc is None or document_path_key(str(value(doc, "GetPathName") or "")) != document_path_key(str(path)):
            return result(False, "Reload requires the exact active document path.")
        if bool(value(doc, "GetSaveFlag")):
            return result(False, "Refusing to reload a document with unsaved changes.", document=document_info(doc))
        read_only = bool(value(flag_methods(doc, "IsOpenedReadOnly"), "IsOpenedReadOnly"))
        ext = flag_methods(extension(doc), "ReloadOrReplace")
        status = int(ext.ReloadOrReplace(read_only, "", False, True))
        current = app.ActiveDoc
        if status != 0:
            return result(False, "SOLIDWORKS did not reload the requested document.", error_code=status,
                          active_document=document_info(current) if current is not None else None)
        if current is None or document_path_key(str(value(current, "GetPathName") or "")) != document_path_key(str(path)):
            try:
                documents = as_list(value(app, "GetDocuments"))
                matches = [d for d in documents if document_path_key(str(value(d, "GetPathName") or "")) == document_path_key(str(path))]
                if len(matches) != 1:
                    raise RuntimeError("Could not uniquely reacquire the reloaded document by its path.")
                reloaded = matches[0]
                title = str(value(reloaded, "GetTitle"))
                if sum(str(value(d, "GetTitle")).casefold() == title.casefold() for d in documents) != 1:
                    raise RuntimeError("The reloaded document's title is ambiguous.")
                activation_errors = byref_long(0)
                flag_methods(app, "ActivateDoc3", "IsSame")
                activated = app.ActivateDoc3(title, False, 1, activation_errors)
                current = app.ActiveDoc
                if (activated is None or current is None
                        or document_path_key(str(value(current, "GetPathName") or "")) != document_path_key(str(path))
                        or int(app.IsSame(current, reloaded)) != 1
                        or int(app.IsSame(activated, reloaded)) != 1):
                    raise RuntimeError(f"Activation did not select the reloaded document (status {activation_errors.value}).")
            except Exception as exc:
                current = app.ActiveDoc
                return result(False, f"The document was reloaded but could not be made active: {exc}",
                              reloaded=True, activated=False, error_code=status,
                              active_document=document_info(current) if current is not None else None)
        return result(True, f"Reloaded {path} without discarding changes.",
                      reloaded=True, activated=True, document=document_info(current), read_only=read_only)
    if path.suffix.lower() in {".step", ".stp"}:
        return _open_step(app, path)
    if doc_type is None:
        return result(False, "Only native SOLIDWORKS and STEP files can be opened.")
    errors, warnings = byref_long(0), byref_long(0)
    doc = app.OpenDoc6(str(path), doc_type, 0, "", errors, warnings)
    if doc is None:
        return result(False, f"SOLIDWORKS could not open {path}.", error_code=int(errors.value))
    # OpenDoc6 returns an already-open document without activating it.  That
    # left the next tool operating on whichever drawing or part happened to be
    # active, despite this tool promising otherwise.
    activation_errors = byref_long(0)
    activated = app.ActivateDoc3(str(value(doc, "GetTitle")), False, 0, activation_errors)
    current = app.ActiveDoc
    matches = current is not None and document_path_key(str(value(current, "GetPathName") or "")) == document_path_key(str(path))
    if activated is None or not matches:
        return result(
            False,
            f"Opened {path}, but SOLIDWORKS could not make it active.",
            error_code=int(activation_errors.value),
            document=document_info(doc),
            active_document=document_info(current) if current is not None else None,
        )
    return result(True, f"Opened and activated {path}.", document=document_info(current))


_STEP_PRODUCT = re.compile(rb"#\d+\s*=\s*PRODUCT\s*\(\s*'((?:''|[^'])*)'\s*,\s*'((?:''|[^'])*)'", re.I)
_STEP_PRODUCT_START = re.compile(rb"#\d+\s*=\s*PRODUCT\s*\(", re.I)
_STEP_UNICODE = re.compile(r"\\X([24])\\([0-9A-F]+)\\X0\\", re.I)


def _decode_step_name(raw: bytes) -> str:
    text = raw.replace(b"''", b"'").decode("ascii")
    decoded = []
    index = 0
    while index < len(text):
        if text[index] != "\\":
            decoded.append(text[index])
            index += 1
            continue
        segment = _STEP_UNICODE.match(text, index)
        if segment is None:
            raise ValueError("Unsupported or malformed STEP string escape")
        width, hexadecimal = segment.groups()
        if len(hexadecimal) % (4 if width == "2" else 8):
            raise ValueError("Invalid STEP Unicode code-unit length")
        decoded.append(bytes.fromhex(hexadecimal).decode("utf-16-be" if width == "2" else "utf-32-be"))
        index = segment.end()
    result_text = "".join(decoded)
    if any(ord(c) < 32 or 127 <= ord(c) < 160 for c in result_text):
        raise ValueError("Control character in STEP component name")
    return result_text


def _step_name_key(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).strip().casefold()
    for suffix in (".sldprt", ".sldasm", ".slddrw", ".step", ".stp"):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
    return text


def _step_collisions(path: Path, documents: list[Any]) -> tuple[list[str], str | None]:
    if not documents:
        return [], None
    try:
        if path.stat().st_size > 256 * 1024 * 1024:
            return [], "STEP metadata is too large to check safely while other documents are open."
        data = path.read_bytes()
    except OSError as exc:
        return [], f"Could not inspect STEP component identities: {exc}"
    products = list(_STEP_PRODUCT.finditer(data))
    if not products or len(products) != len(_STEP_PRODUCT_START.findall(data)):
        return [], "STEP component identities could not be determined while other documents are open."
    names = {_step_name_key(path.name)}
    for product in products:
        for raw in product.groups():
            try:
                text = _decode_step_name(raw).strip()
            except (UnicodeError, ValueError):
                return [], "Encoded STEP component names cannot be checked safely while other documents are open."
            if text and any(char in text for char in '\\/:*?"<>|'):
                return [], "STEP component names cannot be checked safely while other documents are open."
            if text:
                names.add(_step_name_key(text))
    collisions = []
    for doc in documents:
        title = str(value(doc, "GetTitle") or "")
        if not title:
            return [], "An open document has no identifiable title; STEP collisions cannot be excluded."
        if _step_name_key(title) in names:
            collisions.append(title)
    return sorted(set(collisions)), None


def _open_step(app: Any, path: Path) -> dict[str, Any]:
    try:
        documents = as_list(value(app, "GetDocuments"))
        collisions, problem = _step_collisions(path, documents)
    except Exception as exc:
        return result(False, f"Could not check open documents for STEP import conflicts: {exc}")
    if problem:
        return result(False, problem)
    if collisions:
        return result(False, "STEP import could conflict with open documents; no file was imported.",
                      conflicting_documents=collisions)
    flag_methods(app, "GetImportFileData", "LoadFile4", "IsSame")
    import_data = app.GetImportFileData(str(path))
    if import_data is None:
        return result(False, "SOLIDWORKS did not provide STEP import data.")
    errors = byref_long(0)
    imported = app.LoadFile4(str(path), "r", import_data, errors)
    if imported is None:
        return result(False, f"SOLIDWORKS could not import {path}.", error_code=int(errors.value))
    current = app.ActiveDoc
    if current is None or int(app.IsSame(imported, current)) != 1:
        activation_errors = byref_long(0)
        app.ActivateDoc3(str(value(imported, "GetTitle")), False, 0, activation_errors)
        current = app.ActiveDoc
    if current is None or int(app.IsSame(imported, current)) != 1:
        return result(False, "STEP imported, but its document could not be verified as active.",
                      imported=True, source_path=str(path), document=document_info(imported),
                      error_code=int(errors.value))
    return result(True, f"Imported and activated {path}.", source_path=str(path),
                  document=document_info(current), error_code=int(errors.value))


@tool(
    "save_document",
    "Save the active document as a new .sldprt, .sldasm, or .slddrw file under the designated "
    "outputs folder. Existing files are never overwritten unless overwrite is true.",
    {"path": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}},
    ["path"],
)
def save_document(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = active_document()
    output = validated_output_path(
        str(args["path"]), {".sldprt", ".sldasm", ".slddrw"}, bool(args.get("overwrite", False))
    )
    clear_selection(doc)
    saved = _save_as(doc, str(output))
    if saved:
        return result(True, f"Saved document to {output}.", path=str(output))
    return result(False, _save_failure_reason(doc, output), path=str(output))


def _save_failure_reason(doc: Any, output: Path) -> str:
    """SOLIDWORKS refuses a save-as onto a file it already has open elsewhere."""
    try:
        for other in as_list(value(running_app(), "GetDocuments")):
            if other is doc:
                continue
            existing = str(value(other, "GetPathName") or "")
            if existing and Path(existing).resolve() == output:
                title = str(value(other, "GetTitle"))
                return (
                    f"SOLIDWORKS already has '{title}' open from {output}, and will not save over "
                    "a document that is open. Close that document first, or save to another name."
                )
    except Exception:
        pass
    return "SOLIDWORKS failed to save the document."


@tool(
    "save_active_document",
    "Save changes to the active document's existing file path. This cannot choose another path.",
    {},
)
def save_active_document(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = active_document()
    path = str(value(doc, "GetPathName") or "")
    if not path:
        return result(False, "The active document has no saved path; use save_document first.")
    errors, warnings = byref_long(0), byref_long(0)
    saved = bool(doc.Save3(1, errors, warnings)) and int(errors.value) == 0
    return result(
        saved,
        f"Saved active document to {path}." if saved else "SOLIDWORKS failed to save the active document.",
        path=path,
    )


@tool(
    "copy_part_document",
    "Save the active part's current geometry to a new output file without changing its path. "
    "Restore an active sketch if native SaveAs exits it. Never overwrite or save the source.",
    {"expected_document_path": {"type": "string"}, "path": {"type": "string"}},
    ["expected_document_path", "path"],
)
def copy_part_document(args: dict[str, Any]) -> dict[str, Any]:
    app, doc = require_document_path(str(args["expected_document_path"]), 1)
    output = validated_output_path(str(args["path"]), {".sldprt"})
    before = document_info(doc)
    sketch = value(doc.SketchManager, "ActiveSketch")
    sketch_name = None
    if sketch is not None:
        for feature in iter_feature_objects(doc):
            if str(value(feature, "GetTypeName2")) not in {"ProfileFeature", "3DProfileFeature"}:
                continue
            candidate = value(feature, "GetSpecificFeature2")
            if candidate is not None and int(flag_methods(app, "IsSame").IsSame(sketch, candidate)) == 1:
                sketch_name = str(value(feature, "Name"))
                break
        if sketch_name is None:
            return result(False, "Cannot identify the open sketch; source was not copied.")
    errors, warnings = byref_long(0), byref_long(0)
    saved, failure = False, None
    try:
        saved = bool(extension(doc).SaveAs(str(output), 0, 3, nothing(), errors, warnings))
    except Exception as exc:
        failure = str(exc)
    finally:
        if sketch_name and value(doc.SketchManager, "ActiveSketch") is None:
            feature = doc.FeatureByName(sketch_name)
            try:
                if feature is None or not flag_methods(feature, "Select2").Select2(False, 0):
                    raise RuntimeError("Could not select the source sketch for restoration.")
                doc.EditSketch()
            except Exception as exc:
                failure = f"Source sketch restoration failed: {exc}"
    after = document_info(doc)
    active = value(doc.SketchManager, "ActiveSketch")
    after_sketch = None
    if active is not None and sketch_name:
        expected_sketch = value(doc.FeatureByName(sketch_name), "GetSpecificFeature2")
        if int(flag_methods(app, "IsSame").IsSame(active, expected_sketch)) == 1:
            after_sketch = sketch_name
    source_unchanged = document_path_key(before["path"]) == document_path_key(after["path"])
    dirty_preserved = not before["dirty"] or after["dirty"]
    ok = (saved and int(errors.value) == 0 and output.is_file() and source_unchanged
          and dirty_preserved and after_sketch == sketch_name and failure is None)
    return result(
        ok,
        "Copied part without saving the source." if ok else (failure or "Part copy failed verification."),
        path=str(output), copied=saved and output.is_file(), source_before=before,
        source_after=after, source_path_preserved=source_unchanged,
        unsaved_state_preserved=dirty_preserved,
        sketch_before=sketch_name, sketch_after=after_sketch,
        error_code=int(errors.value), warning_code=int(warnings.value),
    )


@tool(
    "insert_part_body",
    "Copy exactly one named solid from an already-open source part into the active target "
    "part at its original coordinates, independently of the source. Both parts must already "
    "be in the named configurations. Reads the source's current geometry, including unsaved "
    "changes. Does not switch configurations, save, or create a source link. The new feature "
    "is suppressed in other target configurations.",
    {"expected_document_path": {"type": "string", "minLength": 1},
     "target_configuration": {"type": "string", "minLength": 1},
     "source_document_path": {"type": "string", "minLength": 1},
     "source_configuration": {"type": "string", "minLength": 1},
     "source_body_name": {"type": "string", "minLength": 1},
     "feature_name": {"type": "string", "minLength": 1}},
    ["expected_document_path", "target_configuration", "source_document_path",
     "source_configuration", "source_body_name", "feature_name"],
)
def insert_part_body(args: dict[str, Any]) -> dict[str, Any]:
    from .sw_sheet import (_body_box_mm, _configuration_array, _configurations,
                           _features, _name, _rename, _suppression,
                           _unused_feature_name, _volume_mm3)
    from .sw_surface_ops import _feature_bodies

    doc = None
    attempted = False
    before_features: set[str] | None = None
    created_name = None
    try:
        expected = _name(args.get("expected_document_path"), "expected_document_path")
        source_path = _name(args.get("source_document_path"), "source_document_path")
        target_config = _name(args.get("target_configuration"), "target_configuration")
        source_config = _name(args.get("source_configuration"), "source_configuration")
        body_name = _name(args.get("source_body_name"), "source_body_name")
        feature_name = _name(args.get("feature_name"), "feature_name")
        if document_path_key(expected) == document_path_key(source_path):
            raise ValueError("Source and target must be different part documents.")
        app, doc = require_document_path(expected, 1)
        configs, active = _configurations(doc)
        if active != target_config:
            raise ValueError(f"Target configuration mismatch: expected '{target_config}', got '{active}'.")
        source = open_document_by_path(app, source_path)
        if document_type(source) != 1:
            raise ValueError("The source document must be a part.")
        _, source_active = _configurations(source)
        if source_active != source_config:
            raise ValueError(f"Source configuration mismatch: expected '{source_config}', got '{source_active}'.")
        if value(value(doc, "SketchManager"), "ActiveSketch") is not None:
            raise ValueError("Close the target sketch before inserting a body.")
        if value(value(source, "SketchManager"), "ActiveSketch") is not None:
            raise ValueError("Close the source sketch before copying its body.")
        _unused_feature_name(doc, feature_name)
        solids = as_list(flag_methods(source, "GetBodies2").GetBodies2(0, False))
        matches = [b for b in solids if str(value(b, "Name")) == body_name]
        if len(matches) != 1:
            raise ValueError(f"Source solid '{body_name}' must resolve uniquely; found {len(matches)}.")
        source_body = matches[0]
        if int(value(source_body, "Check2")) != 0:
            raise ValueError("The source solid has geometry faults.")
        source_box, source_volume = _body_box_mm(source_body), _volume_mm3(source_body)

        def summaries(part: Any, kind: int) -> list[dict[str, Any]]:
            bodies = as_list(flag_methods(part, "GetBodies2").GetBodies2(kind, False))
            if kind == 1:
                return [{"name": str(value(b, "Name")), "box": _body_box_mm(b)} for b in bodies]
            return [{"name": str(value(b, "Name")), "box": _body_box_mm(b),
                     "volume": _volume_mm3(b)} for b in bodies]

        before_solids, before_surfaces = summaries(doc, 0), summaries(doc, 1)
        names = [item["name"] for item in before_solids]
        if len(names) != len(set(names)):
            raise ValueError("Target solid body names must be unique for verification.")
        before_features = {str(value(f, "Name")) for f in _features(doc)}
        source_dirty = bool(value(source, "GetSaveFlag"))
        temporary = value(source_body, "Copy")
        if temporary is None or not bool(value(temporary, "IsTemporaryBody")):
            raise RuntimeError("Could not obtain a temporary copy of the source solid.")
        attempted = True
        feature = flag_methods(doc, "CreateFeatureFromBody3").CreateFeatureFromBody3(temporary, False, 1)
        if feature is None:
            raise RuntimeError("SOLIDWORKS did not return an imported body feature.")
        created_name = str(value(feature, "Name"))
        _rename(feature, feature_name)
        created_name = feature_name

        desired = {name: name != target_config for name in configs}
        for _ in range(2 * len(configs) + 1):
            states = _suppression(feature, configs)
            wrong = [name for name in configs if states[name] != desired[name]]
            if not wrong:
                break
            name = wrong[0]
            action = 0 if desired[name] else 1
            if not flag_methods(feature, "SetSuppression2").SetSuppression2(
                    action, 3, _configuration_array([name])):
                raise RuntimeError(f"Could not set new feature suppression in '{name}'.")
        else:
            raise RuntimeError("Could not isolate the new feature in the requested configuration.")
        states = _suppression(feature, configs)
        if states != desired:
            raise RuntimeError("New feature suppression differs from the requested scope.")
        if _configurations(doc)[1] != target_config or _configurations(source)[1] != source_config:
            raise RuntimeError("A part configuration changed unexpectedly.")
        output = _feature_bodies(feature, 0)
        if len(output) != 1:
            raise RuntimeError(f"Expected one new solid, found {len(output)}.")
        inserted = output[0]
        inserted_box, inserted_volume = _body_box_mm(inserted), _volume_mm3(inserted)
        close = lambda a, b: math.isclose(a, b, rel_tol=1e-7, abs_tol=1e-3)
        if (not all(close(a, b) for a, b in zip(source_box, inserted_box))
                or not math.isclose(source_volume, inserted_volume, rel_tol=1e-7, abs_tol=1e-6)):
            raise RuntimeError("Inserted solid does not match the source bounding box and volume.")
        if int(value(inserted, "Check2")) != 0:
            raise RuntimeError("The inserted solid has geometry faults.")
        after_solids, after_surfaces = summaries(doc, 0), summaries(doc, 1)
        if len(after_solids) != len(before_solids) + 1 or len(after_surfaces) != len(before_surfaces):
            raise RuntimeError("Target body counts do not match a single solid insertion.")
        for before in before_solids:
            found = [b for b in after_solids if b["name"] == before["name"]]
            if (len(found) != 1 or not all(close(a, b) for a, b in zip(before["box"], found[0]["box"]))
                    or not math.isclose(before["volume"], found[0]["volume"], rel_tol=1e-7, abs_tol=1e-6)):
                raise RuntimeError(f"Target body '{before['name']}' failed preservation checks.")
        for before in before_surfaces:
            found = [b for b in after_surfaces if b["name"] == before["name"]]
            if len(found) != 1 or not all(close(a, b) for a, b in zip(before["box"], found[0]["box"])):
                raise RuntimeError(f"Target surface '{before['name']}' failed preservation checks.")
        return result(True, "Copied one independent solid without saving.",
                      feature=feature_name, source_document_path=source_path,
                      source_configuration=source_config, source_body=body_name,
                      source_dirty=source_dirty, configuration=target_config,
                      inserted_body=str(value(inserted, "Name")), bounding_box_mm=inserted_box,
                      volume_mm3=inserted_volume, solid_count_before=len(before_solids),
                      solid_count_after=len(after_solids), surface_count=len(after_surfaces),
                      suppressed=states, target_body_checks="names, bounding boxes, volumes and counts",
                      independent=True, saved=False)
    except Exception as exc:
        artifacts = []
        if attempted and doc is not None and before_features is not None:
            try:
                artifacts = [str(value(f, "Name")) for f in _features(doc)
                             if str(value(f, "Name")) not in before_features]
            except Exception:
                if created_name:
                    artifacts = [created_name]
        return result(False, str(exc), may_have_changes=attempted,
                      partial_features=artifacts, rollback_performed=False, saved=False)


@tool(
    "export_document",
    "Export the active document to STEP/STP, IGES/IGS, STL, Parasolid X_T/X_B, 3MF, or an image of "
    "the current view, under the designated outputs folder.",
    {"path": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}},
    ["path"],
)
def export_document(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = active_document()
    output = validated_output_path(str(args["path"]), EXPORT_EXTENSIONS, bool(args.get("overwrite", False)))
    clear_selection(doc)
    exported = _save_as(doc, str(output))
    return result(
        exported,
        f"Exported document to {output}." if exported else "SOLIDWORKS failed to export the document.",
        path=str(output),
    )


@tool(
    "rebuild_document",
    "Force a rebuild of the active document and report any feature that fails.",
    {"force_all": {"type": "boolean", "default": False, "description": "Rebuild every feature, not just the stale ones."}},
)
def rebuild_document(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = active_document()
    if bool(args.get("force_all", False)):
        try:
            rebuilt = bool(extension(doc).ForceRebuildAll())
        except Exception:
            rebuilt = rebuild(doc)
    else:
        rebuilt = rebuild(doc)
    problems = whats_wrong(doc)
    return result(
        rebuilt and not problems,
        "Rebuilt the active document." if rebuilt and not problems else "The rebuild reported problems.",
        problems=problems,
    )


@tool(
    "set_appearance",
    "Set the display colour of the active part, or of the selected faces or features. "
    "RGB components are 0-255.",
    {
        "red": {"type": "integer", "minimum": 0, "maximum": 255},
        "green": {"type": "integer", "minimum": 0, "maximum": 255},
        "blue": {"type": "integer", "minimum": 0, "maximum": 255},
        "transparency": {"type": "number", "minimum": 0, "maximum": 1, "default": 0},
        "selection": {
            "type": "object",
            "description": "Optional: colour only these faces. Omit to colour the whole model.",
            "properties": {"faces": {"type": "array", "items": {"type": "integer"}}},
        },
    },
    ["red", "green", "blue"],
)
def set_appearance(args: dict[str, Any]) -> dict[str, Any]:
    from .sw_core import as_list, enumerate_faces, get_bodies, safe

    _, doc = require_part()
    values = (
        int(args["red"]) / 255.0, int(args["green"]) / 255.0, int(args["blue"]) / 255.0,
        1.0, 1.0, 0.4, 0.3, float(args.get("transparency", 0)), 0.0,
    )
    face_indices = (args.get("selection") or {}).get("faces")
    if face_indices:
        faces = [f for body in get_bodies(doc) for f in as_list(safe(body, "GetFaces"))]
        for raw_index in face_indices:
            index = int(raw_index)
            if not 0 <= index < len(faces):
                return result(False, f"Face index {index} is out of range (0..{len(faces) - 1}).")
            faces[index].MaterialPropertyValues = double_array(values)
        target = f"{len(face_indices)} faces"
    else:
        doc.MaterialPropertyValues = double_array(values)
        target = "the whole model"
    try:
        invoke_no_arg(doc, "GraphicsRedraw2")
    except Exception:
        pass
    return result(True, f"Applied the colour to {target}.", rgb=[args["red"], args["green"], args["blue"]])


@tool(
    "set_material",
    "Assign a SOLIDWORKS material to the active part, which is what makes get_mass_properties "
    "report a real mass. Example: database 'SOLIDWORKS Materials', name 'AISI 1020'.",
    {
        "name": {"type": "string", "description": "Material name exactly as it appears in the material library."},
        "database": {"type": "string", "default": "SOLIDWORKS Materials"},
    },
    ["name"],
)
def set_material(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    name = str(args["name"])
    database = str(args.get("database", "SOLIDWORKS Materials"))
    try:
        doc.SetMaterialPropertyName2("", database, name)
    except Exception as exc:
        return result(False, f"SOLIDWORKS rejected that material: {exc}")
    applied = ""
    try:
        applied = str(doc.GetMaterialPropertyName2("", database) or "")
    except Exception:
        pass
    rebuild(doc)
    # An unknown material name is accepted silently and leaves the part
    # unassigned, so treat anything but an exact read-back as a failure.
    if applied != name:
        return result(
            False,
            f"SOLIDWORKS did not apply '{name}'; the part reports "
            f"'{applied or '<unspecified>'}'. Check the exact name in the material library.",
            material=applied,
        )
    return result(True, f"Applied material '{name}'.", material=applied)
