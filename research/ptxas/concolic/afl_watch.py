#!/usr/bin/env python3
"""Print one line when a new opcode appears in an accepted queue entry or an afl-fuzz instance completes a queue cycle.

python3 afl_watch.py OUT [--interval 60]
"""
import argparse, json, subprocess, tempfile, time
from pathlib import Path
from corpus import heads, tokens

HERE = Path(__file__).resolve().parent
PTXAS = '/usr/local/cuda-13.0/bin/ptxas'

def compiles(prefix, region, suffix):
    with tempfile.NamedTemporaryFile(suffix='.ptx') as f:
        f.write(prefix + region + suffix); f.flush()
        return subprocess.run([PTXAS, '-arch=sm_100', '-o', '/dev/null', f.name], capture_output=True).returncode == 0

def cycles(out):
    result = {}
    for path in (out/'afl').glob('*/fuzzer_stats'):
        stats = {k.strip(): v.strip() for k, v in (l.split(':', 1) for l in path.read_text().splitlines() if ':' in l)}
        result[path.parent.name] = int(stats['cycles_done'])
    return result

def main():
    p = argparse.ArgumentParser(); p.add_argument('out', type=Path); p.add_argument('--interval', type=int, default=60)
    out = p.parse_args().out.resolve(); interval = p.parse_args().interval
    roots = set(json.loads((HERE/'opcode_registry.json').read_text())['roots'])
    prefix, suffix = (out/'scaffold.pre').read_bytes(), (out/'scaffold.post').read_bytes()
    known = {h.decode() for h in heads(tokens(prefix + (out/'seeds'/'seed.ptx').read_bytes() + suffix))} & roots
    seen, last_cycles = set(), cycles(out)
    first = True
    while True:
        for path in (out/'afl').glob('*/queue/id:*'):
            if path in seen: continue
            seen.add(path)
            region = path.read_bytes()
            new = ({h.decode(errors='replace') for h in heads(tokens(region))} & roots) - known
            if new and compiles(prefix, region, suffix):
                known |= new
                if not first: print(f'NEW OPCODE {sorted(new)} accepted by ptxas; found by {path.parts[-3]}: {region.decode(errors="replace").strip()[:120]!r}', flush=True)
        now = cycles(out)
        for name, count in sorted(now.items()):
            if count > last_cycles.get(name, 0) and not first:
                print(f'CYCLE COMPLETED: {name} finished queue cycle {count}', flush=True)
        last_cycles, first = now, False
        time.sleep(interval)

main()
