# CAD review and user-selected improvements

After every completed CAD request, the active agent reviews missing capabilities and opportunities to reduce time or response volume. It presents candidates, waits for the user's selection, then implements and verifies only the selected items.

Hooks collect observations, persist state and request continuation. They never call a model, operate CAD, execute evidence files or change source. The registered commands in `.codex/hooks.json` and their trust settings stay unchanged.

## Workflow

1. Before CAD, inspect the source `catalog` and connected tools separately, then record atomic coverage with `preflight`. A declared gap permits the already-authorized direct API route.
2. During CAD, hooks record supported MCP and UI operations. Use `operation-note` for opaque direct API/CLI work and `fallback-note` for unrecognized UI routes.
3. At the CAD boundary, complete `finish` for coverage. Use `propose` to assess capabilities and efficiency, even without Computer Use. Display the candidate IDs, benefits and acceptance checks, or record why no candidate is useful.
4. Await the user. `select` binds interpreted numbers/names/all/none to the displayed proposal and latest genuine reply. Previous blanket permission and generated Stop prompts are not selections.
5. Implement selected scope. `advance` preserves real progress and verified results. Stop continues runnable selected work while it progresses. Selection waits, explicit pauses, blockers and unavailable live checks remain distinct unfinished states.

Read [post_work_review.md](post_work_review.md) for the agent's procedure and [selected_work_contract.md](selected_work_contract.md) for exact input and evidence formats.

## CLI

Invoke `python coverage_audit.py --workspace ABSOLUTE_ROOT COMMAND`. Inputs and evidence must be local workspace artifacts. Descriptions of commands in those files are never executed.

| Command | Purpose |
| --- | --- |
| `catalog` | Source-declared tools, source locations and digest; not a live handshake. |
| `preflight --context C --operations FILE [--validation-of OWNER]` | Planned atomic operations. Bind selected-improvement validation explicitly to its owner; ordinary CAD stays independent. |
| `status --context C` | Coverage, workflow, metrics, proposal, selection and remaining work. |
| `finish --context C --reviews FILE` | Account for observed events/native candidates and create gap tickets. This does not finish the improvement review. |
| `operation-note --context C --note FILE` | Record opaque authorized CAD work and available measurements. |
| `fallback-note --context C --note FILE` | Record an unrecognized UI fallback and its reason. |
| `propose --context C --proposal FILE` | Capability and efficiency assessments, candidates or a justified empty result. |
| `select --context C --selection FILE` | User selection of a particular displayed proposal. |
| `advance --context C --update FILE` | Selected-item progress, verification, blocker or explicit pause/resume. |
| `post-work --context C --receipt FILE` | Version-1 fallback receipts and deferrals. New contexts use `propose`, `select` and `advance`. |

## State and continuation

New contexts use version 2. The original context owns the proposal and selected work across later turns in the same conversation. Historical version-1 records retain their recorded meaning; startup does not reopen all old records. Writes retain per-context locks and atomic replacement.

Coverage and improvement completion are separate. A ticket, source edit or offline test cannot prove a live MCP capability is ready. Verification must match the route declared in the presented candidate and current source. A hook-only change can use an actual hook/CLI protocol check; a CAD capability requires changed behavior through the named live tool.

Selected work has no fixed one-continuation ceiling. Actual progress permits additional Stop continuations, including when `stop_hook_active` is true. Repeated prose, timestamps and replay are not progress. Three consecutive boundaries without progress leave an explicit unfinished state instead of looping or claiming success. Session start and later prompts surface owned unfinished work. Saved state does not run a closed app or create a background agent.

## Measurements and limits

Status summarizes observed call count, known processing time and serialized response bytes, including missing-measurement counts. Timing comes from actual response metadata where available. Bytes measure response volume, not actual tokens. Response payloads are not retained for metrics.

Opaque script bodies are not executed or inspected by hooks; record their CAD operations explicitly. A script path or `run_script` call alone does not prove atomic coverage. UI attribution remains bounded and can be uncertain. Keep source declarations, connected tools and actual changed behavior distinct.

The active agent is responsible for truthful presentation, interpreting the actual user reply and performing verification. Stored assertions are validated for identity, scope, freshness and hashes; they are not cryptographic execution proofs. Never invent evidence.
