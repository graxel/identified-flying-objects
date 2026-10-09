import cv2
import numpy as np

DEFAULT_PARAMS = {
    "fast_weight": 70,        # 0 to 100 -> 0.0 to 1.0
    "slow_weight": 30,        # 0 to 100 -> 0.0 to 1.0
    "diff_mode": 0,           # 0: Weighted Sum, 1: Fast minus Slow, 2: Max, 3: Min
    "blur_kernel": 3,         # 1, 3, 5, 7, 9
    "thresh_val": 30,         # 0 to 255
    "use_otsu": 0,            # 0: Manual, 1: Otsu
    "skyline_cutoff": 0,      # 0 to 100% of height masked out from bottom
    "morph_kernel": 3,        # 1, 3, 5, 7
    "morph_open_iter": 1,     # 0 to 5
    "morph_close_iter": 1,    # 0 to 5
    "min_area": 4,            # Min contour area (pixels)
    "max_area": 800,          # Max contour area (pixels)
    "min_solidity": 30,       # 0 to 100%
    "max_aspect_ratio": 40,   # 10 to 100 -> 1.0 to 10.0
}

class TransformationPipeline:
    def __init__(self, params=None):
        self.params = DEFAULT_PARAMS.copy()
        if params:
            self.params.update(params)

    def set_params(self, params):
        self.params.update(params)

    def process(self, low_res, slow_diff, fast_diff):
        """
        Process incoming low_res frame and diff frames step by step.
        Returns a dict of intermediate images and detection bounding boxes.
        """
        h, w = low_res.shape[:2]
        
        # Ensure diffs match low_res dimensions
        if slow_diff is None or slow_diff.shape != (h, w):
            slow_diff = np.zeros((h, w), dtype=np.uint8)
        if fast_diff is None or fast_diff.shape != (h, w):
            fast_diff = np.zeros((h, w), dtype=np.uint8)

        # Stage 1: Difference Combination using NumPy matrix math
        fw = self.params.get("fast_weight", 70) / 100.0
        sw = self.params.get("slow_weight", 30) / 100.0
        mode = self.params.get("diff_mode", 0)

        f_float = fast_diff.astype(np.float32)
        s_float = slow_diff.astype(np.float32)

        if mode == 0:
            # Weighted Sum
            comb = fw * f_float + sw * s_float
        elif mode == 1:
            # Fast minus Slow (Highlights objects moving faster than background/clouds)
            comb = np.maximum(0, f_float * fw - s_float * sw)
        elif mode == 2:
            # Maximum of Fast and Slow
            comb = np.maximum(f_float * fw, s_float * sw)
        else:
            # Minimum / Intersection
            comb = np.minimum(f_float * fw, s_float * sw)

        stage1_combined = np.clip(comb, 0, 255).astype(np.uint8)

        # Stage 2: Skyline & Border Suppression Mask
        stage2_masked = stage1_combined.copy()
        cutoff_pct = self.params.get("skyline_cutoff", 0)
        if cutoff_pct > 0:
            cutoff_y = int(h * (1.0 - cutoff_pct / 100.0))
            stage2_masked[cutoff_y:, :] = 0

        # Stage 3: Noise Reduction & Thresholding
        ksize = self.params.get("blur_kernel", 3)
        if ksize % 2 == 0:
            ksize += 1
        ksize = max(1, ksize)

        if ksize > 1:
            blurred = cv2.GaussianBlur(stage2_masked, (ksize, ksize), 0)
        else:
            blurred = stage2_masked

        t_val = self.params.get("thresh_val", 30)
        use_otsu = self.params.get("use_otsu", 0)

        if use_otsu == 1:
            _, stage3_thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        else:
            _, stage3_thresh = cv2.threshold(blurred, t_val, 255, cv2.THRESH_BINARY)

        # Stage 4: Morphological Operations (Opening to remove dots, Closing to merge fragments)
        mksize = self.params.get("morph_kernel", 3)
        if mksize % 2 == 0:
            mksize += 1
        mksize = max(1, mksize)

        open_iter = self.params.get("morph_open_iter", 1)
        close_iter = self.params.get("morph_close_iter", 1)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (mksize, mksize))
        stage4_morphed = stage3_thresh.copy()
        if open_iter > 0:
            stage4_morphed = cv2.morphologyEx(stage4_morphed, cv2.MORPH_OPEN, kernel, iterations=open_iter)
        if close_iter > 0:
            stage4_morphed = cv2.morphologyEx(stage4_morphed, cv2.MORPH_CLOSE, kernel, iterations=close_iter)

        # Stage 5: Contour & Candidate Object Extraction
        contours, _ = cv2.findContours(stage4_morphed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        min_area = self.params.get("min_area", 4)
        max_area = self.params.get("max_area", 800)
        min_solidity = self.params.get("min_solidity", 30) / 100.0
        max_ar = self.params.get("max_aspect_ratio", 40) / 10.0

        detections = []
        detection_overlay = cv2.cvtColor(low_res, cv2.COLOR_GRAY2BGR) if len(low_res.shape) == 2 else low_res.copy()

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if min_area <= area <= max_area:
                x, y, bw, bh = cv2.boundingRect(cnt)
                hull = cv2.convexHull(cnt)
                hull_area = cv2.contourArea(hull)
                solidity = (area / hull_area) if hull_area > 0 else 0
                aspect_ratio = max(bw / max(1, bh), bh / max(1, bw))

                if solidity >= min_solidity and aspect_ratio <= max_ar:
                    detections.append({
                        "x": x, "y": y, "w": bw, "h": bh,
                        "area": area, "solidity": solidity, "ar": aspect_ratio
                    })
                    # Draw detection bounding box
                    cv2.rectangle(detection_overlay, (x, y), (x + bw, y + bh), (0, 255, 0), 1)
                    cv2.putText(detection_overlay, f"{int(area)}p", (x, max(10, y - 2)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 255), 1)

        return {
            "inputs": {
                "low_res": low_res,
                "slow_diff": slow_diff,
                "fast_diff": fast_diff
            },
            "stages": {
                "1_combined": stage1_combined,
                "2_masked": stage2_masked,
                "3_thresh": stage3_thresh,
                "4_morphed": stage4_morphed,
                "5_detections": detection_overlay
            },
            "detections": detections
        }
