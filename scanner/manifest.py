import json
import logging
import os
import threading

_write_lock = threading.Lock()


def append(manifest_path, entry):
    try:
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
        with _write_lock, open(manifest_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        logging.error("Could not append to manifest %s: %s", manifest_path, e)
