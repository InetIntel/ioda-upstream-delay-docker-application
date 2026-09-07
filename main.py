#!/usr/bin/env python3
"""IODA Upstream Delay vantage point.

Runs yarrp on a clock-aligned interval and uploads the raw .yrp output to the
IODA collection server, which does all parsing and annotation. See README.md.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
import os

from iupd import config as config_mod
from iupd import proc, scheduler


def setup_logging(log_path):
    directory = os.path.dirname(log_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            RotatingFileHandler(
                log_path, mode="a", maxBytes=30 * 1024 * 1024, backupCount=3
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main():
    try:
        cfg, warnings = config_mod.load()
    except config_mod.ConfigError as e:
        # Logging isn't configured yet, and a bad config should be loud.
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    setup_logging(cfg["paths"]["log_path"])
    for warning in warnings:
        logging.warning(warning)
    logging.info("Configuration loaded from %s", cfg["config_path"])

    for key in ("staging_dir", "outbox_dir", "scan_log_dir"):
        os.makedirs(cfg["paths"][key], exist_ok=True)

    proc.install_signal_handlers()
    scheduler.run_forever(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
