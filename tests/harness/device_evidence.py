"""Hardware evidence for shared cases: what each scenario's probe returned on the GPU, per architecture
and device, judged afresh by the current comparison against the current expectation, so a change to
either re-judges the recorded observations without a GPU. Recorded observations never skip execution.

usage: python -m tests.harness.device_evidence {unmatched,prune} [SMxx ...]"""
import argparse
import hashlib
import json
from collections import Counter
from functools import cache
from sass.paths import ROOT
from tests.cases import FAMILIES, stream
from tests.harness import device_cases, sharded

OBSERVATIONS = ROOT / 'tests/baseline/gpu_observations'
VERSION = 1


def hardware_expected(case):
    expected = (case.hardware or {}).get('expected', case.expected)
    return {k: v for k, v in expected.items() if not k.startswith('__') or k in ('__memory', '__counter', '__fp8_nan')}


def probed(s):
    """(key, captured names) of scenario s's probe built alone: the key hashes what runs on the GPU
    (target word, inputs, setup, scheduling, capture layout, launch shape), not how it is judged."""
    p = device_cases.scalar_probe([s], 'probe')
    data = (s.arch, s.kind, [f'{w:032x}' for w in p.code.words], p.constants, p.observations, p.arena.hex(),
            p.shared, p.frame_size, p.threads, p.grid)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:24], p.observations[0][1]


def path(arch): return OBSERVATIONS / f'{arch}.jsonl'


@cache
def load(file):
    if not file.exists(): return {}
    head, *lines = file.read_text().splitlines()
    head = json.loads(head)
    if head.get('version') != VERSION: raise ValueError(f'{file}: version {head.get("version")}, expected {VERSION}')
    out = {}
    for line in lines:
        key, device, provenance, values = json.loads(line)
        out.setdefault(key, []).append((head['devices'][device], provenance, tuple(None if v is None else int(v, 16) for v in values)))
    return out


def stored(arch):
    """{probe key: [(device, "observed" | "inferred", captured values, None where unknown)]}"""
    return load(path(arch))


def store(arch, entries):
    devices = sorted({d for es in entries.values() for d, _, _ in es})
    lines = sorted(json.dumps([key, devices.index(d), provenance, [None if v is None else f'{v:08x}' for v in values]], separators=(',', ':'))
                   for key, es in entries.items() for d, provenance, values in es)
    file = path(arch); file.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement prevents interruption from leaving a truncated evidence file.
    tmp = file.with_suffix('.tmp'); tmp.write_text('\n'.join([json.dumps(dict(version=VERSION, devices=devices)), *lines]) + '\n'); tmp.replace(file)
    load.cache_clear()


def record(scenarios, results, device, arch="SM89"):
    """Store what each GPU scenario that ran returned on `device`, replacing that device's earlier
    observation of the same probe. An error is no observation: it, and a scenario without a result,
    keeps whatever was recorded."""
    if any(s.arch != arch for s in scenarios): raise ValueError('evidence architecture does not match scenarios')
    entries = dict(stored(arch))
    for s in scenarios:
        found = results.get(s.id)
        if s.kind == 'emulator' or not device or not isinstance(found, dict): continue
        key, names = probed(s)
        if names != found['names']: raise ValueError(f'{s.id}: captured {found["names"]}, its probe captures {names}')
        entries[key] = [e for e in entries.get(key, ()) if e[0] != device] + [(device, 'observed', tuple(found['values']))]
    store(arch, entries)


def judge(s, names, entries):
    """(verdict, problems) of scenario s over its recorded observations: "fail" when a compared capture
    disagrees on any device, "pass" when one observation agrees in all of them, "unknown" when every
    observation lacks a compared capture, "unobserved" without observations."""
    if not entries: return 'unobserved', []
    found = [(device, list(device_cases.judged(s.case, names, values, s.arch))) for device, _, values in entries]
    problems = [f'{device}: {n}: GPU {value:#x}, reference {want:#x}' for device, js in found for n, value, want, same in js if same is False]
    return 'fail' if problems else 'pass' if any(all(same for *_, same in js) for _, js in found) else 'unknown', problems


def summary(scenarios, ids=True):
    """A family's selection, mergeable across shards: case and kind counts, emulator reasons, the
    verdicts of each word's GPU scenarios and their counts, the failures and optionally the scenario ids."""
    out = dict(cases=0, kinds=Counter(), reasons=Counter(), verdicts=Counter(), words={}, failures=[], ids=set())
    for s in scenarios:
        out['cases'] += 1; out['kinds'][s.kind] += 1
        if ids: out['ids'].add(s.id)
        if s.kind == 'emulator': out['reasons'][s.reason] += 1; continue
        key, names = probed(s)
        verdict, problems = judge(s, names, stored(s.arch).get(key, ()))
        out['verdicts'][verdict] += 1
        out['words'].setdefault(s.case.request['word'], set()).add(verdict)
        if problems: out['failures'].append((s.id, s.case.request, problems))
    return out


def merge(a, b):
    words = {w: a['words'].get(w, set()) | b['words'].get(w, set()) for w in a['words'].keys() | b['words'].keys()}
    return dict(cases=a['cases'] + b['cases'], kinds=a['kinds'] + b['kinds'], reasons=a['reasons'] + b['reasons'], verdicts=a['verdicts'] + b['verdicts'],
                words=words, failures=sorted(a['failures'] + b['failures'], key=lambda f: f[0]), ids=a['ids'] | b['ids'])


def scenarios(job):
    arch, family = job[0], job[2]
    return (device_cases.Scenario(family, c, *device_cases.classify(c, arch=arch), arch=arch) for c in stream(*job))


def classified(job):
    """{family: summary} of one streamed architecture shard; classify raises on a case no backend accepts.
    Ids only for the manual cases: the generated ones don't depend on the sweep policy."""
    return {job[2]: summary(scenarios(job), job[4] is None)}


@cache
def summaries(families, sample, shards=sharded.shards, arch="SM89"):
    """{family: summary} over the shards of the architecture's exact cases."""
    out = {}
    for part in sharded.run(classified, ((arch, 'exact'),), families, sample, shards=shards):
        for family, s in part.items(): out[family] = merge(out[family], s) if family in out else s
    return out


def verified_words(arch="SM89", sample=0, families=FAMILIES):
    """(family, word) for the words whose every GPU scenario in the current selection passes on its recorded observations."""
    if not stored(arch): return []
    return [(f, w) for f, s in summaries(tuple(families), sample, arch=arch).items() for w, vs in sorted(s['words'].items()) if vs == {'pass'}]


def verdicts(arch="SM89", sample=0, families=FAMILIES):
    """{family: {verdict: GPU scenarios}}: a comparison, expectation or reference change shows here as flipped verdicts."""
    return {f: dict(sorted(s['verdicts'].items())) for f, s in summaries(tuple(families), sample, arch=arch).items() if s['verdicts']}


def keys(job): return {probed(s)[0] for s in scenarios(job) if s.kind != 'emulator'}


def unmatched(arch):
    """Recorded probe keys that no current scenario, at any sample, runs."""
    current = set().union(*sharded.run(keys, ((arch, 'exact'),), FAMILIES, 100))
    return stored(arch).keys() - current


def main():
    p = argparse.ArgumentParser(description='Report, or explicitly prune, recorded observations of probes no current scenario runs. '
                                'Observations cannot be re-recorded without the GPU: prune only after checking the report.')
    p.add_argument('action', choices=('unmatched', 'prune'))
    p.add_argument('architectures', nargs='*', default=['SM89', 'SM90'], metavar='SMxx')
    a = p.parse_args()
    for arch in a.architectures:
        stale = unmatched(arch)
        print(f'{arch}: {len(stale):,} of {len(stored(arch)):,} recorded probes unmatched')
        if a.action == 'prune' and stale: store(arch, {k: es for k, es in stored(arch).items() if k not in stale})


if __name__ == '__main__':
    main()
