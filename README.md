# IODA Upstream Delay - vantage point application

The measurement agent that runs on an IODA Upstream Delay vantage point. On a
clock-aligned interval it probes a target list with
[yarrp](https://github.com/cmand/yarrp) and uploads the raw `.yrp` output to
the IODA collection server, which does all parsing, annotation and analysis.
Nothing is processed on the vantage point.

This repository is the application itself. Vantage points are deployed via
[ioda-upstream-delay-container](https://github.com/InetIntel/ioda-upstream-delay-container),
which builds it into a Docker image - follow that repository's instructions to
set one up.

## How it works

Every `interval` seconds, aligned to the machine's clock (so a 1800s interval
starts a cycle on every half hour, and every vantage point in the fleet
measures over the same windows):

1. Upload anything waiting in the outbox.
2. For each profile, in order: read its target list, bucket the targets by
   probe type, and run yarrp once per bucket.
3. Upload again.
4. Sleep until the next boundary.

A cycle that overruns its interval lands on the following boundary rather than
drifting.

### Profiles and buckets

A profile is one target list, scanned once per cycle. Profiles run one after
another.

yarrp takes a single probe type and port per invocation, so a profile's
targets are bucketed by `(protocol, port)` and one yarrp runs per bucket. With
`parallel: true` those buckets run at the same time, sharing the profile's
`probe_rate` in proportion to their target counts; otherwise they run
sequentially, each at the full rate.

Running them together collapses yarrp's fixed 60-second straggler wait from
once per bucket into once for the whole group, and means every protocol
observes the same network conditions instead of being staggered across the
cycle. Each concurrent bucket gets its own `-E` instance id, which is what
keeps their listeners from recording each other's replies - every yarrp opens
a raw ICMP socket, and the kernel hands each one a copy of every inbound ICMP
packet on the host.

Note that yarrp busy-spins to hold its probe rate, so N concurrent buckets
want roughly N cores. The application warns at startup if the host is short,
and `achieved_pps` in the manifest is how you check whether it mattered.

### Retries

A bucket whose yarrp exits non-zero, writes no output, or sends no probes is
retried, up to `options.max_attempts`. Only the failed buckets are re-run, and
only while an attempt can be expected to finish before the next cycle is due -
that estimate is `targets x max_ttl / probe_rate`, plus yarrp's straggler wait
and `options.retry_margin`. A bucket retried on its own gets the profile's
full probe rate, since nothing is running alongside it.

Every attempt's yarrp log is kept, so there is always something to read when a
scan dies.

## Target lists

A target file gives one address per line, optionally with the probe type to
use for it:

```
1.2.3.4                bare address - defaults to an ICMP probe
1.2.3.4,ICMP           ICMP/ICMP_REPLY take no port
1.2.3.4,UDP,53         TCP_ACK/TCP_SYN/UDP require one
1.2.3.4,TCP_SYN,443
```

`protocol` must be one of yarrp's own `-t` values, case-insensitive: `ICMP`,
`ICMP_REPLY`, `TCP_ACK`, `TCP_SYN`, `UDP`. Blank lines and lines starting with
`#` are ignored.

A profile's `targets_file` may be a single file or a directory, in which case
every top-level file in it is read and merged. Target lists are re-read at the
start of each cycle, so editing one takes effect on the next cycle without a
restart.

A malformed row - bad address, unknown protocol, or a TCP/UDP row with no port
- fails that profile for the cycle, with every bad line listed. Other profiles
still run.

## Configuration

[`config/config.yaml`](config/config.yaml) is the single source of truth: what
it says is what runs. There are no defaults layered underneath it and no
environment variables on top, so the file you are reading is the
configuration.

It ships with the application, so a vantage point runs on it out of the box.
To change a setting, edit that file, mount your own over
`/ioda-upstream-delay-application/config/config.yaml`, or point `$CONFIG_FILE`
at a different one. Substitution happens at the filesystem level, not by
merging.

[`config/config.example.yaml`](config/config.example.yaml) documents every
option with its meaning and defaults, including ones the shipped config
doesn't use - per-profile `probe_rate` and `max_ttl` overrides, mixed-protocol
target files, parallel bucketing. It is reference only and is never read.

Every key is required except `vp.hostname` (null derives it from the machine)
and the three under `options`, whose defaults are documented in both files.

Three settings can be overridden per vantage point, set in its
`docker-compose.yaml`. These win over the file, and are how a host is tuned
without editing it:

| Variable | Overrides |
|---|---|
| `PROBE_RATE` | `prober.probe_rate` |
| `INTERVAL` | `prober.interval` |
| `BW_LIMIT` | `reporting.bw_limit` |

Anything invalid - a missing section, a bad probe rate, a duplicate or
unusable profile name, a `max_ttl` out of range - is reported at startup,
before the first scan. Unrecognised keys are logged as warnings rather than
ignored silently, so a misindented setting doesn't leave a run quietly doing
the wrong thing.

## Output

```
/data/
├── staging/         yarrp writes here; scratch
├── outbox/          completed scans, awaiting upload
├── scan-logs/       one log per scan attempt, always kept
├── manifest.jsonl   local record of every scan
└── ioda-ud.log      application log
```

A `.yrp` moves to the outbox only once its scan has exited cleanly, so a
truncated result from a killed container is never uploaded. `rsync` removes
each file as it sends it; anything left after an unreachable server is retried
next cycle, so results accumulate locally rather than being lost.

### Uploaded filenames

```
<vp>_<YYYYMMDD>_<HHMMSS>_<profile>-<bucket>.yrp

traversa_20260907_163000_default-icmp.yrp
traversa_20260907_163000_multiproto-tcp-ack-p443.yrp
```

The timestamp is the cycle's start, so every file from one cycle shares it.

The server splits this name on underscores and truncates it at the first dot,
so it must be exactly four underscore-separated tokens and the trailing label
must contain neither underscores nor dots. That is why hostnames are
domain-stripped and underscores replaced, why protocol names like `TCP_ACK`
become `tcp-ack`, and why profile names are restricted to letters, digits and
hyphens. All of it is enforced in `iupd/naming.py` and at config load.

Only `.yrp` files are uploaded. The collection server processes everything in
its incoming directory without checking extensions, so the manifest and logs
deliberately stay on the vantage point.

### Manifest

One JSON object per bucket per cycle, recording what the scan was asked to do
and what it actually did: allocated and configured probe rates, `probes_sent`
and `achieved_pps` (read from the stats yarrp writes into the tail of its own
output, so it stays correct when buckets run concurrently), target count and
source file, `-E` instance, attempts made and why any failed, and the uploaded
filename.

## Layout

```
main.py              entry point
iupd/
├── config.py        defaults, file, environment; validation
├── targets.py       target file parsing and bucketing
├── naming.py        filename construction and its invariants
├── rates.py         splitting a probe rate across buckets
├── prober.py        running yarrp; groups, retries
├── reporter.py      uploading the outbox
├── scheduler.py     the clock-aligned measurement loop
├── manifest.py      the local scan record
└── proc.py          subprocess supervision and shutdown
source_data/         default target list
yrp2text/            raw .yrp to text converter (used server-side)
```

`yrp2text` is kept here as the canonical copy of the converter; the vantage
point itself does not run it.
