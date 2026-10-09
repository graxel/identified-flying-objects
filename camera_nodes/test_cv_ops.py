import unittest
import numpy as np
import cv2

from cv_ops import (
    DEFAULT_CV_PARAMS,
    perform_motion_differencing,
    process_motion_diffs,
)


class TestCVOps(unittest.TestCase):
    def setUp(self):
        self.w, self.h = 800, 600
        self.main_w, self.main_h = 4056, 3040
        self.scale_x = self.main_w / float(self.w)
        self.scale_y = self.main_h / float(self.h)

    def test_default_params_detection(self):
        # Create blank diffs with a 10x10 patch of intensity 50 in both (above threshold 20)
        slow_diff = np.zeros((self.h, self.w), dtype=np.uint8)
        fast_diff = np.zeros((self.h, self.w), dtype=np.uint8)
        slow_diff[100:110, 100:110] = 50
        fast_diff[100:110, 100:110] = 50

        boxes = process_motion_diffs(
            slow_diff, fast_diff,
            self.scale_x, self.scale_y,
            self.main_w, self.main_h,
            params=DEFAULT_CV_PARAMS,
        )
        self.assertEqual(len(boxes), 1)
        self.assertIn(0, boxes)
        b = boxes[0]
        self.assertGreater(b["w"], 0)
        self.assertGreater(b["h"], 0)

    def test_threshold_parameter_override(self):
        slow_diff = np.zeros((self.h, self.w), dtype=np.uint8)
        fast_diff = np.zeros((self.h, self.w), dtype=np.uint8)
        # Intensity 30
        slow_diff[100:110, 100:110] = 30
        fast_diff[100:110, 100:110] = 30

        # With diff_thresh=20, should be detected
        params_low = dict(DEFAULT_CV_PARAMS, diff_thresh=20)
        boxes_low = process_motion_diffs(
            slow_diff, fast_diff,
            self.scale_x, self.scale_y,
            self.main_w, self.main_h,
            params=params_low,
        )
        self.assertEqual(len(boxes_low), 1)

        # With diff_thresh=40, should be ignored
        params_high = dict(DEFAULT_CV_PARAMS, diff_thresh=40)
        boxes_high = process_motion_diffs(
            slow_diff, fast_diff,
            self.scale_x, self.scale_y,
            self.main_w, self.main_h,
            params=params_high,
        )
        self.assertEqual(len(boxes_high), 0)

    def test_area_parameter_override(self):
        slow_diff = np.zeros((self.h, self.w), dtype=np.uint8)
        fast_diff = np.zeros((self.h, self.w), dtype=np.uint8)
        # 6x6 patch has area around 25-36 pixels
        slow_diff[100:106, 100:106] = 50
        fast_diff[100:106, 100:106] = 50

        # min_area=15 -> detected
        params_small = dict(DEFAULT_CV_PARAMS, min_area=15)
        boxes_small = process_motion_diffs(
            slow_diff, fast_diff,
            self.scale_x, self.scale_y,
            self.main_w, self.main_h,
            params=params_small,
        )
        self.assertEqual(len(boxes_small), 1)

        # min_area=100 -> rejected
        params_large = dict(DEFAULT_CV_PARAMS, min_area=100)
        boxes_large = process_motion_diffs(
            slow_diff, fast_diff,
            self.scale_x, self.scale_y,
            self.main_w, self.main_h,
            params=params_large,
        )
        self.assertEqual(len(boxes_large), 0)

    def test_perform_motion_differencing(self):
        frame = np.full((self.h, self.w), 128, dtype=np.uint8)
        # Initial call creates backgrounds
        sd, s_bg, fd, f_bg = perform_motion_differencing(frame, None, None)
        self.assertIsNone(sd)
        self.assertIsNotNone(s_bg)
        self.assertIsNotNone(f_bg)

        # Second call with moving pixel
        frame2 = frame.copy()
        frame2[50:60, 50:60] = 200
        params = dict(DEFAULT_CV_PARAMS, alpha_slow=0.05, alpha_fast=0.5)
        sd2, s_bg2, fd2, f_bg2 = perform_motion_differencing(frame2, s_bg, f_bg, params=params)
        self.assertIsNotNone(sd2)
        self.assertIsNotNone(fd2)
        self.assertGreater(np.max(sd2), 0)
        self.assertGreater(np.max(fd2), 0)

    def test_return_diagnostics(self):
        slow_diff = np.zeros((self.h, self.w), dtype=np.uint8)
        fast_diff = np.zeros((self.h, self.w), dtype=np.uint8)
        slow_diff[100:110, 100:110] = 50
        fast_diff[100:110, 100:110] = 60

        boxes, diag = process_motion_diffs(
            slow_diff, fast_diff,
            self.scale_x, self.scale_y,
            self.main_w, self.main_h,
            params=DEFAULT_CV_PARAMS,
            return_diagnostics=True,
        )
        self.assertIn("combined_diff", diag)
        self.assertIn("thresh_mask", diag)
        self.assertIn("morphed_mask", diag)

        # combined_diff should have pixel min (50)
        self.assertEqual(np.max(diag["combined_diff"]), 50)
        # thresh_mask should be 255 where diff > 20
        self.assertEqual(np.max(diag["thresh_mask"]), 255)
        # morphed_mask should preserve surviving blob
        self.assertEqual(np.max(diag["morphed_mask"]), 255)
        self.assertEqual(len(boxes), 1)

    def test_morphology_parameter_tuning(self):
        slow_diff = np.zeros((self.h, self.w), dtype=np.uint8)
        fast_diff = np.zeros((self.h, self.w), dtype=np.uint8)
        # Small isolated noise speckle (2x2)
        slow_diff[100:102, 100:102] = 50
        fast_diff[100:102, 100:102] = 50

        # With morph_open_iter=1 and 3x3 kernel, 2x2 speckle is eroded away
        params_with_open = dict(DEFAULT_CV_PARAMS, min_area=1, morph_kernel=3, morph_open_iter=1, morph_close_iter=0)
        _, diag_open = process_motion_diffs(
            slow_diff, fast_diff,
            self.scale_x, self.scale_y,
            self.main_w, self.main_h,
            params=params_with_open,
            return_diagnostics=True,
        )
        self.assertEqual(np.max(diag_open["morphed_mask"]), 0)

        # With morph_open_iter=0, speckle is not eroded away
        params_no_open = dict(DEFAULT_CV_PARAMS, min_area=1, morph_kernel=3, morph_open_iter=0, morph_close_iter=0)
        _, diag_no_open = process_motion_diffs(
            slow_diff, fast_diff,
            self.scale_x, self.scale_y,
            self.main_w, self.main_h,
            params=params_no_open,
            return_diagnostics=True,
        )
        self.assertEqual(np.max(diag_no_open["morphed_mask"]), 255)


if __name__ == "__main__":
    unittest.main()

