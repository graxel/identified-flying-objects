"""
camera_relay.py
MJPEG Camera Relay Server
Connects to two upstream cameras and relays their streams to web clients.
Each camera connection is single-threaded to respect the "single accessor" limitation.
"""

import threading
import logging
import time
from threading import Lock
from flask import Flask, Response
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Enable CORS for all routes (allows access from main website)
from flask_cors import CORS
CORS(app)

# Camera configuration
CAMERAS = {
    'cam1': {
        'name': 'Camera 1',
        'url': 'http://192.168.0.36:81/stream',
        'timeout': 10,
    },
    'cam2': {
        'name': 'Camera 2',
        'url': 'http://192.168.0.37:81/stream',
        'timeout': 10,
    }
}

# MJPEG boundary marker (use a marker unlikely to appear in JPEG data)
# BOUNDARY = 'mjpegframe'
BOUNDARY = '123456789000000000000987654321'


class MJPEGRelay:
    """
    Handles reading from a single upstream MJPEG stream and buffering
    the latest frame for distribution to multiple web clients.
    """
    
    def __init__(self, camera_id, camera_config):
        self.camera_id = camera_id
        self.name = camera_config['name']
        self.url = camera_config['url']
        self.timeout = camera_config['timeout']
        
        # Frame buffer state
        self.current_frame = None
        self.frame_lock = Lock()
        self.frame_count = 0
        self.error_count = 0
        self.last_update = None
        
        # Control
        self.running = False
        self.thread = None
        
    def start(self):
        """Start the background capture thread."""
        if not self.running:
            self.running = True
            self.thread = threading.Thread(
                target=self._capture_loop,
                daemon=True,
                name=f'MJPEGCapture-{self.camera_id}'
            )
            self.thread.start()
            logger.info(f'{self.name} ({self.camera_id}): Relay started')
    
    def stop(self):
        """Stop the background capture thread."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info(f'{self.name} ({self.camera_id}): Relay stopped')
    

    def _capture_loop(self):
        """Background thread: continuously read from upstream and buffer frames."""
        while self.running:
            try:
                self._read_stream()
            except Exception as e:
                logger.error(f'{self.name} ({self.camera_id}): Unexpected error: {e}')
            
            if self.running:
                logger.info(f'{self.name} ({self.camera_id}): Reconnecting in 5s...')
                time.sleep(5)  # Simple backoff between connection attempts

    
    def _read_stream(self):
        """Read MJPEG stream and buffer frames."""
        session = requests.Session()
        
        # Minimal retry - just connect once and stream continuously
        retry_strategy = Retry(
            total=0,
            connect=0,
            read=0,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        
        logger.info(f'{self.name} ({self.camera_id}): Connecting to {self.url}')
        
        try:
            response = session.get(
                self.url,
                stream=True,
                timeout=(5, 600),  # Increased read timeout to 60s
                headers={'User-Agent': 'MJPEGRelay/1.0'}
            )
            response.raise_for_status()
            
            logger.info(f'{self.name} ({self.camera_id}): Stream connected, parsing frames')
            
            # Parse MJPEG stream - look for JPEG markers directly
            buffer = b''
            
            # Use smaller chunks and process all complete frames each iteration
            for chunk in response.iter_content(chunk_size=1024, decode_unicode=False):
                if not self.running:
                    break
                
                if not chunk:
                    continue
                
                buffer += chunk
                
                # JPEG frames are delimited by FFD8 (start) and FFD9 (end)
                # Process all complete frames in buffer
                while True:
                    jpeg_start = buffer.find(b'\xff\xd8')
                    if jpeg_start == -1:
                        # No complete frame, keep buffering
                        break
                    
                    jpeg_end = buffer.find(b'\xff\xd9', jpeg_start)
                    if jpeg_end == -1:
                        # Frame incomplete, keep buffering
                        break
                    
                    jpeg_end += 2  # Include the FFD9 marker
                    jpeg_data = buffer[jpeg_start:jpeg_end]
                    
                    # Update buffer with frame
                    with self.frame_lock:
                        self.current_frame = jpeg_data
                        self.frame_count += 1
                        self.last_update = time.time()
                    
                    # Remove processed frame from buffer
                    buffer = buffer[jpeg_end:]
                    
                    # Log occasionally so we know it's working
                    if self.frame_count % 30 == 0:
                        logger.debug(f'{self.name} ({self.camera_id}): Parsed {self.frame_count} frames')
        
        except Exception as e:
            logger.error(f'{self.name} ({self.camera_id}): Stream read error: {e}')
        finally:
            session.close()


    def get_stream_generator(self):
        """
        Generator that yields MJPEG-formatted frames to HTTP clients.
        Each client gets its own generator instance but shares the buffered frame.
        """
        boundary_marker = f'--{BOUNDARY}\r\n'.encode()
        content_type = b'Content-Type: image/jpeg\r\n'
        
        while self.running:
            with self.frame_lock:
                frame = self.current_frame
            
            if frame:
                try:
                    # Format: --boundary\r\nContent-Type: image/jpeg\r\nContent-Length: N\r\n\r\n[JPEG]\r\n
                    content_length = f'Content-Length: {len(frame)}\r\n\r\n'.encode()
                    payload = (
                        boundary_marker +
                        content_type +
                        content_length +
                        frame +
                        b'\r\n'
                    )
                    yield payload
                except Exception as e:
                    logger.error(f'Error generating frame for {self.camera_id}: {e}')
            
            # Control frame rate (~30 FPS, adjust as needed)
            time.sleep(0.033)
    
    def get_status(self):
        """Return current relay status."""
        return {
            'camera_id': self.camera_id,
            'name': self.name,
            'url': self.url,
            'running': self.running,
            'frames_captured': self.frame_count,
            'errors': self.error_count,
            'last_update': self.last_update,
        }


# Initialize relays for each camera
relays = {
    camera_id: MJPEGRelay(camera_id, config)
    for camera_id, config in CAMERAS.items()
}

# Track if relays have been started
_relays_started = False


@app.route('/stream/<camera_id>')
def stream(camera_id):
    """
    MJPEG stream endpoint for a specific camera.
    Usage: <img src="/stream/cam1" /> in HTML
    """
    if camera_id not in relays:
        return 'Camera not found', 404
    
    relay = relays[camera_id]
    
    return Response(
        relay.get_stream_generator(),
        mimetype=f'multipart/x-mixed-replace; boundary={BOUNDARY}',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0',
        }
    )


@app.route('/status')
def status():
    """Health check endpoint."""
    import json
    status_data = {
        camera_id: relay.get_status()
        for camera_id, relay in relays.items()
    }
    return json.dumps(status_data, indent=2, default=str), 200, {'Content-Type': 'application/json'}


@app.route('/health')
def health():
    """Simple health check for load balancers."""
    all_running = all(r.running for r in relays.values())
    status_code = 200 if all_running else 503
    return 'OK' if all_running else 'Service Unavailable', status_code


@app.route('/')
def index():
    """Serve the camera monitor page."""
    from flask import render_template
    return render_template('index.html')


@app.before_request
def ensure_relays_started():
    """Ensure relays are started only once."""
    global _relays_started
    if not _relays_started:
        for relay in relays.values():
            if not relay.running:
                relay.start()
        _relays_started = True


# Start relays on app startup
for relay in relays.values():
    relay.start()
