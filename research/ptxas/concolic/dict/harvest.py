#!/usr/bin/env python3
"""Build an AFL dictionary from strings ptxas compares while compiling the seeds."""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parents[3])]
from forms import TARGETS

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ptxas', default='/usr/local/cuda-13.0/bin/ptxas')
    p.add_argument('--output', type=Path, default=HERE / 'ptx-strcmp.dict')
    p.add_argument('seeds', type=Path, nargs='*')
    args = p.parse_args()
    seeds = args.seeds or [HERE.parent / 'generic_sm75.ptx', *sorted(HERE.parents[1].glob('*.ptx'))]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp); shim = tmp / 'strcmp_log.so'; log = tmp / 'log'
        subprocess.check_call(['cc', '-shared', '-fPIC', '-O1', '-o', shim, HERE / 'strcmp_log.c', '-ldl'])
        for seed in seeds:
            for target in TARGETS.values():
                subprocess.run([args.ptxas, f'-arch={target}', '-o', '/dev/null', seed], capture_output=True,
                               env=dict(LD_PRELOAD=str(shim), STRCMP_LOG=str(log)))
        words = set(log.read_bytes().split(b'\n'))
    # Paths, numeric hashes and internal library routines (__cuda_*, labels) are not PTX vocabulary.
    tokens = sorted(w for w in words if 0 < len(w) <= 64 and w.isascii() and w.decode().isprintable()
                    and b'/' not in w and not w.startswith(b'__') and not w.isdigit())
    args.output.write_text(''.join('"%s"\n' % w.decode().replace('\\', '\\\\').replace('"', '\\"')
                                   for w in tokens))
    print(f'{len(tokens)} tokens -> {args.output}')

if __name__ == '__main__': main()
