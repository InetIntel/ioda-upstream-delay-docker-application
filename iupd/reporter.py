"""Uploading finished scans.

Only .yrp files are sent, and only from the outbox - a file arrives there
once its scan has exited cleanly, so a truncated result from a killed
container is never uploaded. rsync removes each file as it goes, so anything
left behind after an unreachable server is retried on the next cycle.
"""

import logging
import os
import subprocess

from . import proc


def pending_files(outbox_dir):
    try:
        names = sorted(os.listdir(outbox_dir))
    except FileNotFoundError:
        return []
    return [
        os.path.join(outbox_dir, name)
        for name in names
        if name.endswith(".yrp") and os.path.isfile(os.path.join(outbox_dir, name))
    ]


def build_command(files, reporting, ssh_identity_file):
    ssh = (
        f"ssh -i {ssh_identity_file} -p {reporting['port']} "
        f'-o "StrictHostKeyChecking no"'
    )
    return [
        "rsync",
        "-avHP",
        "--remove-source-files",
        "--bwlimit=" + str(reporting["bw_limit"]),
        "-e", ssh,
    ] + files + [reporting["destination"]]


def report(cfg):
    """Uploads everything in the outbox. Never raises - an unreachable server
    is an ordinary condition, and the files stay put for the next cycle."""
    if proc.shutdown_requested():
        return

    files = pending_files(cfg["paths"]["outbox_dir"])
    if not files:
        logging.info("Nothing to upload")
        return

    command = build_command(files, cfg["reporting"], cfg["reporting"]["ssh_identity_file"])
    logging.info("Uploading %d file(s): %s", len(files), " ".join(command))
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = process.communicate()
    except OSError as e:
        logging.error("Could not run rsync: %s", e)
        return

    if process.returncode == 0:
        logging.info("Upload succeeded:\n%s", stdout.decode(errors="replace"))
    else:
        logging.error(
            "Upload failed (rsync exited %s) - files kept for the next cycle:\n%s",
            process.returncode, stderr.decode(errors="replace"),
        )
