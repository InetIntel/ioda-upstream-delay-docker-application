
# Naming Convention: <vp>_<YYYYMMDD>_<HHMMSS>_<profile>-<bucket>.yrp

import re

_INVALID_CHARS = re.compile(r"[^A-Za-z0-9-]+")
_LABEL_PART = re.compile(r"^[A-Za-z0-9-]+$")


def is_valid_label_part(value):
    """True if `value` can appear in a filename label as-is."""
    return bool(_LABEL_PART.match(value or ""))


def sanitize_hostname(raw):
    host = (raw or "").strip().split(".")[0]
    return _INVALID_CHARS.sub("-", host).strip("-")


def bucket_tag(protocol, port):
    """A filename-safe tag for one (protocol, port) bucket.

    yarrp's own protocol names contain underscores (TCP_ACK), which would add
    a token to the filename, so they become hyphens: 'tcp-ack-p443', 'icmp'.
    """
    tag = _INVALID_CHARS.sub("-", protocol.lower())
    if port is not None:
        tag = f"{tag}-p{port}"
    return tag


def scan_label(profile_name, protocol, port):
    """The trailing filename token: profile and bucket, joined by a hyphen."""
    label = f"{profile_name}-{bucket_tag(protocol, port)}"
    if not is_valid_label_part(label):
        # Unreachable if the config validated, but the cost of being wrong
        # here is an unparseable file sitting on the server.
        raise ValueError(f"scan label {label!r} is not filename-safe")
    return label


def yrp_filename(hostname, dt, label):
    return f"{hostname}_{dt:%Y%m%d}_{dt:%H%M%S}_{label}.yrp"


def run_prefix(hostname, dt, label):
    """Basename shared by a scan's intermediates (log, manifest entry)."""
    return f"{hostname}_{dt:%Y%m%d}_{dt:%H%M%S}_{label}"
