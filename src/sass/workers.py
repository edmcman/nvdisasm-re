"""How many parallel workers fit on this machine."""


def fitting(gib, *limits):
    """Workers of `gib` GiB each that fit in the memory available now, within `limits`."""
    with open('/proc/meminfo') as f: available = next(int(l.split()[1]) for l in f if l.startswith('MemAvailable:')) >> 20
    return max(1, min(*limits, available // gib))
