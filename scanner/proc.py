"""Subprocess supervision and shutdown handling"""

import logging
import signal
import subprocess
import threading

_live_procs = set()
_live_procs_lock = threading.Lock()
_shutdown = threading.Event()


def shutdown_requested():
    return _shutdown.is_set()


def request_shutdown():
    _shutdown.set()


def wait_for_shutdown(timeout):
    """Sleeps up to `timeout` seconds, returning early if shutdown is
    requested. Returns True if we're shutting down."""
    return _shutdown.wait(timeout)


def install_signal_handlers():
    def handler(signum, _frame):
        logging.warning("Received signal %s - terminating running scans", signum)
        _shutdown.set()
        terminate_live_procs()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def run(cmd, log_file):
    proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)
    with _live_procs_lock:
        _live_procs.add(proc)
    try:
        return proc.wait()
    finally:
        with _live_procs_lock:
            _live_procs.discard(proc)


def terminate_live_procs():
    with _live_procs_lock:
        procs = list(_live_procs)
    for proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass
