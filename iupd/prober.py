"""Running yarrp.

One profile at a time, and within a profile one yarrp per (protocol, port)
bucket - yarrp accepts a single probe type and port per invocation. A
profile's buckets can run concurrently, which collapses yarrp's fixed 60s
straggler wait from once per bucket into once for the group, and means every
protocol observes the same network conditions rather than being staggered
across the cycle.

A bucket that fails is retried, up to options.max_attempts, for as long as a
retry can be expected to finish before the next measurement cycle is due.
"""

import datetime
import logging
import os
import shutil
import threading
import time
from collections import OrderedDict

from . import config as config_mod
from . import manifest, naming, proc, rates

# yarrp sleeps a fixed 60s after its last probe, letting the listener collect
# straggling replies (SHUTDOWN_WAIT in yarrp.h). It's included in the
# "Elapsed" yarrp reports, so subtract it to get time actually spent probing.
YARRP_SHUTDOWN_WAIT = 60


def parse_yarrp_trailer(yrp_path):
    """Reads the stats yarrp writes into the tail of its own output file.

    Per-process, so it stays correct when buckets run concurrently - and more
    accurate than interface counters even when they don't, since those also
    count ssh, arp, and whatever else crossed the interface.
    """
    stats = {}
    try:
        with open(yrp_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return stats

    fields = {"Pkts": "probes_sent", "Elapsed": "yarrp_elapsed", "Bad_Resp": "bad_responses"}
    for line in tail.splitlines():
        if not line.startswith("#") or ":" not in line:
            continue
        key, _, value = line[1:].partition(":")
        field = fields.get(key.strip())
        if not field:
            continue
        try:
            stats[field] = float(value.strip().rstrip("s"))
        except ValueError:
            continue
    for key in ("probes_sent", "bad_responses"):
        if key in stats:
            stats[key] = int(stats[key])
    return stats


def scan_failed(result):
    """Whether a scan needs retrying.

    An unparseable trailer is deliberately not a failure - probes_sent is
    unknown rather than zero, and re-probing a target list because yarrp
    changed its output format would be worse than recording the gap.
    """
    if result.get("yarrp_returncode") != 0:
        return True
    if not result.get("yrp_bytes"):
        return True
    probes_sent = result.get("probes_sent")
    return probes_sent is not None and probes_sent == 0


def failure_reason(result):
    if result.get("error"):
        return result["error"]
    if result.get("yarrp_returncode") != 0:
        return f"yarrp exited {result.get('yarrp_returncode')}"
    if not result.get("yrp_bytes"):
        return "no output file"
    if result.get("probes_sent") == 0:
        return "zero probes sent"
    return "unknown"


def estimate_seconds(target_count, max_ttl, probe_rate):
    """How long a bucket should take: one probe per target per hop, at the
    allocated rate, plus yarrp's fixed straggler wait."""
    return (target_count * max_ttl) / float(probe_rate) + YARRP_SHUTDOWN_WAIT


def move_into(src, dst_dir):
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, os.path.basename(src))
    try:
        os.replace(src, dst)
    except OSError:
        # staging and outbox on different filesystems
        shutil.move(src, dst)
    return dst


def remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def write_bucket_targets(path, ip_list):
    with open(path, "w") as f:
        for ip in ip_list:
            f.write(ip + "\n")


def run_one_scan(profile, bucket, ip_list, probe_rate, instance, cfg, cycle_start, attempt):
    """Runs yarrp for one bucket and, if it succeeded, moves the .yrp into
    the outbox. Returns a result dict; never raises for a scan failure."""
    protocol, port = bucket
    paths = cfg["paths"]
    hostname = cfg["vp"]["hostname"]
    max_ttl = config_mod.profile_max_ttl(profile, cfg)

    label = naming.scan_label(profile["name"], protocol, port)
    run_prefix = naming.run_prefix(hostname, cycle_start, label)
    log_label = f"{label} attempt {attempt}"

    yrp_path = os.path.join(paths["staging_dir"], naming.yrp_filename(hostname, cycle_start, label))
    bucket_targets_path = os.path.join(paths["staging_dir"], f"{run_prefix}.targets.txt")
    scan_log_path = os.path.join(paths["scan_log_dir"], f"{run_prefix}.attempt{attempt}.log")

    os.makedirs(paths["staging_dir"], exist_ok=True)
    os.makedirs(paths["scan_log_dir"], exist_ok=True)
    write_bucket_targets(bucket_targets_path, ip_list)

    yarrp_cmd = [
        paths["yarrp_bin"],
        "-o", yrp_path,
        "-i", bucket_targets_path,
        "-r", str(probe_rate),
        "-t", protocol,
        "-v",
        "-m", str(max_ttl),
    ]
    if port is not None:
        yarrp_cmd += ["-p", str(port)]
    if instance is not None:
        # Only passed when buckets run concurrently. yarrp encodes the
        # instance in the high byte of the probe's IP ID and its listener
        # drops replies carrying anyone else's - necessary because every
        # yarrp opens a raw ICMP socket and the kernel hands each one a copy
        # of every inbound ICMP packet on the host.
        yarrp_cmd += ["-E", str(instance)]

    logging.info(
        "[%s] Starting yarrp: %d targets @ %dpps, max_ttl %d - %s",
        log_label, len(ip_list), probe_rate, max_ttl, " ".join(yarrp_cmd),
    )

    scan_start = datetime.datetime.now()
    started = time.time()
    try:
        with open(scan_log_path, "w") as scan_log:
            returncode = proc.run(yarrp_cmd, scan_log)
        error = None
    except OSError as e:
        returncode, error = None, str(e)
        logging.error("[%s] Could not run yarrp: %s", log_label, e)
    elapsed = time.time() - started

    # The per-bucket target list is scratch; the scan log is always kept, so
    # there is something to read when a scan dies.
    remove_quietly(bucket_targets_path)

    trailer = parse_yarrp_trailer(yrp_path)
    probes_sent = trailer.get("probes_sent")
    yarrp_elapsed = trailer.get("yarrp_elapsed")
    probe_seconds = achieved_pps = None
    if probes_sent is not None and yarrp_elapsed is not None:
        probe_seconds = max(yarrp_elapsed - YARRP_SHUTDOWN_WAIT, 1)
        achieved_pps = probes_sent / probe_seconds

    try:
        yrp_bytes = os.path.getsize(yrp_path)
    except OSError:
        yrp_bytes = 0

    result = {
        "vp": hostname,
        "profile": profile["name"],
        "label": label,
        "protocol": protocol,
        "port": port,
        "targets_file": profile["targets_file"],
        "target_count": len(ip_list),
        "cycle_start": cycle_start.isoformat(),
        "scan_start": scan_start.isoformat(),
        "attempt": attempt,
        "instance": instance,
        "allocated_probe_rate": probe_rate,
        "profile_probe_rate": config_mod.profile_probe_rate(profile, cfg),
        "max_ttl": max_ttl,
        "elapsed_seconds": round(elapsed, 1),
        "probes_sent": probes_sent,
        "probe_seconds": round(probe_seconds, 1) if probe_seconds else None,
        "achieved_pps": round(achieved_pps, 1) if achieved_pps else None,
        "bad_responses": trailer.get("bad_responses"),
        "yarrp_returncode": returncode,
        "yrp_bytes": yrp_bytes,
        "scan_log": scan_log_path,
        "error": error,
    }

    if scan_failed(result):
        logging.error(
            "[%s] FAILED after %.1fs (%s) - see %s",
            log_label, elapsed, failure_reason(result), scan_log_path,
        )
        # A partial .yrp is not worth uploading and would only be re-created
        # by the retry.
        remove_quietly(yrp_path)
        result["yrp_file"] = None
        return result

    result["yrp_file"] = move_into(yrp_path, paths["outbox_dir"])
    if achieved_pps is not None:
        logging.info(
            "[%s] Complete in %.1fs: probes_sent=%d allocated_pps=%d achieved_pps=%.1f",
            log_label, elapsed, probes_sent, probe_rate, achieved_pps,
        )
    else:
        logging.info("[%s] Complete in %.1fs", log_label, elapsed)
    return result


def _worker(results, bucket, args):
    # Positions match run_one_scan's signature.
    profile, bucket_key, cfg, attempt = args[0], args[1], args[5], args[7]
    try:
        results[bucket] = run_one_scan(*args)
    except Exception as e:  # keep one bucket's failure from losing the group
        logging.exception(
            "[%s %s] Unexpected error: %s", profile["name"], naming.bucket_tag(*bucket_key), e
        )
        results[bucket] = {
            "vp": cfg["vp"]["hostname"],
            "profile": profile["name"],
            "label": naming.scan_label(profile["name"], *bucket_key),
            "protocol": bucket_key[0],
            "port": bucket_key[1],
            "target_count": len(args[2]),
            "cycle_start": args[6].isoformat(),
            "attempt": attempt,
            "yarrp_returncode": None,
            "yrp_bytes": 0,
            "yrp_file": None,
            "error": str(e),
        }


def resolve_probe_rates(profile, buckets, cfg):
    """Returns (rates, parallel) for one attempt over `buckets`."""
    total_rate = config_mod.profile_probe_rate(profile, cfg)
    parallel = bool(profile.get("parallel", False)) and len(buckets) > 1
    if not parallel:
        return OrderedDict((bucket, total_rate) for bucket in buckets), False
    weights = [(bucket, len(ip_list)) for bucket, ip_list in buckets.items()]
    return rates.allocate(weights, total_rate, cfg["options"]["min_probe_rate"]), True


def run_group(profile, buckets, cfg, cycle_start, attempt):
    """Runs every bucket in `buckets` once, concurrently or one after another.

    Returns {bucket: result}. All processes have exited by the time this
    returns, which is what makes it safe for a retry group to reuse the same
    -E instance ids.
    """
    name = profile["name"]
    scan_rates, parallel = resolve_probe_rates(profile, buckets, cfg)

    if parallel:
        split = ", ".join(
            f"{naming.bucket_tag(*b)}={r}" for b, r in scan_rates.items()
        )
        logging.info(
            "[%s attempt %d] parallel: %dpps split as %s",
            name, attempt, config_mod.profile_probe_rate(profile, cfg), split,
        )
    else:
        logging.info(
            "[%s attempt %d] sequential: %d bucket(s) at %dpps each",
            name, attempt, len(buckets), config_mod.profile_probe_rate(profile, cfg),
        )

    # Instances are numbered per group and reused across groups, which is safe
    # because every process exits before the next group starts.
    jobs = [
        (
            profile, bucket, ip_list, scan_rates[bucket],
            index if parallel else None, cfg, cycle_start, attempt,
        )
        for index, (bucket, ip_list) in enumerate(buckets.items())
    ]
    results = {}

    if parallel:
        threads = [
            threading.Thread(
                target=_worker, args=(results, job[1], job), name=naming.bucket_tag(*job[1])
            )
            for job in jobs
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    else:
        for job in jobs:
            if proc.shutdown_requested():
                logging.warning("[%s] Shutting down - skipping remaining buckets", name)
                break
            _worker(results, job[1], job)

    for bucket, result in results.items():
        result["parallel"] = parallel
    return results


def run_profile(profile, buckets, cfg, cycle_start, deadline):
    """Runs one profile for this cycle, retrying failed buckets.

    Retries only re-run the buckets that failed, and only while one can be
    expected to finish before `deadline` - the moment the next measurement
    cycle is due. A retried bucket that is alone in its group gets the whole
    profile probe rate, since nothing else is running alongside it.
    """
    name = profile["name"]
    max_attempts = cfg["options"]["max_attempts"]
    margin = cfg["options"]["retry_margin"]
    max_ttl = config_mod.profile_max_ttl(profile, cfg)

    final = {}
    history = {bucket: [] for bucket in buckets}
    pending = buckets

    for attempt in range(1, max_attempts + 1):
        results = run_group(profile, pending, cfg, cycle_start, attempt)
        final.update(results)

        failed = [bucket for bucket in pending if scan_failed(final.get(bucket, {}))]
        for bucket in failed:
            history[bucket].append(
                {"attempt": attempt, "reason": failure_reason(final[bucket])}
            )
        if not failed:
            break

        tags = ", ".join(naming.bucket_tag(*b) for b in failed)
        if attempt >= max_attempts:
            logging.error(
                "[%s] Giving up on %s after %d attempt(s)", name, tags, attempt
            )
            break
        if proc.shutdown_requested():
            logging.warning("[%s] Shutting down - not retrying %s", name, tags)
            break

        pending = OrderedDict((bucket, buckets[bucket]) for bucket in failed)
        retry_rates, _ = resolve_probe_rates(profile, pending, cfg)
        needed = max(
            estimate_seconds(len(ips), max_ttl, retry_rates[bucket])
            for bucket, ips in pending.items()
        )
        finishes_at = datetime.datetime.now() + datetime.timedelta(seconds=needed + margin)
        if finishes_at > deadline:
            logging.error(
                "[%s] Not retrying %s - an attempt needs about %.0fs and would run "
                "past the next cycle at %s",
                name, tags, needed, deadline.strftime("%H:%M:%S"),
            )
            break

        logging.info(
            "[%s] Retrying %s (attempt %d of %d, about %.0fs needed)",
            name, tags, attempt + 1, max_attempts, needed,
        )

    entries = []
    for bucket in buckets:
        result = final.get(bucket)
        if not result:
            continue
        result["attempts"] = result.get("attempt", 0)
        result["failed_attempts"] = history[bucket]
        result["succeeded"] = not scan_failed(result)
        manifest.append(cfg["paths"]["manifest_path"], result)
        entries.append(result)
    return entries
