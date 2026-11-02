"""A file-based CLI over the same registry and dispatcher as MCP."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .server import TOOLS, dispatch


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List the actual imported MCP catalog without CAD access")
    call = commands.add_parser("call", help="Call one registered tool through the MCP dispatcher")
    call.add_argument("tool")
    call.add_argument("--arguments-file", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "list":
        payload = [tool.model_dump(exclude_none=True) for tool in TOOLS]
        ok = True
    else:
        try:
            arguments = json.loads(args.arguments_file.read_text(encoding="utf-8-sig"))
            if not isinstance(arguments, dict):
                raise ValueError("Arguments file must contain a JSON object.")
            payload = dispatch(args.tool, arguments)
        except (OSError, ValueError) as exc:
            payload = {"ok": False, "message": str(exc)}
        ok = payload.get("ok", False)
    print(json.dumps(payload, ensure_ascii=False, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
