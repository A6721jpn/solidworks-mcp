"""Before/after measurement of the fork's token-efficiency changes.

Run with SOLIDWORKS open:

    ..\\venv\\Scripts\\python.exe tests\\bench\\run_bench.py

Each scenario is executed twice on a fresh test part: the *baseline* way an
agent had to work with the upstream server (list everything, re-list after
every change, look at a 1100 px screenshot to verify) and the *fork* way
(model_snapshot, refs, the diff that rides on every result, run_script).
Both paths go through ``server.dispatch`` and ``server.render`` so the
numbers are what the MCP host actually receives.  Token counts are estimated
at 4 characters per token for text; image tokens are reported as pixel area
because they depend on the host's image tokenizer.

Results land in tests/bench/results/ as JSON and Markdown.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import server  # noqa: E402
from solidworks_mcp.sw_core import running_app  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"


class Meter:
    def __init__(self) -> None:
        self.calls = 0
        self.chars = 0
        self.images = 0
        self.pixels = 0
        self.seconds = 0.0

    def call(self, name: str, args: dict[str, Any] | None = None, *, compact: bool = True) -> dict[str, Any]:
        started = time.perf_counter()
        payload = server.dispatch(name, args or {})
        self.seconds += time.perf_counter() - started
        self.calls += 1
        old = server.COMPACT
        try:
            server.COMPACT = compact
            content = server.render(dict(payload))
        finally:
            server.COMPACT = old
        for item in content:
            if getattr(item, "type", "") == "text":
                self.chars += len(item.text)
            else:
                self.images += 1
                self.pixels += _png_pixels(item.data)
        return payload

    def row(self) -> dict[str, Any]:
        return {
            "calls": self.calls, "chars": self.chars, "est_tokens": self.chars // 4,
            "images": self.images, "megapixels": round(self.pixels / 1e6, 2), "seconds": round(self.seconds, 2),
        }


def _png_pixels(b64: str) -> int:
    import base64
    import io

    try:
        from PIL import Image

        with Image.open(io.BytesIO(base64.b64decode(b64))) as image:
            return image.width * image.height
    except Exception:
        return 0


def build_part(meter: Meter) -> None:
    meter.call("create_sketch", {"plane": "front", "name": "Base"})
    meter.call("draw_rectangle", {"x1_mm": 0, "y1_mm": 0, "x2_mm": 60, "y2_mm": 40})
    meter.call("close_sketch")
    meter.call("boss_extrude", {"sketch_name": "Base", "depth_mm": 20, "name": "BasePad"})


# --- Scenario 1: read the model state ----------------------------------------------

def read_state_baseline(m: Meter) -> None:
    m.call("list_features", compact=False)
    m.call("list_dimensions", compact=False)
    m.call("get_bounding_box", compact=False)
    m.call("get_mass_properties", compact=False)
    m.call("check_errors", compact=False)


def read_state_fork(m: Meter) -> None:
    m.call("model_snapshot")


# --- Scenario 2: fillet the four vertical edges, then verify -------------------------

def fillet_baseline(m: Meter) -> None:
    edges = m.call("list_edges", {"limit": 1000}, compact=False)["data"]["edges"]
    picks = [e["index"] for e in edges if e.get("curve_type") == "line" and abs(e.get("length_mm", 0) - 20) < 1e-6]
    m.call("fillet", {"radius_mm": 3, "selection": {"edges": picks}}, compact=False)
    m.call("list_features", compact=False)
    m.call("get_mass_properties", compact=False)
    m.call("capture_screenshot", {"view": "isometric", "width_px": 1100}, compact=False)


def fillet_fork(m: Meter) -> None:
    edges = m.call("list_edges", {"curve_type": "line", "min_length_mm": 19, "max_length_mm": 21})["data"]["edges"]
    m.call("fillet", {"radius_mm": 3, "selection": {"refs": [e["ref"] for e in edges]}})
    # verification is the diff already attached to the fillet result: no extra call


# --- Scenario 3: change three dimensions and rebuild ---------------------------------

def dims_baseline(m: Meter) -> None:
    m.call("set_dimension", {"full_name": "D1@Base", "value_mm": 70}, compact=False)
    m.call("set_dimension", {"full_name": "D2@Base", "value_mm": 45}, compact=False)
    m.call("set_dimension", {"full_name": "D1@BasePad", "value_mm": 25}, compact=False)
    m.call("rebuild_document", compact=False)
    m.call("get_bounding_box", compact=False)


def dims_fork(m: Meter) -> None:
    m.call("run_script", {"code": (
        "for name, v in [('D1@Base', 70), ('D2@Base', 45), ('D1@BasePad', 25)]:\n"
        "    call('set_dimension', full_name=name, value_mm=v)\n"
        "result = call('get_bounding_box')['data']['size_mm']\n"
    )})


# --- Scenario 4: list faces of the part (paging) -------------------------------------

def faces_baseline(m: Meter) -> None:
    m.call("list_faces", {"limit": 1000}, compact=False)


def faces_fork(m: Meter) -> None:
    m.call("list_faces", {"surface_type": "plane", "normal": [0, 0, 1]})


SCENARIOS: list[tuple[str, Callable[[Meter], None], Callable[[Meter], None]]] = [
    ("read model state", read_state_baseline, read_state_fork),
    ("fillet 4 edges + verify", fillet_baseline, fillet_fork),
    ("change 3 dims + rebuild", dims_baseline, dims_fork),
    ("list faces", faces_baseline, faces_fork),
]


def run_variant(fn: Callable[[Meter], None]) -> dict[str, Any]:
    app = running_app()
    setup = Meter()
    setup.call("create_new_document", {"kind": "part"})
    build_part(setup)
    meter = Meter()
    try:
        fn(meter)
    finally:
        try:
            title = server.dispatch("get_active_document_info", {})["data"]["document"]["title"]
            app.QuitDoc(title)
        except Exception:
            pass
    return meter.row()


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for name, baseline, fork in SCENARIOS:
        before = run_variant(baseline)
        after = run_variant(fork)
        rows.append({"scenario": name, "baseline": before, "fork": after})
        print(f"{name}: baseline {before} | fork {after}")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    (RESULTS / f"bench_{stamp}.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    lines = [
        "| scenario | calls | text chars (~tokens) | images (Mpx) | seconds |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        b, f = row["baseline"], row["fork"]
        lines.append(
            f"| {row['scenario']} | {b['calls']} -> {f['calls']} | {b['chars']} (~{b['est_tokens']}) -> {f['chars']} (~{f['est_tokens']}) "
            f"| {b['images']} ({b['megapixels']}) -> {f['images']} ({f['megapixels']}) | {b['seconds']} -> {f['seconds']} |"
        )
    table = "\n".join(lines)
    (RESULTS / f"bench_{stamp}.md").write_text(table + "\n", encoding="utf-8")
    print(table)


if __name__ == "__main__":
    main()
