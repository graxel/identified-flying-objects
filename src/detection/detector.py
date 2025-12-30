import cv2
import numpy as np
from ultralytics import YOLO
from typing import List, Tuple, Optional

class YOLODetector:
    def __init__(self, model_path: str = "yolov8n.pt", conf_threshold: float = 0.5):
        self.model = YOLO(model_path)
        self.conf_threshold = conf_threshold

    def detect(self, frame: np.ndarray) -> List[Tuple[int, int, int, int, float, int]]:
        """
        Detects objects in the frame.
        Returns a list of (x, y, w, h, conf, class_id) tuples.
        """
        results = self.model(frame, verbose=False)
        detections = []
        
        for result in results:
            boxes = result.boxes
            for box in boxes:
                conf = float(box.conf[0])
                if conf < self.conf_threshold:
                    continue
                
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                w = x2 - x1
                h = y2 - y1
                cls = int(box.cls[0])
                
                detections.append((x1, y1, w, h, conf, cls))
        
        return detections

class MotionDetector:
    def __init__(self, history: int = 500, var_threshold: int = 16, detect_shadows: bool = True):
        self.back_sub = cv2.createBackgroundSubtractorMOG2(
            history=history, varThreshold=var_threshold, detectShadows=detect_shadows
        )
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

    def detect(self, frame: np.ndarray, min_area: int = 50) -> List[Tuple[int, int, int, int]]:
        """
        Detects moving objects using background subtraction.
        Returns a list of (x, y, w, h) tuples.
        """
        fg_mask = self.back_sub.apply(frame)
        
        # Morphological operations to remove noise
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, self.kernel)
        
        contours, _ = cv2.findContours(fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        detections = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area > min_area:
                x, y, w, h = cv2.boundingRect(cnt)
                detections.append((x, y, w, h))
        
        return detections
