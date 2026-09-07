"""Filename construction for uploaded scan results.

Uploaded files are named:

    <vp>_<YYYYMMDD>_<HHMMSS>_<profile>-<bucket>.yrp

The server splits that on underscores and truncates at the first dot, so the
name must be exactly four underscore-separated tokens and the trailing label
must contain neither underscores nor dots. Legacy three-token names (without
the label) predate multi-protocol scanning and are still accepted upstream.

Everything that could violate those rules - a hostname carrying a domain or
an underscore, a protocol like TCP_ACK whose own name contains one - is
normalised here, so the invariant is enforced in one place.
"""

import re

_INVALID_CHARS = re.compile(r"[^A-Za-z0-9-]+")
_LABEL_PART = re.compile(r"^[A-Za-z0-9-]+$")


def is_valid_label_part(value):
    """True if `value` can appear in a filename label as-is."""
    return bool(_LABEL_PART.match(value or ""))


def sanitize_hostname(raw):
    """Reduces a hostname to something safe for the first filename token.

    socket.gethostname() may return a fully qualified name, and the server
    truncates the filename at its first dot - 'vp1.gatech.edu_2026...' would
    parse as the single token 'vp1', which raises rather than skipping. The
    domain is dropped and underscores become hyphens so the token count stays
    predictable.
    """
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
