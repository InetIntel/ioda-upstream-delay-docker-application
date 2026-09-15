"""Splitting a profile's probe rate across its buckets."""

from collections import OrderedDict


class RateAllocationError(ValueError):
    pass


def allocate(weights, total_rate, min_rate):
    keys = [key for key, _ in weights]
    if len(keys) == 1:
        return OrderedDict([(keys[0], total_rate)])

    if min_rate * len(keys) > total_rate:
        raise RateAllocationError(
            f"probe_rate {total_rate} cannot be split across {len(keys)} buckets "
            f"at a {min_rate}pps floor (needs at least {min_rate * len(keys)}). "
            f"Raise probe_rate, lower options.min_probe_rate, or drop parallel."
        )

    pinned = {}
    pool = list(weights)
    budget = total_rate
    while pool:
        pool_weight = sum(weight for _, weight in pool)
        under = [key for key, weight in pool if budget * weight / pool_weight < min_rate]
        if not under:
            break
        for key in under:
            pinned[key] = min_rate
            budget -= min_rate
        pool = [(key, weight) for key, weight in pool if key not in pinned]

    alloc = dict(pinned)
    if pool:
        # Largest remainder, so the shares add back up to exactly total_rate
        # instead of drifting low from rounding each one down.
        pool_weight = sum(weight for _, weight in pool)
        exact = {key: budget * weight / pool_weight for key, weight in pool}
        alloc.update({key: int(value) for key, value in exact.items()})
        leftover = budget - sum(alloc[key] for key, _ in pool)
        by_remainder = sorted(pool, key=lambda kw: (-(exact[kw[0]] - alloc[kw[0]]), -kw[1]))
        for key, _ in by_remainder[:leftover]:
            alloc[key] += 1

    return OrderedDict((key, alloc[key]) for key in keys)
