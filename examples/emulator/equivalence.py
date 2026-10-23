"""Equivalence testing on the native emulator: run kernels on the same launches and compare them thread
by thread. `times3` (IMAD) and `added` (IADD3) compute 3 * in[i] + 1 alike; `buggy` drops the + 1
once in[i] >= 1000."""
import random, struct
from pathlib import Path
from sass.assemble import assemble_cubin
from sass.decode import cubin
from sass.emulate import equivalence

IN, OUT, N = 0x1_0000_0000, 0x2_0000_0000, 4096
data = assemble_cubin((Path(__file__).parent / "times3.sass").read_text())
times3, added, buggy = (cubin.kernel_image(data, name) for name in ("times3", "added", "buggy"))


def launch(seed):
    """A launch over N threads whose inputs are half small (below 1000), half random words."""
    rng = random.Random(seed)
    inputs = [rng.randrange(1000) if i % 2 else rng.getrandbits(32) for i in range(N)]
    return equivalence.Launch(arguments=(IN, OUT), memory=(dict(space="ram", address=IN, hex=struct.pack(f"<{N}I", *inputs).hex()),),
                              output=dict(address=OUT, size=4, stride=4), threads=N, block=128)


launches = [launch(seed) for seed in range(4)]
print("times3 == added:", equivalence.equivalent(times3, added, launches))
found = list(equivalence.differences(times3, buggy, launches))
first = found[0]
print(f"times3 vs buggy: {len(found)} differing threads; launch {first.launch} thread {first.thread}: {first.a} vs {first.b}")
