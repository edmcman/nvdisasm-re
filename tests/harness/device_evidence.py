"""Hardware evidence for complete shared-case families; recorded passes never skip execution."""
import ast
import hashlib
import importlib.util
import json
from collections import Counter
from functools import cache
from pathlib import Path
from sass.paths import ROOT
from tests.cases import stream
from tests.harness import device_cases, sampling, sharded

PASSES = ROOT / 'tests/baseline/gpu_case_passes.json'


# Modules a recorded pass does not depend on: the semantics under test never run on the GPU, and
# the cases (their requests, expected values and target words) are already in the signature.
# softfloat is the one semantics module references may use (fpformat compares FP8 NaNs with it).
INDEPENDENT = ('sass.semantics', 'tests.cases', 'tests.harness.autocases')
SHARED = ('sass.semantics.fp.softfloat',)
HARNESS = ('tests.harness.device_cases', 'tests.harness.device_setup', 'tests.harness.device_runner')


def imports(path):
    """Absolute module names imported anywhere in a source file, with `from X import y` also naming X.y."""
    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import): out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out += [node.module, *(f'{node.module}.{a.name}' for a in node.names)]
    return out


def sources():
    """Source files that build, run and compare a GPU case: the harness modules' import closure within
    sass and tests, minus INDEPENDENT."""
    seen, todo = {}, list(HARNESS)
    while todo:
        name = todo.pop()
        if name in seen or not name.startswith(('sass.', 'tests.')) or name.startswith(INDEPENDENT) and name not in SHARED: continue
        try: spec = importlib.util.find_spec(name)
        except ModuleNotFoundError: spec = None
        seen[name] = spec and spec.origin and Path(spec.origin)
        if seen[name]: todo += imports(seen[name])
    return sorted({p for p in seen.values() if p})


@cache
def generator(arch="SM89"):
    paths = [*sources(), *sorted(p for p in (ROOT / 'out' / arch).rglob('*') if p.is_file())]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(ROOT)).encode()); digest.update(path.read_bytes())
    return digest.hexdigest()[:24]


def hardware_expected(case):
    expected = (case.hardware or {}).get('expected', case.expected)
    return {k: v for k, v in expected.items() if not k.startswith('__') or k in ('__memory', '__counter', '__fp8_nan')}


def digest(s):
    request = {k: v for k, v in s.case.request.items() if k != 'inspect_pcode'}
    data = json.dumps((s.arch, s.id, request, hardware_expected(s.case), s.case.hardware), sort_keys=True)
    return int.from_bytes(hashlib.sha256(data.encode()).digest())


def summary(scenarios, ids=True):
    """What a family's evidence depends on, mergeable across shards: case and kind counts, emulator
    reasons, the words and digest sum (an order-free signature) of those that run on the GPU, and
    optionally the scenario ids."""
    out = dict(cases=0, kinds=Counter(), reasons=Counter(), digest=0, words=set(), ids=set())
    for s in scenarios:
        out['cases'] += 1; out['kinds'][s.kind] += 1
        if ids: out['ids'].add(s.id)
        if s.kind == 'emulator': out['reasons'][s.reason] += 1
        else: out['digest'] += digest(s); out['words'].add(s.case.request['word'])
    return out


def merge(a, b):
    return dict(cases=a['cases'] + b['cases'], kinds=a['kinds'] + b['kinds'], reasons=a['reasons'] + b['reasons'],
                digest=a['digest'] + b['digest'], words=a['words'] | b['words'], ids=a['ids'] | b['ids'])


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


def signature(found): return f"{found['digest'] % 2 ** 256:064x}"[:24]


def words(found): return sorted(found['words'])


def passes():
    found = json.loads(PASSES.read_text()) if PASSES.exists() else dict(families={})
    return found if 'architectures' in found else dict(version=3, architectures={'SM89': found.get('families', {})})


def valid(record, found, arch="SM89"):
    return (record.get('sampling_version') == sampling.VERSION and record.get('generator') == generator(arch) and record.get('signature') == signature(found)
            and record.get('words') == words(found) and bool(record.get('device')) and bool(record.get('kernels')))


def record(scenarios, results, sample, device, kernels, arch="SM89"):
    sample = sampling.percentage(sample)
    found = passes(); found['version'] = 3
    if any(s.arch != arch for s in scenarios): raise ValueError('evidence architecture does not match scenarios')
    families = found['architectures'].setdefault(arch, {})
    for family in dict.fromkeys(s.family for s in scenarios):
        selected = [s for s in scenarios if s.family == family and s.kind != 'emulator']
        families.pop(family, None)
        if not selected or not device or any(s.id not in results or results[s.id] for s in selected): continue
        ran = summary(selected, False)
        families[family] = dict(generator=generator(arch), signature=signature(ran), sample=sample, sampling_version=sampling.VERSION,
                                cases=len(selected), words=words(ran), device=device, kernels=sorted(set(kernels)))
    # Atomic replacement prevents interruption from leaving a truncated evidence file.
    path = PASSES.with_suffix('.tmp')
    path.write_text(json.dumps(found, indent=1, sort_keys=True) + '\n'); path.replace(PASSES)


def verified_words(arch="SM89"):
    records = {f: r for f, r in passes()['architectures'].get(arch, {}).items() if r.get('sampling_version') == sampling.VERSION and r.get('generator') == generator(arch)}
    out = []
    for sample in sorted({r['sample'] for r in records.values()}):
        families = tuple(f for f, r in records.items() if r['sample'] == sample)
        found = summaries(families, sample, arch=arch) if families else {}
        out += [(f, w) for f in families if valid(records[f], found[f], arch) for w in records[f]['words']]
    return out
