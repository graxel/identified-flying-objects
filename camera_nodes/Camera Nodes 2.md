# Camera Nodes

**Hardware:** Raspberry Pi AI Camera on a Raspberry Pi Zero 2W host

The main goal of each camera node is to capture images, identify interesting patches in those images, extract the patches and their locations, and send them to a central server for processing as fast as possible.

This code now runs with one request-owned critical-path thread plus two post-release worker threads:

- critical-path capture and processing,
- frame encoding,
- network send.

## Critical-path thread

A single thread owns the camera request from `capture_request()` through `request.release()`. That thread performs the minimum request-lifetime work in one place:

- capture a request,
- access the low-resolution and full-resolution mapped buffers,
- derive bounding boxes from AI output and, later, frame differencing,
- extract patches from the full-resolution frame,
- copy only the durable outputs that must survive request release,
- release the request.

This keeps the request lifetime explicit and avoids queueing live camera requests between threads.

## Encoder thread

The encoder thread receives copied post-release frame data from the critical-path thread. It performs slower work, such as JPEG encoding of the low-resolution frame, without extending camera buffer lifetime.

## Sender thread

The sender thread publishes patches, full-frame payloads, and telemetry over ZeroMQ. Because it only sees copied post-release data, network stalls do not directly hold camera requests open.