"""
ZMQ Forwarder / Proxy Service running on kalman (or central hub).

Architecture:
  - Video Stream (Camera -> Workbench):
      Camera nodes PUB connect to tcp://kalman.local:8000 (Proxy XSUB)
      Workbench SUB connects to tcp://<kalman-tailscale-or-ip>:8002 (Proxy XPUB)
  - Parameter Push (Workbench -> Camera nodes):
      Workbench PUB connects to tcp://<kalman-tailscale-or-ip>:8003 (Proxy XSUB)
      Camera nodes SUB connect to tcp://kalman.local:8001 (Proxy XPUB)
"""

import argparse
import signal
import sys
import threading
import zmq


def run_proxy(frontend_type, frontend_bind, backend_type, backend_bind, name="Proxy"):
    """Runs a zmq.proxy between frontend and backend in a loop."""
    context = zmq.Context.instance()
    frontend = context.socket(frontend_type)
    backend = context.socket(backend_type)

    frontend.bind(frontend_bind)
    backend.bind(backend_bind)

    print(f"[{name}] Frontend ({frontend_type.name}) bound to {frontend_bind}")
    print(f"[{name}] Backend  ({backend_type.name}) bound to {backend_bind}")

    try:
        zmq.proxy(frontend, backend)
    except (zmq.ContextTerminated, zmq.ZMQError):
        pass
    finally:
        frontend.close()
        backend.close()


def main():
    parser = argparse.ArgumentParser(description="ZMQ Proxy for Camera Nodes & Workbench on kalman")
    parser.add_argument("--cam-stream-port", type=int, default=8000,
                        help="Port where cameras connect to send video patches (PUB -> XSUB, default: 8000)")
    parser.add_argument("--workbench-stream-port", type=int, default=8002,
                        help="Port where workbench connects to receive video patches (XPUB -> SUB, default: 8002)")
    parser.add_argument("--cam-param-port", type=int, default=8001,
                        help="Port where cameras connect to subscribe to params (XPUB -> SUB, default: 8001)")
    parser.add_argument("--workbench-param-port", type=int, default=8003,
                        help="Port where workbench connects to push params (PUB -> XSUB, default: 8003)")
    parser.add_argument("--bind-addr", type=str, default="0.0.0.0",
                        help="IP address to bind all sockets (default: 0.0.0.0)")
    args = parser.parse_args()

    context = zmq.Context.instance()

    # 1. Video stream proxy (Camera PUB -> XSUB : XPUB -> Workbench SUB)
    stream_thread = threading.Thread(
        target=run_proxy,
        args=(
            zmq.XSUB, f"tcp://{args.bind_addr}:{args.cam_stream_port}",
            zmq.XPUB, f"tcp://{args.bind_addr}:{args.workbench_stream_port}",
            "Video Stream",
        ),
        daemon=True,
    )

    # 2. Param update proxy (Workbench PUB -> XSUB : XPUB -> Camera SUB)
    param_thread = threading.Thread(
        target=run_proxy,
        args=(
            zmq.XSUB, f"tcp://{args.bind_addr}:{args.workbench_param_port}",
            zmq.XPUB, f"tcp://{args.bind_addr}:{args.cam_param_port}",
            "Param Push",
        ),
        daemon=True,
    )

    stream_thread.start()
    param_thread.start()

    print("ZMQ Proxy running on kalman. Press Ctrl+C to terminate.")

    def shutdown(sig, frame):
        print("\nShutting down ZMQ Proxy...")
        context.term()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Keep main thread alive
    stream_thread.join()
    param_thread.join()


if __name__ == "__main__":
    main()
