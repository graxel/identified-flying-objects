import cv2
import asyncio
import numpy as np
from detection.detector import YOLODetector, MotionDetector
from tracking.tracker import Tracker
from classification.classifier import ObjectClassifier
from database.db import AsyncSessionLocal
from database.models import Frame, Track, Detection
from sqlalchemy.future import select

class PipelineRunner:
    def __init__(self, video_source: str = "0", display: bool = False):
        self.video_source = video_source
        self.display = display
        
        # Initialize modules
        self.detector = YOLODetector()
        self.motion_detector = MotionDetector()
        self.tracker = Tracker()
        self.classifier = ObjectClassifier()
        
        # Video capture
        try:
            self.cap = cv2.VideoCapture(int(video_source))
        except ValueError:
            self.cap = cv2.VideoCapture(video_source)
            
    async def process_stream(self):
        frame_count = 0
        
        while self.cap.isOpened():
            ret, frame = self.cap.read()
            if not ret:
                break
                
            frame_count += 1
            
            # 1. Detection
            # Combine YOLO and Motion? For now, let's just use YOLO for simplicity of the prototype
            # and maybe use motion to filter areas if needed.
            detections = self.detector.detect(frame)
            
            # 2. Tracking
            # Update tracks
            active_tracks = self.tracker.update(detections, frame)
            
            # 3. Classification & Data Storage
            # For each track, we might want to classify the crop
            
            async with AsyncSessionLocal() as session:
                # Create Frame record
                db_frame = Frame(video_source=str(self.video_source))
                session.add(db_frame)
                await session.flush() # Get ID
                
                crops_to_classify = []
                track_metadata = []
                
                for track_data in active_tracks:
                    x, y, w, h, track_id = track_data
                    
                    # Ensure crop is within bounds
                    h_img, w_img = frame.shape[:2]
                    x = max(0, x)
                    y = max(0, y)
                    w = min(w, w_img - x)
                    h = min(h, h_img - y)
                    
                    if w > 0 and h > 0:
                        crop = frame[y:y+h, x:x+w]
                        crops_to_classify.append(crop)
                        track_metadata.append((x, y, w, h, track_id))
                
                # Batch classify
                if crops_to_classify:
                    predictions = self.classifier.predict(crops_to_classify)
                    
                    for i, (pred_class, conf) in enumerate(predictions):
                        x, y, w, h, track_id = track_metadata[i]
                        
                        # Check if track exists in DB, if not create
                        # In a real app we'd cache this or optimize
                        stmt = select(Track).where(Track.external_track_id == track_id, Track.session_id == 'current_session')
                        result = await session.execute(stmt)
                        db_track = result.scalar_one_or_none()
                        
                        if not db_track:
                            db_track = Track(external_track_id=track_id, session_id='current_session')
                            session.add(db_track)
                            await session.flush()
                        
                        # Save Detection
                        # Encode crop to bytes for storage (optional, might be heavy)
                        _, crop_encoded = cv2.imencode('.jpg', crops_to_classify[i])
                        crop_blob = crop_encoded.tobytes()
                        
                        detection = Detection(
                            frame_id=db_frame.id,
                            track_id=db_track.id,
                            bbox_x=float(x),
                            bbox_y=float(y),
                            bbox_w=float(w),
                            bbox_h=float(h),
                            confidence=float(conf),
                            class_label=pred_class,
                            crop_image=crop_blob
                        )
                        session.add(detection)
                
                await session.commit()
            
            # Visualization
            if self.display:
                for x, y, w, h, track_id in active_tracks:
                    cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
                    cv2.putText(frame, f"ID: {track_id}", (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                
                cv2.imshow("Pipeline", frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
                    
        self.cap.release()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    import argparse
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    default_ip = os.getenv("CAM_IP", "192.168.0.30")
    default_source = f"http://{default_ip}:81/stream"

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=str, default=default_source, help="Video source (0, 1, or URL)")
    args = parser.parse_args()
    
    runner = PipelineRunner(video_source=args.source, display=True)
    asyncio.run(runner.process_stream())
