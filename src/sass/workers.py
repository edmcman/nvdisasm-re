"""How many parallel workers fit on this machine."""
import re
import subprocess
import sys


def available_bytes():
    """Memory that can be given to new processes without swapping."""
    match sys.platform:
        case 'darwin':
            # Free, speculative (read-ahead) and inactive pages are reclaimable.
            stats = subprocess.run(['vm_stat'], capture_output=True, text=True, check=True).stdout
            pages = dict(re.findall(r'^Pages (free|inactive|speculative):\s+(\d+)', stats, re.M))
            return sum(map(int, pages.values())) * int(re.search(r'page size of (\d+)', stats)[1])
        case _:
            with open('/proc/meminfo') as f:
                return next(int(l.split()[1]) for l in f if l.startswith('MemAvailable:')) << 10


def fitting(gib, *limits):
    """Workers of `gib` GiB each that fit in the memory available now, within `limits`."""
    return max(1, min(*limits, (available_bytes() >> 30) // gib))
