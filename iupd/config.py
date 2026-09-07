"""Configuration loading and validation.

Settings come from three places, each overlaying the one before it:

    1. DEFAULTS below
    2. /config/config.yaml (or $CONFIG_FILE), if it exists
    3. environment variables (PROBE_RATE, BW_LIMIT, INTERVAL)

The environment layer exists because deployed vantage points set these in
their docker-compose.yaml, and those files must keep working untouched.
"""

import copy
import os
import socket

import yaml

from . import naming


class ConfigError(ValueError):
    pass


DEFAULT_CONFIG_PATH = "/config/config.yaml"

DEFAULTS = {
    "paths": {
        "targets_dir": "/ioda-upstream-delay-application/source_data",
        "staging_dir": "/data/staging",
        "outbox_dir": "/data/outbox",
        "scan_log_dir": "/data/scan-logs",
        "manifest_path": "/data/manifest.jsonl",
        "log_path": "/data/ioda-ud.log",
        "yarrp_bin": "/yarrp/yarrp",
    },
    "vp": {
        # TODO: Change back to - Left unset, the hostname is taken from the machine itself.
        "hostname": "simonTestHost",
    },
    "prober": {
        "probe_rate": 30000,
        "interval": 1800,
        "max_ttl": 32,
    },
    "reporting": {
        "destination": "ioda-ud@traversa.cc.gatech.edu:/traversa-pool/upstream-delay/incoming",
        "port": 3412,
        "bw_limit": "100m",
        "ssh_identity_file": "/data/ssh_id",
    },
    "options": {
        "max_attempts": 2,
        "min_probe_rate": 50,
        "retry_margin": 60,
    },
    "scans": [
        {
            "name": "default",
            "targets_file": "targets",
            "parallel": True,
        }
    ],
}

KNOWN_TOP_LEVEL = set(DEFAULTS)
KNOWN_PROFILE_KEYS = {"name", "targets_file", "probe_rate", "max_ttl", "parallel"}


def deep_merge(base, override):
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def collect_unknown_keys(config):
    warnings = []
    unknown = sorted(set(config) - KNOWN_TOP_LEVEL)
    if unknown:
        warnings.append(f"ignoring unknown top-level config key(s): {unknown}")
    for section in ("paths", "vp", "prober", "reporting", "options"):
        unknown = sorted(set(config.get(section) or {}) - set(DEFAULTS[section]))
        if unknown:
            warnings.append(f"ignoring unknown key(s) in {section}: {unknown}")
    for profile in config.get("scans") or []:
        if not isinstance(profile, dict):
            continue
        unknown = sorted(set(profile) - KNOWN_PROFILE_KEYS)
        if unknown:
            warnings.append(f"[{profile.get('name', '?')}] ignoring unknown key(s): {unknown}")
    return warnings


def apply_env_overrides(config):
    """Applies the environment variables deployed vantage points already set."""
    probe_rate = os.environ.get("PROBE_RATE")
    if probe_rate:
        try:
            config["prober"]["probe_rate"] = int(probe_rate)
        except ValueError:
            raise ConfigError(f"PROBE_RATE must be an integer, got {probe_rate!r}")

    interval = os.environ.get("INTERVAL")
    if interval:
        try:
            config["prober"]["interval"] = int(interval)
        except ValueError:
            raise ConfigError(f"INTERVAL must be an integer, got {interval!r}")

    bw_limit = os.environ.get("BW_LIMIT")
    if bw_limit:
        config["reporting"]["bw_limit"] = bw_limit

    return config


def resolve_hostname(config):
    configured = config["vp"].get("hostname")
    raw = configured or socket.gethostname()
    hostname = naming.sanitize_hostname(raw)
    if not hostname:
        raise ConfigError(
            f"could not derive a usable vantage point name from {raw!r} - "
            f"set vp.hostname in the config"
        )
    config["vp"]["hostname"] = hostname
    return hostname


def validate(config):
    """Raises ConfigError on anything that would make a run meaningless.

    Everything here is checked before the first scan starts, so a bad config
    fails at startup rather than half an hour in.
    """
    prober = config["prober"]
    for key in ("probe_rate", "interval", "max_ttl"):
        value = prober.get(key)
        if not isinstance(value, int) or value <= 0:
            raise ConfigError(f"prober.{key} must be a positive integer, got {value!r}")
    if prober["max_ttl"] > 255:
        raise ConfigError(f"prober.max_ttl must be at most 255, got {prober['max_ttl']}")

    options = config["options"]
    if not isinstance(options["max_attempts"], int) or options["max_attempts"] < 1:
        raise ConfigError(
            f"options.max_attempts must be at least 1 (1 means no retry), "
            f"got {options['max_attempts']!r}"
        )
    for key in ("min_probe_rate", "retry_margin"):
        if not isinstance(options[key], int) or options[key] < 0:
            raise ConfigError(f"options.{key} must be a non-negative integer, got {options[key]!r}")

    scans = config.get("scans")
    if not scans:
        raise ConfigError("config has no scans - nothing to do")

    seen = set()
    for profile in scans:
        if not isinstance(profile, dict) or not profile.get("name"):
            raise ConfigError(f"every entry under scans needs a name: {profile!r}")
        name = profile["name"]
        # The profile name becomes part of the uploaded filename, which the
        # server splits on underscores and truncates at the first dot.
        if not naming.is_valid_label_part(name):
            raise ConfigError(
                f"profile name {name!r} must contain only letters, digits and "
                f"hyphens - it becomes part of the uploaded filename"
            )
        if name in seen:
            raise ConfigError(f"duplicate profile name {name!r}")
        seen.add(name)

        if not profile.get("targets_file"):
            raise ConfigError(f"[{name}] targets_file is required")

        rate = profile.get("probe_rate", prober["probe_rate"])
        if not isinstance(rate, int) or rate <= 0:
            raise ConfigError(f"[{name}] probe_rate must be a positive integer, got {rate!r}")

        max_ttl = profile.get("max_ttl", prober["max_ttl"])
        if not isinstance(max_ttl, int) or not (0 < max_ttl <= 255):
            raise ConfigError(f"[{name}] max_ttl must be between 1 and 255, got {max_ttl!r}")

    return config


def load(path=None):
    """Loads, merges, validates. Returns (config, warnings)."""
    path = path or os.environ.get("CONFIG_FILE") or DEFAULT_CONFIG_PATH

    user_config = {}
    warnings = []
    try:
        with open(path, "r") as f:
            user_config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        warnings.append(f"no config file at {path} - using built-in defaults")
    except yaml.YAMLError as e:
        raise ConfigError(f"could not parse {path}: {e}")

    if not isinstance(user_config, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    warnings.extend(collect_unknown_keys(user_config))

    config = deep_merge(DEFAULTS, user_config)
    apply_env_overrides(config)
    resolve_hostname(config)
    validate(config)
    config["config_path"] = path
    return config, warnings


def profile_probe_rate(profile, config):
    return profile.get("probe_rate", config["prober"]["probe_rate"])


def profile_max_ttl(profile, config):
    return profile.get("max_ttl", config["prober"]["max_ttl"])
