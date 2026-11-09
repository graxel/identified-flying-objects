"""
Messages between capture.py (laptop) and camera_node.py (on each Pi), over SSH.

Camera node -> laptop, on the node's stdout: framed binary messages, each a
HEADER (kind, payload length) followed by the payload.

  HELLO    camera started; sizes it actually configured (JSON)
  PREVIEW  frame sequence number, ms the frame sat on the Pi, JPEG
  ACK      capture set number, whether a frame was grabbed for it
  PHOTO    capture set number, full-res Y plane
  ERROR    traceback text

Laptop -> camera node, on the node's stdin: one text line per capture
request, "C <set number>".
"""

import json
import struct
import threading

import numpy as np

HEADER      = struct.Struct("!cI")   # message kind (1 byte) + payload length
PREVIEW_HDR = struct.Struct("!II")   # frame sequence number, Pi pipeline lag (ms)
ACK_HDR     = struct.Struct("!IB")   # capture set number, ACK_* status
PHOTO_HDR   = struct.Struct("!I")    # capture set number

MSG_HELLO   = b"H"
MSG_PREVIEW = b"P"
MSG_ACK     = b"A"
MSG_PHOTO   = b"M"
MSG_ERROR   = b"E"

ACK_GRABBED, ACK_LATE = 0, 1   # LATE: no frame arrived in time, request rejected


# ---- Payloads -------------------------------------------------------------

def encode_hello(main_size, preview_size, model, sensor_mode):
    return json.dumps({"main_size": main_size, "preview_size": preview_size,
                       "model": model, "sensor_mode": sensor_mode}).encode()


def decode_hello(payload):
    hello = json.loads(payload)
    hello["main_size"]    = tuple(hello["main_size"])
    hello["preview_size"] = tuple(hello["preview_size"])
    return hello


def encode_preview(seq, lag_ms, jpeg_bytes):
    return PREVIEW_HDR.pack(seq, min(lag_ms, 0xFFFFFFFF)) + jpeg_bytes


def decode_preview(payload):
    """Returns (seq, lag_ms, JPEG bytes as a uint8 array)."""
    seq, lag_ms = PREVIEW_HDR.unpack_from(payload)
    return seq, lag_ms, np.frombuffer(payload, np.uint8, offset=PREVIEW_HDR.size)


def encode_ack(set_no, status):
    return ACK_HDR.pack(set_no, status)


def decode_ack(payload):
    """Returns (set_no, status)."""
    return ACK_HDR.unpack(payload)


def encode_photo(set_no, y_plane):
    return PHOTO_HDR.pack(set_no) + y_plane.tobytes()


def decode_photo(payload, size):
    """Returns (set_no, Y plane as an (h, w) uint8 array)."""
    (set_no,) = PHOTO_HDR.unpack_from(payload)
    w, h = size
    return set_no, np.frombuffer(payload, np.uint8, offset=PHOTO_HDR.size).reshape(h, w)


def capture_request_line(set_no):
    return f"C {set_no}\n".encode()


def parse_capture_request(line):
    """Returns the set number, or None if the line isn't a capture request."""
    parts = line.split()
    if len(parts) == 2 and parts[0] == "C":
        return int(parts[1])
    return None


# ---- Framing --------------------------------------------------------------

class MessageWriter:
    """Writes framed messages to a stream. Several threads share it, so writes are locked."""

    def __init__(self, stream):
        self.stream = stream
        self.lock   = threading.Lock()

    def send(self, kind, payload):
        with self.lock:
            self.stream.write(HEADER.pack(kind, len(payload)))
            self.stream.write(payload)
            self.stream.flush()


def _read_exact(stream, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def read_message(stream):
    """Returns (kind, payload), or (None, None) at end of stream."""
    head = _read_exact(stream, HEADER.size)
    if head is None:
        return None, None
    kind, length = HEADER.unpack(head)
    payload = _read_exact(stream, length) if length else b""
    if payload is None:
        return None, None
    return kind, payload
