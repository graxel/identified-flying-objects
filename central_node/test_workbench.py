import unittest
import numpy as np
import json
import os
from pathlib import Path
from pipeline import TransformationPipeline, DEFAULT_PARAMS

class TestWorkbenchPipeline(unittest.TestCase):
    def setUp(self):
        self.pipeline = TransformationPipeline()
        self.h, self.w = 600, 800

    def test_pipeline_execution(self):
        low_res = np.zeros((self.h, self.w), dtype=np.uint8)
        slow_diff = np.zeros((self.h, self.w), dtype=np.uint8)
        fast_diff = np.zeros((self.h, self.w), dtype=np.uint8)

        # Draw a small artificial object in fast_diff
        fast_diff[200:210, 300:310] = 200

        results = self.pipeline.process(low_res, slow_diff, fast_diff)

        self.assertIn("inputs", results)
        self.assertIn("stages", results)
        self.assertIn("detections", results)
        self.assertIn("1_combined", results["stages"])
        self.assertIn("5_detections", results["stages"])

        # Check candidate bounding box detected
        self.assertGreater(len(results["detections"]), 0)
        det = results["detections"][0]
        self.assertTrue(295 <= det["x"] <= 305)
        self.assertTrue(195 <= det["y"] <= 205)

    def test_params_update(self):
        self.pipeline.set_params({"fast_weight": 50, "slow_weight": 50})
        self.assertEqual(self.pipeline.params["fast_weight"], 50)
        self.assertEqual(self.pipeline.params["slow_weight"], 50)

    def test_cv_params_broadcasting(self):
        from workbench import WorkbenchState, CV_PARAM_DEFAULTS
        state = WorkbenchState()

        sent_messages = []
        class MockPub:
            def send_multipart(self, parts):
                sent_messages.append(parts)

        state._param_pub = MockPub()
        state.update_cv_param("alpha_slow_x1000", 50)
        state.update_cv_param("min_area", 35)
        state.update_cv_param("morph_kernel", 5)

        self.assertEqual(len(sent_messages), 3)
        topic, payload_bytes = sent_messages[-1]
        self.assertEqual(topic, b"PARAMS")
        payload = json.loads(payload_bytes.decode("utf-8"))
        self.assertAlmostEqual(payload["alpha_slow"], 0.05)
        self.assertEqual(payload["min_area"], 35)
        self.assertEqual(payload["morph_kernel"], 5)
        self.assertEqual(payload["diff_thresh"], state.cv_params["diff_thresh"])

    def test_telemetry_packing_and_unpacking(self):
        from workbench import TELEMETRY_STRUCT
        import time

        now = time.time()
        packed = TELEMETRY_STRUCT.pack(
            15.5,  # cap_ms
            12.3,  # diff_ms
            25.4,  # bbox_ms
            0.0,   # ml_train_ms
            5.2,   # extract_ms
            1.1,   # pack_ms
            2.0,   # send_ms
            now,   # send_wall_time
            52.5,  # cpu_temp_c
            34.0,  # mem_used_pct
            0,     # encoder_q_size
            2,     # send_q_size
        )

        unpacked = TELEMETRY_STRUCT.unpack(packed)
        self.assertAlmostEqual(unpacked[0], 15.5, places=3)
        self.assertAlmostEqual(unpacked[1], 12.3, places=3)
        self.assertAlmostEqual(unpacked[2], 25.4, places=3)
        self.assertAlmostEqual(unpacked[4], 5.2, places=3)
        self.assertAlmostEqual(unpacked[7], now, places=3)
        self.assertAlmostEqual(unpacked[8], 52.5, places=3)
        self.assertEqual(unpacked[11], 2)


if __name__ == "__main__":
    unittest.main()

