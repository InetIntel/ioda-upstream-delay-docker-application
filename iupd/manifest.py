"""The local record of what each scan actually did.

Manifests stay on the vantage point - only .yrp files are uploaded, because
the server feeds everything in its incoming directory to yrp2text without
checking the extension. What the manifest is for is answering, locally,
whether a scan delivered the probe rate it was configured for, which target
file produced a given upload, and how many attempts it took.
"""

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
        # A manifest we couldn't write is worth complaining about, but it is
        # not worth losing the scan over.
        logging.error("Could not append to manifest %s: %s", manifest_path, e)
