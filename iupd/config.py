import os
import socket

import yaml

from . import naming


class ConfigError(ValueError):
    pass


APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(APP_ROOT, "config", "config.yaml")

REQUIRED_PATHS = (
    "targets_dir", "staging_dir", "outbox_dir", "scan_log_dir",
    "manifest_path", "log_path", "yarrp_bin",
)
REQUIRED_PROBER = ("probe_rate", "interval", "max_ttl")
REQUIRED_REPORTING = ("destination", "port", "bw_limit", "ssh_identity_file")

# The only keys that may be omitted, with the value used when they are.
OPTIONAL_OPTIONS = {"max_attempts": 2, "min_probe_rate": 50, "retry_margin": 60}

# Settings a vantage point may override in its docker-compose.yaml, as
# {env var: (config section, key, type)}. These win over config.yaml.
ENV_OVERRIDES = {
    "PROBE_RATE": ("prober", "probe_rate", int),
    "INTERVAL": ("prober", "interval", int),
    "BW_LIMIT": ("reporting", "bw_limit", str),
}

KNOWN_TOP_LEVEL = {"paths", "vp", "prober", "reporting", "options", "scans"}
KNOWN_PROFILE_KEYS = {"name", "targets_file", "probe_rate", "max_ttl", "parallel"}


def collect_unknown_keys(config):
    # Log warnings for keys we don't recognize
    warnings = []
    unknown = sorted(set(config) - KNOWN_TOP_LEVEL)
    if unknown:
        warnings.append(f"ignoring unknown top-level config key(s): {unknown}")
    known = {
        "paths": set(REQUIRED_PATHS),
        "vp": {"hostname"},
        "prober": set(REQUIRED_PROBER),
        "reporting": set(REQUIRED_REPORTING),
        "options": set(OPTIONAL_OPTIONS),
    }
    for section, keys in known.items():
        unknown = sorted(set(config.get(section) or {}) - keys)
        if unknown:
            warnings.append(f"ignoring unknown key(s) in {section}: {unknown}")
    for profile in config.get("scans") or []:
        if isinstance(profile, dict):
            unknown = sorted(set(profile) - KNOWN_PROFILE_KEYS)
            if unknown:
                warnings.append(f"[{profile.get('name', '?')}] ignoring unknown key(s): {unknown}")
    return warnings


def apply_env_overrides(config):
    for name, (section, field, cast) in ENV_OVERRIDES.items():
        raw = os.environ.get(name)
        if not raw:
            continue
        try:
            value = cast(raw)
        except ValueError:
            raise ConfigError(f"{name} must be an integer, got {raw!r}")
        config.setdefault(section, {})[field] = value
    return config


def require_section(config, name, keys):
    section = config.get(name)
    if not isinstance(section, dict):
        raise ConfigError(f"config is missing the {name!r} section")
    missing = sorted(k for k in keys if section.get(k) is None)
    if missing:
        raise ConfigError(f"{name} is missing required key(s): {missing}")
    return section


def resolve_hostname(config):
    vp = config.setdefault("vp", {}) or {}
    config["vp"] = vp
    raw = vp.get("hostname") or socket.gethostname()
    hostname = naming.sanitize_hostname(raw)
    if not hostname:
        raise ConfigError(
            f"could not derive a usable vantage point name from {raw!r} - "
            f"set vp.hostname in the config"
        )
    vp["hostname"] = hostname
    return hostname


def validate(config):
    # Raises ConfigError on anything that would make a run meaningless
    paths = require_section(config, "paths", REQUIRED_PATHS)
    for key in REQUIRED_PATHS:
        if not isinstance(paths[key], str):
            raise ConfigError(f"paths.{key} must be a string, got {paths[key]!r}")

    prober = require_section(config, "prober", REQUIRED_PROBER)
    for key in REQUIRED_PROBER:
        if not isinstance(prober[key], int) or prober[key] <= 0:
            raise ConfigError(f"prober.{key} must be a positive integer, got {prober[key]!r}")
    if prober["max_ttl"] > 255:
        raise ConfigError(f"prober.max_ttl must be at most 255, got {prober['max_ttl']}")

    require_section(config, "reporting", REQUIRED_REPORTING)

    options = config.setdefault("options", {}) or {}
    config["options"] = options
    for key, fallback in OPTIONAL_OPTIONS.items():
        options.setdefault(key, fallback)
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

        rate = profile_probe_rate(profile, config)
        if not isinstance(rate, int) or rate <= 0:
            raise ConfigError(f"[{name}] probe_rate must be a positive integer, got {rate!r}")

        max_ttl = profile_max_ttl(profile, config)
        if not isinstance(max_ttl, int) or not (0 < max_ttl <= 255):
            raise ConfigError(f"[{name}] max_ttl must be between 1 and 255, got {max_ttl!r}")

    return config


def load(path=None):
    path = path or os.environ.get("CONFIG_FILE") or DEFAULT_CONFIG_PATH

    try:
        with open(path, "r") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError(
            f"no config file at {path}. The application ships one at "
            f"{DEFAULT_CONFIG_PATH}; mount your own over it or point "
            f"$CONFIG_FILE at it."
        )
    except yaml.YAMLError as e:
        raise ConfigError(f"could not parse {path}: {e}")

    if not isinstance(config, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    warnings = collect_unknown_keys(config)
    apply_env_overrides(config)
    resolve_hostname(config)
    validate(config)
    config["config_path"] = path
    return config, warnings


def profile_probe_rate(profile, config):
    return profile.get("probe_rate", config["prober"]["probe_rate"])


def profile_max_ttl(profile, config):
    return profile.get("max_ttl", config["prober"]["max_ttl"])
