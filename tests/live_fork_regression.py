"""Live SOLIDWORKS checks for the fork additions.

Run only with SOLIDWORKS already running:

    ..\\venv\\Scripts\\python.exe tests\\live_fork_regression.py

Everything goes through ``server.dispatch`` so the watchdog, auto-diff and
compact rendering are exercised exactly as the MCP host sees them.  The test
creates its own part, saves it under the output root, and closes it again; it
never touches a document that was already open.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import server  # noqa: E402
from solidworks_mcp.sw_core import OUTPUT_ROOT, running_app  # noqa: E402

D = server.dispatch
PASSED: list[str] = []


def ok(payload: dict[str, Any], context: str) -> dict[str, Any]:
    if not payload.get("ok"):
        raise AssertionError(f"{context} failed: {payload.get('message')} | {payload}")
    PASSED.append(context)
    return payload


def check(condition: bool, context: str, detail: Any = "") -> None:
    if not condition:
        raise AssertionError(f"{context}: {detail}")
    PASSED.append(context)


def close_active(app: Any) -> None:
    try:
        info = D("get_active_document_info", {})
        title = info["data"]["document"]["title"]
        app.QuitDoc(title)
    except Exception:
        pass


def main() -> None:
    app = running_app()
    ok(D("create_new_document", {"kind": "part"}), "create part")
    temp_file = OUTPUT_ROOT / "tests" / f"fork_{int(time.time())}.sldprt"
    try:
        ok(D("create_sketch", {"plane": "front", "name": "Base"}), "create sketch")
        ok(D("draw_rectangle", {"x1_mm": 0, "y1_mm": 0, "x2_mm": 60, "y2_mm": 40}), "draw rectangle")
        ok(D("close_sketch", {}), "close sketch")

        pad = ok(D("boss_extrude", {"sketch_name": "Base", "depth_mm": 20, "name": "BasePad"}), "boss extrude")
        check("BasePad" in pad.get("diff", ""), "auto-diff on boss_extrude", pad.get("diff"))
        check("size" in pad.get("diff", ""), "auto-diff reports size", pad.get("diff"))

        snap = ok(D("model_snapshot", {"label": "after_pad"}), "model_snapshot")
        text = snap["_text"]
        check("BasePad" in text and "size 60 x 40 x 20 mm" in text, "snapshot text", text)
        check("D1@BasePad=20" in text, "snapshot inline dimensions", text)
        check(len(text) < 900, "snapshot is compact", len(text))

        edges = ok(D("list_edges", {"curve_type": "line", "min_length_mm": 19, "max_length_mm": 21}), "list vertical edges")
        vertical = edges["data"]["edges"]
        check(len(vertical) == 4, "four vertical edges", vertical)
        refs = [e["ref"] for e in vertical]
        check(all(r.startswith("e") for r in refs), "edges carry refs", refs)

        # Change the geometry *before* using the refs: indices shift, refs must not.
        ok(D("create_sketch", {"plane": "front", "name": "HoleSk"}), "create hole sketch")
        ok(D("draw_circle", {"x_mm": 30, "y_mm": 20, "radius_mm": 5}), "draw hole circle")
        ok(D("close_sketch", {}), "close hole sketch")
        # A single-direction through_all from the sketch plane can point away from
        # the material: SOLIDWORKS still reports success, the volume just does not
        # move.  The server now flags that; the test uses both directions.
        noop = ok(D("cut_extrude", {"sketch_name": "HoleSk", "end_condition": "through_all", "name": "HoleNoop"}), "one-way cut")
        check("volume unchanged" in noop.get("warning", ""), "no-op geometry warning", noop)
        ok(D("delete_feature", {"feature_names": ["HoleNoop"]}), "delete no-op cut")
        cut = ok(D("cut_extrude", {"sketch_name": "HoleSk", "end_condition": "through_all_both", "name": "Hole"}), "cut through all both")
        check("Hole" in cut.get("diff", "") and "vol" in cut.get("diff", ""), "auto-diff on cut", cut.get("diff"))

        fillet = ok(D("fillet", {"radius_mm": 3, "selection": {"refs": refs}}), "fillet via persistent refs")
        check("+" in fillet.get("diff", "") and "Fillet" in fillet.get("diff", ""), "auto-diff on fillet", fillet.get("diff"))

        gone = D("measure", {"selection": {"refs": [refs[0]]}})
        check(not gone.get("ok") and "no longer resolves" in gone.get("message", ""), "consumed ref reports clearly", gone)

        diff = ok(D("model_diff", {"since": "after_pad"}), "model_diff since checkpoint")
        check("Hole" in diff["_text"] and "Fillet" in diff["_text"] and "vol" in diff["_text"], "checkpoint diff content", diff["_text"])

        code = (
            "r = call('set_dimension', full_name='D1@BasePad', value_mm=25)\n"
            "box = call('get_bounding_box')['data']['size_mm']\n"
            "print('script ok')\n"
            "result = {'set': r['ok'], 'size': box, 'features': len(core.iter_features(doc))}\n"
        )
        script = ok(D("run_script", {"code": code}), "run_script batch")
        data = script["data"]
        check(data["result"]["set"] is True and abs(data["result"]["size"][2] - 25) < 0.5, "script result (GetPartBox is loose around fillets)", data)
        check("script ok" in data.get("stdout", ""), "script stdout captured", data)
        check("D1@BasePad 20 -> 25" in script.get("diff", ""), "script diff shows dimension change", script.get("diff"))

        bad = D("run_script", {"code": "x = 1/0\n"})
        check(not bad.get("ok") and "ZeroDivisionError" in bad.get("message", ""), "script error surfaced", bad)

        recipe = ok(D("save_recipe", {
            "name": "fork_test_depth", "description": "Set BasePad depth.", "params": "depth_mm=30",
            "code": "call('set_dimension', full_name='D1@BasePad', value_mm=depth_mm)\nresult = depth_mm\n",
            "overwrite": True,
        }), "save_recipe")
        ran = ok(D("run_recipe", {"name": "fork_test_depth", "params": {"depth_mm": 30}}), "run_recipe")
        check(ran["data"]["result"] == 30 and "D1@BasePad 25 -> 30" in ran.get("diff", ""), "recipe applied", ran)
        listed = ok(D("list_recipes", {}), "list_recipes")
        check(any(r["name"] == "fork_test_depth" for r in listed["data"]["recipes"]), "recipe listed", listed)

        page = ok(D("list_faces", {"limit": 3}), "list_faces paging")
        check(len(page["data"]["faces"]) == 3 and "showing 0..2" in page["message"], "paging note", page["message"])
        check(all("ref" in f for f in page["data"]["faces"]), "faces carry refs", page["data"]["faces"])

        shot = ok(D("capture_screenshot", {"view": "isometric"}), "capture_screenshot")
        check(shot["data"]["bytes"] > 1000 and "_image_png_base64" in shot, "screenshot bytes", shot["data"])

        dims = ok(D("list_dimensions", {"feature_name": "BasePad"}), "list_dimensions")
        check(any(d["full_name"] == "D1@BasePad" and abs(d.get("value_mm", 0) - 30) < 1e-6 for d in dims["data"]["dimensions"]),
              "dimension listed in mm", dims["data"])

        noop = ok(D("dismiss_dialog", {}), "dismiss_dialog no-op")
        check(noop.get("data", {}).get("blocked") is False, "not blocked", noop)

        [rendered] = server.render(dict(D("get_bounding_box", {})))
        check("\n" not in rendered.text and '"ok":true' in rendered.text, "compact json rendering", rendered.text)
        [rendered] = server.render(dict(D("model_snapshot", {})))
        check(rendered.text.startswith("doc:"), "text passthrough rendering", rendered.text[:40])

        # open_document was the tool broken by the gen_py cache: save, close, reopen.
        saved = ok(D("save_document", {"path": str(temp_file)}), "save_document")
        close_active(app)
        opened = ok(D("open_document", {"path": str(temp_file)}), "open_document (previously broken)")
        check(Path(opened["data"]["document"]["path"]).resolve() == temp_file.resolve(), "opened path", opened)
        reread = ok(D("model_snapshot", {}), "snapshot of reopened file")
        check("BasePad" in reread["_text"] and "Fillet" in reread["_text"], "reopened content", reread["_text"])
    finally:
        close_active(app)
        try:
            temp_file.unlink()
        except OSError:
            pass
    print(f"live fork regression passed: {len(PASSED)} checks")


if __name__ == "__main__":
    main()
