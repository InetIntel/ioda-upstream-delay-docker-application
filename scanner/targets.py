"""Target list parsing.

A target file gives one address per line, optionally with the probe type to
use for it:

    1.2.3.4                  bare address - defaults to ICMP
    1.2.3.4,ICMP             ICMP/ICMP_REPLY take no port
    1.2.3.4,UDP,53           TCP_ACK/TCP_SYN/UDP require one
    1.2.3.4,TCP_SYN,443

yarrp accepts a single probe type and port per invocation, so targets are
bucketed by (protocol, port) and one yarrp is run per bucket.
"""

import ipaddress
import os
from collections import OrderedDict

VALID_PROTOCOLS = {"ICMP", "ICMP_REPLY", "TCP_ACK", "TCP_SYN", "UDP"}
PORTLESS_PROTOCOLS = {"ICMP", "ICMP_REPLY"}

DEFAULT_PROTOCOL = "ICMP"


class TargetFileError(ValueError):
    pass


def resolve_target_files(path):
    """A targets path may be one file, or a directory of them (top-level files only)"""
    if os.path.isdir(path):
        files = sorted(
            os.path.join(path, f)
            for f in os.listdir(path)
            if os.path.isfile(os.path.join(path, f)) and not f.startswith(".")
        )
        if not files:
            raise TargetFileError(f"targets directory has no files: {path}")
        return files
    if os.path.isfile(path):
        return [path]
    raise TargetFileError(f"targets path does not exist: {path}")


def parse_target_line(line):
    """Returns (ip, protocol, port), with port None where the protocol doesn't take one"""
    fields = [f.strip() for f in line.split(",")]
    if len(fields) > 3:
        raise TargetFileError(
            f"expected at most 3 comma-separated fields, got {len(fields)}: {line!r}"
        )

    addr = fields[0]
    try:
        ipaddress.ip_address(addr)
    except ValueError:
        raise TargetFileError(f"not a valid IP address: {addr!r}")

    if len(fields) == 1:
        return addr, DEFAULT_PROTOCOL, None

    protocol = fields[1].upper()
    if protocol not in VALID_PROTOCOLS:
        raise TargetFileError(
            f"unknown protocol {fields[1]!r} for {addr} "
            f"(expected one of {sorted(VALID_PROTOCOLS)})"
        )

    port_field = fields[2] if len(fields) == 3 else ""
    if protocol in PORTLESS_PROTOCOLS:
        return addr, protocol, None

    if not port_field:
        raise TargetFileError(
            f"{protocol} target {addr} requires a port (addr,{protocol.lower()},port)"
        )
    try:
        port = int(port_field)
    except ValueError:
        raise TargetFileError(f"invalid port {port_field!r} for {addr}")
    if not (0 < port < 65536):
        raise TargetFileError(f"port out of range for {addr}: {port}")

    return addr, protocol, port


def bucket_targets(path):
    """Reads a target file (or directory of them) and buckets every address
    by (protocol, port).
    """
    buckets = OrderedDict()
    errors = []

    for file_path in resolve_target_files(path):
        with open(file_path, "r") as f:
            for lineno, raw_line in enumerate(f, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    addr, protocol, port = parse_target_line(line)
                except TargetFileError as e:
                    errors.append(f"{file_path}:{lineno}: {e}")
                    continue
                buckets.setdefault((protocol, port), []).append(addr)

    if errors:
        shown = errors[:20]
        suffix = "" if len(errors) == len(shown) else f"\n... and {len(errors) - len(shown)} more"
        raise TargetFileError("invalid target file(s):\n" + "\n".join(shown) + suffix)

    # Sorted so a bucket keeps the same -E instance id from cycle to cycle.
    return OrderedDict(sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)))


def resolve_targets_path(profile, paths):
    targets_path = profile["targets_file"]
    if not os.path.isabs(targets_path):
        targets_path = os.path.join(paths["targets_dir"], targets_path)
    return targets_path
