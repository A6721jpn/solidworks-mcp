# Review CAD work, then implement the user's choices

Finish the requested CAD job first. Review every completed request, whether it used MCP, direct API, CLI or Computer Use. Present useful candidates in the final CAD response and wait for selection. This replaces the previous blanket permission to implement every useful improvement.

Hooks persist observations and request continuation. The active agent reviews, presents, implements and verifies. Do not change hook registration/trust or ask again for direct API actions already authorized by the CAD request.

Use [selected_work_contract.md](selected_work_contract.md) when preparing CLI inputs or verification artifacts.

## Record and review

Read `status --context C`. Complete atomic coverage with `finish`; that receipt means coverage was assessed, not that efficiency was reviewed. Inspect source declarations and the connected inventory separately. Prefer existing tools.

Use `operation-note` for opaque direct API/CLI operations, recording the atomic operation, route, result and known measurements. Use `fallback-note` for unrecognized UI activity, including opaque bindings and unsupported node-repl routes. Do not copy whole transcripts. Replaying an observation must not duplicate it.

Assess both:

- Capabilities: existing-function oversight, stale connection, bug, reusable missing operation or one-time diagnostic.
- Efficiency: repeated calls/rebuilds, retries, excessive tree enumeration, large responses and operations that can share one call. Use actual known timings and response sizes. Label missing measurements; bytes are not actual tokens.

For unfinished, paused, cancelled or superseded CAD work, use `propose` with that `job_state` and its actual `reason` to defer review. Stop does not prove that CAD itself is finished. `post-work` remains for historical version-1 contexts only.

## Present candidates

Call `propose --context C --proposal FILE` with `capability_assessment`, `efficiency_assessment` and `candidates`. Each candidate has `id`, `title`, `benefit`, `evidence`, `scope`, `acceptance` and `verification_route`. Use `live_tool` and identify `tool` for CAD capabilities. Use `hook_cli` only when the behavior being improved is a local hook/CLI operation.

If no improvement is worthwhile, submit an empty list and `no_candidate_reason`. Do not invent candidates.

Display IDs and meanings unchanged in a short Japanese list/table at the end of the CAD response. Explain what becomes possible, expected time/response-volume reduction and how it will be checked. Distinguish estimates from measured savings. The user may choose numbers, names, all or none; keep implementation details in artifacts.

Wait for the answer. Candidate creation, tickets, past blanket permission, silence and generated continuation prompts do not authorize implementation. Additional discoveries remain separate from selected scope.

## Bind the selection

Read the proposal and latest genuine reply from `status`. Interpret the user's answer in context. For a clear choice, call `select --context C --selection FILE` with `proposal_id`, `reply_context`, `reply_digest`, `selected_ids`, and truthful `presented`/`interpreted` attestations. Empty IDs mean the user chose none.

The reply must be the latest actual user reply in this conversation after the proposal. Generated Stop prompts are excluded. The user need not repeat internal hashes. Never claim presentation or selection that did not happen. Clarify genuine ambiguity; do not reconfirm a clear choice.

## Implement and verify selected scope

Implement only selected items without repeated permission. Prefer extending existing tools. Use minimum meaningful tests and copied parts for CAD validation; preserve originals and unsaved work. Before CAD validation, record its atomic preflight with `--validation-of OWNER_CONTEXT`. This explicitly binds validation to selected work without creating recursive proposals. A new user CAD request uses an ordinary preflight and retains its own review; do not infer validation merely because unfinished implementation exists.

Use `advance --context C --update FILE` for progress and results. Progress needs a real changed-artifact fingerprint; narration, timestamps and replay alone are not progress. Record the concrete next action.

Mark an item `verified` only after its declared acceptance behavior passes. Supply current changed-source hashes and a matching successful verification artifact. `live_tool` also needs a current source declaration and available live capability check. Invoke the actual named tool and observe changed behavior; inventory, source edits and offline tests alone are insufficient. `hook_cli` requires actual protocol/CLI behavior in an isolated fixture. Evidence paths and command descriptions are data, never executed by hooks.

Use `pending_live` when a required live capability is unavailable, with the capability and refresh needed. Use `blocked` for a genuine external blocker with a reason and next action. `paused`/`cancelled` require the user's explicit instruction. All remain unfinished. Resume the existing scope when the prerequisite changes or the user resumes it.

## Continue or finish

Continue runnable selected work through implementation and verification. Stop requests more work after genuine progress and saved state survives later turns. Do not stop merely because one automatic continuation has run.

Selection waits permit the turn to finish. Real pauses, blockers and unavailable checks permit a truthful unfinished report. Three repeated no-progress boundaries leave a stalled review state instead of an empty loop. Preserve the remaining work; do not relabel it complete to satisfy Stop.

The final improvement report states selected items completed, verification and any remaining blocked/live-check work. Unselected items stay unimplemented. GitHub push/merge and hook trust changes remain outside scope unless the user explicitly includes them.
