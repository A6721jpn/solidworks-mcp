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

## Modified upstream files

| File | Changes |
| --- | --- |
| `solidworks_mcp/server.py` | Register fork tools, return compact output and model differences, and monitor dialogs. |
| `solidworks_mcp/sw_core.py` | Use late-bound COM dispatch, persistent references, and dimension fixes. |
| `solidworks_mcp/sw_inspect.py` | Add geometry pagination and reference handles. |
| `solidworks_mcp/sw_sketch.py` | Normalize dimension names and correct angular and driven-dimension reporting. |
| `README.md` | Identify this fork and document installation and script permissions. |
| `pyproject.toml` | Point project links at this fork and retain the upstream link. |
| `.gitignore` | Exclude local data, CAD files, logs, and credentials. |
| `RELEASING.md` | Document source-only distribution. |
| `.github/workflows/tests.yml` | Run offline CI on the `codex-enhancements` branch as well as `main`. |

The fork adds `sw_snapshot.py`, `sw_script.py`, `sw_watchdog.py`, offline tests,
an optional live regression script, and a benchmark script.
The upstream package publishing workflow and registry manifest are removed.
The upstream offline test workflow is retained.

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
