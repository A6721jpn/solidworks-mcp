# Synchronize a projected sketch

`convert_entities` accepts an optional `sync` object. Calls without it retain ordinary Convert Entities behavior.

Use this when a source profile gains or deactivates segments and an existing projected sketch must follow. Native Convert Entities relations establish correspondence. The operation retains existing target entities and their relations, adds missing projections in one batch, and changes obsolete projections to construction geometry. It does not infer ownership from geometric similarity.

## Define the intended profile

`selection.sketch_name` names the source sketch. `selection.sketch_segments` is an explicit, complete list of the source segment indices to keep projected. Refresh these indices after source topology changes. A selected construction segment remains construction in the target. A source-owned projection omitted from the list becomes construction unless protected.

Set `chain` and `inner_loops` to `false` so native selection cannot expand the requested set.

Supply these `sync` fields:

| Field | Meaning |
| --- | --- |
| `expected_document_path` | Exact active part document key. |
| `expected_configuration` | Exact active configuration. The operation does not switch it. |
| `target_sketch` | Exact existing 2D target sketch, distinct from the source. |
| `protected_target_segments` | Explicit array of target native identities, each with integer `type` and two-integer `id`. An empty array must be intentional. Positional indices are rejected because added projections can reorder enumeration. |
| `rollback_before` | Optional exact feature before which the target can be edited. |
| `apply` | `false` by default for a read-only plan. Set `true` to apply the recomputed plan. |

Obtain native segment identities from `inspect_sketch` or `list_sketch_segments`. They are scoped to the named sketch and document. Resolve the identities of the contours you want to protect before editing. Unmanaged geometry remains unchanged.

## Read the result

The response gives added, construction-changed, unchanged and protected counts, bounded native identities, measured elapsed time, and restoration status. A repeated request with unchanged source indices and the same protected identities is a no-op. Changing the source requires a fresh authoritative selection.

The operation requires a closed sketch on entry. It restores the original rollback boundary and closed editing state. It does not save or close the document. Successful mutation is grouped as one native undo operation. A failure attempts to undo only that confirmed group and checks the source and target fingerprints. Always inspect `ok`, `restored`, `may_have_changes` and any restoration errors before continuing.

Ambiguous or unreadable projection provenance, conflicting protected geometry and unsupported segment types are refused. Analytic lines, arcs/circles and ellipses are supported. Deleted-source orphan relations are not guessed or automatically removed.
