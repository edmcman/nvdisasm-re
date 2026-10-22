"""Hardware evidence for shared cases: a fingerprint per scenario of what ran on the GPU and how it was
judged, recorded when it passes; recorded passes never skip execution."""
import hashlib
import inspect
import json
from collections import Counter
from functools import cache
from sass.paths import ROOT
from tests.cases import FAMILIES, stream
from tests.harness import device_cases, sharded
from tests.refs import bits, fpformat

PASSES = ROOT / 'tests/baseline/gpu_case_passes.json'
VERSION = 4


def hardware_expected(case):
    expected = (case.hardware or {}).get('expected', case.expected)
    return {k: v for k, v in expected.items() if not k.startswith('__') or k in ('__memory', '__counter', '__fp8_nan')}


# The code that judges a run; the probe and the hardware expectation fix everything else.
COMPARISON = hashlib.sha256(''.join(inspect.getsource(f) for f in (
    device_cases.compare, device_cases.half_nan_equal, device_cases.nan_lanes, hardware_expected, fpformat.same_fp8, bits.nan16)).encode()).hexdigest()


def fingerprint(s):
    """What scenario s runs on the GPU and how its result is judged: its probe built alone (target word,
    inputs, setup, scheduling, capture layout, launch shape), its hardware expectation, NaN policy and
    the comparison code. Edits elsewhere, or other cases, leave it unchanged."""
    p = device_cases.scalar_probe([s], 'probe')
    data = (COMPARISON, s.arch, s.kind, [f'{w:032x}' for w in p.code.words], p.constants, p.observations, p.arena.hex(),
            p.shared, p.frame_size, p.threads, p.grid, hardware_expected(s.case), device_cases.nan_lanes(s.case, s.arch))
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:24]


def summary(scenarios, ids=True):
    """A family's selection, mergeable across shards: case and kind counts, emulator reasons, the
    fingerprints of each word's GPU scenarios and optionally the scenario ids."""
    out = dict(cases=0, kinds=Counter(), reasons=Counter(), fingerprints={}, ids=set())
    for s in scenarios:
        out['cases'] += 1; out['kinds'][s.kind] += 1
        if ids: out['ids'].add(s.id)
        if s.kind == 'emulator': out['reasons'][s.reason] += 1
        else: out['fingerprints'].setdefault(s.case.request['word'], set()).add(fingerprint(s))
    return out


def merge(a, b):
    words = {w: a['fingerprints'].get(w, set()) | b['fingerprints'].get(w, set()) for w in a['fingerprints'].keys() | b['fingerprints'].keys()}
    return dict(cases=a['cases'] + b['cases'], kinds=a['kinds'] + b['kinds'], reasons=a['reasons'] + b['reasons'], fingerprints=words, ids=a['ids'] | b['ids'])


def classified(job):
    """{family: summary} of one streamed architecture shard; classify raises on a case no backend accepts.
    Ids only for the manual cases: the generated ones don't depend on the sweep policy."""
    arch, family, shard = job[0], job[2], job[4]
    return {family: summary((device_cases.Scenario(family, c, *device_cases.classify(c, arch=arch), arch=arch) for c in stream(*job)), shard is None)}


@cache
def summaries(families, sample, shards=sharded.shards, arch="SM89"):
    """{family: summary} over the shards of the architecture's exact cases."""
    out = {}
    for part in sharded.run(classified, ((arch, 'exact'),), families, sample, shards=shards):
        for family, s in part.items(): out[family] = merge(out[family], s) if family in out else s
    return out


def passes():
    """{"version": 4, "architectures": {arch: {device: [fingerprint]}}}"""
    found = json.loads(PASSES.read_text()) if PASSES.exists() else dict(version=VERSION, architectures={})
    if found.get('version') != VERSION: raise ValueError(f'{PASSES}: version {found.get("version")}, expected {VERSION}')
    return found


def passed(arch): return set().union(*passes()['architectures'].get(arch, {}).values())


def write(found):
    # Atomic replacement prevents interruption from leaving a truncated evidence file.
    path = PASSES.with_suffix('.tmp')
    path.write_text(json.dumps(found, indent=1, sort_keys=True) + '\n'); path.replace(PASSES)


def record(scenarios, results, device, arch="SM89"):
    """Add the fingerprints of the GPU scenarios that passed on `device` and drop those that failed;
    scenarios without a result keep whatever was recorded."""
    if any(s.arch != arch for s in scenarios): raise ValueError('evidence architecture does not match scenarios')
    ran = {fingerprint(s): results[s.id] for s in scenarios if s.kind != 'emulator' and s.id in results}
    failed = {f for f, problems in ran.items() if problems}
    found = passes(); devices = found['architectures'].setdefault(arch, {})
    if device: devices[device] = [*devices.get(device, ()), *(f for f, problems in ran.items() if not problems)]
    found['architectures'][arch] = {d: sorted(set(fs) - failed) for d, fs in devices.items() if set(fs) - failed}
    write(found)


def verified_words(arch="SM89", sample=0, families=FAMILIES):
    """(family, word) for the words whose every GPU scenario in the current selection has passed."""
    known = passed(arch)
    if not known: return []
    return [(f, w) for f, s in summaries(tuple(families), sample, arch=arch).items() for w, fs in sorted(s['fingerprints'].items()) if fs <= known]
