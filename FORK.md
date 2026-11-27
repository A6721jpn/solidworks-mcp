# About this fork

This is A6721jpn's modified fork of solidworks-mcp by JIALE LIU.
It is based on upstream commit `6019ce0` and retains the Apache License 2.0,
NOTICE, and upstream copyright notices.

Upstream: https://github.com/Slacker-LLC/solidworks-mcp
The original source and attribution used the `limuzi013` GitHub account.
GitHub currently identifies `Slacker-LLC/solidworks-mcp` as this fork's parent.

## Additions

- `model_snapshot` returns document state in one response.
- `model_diff` compares document state with a saved checkpoint.
- `run_script` batches Python operations against the active SOLIDWORKS session.
- Recipe tools save and rerun named scripts.
- `run_macro` runs an existing SOLIDWORKS macro.
- `dismiss_dialog` handles modal dialogs.
- Mutating tool responses include model differences.
- `selection.refs` identifies entities using persistent references.
- Compact output and paginated geometry lists reduce response size.
- A dialog watchdog reports dialogs encountered during tool execution.
- Additional drawing, sheet, annotation, surface and deformation tools.
- A local CLI shares the MCP dispatcher and serializes concurrent operations.
- Project-local coverage audit tools support tracking CAD operation coverage.

## Modified upstream files

| File | Changes |
| --- | --- |
| `solidworks_mcp/server.py` | Register fork tools, return compact output and model differences, and monitor dialogs. |
| `solidworks_mcp/sw_core.py` | Use late-bound COM dispatch, persistent references, and dimension fixes. |
| `solidworks_mcp/sw_inspect.py` | Add geometry pagination and reference handles. |
| `solidworks_mcp/sw_sketch.py` | Normalize dimension names and correct angular and driven-dimension reporting. |
| `solidworks_mcp/sw_assembly.py`, `sw_drawing.py`, `sw_file.py`, `sw_refgeom.py` | Extend document and drawing operations and correct imports. |
| `README.md` | Identify this fork and document installation and script permissions. |
| `pyproject.toml` | Point project links at this fork and retain the upstream link. |
| `.gitignore` | Exclude local data, CAD files, logs, and credentials. |
| `RELEASING.md` | Document source-only distribution. |
| `.github/workflows/tests.yml` | Run offline CI on the `codex-enhancements` branch as well as `main`. |

The fork adds `sw_snapshot.py`, `sw_script.py`, `sw_watchdog.py`, offline tests,
an optional live regression script, and a benchmark script.
The upstream package publishing workflow and registry manifest are removed.
The upstream offline test workflow is retained.

## Scoped sketch edits

Use `inspect_sketch` to read a named sketch's dimensions, geometric relations,
and dependent equations. It resolves the feature directly and leaves the active
sketch and configuration unchanged. Use `relation_types` and the `include_*`
options to bound the information requested. Relations with external or ambiguous
entity identities remain visible but have no deletion reference.

| Tool | Behavior |
| --- | --- |
| `delete_sketch_relations` | Remove inspected relation signatures from the already-open named sketch. Stale or ambiguous signatures are refused. |
| `rename_dimension` | Rename an existing dimension after checking for a name collision. |
| `set_dimensions` | Validate all typed updates before writing; update active or all configurations; rebuild once outside an open sketch. |
| `set_equation` | Add or compare-and-set one quoted equation target. Updating requires the exact prior equation text. Multi-configuration documents require explicit `all` scope. |
| `copy_part_document` | Copy current part geometry to a new output file, retain the source path and unsaved changes, and restore an open sketch if SaveAs exits it. |

Each tool requires `expected_document_path`. Standard length inputs use mm and
angle inputs use degrees; fields named `system_value` or `system_values` are
native metres/radians, with the dimension kind reported alongside them.
The new tools never save the source. Legitimate mutations can make a clean
document dirty. Bulk operations stop at a native failure and report partial
changes; they do not promise rollback. Local `diff_scope` explicitly says that
downstream geometry has not been checked. Use native model verification after
editing a meaningful unit of work.

`set_dimensions` verifies values in the active configuration. An `all` write
uses the native all-configuration setter and explicitly lists inactive
configurations as unverified. SOLIDWORKS can return zero for inactive sketch
dimensions even when the stored value is correct. The tool leaves the sketch
and configuration untouched; use `set_configuration` and model verification
when other configurations also need to be checked.

These tools return scoped native readback rather than collecting the whole
model twice. Unrestricted `run_script` keeps its full automatic model snapshots;
its existing `call()` function can compose the new primitives. Every dispatched
response carries `timing_ms` for guards, snapshots, handler, mutex acquisition
and total. Handler time includes its native writes, rebuild and readback.

For a same-work comparison on a copied part, run
`tests/bench/sketch_inspection.py` with `--document`, `--sketch`, and `--output`.
The benchmark alternates the existing script wrapper and the scoped tool and
checks that they return the same data. It reports in-process dispatcher time,
not whole-task or network latency.

A five-pair comparison on one copied 37-segment sketch in SOLIDWORKS 2026
SP3.2 returned equal filtered relations, dimensions and equation data. Median
dispatcher time was 27.29 s through the script wrapper and 6.68 s through the
scoped tool. Ranges were 24.83–60.16 s and 6.01–7.82 s respectively, with zero
errors in the completed comparison. A separate phase profile attributed
34.44 s of one 35.24 s short script call to its two whole-model snapshots.
These figures describe this inspection case, not the duration of an entire
CAD editing task.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SW_MCP_ALLOW_SCRIPTS` | `1` | Set to `0` to disable Python scripts and recipes. |
| `SW_MCP_AUTO_DIFF` | `1` | Set to `0` to disable automatic model differences. |
| `SW_MCP_COMPACT` | `1` | Set to `0` to retain expanded JSON output. |
| `SW_MCP_REFS` | `1` | Set to `0` to disable persistent reference handles. |
| `SW_MCP_AUTO_DISMISS_DIALOGS` | `1` | Set to `0` to report dialogs without automatically dismissing them. |
| `SW_MCP_DIALOG_TIMEOUT_S` | `8` | Seconds before the watchdog attempts to dismiss a dialog. |

## Permissions and verification

Caller-supplied scripts run as local Python with the server process permissions.
They are not sandboxed and can access files and the network.
The standard tools' output-directory checks do not restrict arbitrary scripts.
Connect this server only to a trusted local MCP host.
Use `SW_MCP_ALLOW_SCRIPTS=0` when script execution is unnecessary.

The fork was used with SOLIDWORKS Design Premium 2026 SP3.2 on Windows.
Compatibility with other SOLIDWORKS versions is not established.
Offline tests do not verify live COM operations.
Live regression and benchmark scripts are optional and create test documents.
Review those scripts before running them against a SOLIDWORKS session.

This repository contains the MCP source and tests.
It excludes working CAD documents, screenshots, runtime logs, local account
configuration, and generated benchmark results.
