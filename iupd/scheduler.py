"""The measurement loop.

Cycles are aligned to the machine's clock, so an interval of 1800 seconds
starts a cycle on every half hour and every vantage point in the fleet
measures over the same windows. A cycle that overruns its interval simply
lands on the following boundary rather than drifting.
"""

import datetime
import logging
import os

from . import naming, prober, proc, rates, reporter, targets


def ceil_datetime(dt, delta):
    """The next multiple of `delta` at or after `dt`, measured from midnight."""
    return dt + (datetime.datetime.min - dt) % delta


def load_profiles(cfg):
    """Buckets every profile's targets by (protocol, port).

    Done at the top of each cycle rather than once at startup, so editing a
    target list takes effect on the next cycle without a restart. A profile
    whose targets or probe rate don't validate is skipped with an error - one
    bad profile shouldn't stop the others from measuring.
    """
    profiles = []
    for profile in cfg["scans"]:
        name = profile["name"]
        targets_path = targets.resolve_targets_path(profile, cfg["paths"])
        try:
            buckets = targets.bucket_targets(targets_path)
        except (targets.TargetFileError, OSError) as e:
            logging.error("[%s] Skipping profile: %s", name, e)
            continue
        if not buckets:
            logging.warning("[%s] Skipping profile: no targets in %s", name, targets_path)
            continue
        try:
            prober.resolve_probe_rates(profile, buckets, cfg)
        except rates.RateAllocationError as e:
            logging.error("[%s] Skipping profile: %s", name, e)
            continue
        profiles.append((profile, buckets))
    return profiles


def warn_if_short_on_cpus(profiles):
    widest = max(
        (len(buckets) for profile, buckets in profiles if profile.get("parallel")),
        default=1,
    )
    cpus = os.cpu_count() or 1
    if widest > 1 and widest + 1 > cpus:
        logging.warning(
            "Up to %d concurrent scans on %d cpu(s). yarrp busy-spins to hold its "
            "probe rate, so scans may under-deliver their allocated pps - check "
            "achieved_pps in the manifest.",
            widest, cpus,
        )


def run_cycle(cfg, cycle_start, deadline):
    profiles = load_profiles(cfg)
    if not profiles:
        logging.error("No runnable profiles this cycle")
        return
    warn_if_short_on_cpus(profiles)

    for profile, buckets in profiles:
        name = profile["name"]
        for bucket, ip_list in buckets.items():
            logging.info(
                "[%s] bucket %s: %d targets",
                name, naming.bucket_tag(*bucket), len(ip_list),
            )

    for profile, buckets in profiles:
        if proc.shutdown_requested():
            logging.warning("Shutting down - skipping remaining profiles")
            return
        entries = prober.run_profile(profile, buckets, cfg, cycle_start, deadline)
        succeeded = sum(1 for entry in entries if entry.get("succeeded"))
        logging.info(
            "[%s] Profile complete: %d/%d bucket(s) succeeded",
            profile["name"], succeeded, len(entries),
        )


def run_forever(cfg):
    interval = datetime.timedelta(seconds=cfg["prober"]["interval"])
    logging.info(
        "IODA Upstream Delay vantage point %s starting - interval %ds, %d profile(s)",
        cfg["vp"]["hostname"], cfg["prober"]["interval"], len(cfg["scans"]),
    )

    while not proc.shutdown_requested():
        cycle_start = datetime.datetime.now()
        deadline = ceil_datetime(cycle_start, interval)
        try:
            reporter.report(cfg)
            logging.info(
                "=== Cycle starting %s (next cycle due %s) ===",
                cycle_start.strftime("%Y-%m-%d %H:%M:%S"),
                deadline.strftime("%H:%M:%S"),
            )
            run_cycle(cfg, cycle_start, deadline)
            reporter.report(cfg)
        except Exception:
            # A cycle that blows up shouldn't take the vantage point down; the
            # next one gets a clean attempt.
            logging.exception("Cycle failed")

        if proc.shutdown_requested():
            break

        # Deliberately outside the try above: an exception must not skip the
        # wait and spin the loop.
        now = datetime.datetime.now()
        next_run = ceil_datetime(now, interval)
        sleep_seconds = (next_run - now).total_seconds()
        if next_run > deadline:
            logging.warning(
                "Cycle overran its interval - next cycle at %s",
                next_run.strftime("%H:%M:%S"),
            )
        logging.info("Sleeping %.0fs until %s", sleep_seconds, next_run.strftime("%H:%M:%S"))
        proc.wait_for_shutdown(sleep_seconds)

    logging.info("Shutdown complete")
