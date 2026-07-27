# system_utils.py

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


def read_cpu_temp_c():
    """Read CPU temperature from sysfs. Returns 0.0 on failure."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return 0.0


def read_mem_used_pct():
    """Read memory usage percentage from /proc/meminfo. Returns 0.0 on failure."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if parts[0] in ("MemTotal:", "MemAvailable:"):
                    info[parts[0]] = int(parts[1])
                if len(info) == 2:
                    break
        total = info["MemTotal:"]
        available = info["MemAvailable:"]
        return (total - available) / total * 100.0
    except (OSError, ValueError, KeyError, ZeroDivisionError):
        return 0.0
