"""Hardware evidence for complete shared-case families; recorded passes never skip execution."""
import ast
import hashlib
import importlib.util
import json
from functools import cache
from pathlib import Path
from sass.paths import ROOT
from tests.harness import device_cases

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
def generator():
    paths = [*sources(), *sorted(p for p in (ROOT / 'out/SM89').rglob('*') if p.is_file())]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(ROOT)).encode()); digest.update(path.read_bytes())
    return digest.hexdigest()[:24]


def hardware_expected(case):
    expected = (case.hardware or {}).get('expected', case.expected)
    return {k: v for k, v in expected.items() if not k.startswith('__') or k in ('__memory', '__counter', '__fp8_nan')}


def signature(scenarios):
    data = [(s.id, {k: v for k, v in s.case.request.items() if k != 'inspect_pcode'}, hardware_expected(s.case), s.case.hardware)
            for s in scenarios if s.kind != 'emulator']
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:24]


def words(scenarios):
    return sorted({s.case.request['word'] for s in scenarios if s.kind != 'emulator'})


def passes(): return json.loads(PASSES.read_text()) if PASSES.exists() else dict(version=1, families={})


def valid(record, scenarios):
    return (record.get('generator') == generator() and record.get('signature') == signature(scenarios)
            and record.get('words') == words(scenarios) and bool(record.get('device')) and bool(record.get('kernels')))


def record(scenarios, results, exhaustive, device, kernels):
    found = passes(); families = found.setdefault('families', {})
    for family in dict.fromkeys(s.family for s in scenarios):
        selected = [s for s in scenarios if s.family == family and s.kind != 'emulator']
        families.pop(family, None)
        if not selected or not device or any(s.id not in results or results[s.id] for s in selected): continue
        families[family] = dict(generator=generator(), signature=signature(selected), exhaustive=exhaustive,
                                cases=len(selected), words=words(selected), device=device, kernels=sorted(set(kernels)))
    # Atomic replacement prevents interruption from leaving a truncated evidence file.
    path = PASSES.with_suffix('.tmp')
    path.write_text(json.dumps(found, indent=1, sort_keys=True) + '\n'); path.replace(PASSES)


def verified_words():
    out = []
    for family, record in passes().get('families', {}).items():
        if record.get('generator') != generator(): continue
        scenarios = device_cases.selection((family,), record.get('exhaustive', False))
        if valid(record, scenarios): out += [(family, w) for w in record['words']]
    return out
