# threading_utils.py

import os
import threading

def try_pin_and_prioritize(core_id=None, realtime_priority=None):
    """
    Best-effort: pin the calling thread to a core and/or raise its
    scheduling priority.
    
    Both require root and are Linux-only. Failures are swallowed on
    purpose -- this is an optimization, not a correctness requirement,
    and shouldn't crash the capture pipeline if run without privileges.
    """
    tid = threading.get_native_id()
    
    if core_id is not None:
        try:
            os.sched_setaffinity(tid, {core_id})
        except (PermissionError, OSError) as e:
            print(f"[warn] could not set core affinity: {e}")
            
    if realtime_priority is not None:
        try:
            param = os.sched_param(realtime_priority)
            os.sched_setscheduler(tid, os.SCHED_FIFO, param)
        except (PermissionError, OSError) as e:
            print(f"[warn] could not set realtime priority: {e}")
            