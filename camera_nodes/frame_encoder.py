import cv2
import queue

def encoder_worker_thread(encoder_queue, send_queue):
    while True:
        try:
            job = encoder_queue.get()
            
            ml_train_frame = job.get("ml_train_frame")
            low_res_gray = job.get("low_res_gray")
            
            ml_train_full = None
            low_res_full = None
            
            if ml_train_frame is not None:
                ml_train_full = ml_train_frame.tobytes()
                
            if low_res_gray is not None:
                success, jpeg_bytes = cv2.imencode(".jpg", low_res_gray, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if success:
                    low_res_full = jpeg_bytes.tobytes()
                    
            # Put back into send_queue
            send_obj = {
                "type": "frames",
                "camera_id": job["camera_id"],
                "sensor_ts_ns": job["sensor_ts_ns"],
                "ml_train_full": ml_train_full,
                "ml_train_shape": ml_train_frame.shape if ml_train_frame is not None else None,
                "low_res_full": low_res_full,
                "low_res_shape": low_res_gray.shape if low_res_gray is not None else None,
            }
            try:
                send_queue.put(send_obj, block=False)
            except queue.Full:
                pass # Drop frames if network is saturated
                
        except Exception as e:
            print(f"Encoder worker error: {e}")
        finally:
            encoder_queue.task_done()
