import json
import unittest

from solidworks_mcp import server


class PosePrecisionTests(unittest.TestCase):
    def test_rendered_pose_can_be_reused_without_losing_precision(self):
        pose = {
            "rotation": [0.7071067811865476, -0.7071067811865475, 0.0,
                         0.7071067811865475, 0.7071067811865476, 0.0,
                         0.0, 0.0, 1.0],
            "translation_mm": [1.23456789, -30.0128, 0.0000101579],
        }
        payload = {"ok": True, "data": {"components": [{"pose": pose}], "size": 1.23456789}}
        rendered = json.loads(server.render(payload)[0].text)
        self.assertEqual(rendered["data"]["components"][0]["pose"], pose)
        self.assertEqual(rendered["data"]["size"], 1.235)


if __name__ == "__main__":
    unittest.main()
