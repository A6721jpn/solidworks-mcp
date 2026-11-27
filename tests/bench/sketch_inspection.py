"""Compare equal sketch inspection work through the script wrapper and scoped tool.

Run against an already-open disposable part copy. This does not change geometry.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from solidworks_mcp import server
from solidworks_mcp.sw_script import jsonable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", required=True)
    parser.add_argument("--sketch", required=True)
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--relation-type", type=int, action="append")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error("--pairs must be positive")
    inspection = {"expected_document_path": args.document, "sketch_name": args.sketch}
    if args.relation_type is not None:
        inspection["relation_types"] = args.relation_type
    samples = []
    errors = []
    for pair in range(args.pairs):
        results = []
        for tool, arguments in [
            ("run_script", {"code": "result=call('inspect_sketch',**params['inspection'])",
                            "params": {"inspection": inspection}}),
            ("inspect_sketch", inspection),
        ]:
            started = time.perf_counter()
            response = server.dispatch(tool, arguments)
            elapsed = time.perf_counter() - started
            data = response.get("data", {})
            nested = data.get("result", {}) if tool == "run_script" else response
            if not response.get("ok") or not nested.get("ok"):
                errors.append({"pair": pair + 1, "tool": tool, "response": response})
                break
            results.append((elapsed, nested["data"], response["timing_ms"]))
        if errors:
            break
        old, new = results
        equal = old[1] == jsonable({"data": new[1]})["data"]
        if not equal:
            errors.append({"pair": pair + 1, "error": "Inspection outputs differ."})
            break
        samples.append({"old_seconds": old[0], "new_seconds": new[0], "equal": equal,
                        "old_timing_ms": old[2], "new_timing_ms": new[2]})
        print(json.dumps({"pair": pair + 1, **samples[-1]}), flush=True)
    report = {"pairs": samples, "errors": errors,
              "scope": "Same live copied document and inspection. In-process dispatch; excludes host transport.",
              "order": "old wrapper, new scoped tool, alternating"}
    if samples:
        for label in ["old", "new"]:
            seconds = [s[label + "_seconds"] for s in samples]
            report[label] = {"median_seconds": statistics.median(seconds),
                             "range_seconds": [min(seconds), max(seconds)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
