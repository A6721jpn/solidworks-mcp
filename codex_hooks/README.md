# SolidWorks MCP coverage hooks

This script supports project-local Codex hooks. It never imports CAD code, connects to SolidWorks, calls an external model, creates a chat, or changes MCP implementation. The active Codex agent performs semantic coverage review. The script validates and persists that review.

The parent project's `.codex/hooks.json` should call the script with `--workspace` set to the absolute workspace root, followed by `hook`. Register `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop`. New hooks must be reviewed and trusted through Codex's native `/hooks` interface. Do not write trust hashes or bypass trust.

## Lifecycle

`UserPromptSubmit` supplies the exact turn context so the agent can prepare before attempting CAD. Before the first recognizable CAD call, `PreToolUse` requires a preflight record. The agent reads the source catalog and records one entry per intended atomic operation. A declared missing operation permits an already-authorized direct API fallback. This is an internal task check, not a human approval request.

`PostToolUse` stores bounded operation evidence, including registered `call('tool')` uses, native API candidates, and a local code/command excerpt capped at about 4,000 characters. It does not store whole prompts or transcripts. Using `run_script` is not by itself evidence that a primitive is missing. Code-mode wrapper events are skipped after the nested MCP event path is recognized, avoiding duplicate evidence.

After the main CAD job, the active agent proactively follows [post_work_review.md](post_work_review.md) when UI fallback observations exist. It decides whether an existing capability, stale connection, bug, reusable gap, one-time inspection, or unknown state explains each fallback. Useful feasible local fixes/extensions are already authorized. Their actual source and live behavior must be verified before reporting completion.

`Stop` requests one combined coverage/improvement continuation per context evidence generation. A successful coverage finish accounts for every observed event and native candidate. Confirmed missing primitives become implementation tickets. Uncertain candidates become `needs_review`, while already-covered operations create no extension ticket. The distinct improvement receipt records implementation or an honest pending/no-change outcome. If review does not finish, a repeated Stop saves uncertain candidates and unresolved fallback IDs, then permits exit. It does not repeatedly continue or invent confirmed gaps. Stop does not imply the CAD objective is complete. Reasoned deferral for unfinished/paused/cancelled/superseded/higher-priority work suppresses Stop; a later genuine CAD boundary reminds once without renewing the old allowance.
An older context falling back to `needs_review` does not suppress a fresh context's first review continuation. When several contexts are pending, only newly requested contexts enter that continuation.

## CLI

Invoke with `python coverage_audit.py --workspace ABSOLUTE_ROOT COMMAND`.

- `catalog` returns source-declared tool names, descriptions, source locations and a digest. It does not prove the live MCP connection has reloaded those tools.
- `preflight --context AUDIT_JSON --operations OPERATIONS_JSON` records a nonempty operations array. Each entry has `operation`, `status`, `reason`, and `tool` when covered. Status is `covered`, `missing`, or `needs_review`. The first CAD hook supplies the turn-specific audit context path.
- `status --context AUDIT_JSON` returns current evidence, the existing `evidence_digest`, independent `postwork_digest`, and separate `coverage_status`/`improvement_status`.
- `finish --context AUDIT_JSON --reviews REVIEWS_JSON` accepts an object containing the current `evidence_digest` and `reviews`. Each review identifies one operation and lists `evidence_ids`, `native_api`, and coverage `status`. Covered reviews name a source tool. Missing or uncertain reviews also supply `proposed_tool`, `reason`, `inputs`, `expected_output`, and `validation`.
- `fallback-note --context AUDIT_JSON --note NOTE_JSON` records bounded operation/reason/route/kind/attribution/source IDs. Semantic normalization excludes timestamps, deduplicates IDs, and makes replay a no-op. It is the escape hatch for opaque bindings and unhooked direct node-repl paths.
- `post-work --context AUDIT_JSON --receipt RECEIPT_JSON` classifies exactly every fallback ID and records a digest-bound improvement receipt or reasoned job deferral. See [the workflow](post_work_review.md) for the evidence schema. A fixed/extended result requires current changed-source hashes, a named catalog tool, a real live check, and an artifact asserting successful changed behavior through that live tool. Offline checks alone remain pending-live. Evidence is validated structurally and never executed.

The context and input JSON files must be inside the workspace. Audit records are under `outputs/mcp-coverage-audit/`. Tickets are canonical JSON plus readable Markdown under `tickets/solidworks-mcp/`. Evidence and tickets use OS file locks and atomic file replacement. Repeated reports with the same proposed tool identifier and normalized API set share a ticket and aggregate evidence, even when the wording changes. Later CAD calls invalidate an earlier finish receipt.

## Limits

Source declarations are not a live MCP capability handshake. A stale connection or a failed call is not automatically a missing implementation. The agent must distinguish these cases.

Detection covers named SolidWorks MCP calls, supported top-level awaited wrappers, explicit COM connection commands, direct CUA, and supported executable CUA/Sky code arguments nested in `functions.exec`. The UI recognizer skips comments, quoted examples/patches, uninvoked functions/arrows, unrelated explicit app targets, and unsupported conditional syntax. It decodes only literal `code` arguments of known tools. It retains no raw UI text, typed values, screenshots, or transcript. Ambiguous persistent bindings produce candidates; an earlier SolidWorks mention cannot confirm their scope. Direct node-repl routes outside the existing matcher need `fallback-note`. This deliberately does not recognize every syntax or route. Existing hook registration and trust state remain unchanged.

Native methods/property writes are evidence candidates. They need semantic review; API names alone cannot determine whether an existing higher-level primitive already covers the action. Tickets represent single operations, not complete drawing workflows.
`run_script`, `run_recipe`, and `run_macro` cannot be named as coverage for a constituent CAD primitive. They can compose existing commands or provide a fallback, but the audit requires the underlying dedicated CAD command or records a gap.
