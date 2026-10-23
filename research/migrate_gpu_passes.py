"""One-off (#63): infer GPU observations from the pass fingerprints in gpu_case_passes.json.

Ran at nan-ops 01129e0 (#61), whose comparison is bitwise except for FP8 NaN bytes, and folded in
the observations inferred the same way at main 60cfa3a (where F16 NaN lanes were tolerated too).
A scenario whose fingerprint passed was judged by exactly that comparison against exactly that
expectation, so each exactly compared capture held its expected value; a capture the comparison
tolerates (an FP8 NaN byte) or skips stays unknown. Writes tests/baseline/gpu_observations in
tests.harness.device_evidence's format and the fingerprints' verified words, to check against."""
import argparse
import hashlib
import json
from collections import Counter, defaultdict
from functools import cache
from pathlib import Path
from tests.cases import FAMILIES, stream
from tests.harness import device_cases, device_evidence, sharded
from tests.refs import fpformat

OBSERVATIONS = device_evidence.ROOT / 'tests/baseline/gpu_observations'


def probed(s):
    p = device_cases.scalar_probe([s], 'probe')
    data = (s.arch, s.kind, [f'{w:032x}' for w in p.code.words], p.constants, p.observations, p.arena.hex(),
            p.shared, p.frame_size, p.threads, p.grid)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:24], p.observations[0][1]


def load(file):
    head, *lines = file.read_text().splitlines()
    devices = json.loads(head)['devices']
    return {(key, devices[d]): (provenance, values) for key, d, provenance, values in map(json.loads, lines)}


def store(file, entries):
    devices = sorted({d for _, d in entries})
    lines = sorted(json.dumps([key, devices.index(d), provenance, values], separators=(',', ':')) for (key, d), (provenance, values) in entries.items())
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text('\n'.join([json.dumps(dict(version=1, devices=devices)), *lines]) + '\n')


@cache
def recorded(arch):
    """{fingerprint: [devices]}"""
    out = defaultdict(list)
    for device, fs in device_evidence.passes()['architectures'].get(arch, {}).items():
        for f in fs: out[f].append(device)
    return out


def inferred(s, names):
    expected = (s.case.hardware or {}).get('expected', s.case.expected)
    def value(n):
        if n.startswith('__memory:'):
            _, index, offset, size = n.split(':')
            return None if int(index) else int.from_bytes(bytes.fromhex(expected['__memory'])[int(offset):int(offset) + int(size)], 'little')
        want = expected[n]
        if '__fp8_nan' in expected and any(fpformat.decode(expected['__fp8_nan'], want >> b & 0xff)[0] == 'nan' for b in (0, 8)): return None
        return want
    return [None if v is None else f'{v:08x}' for v in map(value, names)]


def agree(a, b): return [x if x == y else None for x, y in zip(a, b)]


def migrated(job):
    """Inferred entries, matched fingerprints, per-family unknown counts and the fingerprints' verified words of one shard."""
    arch, family = job[0], job[2]
    entries, matched, unknown, words = {}, set(), Counter(), defaultdict(set)
    for c in stream(*job):
        s = device_cases.Scenario(family, c, *device_cases.classify(c, arch=arch), arch=arch)
        if s.kind == 'emulator': continue
        f = device_evidence.fingerprint(s)
        devices = recorded(arch).get(f)
        words[c.request['word']].add(bool(devices))
        if not devices: continue
        key, names = probed(s)
        values = inferred(s, names)
        matched.add(f); unknown[family, None in values] += 1
        for d in devices: entries[key, d] = agree(values, entries.get((key, d), values))
    return entries, matched, unknown, {(family, w) for w, ok in words.items() if ok == {True}}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('architectures', nargs='*', default=['SM89', 'SM90'])
    p.add_argument('--earlier', type=Path, required=True, help="the main-era inferred observations' directory")
    p.add_argument('--verified', type=Path, required=True, help="write the fingerprints' verified words here")
    a = p.parse_args()
    verified = {}
    for arch in a.architectures:
        entries, matched, unknown, words = {}, set(), Counter(), set()
        for e, m, u, ws in sharded.run(migrated, ((arch, 'exact'),), FAMILIES, 0):
            for k, vs in e.items(): entries[k] = agree(vs, entries.get(k, vs))
            matched |= m; unknown += u; words |= ws
        print(f'{arch}: {len(matched):,} of {len(device_evidence.passed(arch)):,} fingerprints matched, {len(entries):,} observations')
        for (family, partial), n in sorted(unknown.items()): print(f'  {family}: {n:,} scenarios {"with unknown captures" if partial else "fully inferred"}')
        out = {k: ('inferred', vs) for k, vs in entries.items()}
        earlier, conflicts, added, filled = load(a.earlier / f'{arch}.jsonl'), 0, 0, 0
        for k, (_, old) in earlier.items():
            if k not in out: out[k] = ('inferred', old); added += 1; continue
            new = out[k][1]
            if any(x is not None and y is not None and x != y for x, y in zip(old, new)): conflicts += 1; out[k] = ('inferred', agree(old, new)); continue
            merged = [x if y is None else y for x, y in zip(old, new)]
            filled += merged != new; out[k] = ('inferred', merged)
        print(f'  earlier: {len(earlier):,} observations, {added:,} added, {filled:,} filled unknowns, {conflicts:,} conflicting')
        store(OBSERVATIONS / f'{arch}.jsonl', out)
        verified[arch] = sorted(words)
    a.verified.write_text(json.dumps(verified))


if __name__ == '__main__':
    main()
